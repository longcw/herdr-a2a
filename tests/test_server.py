import asyncio
import uuid

import httpx
import pytest

from herdr_a2a import claude, server, session


@pytest.fixture
def herdr(monkeypatch):
    """Fakes herdr: records each command, sets `prompted` on every prompt."""
    calls: list[tuple[str, ...]] = []
    prompted = asyncio.Queue()

    async def fake(*args: str):
        calls.append(args)
        if args[:2] == ("agent", "get"):
            raise RuntimeError("no such agent")
        if args[:2] == ("workspace", "list"):
            return {"result": {"workspaces": []}}
        if args[:2] == ("workspace", "create"):
            return {"result": {"tab": {"tab_id": "w1:t1"}, "root_pane": {"pane_id": "w1:p1"}}}
        if args[:2] == ("agent", "prompt"):
            prompted.put_nowait(args[3])
        return {}

    monkeypatch.setattr(session, "herdr", fake)
    return calls, prompted


@pytest.fixture
def client(tmp_path):
    app = server.build_app(url="http://test", cwd=tmp_path, state=tmp_path / "s.json")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def session_id(herdr_calls) -> str:
    start = next(c for c in herdr_calls if c[:2] == ("agent", "start"))
    return start[start.index("--session-id") + 1]


async def send(client, text, *, context_id=None, task_id=None):
    message = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": text}]}
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}}
    r = await client.post("/", json=body, headers={"A2A-Version": "1.0"}, timeout=10)
    return r.json()["result"]["task"]


async def hook(client, sid, event, **fields):
    body = {"session_id": sid, "hook_event_name": event, **fields}
    return (await client.post("/hooks/claude", json=body, timeout=10)).json()


def texts(task, flagged):
    return [
        m["parts"][0]["text"]
        for m in task.get("history", [])
        if m["role"] == "ROLE_AGENT"
        and bool(m.get("metadata", {}).get("livekit/progress")) == flagged
    ]


async def test_a_turn_streams_steps_flags_its_opening_line_and_answers_with_its_last_message(
    herdr, client, monkeypatch
):
    monkeypatch.setattr(claude, "SETTLE", 0.01)
    calls, prompted = herdr
    sending = asyncio.create_task(send(client, "fix the test", context_id="c1"))
    assert await prompted.get() == "fix the test"
    sid = session_id(calls)
    # Claude Code shows a message once its tool call has started
    for tool, tool_input, line in [
        ("Read", {"file_path": "a.py"}, "I'll look.\n"),
        ("Bash", {"command": "pytest"}, "Found it.\n"),
    ]:
        await hook(client, sid, "PreToolUse", tool_name=tool, tool_input=tool_input)
        await hook(client, sid, "MessageDisplay", index=0, final=True, delta=line)
        await asyncio.sleep(0.05)
    await hook(client, sid, "MessageDisplay", index=0, final=True, delta="Fixed.\n")
    await hook(client, sid, "Stop", last_assistant_message="Fixed.")
    task = await sending

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"][0]["text"] == "Fixed."
    # the opening line goes at once; the next, within the interval, only to who reads steps
    assert texts(task, flagged=True) == ["I'll look."]
    assert texts(task, flagged=False) == ["Read: a.py", "Bash: pytest", "Found it."]


async def test_a_question_is_input_required_and_its_answer_reaches_claude(herdr, client):
    calls, prompted = herdr
    sending = asyncio.create_task(send(client, "add a flag", context_id="c1"))
    await prompted.get()
    sid = session_id(calls)
    question = {"question": "Which name?", "options": [{"label": "fast"}, {"label": "quick"}]}
    asking = asyncio.create_task(
        hook(
            client,
            sid,
            "PreToolUse",
            tool_name="AskUserQuestion",
            tool_input={"questions": [question]},
        )
    )
    task = await sending
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert task["status"]["message"]["parts"][0]["text"] == "Which name? (fast, quick)"

    answering = asyncio.create_task(send(client, "quick", task_id=task["id"]))
    output = (await asking)["hookSpecificOutput"]
    assert output["permissionDecision"] == "allow"
    assert output["updatedInput"]["answers"] == {"Which name?": "quick"}
    await hook(client, sid, "Stop", last_assistant_message="Added --quick.")
    assert (await answering)["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_a_message_to_a_busy_session_waits_for_the_turn_before_it(herdr, client):
    calls, prompted = herdr
    first = asyncio.create_task(send(client, "one", context_id="c1"))
    await prompted.get()
    second = asyncio.create_task(send(client, "two", context_id="c1"))
    await asyncio.sleep(0.2)
    assert prompted.empty()

    sid = session_id(calls)
    await hook(client, sid, "Stop", last_assistant_message="did one")
    assert (await first)["artifacts"][0]["parts"][0]["text"] == "did one"
    assert await prompted.get() == "two"
    await hook(client, sid, "Stop", last_assistant_message="did two")
    assert (await second)["artifacts"][0]["parts"][0]["text"] == "did two"
