from __future__ import annotations

import pytest

from hagent.config import DEFAULT_MODEL, ConfigError, Settings


def test_missing_values_raise() -> None:
    with pytest.raises(ConfigError, match="HA_TOKEN"):
        Settings.from_env({"HA_URL": "http://ha.local:8123"})


def test_defaults_are_read_only() -> None:
    settings = Settings.from_env({"HA_URL": "http://ha/", "HA_TOKEN": "t"})
    assert settings.ha_url == "http://ha"
    assert settings.model == DEFAULT_MODEL
    assert settings.allow_actions is False


def test_actions_opt_in() -> None:
    env = {"HA_URL": "http://ha", "HA_TOKEN": "t", "HAGENT_ALLOW_ACTIONS": "true"}
    assert Settings.from_env(env).allow_actions is True
