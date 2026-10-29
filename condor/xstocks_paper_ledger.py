"""A simulated account for the paper xStocks crew.

The paper crew used to log and score calls only: no cash, no positions, so no
equity and no PnL. This gives it a per-user ledger that behaves like the live
Kraken xStocks one (:mod:`condor.kraken_xstocks`) but never sends anything:

* cash, positions (units and cost) and an order history, in
  ``CONDOR_DATA_DIR/xstocks_paper/<user>/ledger.json``;
* sizing, session, spread, lot, minimum and capital guards are the live module's
  own (they are called, not copied), so the two crews are held to the same rules;
* a fill is walked, deterministically, down the LIVE public Kraken order book of
  the moment: levels are taken at the touch outward while they stay inside the
  order's limit price, so a thin book gives a partial fill. Every fill pays the
  taker fee and a small slippage allowance, recorded per order;
* equity is cash plus every position at the best bid, the price a sale would
  meet first, in USD.

Only Kraken's public endpoints are read (ticker, depth, pairs). There is no key,
no private call and no order: nothing here can move money. The account holds no
deposits or withdrawals by design. The owner can reset it, which archives the old
ledger and starts again from :data:`START_CASH`; the reset is logged so it never
reads as a loss (:meth:`XStocksPaperAccount.adjustments`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from condor import kraken_xstocks as live
from condor import xstocks_paper
from condor.fsutil import atomic_write_json
from condor.paths import data_dir

log = logging.getLogger(__name__)

VENUE = "xstocks_paper"
LABEL = "Kraken xStocks (paper)"
QUOTE = "USD"
#: What a new or reset paper account starts with (USD).
START_CASH = Decimal("100")
#: Kraken's taker fee on the xStocks pairs as the owner reported it, per side.
TAKER_FEE_PCT = Decimal("0.08")
#: Extra adverse move applied to every fill price, per side (percent): the book
#: can move between the snapshot and a real fill. Override with this variable.
SLIPPAGE_ENV = "CONDOR_XSTOCKS_PAPER_SLIPPAGE_PCT"
DEFAULT_SLIPPAGE_ALLOWANCE_PCT = Decimal("0.02")
MAX_SLIPPAGE_ALLOWANCE_PCT = Decimal("1")
#: An order below this value (USD) is refused.
MIN_ORDER_USD = Decimal("0.5")
BOOK_DEPTH = 50
MAX_ORDERS = 500
MAX_ORDERS_PER_MINUTE = 10
MAX_RESETS = 200
NOTE = (
    "SIMULATED: a paper account on Kraken's public order book. No real money, "
    "and not part of any real total."
)
_CLIENT_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_PRECISION = Decimal("0.00000001")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_PRECISION, rounding=ROUND_HALF_UP)


def slippage_allowance_pct() -> Decimal:
    """The configured per-side slippage allowance (percent), the default when unset or invalid."""
    raw = os.environ.get(SLIPPAGE_ENV, "").strip()
    if not raw:
        return DEFAULT_SLIPPAGE_ALLOWANCE_PCT
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return DEFAULT_SLIPPAGE_ALLOWANCE_PCT
    if not value.is_finite() or value < 0 or value > MAX_SLIPPAGE_ALLOWANCE_PCT:
        return DEFAULT_SLIPPAGE_ALLOWANCE_PCT
    return value


# ── storage ──


def _root(user_id):
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid account owner")
    return data_dir() / VENUE / str(user_id)


def ledger_path(user_id):
    return _root(user_id) / "ledger.json"


def _resets_path(user_id):
    return _root(user_id) / "resets.json"


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, content)


def new_ledger(now: float | None = None) -> dict:
    return {
        "paper": True,
        "quote": QUOTE,
        "start_cash": live.fmt(START_CASH),
        "cash": live.fmt(START_CASH),
        "positions": {},
        # Nothing is ever sent, so an order is never "uncertain"; the key keeps the
        # shape the live ledger and the Trade panel read.
        "uncertain_order": None,
        "enabled": True,
        "orders": [],
        "floor": str(live.FLOOR),
        "synced": True,
        "created": now if now is not None else time.time(),
    }


def read_ledger(user_id) -> dict | None:
    path = ledger_path(user_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def read_resets(user_id) -> list[dict]:
    path = _resets_path(user_id)
    try:
        rows = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _limits() -> dict:
    return {
        "floor": str(live.FLOOR),
        "reserve_fraction": "0.5",
        "position_fraction": "1/6",
        "max_positions": 3,
        "guards_apply_from": str(live.GUARD_THRESHOLD),
        "max_asset_fraction_below_threshold": str(live.MAX_ASSET_FRACTION),
        "default_slippage_pct": str(live.DEFAULT_SLIPPAGE_PCT),
        "min_order_usd": str(MIN_ORDER_USD),
        "pairs": "Kraken xStocks quoted in USD only",
        "session": "regular US session only (Mon-Fri 09:30-16:00 New York)",
    }


def _assumptions() -> dict:
    return {
        "taker_fee_pct": str(TAKER_FEE_PCT),
        "slippage_allowance_pct": str(slippage_allowance_pct()),
        "start_cash": live.fmt(START_CASH),
        "fill_model": "walks the live public order book inside the limit price",
    }


class XStocksPaperAccount:
    """The simulated ledger, orders and valuation. See the module docstring."""

    def __init__(self, market=None):
        #: The public-data reader (pairs, ticker, depth); tests pass a fake.
        self.market = market or xstocks_paper.service
        self.clock = time.time
        self.locks = defaultdict(asyncio.Lock)
        # The live service's sizing, guards and valuation, pointed at the public
        # reader. It never gets credentials and none of its private methods run.
        self._live = live.KrakenXStocksService()
        self._live.paper = self.market

    # ── state ──

    def _now(self) -> float:
        return float(self.clock())

    def _market_hours(self) -> dict:
        return xstocks_paper.us_market_open(self._now())

    def status(self, user_id) -> dict:
        ledger = read_ledger(user_id)
        shown = ledger if ledger is not None else new_ledger(self._now())
        return {
            "connected": ledger is not None,
            "paper": True,
            "simulated": True,
            "quote": QUOTE,
            "enabled": bool(ledger and ledger["enabled"]),
            "ledger": shown,
            "market": self._market_hours(),
            "limits": _limits(),
            "assumptions": _assumptions(),
            "banner": NOTE,
            "opened": ledger is not None,
        }

    def _open(self, user_id) -> dict:
        """The ledger, created with the starting cash on first use."""
        ledger = read_ledger(user_id)
        if ledger is None:
            ledger = new_ledger(self._now())
            _write(ledger_path(user_id), ledger)
        return ledger

    def _sample(self, user_id, equity) -> None:
        """Record the equity for the loop card and the chart (the recorder throttles)."""
        try:
            from condor import portfolio_history

            portfolio_history.record(user_id, LABEL, float(equity), now=self._now())
        except Exception as error:  # noqa: BLE001 - history never blocks the account
            log.debug("Paper equity sample skipped (%s)", type(error).__name__)

    def _stop(self, user_id, ledger) -> None:
        ledger["enabled"] = False
        _write(ledger_path(user_id), ledger)

    # ── valuation ──

    async def equity(self, user_id, ledger) -> Decimal:
        """Cash plus every position at the best bid (the live module's own reading)."""
        return await self._live.equity(user_id, ledger)

    async def valuation(self, user_id) -> dict:
        async with self.locks[user_id]:
            ledger = self._open(user_id)
            equity = await self.equity(user_id, ledger)
            # Every valuation is a sample, so the loop card has a start for the session
            # at once instead of waiting for the five-minute recorder.
            self._sample(user_id, equity)
            if equity <= live.FLOOR and ledger["enabled"]:
                self._stop(user_id, ledger)
            guards = live.guards_active(equity)
            return {
                "equity": live.fmt(equity),
                "quote": QUOTE,
                "new_position_limit": live.fmt(
                    equity / 6 if guards else equity * live.MAX_ASSET_FRACTION
                ),
                "minimum_cash_reserve": live.fmt(equity / 2 if guards else Decimal(0)),
                "valued_at": self._now(),
                **self.status(user_id),
            }

    async def portfolio(self, user_id) -> dict:
        """The dashboard view: USD cash and each position at the best bid, flagged paper."""
        ledger = read_ledger(user_id)
        if ledger is None:
            raise ValueError("The paper account has not been opened yet")
        markets = await self._live.markets()
        pairs = {}
        for symbol in ledger["positions"]:
            if symbol not in markets:
                raise ValueError(f"No market price for {symbol}")
            pairs[symbol] = markets[symbol]["pair"]
        tickers = await self._live._tickers(pairs.values()) if pairs else {}
        cash = Decimal(ledger["cash"])
        balances = [
            {
                "token": QUOTE,
                "total": float(cash),
                "available": float(cash),
                "usd_value": float(cash),
            }
        ]
        total = cash
        for symbol, position in ledger["positions"].items():
            bid = self._live._touch(tickers.get(pairs[symbol]) or {})[0]
            if not bid:
                raise ValueError(f"No market price for {symbol}")
            units = Decimal(position["units"])
            total += bid * units
            balances.append(
                {
                    "token": symbol + "x",
                    "total": float(units),
                    "available": float(units),
                    "usd_value": float(bid * units),
                }
            )
        return {
            "connector": LABEL,
            "paper": True,
            "balances": balances,
            "total_usd": float(total),
            "note": NOTE + " Positions are valued at the best bid.",
        }

    async def accounts(self, user_id) -> dict:
        """The simulated balances in Kraken's shape (USD, then ``<TICKER>x``)."""
        ledger = read_ledger(user_id)
        if ledger is None:
            return {"accounts": []}
        rows = []
        cash = Decimal(ledger["cash"])
        if cash > 0:
            rows.append(
                {
                    "currency": QUOTE,
                    "balance": live.fmt(cash),
                    "available": live.fmt(cash),
                }
            )
        for symbol, position in sorted(ledger["positions"].items()):
            rows.append(
                {
                    "currency": symbol + "x",
                    "balance": position["units"],
                    "available": position["units"],
                }
            )
        return {"accounts": rows}

    async def history(self, user_id) -> list[dict]:
        """The simulated orders, newest first, in the shape the live history uses."""
        ledger = read_ledger(user_id) or {"orders": []}
        rows = []
        for order in reversed(ledger["orders"][-50:]):
            row = {
                "order_id": order.get("order_id"),
                "client_oid": order.get("client_oid"),
                "pair": order.get("pair"),
                "side": order.get("side"),
                "status": order.get("status"),
                "volume": order.get("requested_quantity"),
                "vol_exec": order.get("quantity"),
                "limit_price": order.get("limit_price"),
                "avg_price": order.get("avg_price"),
                "cost": order.get("value"),
                "fee": order.get("fee"),
                "fee_currency": order.get("fee_currency", QUOTE),
                "closetm": order.get("time"),
                "simulated": True,
            }
            if order.get("realized_pnl") is not None:
                row["realized_pnl"] = order["realized_pnl"]
            rows.append({k: v for k, v in row.items() if v is not None})
        return rows

    # ── fills ──

    def _walk(self, levels, plan, steps, allowance_pct) -> list[tuple]:
        """``[(fill price, units, book price)]`` taken from the book, best level first.

        A level is used while its price, after the slippage allowance, is inside the
        order's limit price; sizes are cut to the pair's lot step. Anything the book
        cannot cover inside the limit stays unfilled (an immediate-or-cancel order).
        """
        buy = plan["side"] == "buy"
        factor = allowance_pct / 100
        limit = plan["limit_price"]
        rows = []
        for level in levels:
            try:
                price, size = live._decimal(level[0]), live._decimal(level[1])
            except (IndexError, TypeError, KeyError):
                continue
            if price and size and price > 0 and size > 0:
                rows.append((price, size))
        rows.sort(key=lambda r: r[0], reverse=not buy)
        remaining = plan["quantity"]
        fills = []
        for price, size in rows:
            effective = _money(price * (1 + factor if buy else 1 - factor))
            if (buy and effective > limit) or (not buy and effective < limit):
                break
            take = min(remaining, live._round_down(size, steps["qty_step"]))
            if take <= 0:
                continue
            fills.append((effective, take, price))
            remaining -= take
            if remaining <= 0:
                break
        return fills

    async def _book_levels(self, pair, side) -> list:
        result = await self.market._get("Depth", {"pair": pair, "count": BOOK_DEPTH})
        data = next(iter(result.values()), {}) if isinstance(result, dict) else {}
        return data.get("asks" if side == "buy" else "bids", [])

    @staticmethod
    def _gate(ledger, now) -> None:
        """Refuse a paused account or a closed session, in the live module's words."""
        if not ledger["enabled"]:
            raise ValueError(
                "Trading is paused: the paper account reached its equity floor. "
                "The owner can reset it."
            )
        market = xstocks_paper.us_market_open(now)
        if not market["open"]:
            raise ValueError(
                "Kraken xStocks orders are refused outside the regular US session "
                f"(Mon-Fri 09:30-16:00 New York); it is {market['new_york_time']} "
                "in New York"
            )

    def _guard(self, ledger, plan, equity, now) -> None:
        """Every refusal the live module's order would raise, in the same words."""
        self._gate(ledger, now)
        notional = plan["quantity"] * plan["limit_price"]
        if plan["side"] == "buy":
            if equity <= live.FLOOR:
                raise ValueError(
                    f"Bot equity reached the {live.FLOOR} stop threshold; trading paused"
                )
            if notional * (1 + live.FEE_HEADROOM) > Decimal(ledger["cash"]):
                raise ValueError("Not enough bot cash for this purchase and its fee")
            live.check_purchase(ledger, plan["symbol"], notional, equity)
        else:
            position = ledger["positions"].get(plan["symbol"])
            if not position or plan["quantity"] > Decimal(position["units"]):
                raise ValueError("Cannot sell assets outside the bot allocation")
        if notional < MIN_ORDER_USD:
            raise ValueError(
                f"Order value {live.fmt(notional)} USD is below the {MIN_ORDER_USD} "
                "USD minimum order"
            )

    @staticmethod
    def _summary(order) -> dict:
        keys = (
            "order_id",
            "client_oid",
            "pair",
            "side",
            "symbol",
            "status",
            "requested_quantity",
            "quantity",
            "limit_price",
            "avg_price",
            "value",
            "fee",
            "fee_currency",
            "slippage_allowance_pct",
            "levels",
            "partial",
            "realized_pnl",
            "time",
        )
        return {
            **{k: order[k] for k in keys if order.get(k) is not None},
            "simulated": True,
        }

    async def preview(self, user_id, side, symbol, value, slippage_pct=None) -> dict:
        """Size an order and walk the live book for it; places nothing and writes nothing."""
        plan = await self._live._plan(user_id, side, symbol, value, slippage_pct)
        levels = await self._book_levels(plan["pair"], plan["side"])
        steps = self._live._steps((await self._live._market_for(symbol))["row"])
        allowance = slippage_allowance_pct()
        fills = self._walk(levels, plan, steps, allowance)
        filled = sum((f[1] for f in fills), Decimal(0))
        cost = _money(sum((f[0] * f[1] for f in fills), Decimal(0)))
        average = cost / filled if filled > 0 else None
        reference = plan["ask"] if plan["side"] == "buy" else plan["bid"]
        impact = (
            abs(average - reference) / reference * 100 if average is not None else None
        )
        # Spread plus a taker fee and the slippage allowance on each of the two legs.
        round_trip = plan["spread_pct"] + 2 * (TAKER_FEE_PCT + allowance)
        market = self._market_hours()
        blocked = None
        ledger = read_ledger(user_id)
        if ledger is not None:
            try:
                equity = await self.equity(user_id, ledger)
                self._guard(ledger, plan, equity, self._now())
            except ValueError as error:
                blocked = str(error)
        return {
            "simulated": True,
            "pair": plan["pair"],
            "side": plan["side"],
            "taker_fee_pct": live.fmt(TAKER_FEE_PCT),
            "slippage_allowance_pct": live.fmt(allowance),
            "estimated_round_trip_pct": str(round(round_trip, 3)),
            "fee_note": "Simulated fee: Kraken's xStocks taker fee as assumed for the "
            "paper account, plus the slippage allowance, on each leg.",
            "quantity": live.fmt(plan["quantity"]),
            "limit_price": live.fmt(plan["limit_price"]),
            "notional_usd": live.fmt(plan["quantity"] * plan["limit_price"]),
            "bid": live.fmt(plan["bid"]),
            "ask": live.fmt(plan["ask"]),
            "spread_pct": str(round(plan["spread_pct"], 3)),
            "estimated_filled_quantity": live.fmt(filled),
            "estimated_average_price": (
                live.fmt(average) if average is not None else None
            ),
            "estimated_impact_pct": (
                str(round(impact, 3)) if impact is not None else None
            ),
            "estimated_fee_usd": live.fmt(_money(cost * TAKER_FEE_PCT / 100)),
            "book_covers_order": filled >= plan["quantity"],
            "market_open": market["open"],
            "minutes_to_close": market["minutes_to_close"],
            "blocked_reason": blocked,
        }

    async def order(
        self, user_id, side, symbol, value, slippage_pct=None, client_id=None
    ) -> dict:
        """Fill a simulated immediate-or-cancel order from the live book, once.

        ``client_id`` makes the call idempotent: the same id again returns the
        recorded order and changes nothing.
        """
        if client_id is not None and client_id != "":
            if not isinstance(client_id, str) or not _CLIENT_ID.fullmatch(client_id):
                raise ValueError("client_id must be 1-64 letters, digits, . _ or -")
        else:
            client_id = None
        async with self.locks[user_id]:
            ledger = self._open(user_id)
            if client_id:
                for old in ledger["orders"]:
                    if old.get("client_oid") == client_id:
                        return {
                            "order": self._summary(old),
                            "duplicate": True,
                            "simulated": True,
                            "status": self.status(user_id),
                        }
            now = self._now()
            self._gate(ledger, now)
            plan = await self._live._plan(user_id, side, symbol, value, slippage_pct)
            symbol = plan["symbol"]
            equity = await self.equity(user_id, ledger)
            if plan["side"] == "buy" and equity <= live.FLOOR:
                self._stop(user_id, ledger)
                raise ValueError(
                    f"Bot equity reached the {live.FLOOR} stop threshold; trading paused"
                )
            self._guard(ledger, plan, equity, now)
            if len([o for o in ledger["orders"] if now - o["time"] < 60]) >= (
                MAX_ORDERS_PER_MINUTE
            ):
                raise ValueError("Maximum ten orders per minute")
            steps = self._live._steps((await self._live._market_for(symbol))["row"])
            allowance = slippage_allowance_pct()
            fills = self._walk(
                await self._book_levels(plan["pair"], plan["side"]),
                plan,
                steps,
                allowance,
            )
            record = self._apply(ledger, plan, fills, allowance, client_id, now)
            _write(ledger_path(user_id), ledger)
            return {
                "order": self._summary(record),
                "simulated": True,
                "status": self.status(user_id),
            }

    def _apply(self, ledger, plan, fills, allowance, client_id, now) -> dict:
        """Book the fill: cash, position and an order record with its costs."""
        side, symbol = plan["side"], plan["symbol"]
        quantity = sum((f[1] for f in fills), Decimal(0))
        value = _money(sum((f[0] * f[1] for f in fills), Decimal(0)))
        fee = _money(value * TAKER_FEE_PCT / 100)
        realized = None
        if quantity > 0:
            if side == "buy":
                spent = value + fee
                previous = ledger["positions"].get(symbol, {"units": "0", "cost": "0"})
                ledger["positions"][symbol] = {
                    "units": live.fmt(Decimal(previous["units"]) + quantity),
                    "cost": live.fmt(Decimal(previous["cost"]) + spent),
                }
                ledger["cash"] = live.fmt(Decimal(ledger["cash"]) - spent)
            else:
                position = ledger["positions"][symbol]
                proceeds = value - fee
                held = Decimal(position["units"])
                fraction = quantity / held
                cost = Decimal(position["cost"]) * fraction
                if fraction == 1:
                    del ledger["positions"][symbol]
                else:
                    position["units"] = live.fmt(held - quantity)
                    position["cost"] = live.fmt(Decimal(position["cost"]) - cost)
                ledger["cash"] = live.fmt(Decimal(ledger["cash"]) + proceeds)
                realized = live.fmt(proceeds - cost)
        full = quantity >= plan["quantity"]
        record = {
            "time": now,
            "side": side,
            "symbol": symbol,
            "pair": plan["pair"],
            "order_id": "paper-" + uuid.uuid4().hex[:12],
            "client_oid": client_id or str(uuid.uuid4()),
            # An IOC order that could not be filled in full ends "canceled", as on Kraken.
            "status": "closed" if full else "canceled",
            "requested_quantity": live.fmt(plan["quantity"]),
            "quantity": live.fmt(quantity),
            "limit_price": live.fmt(plan["limit_price"]),
            "avg_price": live.fmt(value / quantity) if quantity > 0 else None,
            "value": live.fmt(value),
            "fee": live.fmt(fee),
            "fee_currency": QUOTE,
            "slippage_allowance_pct": live.fmt(allowance),
            "levels": len(fills),
            "partial": 0 < quantity < plan["quantity"],
            "realized_pnl": realized,
        }
        ledger["orders"] = (ledger["orders"] + [record])[-MAX_ORDERS:]
        return record

    # ── reset ──

    async def reset(self, user_id) -> dict:
        """Archive the ledger and start again from the starting cash (owner action).

        The reset is logged with the equity before and after, so the PnL treats the
        jump as a new baseline and not as a loss or a gain.
        """
        async with self.locks[user_id]:
            old = read_ledger(user_id)
            now = self._now()
            entry = {
                "ts": now,
                "equity_before": None,
                "equity_after": float(START_CASH),
                "estimated": False,
                "archive": None,
            }
            if old is not None:
                try:
                    before = await self.equity(user_id, old)
                except Exception:  # noqa: BLE001 - a reset must not need the market
                    before = Decimal(old["cash"]) + sum(
                        (Decimal(p["cost"]) for p in old["positions"].values()),
                        Decimal(0),
                    )
                    entry["estimated"] = True
                entry["equity_before"] = float(before)
                name = f"ledger-{int(now)}-{uuid.uuid4().hex[:6]}.json"
                entry["archive"] = name
                _write(
                    _root(user_id) / "archive" / name,
                    {
                        "archived_at": now,
                        "equity_at_reset": float(before),
                        "estimated": entry["estimated"],
                        "ledger": old,
                    },
                )
            _write(ledger_path(user_id), new_ledger(now))
            _write(
                _resets_path(user_id), (read_resets(user_id) + [entry])[-MAX_RESETS:]
            )
        # The fresh baseline, so the chart and the next session start at the starting
        # cash (the recorder drops it if one was just taken).
        self._sample(user_id, START_CASH)
        return {**self.status(user_id), "reset": entry}

    def adjustments(self, user_id, start: float, end: float) -> float:
        """Equity the resets in ``(start, end]`` moved without any trading (USD).

        The sum of ``equity_after - equity_before`` of each reset; the PnL maths
        subtracts it, so a reset is a new baseline and never a gain or a loss. A
        first opening (no old ledger) moves nothing.
        """
        net = 0.0
        for row in read_resets(user_id):
            before, after, stamp = (
                row.get("equity_before"),
                row.get("equity_after"),
                row.get("ts"),
            )
            if (
                isinstance(before, (int, float))
                and isinstance(after, (int, float))
                and isinstance(stamp, (int, float))
                and start < stamp <= end
            ):
                net += float(after) - float(before)
        return net


service = XStocksPaperAccount()
