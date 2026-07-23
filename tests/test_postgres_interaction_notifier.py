import asyncio
from typing import Any

import infrastructure.postgres_interaction_notifier as notifier_module
from infrastructure.postgres_interaction_notifier import (
    A2A_JOB_CHANNEL,
    COMMAND_CHANNEL,
    OUTPUT_CHANNEL,
    PostgresInteractionNotifier,
)


class FakeConnection:
    """Capture the dedicated PostgreSQL listener lifecycle."""

    def __init__(self) -> None:
        """Initialize channel and termination listener observations."""
        self.listeners: dict[str, object] = {}
        self.termination_listener: object | None = None
        self.closed = False

    async def add_listener(self, channel: str, callback: object) -> None:
        """Register one channel callback."""
        self.listeners[channel] = callback

    async def remove_listener(self, channel: str, callback: object) -> None:
        """Remove the exact registered channel callback."""
        assert self.listeners.pop(channel) == callback

    def add_termination_listener(self, callback: object) -> None:
        """Register the connection termination callback."""
        self.termination_listener = callback

    def remove_termination_listener(self, callback: object) -> None:
        """Remove the exact connection termination callback."""
        assert self.termination_listener == callback
        self.termination_listener = None

    def is_closed(self) -> bool:
        """Report the captured connection state."""
        return self.closed

    async def close(self) -> None:
        """Record graceful listener connection closure."""
        self.closed = True


def notifier() -> PostgresInteractionNotifier:
    """Build a listener whose local fan-out can be tested without PostgreSQL."""
    return PostgresInteractionNotifier(
        "postgresql://test",
        command_timeout_seconds=30,
    )


async def test_notifier_owns_one_dedicated_listener_connection(monkeypatch: Any) -> None:
    """Subscribe both channels before consumers start and release them on shutdown."""
    connection = FakeConnection()

    async def connect(**kwargs: object) -> FakeConnection:
        """Return the listener connection without consuming a pool slot."""
        assert kwargs == {"dsn": "postgresql://test", "command_timeout": 30}
        return connection

    monkeypatch.setattr(notifier_module.asyncpg, "connect", connect)
    listener = notifier()

    await listener.start()

    assert set(connection.listeners) == {A2A_JOB_CHANNEL, COMMAND_CHANNEL, OUTPUT_CHANNEL}
    assert connection.termination_listener is not None

    await listener.close()

    assert connection.listeners == {}
    assert connection.termination_listener is None
    assert connection.closed is True


async def test_command_notification_advances_every_competing_subscription() -> None:
    """Wake all local workers so each can compete through the durable claim query."""
    listener = notifier()
    async with listener.subscribe_commands() as first, listener.subscribe_commands() as second:
        first_checkpoint = first.checkpoint()
        second_checkpoint = second.checkpoint()

        listener._handle_command(None, 1, "commands", "command-1")  # type: ignore[arg-type]

        async with asyncio.timeout(0.1):
            await first.wait_for_change(first_checkpoint, 30)
            await second.wait_for_change(second_checkpoint, 30)


async def test_realtime_command_notification_only_wakes_the_matching_conversation() -> None:
    """Route realtime hints without waking another active speech session."""
    listener = notifier()
    async with (
        listener.subscribe_realtime_commands("conversation-1") as matching,
        listener.subscribe_realtime_commands("conversation-2") as unrelated,
        listener.subscribe_commands() as turn_based,
    ):
        matching_checkpoint = matching.checkpoint()
        unrelated_checkpoint = unrelated.checkpoint()
        turn_based_checkpoint = turn_based.checkpoint()

        listener._handle_command(  # type: ignore[arg-type]
            None,
            1,
            "commands",
            '{"command_id":"command-1","conversation_id":"conversation-1",'
            '"delivery_mode":"realtime"}',
        )

        async with asyncio.timeout(0.1):
            await matching.wait_for_change(matching_checkpoint, 30)
        assert unrelated.checkpoint() == unrelated_checkpoint
        assert turn_based.checkpoint() == turn_based_checkpoint


async def test_a2a_job_notification_advances_every_competing_subscription() -> None:
    """Wake all local A2A workers while durable claiming selects the owner."""
    listener = notifier()
    async with listener.subscribe_jobs() as first, listener.subscribe_jobs() as second:
        first_checkpoint = first.checkpoint()
        second_checkpoint = second.checkpoint()

        listener._handle_a2a_job(None, 1, "a2a-jobs", "job-1")  # type: ignore[arg-type]

        async with asyncio.timeout(0.1):
            await first.wait_for_change(first_checkpoint, 30)
            await second.wait_for_change(second_checkpoint, 30)


async def test_output_notification_only_wakes_matching_delivery_scopes() -> None:
    """Route outbox hints by command and conversation without leaking other chats."""
    listener = notifier()
    async with (
        listener.subscribe_outputs("conversation-1", command_id="command-1") as command,
        listener.subscribe_outputs("conversation-1") as conversation,
        listener.subscribe_outputs("conversation-2") as unrelated,
    ):
        command_checkpoint = command.checkpoint()
        conversation_checkpoint = conversation.checkpoint()
        unrelated_checkpoint = unrelated.checkpoint()

        listener._handle_output(  # type: ignore[arg-type]
            None,
            1,
            "outputs",
            '{"command_id":"command-1","conversation_id":"conversation-1"}',
        )

        async with asyncio.timeout(0.1):
            await command.wait_for_change(command_checkpoint, 30)
            await conversation.wait_for_change(conversation_checkpoint, 30)
        assert unrelated.checkpoint() == unrelated_checkpoint
