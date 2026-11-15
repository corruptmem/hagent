"""Constants of the hagent integration."""

from typing import Final

DOMAIN: Final = "hagent"
MANUFACTURER: Final = "hagent"

CONF_SECRET: Final = "secret"  # noqa: S105 - a key name, not a secret

DEFAULT_PORT: Final = 8099
LINK_PATH: Final = "/api/link/ws"
PROTOCOL_VERSION: Final = 1
# Attachments travel as base64 in an invoke command (see docs/link.md).
MAX_MESSAGE_BYTES: Final = 16 * 1024 * 1024
# The app accepts at most this many attachment bytes per invocation.
MAX_ATTACHMENT_BYTES: Final = 8 * 1024 * 1024

# hagent.invoke (design section 9.2).
SERVICE_INVOKE: Final = "invoke"
ATTR_DATA: Final = "data"
ATTR_WAIT: Final = "wait"
ATTR_TIMEOUT: Final = "timeout"
ATTR_ON_TIMEOUT: Final = "on_timeout"
DEFAULT_TIMEOUT: Final = 30.0
MAX_TIMEOUT: Final = 3600.0
# The integration waits this much longer than the app timeout, so that the
# app can answer with its own timeout result first.
TRANSPORT_GRACE: Final = 10.0
# Commands other than invoke.
COMMAND_TIMEOUT: Final = 15.0

EVENT_RESULT: Final = "hagent_result"

# Reconnect back-off in seconds.
BACKOFF_INITIAL: Final = 1.0
BACKOFF_MAX: Final = 60.0

# HA limits a state to 255 characters.
STATE_MAX_CHARS: Final = 255


def signal_agents(entry_id: str) -> str:
    """The dispatcher signal for a change of the agent list or an agent."""
    return f"{DOMAIN}_agents_{entry_id}"


def signal_connection(entry_id: str) -> str:
    """The dispatcher signal for a change of the link state."""
    return f"{DOMAIN}_connection_{entry_id}"
