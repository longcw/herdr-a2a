"""Claude Code: its hooks, posted to this server, become the session's events."""

from __future__ import annotations

import json
from typing import Any

from .session import Session, Sessions

# a question hook waits on a person, who can take a while to answer
ASK_TIMEOUT = 3600
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
        # per session, the assistant message shown so far: progress once a tool call follows it
        self._shown: dict[str, list[str]] = {}

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._sessions.by_session_id(payload.get("session_id", ""))
        if session is None or session.task_id is None:
            return {}
        event = payload.get("hook_event_name")
        if event == "MessageDisplay":
            if payload.get("index") == 0:
                self._shown[session.session_id] = []
            self._shown.setdefault(session.session_id, []).append(payload.get("delta", ""))
            return {}
        if event == "PreToolUse":
            tool, tool_input = payload.get("tool_name", ""), payload.get("tool_input") or {}
            if text := "".join(self._shown.pop(session.session_id, [])).strip():
                session.progress(text, flagged=True)
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
            self._shown.pop(session.session_id, None)
            session.done(payload.get("last_assistant_message") or "")
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
