"""What each native venue earns and loses, from real recorded data only.

One report for the four venues in :mod:`condor.native_venues` (Crypto.com,
Kraken xStocks, Revolut X, Trading 212), behind ``GET /<venue>/performance``.
Nothing here moves money or places an order, and the replay never contacts an
exchange: it reads the venue's own ledger (cash, positions, the filled orders
the service recorded) and the value history the recorder keeps.

The figures, and where each comes from:

* **Today / Since start**: the change in whole-account value (USD, the unit of
  the recorded history) from the start of the UTC day / from the first recorded
  sample, **net of confirmed deposits and withdrawals** and of the owner's
  excluded holdings. It is the same arithmetic as the loop cards' PnL
  (:func:`condor.native_accounts.performance`), so a deposit is never a profit.
  A missing baseline is ``None`` with a reason, never a guess; a history that
  starts after midnight is reported as "since <first sample>".
* **Realized**: replay of the ledger's filled orders in time order, per asset,
  with average-cost accounting, in the venue's quote currency and after fees
  (:func:`replay`).
* **Unrealized**: the bot allocation's equity at the bid, minus its cash, minus
  what the open positions cost.
* **Fees**: every filled order's fee converted to the quote currency; a figure
  from an estimate is named as such.
* **Trades**: filled orders, sells that closed a position, wins and losses.

Cost basis rules
================

A buy adds its units and its cost (value plus a fee paid in the quote currency;
a fee paid in the coin reduces the units received instead, which is the same
money). A sell realises ``proceeds - average cost * units sold`` where the
proceeds are net of a quote-currency fee. This mirrors, line by line, how each
service books a fill in its ledger, so the replay reproduces the ledger.

A position carried over at a sync is **adopted** with its cost restarted at the
bid at that moment (the services say so, and a sync starts a new order list). A
position can also predate the oldest order still kept (the ledger keeps the last
200). Neither has orders behind it, so the opening lot is *inferred* from the
ledger's own final position: opening units are the ledger's units minus what the
recorded orders changed, and the opening cost is solved from the ledger's final
cost (the cost after a series of buys and partial sells is linear in the opening
cost). When the opening lot was sold out completely its cost cannot be recovered;
those sells show no result rather than a guess.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from condor import native_accounts, native_venues, portfolio_history

log = logging.getLogger(__name__)

#: A report is reused this long (the page polls; the valuation reads the exchange).
CACHE_TTL_SEC = 5.0
#: The services keep only this many orders in the ledger.
ORDER_CAP = 200
#: A day-start baseline sample older than this is not used (an estimate beyond it
#: would be a guess).
MAX_BASELINE_GAP_SEC = 3 * 3600
#: Below this share of the opening lot still held, the ledger's rounded cost cannot
#: pin the opening cost down (the rounding is divided by the share): not solved.
MIN_OPENING_SHARE = Decimal("0.0001")
ZERO = Decimal(0)
#: Venues whose ledger takes a sale's fee in the coin out of the position as well.
_SELL_FEE_UNITS_OUT = frozenset({"revolut_x", "kraken_xstocks"})

_cache: dict[tuple[str, int], tuple[float, dict]] = {}


def forget() -> None:
    """Drop the cached reports (tests)."""
    _cache.clear()


# ── small helpers ──


def _dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default
    return number if number.is_finite() else default


def _f(value: Decimal | float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def _tolerance(*amounts: Decimal) -> Decimal:
    """Unit rounding slack: ledger figures are stored to 8 decimals."""
    biggest = max((abs(a) for a in amounts), default=ZERO)
    return max(Decimal("0.00000002"), biggest * Decimal("0.000001"))


# ── the replay ──


@dataclass
class Fill:
    """One filled order, in the units the ledger booked it."""

    index: int
    time: float
    side: str
    symbol: str
    units_in: Decimal = ZERO  # buys: units added to the position
    cash_cost: Decimal = ZERO  # buys: quote spent, fee included when paid in quote
    units_out: Decimal = ZERO  # sells: units removed from the position
    proceeds: Decimal = ZERO  # sells: quote received net of a quote fee
    units_adjust: Decimal = ZERO  # a balance correction (<= 0) applied after it
    fee_value: Decimal = ZERO  # the fee in the quote currency
    fee_estimated: bool = False
    fee_unconverted: bool = False
    order: Mapping[str, Any] = field(default_factory=dict)


def _fee_parts(
    venue_key: str,
    order: Mapping[str, Any],
    symbol: str,
    quote: str,
    price: Decimal | None,
) -> tuple[Decimal, Decimal, Decimal, bool]:
    """``(fee paid in quote, fee paid in units, fee in quote terms, convertible)``.

    The currency the order says it was charged in decides how the ledger booked it:
    in the quote currency it is cash; in the coin it is taken off the units. Kraken
    reports the number in the quote currency even when the fee was taken from the
    coin (the currency then reads ``AAPLx``), so it converts at the fill price.
    """
    fee = _dec(order.get("fee"), ZERO) or ZERO
    if fee <= 0:
        return ZERO, ZERO, ZERO, True
    currency = str(order.get("fee_currency") or "").strip().upper()
    quote = quote.upper()
    if currency == quote:
        return fee, ZERO, fee, True
    if venue_key == "kraken_xstocks" and currency == f"{symbol.upper()}X":
        if price and price > 0:
            return ZERO, fee / price, fee, True
        return ZERO, ZERO, fee, False
    if currency == symbol.upper():
        if price and price > 0:
            return ZERO, fee, fee * price, True
        return ZERO, fee, ZERO, False
    return ZERO, ZERO, ZERO, False


def fills_of(venue_key: str, orders: list, quote: str) -> list[Fill]:
    """The ledger's filled orders in time order, each as the ledger booked it."""
    fills: list[Fill] = []
    for index, order in enumerate(orders or []):
        if not isinstance(order, Mapping):
            continue
        side = str(order.get("side") or "").lower()
        symbol = str(order.get("symbol") or "").upper()
        quantity = _dec(order.get("quantity"), ZERO) or ZERO
        value = _dec(order.get("value"), ZERO) or ZERO
        stamp = _dec(order.get("time"))
        if side not in ("buy", "sell") or not symbol or quantity <= 0 or stamp is None:
            continue
        price = value / quantity if value > 0 else None
        cash_fee, unit_fee, fee_value, convertible = _fee_parts(
            venue_key, order, symbol, quote, price
        )
        fill = Fill(
            index=index,
            time=float(stamp),
            side=side,
            symbol=symbol,
            fee_value=fee_value,
            fee_estimated=(
                str(order.get("fee_source") or "") == "estimate" and fee_value > 0
            ),
            fee_unconverted=not convertible,
            order=order,
        )
        if side == "buy":
            fill.units_in = quantity - unit_fee
            fill.cash_cost = value + cash_fee
        else:
            fill.proceeds = value - cash_fee
            fill.units_out = quantity + (
                unit_fee if venue_key in _SELL_FEE_UNITS_OUT else ZERO
            )
        correction = (order.get("balance_correction") or {}).get("units")
        if isinstance(correction, Mapping):
            before, after = _dec(correction.get("ledger")), _dec(
                correction.get("set_to")
            )
            if before is not None and after is not None and after < before:
                fill.units_adjust = after - before
        fills.append(fill)
    fills.sort(key=lambda f: (f.time, f.index))
    return fills


@dataclass
class SellResult:
    fill: Fill
    pnl: Decimal | None
    cost: Decimal | None
    reason: str = ""
    closed_position: bool = False


@dataclass
class AssetReplay:
    symbol: str
    sells: list[SellResult] = field(default_factory=list)
    opening_units: Decimal = ZERO
    opening_cost: Decimal | None = None
    #: ``"adopted_at_sync"`` or ``"before_recorded_orders"`` (the order list was full).
    opening_origin: str = ""
    partial_history: bool = False
    mismatch: str = ""
    round_trips: int = 0


def _replay_asset(
    symbol: str,
    fills: list[Fill],
    ledger_position: Mapping[str, Any] | None,
    truncated: bool,
) -> AssetReplay:
    out = AssetReplay(symbol)
    final_units = _dec((ledger_position or {}).get("units"), ZERO) or ZERO
    final_cost = _dec((ledger_position or {}).get("cost"))

    # What the recorded orders changed, to find the units held before the first one.
    net = sum((f.units_in - f.units_out + f.units_adjust for f in fills), ZERO)
    biggest = max(
        [final_units, *(f.units_in for f in fills), *(f.units_out for f in fills)]
    )
    tol = _tolerance(biggest)
    opening = final_units - net
    if opening < -tol:
        # The orders sold more than the ledger ever held: the ledger moved by
        # something the orders do not show. Replay from nothing and say so.
        out.mismatch = "orders and ledger positions disagree"
        opening = ZERO
    elif opening <= tol:
        opening = ZERO
    out.opening_units = opening
    if opening > 0:
        out.opening_origin = (
            "before_recorded_orders" if truncated else "adopted_at_sync"
        )
        out.partial_history = truncated

    # Cost is linear in the opening cost C0: cost = a * C0 + b (a is the share of the
    # opening lot still held). Each sale's cost out is A * C0 + B.
    units, a, b = opening, (Decimal(1) if opening > 0 else ZERO), ZERO
    sales: list[tuple[Fill, Decimal | None, Decimal, Decimal, bool]] = []
    for fill in fills:
        if fill.side == "buy":
            units += fill.units_in
            b += fill.cash_cost
        else:
            if units <= tol:
                sales.append((fill, None, ZERO, ZERO, False))
                continue
            taken = fill.units_out
            if taken > units + tol:
                out.mismatch = out.mismatch or "a sale is larger than the position"
                taken = units
            closes = taken >= units - tol
            cost_a, cost_b = a * taken / units, b * taken / units
            sales.append((fill, fill.proceeds, cost_a, cost_b, closes))
            if closes:
                units, a, b = ZERO, ZERO, ZERO
            else:
                keep = (units - taken) / units
                units, a, b = units - taken, a * keep, b * keep
        if fill.units_adjust < 0:
            if units + fill.units_adjust <= tol:
                units, a, b = ZERO, ZERO, ZERO
            else:
                units += fill.units_adjust

    opening_cost: Decimal | None = ZERO if opening == 0 else None
    if opening > 0:
        if a >= MIN_OPENING_SHARE and final_cost is not None and final_units > 0:
            solved = (final_cost - b) / a
            if solved >= 0:
                opening_cost = solved
            elif solved > Decimal("-0.000001"):
                opening_cost = ZERO
            else:
                out.mismatch = out.mismatch or "ledger cost does not fit the orders"
    out.opening_cost = opening_cost

    for fill, proceeds, cost_a, cost_b, closes in sales:
        if proceeds is None:
            out.sells.append(
                SellResult(fill, None, None, "no recorded cost for this sale")
            )
            continue
        if cost_a > 0 and opening_cost is None:
            out.sells.append(
                SellResult(
                    fill,
                    None,
                    None,
                    "cost of the position held before the first recorded order "
                    "is not on record",
                    closes,
                )
            )
        else:
            cost = cost_a * (opening_cost or ZERO) + cost_b
            out.sells.append(SellResult(fill, proceeds - cost, cost, "", closes))
        if closes:
            out.round_trips += 1
    return out


@dataclass
class Replay:
    assets: dict[str, AssetReplay] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    truncated: bool = False

    @property
    def sells(self) -> list[SellResult]:
        found = [s for asset in self.assets.values() for s in asset.sells]
        return sorted(found, key=lambda s: (s.fill.time, s.fill.index))


def replay(venue_key: str, ledger: Mapping[str, Any]) -> Replay:
    """Average-cost replay of the ledger's filled orders, per asset.

    ``ledger`` is the service's ledger: ``quote``, ``positions`` (``units`` and
    ``cost`` per symbol) and ``orders``. Pure: nothing is read or written.
    """
    quote = str(ledger.get("quote") or "USD")
    orders = ledger.get("orders") or []
    fills = fills_of(venue_key, orders, quote)
    result = Replay(fills=fills, truncated=len(orders) >= ORDER_CAP)
    positions = {str(k).upper(): v for k, v in (ledger.get("positions") or {}).items()}
    symbols = sorted({f.symbol for f in fills} | set(positions))
    for symbol in symbols:
        mine = [f for f in fills if f.symbol == symbol]
        if not mine:
            continue
        result.assets[symbol] = _replay_asset(
            symbol, mine, positions.get(symbol), result.truncated
        )
    return result


# ── the report ──


def _utc(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _trade_row(sell: SellResult) -> dict:
    cost = sell.cost
    pct = (
        _f(sell.pnl / cost * 100, 4)
        if sell.pnl is not None and cost and cost > 0
        else None
    )
    return {
        "time": sell.fill.time,
        "symbol": sell.fill.symbol,
        "order_id": sell.fill.order.get("order_id"),
        "client_oid": sell.fill.order.get("client_oid"),
        "pnl": _f(sell.pnl),
        "pnl_pct": pct,
        "closed_position": sell.closed_position,
        "reason": sell.reason or None,
    }


def realized_block(run: Replay, currency: str) -> dict:
    """Realized P&L, trade counts and fees, from a replay."""
    sells = run.sells
    known = [s for s in sells if s.pnl is not None]
    unknown = [s for s in sells if s.pnl is None]
    total = sum((s.pnl for s in known), ZERO)
    wins = [s for s in known if s.pnl > 0]
    losses = [s for s in known if s.pnl < 0]
    decided = len(wins) + len(losses)
    best = max(known, key=lambda s: s.pnl, default=None)
    worst = min(known, key=lambda s: s.pnl, default=None)
    partial = [a.symbol for a in run.assets.values() if a.partial_history]
    first = run.fills[0].time if run.fills else None
    if sells and not known:
        reason = unknown[0].reason
    elif unknown:
        reason = (
            f"{len(unknown)} sale(s) have no recorded cost and are not counted: "
            f"{unknown[0].reason}"
        )
    else:
        reason = None
    fee_total = sum((f.fee_value for f in run.fills), ZERO)
    fee_estimated = sum((f.fee_value for f in run.fills if f.fee_estimated), ZERO)
    unconverted = sum(1 for f in run.fills if f.fee_unconverted)
    return {
        "realized": {
            "value": None if (sells and not known) else _f(total),
            "complete": not unknown,
            "reason": reason,
            "sells": len(sells),
            "unknown_sells": len(unknown),
            "partial_history": bool(partial),
            "partial_assets": partial,
            "history_starts_at": first,
            "currency": currency,
        },
        "fees": {
            "value": _f(fee_total),
            "estimated": _f(fee_estimated),
            "is_estimate": fee_estimated > 0,
            "unconverted": unconverted,
            "complete": unconverted == 0,
            "currency": currency,
        },
        "trades": {
            "filled_orders": len(run.fills),
            "buys": sum(1 for f in run.fills if f.side == "buy"),
            "sells": len(sells),
            "round_trips_closed": sum(a.round_trips for a in run.assets.values()),
            "wins": len(wins),
            "losses": len(losses),
            "flat": len(known) - decided,
            "win_rate": _f(Decimal(len(wins)) / decided, 4) if decided else None,
            "best": _trade_row(best) if best and best.pnl > 0 else None,
            "worst": _trade_row(worst) if worst and worst.pnl < 0 else None,
        },
    }


def _asset_rows(run: Replay) -> list[dict]:
    rows = []
    for asset in run.assets.values():
        known = [s.pnl for s in asset.sells if s.pnl is not None]
        rows.append(
            {
                "symbol": asset.symbol,
                "realized": _f(sum(known, ZERO)) if known else None,
                "sells": len(asset.sells),
                "unknown_sells": sum(1 for s in asset.sells if s.pnl is None),
                "opening_units": _f(asset.opening_units, 8) or None,
                "opening_cost": _f(asset.opening_cost),
                "opening_origin": asset.opening_origin or None,
                "partial_history": asset.partial_history,
                "mismatch": asset.mismatch or None,
            }
        )
    return rows


def _ledger_totals(ledger: Mapping[str, Any]) -> tuple[Decimal | None, Decimal]:
    cash = _dec(ledger.get("cash"))
    cost = sum(
        (_dec((p or {}).get("cost"), ZERO) or ZERO)
        for p in (ledger.get("positions") or {}).values()
    )
    return cash, cost


async def _equity_block(
    service: Any, user_id: int, ledger: Mapping[str, Any], currency: str
) -> dict:
    cash, cost = _ledger_totals(ledger)
    block: dict[str, Any] = {
        "now": None,
        "cash": _f(cash),
        "positions_cost": _f(cost),
        "unrealized": None,
        "currency": currency,
        "reason": None,
    }
    try:
        valuation = await service.valuation(user_id)
        equity = _dec(valuation.get("equity"))
        if equity is None or cash is None:
            raise ValueError("The valuation returned no equity")
    except ValueError as error:
        block["reason"] = str(error)[:200] or "Equity could not be valued"
        return block
    except Exception as error:  # noqa: BLE001 - never hide the recorded figures
        log.warning("performance valuation skipped (%s)", type(error).__name__)
        block["reason"] = f"Equity could not be valued ({type(error).__name__})"
        return block
    block["now"] = _f(equity)
    block["unrealized"] = _f(equity - cash - cost)
    return block


def _window(
    *,
    value: float | None,
    since: float | None,
    reason: str | None,
    partial: bool = False,
    estimated: bool = False,
    flows: float = 0.0,
    note: str = "",
) -> dict:
    return {
        "value": None if value is None else round(float(value), 4),
        "currency": "USD",
        "since": since,
        "since_label": _utc(since),
        "partial": partial,
        "estimated": estimated,
        "flows_usd": round(float(flows), 4),
        "reason": reason,
        "note": note or None,
    }


async def _account_block(
    venue: native_venues.NativeVenue,
    user_id: int,
    services: Mapping[str, Any] | None,
    now: float,
) -> dict:
    """Today and since-start results of the whole account, deposits netted out."""
    first, last = portfolio_history.bounds(user_id, venue.label)
    if first is None or last is None:
        gone = _window(value=None, since=None, reason="No equity history recorded yet")
        return {"today": gone, "since_start": dict(gone), "equity_now": None}

    day_start = int(now // 86400 * 86400)
    before, after = portfolio_history.nearest(user_id, venue.label, [day_start])[0]
    begin = native_accounts.pick(before, after, day_start)

    overall = await native_accounts.performance(
        venue, user_id, [], services=services, now=now
    )
    flows_note = ""
    if not overall.flows_tracked:
        flows_note = (
            "Deposits and withdrawals are not tracked for this venue: a deposit "
            "would read as a gain."
        )
    pending_note = (
        f"{overall.pending_count} unconfirmed deposit/withdrawal(s) are not "
        "subtracted; confirm or dismiss them in Flows."
        if overall.pending_count
        else ""
    )

    def notes(*parts: str) -> str:
        return " ".join(p for p in (*parts, flows_note, pending_note) if p)

    if overall.unavailable:
        since_start = _window(value=None, since=None, reason="No equity history")
    else:
        since_start = _window(
            value=overall.total_pnl,
            since=overall.since or float(first[0]),
            reason=None,
            estimated=overall.estimated,
            flows=overall.flows_usd,
            note=notes(overall.note),
        )

    if begin is None:
        today = _window(value=None, since=None, reason="No equity history")
    elif before is None:
        # The history starts after midnight: the result is since that first sample.
        spans = [native_accounts.SessionSpan(1, float(first[0]), live=True)]
        run = await native_accounts.performance(
            venue, user_id, spans, services=services, now=now
        )
        session = run.sessions[0] if run.sessions else None
        today = _window(
            value=session.pnl if session else None,
            since=float(first[0]),
            reason=None if session and session.pnl is not None else "Not available",
            partial=True,
            estimated=bool(session and session.estimated),
            flows=session.flows_usd if session else 0.0,
            note=notes(
                f"History starts at {_utc(float(first[0]))}, after the start of "
                "the UTC day."
            ),
        )
    elif not begin.exact and abs(begin.gap) > MAX_BASELINE_GAP_SEC:
        today = _window(
            value=None,
            since=None,
            reason=(
                "No equity sample near the start of the UTC day "
                f"(nearest is {native_accounts._span_text(begin.gap)} away)"
            ),
        )
    else:
        spans = [native_accounts.SessionSpan(1, float(day_start), live=True)]
        run = await native_accounts.performance(
            venue, user_id, spans, services=services, now=now
        )
        session = run.sessions[0] if run.sessions else None
        today = _window(
            value=session.pnl if session else None,
            since=float(day_start),
            reason=(
                None
                if session and session.pnl is not None
                else (session.note if session else "Not available")
            ),
            estimated=bool(session and session.estimated),
            flows=session.flows_usd if session else 0.0,
            note=notes(session.note if session else ""),
        )

    equity_now = None
    try:
        view = await native_accounts.value_account(venue, user_id, services=services)
        equity_now = round(float(view["total_usd"]), 4)
    except Exception:  # noqa: BLE001 - the recorded figures still stand
        pass
    return {"today": today, "since_start": since_start, "equity_now": equity_now}


def _static_notes(currency: str) -> list[str]:
    return [
        f"Realized, unrealized and fees are in {currency}, the bot allocation's "
        "currency, from the orders the bot's ledger recorded; Today and Since "
        "start are the whole account in USD from the recorded value history, net "
        "of confirmed deposits and withdrawals.",
        "Realized is after fees. Cost uses the average cost of the position; a "
        "position adopted at a sync starts at its bid then.",
    ]


async def report(
    venue_key: str,
    user_id: int,
    *,
    services: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict:
    """The performance report of one native venue for one user.

    Cached for a few seconds when ``now`` is not given. Every part that cannot be
    stated is ``None`` with a ``reason``; the others are still returned.
    """
    cache_key = (venue_key, user_id)
    if now is None:
        cached = _cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SEC:
            return cached[1]
    stamp = time.time() if now is None else now
    venue = native_venues.get(venue_key)
    service = (services or {}).get(venue.key) or venue.service
    out: dict[str, Any] = {
        "venue": venue.key,
        "label": venue.label,
        "as_of": int(stamp),
        "notes": [],
    }
    try:
        status = service.status(user_id)
        ledger = status.get("ledger") or {}
    except Exception as error:  # noqa: BLE001 - an unreadable ledger is a state
        log.warning("%s performance: ledger unreadable (%s)", venue.label, error)
        out.update(
            currency="",
            connected=False,
            synced=False,
            available=False,
            reason=f"The {venue.label} ledger could not be read",
        )
        return out
    currency = str(ledger.get("quote") or status.get("quote") or "")
    out.update(
        currency=currency,
        connected=bool(status.get("connected")),
        synced=bool(ledger.get("synced")),
        available=True,
        reason=None,
    )

    run = replay(venue.key, ledger)
    out.update(realized_block(run, currency))
    out["assets"] = _asset_rows(run)
    out["orders"] = [_trade_row(s) for s in run.sells]
    notes = _static_notes(currency)
    realized = out["realized"]
    if realized["partial_history"]:
        notes.append(
            "The order list starts after some positions were opened (the ledger "
            f"keeps the last {ORDER_CAP} orders); their cost is the ledger's, so "
            "older results are not included."
        )
    if realized["reason"]:
        notes.append(realized["reason"])
    if out["fees"]["is_estimate"]:
        notes.append(
            "Some fees are estimates (the venue had not reported them when the "
            "fill was booked)."
        )
    if out["fees"]["unconverted"]:
        notes.append(
            f"{out['fees']['unconverted']} fee(s) in another currency are not "
            "counted."
        )

    if out["connected"]:
        out["equity"] = await _equity_block(service, user_id, ledger, currency)
        if out["equity"]["reason"]:
            notes.append(out["equity"]["reason"])
    else:
        _cash, cost = _ledger_totals(ledger)
        out["equity"] = {
            "now": None,
            "cash": _f(_cash),
            "positions_cost": _f(cost),
            "unrealized": None,
            "currency": currency,
            "reason": "Not connected",
        }
    out["unrealized"] = {
        "value": out["equity"]["unrealized"],
        "currency": currency,
        "reason": out["equity"]["reason"],
    }

    if out["connected"]:
        try:
            account = await _account_block(venue, user_id, services, stamp)
        except (
            Exception
        ) as error:  # noqa: BLE001 - history never blocks the ledger figures
            log.warning("%s account history skipped (%s)", venue.label, error)
            gone = _window(value=None, since=None, reason="History could not be read")
            account = {"today": gone, "since_start": dict(gone), "equity_now": None}
    else:
        gone = _window(value=None, since=None, reason="Not connected")
        account = {"today": gone, "since_start": dict(gone), "equity_now": None}
    out["today"] = account["today"]
    out["since_start"] = account["since_start"]
    out["account_equity_usd"] = account["equity_now"]
    out["notes"] = notes
    if now is None:
        _cache[cache_key] = (time.monotonic(), out)
    return out
