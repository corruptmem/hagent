"""Agent turns, the turn history and running summaries (task-10).

See ``app/README.md`` (section "How to run an agent turn") and design
sections 9.5, 11, 13.1 and 13.2.
"""

from hagent_app.turns.component import TurnsComponent, TurnsUnavailableError
from hagent_app.turns.config import TurnsConfig
from hagent_app.turns.history import HistoryIndex, TurnHistory
from hagent_app.turns.models import (
    CompactionRecord,
    SourceSummary,
    StopReason,
    ToolCallRecord,
    TurnInput,
    TurnRecord,
    TurnResult,
    TurnStatus,
    TurnUsage,
)
from hagent_app.turns.prompt import Prompt, PromptBuilder
from hagent_app.turns.runner import TurnRunner, model_tool_name
from hagent_app.turns.summaries import SummaryWriter

__all__ = [
    "CompactionRecord",
    "HistoryIndex",
    "Prompt",
    "PromptBuilder",
    "SourceSummary",
    "StopReason",
    "SummaryWriter",
    "ToolCallRecord",
    "TurnHistory",
    "TurnInput",
    "TurnRecord",
    "TurnResult",
    "TurnRunner",
    "TurnStatus",
    "TurnUsage",
    "TurnsComponent",
    "TurnsConfig",
    "TurnsUnavailableError",
    "model_tool_name",
]
