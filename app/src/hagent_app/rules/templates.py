"""The rule template engine (design section 10.2).

Rule templates (match, action, result, write data and agreement) use HA
template syntax: Jinja with a documented subset of the HA functions. The app
renders them itself, in a sandbox. It never sends them to HA Core. See
``docs/rule-templates.md`` for the supported subset.

Terms:

- **Sandbox**: a Jinja environment that blocks access to Python internals
  (``jinja2.sandbox.ImmutableSandboxedEnvironment``). It also blocks the
  methods that change lists and dicts.
- **Scoped state view**: a copy of the HA state cache that holds only the
  entities that the agent can read. ``states()`` returns ``unknown`` for
  every other entity. The template therefore cannot learn whether such an
  entity exists.

Limits. A template is text from a model, so this module also limits what a
template can cost:

- The template text and the rendered output have a maximum size.
- Each render runs in a worker thread with a time limit. A trace function
  stops the render at the time limit, also inside a loop.
- Operators and filters that can build very large values (``*``, ``**``,
  ``%``, ``join``, ``replace``, ``center``, ``indent``, ``format``) check the
  size first.
- Only a short list of string methods is callable.
"""

from __future__ import annotations

import asyncio
import functools
import json
import math
import re
import statistics
import sys
import time
from ast import literal_eval
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from types import FrameType
from typing import Any

from jinja2 import TemplateSyntaxError, meta, nodes, pass_environment
from jinja2.compiler import CodeGenerator, Frame
from jinja2.exceptions import SecurityError, UndefinedError
from jinja2.filters import do_batch as jinja_batch
from jinja2.filters import make_attrgetter
from jinja2.runtime import Context
from jinja2.sandbox import ImmutableSandboxedEnvironment

from hagent_app.ha import State

MAX_TEMPLATE = 10_000
MAX_OUTPUT = 256 * 1024
MAX_VALUE = 256 * 1024
MAX_WIDTH = 1000
MAX_DEEP = 1024 * 1024
RENDER_THREADS = 4
MAX_BITS = 4096
RENDER_SECONDS = 1.0

# The variables that the runtime passes to rule templates (design 10.2).
VARIABLES = frozenset(
    {
        "source",
        "inputs",
        "data",
        "context",
        "output",
        "answers",
        "result",
        "rule_result",
        "agent_result",
    }
)

DECLINE = "decline"
_UNKNOWN = "unknown"
_NO_VALUE = frozenset({"unknown", "unavailable"})

# HA's check for a number that keeps its text form (homeassistant/helpers/
# template). "01" stays text, "1.0" becomes a float.
_IS_NUMERIC = re.compile(r"^[+-]?(?!0\d)\d*(?:\.\d*)?$")
_FORMAT_WIDTH = re.compile(r"%[-+ #0]*(\d+)?(?:\.(\d+))?")


# A small pool of its own, so that a stuck render never blocks the default
# executor of the event loop.
_RENDER_POOL = ThreadPoolExecutor(
    max_workers=RENDER_THREADS, thread_name_prefix="rule-template"
)


class TemplateError(Exception):
    """A template failed to parse, to pass the check, or to render."""


class _Deadline(Exception):  # noqa: N818 - internal signal
    """The render reached its time limit."""


# --------------------------------------------------------------- state view


class TemplateState:
    """The template view of one entity state.

    It holds plain copies only. It has no ``context`` attribute, because the
    HA context holds user IDs.
    """

    __slots__ = (
        "attributes",
        "domain",
        "entity_id",
        "last_changed",
        "last_updated",
        "name",
        "object_id",
        "state",
    )

    def __init__(self, state: State) -> None:
        self.entity_id = state.entity_id
        self.domain, _, self.object_id = state.entity_id.partition(".")
        self.state = state.state
        self.attributes = dict(state.attributes)
        name = self.attributes.get("friendly_name")
        self.name = name if isinstance(name, str) else self.object_id
        self.last_changed = state.last_changed
        self.last_updated = state.last_updated

    def __repr__(self) -> str:
        return f"<template TemplateState({self.entity_id}={self.state})>"

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True)
class ScopedStates:
    """The states that one agent can read, for one invocation.

    The router reads the states once per invocation, so that every template
    of the invocation sees the same data (design section 13.2).
    """

    states: Mapping[str, TemplateState] = field(default_factory=dict)

    @classmethod
    def build(
        cls, source: Iterable[State], visible: Callable[[str], bool]
    ) -> ScopedStates:
        """Copy the states for which ``visible(entity_id)`` is true."""
        return cls(
            {s.entity_id: TemplateState(s) for s in source if visible(s.entity_id)}
        )

    def get(self, entity_id: Any) -> TemplateState | None:
        """The state, or ``None`` if it is missing or out of scope."""
        if not isinstance(entity_id, str):
            return None
        return self.states.get(entity_id.lower())


class _DomainStates:
    """``states.<domain>``: iterate or look up the visible entities."""

    __slots__ = ("_domain", "_view")

    def __init__(self, view: ScopedStates, domain: str) -> None:
        self._view = view
        self._domain = domain

    def __getattr__(self, name: str) -> TemplateState | None:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._view.get(f"{self._domain}.{name}")

    def __iter__(self) -> Iterator[TemplateState]:
        prefix = self._domain + "."
        return iter(
            [
                s
                for eid, s in sorted(self._view.states.items())
                if eid.startswith(prefix)
            ]
        )

    def __len__(self) -> int:
        return len(list(iter(self)))

    def __repr__(self) -> str:
        return f"<template DomainStates('{self._domain}')>"


class _AllStates:
    """``states``: call it, look up a domain, or iterate the visible states."""

    __slots__ = ("_view",)

    def __init__(self, view: ScopedStates) -> None:
        self._view = view

    def __call__(
        self, entity_id: Any, rounded: bool = False, with_unit: bool = False
    ) -> str:
        state = self._view.get(entity_id)
        if state is None:
            return _UNKNOWN
        if with_unit:
            unit = state.attributes.get("unit_of_measurement")
            if unit:
                return f"{state.state} {unit}"
        return state.state

    def __getattr__(self, name: str) -> _DomainStates:
        if name.startswith("_"):
            raise AttributeError(name)
        return _DomainStates(self._view, name)

    def __iter__(self) -> Iterator[TemplateState]:
        return iter([s for _eid, s in sorted(self._view.states.items())])

    def __len__(self) -> int:
        return len(self._view.states)

    def __repr__(self) -> str:
        return "<template AllStates>"


# ------------------------------------------------------------- HA functions


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING: Any = _Missing()


def _fail(name: str, value: Any) -> TemplateError:
    return TemplateError(f"{name} got invalid input {value!r} and no default was given")


def _to_float(value: Any, default: Any = _MISSING) -> Any:
    try:
        return float(value)
    except ValueError, TypeError:
        if default is _MISSING:
            raise _fail("float", value) from None
        return default


def _to_int(value: Any, default: Any = _MISSING, base: int = 10) -> Any:
    try:
        if isinstance(value, str):
            return int(value, base)
        return int(value)
    except ValueError, TypeError:
        try:
            return int(float(value))
        except ValueError, TypeError, OverflowError:
            if default is _MISSING:
                raise _fail("int", value) from None
            return default


_TRUE = frozenset({"1", "true", "yes", "on", "enable"})
_FALSE = frozenset({"0", "false", "no", "off", "disable"})


def _to_bool(value: Any, default: Any = _MISSING) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    if default is _MISSING:
        raise _fail("bool", value)
    return default


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, str):
        try:
            return math.isfinite(float(value))
        except ValueError:
            return False
    return False


def _round(
    value: Any, precision: int = 0, method: str = "common", default: Any = _MISSING
) -> Any:
    try:
        number = float(value)
    except ValueError, TypeError:
        if default is _MISSING:
            raise _fail("round", value) from None
        return default
    multiplier = float(10**precision)
    if method == "ceil":
        result: float = math.ceil(number * multiplier) / multiplier
    elif method == "floor":
        result = math.floor(number * multiplier) / multiplier
    elif method == "half":
        result = round(number * 2) / 2
    else:
        result = round(number, precision)
    # As HA: precision 0 gives an int.
    return int(result) if precision == 0 else result


def _multiply(value: Any, amount: Any, default: Any = _MISSING) -> Any:
    try:
        return float(value) * amount
    except ValueError, TypeError:
        if default is _MISSING:
            raise _fail("multiply", value) from None
        return default


def _add(value: Any, amount: Any, default: Any = _MISSING) -> Any:
    try:
        return float(value) + amount
    except ValueError, TypeError:
        if default is _MISSING:
            raise _fail("add", value) from None
        return default


def _numbers(name: str, args: tuple[Any, ...], default: Any) -> list[float] | Any:
    items: Iterable[Any] = args
    if (
        len(args) == 1
        and isinstance(args[0], Iterable)
        and not isinstance(args[0], str)
    ):
        items = args[0]
    values: list[float] = []
    for item in items:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            # Keep the type, as HA does: the median of ints can be an int.
            values.append(item)
            continue
        try:
            values.append(float(item))
        except ValueError, TypeError:
            if default is _MISSING:
                raise _fail(name, item) from None
            return _MISSING
    if not values:
        if default is _MISSING:
            msg = f"{name} needs at least one value"
            raise TemplateError(msg)
        return _MISSING
    return values


def _average(*args: Any, default: Any = _MISSING) -> Any:
    values = _numbers("average", args, default)
    return default if values is _MISSING else statistics.fmean(values)


def _median(*args: Any, default: Any = _MISSING) -> Any:
    values = _numbers("median", args, default)
    return default if values is _MISSING else statistics.median(values)


def _min_max(fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if len(args) == 1 and isinstance(args[0], Iterable):
            return fn(args[0], **kwargs)
        return fn(args, **kwargs)

    return wrapper


def _iif(
    value: Any, if_true: Any = True, if_false: Any = False, if_none: Any = _MISSING
) -> Any:
    if value is None and if_none is not _MISSING:
        return if_none
    if bool(value):
        return if_true
    return if_false


def _to_json(value: Any, ensure_ascii: bool = False, sort_keys: bool = False) -> str:
    return (
        json.dumps(value, ensure_ascii=ensure_ascii, sort_keys=sort_keys, default=str)
        .replace(", ", ",")
        .replace(": ", ":")
    )


def _from_json(value: Any, default: Any = _MISSING) -> Any:
    try:
        return json.loads(value)
    except ValueError, TypeError:
        if default is _MISSING:
            raise _fail("from_json", value) from None
        return default


def _regex(value: Any, find: str, ignorecase: bool) -> re.Match[str] | None:
    if not isinstance(value, str):
        value = str(value)
    flags = re.IGNORECASE if ignorecase else 0
    return re.compile(find, flags).match(value)


def _regex_match(value: Any, find: str = "", ignorecase: bool = False) -> bool:
    return _regex(value, find, ignorecase) is not None


def _regex_search(value: Any, find: str = "", ignorecase: bool = False) -> bool:
    if not isinstance(value, str):
        value = str(value)
    flags = re.IGNORECASE if ignorecase else 0
    return re.compile(find, flags).search(value) is not None


def _regex_replace(
    value: Any = "", find: str = "", replace: str = "", ignorecase: bool = False
) -> str:
    if not isinstance(value, str):
        value = str(value)
    flags = re.IGNORECASE if ignorecase else 0
    pattern = re.compile(find, flags)
    total = len(value)

    def piece(match: re.Match[str]) -> str:
        nonlocal total
        text = match.expand(replace)
        total += len(text) - (match.end() - match.start())
        if total > MAX_VALUE:
            msg = f"a replaced value is longer than {MAX_VALUE}"
            raise TemplateError(msg)
        return text

    return pattern.sub(piece, value)


def _regex_findall(value: Any, find: str = "", ignorecase: bool = False) -> list[Any]:
    if not isinstance(value, str):
        value = str(value)
    flags = re.IGNORECASE if ignorecase else 0
    return re.compile(find, flags).findall(value)


def _regex_findall_index(
    value: Any, find: str = "", index: int = 0, ignorecase: bool = False
) -> Any:
    return _regex_findall(value, find, ignorecase)[index]


def _contains(value: Any, item: Any) -> bool:
    try:
        return item in value
    except TypeError:
        return False


# ----------------------------------------------------------- size checks


def deep_size(value: Any, limit: int = MAX_DEEP) -> int:
    """An estimate of the text length of ``value``, with every reference
    counted. Raise ``TemplateError`` as soon as it passes ``limit``.

    Nested lists that share references can be small in memory but very
    large as text. The walk counts each reference again, so it finds them.
    """
    total = 0
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            total += len(item) + 2
        elif isinstance(item, Context):
            # Jinja passes the render context to some filters.
            total += 24
        elif isinstance(item, Mapping):
            total += 2 + 2 * len(item)
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            total += 2 + 2 * len(item)
            stack.extend(item)
        else:
            total += 24
        if total > limit:
            msg = f"a template value is larger than {limit}"
            raise TemplateError(msg)
    return total


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Check the size of every argument before ``fn`` runs.

    Many filters turn their input into text in one call. The check stops a
    value that is too large before that call.
    """

    @functools.wraps(fn)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        for arg in (*args, *kwargs.values()):
            if isinstance(
                arg, (str, Mapping, list, tuple, set, frozenset)
            ) and not isinstance(arg, Context):
                deep_size(arg)
        return fn(*args, **kwargs)

    return guarded


def _check_size(value: Any) -> Any:
    if isinstance(value, (str, list, tuple, dict)) and len(value) > MAX_VALUE:
        msg = f"a template value is longer than {MAX_VALUE}"
        raise TemplateError(msg)
    return value


def _check_format(text: str) -> None:
    for match in _FORMAT_WIDTH.finditer(text):
        for group in match.groups():
            if group is not None and int(group) > MAX_WIDTH:
                msg = f"a format width or precision is larger than {MAX_WIDTH}"
                raise TemplateError(msg)


def _join(value: Iterable[Any], d: str = "", attribute: Any = None) -> str:
    del attribute
    _check_size(d)
    parts: list[str] = []
    total = 0
    for item in value:
        total += deep_size(item) + len(d)
        if total > MAX_VALUE:
            msg = f"a joined value is longer than {MAX_VALUE}"
            raise TemplateError(msg)
        text = str(item)
        total += len(text) + len(d)
        if total > MAX_VALUE:
            msg = f"a joined value is longer than {MAX_VALUE}"
            raise TemplateError(msg)
        parts.append(text)
    return d.join(parts)


def _replace(s: Any, old: str, new: str, count: int | None = None) -> str:
    text = str(s)
    hits = text.count(str(old)) if old else len(text) + 1
    if count is not None and count >= 0:
        hits = min(hits, count)
    if len(text) + hits * len(str(new)) > MAX_VALUE:
        msg = f"a replaced value is longer than {MAX_VALUE}"
        raise TemplateError(msg)
    return text.replace(str(old), str(new), -1 if count is None else count)


def _center(value: Any, width: int = 80) -> str:
    return str(value).center(min(int(width), MAX_WIDTH))


def _indent(s: Any, width: int | str = 4, first: bool = False) -> str:
    pad = width if isinstance(width, str) else " " * min(int(width), MAX_WIDTH)
    _check_size(pad)
    lines = str(s).splitlines(keepends=True)
    if len(str(s)) + len(pad) * len(lines) > MAX_VALUE:
        msg = f"an indented value is longer than {MAX_VALUE}"
        raise TemplateError(msg)
    out = "".join(
        (pad + line if (i or first) and line.strip() else line)
        for i, line in enumerate(lines)
    )
    _check_size(out)
    return out


@pass_environment
def _batch(environment: Any, value: Any, linecount: int, fill_with: Any = None) -> Any:
    if not isinstance(linecount, int) or not 1 <= linecount <= MAX_WIDTH * 10:
        msg = f"batch takes a size from 1 to {MAX_WIDTH * 10}"
        raise TemplateError(msg)
    return jinja_batch(value, linecount, fill_with)


@pass_environment
def _sum(environment: Any, iterable: Any, attribute: Any = None, start: Any = 0) -> Any:
    items = list(iterable)
    if attribute is not None:
        getter = make_attrgetter(environment, attribute)
        items = [getter(item) for item in items]
    for item in (start, *items):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            msg = "sum adds numbers only"
            raise TemplateError(msg)
    return sum(items, start)


def _checked_concat(values: Iterable[Any]) -> str:
    """The ``~`` operator, with a size check before each part."""
    parts: list[str] = []
    total = 0
    for value in values:
        total += deep_size(value)
        if total > MAX_VALUE:
            msg = f"a joined value is longer than {MAX_VALUE}"
            raise TemplateError(msg)
        parts.append(str(value))
    return "".join(parts)


def _finalize(value: Any) -> Any:
    """Check each ``{{ }}`` value before Jinja turns it into text."""
    deep_size(value, MAX_OUTPUT)
    return value


def _format(value: Any, *args: Any, **kwargs: Any) -> str:
    text = str(value)
    _check_format(text)
    if args and kwargs:
        msg = "format takes positional or keyword arguments, not both"
        raise TemplateError(msg)
    out = text % (kwargs or args)
    _check_size(out)
    return out


# The string methods that a template can call. Every other method of str,
# for example ljust, zfill or format, is blocked: it can build a very
# large value in one call.
_STR_METHODS = frozenset(
    {
        "capitalize",
        "casefold",
        "count",
        "endswith",
        "find",
        "isalnum",
        "isalpha",
        "isdigit",
        "islower",
        "isnumeric",
        "isspace",
        "isupper",
        "lower",
        "lstrip",
        "removeprefix",
        "removesuffix",
        "rfind",
        "rsplit",
        "rstrip",
        "split",
        "splitlines",
        "startswith",
        "strip",
        "title",
        "upper",
    }
)

# The Jinja filters and tests that the subset keeps. HA filters are added
# below. Every other built-in filter is removed.
_KEEP_FILTERS = frozenset(
    {
        "abs",
        "attr",
        "batch",
        "capitalize",
        "count",
        "d",
        "default",
        "dictsort",
        "first",
        "groupby",
        "items",
        "last",
        "length",
        "list",
        "lower",
        "map",
        "max",
        "min",
        "reject",
        "rejectattr",
        "reverse",
        "select",
        "selectattr",
        "slice",
        "sort",
        "string",
        "sum",
        "title",
        "tojson",
        "trim",
        "truncate",
        "unique",
        "upper",
        "wordcount",
    }
)
_KEEP_TESTS = frozenset(
    {
        "!=",
        "<",
        "<=",
        "==",
        ">",
        ">=",
        "boolean",
        "defined",
        "divisibleby",
        "eq",
        "equalto",
        "even",
        "false",
        "float",
        "ge",
        "greaterthan",
        "gt",
        "in",
        "integer",
        "iterable",
        "le",
        "lessthan",
        "lower",
        "lt",
        "mapping",
        "ne",
        "none",
        "number",
        "odd",
        "sameas",
        "sequence",
        "string",
        "true",
        "undefined",
        "upper",
    }
)
_KEEP_GLOBALS = frozenset({"dict", "namespace", "range"})


# -------------------------------------------------------------- the sandbox


class _CodeGenerator(CodeGenerator):
    """Compiles ``~`` to a join with a size check."""

    def visit_Concat(self, node: nodes.Concat, frame: Frame) -> None:  # noqa: N802
        self.write("environment.checked_concat((")
        for arg in node.nodes:
            self.visit(arg, frame)
            self.write(", ")
        self.write("))")


class _Sandbox(ImmutableSandboxedEnvironment):
    """The sandbox with size checks on operators and method calls."""

    intercepted_binops = frozenset({"*", "**", "%", "+"})
    code_generator_class = _CodeGenerator
    checked_concat = staticmethod(_checked_concat)

    def call_binop(self, context: Context, operator: str, left: Any, right: Any) -> Any:
        if operator == "**":
            if (
                isinstance(left, int)
                and isinstance(right, int)
                and (
                    right > MAX_WIDTH
                    or abs(left).bit_length() * max(right, 0) > MAX_BITS
                )
            ):
                msg = "the power is too large"
                raise TemplateError(msg)
            return left**right
        if operator == "*":
            for seq, n in ((left, right), (right, left)):
                if (
                    isinstance(seq, (str, list, tuple))
                    and isinstance(n, int)
                    and len(seq) * n > MAX_VALUE
                ):
                    msg = f"a repeated value is longer than {MAX_VALUE}"
                    raise TemplateError(msg)
            return left * right
        if operator == "+":
            if (
                isinstance(left, (str, list, tuple))
                and isinstance(right, (str, list, tuple))
                and len(left) + len(right) > MAX_VALUE
            ):
                msg = f"an added value is longer than {MAX_VALUE}"
                raise TemplateError(msg)
            return left + right
        if isinstance(left, str):
            _check_format(left)
            deep_size(right)
            return _check_size(left % right)
        return left % right

    def wrap_str_format(self, value: Any) -> Callable[..., str] | None:
        """Block ``str.format`` and ``str.format_map``.

        A format width can build a very large value in one call. Templates
        use the ``format`` filter, which checks the width first.
        """
        owner = getattr(value, "__self__", None)
        if isinstance(owner, str) and getattr(value, "__name__", "") in (
            "format",
            "format_map",
        ):

            def blocked(*_args: Any, **_kwargs: Any) -> str:
                msg = f"{value!r} is not safely callable"
                raise SecurityError(msg)

            return blocked
        return None

    def is_safe_callable(self, obj: Any) -> bool:
        owner = getattr(obj, "__self__", None)
        if isinstance(owner, str):
            return getattr(obj, "__name__", "") in _STR_METHODS
        return bool(super().is_safe_callable(obj))


@dataclass(frozen=True, slots=True)
class TemplateClock:
    """The time zone for ``now()``: HA's ``time_zone`` when it is known.

    Without it (HA is down after an app start), ``now()`` uses the local
    time zone of the app.
    """

    tz: tzinfo | None = None

    def now(self) -> datetime:
        """The current time in the HA time zone."""
        if self.tz is None:
            return datetime.now().astimezone()
        return datetime.now(self.tz)


def _make_env() -> _Sandbox:
    env = _Sandbox(autoescape=False, keep_trailing_newline=False, finalize=_finalize)
    for name in list(env.filters):
        if name not in _KEEP_FILTERS:
            del env.filters[name]
    for name in list(env.tests):
        if name not in _KEEP_TESTS:
            del env.tests[name]
    for name in list(env.globals):
        if name not in _KEEP_GLOBALS:
            del env.globals[name]
    env.filters.update(
        {
            "float": _to_float,
            "int": _to_int,
            "bool": _to_bool,
            "round": _round,
            "multiply": _multiply,
            "add": _add,
            "iif": _iif,
            "to_json": _to_json,
            "from_json": _from_json,
            "regex_match": _regex_match,
            "regex_search": _regex_search,
            "regex_replace": _regex_replace,
            "regex_findall": _regex_findall,
            "regex_findall_index": _regex_findall_index,
            "average": _average,
            "median": _median,
            "is_number": _is_number,
            "contains": _contains,
            "join": _join,
            "replace": _replace,
            "center": _center,
            "indent": _indent,
            "format": _format,
            "batch": _batch,
            "sum": _sum,
        }
    )
    env.tests.update(
        {
            "is_number": _is_number,
            "match": _regex_match,
            "search": _regex_search,
            "contains": _contains,
            "list": lambda v: isinstance(v, list),
            "datetime": lambda v: isinstance(v, datetime),
        }
    )
    _guard_all(env.filters)
    _guard_all(env.tests)
    return env


def _guard_all(table: dict[str, Any]) -> None:
    for name, fn in list(table.items()):
        table[name] = _guard(fn)


_PLAIN_GLOBALS: dict[str, Any] = {
    "float": _to_float,
    "int": _to_int,
    "bool": _to_bool,
    "iif": _iif,
    "is_number": _is_number,
    "average": _average,
    "median": _median,
    "min": _min_max(min),
    "max": _min_max(max),
    "timedelta": timedelta,
    "regex_match": _regex_match,
    "regex_search": _regex_search,
}


def _bound(view: ScopedStates, clock: TemplateClock) -> tuple[dict[str, Any], ...]:  # noqa: PLR0915
    """The functions that read the state view or the clock."""

    def is_state(entity_id: Any, value: Any) -> bool:
        state = view.get(entity_id)
        if state is None:
            return False
        if isinstance(value, str):
            return state.state == value
        return isinstance(value, Iterable) and state.state in value

    def state_attr(entity_id: Any, name: Any) -> Any:
        state = view.get(entity_id)
        return None if state is None else state.attributes.get(name)

    def is_state_attr(entity_id: Any, name: Any, value: Any) -> bool:
        state = view.get(entity_id)
        if state is None or name not in state.attributes:
            return False
        return bool(state.attributes[name] == value)

    def has_value(entity_id: Any) -> bool:
        state = view.get(entity_id)
        return state is not None and state.state not in _NO_VALUE

    def now() -> datetime:
        return clock.now()

    def utcnow() -> datetime:
        return datetime.now(UTC)

    def as_local(value: datetime) -> datetime:
        if clock.tz is None:
            return value.astimezone()
        return value.astimezone(clock.tz)

    def as_datetime(value: Any, default: Any = _MISSING) -> Any:
        try:
            if isinstance(value, datetime):
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return datetime.fromtimestamp(value, UTC)
            text = str(value)
            try:
                return datetime.fromtimestamp(float(text), UTC)
            except ValueError:
                return datetime.fromisoformat(text.replace(" ", "T", 1))
        except ValueError, TypeError, OverflowError, OSError:
            if default is _MISSING:
                raise _fail("as_datetime", value) from None
            return default

    def as_timestamp(value: Any, default: Any = _MISSING) -> Any:
        try:
            if isinstance(value, datetime):
                return value.timestamp()
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            moment = datetime.fromisoformat(str(value).replace(" ", "T", 1))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=clock.tz or now().tzinfo)
            return moment.timestamp()
        except ValueError, TypeError, OverflowError:
            if default is _MISSING:
                raise _fail("as_timestamp", value) from None
            return default

    def today_at(text: str = "00:00") -> datetime:
        try:
            hour, minute, *rest = (int(p) for p in str(text).split(":"))
        except ValueError:
            raise _fail("today_at", text) from None
        second = rest[0] if rest else 0
        return now().replace(hour=hour, minute=minute, second=second, microsecond=0)

    def timestamp_local(value: Any, default: Any = _MISSING) -> Any:
        try:
            moment = datetime.fromtimestamp(float(value), clock.tz or UTC)
            if clock.tz is None:
                moment = moment.astimezone()
            return moment.isoformat()
        except ValueError, TypeError, OverflowError, OSError:
            if default is _MISSING:
                raise _fail("timestamp_local", value) from None
            return default

    def timestamp_utc(value: Any, default: Any = _MISSING) -> Any:
        try:
            return datetime.fromtimestamp(float(value), UTC).isoformat()
        except ValueError, TypeError, OverflowError, OSError:
            if default is _MISSING:
                raise _fail("timestamp_utc", value) from None
            return default

    def timestamp_custom(
        value: Any,
        format_string: str = "%Y-%m-%d %H:%M:%S",
        local: bool = True,
        default: Any = _MISSING,
    ) -> Any:
        _check_format(format_string)
        try:
            moment = datetime.fromtimestamp(float(value), UTC)
            if local:
                moment = as_local(moment)
            return _check_size(moment.strftime(format_string))
        except ValueError, TypeError, OverflowError, OSError:
            if default is _MISSING:
                raise _fail("timestamp_custom", value) from None
            return default

    funcs: dict[str, Any] = {
        "states": _AllStates(view),
        "is_state": is_state,
        "state_attr": state_attr,
        "is_state_attr": is_state_attr,
        "has_value": has_value,
        "now": now,
        "utcnow": utcnow,
        "today_at": today_at,
        "as_datetime": as_datetime,
        "as_timestamp": as_timestamp,
        "as_local": as_local,
    }
    filters = {
        "is_state": is_state,
        "state_attr": state_attr,
        "is_state_attr": is_state_attr,
        "has_value": has_value,
        "as_datetime": as_datetime,
        "as_timestamp": as_timestamp,
        "as_local": as_local,
        "timestamp_local": timestamp_local,
        "timestamp_utc": timestamp_utc,
        "timestamp_custom": timestamp_custom,
    }
    tests = {
        "is_state": is_state,
        "is_state_attr": is_state_attr,
        "has_value": has_value,
    }
    return funcs, filters, tests


# ---------------------------------------------------------- static check


def _check_env() -> _Sandbox:
    env = _make_env()
    funcs, filters, tests = _bound(ScopedStates(), TemplateClock())
    env.globals.update(_PLAIN_GLOBALS)
    env.globals.update(funcs)
    env.filters.update(filters)
    env.tests.update(tests)
    return env


_ENV = _check_env()
KNOWN_GLOBALS = frozenset(_ENV.globals)
KNOWN_FILTERS = frozenset(_ENV.filters)
KNOWN_TESTS = frozenset(_ENV.tests)


def check_template(source: str, variables: Iterable[str] = VARIABLES) -> None:
    """Raise ``TemplateError`` if the template is outside the subset.

    The check finds syntax errors, unknown functions, filters and tests,
    and the ``import``, ``include``, ``extends`` and ``macro`` statements
    (the sandbox has no template loader).
    """
    if len(source) > MAX_TEMPLATE:
        msg = f"a template is longer than {MAX_TEMPLATE} characters"
        raise TemplateError(msg)
    try:
        tree = _ENV.parse(source)
    except TemplateSyntaxError as err:
        msg = f"template syntax error: {err.message} (line {err.lineno})"
        raise TemplateError(msg) from err
    blocked = (
        nodes.Import,
        nodes.FromImport,
        nodes.Include,
        nodes.Extends,
        nodes.Macro,
        nodes.CallBlock,
    )
    for stmt in tree.find_all(blocked):
        msg = f"the template statement {type(stmt).__name__.lower()} is not supported"
        raise TemplateError(msg)
    for flt in tree.find_all(nodes.Filter):
        if flt.name not in KNOWN_FILTERS:
            msg = f"the filter {flt.name!r} is not in the supported subset"
            raise TemplateError(msg)
    for test in tree.find_all(nodes.Test):
        if test.name not in KNOWN_TESTS:
            msg = f"the test {test.name!r} is not in the supported subset"
            raise TemplateError(msg)
    allowed = KNOWN_GLOBALS | frozenset(variables)
    try:
        undeclared = meta.find_undeclared_variables(tree)
    except TemplateSyntaxError as err:
        msg = f"template error: {err.message}"
        raise TemplateError(msg) from err
    unknown = sorted(undeclared - allowed)
    if unknown:
        msg = f"unknown names in the template: {', '.join(unknown)}"
        raise TemplateError(msg)


# ---------------------------------------------------------------- render


def parse_result(text: str) -> Any:
    """Turn rendered text into a native value, as HA does.

    ``"5"`` becomes 5, ``"[1, 2]"`` becomes a list, ``"True"`` becomes
    True. Text that is not a Python literal stays text. A number text with
    a leading zero (``"01"``) stays text.
    """
    try:
        result = literal_eval(text)
    except ValueError, TypeError, SyntaxError, MemoryError, RecursionError:
        return text
    if isinstance(result, (str, complex)):
        return text
    if (
        isinstance(result, (int, float))
        and not isinstance(result, bool)
        and _IS_NUMERIC.match(text) is None
    ):
        return text
    if isinstance(result, (set, frozenset)):
        return text
    return result


@dataclass(frozen=True, slots=True)
class TemplateEngine:
    """Renders rule templates against one scoped state view."""

    view: ScopedStates
    clock: TemplateClock = TemplateClock()
    seconds: float = RENDER_SECONDS

    def _render_sync(self, source: str, variables: Mapping[str, Any]) -> str:
        check_template(source, variables.keys() | VARIABLES)
        env = _make_env()
        funcs, filters, tests = _bound(self.view, self.clock)
        env.globals.update({k: _guard(v) for k, v in _PLAIN_GLOBALS.items()})
        env.globals.update(
            {k: v if k == "states" else _guard(v) for k, v in funcs.items()}
        )
        env.filters.update({k: _guard(v) for k, v in filters.items()})
        env.tests.update({k: _guard(v) for k, v in tests.items()})
        template = env.from_string(source)
        deadline = time.monotonic() + self.seconds

        def tracer(frame: FrameType, event: str, arg: Any) -> Any:
            if time.monotonic() > deadline:
                raise _Deadline
            return tracer

        previous = sys.gettrace()
        sys.settrace(tracer)
        try:
            text = template.render(dict(variables))
        except _Deadline:
            msg = f"the template took longer than {self.seconds} s"
            raise TemplateError(msg) from None
        except TemplateError:
            raise
        except (SecurityError, UndefinedError) as err:
            raise TemplateError(str(err)) from err
        except RecursionError as err:
            msg = "the template recursed too deep"
            raise TemplateError(msg) from err
        except Exception as err:
            msg = f"{type(err).__name__}: {err}"
            raise TemplateError(msg) from err
        finally:
            sys.settrace(previous)
        if len(text) > MAX_OUTPUT:
            msg = f"the template output is longer than {MAX_OUTPUT}"
            raise TemplateError(msg)
        return text.strip()

    async def render(self, source: str, variables: Mapping[str, Any]) -> str:
        """Render a template to text, in a worker thread with a time limit.

        The trace function stops a slow render inside Python code. One slow
        call in C, for example a regular expression that backtracks, cannot
        be stopped. The outer time limit then ends the wait, so that the
        invocation ends. The thread runs on until the call ends, and keeps
        one of the ``RENDER_THREADS`` threads.
        """
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            _RENDER_POOL, self._render_sync, source, dict(variables)
        )
        try:
            return await asyncio.wait_for(future, self.seconds + 1)
        except TimeoutError:
            msg = f"the template took longer than {self.seconds} s"
            raise TemplateError(msg) from None

    async def render_value(self, source: str, variables: Mapping[str, Any]) -> Any:
        """Render a template and parse the text into a native value."""
        return parse_result(await self.render(source, variables))


def is_true(value: Any) -> bool:
    """The truth of a rendered match template, as HA reads a condition.

    True for ``True``, a non-zero number, and the texts ``true``, ``yes``,
    ``on``, ``enable`` and ``1``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    return False
