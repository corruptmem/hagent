"""Checks of the HA app package in ``hagent/``.

The Supervisor reads ``hagent/config.yaml``. These tests check it in three
ways:

- against the JSON schema of the HA app linter (``frenck/action-app-linter``),
  with the linter rule that a key with its default value is an error;
- against the option schema rules of the Supervisor
  (``supervisor/apps/options.py``): the element syntax, nested objects and
  lists, required keys and value types;
- against the app: each option that the Supervisor can write must load in
  ``Config`` and in the section dataclass of its area. ``Config.section``
  refuses unknown keys, so a schema key that the app does not know would
  stop the app.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import fields
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from hagent_app.config import DEFAULT_PORT, LOG_LEVELS, Config, load_config
from hagent_app.link.config import LinkConfig
from hagent_app.providers.config import ProvidersConfig
from jsonschema import Draft7Validator, ValidationError, validators

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "hagent"
LINTER_SCHEMA = Path(__file__).parent / "fixtures" / "app_linter" / "config.schema.json"

# The option sections in config.yaml and the dataclass that reads each one.
SECTIONS: dict[str, type[Any]] = {"providers": ProvidersConfig, "link": LinkConfig}
# The top-level keys that Config reads itself.
CORE_KEYS = {"log_level"}

# RE_SCHEMA_ELEMENT of supervisor/apps/options.py (commit 98a2322374ae).
RE_SCHEMA_ELEMENT = re.compile(
    r"^(?:"
    r"|bool"
    r"|email"
    r"|url"
    r"|port"
    r"|device(?:\((?P<filter>subsystem=[a-z]+)\))?"
    r"|str(?:\((?P<s_min>\d+)?,(?P<s_max>\d+)?\))?"
    r"|password(?:\((?P<p_min>\d+)?,(?P<p_max>\d+)?\))?"
    r"|int(?:\((?P<i_min>-?\d+)?,(?P<i_max>-?\d+)?\))?"
    r"|float(?:\((?P<f_min>-?\d*\.?\d+)?,(?P<f_max>-?\d*\.?\d+)?\))?"
    r"|match\((?P<match>.*)\)"
    r"|list\((?P<list>.+)\)"
    r")\??$"
)

# A value for each element type, for the "every option set" test.
SAMPLES: dict[str, Any] = {
    "bool": True,
    "str": "app-host",
    "password": "sk-test-0000000000000000",
    "int": 1,
    "float": 1.5,
}


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    data = _load_yaml(APP_DIR / "config.yaml")
    assert isinstance(data, dict)
    return data


# --------------------------------------------------------------- the linter


def _default_checking_validator() -> type[Draft7Validator]:
    """Draft 7 with the linter rule: a key with its default value is an error."""
    validate_properties = Draft7Validator.VALIDATORS["properties"]

    def properties(
        validator: Draft7Validator,
        props: dict[str, Any],
        instance: Any,
        schema: dict[str, Any],
    ) -> Any:
        if isinstance(instance, dict):
            for name, sub in props.items():
                if "default" in sub and instance.get(name) == sub["default"]:
                    yield ValidationError(
                        f"'{name}' should be removed, it uses a default value"
                    )
        yield from validate_properties(validator, props, instance, schema)

    # jsonschema ships no type for extend().
    extended: type[Draft7Validator] = validators.extend(  # type: ignore[no-untyped-call]
        Draft7Validator, {"properties": properties}
    )
    return extended


def test_config_passes_the_linter_schema(config: dict[str, Any]) -> None:
    schema = json.loads(LINTER_SCHEMA.read_text(encoding="utf-8"))
    validator = _default_checking_validator()(schema)
    errors = sorted(e.message for e in validator.iter_errors(config))
    assert errors == []


def test_linter_schema_refuses_a_default_value(config: dict[str, Any]) -> None:
    schema = json.loads(LINTER_SCHEMA.read_text(encoding="utf-8"))
    validator = _default_checking_validator()(schema)
    bad = {**config, "ingress_port": 8099}
    with pytest.raises(jsonschema.ValidationError, match="default value"):
        validator.validate(bad)


def test_config_passes_the_linter_rules(config: dict[str, Any]) -> None:
    # The checks of lint.py outside the JSON schema that apply to this app.
    assert not ({"webui"} & set(config)), "Ingress apps must not set webui"
    assert config.get("ingress_port", 8099) != 0
    assert "watchdog" not in config
    assert "codenotary" not in config
    if config.get("backup") == "cold":
        assert not ({"backup_pre", "backup_post"} & set(config))
    assert not ({"armhf", "armv7", "i386"} & set(config["arch"]))


def test_config_has_what_the_app_needs(config: dict[str, Any]) -> None:
    assert config["slug"] == "hagent"
    assert config["arch"] == ["amd64"]
    assert config["ingress"] is True
    # ingress_port keeps its default, the port of the app HTTP server.
    assert config.get("ingress_port", 8099) == DEFAULT_PORT
    assert config["homeassistant_api"] is True
    # The link component posts the discovery for the "hagent" service.
    assert config["discovery"] == ["hagent"]
    # s6-overlay of the base image must be PID 1.
    assert config["init"] is False
    # SQLite in WAL mode: a backup must copy closed files.
    assert config["backup"] == "cold"


# ----------------------------------------------------- the Supervisor rules


def _walk_leaves(schema: Any, path: str = "") -> list[tuple[str, str]]:
    if isinstance(schema, dict):
        return [
            leaf
            for key, sub in schema.items()
            for leaf in _walk_leaves(sub, f"{path}.{key}" if path else key)
        ]
    if isinstance(schema, list):
        assert len(schema) == 1, f"{path}: a list schema has one element"
        return _walk_leaves(schema[0], path)
    assert isinstance(schema, str), f"{path}: not a schema element"
    return [(path, schema)]


def _check_leaf(typ: str, value: Any, path: str) -> None:
    match = RE_SCHEMA_ELEMENT.match(typ)
    assert match is not None, f"{path}: {typ!r} is not a schema element"
    base = typ.rstrip("?").split("(")[0]
    if base == "list":
        allowed = (match.group("list") or "").split("|")
        assert value in allowed, f"{path}: {value!r} not in {allowed}"
    elif base == "bool":
        assert isinstance(value, bool), f"{path}: not a bool"
    elif base in {"str", "password"}:
        assert isinstance(value, str), f"{path}: not text"
    elif base in {"int", "float"}:
        low = match.group("i_min" if base == "int" else "f_min")
        high = match.group("i_max" if base == "int" else "f_max")
        assert isinstance(value, int | float), f"{path}: not a number"
        assert not isinstance(value, bool), f"{path}: not a number"
        if base == "int":
            assert isinstance(value, int), f"{path}: not an integer"
        if low is not None:
            assert value >= float(low), f"{path}: below {low}"
        if high is not None:
            assert value <= float(high), f"{path}: above {high}"
    else:
        pytest.fail(f"{path}: the test does not know the type {typ!r}")


def supervisor_validate(schema: Any, value: Any, path: str = "root") -> Any:
    """Validate options like ``AppOptions`` of the Supervisor.

    Unknown keys are dropped (the Supervisor logs a warning). A missing key
    is an error unless its element ends with ``?``.
    """
    if isinstance(schema, list):
        assert isinstance(value, list), f"{path}: not a list"
        return [supervisor_validate(schema[0], v, path) for v in value]
    if isinstance(schema, dict):
        assert isinstance(value, dict), f"{path}: not a dict"
        out = {
            key: supervisor_validate(schema[key], sub, key)
            for key, sub in value.items()
            if key in schema
        }
        for key in set(schema) - set(out):
            element = schema[key]
            if isinstance(element, list):
                element = element[0]
            assert isinstance(element, str), f"{path}: missing option {key!r}"
            assert element.endswith("?"), f"{path}: missing option {key!r}"
        return out
    _check_leaf(schema, value, path)
    return value


def test_schema_elements_are_valid(config: dict[str, Any]) -> None:
    leaves = _walk_leaves(config["schema"])
    assert leaves
    for path, typ in leaves:
        assert RE_SCHEMA_ELEMENT.match(typ), f"{path}: {typ!r}"


def test_default_options_pass_the_supervisor_rules(config: dict[str, Any]) -> None:
    out = supervisor_validate(config["schema"], config["options"])
    assert out == config["options"]


def test_supervisor_rules_refuse_bad_options(config: dict[str, Any]) -> None:
    schema = config["schema"]
    with pytest.raises(AssertionError, match="not in"):
        supervisor_validate(schema, {**config["options"], "log_level": "loud"})
    without_link = {k: v for k, v in config["options"].items() if k != "link"}
    with pytest.raises(AssertionError, match="missing option 'link'"):
        supervisor_validate(schema, without_link)


# -------------------------------------------------------------- the app side


def _every_option(schema: Any) -> Any:
    """Options with a value for every key of the schema."""
    if isinstance(schema, dict):
        return {key: _every_option(sub) for key, sub in schema.items()}
    if isinstance(schema, list):
        return [_every_option(schema[0])]
    base = schema.rstrip("?").split("(")[0]
    if base == "list":
        match = RE_SCHEMA_ELEMENT.match(schema)
        assert match is not None
        return (match.group("list") or "").split("|")[-1]
    return SAMPLES[base]


def _load(tmp_path: Path, options: dict[str, Any]) -> Config:
    (tmp_path / "options.json").write_text(json.dumps(options), encoding="utf-8")
    env = {"HAGENT_DATA_DIR": str(tmp_path), "SUPERVISOR_TOKEN": "test-token"}
    return load_config(env)


def test_schema_keys_are_known_to_the_app(config: dict[str, Any]) -> None:
    for key, sub in config["schema"].items():
        if isinstance(sub, dict):
            assert key in SECTIONS, f"no app section for {key!r}"
            known = {f.name for f in fields(SECTIONS[key])}
            assert set(sub) <= known, f"{key}: {set(sub) - known}"
        else:
            assert key in CORE_KEYS, f"{key!r} is not a core key of Config"


@pytest.mark.parametrize("which", ["defaults", "every option"])
def test_supervisor_options_load_in_the_app(
    config: dict[str, Any], tmp_path: Path, which: str
) -> None:
    raw = config["options"] if which == "defaults" else _every_option(config["schema"])
    options = supervisor_validate(config["schema"], raw)
    loaded = _load(tmp_path, options)
    assert loaded.log_level in LOG_LEVELS
    for name, cls in SECTIONS.items():
        loaded.section(name, cls)


def test_every_log_level_loads(config: dict[str, Any], tmp_path: Path) -> None:
    match = RE_SCHEMA_ELEMENT.match(config["schema"]["log_level"])
    assert match is not None
    for level in (match.group("list") or "").split("|"):
        assert _load(tmp_path, {"log_level": level}).log_level == level.upper()


# ----------------------------------------------------------- other app files


def _check_translation(schema: Any, entry: Any, path: str) -> None:
    assert isinstance(entry, dict), f"{path}: no translation"
    assert isinstance(entry.get("name"), str), f"{path}: no name"
    if isinstance(schema, dict):
        entry_fields = entry.get("fields", {})
        assert set(entry_fields) == set(schema), f"{path}: fields differ"
        for key, sub in schema.items():
            _check_translation(sub, entry_fields[key], f"{path}.{key}")


def test_translations_cover_the_schema(config: dict[str, Any]) -> None:
    data = _load_yaml(APP_DIR / "translations" / "en.yaml")
    configuration = data["configuration"]
    assert set(configuration) == set(config["schema"])
    for key, sub in config["schema"].items():
        _check_translation(sub, configuration[key], key)


def test_service_scripts_are_executable() -> None:
    service = APP_DIR / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "hagent"
    assert (service / "type").read_text(encoding="utf-8").strip() == "longrun"
    for name in ("run", "finish"):
        assert os.access(service / name, os.X_OK), f"{name} is not executable"
    bundle = APP_DIR / "rootfs" / "etc" / "s6-overlay" / "user-bundles.d"
    assert (bundle / "user" / "contents.d" / "hagent").exists()


def test_app_store_files_exist() -> None:
    for name in ("DOCS.md", "CHANGELOG.md", "icon.png", "logo.png", "Dockerfile"):
        assert (APP_DIR / name).is_file(), name
