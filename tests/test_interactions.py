import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID

from application.interactions import ConversationCoordinator, TurnInteractionAgent
from domain.agent import AgentDefinition, AgentResult
from domain.conversations import ConversationKey
from domain.interactions import (
    InteractionCommand,
    InteractionEmission,
    InteractionOutput,
    InteractionSource,
)
from domain.turn_events import AgentAudioDelta, AgentStreamCompleted, AgentTextDelta


class InMemorySubscription:
    """Provide race-safe notification generations for coordinator tests."""

    def __init__(self) -> None:
        """Initialize one process-local signal."""
        self.generation = 0
        self.event = asyncio.Event()

    def checkpoint(self) -> int:
        """Return the current notification generation."""
        return self.generation

    async def wait_for_change(self, checkpoint: int, deadline_seconds: float) -> None:
        """Wait for a publication without making tests depend on polling."""
        if self.generation != checkpoint:
            return
        self.event.clear()
        if self.generation != checkpoint:
            return
        await asyncio.wait_for(self.event.wait(), timeout=deadline_seconds)

    def publish(self) -> None:
        """Advance the signal and wake its observer."""
        self.generation += 1
        self.event.set()


class InMemoryInteractionNotifier:
    """Fan interaction changes out to deterministic in-memory subscriptions."""

    def __init__(self) -> None:
        """Initialize command and scoped output observer registries."""
        self.command_subscriptions: set[InMemorySubscription] = set()
        self.output_subscriptions: dict[tuple[str, str | None], set[InMemorySubscription]] = {}
        self.command_subscribed = asyncio.Event()
        self.two_commands_subscribed = asyncio.Event()
        self.output_subscribed = asyncio.Event()

    async def start(self) -> None:
        """Match the production lifecycle without external resources."""

    async def close(self) -> None:
        """Match the production lifecycle without external resources."""

    @asynccontextmanager
    async def subscribe_commands(self) -> AsyncIterator[InMemorySubscription]:
        """Register one command observer for its task lifetime."""
        subscription = InMemorySubscription()
        self.command_subscriptions.add(subscription)
        self.command_subscribed.set()
        if len(self.command_subscriptions) >= 2:
            self.two_commands_subscribed.set()
        try:
            yield subscription
        finally:
            self.command_subscriptions.discard(subscription)

    @asynccontextmanager
    async def subscribe_outputs(
        self,
        conversation_id: str,
        *,
        command_id: str | None = None,
    ) -> AsyncIterator[InMemorySubscription]:
        """Register one command- or conversation-scoped output observer."""
        key = (conversation_id, command_id)
        subscription = InMemorySubscription()
        subscriptions = self.output_subscriptions.setdefault(key, set())
        subscriptions.add(subscription)
        self.output_subscribed.set()
        try:
            yield subscription
        finally:
            subscriptions.discard(subscription)
            if not subscriptions:
                self.output_subscriptions.pop(key, None)

    def publish_command(self) -> None:
        """Wake every competing local command consumer."""
        for subscription in tuple(self.command_subscriptions):
            subscription.publish()

    def publish_output(self, output: InteractionOutput) -> None:
        """Wake command-specific and conversation-wide output observers."""
        keys = (
            (output.conversation.conversation_id, output.command_id),
            (output.conversation.conversation_id, None),
        )
        for key in keys:
            for subscription in tuple(self.output_subscriptions.get(key, ())):
                subscription.publish()


class InMemoryInteractionRepository:
    """Model ordered inbox and durable outbox semantics for coordinator tests."""

    def __init__(self, notifier: InMemoryInteractionNotifier | None = None) -> None:
        """Initialize deterministic command, claim, output, and delivery state."""
        self.notifier = notifier
        self.commands: list[InteractionCommand] = []
        self.claims: dict[str, str] = {}
        self.outputs: list[InteractionOutput] = []
        self.delivered: set[str] = set()
        self._lock = asyncio.Lock()

    async def enqueue(self, command: InteractionCommand) -> None:
        """Append a command once using its stable identifier."""
        async with self._lock:
            if all(item.command_id != command.command_id for item in self.commands):
                self.commands.append(command)
        if self.notifier is not None:
            self.notifier.publish_command()

    async def claim_next(
        self,
        worker_id: str,
        lease_seconds: float,
    ) -> InteractionCommand | None:
        """Claim only the earliest unfinished command in each conversation."""
        del lease_seconds
        async with self._lock:
            for position, command in enumerate(self.commands):
                if command.status != "queued":
                    continue
                earlier = self.commands[:position]
                if any(
                    item.conversation.conversation_id == command.conversation.conversation_id
                    and item.status in {"queued", "running"}
                    for item in earlier
                ):
                    continue
                claimed = replace(
                    command,
                    status="running",
                    attempt_count=command.attempt_count + 1,
                )
                self.commands[position] = claimed
                self.claims[claimed.command_id] = worker_id
                return claimed
        return None

    async def append_output(self, output: InteractionOutput) -> None:
        """Append an output once and assign its in-memory global sequence."""
        async with self._lock:
            if any(item.output_id == output.output_id for item in self.outputs):
                return
            persisted = replace(output, sequence=len(self.outputs) + 1)
            self.outputs.append(persisted)
        if self.notifier is not None:
            self.notifier.publish_output(persisted)

    async def complete(self, command_id: str, worker_id: str) -> None:
        """Complete the command held by the expected coordinator."""
        await self._finish(command_id, worker_id, "completed")

    async def fail(self, command_id: str, worker_id: str, error_code: str) -> None:
        """Fail the command held by the expected coordinator."""
        del error_code
        await self._finish(command_id, worker_id, "failed")

    async def requeue(self, command_id: str, worker_id: str) -> None:
        """Return an interrupted command to its original queue position."""
        await self._finish(command_id, worker_id, "queued")

    async def load_outputs(
        self,
        conversation: ConversationKey,
        *,
        after_sequence: int,
        command_id: str | None = None,
        limit: int = 100,
    ) -> tuple[InteractionOutput, ...]:
        """Load undelivered outputs through the exact ownership key."""
        return tuple(
            output
            for output in self.outputs
            if output.conversation == conversation
            and output.sequence > after_sequence
            and output.output_id not in self.delivered
            and (command_id is None or output.command_id == command_id)
        )[:limit]

    async def acknowledge(self, output_id: str, conversation: ConversationKey) -> None:
        """Acknowledge an output only through its owning conversation."""
        output = next(item for item in self.outputs if item.output_id == output_id)
        assert output.conversation == conversation
        self.delivered.add(output_id)

    async def _finish(self, command_id: str, worker_id: str, status: str) -> None:
        """Apply one terminal or release transition to an active claim."""
        async with self._lock:
            assert self.claims.pop(command_id) == worker_id
            position = next(
                index
                for index, command in enumerate(self.commands)
                if command.command_id == command_id
            )
            self.commands[position] = replace(self.commands[position], status=status)


class BlockingAgentService:
    """Capture command order and expose accidental overlapping model executions."""

    def __init__(self) -> None:
        """Block the first execution until the concurrency assertion is made."""
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.two_started = asyncio.Event()
        self.calls: list[tuple[str, InteractionSource]] = []
        self.active = 0
        self.max_active = 0

    async def stream(
        self,
        command: InteractionCommand,
    ) -> AsyncIterator[InteractionEmission]:
        """Emit one streaming response while tracking concurrent invocations."""
        self.calls.append((command.message, command.source))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        if self.active >= 2:
            self.two_started.set()
        try:
            yield InteractionEmission(
                modality="text",
                event=AgentTextDelta(text="Procesando"),
            )
            await self.release.wait()
            yield InteractionEmission(
                modality="text",
                event=AgentStreamCompleted(
                    result=AgentResult(
                        answer="Respuesta",
                        response_id=f"response-{len(self.calls)}",
                        conversation_id=command.conversation.conversation_id,
                    )
                ),
            )
        finally:
            self.active -= 1


class AudioEventAgentService:
    """Emit mixed media through the AgentService-facing adapter contract."""

    async def stream(
        self,
        message: str,
        definition: AgentDefinition,
        conversation: ConversationKey,
        *,
        source: InteractionSource,
        turn_id: str,
    ) -> AsyncIterator[AgentAudioDelta | AgentTextDelta | AgentStreamCompleted]:
        """Yield one audio fragment, transcription, and terminal result."""
        del message, definition, source, turn_id
        yield AgentAudioDelta(data=b"pcm", mime_type="audio/pcm;rate=24000")
        yield AgentTextDelta(text="Hola")
        yield AgentStreamCompleted(
            result=AgentResult(
                answer="Hola",
                response_id="audio-1",
                conversation_id=conversation.conversation_id,
            )
        )


def key() -> ConversationKey:
    """Return the owned conversation shared by race-condition tests."""
    return ConversationKey(conversation_id="conversation-1", user_id="user-1")


async def test_turn_adapter_tags_audio_without_provider_branches() -> None:
    """Derive output modality from neutral events for every configured gateway."""
    adapter = TurnInteractionAgent(
        AudioEventAgentService(),  # type: ignore[arg-type]
        AgentDefinition(model="audio-model", instructions="Speak", tool_names=()),
    )
    command = InteractionCommand(
        command_id="command-1",
        request_id="request-1",
        conversation=key(),
        kind="user_message",
        source="text_user",
        message="Hola",
    )

    emissions = [emission async for emission in adapter.stream(command)]

    assert [emission.modality for emission in emissions] == ["audio", "text", "text"]


def coordinator(
    repository: InMemoryInteractionRepository,
    agent: BlockingAgentService,
    notifier: InMemoryInteractionNotifier | None = None,
    *,
    worker_count: int = 1,
) -> ConversationCoordinator:
    """Build a deterministic coordinator without starting its polling loop."""
    identifiers = iter(UUID(int=value) for value in range(1, 10))
    return ConversationCoordinator(
        repository,
        notifier or InMemoryInteractionNotifier(),
        agent,
        worker_id="coordinator-1",
        reconciliation_seconds=30,
        output_reconciliation_seconds=30,
        command_timeout_seconds=5,
        worker_count=worker_count,
        uid_factory=lambda: next(identifiers),
    )


async def test_user_message_and_worker_completion_never_overlap_one_conversation() -> None:
    """Queue a simultaneous result behind the model turn already holding the chat."""
    repository = InMemoryInteractionRepository()
    agent = BlockingAgentService()
    service = coordinator(repository, agent)
    await service.submit("Mensaje del usuario", key(), request_id="request-user")
    await repository.enqueue(
        InteractionCommand(
            command_id="a2a-result:job-1",
            request_id="job-1",
            conversation=key(),
            kind="worker_completed",
            source="worker_agent",
            message='{"protocol":"tesseraflow.a2a.result","answer":"Saldo"}',
            causation_id="job-1",
        )
    )

    first = asyncio.create_task(service.run_once())
    await agent.started.wait()
    assert await service.run_once() is False
    assert agent.max_active == 1

    agent.release.set()
    assert await first is True
    assert await service.run_once() is True

    assert agent.max_active == 1
    assert agent.calls == [
        ("Mensaje del usuario", "text_user"),
        ('{"protocol":"tesseraflow.a2a.result","answer":"Saldo"}', "worker_agent"),
    ]
    assert [command.status for command in repository.commands] == ["completed", "completed"]


async def test_speech_source_uses_the_same_serialized_command_path() -> None:
    """Reserve STS-originated turns without adding transport logic to the coordinator."""
    repository = InMemoryInteractionRepository()
    agent = BlockingAgentService()
    agent.release.set()
    service = coordinator(repository, agent)

    await service.submit(
        "Transcripción del turno de voz",
        key(),
        request_id="speech-turn-1",
        source="speech_user",
    )
    assert await service.run_once() is True

    assert agent.calls == [("Transcripción del turno de voz", "speech_user")]
    assert repository.outputs[-1].modality == "text"


async def test_user_turn_waits_when_worker_result_is_already_being_processed() -> None:
    """Finish a proactive model turn before consuming a concurrent user message."""
    repository = InMemoryInteractionRepository()
    agent = BlockingAgentService()
    service = coordinator(repository, agent)
    await repository.enqueue(
        InteractionCommand(
            command_id="a2a-result:job-1",
            request_id="job-1",
            conversation=key(),
            kind="worker_completed",
            source="worker_agent",
            message="Resultado worker",
            causation_id="job-1",
        )
    )

    proactive = asyncio.create_task(service.run_once())
    await agent.started.wait()
    await service.submit("Mensaje simultáneo", key(), request_id="request-user")
    assert await service.run_once() is False

    agent.release.set()
    assert await proactive is True
    assert await service.run_once() is True
    assert agent.calls == [
        ("Resultado worker", "worker_agent"),
        ("Mensaje simultáneo", "text_user"),
    ]
    assert agent.max_active == 1


async def test_unacknowledged_output_is_available_after_reconnection() -> None:
    """Redeliver an event when a socket disconnects before acknowledging its send."""
    repository = InMemoryInteractionRepository()
    agent = BlockingAgentService()
    agent.release.set()
    service = coordinator(repository, agent)
    await service.submit("Mensaje del usuario", key(), request_id="request-user")
    assert await service.run_once() is True

    first_connection = service.stream_pending_outputs(key())
    first = await anext(first_connection)
    await first_connection.aclose()
    assert first.output_id not in repository.delivered

    second_connection = service.stream_pending_outputs(key())
    repeated = await anext(second_connection)
    next_output = await anext(second_connection)
    await second_connection.aclose()

    assert repeated.output_id == first.output_id
    assert first.output_id in repository.delivered
    assert isinstance(next_output.event, AgentStreamCompleted)


async def test_output_notification_wakes_stream_without_reconciliation_delay() -> None:
    """Deliver a newly persisted event through its scoped notification."""
    notifier = InMemoryInteractionNotifier()
    repository = InMemoryInteractionRepository(notifier)
    agent = BlockingAgentService()
    service = coordinator(repository, agent, notifier)
    command = await service.submit("Mensaje", key(), request_id="request-1")
    stream = service.stream_command_outputs(command)
    pending_output = asyncio.create_task(anext(stream))
    await notifier.output_subscribed.wait()

    await repository.append_output(
        InteractionOutput(
            output_id="output-1",
            command_id=command.command_id,
            request_id=command.request_id,
            conversation=key(),
            modality="text",
            event=AgentTextDelta(text="Inmediato"),
        )
    )

    async with asyncio.timeout(0.5):
        output = await pending_output
    await stream.aclose()
    assert output.event == AgentTextDelta(text="Inmediato")


async def test_command_notification_wakes_background_consumer_immediately() -> None:
    """Start a queued turn from NOTIFY semantics rather than the long fallback."""
    notifier = InMemoryInteractionNotifier()
    repository = InMemoryInteractionRepository(notifier)
    agent = BlockingAgentService()
    agent.release.set()
    service = coordinator(repository, agent, notifier)
    service.start()
    await notifier.command_subscribed.wait()

    try:
        await service.submit("Mensaje", key(), request_id="request-1")
        async with asyncio.timeout(0.5):
            await agent.started.wait()
    finally:
        await service.close()

    assert agent.calls == [("Mensaje", "text_user")]


async def test_worker_pool_processes_different_conversations_concurrently() -> None:
    """Bound parallel turns while repository claims still serialize each chat."""
    notifier = InMemoryInteractionNotifier()
    repository = InMemoryInteractionRepository(notifier)
    agent = BlockingAgentService()
    service = coordinator(repository, agent, notifier, worker_count=2)
    other_key = ConversationKey(conversation_id="conversation-2", user_id="user-1")
    service.start()
    async with asyncio.timeout(0.5):
        await notifier.two_commands_subscribed.wait()

    try:
        await service.submit("Primero", key(), request_id="request-1")
        await service.submit("Segundo", other_key, request_id="request-2")
        async with asyncio.timeout(0.5):
            await agent.two_started.wait()
        assert agent.max_active == 2
    finally:
        agent.release.set()
        await service.close()
