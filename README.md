# herdr-a2a

Serve interactive Claude Code sessions over the [A2A](https://a2a-protocol.org/) protocol. Each A2A context is one Claude Code session running in its own [herdr](https://herdr.dev) tab of the `a2a-server` workspace (`--workspace` names another), so you can watch every session work and take it over by typing.

The sessions are the plain `claude` CLI on your own login. Their [hooks](https://code.claude.com/docs/en/hooks) post to this server, which turns them into A2A events:

| Claude Code hook | A2A |
|---|---|
| `PreToolUse` | `working` status, one line per tool call |
| `MessageDisplay` text that is not the answer | `working` status flagged `livekit/progress`: a turn's first line at once, then at most one per 20 s |
| `TaskCompleted` | `working` status flagged `livekit/progress` |
| `PreToolUse` on `AskUserQuestion` | `input-required`; the answer returns to Claude as the tool's `answers` |
| `PermissionRequest` | `input-required`; yes allows, any other answer denies and Claude reads it |
| `Stop` | the `answer` artifact, then `completed` |

A caller that shows only flagged progress, such as a voice agent, hears a line when Claude says what it is doing or finishes a step of its task list, not every tool call.

## Tasks

- One message is one task and one turn of the context's session.
- Different contexts run in parallel, each in its own session.
- A message to a context whose session is busy stays `submitted` until the turn before it ends, so every task gets an answer of its own.
- The answer to a question goes on the question's task.
- Cancel sends Escape to the session and denies any question it waits on.

The context to session id map is saved in `~/.herdr-a2a/sessions.json`, so a context continues its session after a restart.

## Run

With herdr running:

```bash
uv run herdr-a2a --cwd ~/code/my-project
```

The server listens on `http://127.0.0.1:8790`, and the agent card is at `/.well-known/agent-card.json`. Arguments after `--` go to `claude`, such as `-- --model opus`.

## Instructions

Each session's system prompt gets the built-in instructions (`INSTRUCTIONS` in `server.py`): the person is in a voice or text chat and cannot see the screen, so the agent ends each turn in a few plain sentences, reports only milestones, keeps a turn to a few minutes and asks with `AskUserQuestion` when a choice is the person's. `--instructions <file>` replaces them, and an empty file sends none. They are appended with `--append-system-prompt`, so they hold on every turn and after a resume, and never show as a turn in the session.

## Access

There is no authentication: anyone who reaches the port drives a Claude Code session in `--cwd`. Keep it on loopback or behind a proxy that authenticates.
