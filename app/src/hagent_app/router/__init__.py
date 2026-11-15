"""The router, the invocation queue and the fallback (task-12).

See ``app/README.md`` (section "How to invoke an agent") and design
sections 9.1, 9.4, 9.6, 9.7 and 18.
"""

from hagent_app.router.component import (
    InvocationHandle,
    RetryingChatModels,
    RouterComponent,
    RouterNotRunningError,
    TestOutcome,
    TurnsLike,
    fallback_result,
)
from hagent_app.router.config import RouterConfig
from hagent_app.router.models import InvocationRequest, InvocationResult
from hagent_app.router.retry import RetryingModel, RetryPolicy, retryable
from hagent_app.router.rules import (
    NO_RULE_MATCHED,
    NoRules,
    RuleOutcome,
    RuleRequest,
    RuleStage,
)

__all__ = [
    "NO_RULE_MATCHED",
    "InvocationHandle",
    "InvocationRequest",
    "InvocationResult",
    "NoRules",
    "RetryPolicy",
    "RetryingChatModels",
    "RetryingModel",
    "RouterComponent",
    "RouterConfig",
    "RouterNotRunningError",
    "RuleOutcome",
    "RuleRequest",
    "RuleStage",
    "TestOutcome",
    "TurnsLike",
    "fallback_result",
    "retryable",
]
