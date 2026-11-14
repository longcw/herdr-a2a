"""A2A tasks on the sessions: one message is one task, one turn of the context's session."""

from __future__ import annotations

import asyncio
import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus
from google.protobuf import json_format, struct_pb2

from .session import Done, Progress, Question, Session, Sessions

logger = logging.getLogger(__name__)

# the flag that makes a working message progress a person hears, not a step they never see
PROGRESS = "livekit/progress"


class SessionExecutor(AgentExecutor):
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        assert task_id is not None and context_id is not None
        updater = TaskUpdater(event_queue, task_id, context_id)
        text = context.get_user_input()
        session = self._sessions.get(context_id)

        if session is not None and session.task_id == task_id and session.asking is not None:
            # the answer to the question this task waits on
            await updater.start_work()
            session.asking.answer.set_result(text)
        else:
            await event_queue.enqueue_event(
                Task(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                )
            )
            session = await self._sessions.open(context_id)
            # a later message waits, still submitted, for the turn before it to end
            await session.turn.acquire()
            session.task_id = task_id
            try:
                await updater.start_work()
                await session.prompt(text)
            except BaseException:
                session.end_turn()
                raise
        await self._pump(updater, session)

    async def _pump(self, updater: TaskUpdater, session: Session) -> None:
        try:
            while True:
                event = await session.events.get()
                if isinstance(event, Progress):
                    metadata = {PROGRESS: True} if event.flagged else None
                    message = updater.new_agent_message([Part(text=event.text)], metadata)
                    await updater.update_status(TaskState.TASK_STATE_WORKING, message=message)
                elif isinstance(event, Question):
                    data = json_format.ParseDict(event.data, struct_pb2.Value())
                    parts = [Part(text=event.text), Part(data=data)]
                    await updater.requires_input(updater.new_agent_message(parts))
                    return
                elif isinstance(event, Done):
                    await updater.add_artifact([Part(text=event.text)], name="answer")
                    session.end_turn()
                    await updater.complete()
                    return
        except asyncio.CancelledError:
            await self._stop(updater, session)
            raise

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id is not None and context.context_id is not None
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        session = self._sessions.get(context.context_id)
        if session is not None and session.task_id == context.task_id:
            await self._stop(updater, session)
        else:
            await updater.cancel()

    @staticmethod
    async def _stop(updater: TaskUpdater, session: Session) -> None:
        try:
            await session.interrupt()
        except Exception:
            logger.exception("could not interrupt %s", session.name)
        session.end_turn()
        try:
            await updater.cancel()
        except Exception:
            logger.debug("could not report the cancel", exc_info=True)
