"""The A2A app, the hook endpoint the sessions post to, and the command line."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import claude
from .executor import SessionExecutor
from .session import Sessions

HOOK_PATH = "/hooks/claude"

# appended to the agent's system prompt, so it holds on every turn and after a resume
INSTRUCTIONS = """\
You work for a person in a voice or text chat, through another agent that relays your \
messages; the person cannot see your screen.
- End each turn with one to three plain sentences: no code, file paths, tables or markdown \
unless asked.
- Open each turn with one short sentence on what you will do; after it, speak only at a milestone.
- Keep a turn to a few minutes: for a large request, do the first step, report it, and ask \
whether to go on.
- When a choice is the person's, ask with AskUserQuestion, with short options."""


def build_app(
    *,
    url: str,
    cwd: Path,
    state: Path,
    name: str = "Claude Code",
    description: str = "A coding agent working in a real project.",
    instructions: str = INSTRUCTIONS,
    agent_args: list[str] | None = None,
    workspace: str = "a2a-server",
) -> Starlette:
    # files, not arguments: herdr refuses arguments it cannot quote for the shell
    state.parent.mkdir(parents=True, exist_ok=True)
    hooks_file = state.parent / "claude-settings.json"
    hooks_file.write_text(claude.settings(url.rstrip("/") + HOOK_PATH))
    args = ["--settings", str(hooks_file)]
    if instructions:
        instructions_file = state.parent / "instructions.md"
        instructions_file.write_text(instructions)
        args += ["--append-system-prompt-file", str(instructions_file)]
    args += agent_args or []
    sessions = Sessions(kind="claude", cwd=cwd, agent_args=args, state=state, workspace=workspace)
    hooks = claude.Hooks(sessions)
    card = AgentCard(
        name=name,
        description=description,
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        skills=[AgentSkill(id="code", name=name, description=description, tags=["code"])],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    handler = DefaultRequestHandler(
        agent_executor=SessionExecutor(sessions), task_store=InMemoryTaskStore(), agent_card=card
    )

    async def hook(request: Request) -> JSONResponse:
        return JSONResponse(await hooks.handle(await request.json()))

    return Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url="/"),
            Route(HOOK_PATH, hook, methods=["POST"]),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--url", help="the URL callers reach this server at")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="where each session works")
    parser.add_argument("--name", default="Claude Code")
    parser.add_argument("--description", default="A coding agent working in a real project.")
    parser.add_argument(
        "--instructions",
        type=Path,
        help="a file whose text replaces the built-in instructions; an empty file sends none",
    )
    parser.add_argument(
        "--workspace", default="a2a-server", help="the herdr workspace the sessions open in"
    )
    parser.add_argument("--state", type=Path, default=Path.home() / ".herdr-a2a" / "sessions.json")
    parser.add_argument("agent_args", nargs="*", help="extra arguments for claude, after --")
    ns = parser.parse_args()
    url = ns.url or f"http://{ns.host}:{ns.port}"
    app = build_app(
        url=url,
        cwd=ns.cwd.resolve(),
        state=ns.state,
        name=ns.name,
        description=ns.description,
        instructions=ns.instructions.read_text().strip() if ns.instructions else INSTRUCTIONS,
        agent_args=ns.agent_args,
        workspace=ns.workspace,
    )
    uvicorn.run(app, host=ns.host, port=ns.port)


if __name__ == "__main__":
    main()
