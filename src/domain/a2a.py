import json
from dataclasses import dataclass
from typing import Literal

from domain.conversations import ConversationKey
from domain.interactions import InteractionDeliveryMode

A2AJobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class A2AMessage:
    """Versioned message envelope presented to the worker as human input."""

    message_id: str
    content: str

    def serialize(self) -> str:
        """Encode a deterministic provider-neutral A2A prompt envelope."""
        return json.dumps(
            {
                "protocol": "tesseraflow.a2a",
                "version": 1,
                "message_id": self.message_id,
                "content": self.content,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class A2ACompletionMessage:
    """Versioned worker result presented as non-instructional structured data."""

    job_id: str
    thread_id: str
    status: Literal["completed", "failed"]
    answer: str | None = None
    error_code: str | None = None

    def serialize(self) -> str:
        """Encode a deterministic result envelope for a fresh primary-agent turn."""
        return json.dumps(
            {
                "protocol": "tesseraflow.a2a.result",
                "version": 1,
                "job_id": self.job_id,
                "thread_id": self.thread_id,
                "status": self.status,
                "answer": self.answer,
                "error_code": self.error_code,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class A2AThread:
    """Durable conversation that lets one agent address another as a user."""

    thread_id: str
    parent_conversation: ConversationKey
    worker_conversation_id: str


@dataclass(frozen=True, slots=True)
class A2AJob:
    """One ordered message awaiting processing in an agent-to-agent thread."""

    job_id: str
    thread_id: str
    parent_conversation: ConversationKey
    worker_conversation_id: str
    message: str
    delivery_mode: InteractionDeliveryMode = "turn_based"
    status: A2AJobStatus = "queued"
    answer: str | None = None
    response_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class A2AJobReceipt:
    """Safe enqueue result returned to the interactive agent."""

    thread_id: str
    job_id: str
    status: A2AJobStatus


@dataclass(frozen=True, slots=True)
class A2AJobReport:
    """Safe status and optional worker answer exposed through the A2A protocol."""

    thread_id: str
    job_id: str
    status: A2AJobStatus
    answer: str | None = None
    error_code: str | None = None
