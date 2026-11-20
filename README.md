# herdr-a2a

Serve interactive Claude Code or Codex sessions over the [A2A](https://a2a-protocol.org/) protocol. Each A2A context is one agent session running in its own [herdr](https://herdr.dev) tab of the `a2a-server` workspace (`--workspace` names another), so you can watch every session work and take it over by typing.

The sessions are the plain `claude` CLI on your own login. Their [hooks](https://code.claude.com/docs/en/hooks) post to this server, which turns them into A2A events:

| Claude Code hook | A2A |
|---|---|
| `PreToolUse` | `working` status, one line per tool call |
| `MessageDisplay` text that is not the answer | `working` status flagged `livekit/progress`: a turn's first line at once, then at most one per 20 s |
| `TaskCompleted` | `working` status flagged `livekit/progress` |
| `PreToolUse` on `AskUserQuestion` | `input-required`; the answer returns to Claude as the tool's `answers` |
| `PermissionRequest` | `input-required`; yes allows, any other answer denies and Claude reads it |
| `Stop` | the `answer` artifact, then `completed`; with background tasks or a one-shot wakeup in flight, flagged progress instead, and the task stays open |

A caller that shows only flagged progress, such as a voice agent, hears a line when Claude says what it is doing or finishes a step of its task list, not every tool call.

## Tasks

- One message is one task and one turn of the context's session.
- Different contexts run in parallel, each in its own session.
- A message to a context whose session is busy stays `submitted` until the turn before it ends, so every task gets an answer of its own.
- The answer to a question goes on the question's task.
- A turn that ends with background work in flight, such as a background shell, a subagent or a scheduled wakeup, keeps its task open: the turns that work wakes continue it, and the first turn with nothing in flight answers it. A new message, or 30 minutes, ends it with its last message.
- A session wakes for other things too, such as a reply from another session. Those turns have no task and reach no one, so the instructions tell Claude to say the person can ask again.
- Cancel sends Escape to the session and denies any question it waits on.
- The answer artifact's metadata carries the turn's `usage`: input, cache read, cache write and output tokens summed over its API calls, read from the session's transcript.

The context to session id map is saved in `~/.herdr-a2a/sessions.json`, so a context continues its session after a restart.

## Codex

`--kind codex` runs Codex instead. Codex's hooks carry the same payloads as Claude Code's but are shell commands, so each session gets a Codex home of its own next to the state file, with a `hooks.json` whose hooks `curl` their input to this server, the instructions as `developer_instructions`, and a link to `~/.codex/auth.json`; your own `~/.codex` is never changed, though the hooks in your `~/.codex/hooks.json` run too, so tools such as Moshi still see these sessions. `--codex-config <file>` appends TOML to that home's `config.toml`, such as a model, a reasoning effort or a permission profile. Codex picks its own session id, which the server learns from the `SessionStart` hook, and a context resumes it with `codex resume`. Codex has no hook for its messages, so a Codex turn reports its tool calls but no flagged progress, and its usage also carries Codex's `rate_limits`.

Every session's tab gets this server's `PATH`, so it runs the `claude` or `codex` the server sees, not a wrapper the herdr server inherited from another terminal.

## Run

With herdr running:

```bash
uv run herdr-a2a --cwd ~/code/my-project
```

The server listens on `http://127.0.0.1:8790`, and the agent card is at `/.well-known/agent-card.json`. Arguments after `--` go to `claude`, such as `-- --model opus`. `--close-idle <seconds>` closes the tab of a session idle that long; its context resumes the same session on its next message. `--max-sessions <n>` keeps at most n sessions open: a new one closes the least recently active idle tab, which also resumes when its context is messaged again.

## Instructions

Each session's system prompt gets the built-in instructions (`INSTRUCTIONS` in `server.py`): the person is in a voice or text chat and cannot see the screen, so the agent ends each turn in a few plain sentences, reports only milestones, keeps a turn to a few minutes, promises a later report only for a background task, and asks with `AskUserQuestion` when a choice is the person's. `--instructions <file>` replaces them, and an empty file sends none. They are appended with `--append-system-prompt`, so they hold on every turn and after a resume, and never show as a turn in the session.

## Access

There is no authentication: anyone who reaches the port drives a Claude Code session in `--cwd`. Keep it on loopback or behind a proxy that authenticates.
