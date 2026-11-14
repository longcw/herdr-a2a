"""One A2A context: one interactive coding agent session in a herdr tab."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# a flagged progress line reaches the person, so they come at most this often
PROGRESS_INTERVAL = 20.0


@dataclass
class Progress:
    text: str
    flagged: bool = False


@dataclass
class Question:
    """The agent waits on the person; the hook that asked holds until it is answered."""

    text: str
    data: dict[str, Any]
    answer: asyncio.Future[str]


@dataclass
class Done:
    text: str


Event = Progress | Question | Done


async def herdr(*args: str) -> dict[str, Any]:
    """Runs a herdr command and returns its JSON result."""
    proc = await asyncio.create_subprocess_exec(
        "herdr", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args)}: {err.decode().strip()}")
    return json.loads(out) if out.strip() else {}


@dataclass
class Session:
    context_id: str
    session_id: str
    name: str
    events: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)
    # one turn at a time: a task holds it from its prompt until its answer or cancel
    turn: asyncio.Lock = field(default_factory=asyncio.Lock)
    task_id: str | None = None
    asking: Question | None = None
    last_flagged: float = 0.0

    def progress(self, text: str, *, flagged: bool = False) -> None:
        if flagged and time.monotonic() - self.last_flagged < PROGRESS_INTERVAL:
            flagged = False
        if flagged:
            self.last_flagged = time.monotonic()
        self.events.put_nowait(Progress(text, flagged))

    async def ask(self, text: str, data: dict[str, Any]) -> str:
        """Puts the question to the A2A caller and waits for the answer."""
        question = Question(text, data, asyncio.get_running_loop().create_future())
        self.asking = question
        self.events.put_nowait(question)
        try:
            return await question.answer
        finally:
            self.asking = None

    def done(self, text: str) -> None:
        self.events.put_nowait(Done(text))

    async def prompt(self, text: str) -> None:
        while not self.events.empty():
            self.events.get_nowait()
        self.last_flagged = time.monotonic()
        await herdr("agent", "prompt", self.name, text)

    async def interrupt(self) -> None:
        if self.asking is not None and not self.asking.answer.done():
            self.asking.answer.cancel()
        await herdr("agent", "send-keys", self.name, "esc")

    def end_turn(self) -> None:
        self.task_id = None
        if self.turn.locked():
            self.turn.release()


class Sessions:
    """The live sessions, and the context to session id map that outlives the server."""

    def __init__(
        self,
        *,
        kind: str,
        cwd: Path,
        agent_args: list[str],
        state: Path,
        workspace: str = "a2a-server",
    ) -> None:
        self._kind, self._cwd, self._args, self._workspace = kind, cwd, agent_args, workspace
        self._state = state
        self._ids: dict[str, str] = json.loads(state.read_text()) if state.exists() else {}
        self._live: dict[str, Session] = {}
        self._opening: dict[str, asyncio.Lock] = {}

    def by_session_id(self, session_id: str) -> Session | None:
        return next((s for s in self._live.values() if s.session_id == session_id), None)

    def get(self, context_id: str) -> Session | None:
        return self._live.get(context_id)

    async def open(self, context_id: str) -> Session:
        """The context's session, started in a new herdr tab if it is not running."""
        async with self._opening.setdefault(context_id, asyncio.Lock()):
            if (session := self._live.get(context_id)) is not None:
                return session
            name = "a2a-" + hashlib.sha1(context_id.encode()).hexdigest()[:8]
            if (session_id := self._ids.get(context_id)) is not None:
                resume = ["--resume", session_id]
            else:
                session_id = str(uuid.uuid4())
                resume = ["--session-id", session_id]
            session = Session(context_id, session_id, name)
            # registered first: the agent's hooks can fire before start returns
            self._live[context_id] = session
            try:
                await self._start(name, resume)
            except BaseException:
                del self._live[context_id]
                raise
            self._ids[context_id] = session_id
            self._state.parent.mkdir(parents=True, exist_ok=True)
            self._state.write_text(json.dumps(self._ids, indent=1))
            return session

    async def _start(self, name: str, resume: list[str]) -> None:
        try:
            await herdr("agent", "get", name)
            return  # still running from before a server restart
        except RuntimeError:
            pass
        cwd = str(self._cwd)
        listed = await herdr("workspace", "list")
        workspace = next(
            (
                w["workspace_id"]
                for w in listed["result"]["workspaces"]
                if w.get("label") == self._workspace
            ),
            None,
        )
        if workspace is None:
            # a new workspace's first tab hosts this session
            made = await herdr(
                "workspace", "create", "--cwd", cwd, "--label", self._workspace, "--no-focus"
            )
            await herdr("tab", "rename", made["result"]["tab"]["tab_id"], name)
        else:
            made = await herdr(
                "tab",
                "create",
                "--workspace",
                workspace,
                "--cwd",
                cwd,
                "--label",
                name,
                "--no-focus",
            )
        pane = made["result"]["root_pane"]["pane_id"]
        try:
            await herdr(
                "agent",
                "start",
                name,
                "--kind",
                self._kind,
                "--pane",
                pane,
                "--timeout",
                "60000",
                "--",
                *resume,
                *self._args,
            )
        except RuntimeError:
            await herdr("tab", "close", made["result"]["tab"]["tab_id"])
            raise
