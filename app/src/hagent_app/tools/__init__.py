"""Agent tools, the scope check and the audit log (design section 8).

Every tool call goes through ``ToolExecutor``. The executor checks the
scope before a tool reaches HA or a store. Tools read agent definitions
only through the ``Agent`` snapshot in ``ToolContext``. No tool receives
``AgentService`` or another definition writer.
"""

from hagent_app.tools.audit import AuditEntry, AuditLog
from hagent_app.tools.base import (
    Prepared,
    ServiceCallObserver,
    StateView,
    Tool,
    ToolContext,
    ToolEnv,
    ToolResult,
)
from hagent_app.tools.component import ToolsComponent
from hagent_app.tools.executor import (
    ALL_TOOLS,
    ApprovalGate,
    ApprovalOutcome,
    ToolExecutor,
)
from hagent_app.tools.registry import HARegistry, RegistrySnapshot
from hagent_app.tools.scope import ScopeDeniedError

__all__ = [
    "ALL_TOOLS",
    "ApprovalGate",
    "ApprovalOutcome",
    "AuditEntry",
    "AuditLog",
    "HARegistry",
    "Prepared",
    "RegistrySnapshot",
    "ScopeDeniedError",
    "ServiceCallObserver",
    "StateView",
    "Tool",
    "ToolContext",
    "ToolEnv",
    "ToolExecutor",
    "ToolResult",
    "ToolsComponent",
]
