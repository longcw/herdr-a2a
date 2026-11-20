"""Agent hooks, posted to this server, become the session's events; Codex's hooks mirror Claude Code's."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from .session import Session, Sessions

# a question hook waits on a person, who can take a while to answer
ASK_TIMEOUT = 3600
# how long a finished message waits for the Stop that makes it the answer, in seconds
SETTLE = 1.0
ASK_TOOL = "AskUserQuestion"
# words that approve a permission; any other answer denies it and Claude reads the words
ALLOW = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "allow", "approve", "go", "do it"}


def settings(url: str) -> str:
    """The `--settings` JSON that posts this session's hooks to `url`."""

    def hook(timeout: int) -> list[dict[str, Any]]:
        return [{"hooks": [{"type": "http", "url": url, "timeout": timeout}]}]

    asks = {event: hook(ASK_TIMEOUT) for event in ("PreToolUse", "PermissionRequest")}
    notes = {event: hook(10) for event in ("MessageDisplay", "TaskCompleted", "Stop")}
    return json.dumps({"hooks": asks | notes}, indent=1)


def codex_hooks(url: str) -> str:
    """Codex's hooks.json: command hooks that post their input here, tagged with the session's name."""

    def hook(timeout: int) -> list[dict[str, Any]]:
        command = (
            f"curl -s -m {timeout} -X POST -H 'Content-Type: application/json' --data-binary @- "
            f'"{url}?session=$HERDR_A2A_SESSION"'
        )
        return [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}]

    asks = {event: hook(ASK_TIMEOUT) for event in ("PreToolUse", "PermissionRequest")}
    notes = {event: hook(10) for event in ("SessionStart", "Stop")}
    return json.dumps({"hooks": asks | notes}, indent=1)


def turn_usage(transcript: str) -> dict[str, Any] | None:
    """Tokens the last turn used, summed over its API calls, from the session's transcript."""
    try:
        with open(transcript) as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return None
    events = [
        (i, r.get("payload") or {}) for i, r in enumerate(rows) if r.get("type") == "event_msg"
    ]
    if events:
        # a Codex rollout: running totals, so the turn is the last total minus the one before it
        start = max((i for i, p in events if p.get("type") == "task_started"), default=-1)
        counts = [(i, p) for i, p in events if p.get("type") == "token_count" and p.get("info")]
        before = [p["info"]["total_token_usage"] for i, p in counts if i < start]
        after = [p for i, p in counts if i > start]
        if not after:
            return None
        end, begin = after[-1]["info"]["total_token_usage"], before[-1] if before else {}
        d = {k: (end.get(k) or 0) - (begin.get(k) or 0) for k in end}
        contexts = [r.get("payload") or {} for r in rows if r.get("type") == "turn_context"]
        usage = {
            "input_tokens": d.get("input_tokens", 0) - d.get("cached_input_tokens", 0),
            "cache_read_input_tokens": d.get("cached_input_tokens", 0),
            "cache_creation_input_tokens": d.get("cache_write_input_tokens", 0),
            "output_tokens": d.get("output_tokens", 0),
            "calls": len(after),
            "model": contexts[-1].get("model", "") if contexts else "",
        }
        if limits := after[-1].get("rate_limits"):
            usage["rate_limits"] = limits
        return usage
    # the turn starts at the person's prompt; tool results are user entries too
    start = max(
        (
            i
            for i, r in enumerate(rows)
            if r.get("type") == "user"
            and not r.get("isMeta")
            and r.get("toolUseResult") is None
            and not any(
                isinstance(c, dict) and c.get("type") == "tool_result"
                for c in (r.get("message") or {}).get("content") or []
            )
        ),
        default=-1,
    )
    # one API call can span several entries that repeat its usage
    calls: dict[str, dict[str, Any]] = {}
    for r in rows[start + 1 :]:
        message = r.get("message") or {}
        if r.get("type") == "assistant" and message.get("usage"):
            calls[message.get("id") or str(len(calls))] = message
    if not calls:
        return None
    keys = (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
    )
    usage: dict[str, Any] = {k: sum(m["usage"].get(k) or 0 for m in calls.values()) for k in keys}
    return usage | {"calls": len(calls), "model": next(reversed(calls.values())).get("model", "")}


def describe(tool: str, tool_input: dict[str, Any]) -> str:
    """One short line for a tool call."""
    detail = (
        tool_input.get("description")
        or tool_input.get("command")
        or tool_input.get("file_path")
        or tool_input.get("pattern")
        or tool_input.get("url")
        or ""
    )
    line = f"{tool}: {detail}".strip().rstrip(":")
    return line if len(line) <= 120 else line[:117] + "..."


class Hooks:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions
        # per session, the assistant message shown so far
        self._shown: dict[str, list[str]] = {}
        # per session, a finished message waiting to become progress unless Stop makes it the answer
        self._settling: dict[str, asyncio.Task[None]] = {}

    def _flush(self, session: Session) -> None:
        """The message shown so far, as progress a person hears."""
        if task := self._settling.pop(session.session_id, None):
            task.cancel()
        if text := "".join(self._shown.pop(session.session_id, [])).strip():
            session.progress(text, flagged=True)

    async def _settle(self, session: Session) -> None:
        await asyncio.sleep(SETTLE)
        self._settling.pop(session.session_id, None)
        self._flush(session)

    async def handle(self, payload: dict[str, Any], name: str | None = None) -> dict[str, Any]:
        # a Codex session is known by name until its first hook tells its id
        session = self._sessions.by_session_id(payload.get("session_id", "")) or (
            self._sessions.by_name(name) if name else None
        )
        event = payload.get("hook_event_name")
        if session is not None and event == "SessionStart":
            self._sessions.bind(session, payload.get("session_id", ""))
        if session is None or session.task_id is None:
            return {}
        if event == "MessageDisplay":
            # a message can show before or after its tool call starts, so a finished one waits
            # briefly for the Stop that would make it the answer
            if payload.get("index") == 0:
                self._flush(session)
            self._shown.setdefault(session.session_id, []).append(payload.get("delta", ""))
            if payload.get("final", True):
                self._settling[session.session_id] = asyncio.create_task(self._settle(session))
            return {}
        if event == "PreToolUse":
            tool, tool_input = payload.get("tool_name", ""), payload.get("tool_input") or {}
            self._flush(session)
            if tool == ASK_TOOL:
                return await self._ask_user(session, tool_input)
            session.progress(describe(tool, tool_input))
            return {}
        if event == "TaskCompleted":
            if subject := payload.get("task_subject"):
                session.progress(f"Done: {subject}", flagged=True)
            return {}
        if event == "PermissionRequest":
            return await self._permission(session, payload)
        if event == "Stop":
            if task := self._settling.pop(session.session_id, None):
                task.cancel()
            self._shown.pop(session.session_id, None)
            text = payload.get("last_assistant_message") or ""
            # work that wakes the session later keeps the task open; a recurring cron never ends
            crons = [c for c in payload.get("session_crons") or [] if not c.get("recurring")]
            if payload.get("background_tasks") or crons:
                session.hold(text)
            else:
                path = payload.get("transcript_path") or ""
                # the agent can still be writing the turn's last call when Stop fires
                size, waited = -1, 0.0
                while waited < 3 and os.path.exists(path) and os.path.getsize(path) != size:
                    size = os.path.getsize(path)
                    await asyncio.sleep(0.3)
                    waited += 0.3
                session.done(text, turn_usage(path))
        return {}

    async def _ask_user(self, session: Session, tool_input: dict[str, Any]) -> dict[str, Any]:
        questions = tool_input.get("questions") or []
        text = "\n".join(_question_line(q) for q in questions)
        answer = await session.ask(text, {"kind": "ask_user_question", "questions": questions})
        # one spoken answer answers every question asked together
        answers = {q["question"]: answer for q in questions if "question" in q}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {**tool_input, "answers": answers},
            }
        }

    async def _permission(self, session: Session, payload: dict[str, Any]) -> dict[str, Any]:
        tool, tool_input = payload.get("tool_name", ""), payload.get("tool_input") or {}
        text = f"Claude wants to run {describe(tool, tool_input)}. Allow it?"
        answer = await session.ask(
            text, {"kind": "permission_request", "tool": tool, "input": tool_input}
        )
        words = answer.strip().lower().rstrip(".!")
        if words in ALLOW or words.split(",")[0].split(" ")[0] in ALLOW:
            decision: dict[str, Any] = {"behavior": "allow"}
        else:
            decision = {"behavior": "deny", "message": f"The person answered: {answer}"}
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def _question_line(question: dict[str, Any]) -> str:
    options = [o.get("label", "") for o in question.get("options") or []]
    line = question.get("question", "")
    return f"{line} ({', '.join(options)})" if options else line
