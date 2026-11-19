"""Rules: the spec, the sandboxed templates, the store and the rule engine.

See ``app/README.md`` (section "Rules") and design section 10.

The package imports its parts lazily (PEP 562). The template worker process
(``rules.render_worker``) imports ``rules.templates`` only. A plain import
here would load most of the app into each worker.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hagent_app.rules.component import RulesComponent, RulesUnavailableError
    from hagent_app.rules.config import RulesConfig
    from hagent_app.rules.spec import RuleSpec
    from hagent_app.rules.stage import RuleEngine
    from hagent_app.rules.store import AgentStateStores, RuleStore

_EXPORTS = {
    "AgentStateStores": "store",
    "RuleEngine": "stage",
    "RuleSpec": "spec",
    "RuleStore": "store",
    "RulesComponent": "component",
    "RulesConfig": "config",
    "RulesUnavailableError": "component",
}

__all__ = [
    "AgentStateStores",
    "RuleEngine",
    "RuleSpec",
    "RuleStore",
    "RulesComponent",
    "RulesConfig",
    "RulesUnavailableError",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)
