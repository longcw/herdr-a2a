"""One A2A context: one interactive coding agent session in a herdr tab."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# a flagged progress line reaches the person: a turn's first goes at once, as its ack, and the
# rest come at most this often
PROGRESS_INTERVAL = 20.0
# the longest a task stays open for the session's background work after its turn ended, in seconds
HOLD_LIMIT = 1800.0


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
    # the turn's tokens, when the transcript could be read
    usage: dict[str, Any] | None = None


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
    # the last message of a turn that ended with background work in flight, its task still open
    held: str | None = None
    last_active: float = field(default_factory=time.monotonic)
    _hold_timer: asyncio.TimerHandle | None = None

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

    def done(self, text: str, usage: dict[str, Any] | None = None) -> None:
        if self._hold_timer is not None:
            self._hold_timer.cancel()
        self.held, self._hold_timer = None, None
        self.last_active = time.monotonic()
        self.events.put_nowait(Done(text, usage))

    def hold(self, text: str) -> None:
        """A turn ended while background work runs: its words are progress, and the task stays
        open for the turns that work wakes, up to `HOLD_LIMIT`."""
        self.last_flagged = 0.0
        self.progress(text, flagged=True)
        if self._hold_timer is not None:
            self._hold_timer.cancel()
        self.held = text
        self._hold_timer = asyncio.get_running_loop().call_later(HOLD_LIMIT, self.done, text)

    def release(self) -> None:
        """Ends a held task with what it has, so a new message never waits behind it."""
        if self.held is not None:
            self.done(self.held)

    async def prompt(self, text: str) -> None:
        while not self.events.empty():
            self.events.get_nowait()
        self.last_flagged = 0.0
        self.last_active = time.monotonic()
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
        env: dict[str, str] | None = None,
    ) -> None:
        self._kind, self._cwd, self._args, self._workspace = kind, cwd, agent_args, workspace
        self._env = env or {}
        self._state = state
        self._ids: dict[str, str] = json.loads(state.read_text()) if state.exists() else {}
        self._live: dict[str, Session] = {}
        self._opening: dict[str, asyncio.Lock] = {}

    def by_session_id(self, session_id: str) -> Session | None:
        if not session_id:
            return None
        return next((s for s in self._live.values() if s.session_id == session_id), None)

    def by_name(self, name: str) -> Session | None:
        return next((s for s in self._live.values() if s.name == name), None)

    def bind(self, session: Session, session_id: str) -> None:
        """Records the id an agent chose for itself, so its context resumes it."""
        if session_id and session.session_id != session_id:
            session.session_id = session_id
            self._ids[session.context_id] = session_id
            self._save()

    def _save(self) -> None:
        self._state.parent.mkdir(parents=True, exist_ok=True)
        self._state.write_text(json.dumps(self._ids, indent=1))

    def get(self, context_id: str) -> Session | None:
        return self._live.get(context_id)

    async def open(self, context_id: str) -> Session:
        """The context's session, started in a new herdr tab if it is not running."""
        async with self._opening.setdefault(context_id, asyncio.Lock()):
            if (session := self._live.get(context_id)) is not None:
                return session
            name = "a2a-" + hashlib.sha1(context_id.encode()).hexdigest()[:8]
            # Claude takes the id it is given; Codex picks its own, which SessionStart reports
            if (session_id := self._ids.get(context_id)) is not None:
                resume = (
                    ["resume", session_id] if self._kind == "codex" else ["--resume", session_id]
                )
            elif self._kind == "codex":
                session_id, resume = "", []
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
            if session.session_id:
                self._ids[context_id] = session.session_id
                self._save()
            return session

    async def close_idle(self, idle: float) -> None:
        """Closes the tab of every session idle for `idle` seconds; its context resumes on the next message."""
        for context_id, session in list(self._live.items()):
            if session.turn.locked() or time.monotonic() - session.last_active < idle:
                continue
            del self._live[context_id]
            try:
                got = await herdr("agent", "get", session.name)
                await herdr("tab", "close", got["result"]["agent"]["tab_id"])
            except (RuntimeError, KeyError):
                pass  # already gone

    async def _start(self, name: str, resume: list[str]) -> None:
        try:
            await herdr("agent", "get", name)
            return  # still running from before a server restart
        except RuntimeError:
            pass
        cwd = str(self._cwd)
        # the server's PATH, not the herdr server's, which may hold another terminal's wrappers
        tagged = {"PATH": os.environ.get("PATH", ""), **self._env, "HERDR_A2A_SESSION": name}
        env = [f for k, v in tagged.items() for f in ("--env", f"{k}={v}")]
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
                "workspace", "create", "--cwd", cwd, "--label", self._workspace, "--no-focus", *env
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
                *env,
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
