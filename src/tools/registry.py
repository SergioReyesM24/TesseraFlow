from application.a2a import A2AService
from application.tools import ToolRegistry
from tools.a2a import ContinueWorkerTool, DelegateToWorkerTool, WorkerAgentStatusTool
from tools.calculator import CalculatorTool
from tools.current_time import CurrentTimeTool


def build_tool_registry() -> ToolRegistry:
    """Build operational tools exposed only to the background worker agent."""
    return ToolRegistry([CalculatorTool(), CurrentTimeTool()])


def build_interactive_tool_registry(service: A2AService) -> ToolRegistry:
    """Build the A2A protocol surface exposed to the interactive agent."""
    return ToolRegistry(
        [
            DelegateToWorkerTool(service),
            WorkerAgentStatusTool(service),
            ContinueWorkerTool(service),
        ]
    )
