import operator
from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import Field

from application.tools import AgentTool, ToolArguments


class CalculatorArguments(ToolArguments):
    """Validated operands and operation accepted by the calculator tool."""

    operation: Literal["add", "subtract", "multiply", "divide"] = Field(
        description="Arithmetic operation to perform"
    )
    a: Decimal = Field(description="First operand")
    b: Decimal = Field(description="Second operand")


class CalculatorTool(AgentTool[CalculatorArguments]):
    """Perform deterministic decimal arithmetic without evaluating code."""

    name = "calculator"
    description = "Performs one exact arithmetic operation with two decimal numbers."
    arguments_model: ClassVar[type[CalculatorArguments]] = CalculatorArguments

    async def execute(self, arguments: CalculatorArguments) -> object:
        """Apply the requested arithmetic operation to two decimal operands."""
        operations = {
            "add": operator.add,
            "subtract": operator.sub,
            "multiply": operator.mul,
            "divide": operator.truediv,
        }
        if arguments.operation == "divide" and arguments.b == 0:
            raise ValueError("Cannot divide by zero")
        result = operations[arguments.operation](arguments.a, arguments.b)
        return {"result": str(result)}
