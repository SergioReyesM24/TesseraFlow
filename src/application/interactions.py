import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing

import structlog

from application.agent import AgentService
from application.ports import InteractionNotifier, InteractionRepository, InteractiveAgent
from domain.agent import AgentDefinition
from domain.conversations import ConversationKey
from domain.interactions import (
    InteractionCommand,
    InteractionEmission,
    InteractionOutput,
    InteractionSource,
    is_terminal_output,
)
from domain.turn_events import (
    AgentAudioDelta,
    AgentAudioInterrupted,
    AgentStreamCompleted,
    AgentStreamFailed,
)

logger = structlog.get_logger(__name__)


class InteractionQueueFullError(RuntimeError):
    """Raised when a conversation already has its configured pending command limit."""


class TurnInteractionAgent:
    """Adapt turn-based AgentService events to the modality-neutral coordinator."""

    def __init__(self, service: AgentService, definition: AgentDefinition) -> None:
        """Bind shared orchestration and its immutable model definition."""
        self._service = service
        self._definition = definition

    async def stream(self, command: InteractionCommand) -> AsyncIterator[InteractionEmission]:
        """Tag audio and non-audio events while preserving input provenance."""
        events = self._service.stream(
            command.message,
            self._definition,
            command.conversation,
            source=command.source,
        )
        async with aclosing(events):
            async for event in events:
                if isinstance(event, AgentAudioDelta | AgentAudioInterrupted):
                    yield InteractionEmission(modality="audio", event=event)
                else:
                    yield InteractionEmission(modality="text", event=event)


class TextInteractionAgent(TurnInteractionAgent):
    """Backward-compatible name for the turn-based interaction adapter."""


class ConversationCoordinator:
    """Serialize all model-driving inputs and publish their outputs durably."""

    def __init__(
        self,
        repository: InteractionRepository,
        notifier: InteractionNotifier,
        interactive_agent: InteractiveAgent,
        *,
        worker_id: str,
        reconciliation_seconds: float,
        output_reconciliation_seconds: float,
        command_timeout_seconds: float,
        worker_count: int,
        uid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        """Configure durable delivery, event-driven wakeups, and bounded workers."""
        if reconciliation_seconds <= 0 or output_reconciliation_seconds <= 0:
            raise ValueError("reconciliation intervals must be positive")
        if worker_count < 1:
            raise ValueError("worker_count must be positive")
        self._repository = repository
        self._notifier = notifier
        self._interactive_agent = interactive_agent
        self._worker_id = worker_id
        self._reconciliation_seconds = reconciliation_seconds
        self._output_reconciliation_seconds = output_reconciliation_seconds
        self._command_timeout_seconds = command_timeout_seconds
        self._worker_count = worker_count
        self._uid_factory = uid_factory
        self._stop = asyncio.Event()
        self._tasks: tuple[asyncio.Task[None], ...] = ()

    def start(self) -> None:
        """Start the process-local coordinator loop exactly once."""
        if self._tasks:
            raise RuntimeError("Conversation coordinator has already been started")
        self._tasks = tuple(
            asyncio.create_task(
                self._run(f"{self._worker_id}:{position}"),
                name=f"conversation-coordinator-{self._worker_id}-{position}",
            )
            for position in range(self._worker_count)
        )

    async def close(self) -> None:
        """Stop consumers and release commands interrupted by process shutdown."""
        self._stop.set()
        if not self._tasks:
            return
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = ()

    async def submit(
        self,
        message: str,
        conversation: ConversationKey,
        *,
        request_id: str,
        source: InteractionSource = "text_user",
    ) -> InteractionCommand:
        """Persist a user-originated text or speech turn for ordered processing."""
        command = InteractionCommand(
            command_id=str(self._uid_factory()),
            request_id=request_id,
            conversation=conversation,
            kind="user_message",
            source=source,
            message=message,
        )
        await self._repository.enqueue(command)
        logger.info(
            "interaction_command_enqueued",
            command_id=command.command_id,
            request_id=request_id,
            source=source,
        )
        return command

    async def stream_command_outputs(
        self,
        command: InteractionCommand,
    ) -> AsyncIterator[InteractionOutput]:
        """Yield and acknowledge one command's outbox until its terminal event."""
        after_sequence = 0
        async with self._notifier.subscribe_outputs(
            command.conversation.conversation_id,
            command_id=command.command_id,
        ) as subscription:
            while True:
                checkpoint = subscription.checkpoint()
                outputs = await self._repository.load_outputs(
                    command.conversation,
                    after_sequence=after_sequence,
                    command_id=command.command_id,
                )
                if not outputs:
                    await subscription.wait_for_change(
                        checkpoint,
                        self._output_reconciliation_seconds,
                    )
                    continue
                for output in outputs:
                    yield output
                    await self._repository.acknowledge(
                        output.output_id,
                        command.conversation,
                    )
                    after_sequence = output.sequence
                    if is_terminal_output(output):
                        return

    async def stream_pending_outputs(
        self,
        conversation: ConversationKey,
    ) -> AsyncIterator[InteractionOutput]:
        """Yield every undelivered event, including results completed while offline."""
        async with self._notifier.subscribe_outputs(
            conversation.conversation_id,
        ) as subscription:
            while True:
                checkpoint = subscription.checkpoint()
                outputs = await self._repository.load_outputs(
                    conversation,
                    after_sequence=0,
                )
                if not outputs:
                    await subscription.wait_for_change(
                        checkpoint,
                        self._output_reconciliation_seconds,
                    )
                    continue
                for output in outputs:
                    yield output
                    await self._repository.acknowledge(output.output_id, conversation)

    async def run_once(self, worker_id: str | None = None) -> bool:
        """Process at most one globally claimable conversation command."""
        owner_id = worker_id or self._worker_id
        lease_seconds = self._command_timeout_seconds + 30.0
        command = await self._repository.claim_next(owner_id, lease_seconds)
        if command is None:
            return False
        logger.info(
            "interaction_command_started",
            command_id=command.command_id,
            request_id=command.request_id,
            source=command.source,
            attempt=command.attempt_count,
        )
        try:
            await asyncio.wait_for(
                self._process(command, owner_id),
                timeout=self._command_timeout_seconds,
            )
        except asyncio.CancelledError:
            await self._requeue_safely(command.command_id, owner_id)
            raise
        except Exception as exc:
            await self._fail(command, owner_id, exc)
        return True

    async def _process(self, command: InteractionCommand, worker_id: str) -> None:
        """Run one model turn, persist every event, and close the leased command."""
        position = 0
        terminal_seen = False
        terminal_error_code: str | None = None
        events = self._interactive_agent.stream(command)
        async for emission in events:
            if terminal_seen:
                raise RuntimeError("Interactive agent emitted output after a terminal event")
            output = InteractionOutput(
                output_id=self._output_id(command, position),
                command_id=command.command_id,
                request_id=command.request_id,
                conversation=command.conversation,
                modality=emission.modality,
                event=emission.event,
            )
            await self._repository.append_output(output)
            position += 1
            if isinstance(
                emission.event,
                AgentStreamCompleted | AgentStreamFailed,
            ):
                terminal_seen = True
            if isinstance(emission.event, AgentStreamFailed):
                terminal_error_code = emission.event.code
        if not terminal_seen:
            raise RuntimeError("Agent command ended without a terminal event")
        if terminal_error_code is None:
            await self._repository.complete(command.command_id, worker_id)
        else:
            await self._repository.fail(
                command.command_id,
                worker_id,
                terminal_error_code,
            )
        logger.info(
            "interaction_command_completed",
            command_id=command.command_id,
            request_id=command.request_id,
        )

    async def _fail(
        self,
        command: InteractionCommand,
        worker_id: str,
        exc: Exception,
    ) -> None:
        """Publish a safe terminal error and mark an owned command failed."""
        error_code = (
            "interaction_timeout" if isinstance(exc, TimeoutError) else "interaction_failed"
        )
        logger.exception(
            "interaction_command_failed",
            command_id=command.command_id,
            request_id=command.request_id,
            error_type=type(exc).__name__,
        )
        output = InteractionOutput(
            output_id=self._output_id(command, 1_000_000),
            command_id=command.command_id,
            request_id=command.request_id,
            conversation=command.conversation,
            modality="text",
            event=AgentStreamFailed(
                code=error_code,
                message="The agent turn could not be completed.",
            ),
        )
        await self._repository.append_output(output)
        await self._repository.fail(command.command_id, worker_id, error_code)

    async def _requeue_safely(self, command_id: str, worker_id: str) -> None:
        """Best-effort release of an active command while preserving cancellation."""
        try:
            await asyncio.shield(self._repository.requeue(command_id, worker_id))
        except Exception as exc:
            logger.warning(
                "interaction_command_requeue_failed",
                command_id=command_id,
                error_type=type(exc).__name__,
            )

    def _output_id(self, command: InteractionCommand, position: int) -> str:
        """Build an idempotency key scoped to a command execution attempt."""
        return f"{command.command_id}:{command.attempt_count}:{position}"

    async def _run(self, worker_id: str) -> None:
        """Consume notifications while periodically reconciling durable state."""
        async with self._notifier.subscribe_commands() as subscription:
            while not self._stop.is_set():
                checkpoint = subscription.checkpoint()
                try:
                    worked = await self.run_once(worker_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "conversation_coordinator_consumer_failed",
                        error_type=type(exc).__name__,
                    )
                    worked = False
                if worked:
                    continue
                await subscription.wait_for_change(
                    checkpoint,
                    self._reconciliation_seconds,
                )
