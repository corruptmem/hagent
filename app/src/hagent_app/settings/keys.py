"""API keys from the settings store.

``SettingsKeySource`` is the ``KeySource`` of the providers area. It reads
the key on each call, so a key that the user saves takes effect on the next
provider call. The order is:

1. The key in the settings store (the management UI).
2. ``HAGENT_OPENAI_API_KEY`` or ``HAGENT_OPENROUTER_API_KEY``.
3. The key in the ``providers`` option section.

The API shows a key only as a **hint**: the last four characters, and only
for a key of at least ``HINT_MIN_LENGTH`` characters.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Literal

from hagent_app.providers.config import KEY_ENV, Provider, ProvidersConfig, Secret
from hagent_app.settings.service import SettingsService

KeySourceName = Literal["settings", "environment", "options"]

HINT_MIN_LENGTH = 20
HINT_CHARS = 4
MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 512
_KEY_CHARS = re.compile(r"[\x21-\x7e]+")


class KeyFormatError(ValueError):
    """The key is not valid. The message never holds the key."""


def check_key(value: object) -> str:
    """Return ``value`` if it can be an API key. Raise ``KeyFormatError``."""
    if not isinstance(value, str):
        msg = "key must be a string"
        raise KeyFormatError(msg)
    if not MIN_KEY_LENGTH <= len(value) <= MAX_KEY_LENGTH:
        msg = f"key must have {MIN_KEY_LENGTH} to {MAX_KEY_LENGTH} characters"
        raise KeyFormatError(msg)
    if not _KEY_CHARS.fullmatch(value):
        msg = "key must be printable ASCII with no spaces"
        raise KeyFormatError(msg)
    return value


def hint(secret: Secret | None) -> str | None:
    """The masked form of a key: ``…`` and its last characters, or ``None``."""
    if not secret:
        return None
    value = secret.reveal()
    if len(value) < HINT_MIN_LENGTH:
        return None
    return "…" + value[-HINT_CHARS:]


class SettingsKeySource:
    """Settings first, then the environment, then the options."""

    def __init__(
        self,
        settings: SettingsService,
        config: ProvidersConfig,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        env = os.environ if env is None else env
        self._fallback: dict[str, tuple[KeySourceName, Secret]] = {}
        for provider, configured in (
            ("openai", config.openai_api_key),
            ("openrouter", config.openrouter_api_key),
        ):
            if env.get(KEY_ENV[provider]):
                self._fallback[provider] = (
                    "environment",
                    Secret(env[KEY_ENV[provider]]),
                )
            elif configured:
                self._fallback[provider] = ("options", Secret(configured))

    def key(self, provider: Provider) -> Secret | None:
        return self.lookup(provider)[1]

    def lookup(self, provider: Provider) -> tuple[KeySourceName | None, Secret | None]:
        """The source and the key of ``provider``."""
        stored = self._settings.key(provider)
        if stored:
            return "settings", stored
        fallback = self._fallback.get(provider)
        if fallback is None:
            return None, None
        return fallback

    def __repr__(self) -> str:
        return "SettingsKeySource()"
