import asyncio
import json
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


BACKGROUND = [{"id": "b1", "type": "shell", "status": "running", "command": "make"}]


async def test_a_turn_that_leaves_background_work_keeps_its_task_until_the_work_ends(herdr, client):
    calls, prompted = herdr
    sending = asyncio.create_task(send(client, "build it", context_id="c1"))
    await prompted.get()
    sid = session_id(calls)
    await hook(client, sid, "Stop", last_assistant_message="Building.", background_tasks=BACKGROUND)
    await asyncio.sleep(0.2)
    assert not sending.done()

    # the turn the finished build wakes answers the task
    await hook(client, sid, "Stop", last_assistant_message="Built.", background_tasks=[])
    task = await sending
    assert task["artifacts"][0]["parts"][0]["text"] == "Built."
    assert texts(task, flagged=True) == ["Building."]


async def test_a_new_message_ends_a_held_task_with_its_last_message(herdr, client):
    calls, prompted = herdr
    first = asyncio.create_task(send(client, "build it", context_id="c1"))
    await prompted.get()
    sid = session_id(calls)
    await hook(client, sid, "Stop", last_assistant_message="Building.", background_tasks=BACKGROUND)
    await asyncio.sleep(0.2)

    second = asyncio.create_task(send(client, "is it done?", context_id="c1"))
    assert (await first)["artifacts"][0]["parts"][0]["text"] == "Building."
    assert await prompted.get() == "is it done?"
    await hook(client, sid, "Stop", last_assistant_message="Not yet.")
    assert (await second)["artifacts"][0]["parts"][0]["text"] == "Not yet."


async def test_the_answer_carries_the_turn_usage_from_the_transcript(herdr, client, tmp_path):
    calls, prompted = herdr
    sending = asyncio.create_task(send(client, "rank these", context_id="c1"))
    await prompted.get()
    rows = [
        {"type": "user", "message": {"content": "earlier"}},
        {"type": "assistant", "message": {"id": "m0", "usage": {"output_tokens": 99}}},
        {"type": "user", "message": {"content": "rank these"}},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": "sonnet",
                "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7},
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": "sonnet",
                "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7},
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "model": "sonnet",
                "usage": {"input_tokens": 1, "output_tokens": 3},
            },
        },
    ]
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows))
    await hook(
        client,
        session_id(calls),
        "Stop",
        last_assistant_message="{}",
        transcript_path=str(transcript),
    )
    usage = (await sending)["artifacts"][0]["metadata"]["usage"]
    assert usage == {
        "input_tokens": 6,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 0,
        "output_tokens": 10,
        "calls": 2,
        "model": "sonnet",
    }


async def test_an_idle_session_closes_its_tab_and_resumes_on_the_next_message(
    tmp_path, monkeypatch
):
    calls = []

    async def fake(*args):
        calls.append(args)
        if args[:2] == ("agent", "get"):
            if any(c[:2] == ("tab", "close") for c in calls) or not any(
                c[:2] == ("agent", "start") for c in calls
            ):
                raise RuntimeError("no such agent")
            return {"result": {"agent": {"tab_id": "w1:t1"}}}
        if args[:2] == ("workspace", "list"):
            return {"result": {"workspaces": []}}
        if args[:2] == ("workspace", "create"):
            return {"result": {"tab": {"tab_id": "w1:t1"}, "root_pane": {"pane_id": "w1:p1"}}}
        return {}

    monkeypatch.setattr(session, "herdr", fake)
    sessions = session.Sessions(
        kind="claude", cwd=tmp_path, agent_args=[], state=tmp_path / "s.json"
    )
    first = await sessions.open("c1")
    await sessions.close_idle(3600)
    assert sessions.get("c1") is first
    await sessions.close_idle(0)
    assert sessions.get("c1") is None and ("tab", "close", "w1:t1") in calls
    await sessions.open("c1")
    start = [c for c in calls if c[:2] == ("agent", "start")][-1]
    assert start[start.index("--resume") + 1] == first.session_id
