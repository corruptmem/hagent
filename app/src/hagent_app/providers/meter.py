"""Budgets, cost reservation and call records.

Terms:

- **Reservation**: the expected cost of a call, held against the budgets
  before the call starts. Parallel calls see each other's reservations, so
  they cannot spend more than the budget together.
- **Budget day**: the local date. The Supervisor sets the time zone of the
  app to the time zone of HA, so budgets reset at local midnight in the
  home. The ``clock`` argument replaces the time source in tests.

Each adapter wraps a provider call in ``Meter.call``:

```python
async with meter.call(spec, estimate) as call:
    response = await send()
    call.complete(usage, provider_cost_usd=...)
```

``call`` reserves the cost first. It raises ``BudgetExceededError`` when the
reservation does not fit. On exit it writes one ``provider_calls`` row and
deletes the reservation in one transaction.

The reservation check and insert run in one ``BEGIN IMMEDIATE`` transaction
on the single database thread, so two reservations never interleave.

A call can cost more than its reservation. The meter records the real cost.
The next reservation then sees the higher spend.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from hagent_app.providers.config import KeySource, ProvidersConfig, redact
from hagent_app.providers.errors import BudgetExceededError, PriceUnknownError
from hagent_app.providers.prices import (
    PricedCost,
    PriceTable,
    TokenUsage,
    from_pusd,
    to_pusd,
)
from hagent_app.storage import Database, transaction

_LOG = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def local_now() -> datetime:
    """The current time in the local time zone of the app."""
    return datetime.now().astimezone()


@dataclass(frozen=True, slots=True)
class CallSpec:
    """What a call is: who pays, which provider, which model.

    ``agent_id`` is ``None`` for a cost that belongs to no agent.
    ``price_provider`` is the provider name of the price table (see
    ``prices``).
    """

    agent_id: str | None
    provider: str
    call_type: str
    model: str
    price_provider: str


@dataclass(frozen=True, slots=True)
class Reservation:
    """A reservation row."""

    id: int
    day: str
    agent_id: str | None
    amount_pusd: int
    priced: bool


@dataclass(frozen=True, slots=True)
class BudgetState:
    """Spend, reservations and budget of one agent or the installation."""

    day: str
    spent_usd: Decimal
    reserved_usd: Decimal
    budget_usd: Decimal

    @property
    def remaining_usd(self) -> Decimal:
        return self.budget_usd - self.spent_usd - self.reserved_usd


@dataclass(slots=True)
class MeteredCall:
    """A call in progress. The adapter calls ``complete`` on success."""

    spec: CallSpec
    reservation: Reservation
    started_at: datetime
    usage: TokenUsage | None = None
    provider_cost_usd: Decimal | None = None
    charge_reservation: bool = False

    def complete(
        self, usage: TokenUsage, provider_cost_usd: Decimal | float | None = None
    ) -> None:
        """Record the token counts and the cost that the provider reported."""
        self.usage = usage
        self.provider_cost_usd = (
            None if provider_cost_usd is None else Decimal(str(provider_cost_usd))
        )

    def usage_unknown(self) -> None:
        """The call ran, but its usage is not known (for example, a stream
        that stopped before its usage event). The reservation counts as the
        cost, because the provider still bills the call."""
        self.charge_reservation = True


class Meter:
    """Budgets and call records. See the module docstring."""

    def __init__(
        self,
        db: Database,
        config: ProvidersConfig,
        prices: PriceTable,
        keys: KeySource,
        clock: Clock = local_now,
    ) -> None:
        self._db = db
        self._config = config
        self.prices = prices
        self._keys = keys
        self._clock = clock
        self._agent_budgets: dict[str, float] = dict(config.agent_budgets_usd)

    # Budgets

    def agent_budget_usd(self, agent_id: str) -> Decimal:
        """The daily budget of ``agent_id``."""
        value = self._agent_budgets.get(agent_id, self._config.agent_daily_budget_usd)
        return Decimal(str(value))

    def set_config(self, config: ProvidersConfig) -> None:
        """Replace the budgets and limits (a settings change)."""
        self._config = config

    def set_agent_budgets(self, budgets: Mapping[str, float]) -> None:
        """Replace the per-agent budgets (for the agent definitions)."""
        self._agent_budgets = dict(budgets)

    def installation_budget_usd(self) -> Decimal:
        """The daily budget of the installation."""
        return Decimal(str(self._config.installation_daily_budget_usd))

    def today(self) -> str:
        """The current budget day as an ISO date."""
        return self._clock().date().isoformat()

    async def budget_state(self, agent_id: str | None) -> BudgetState:
        """Today's state of an agent budget, or of the installation (None)."""
        day = self.today()
        spent, reserved = await self._db.run(lambda c: _totals(c, day, agent_id))
        budget = (
            self.installation_budget_usd()
            if agent_id is None
            else self.agent_budget_usd(agent_id)
        )
        return BudgetState(day, from_pusd(spent), from_pusd(reserved), budget)

    # Reservations

    def estimate(self, spec: CallSpec, usage: TokenUsage) -> tuple[int, bool]:
        """Return the amount to reserve in pico-USD and whether it is priced.

        Raise ``PriceUnknownError`` when no price is known and no
        ``unknown_price_reserve_usd`` is set.
        """
        try:
            priced = self.prices.cost(spec.price_provider, spec.model, usage)
        except PriceUnknownError:
            fallback = self._config.unknown_price_reserve_usd
            if fallback is None:
                raise
            return to_pusd(fallback), False
        return to_pusd(priced.usd), True

    async def reserve(
        self, spec: CallSpec, amount_pusd: int, priced: bool
    ) -> Reservation:
        """Reserve ``amount_pusd``. Raise ``BudgetExceededError``."""
        day = self.today()
        agent_budget = (
            None
            if spec.agent_id is None
            else to_pusd(self.agent_budget_usd(spec.agent_id))
        )
        install_budget = to_pusd(self.installation_budget_usd())
        now = self._clock().isoformat()

        def run(conn: sqlite3.Connection) -> Reservation:
            with transaction(conn):
                if agent_budget is not None:
                    spent, held = _totals(conn, day, spec.agent_id)
                    if spent + held + amount_pusd > agent_budget:
                        msg = f"daily budget of agent {spec.agent_id} is spent"
                        raise BudgetExceededError(msg, "agent")
                spent, held = _totals(conn, day, None)
                if spent + held + amount_pusd > install_budget:
                    msg = "daily budget of the installation is spent"
                    raise BudgetExceededError(msg, "installation")
                cur = conn.execute(
                    "INSERT INTO budget_reservations"
                    " (created_at, budget_day, agent_id, amount_pusd)"
                    " VALUES (?, ?, ?, ?)",
                    (now, day, spec.agent_id, amount_pusd),
                )
                assert cur.lastrowid is not None  # noqa: S101 - sqlite always sets it
                return Reservation(
                    cur.lastrowid, day, spec.agent_id, amount_pusd, priced
                )

        return await self._db.run(run)

    async def clear_reservations(self) -> int:
        """Delete all reservations. Call it on start: no call survives a restart."""

        def run(conn: sqlite3.Connection) -> int:
            return conn.execute("DELETE FROM budget_reservations").rowcount

        return await self._db.run(run)

    # Calls

    @asynccontextmanager
    async def call(
        self, spec: CallSpec, estimate: TokenUsage
    ) -> AsyncIterator[MeteredCall]:
        """Reserve, run the block, then record the call. See the module docstring."""
        amount, priced = self.estimate(spec, estimate)
        reservation = await self.reserve(spec, amount, priced)
        call = MeteredCall(spec, reservation, self._clock())
        try:
            yield call
        except BaseException as err:
            error = redact(f"{type(err).__name__}: {err}", self._keys)
            # Shield the record, so a cancelled call still frees its reservation.
            await asyncio.shield(self._finish(call, error=error))
            raise
        if call.usage is None:
            await self._finish(call, error="the provider did not report usage")
        else:
            await self._finish(call, error=None)

    async def _finish(self, call: MeteredCall, error: str | None) -> None:
        spec = call.spec
        usage = call.usage or TokenUsage()
        computed: PricedCost | None = None
        if call.usage is not None:
            try:
                computed = self.prices.cost(spec.price_provider, spec.model, usage)
            except PriceUnknownError:
                computed = None
        computed_pusd = None if computed is None else to_pusd(computed.usd)
        provider_pusd = (
            None if call.provider_cost_usd is None else to_pusd(call.provider_cost_usd)
        )
        if provider_pusd is not None:
            cost, source = provider_pusd, "provider"
        elif computed_pusd is not None:
            cost, source = computed_pusd, "price_table"
        elif call.usage is not None or call.charge_reservation:
            # No price or no usage: count the reservation, so the budget
            # still holds.
            cost, source = call.reservation.amount_pusd, "reserved"
        else:
            cost, source = 0, "none"
        drift = None
        if provider_pusd is not None and computed_pusd:
            drift = (provider_pusd - computed_pusd) / computed_pusd
            if abs(drift) > self._config.drift_warn_ratio:
                _LOG.warning(
                    "cost drift %.1f%% for %s %s: reported %s USD, computed %s USD",
                    drift * 100,
                    spec.provider,
                    spec.model,
                    from_pusd(provider_pusd),
                    from_pusd(computed_pusd),
                )
        row = (
            call.started_at.isoformat(),
            self._clock().isoformat(),
            call.reservation.day,
            spec.agent_id,
            spec.provider,
            spec.call_type,
            spec.model,
            "error" if error else "ok",
            error,
            usage.input_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            usage.output_tokens,
            cost,
            source,
            computed_pusd,
            provider_pusd,
            drift,
            None if computed is None else computed.source,
            None if computed is None else computed.version,
            call.reservation.amount_pusd,
        )
        reservation_id = call.reservation.id

        def run(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute(
                    "DELETE FROM budget_reservations WHERE id = ?", (reservation_id,)
                )
                conn.execute(_INSERT_CALL, row)

        await self._db.run(run)


_INSERT_CALL = (
    "INSERT INTO provider_calls (started_at, finished_at, budget_day, agent_id,"
    " provider, call_type, model, status, error, input_tokens, cache_read_tokens,"
    " cache_write_tokens, output_tokens, cost_pusd, cost_source,"
    " computed_cost_pusd, provider_cost_pusd, drift, price_source, price_version,"
    " reserved_pusd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
    " ?, ?, ?, ?)"
)


def _totals(
    conn: sqlite3.Connection, day: str, agent_id: str | None
) -> tuple[int, int]:
    """Return (spent, reserved) in pico-USD for one agent or for all (None)."""
    where, args = ("budget_day = ?", [day])
    if agent_id is not None:
        where += " AND agent_id = ?"
        args.append(agent_id)
    spent = conn.execute(
        f"SELECT COALESCE(SUM(cost_pusd), 0) FROM provider_calls WHERE {where}",  # noqa: S608 - fixed clauses
        args,
    ).fetchone()[0]
    held = conn.execute(
        f"SELECT COALESCE(SUM(amount_pusd), 0) FROM budget_reservations WHERE {where}",  # noqa: S608 - fixed clauses
        args,
    ).fetchone()[0]
    return int(spent), int(held)
