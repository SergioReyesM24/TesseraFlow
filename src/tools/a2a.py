from typing import ClassVar

from pydantic import Field

from application.a2a import A2AJobNotFoundError, A2AService, A2AThreadNotFoundError
from application.tools import AgentTool, ToolArguments, ToolExecutionContext


class DelegateToWorkerArguments(ToolArguments):
    """Validated task description sent by the interactive agent to its worker."""

    message: str = Field(
        min_length=1,
        max_length=20_000,
        description=(
            "Self-contained request for the worker agent, including the facts it should "
            "investigate and the useful context it should return"
        ),
    )


class DelegateToWorkerTool(AgentTool[DelegateToWorkerArguments]):
    """Start a durable worker-agent thread without blocking the interactive turn."""

    name = "delegate_to_worker_agent"
    description = (
        "Immediately delegates any request that needs a tool, API, internal user data, or "
        "information absent from the current conversation to a persistent worker agent. "
        "Use it before asking clarifying questions; the worker decides whether missing "
        "parameters can be discovered or defaulted. Returns a thread_id and job_id without "
        "waiting for completion."
    )
    arguments_model: ClassVar[type[DelegateToWorkerArguments]] = DelegateToWorkerArguments

    def __init__(self, service: A2AService) -> None:
        """Bind the application-level A2A protocol service."""
        self._service = service

    async def execute(
        self,
        arguments: DelegateToWorkerArguments,
        context: ToolExecutionContext,
    ) -> object:
        """Create the worker conversation using the current chat as its owner."""
        receipt = await self._service.delegate(
            context.conversation_key(),
            arguments.message,
            delivery_mode=context.delivery_mode,
        )
        return {
            "thread_id": receipt.thread_id,
            "job_id": receipt.job_id,
            "status": receipt.status,
        }


class WorkerAgentStatusArguments(ToolArguments):
    """Validated identifier of a previously delegated worker message."""

    job_id: str = Field(
        min_length=1,
        max_length=128,
        description="job_id returned by delegate_to_worker_agent or continue_worker_agent",
    )


class WorkerAgentStatusTool(AgentTool[WorkerAgentStatusArguments]):
    """Read the status and completed answer of one owned worker job."""

    name = "get_worker_agent_status"
    description = (
        "Checks a delegated worker job. A completed job includes the worker's detailed "
        "answer; queued and running jobs do not."
    )
    arguments_model: ClassVar[type[WorkerAgentStatusArguments]] = WorkerAgentStatusArguments

    def __init__(self, service: A2AService) -> None:
        """Bind the application-level A2A protocol service."""
        self._service = service

    async def execute(
        self,
        arguments: WorkerAgentStatusArguments,
        context: ToolExecutionContext,
    ) -> object:
        """Return a safe status object without leaking jobs from other conversations."""
        try:
            report = await self._service.status(context.conversation_key(), arguments.job_id)
        except A2AJobNotFoundError:
            return {"found": False, "job_id": arguments.job_id}
        return {
            "found": True,
            "thread_id": report.thread_id,
            "job_id": report.job_id,
            "status": report.status,
            "answer": report.answer,
            "error_code": report.error_code,
        }


class ContinueWorkerArguments(ToolArguments):
    """Validated follow-up addressed to an existing worker-agent conversation."""

    thread_id: str = Field(
        min_length=1,
        max_length=128,
        description="thread_id returned when the worker conversation was created",
    )
    message: str = Field(
        min_length=1,
        max_length=20_000,
        description=(
            "Follow-up written as a human message; the worker will answer using its retained "
            "tool calls and conversation history"
        ),
    )


class ContinueWorkerTool(AgentTool[ContinueWorkerArguments]):
    """Queue a follow-up in the same worker history and return its new job ID."""

    name = "continue_worker_agent"
    description = (
        "Sends another human-style message to an existing worker-agent thread. Use this for "
        "questions that depend on the worker's previous research or tool results."
    )
    arguments_model: ClassVar[type[ContinueWorkerArguments]] = ContinueWorkerArguments

    def __init__(self, service: A2AService) -> None:
        """Bind the application-level A2A protocol service."""
        self._service = service

    async def execute(
        self,
        arguments: ContinueWorkerArguments,
        context: ToolExecutionContext,
    ) -> object:
        """Append a message while preserving per-conversation ownership."""
        try:
            receipt = await self._service.continue_thread(
                context.conversation_key(),
                arguments.thread_id,
                arguments.message,
                delivery_mode=context.delivery_mode,
            )
        except A2AThreadNotFoundError:
            return {"found": False, "thread_id": arguments.thread_id}
        return {
            "found": True,
            "thread_id": receipt.thread_id,
            "job_id": receipt.job_id,
            "status": receipt.status,
        }
