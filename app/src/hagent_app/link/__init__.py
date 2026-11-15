"""The integration link: the WebSocket server for the HA integration (task-17).

See ``docs/link.md`` for the protocol and ``app/README.md`` for the rules.
"""

from hagent_app.link.component import INFO_PATH, LinkComponent, post_discovery
from hagent_app.link.config import LinkConfig
from hagent_app.link.protocol import LINK_PATH, PROTOCOL_VERSION
from hagent_app.link.secret import load_or_create_secret, secret_matches

__all__ = [
    "INFO_PATH",
    "LINK_PATH",
    "PROTOCOL_VERSION",
    "LinkComponent",
    "LinkConfig",
    "load_or_create_secret",
    "post_discovery",
    "secret_matches",
]
