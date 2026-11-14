import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from hagent_app.config import SUPERVISOR_WS_URL, ConfigError, load_config


def test_default_uses_supervisor_proxy_and_token(tmp_path: Path) -> None:
    config = load_config({"HAGENT_DATA_DIR": str(tmp_path), "SUPERVISOR_TOKEN": "t"})
    assert config.ha_url == SUPERVISOR_WS_URL == "ws://supervisor/core/websocket"
    assert config.ha_token == "t"
    assert config.port == 8099
    assert config.database_path == tmp_path / "hagent.db"


def test_environment_overrides(tmp_path: Path) -> None:
    config = load_config(
        {
            "HAGENT_DATA_DIR": str(tmp_path),
            "SUPERVISOR_TOKEN": "s",
            "HAGENT_HA_TOKEN": "dev",
            "HAGENT_HA_URL": "ws://localhost:8123/api/websocket",
            "HAGENT_PORT": "9000",
            "HAGENT_LOG_LEVEL": "debug",
        }
    )
    assert config.ha_token == "dev"
    assert config.ha_url == "ws://localhost:8123/api/websocket"
    assert config.port == 9000
    assert config.log_level == "DEBUG"


def test_missing_token_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="token"):
        load_config({"HAGENT_DATA_DIR": str(tmp_path)})


def test_options_file_and_bad_values(tmp_path: Path) -> None:
    env = {"HAGENT_DATA_DIR": str(tmp_path), "SUPERVISOR_TOKEN": "t"}
    (tmp_path / "options.json").write_text(json.dumps({"log_level": "warning"}))
    assert load_config(env).log_level == "WARNING"
    (tmp_path / "options.json").write_text(json.dumps({"log_level": "loud"}))
    with pytest.raises(ConfigError, match="log level"):
        load_config(env)
    (tmp_path / "options.json").write_text("[1]")
    with pytest.raises(ConfigError, match="object"):
        load_config(env)
    (tmp_path / "options.json").write_text("{")
    with pytest.raises(ConfigError, match="JSON"):
        load_config(env)


@dataclass(frozen=True)
class _Example:
    limit: int = 5
    name: str = "x"


def test_module_section(tmp_path: Path) -> None:
    env = {"HAGENT_DATA_DIR": str(tmp_path), "SUPERVISOR_TOKEN": "t"}
    (tmp_path / "options.json").write_text(
        json.dumps({"example": {"limit": 9}, "bad": {"nope": 1}, "list": [1]})
    )
    config = load_config(env)
    assert config.section("example", _Example) == _Example(limit=9)
    assert config.section("absent", _Example) == _Example()
    with pytest.raises(ConfigError, match="unknown keys"):
        config.section("bad", _Example)
    with pytest.raises(ConfigError, match="object"):
        config.section("list", _Example)


def test_repr_hides_token_and_options(tmp_path: Path) -> None:
    token = "ha-token-SENTINEL"
    key = "sk-key-SENTINEL"
    (tmp_path / "options.json").write_text(
        json.dumps({"providers": {"openai_api_key": key}})
    )
    config = load_config({"HAGENT_DATA_DIR": str(tmp_path), "SUPERVISOR_TOKEN": token})
    assert config.ha_token == token
    for text in (repr(config), str(config)):
        assert token not in text
        assert key not in text
        assert "data_dir" in text
