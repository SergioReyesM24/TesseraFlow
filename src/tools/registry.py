from application.tools import ToolRegistry
from tools.calculator import CalculatorTool
from tools.current_time import CurrentTimeTool


def build_tool_registry() -> ToolRegistry:
    """Composition root for all tools exposed to the model."""
    return ToolRegistry([CalculatorTool(), CurrentTimeTool()])
