from application.a2a import A2AService
from application.tools import ToolRegistry
from tools.a2a import ContinueWorkerTool, DelegateToWorkerTool, WorkerAgentStatusTool
from tools.application_usage import ApplicationUsageReader, ApplicationUsageTool
from tools.mock_bizum import MockBizumTool
from tools.present_visual import PresentVisualTool
from tools.recent_transactions import RecentTransactionsTool
from tools.weekly_balance_history import WeeklyBalanceHistoryTool


def build_tool_registry(histories: ApplicationUsageReader) -> ToolRegistry:
    """Build operational tools exposed only to the background worker agent."""
    return ToolRegistry(
        [
            WeeklyBalanceHistoryTool(),
            MockBizumTool(),
            RecentTransactionsTool(),
            ApplicationUsageTool(histories),
        ]
    )


def build_interactive_tool_registry(service: A2AService) -> ToolRegistry:
    """Build the A2A and presentation surface exposed to the interactive agent."""
    return ToolRegistry(
        [
            DelegateToWorkerTool(service),
            WorkerAgentStatusTool(service),
            ContinueWorkerTool(service),
            PresentVisualTool(),
        ]
    )
