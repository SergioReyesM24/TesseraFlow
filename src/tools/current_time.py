from datetime import datetime
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from application.tools import AgentTool, ToolArguments, ToolExecutionContext


class CurrentTimeArguments(ToolArguments):
    """Validated IANA timezone requested by the model."""

    timezone: str = Field(
        description="IANA timezone name, for example Europe/Madrid or America/Bogota"
    )


class CurrentTimeTool(AgentTool[CurrentTimeArguments]):
    """Return the current zoned time using the standard IANA database."""

    name = "current_time"
    description = "Returns the current date and time in an IANA timezone."
    arguments_model: ClassVar[type[CurrentTimeArguments]] = CurrentTimeArguments

    async def execute(
        self,
        arguments: CurrentTimeArguments,
        context: ToolExecutionContext,
    ) -> object:
        """Resolve the timezone and return an ISO 8601 timestamp."""
        del context
        try:
            timezone = ZoneInfo(arguments.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {arguments.timezone}") from exc
        now = datetime.now(timezone)
        return {"datetime": now.isoformat(), "timezone": arguments.timezone}
