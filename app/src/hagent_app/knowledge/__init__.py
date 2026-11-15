"""Knowledge stores, documents and hybrid search (design sections 13.3, 13.4)."""

from hagent_app.knowledge.component import KnowledgeComponent
from hagent_app.knowledge.context import TurnContext, build_turn_context
from hagent_app.knowledge.history import HistoryIndex
from hagent_app.knowledge.service import KnowledgeError, KnowledgeService

__all__ = [
    "HistoryIndex",
    "KnowledgeComponent",
    "KnowledgeError",
    "KnowledgeService",
    "TurnContext",
    "build_turn_context",
]
