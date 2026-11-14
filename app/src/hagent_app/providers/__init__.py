"""Provider adapters, price table, cost records and budgets (task-9).

See ``app/README.md`` (section "Providers and cost") and design sections
10.3, 13.3, 16 and 17.
"""

from hagent_app.providers.chat import MeteredModel, build_chat_model
from hagent_app.providers.config import (
    ConfigKeySource,
    KeySource,
    Provider,
    ProvidersConfig,
    Secret,
)
from hagent_app.providers.decisions import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionClient,
    DecisionModel,
    DecisionRequest,
    DecisionResult,
    Level,
    Question,
    Refusal,
    ScoreAnswer,
    ScoreQuestion,
    YesNoAnswer,
    YesNoQuestion,
)
from hagent_app.providers.embeddings import EmbeddingClient, EmbeddingResult
from hagent_app.providers.errors import (
    BudgetExceededError,
    MissingKeyError,
    PriceUnknownError,
    ProviderError,
    ProviderHTTPError,
    ProviderResponseError,
)
from hagent_app.providers.meter import BudgetState, CallSpec, Meter
from hagent_app.providers.prices import PriceTable, TokenUsage
from hagent_app.providers.service import ProvidersComponent

__all__ = [
    "Answer",
    "BudgetExceededError",
    "BudgetState",
    "CallSpec",
    "ChoiceAnswer",
    "ChoiceQuestion",
    "ConfigKeySource",
    "DecisionClient",
    "DecisionModel",
    "DecisionRequest",
    "DecisionResult",
    "EmbeddingClient",
    "EmbeddingResult",
    "KeySource",
    "Level",
    "Meter",
    "MeteredModel",
    "MissingKeyError",
    "PriceTable",
    "PriceUnknownError",
    "Provider",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderResponseError",
    "ProvidersComponent",
    "ProvidersConfig",
    "Question",
    "Refusal",
    "ScoreAnswer",
    "ScoreQuestion",
    "Secret",
    "TokenUsage",
    "YesNoAnswer",
    "YesNoQuestion",
    "build_chat_model",
]
