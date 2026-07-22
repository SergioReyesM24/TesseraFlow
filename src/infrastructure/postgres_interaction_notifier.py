import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import structlog

from application.ports import A2AJobNotifier, InteractionNotifier, NotificationSubscription

COMMAND_CHANNEL = "tesseraflow_interaction_commands"
OUTPUT_CHANNEL = "tesseraflow_interaction_outputs"
A2A_JOB_CHANNEL = "tesseraflow_a2a_jobs"

logger = structlog.get_logger(__name__)


class _NotificationSubscription:
    """Track notification generations so a signal cannot be lost around a query."""

    def __init__(self) -> None:
        """Initialize one event-loop-local monotonic signal."""
        self._generation = 0
        self._event = asyncio.Event()

    def checkpoint(self) -> int:
        """Capture the generation immediately before reading durable state."""
        return self._generation

    async def wait_for_change(self, checkpoint: int, deadline_seconds: float) -> None:
        """Wait for a newer notification, with a timeout for durable reconciliation."""
        if self._generation != checkpoint:
            return
        self._event.clear()
        if self._generation != checkpoint:
            return
        try:
            await asyncio.wait_for(self._event.wait(), timeout=deadline_seconds)
        except TimeoutError:
            pass

    def publish(self) -> None:
        """Advance the generation and wake every coroutine observing this scope."""
        self._generation += 1
        self._event.set()


class PostgresInteractionNotifier(InteractionNotifier, A2AJobNotifier):
    """Fan PostgreSQL work notifications out to process-local subscriptions."""

    def __init__(self, dsn: str, *, command_timeout_seconds: float) -> None:
        """Configure one dedicated LISTEN connection outside the query pool."""
        self._dsn = dsn
        self._command_timeout_seconds = command_timeout_seconds
        self._connection: asyncpg.Connection | None = None
        self._command_subscribers: set[_NotificationSubscription] = set()
        self._realtime_command_subscribers: dict[str, set[_NotificationSubscription]] = defaultdict(
            set
        )
        self._a2a_job_subscribers: set[_NotificationSubscription] = set()
        self._command_output_subscribers: dict[str, set[_NotificationSubscription]] = defaultdict(
            set
        )
        self._conversation_output_subscribers: dict[str, set[_NotificationSubscription]] = (
            defaultdict(set)
        )

    async def start(self) -> None:
        """Open the dedicated connection and subscribe before workers accept traffic."""
        if self._connection is not None:
            raise RuntimeError("Interaction notifier has already been started")
        connection = await asyncpg.connect(
            dsn=self._dsn,
            command_timeout=self._command_timeout_seconds,
        )
        try:
            await connection.add_listener(COMMAND_CHANNEL, self._handle_command)
            await connection.add_listener(OUTPUT_CHANNEL, self._handle_output)
            await connection.add_listener(A2A_JOB_CHANNEL, self._handle_a2a_job)
            connection.add_termination_listener(self._handle_termination)
        except BaseException:
            await connection.close()
            raise
        self._connection = connection

    async def close(self) -> None:
        """Remove listeners and close the dedicated connection idempotently."""
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        if not connection.is_closed():
            connection.remove_termination_listener(self._handle_termination)
            await connection.remove_listener(COMMAND_CHANNEL, self._handle_command)
            await connection.remove_listener(OUTPUT_CHANNEL, self._handle_output)
            await connection.remove_listener(A2A_JOB_CHANNEL, self._handle_a2a_job)
            await connection.close()

    @asynccontextmanager
    async def subscribe_commands(self) -> AsyncIterator[NotificationSubscription]:
        """Register one bounded-lifetime observer for runnable commands."""
        subscription = _NotificationSubscription()
        self._command_subscribers.add(subscription)
        try:
            yield subscription
        finally:
            self._command_subscribers.discard(subscription)

    @asynccontextmanager
    async def subscribe_jobs(self) -> AsyncIterator[NotificationSubscription]:
        """Register one observer that wakes when an A2A job may be runnable."""
        subscription = _NotificationSubscription()
        self._a2a_job_subscribers.add(subscription)
        try:
            yield subscription
        finally:
            self._a2a_job_subscribers.discard(subscription)

    @asynccontextmanager
    async def subscribe_realtime_commands(
        self,
        conversation_id: str,
    ) -> AsyncIterator[NotificationSubscription]:
        """Observe realtime inbox changes for one live conversation."""
        subscription = _NotificationSubscription()
        subscribers = self._realtime_command_subscribers[conversation_id]
        subscribers.add(subscription)
        try:
            yield subscription
        finally:
            subscribers.discard(subscription)
            if not subscribers:
                self._realtime_command_subscribers.pop(conversation_id, None)

    @asynccontextmanager
    async def subscribe_outputs(
        self,
        conversation_id: str,
        *,
        command_id: str | None = None,
    ) -> AsyncIterator[NotificationSubscription]:
        """Register one output observer and remove its routing state on disconnect."""
        subscription = _NotificationSubscription()
        subscribers = (
            self._command_output_subscribers[command_id]
            if command_id is not None
            else self._conversation_output_subscribers[conversation_id]
        )
        subscribers.add(subscription)
        try:
            yield subscription
        finally:
            subscribers.discard(subscription)
            if not subscribers:
                registry = (
                    self._command_output_subscribers
                    if command_id is not None
                    else self._conversation_output_subscribers
                )
                registry.pop(command_id or conversation_id, None)

    def _handle_command(
        self,
        connection: object,
        process_id: int,
        channel: str,
        payload: object,
    ) -> None:
        """Wake turn workers and the matching realtime conversation consumer."""
        del connection, process_id, channel
        try:
            if not isinstance(payload, str):
                raise TypeError("notification payload must be text")
            decoded: Any = json.loads(payload)
            conversation_id = decoded["conversation_id"]
            delivery_mode = decoded["delivery_mode"]
            if not isinstance(conversation_id, str) or not isinstance(delivery_mode, str):
                raise TypeError("notification routing fields must be strings")
        except (json.JSONDecodeError, KeyError, TypeError):
            for subscription in tuple(self._command_subscribers):
                subscription.publish()
            return
        if delivery_mode == "turn_based":
            for subscription in tuple(self._command_subscribers):
                subscription.publish()
        elif delivery_mode == "realtime":
            for subscription in tuple(self._realtime_command_subscribers.get(conversation_id, ())):
                subscription.publish()

    def _handle_a2a_job(
        self,
        connection: object,
        process_id: int,
        channel: str,
        payload: object,
    ) -> None:
        """Wake every local A2A consumer so durable claiming decides the winner."""
        del connection, process_id, channel, payload
        for subscription in tuple(self._a2a_job_subscribers):
            subscription.publish()

    def _handle_output(
        self,
        connection: object,
        process_id: int,
        channel: str,
        payload: object,
    ) -> None:
        """Route one committed outbox hint to matching local delivery scopes."""
        del connection, process_id, channel
        try:
            if not isinstance(payload, str):
                raise TypeError("notification payload must be text")
            decoded: Any = json.loads(payload)
            command_id = decoded["command_id"]
            conversation_id = decoded["conversation_id"]
            if not isinstance(command_id, str) or not isinstance(conversation_id, str):
                raise TypeError("notification identifiers must be strings")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(
                "interaction_output_notification_invalid",
                error_type=type(exc).__name__,
            )
            return
        subscribers = (
            *self._command_output_subscribers.get(command_id, ()),
            *self._conversation_output_subscribers.get(conversation_id, ()),
        )
        for subscription in subscribers:
            subscription.publish()

    def _handle_termination(self, connection: object) -> None:
        """Wake observers so they fall back to durable reconciliation promptly."""
        del connection
        logger.warning("interaction_notifier_connection_terminated")
        subscriptions = (
            *self._command_subscribers,
            *self._a2a_job_subscribers,
            *(
                subscription
                for subscribers in self._realtime_command_subscribers.values()
                for subscription in subscribers
            ),
            *(
                subscription
                for subscribers in self._command_output_subscribers.values()
                for subscription in subscribers
            ),
            *(
                subscription
                for subscribers in self._conversation_output_subscribers.values()
                for subscription in subscribers
            ),
        )
        for subscription in subscriptions:
            subscription.publish()
