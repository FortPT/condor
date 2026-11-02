"""Slow, rule-based crypto signals from DAILY candles, computed in code.

Why this exists: no intraday setup tried on 30 days of 15-minute candles or a year of
hourly ones beat a round trip of about 0.3% (Revolut X) to 1.2% (Crypto.com). Over nine
years of BTC and ETH daily candles (long only, signal at the close, trade at the next
open, after those same costs) two slow rules made 5 to 12 trades a year, cut the worst
drop (BTC from -83% to between -54% and -66%) and ended at or above simply holding:

  trend     long while the last closed day is above its 50-day average (Revolut X)
  breakout  long after a close above the prior 20-day high, flat after a close below the
            prior 10-day low (Crypto.com, where a trade costs more, so fewer is better)

Past results are not a promise, and a bear market still costs 40% or more. This module
only answers "long or flat?" so a crew follows a signal instead of judging a chart. It
reads closed UTC days only (today's candle is unfinished and is dropped), places no
order, needs no credentials and says ``no_data`` rather than guess.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Mapping

DAY_MS = 86_400_000
#: Closed daily candles needed before a signal means anything (the 50-day average
#: needs warm-up; the breakout needs 20 days).
MIN_BARS = 120
#: The newest closed candle may be at most this many days old.
MAX_AGE_DAYS = 3
#: A holding worth less than this (in the venue's quote currency) is dust, not a position.
DUST_VALUE = 2.0

NOTE = (
    "A signal comes from closed UTC daily candles and changes at most once a day. It "
    "does not know your cash, the 40% cap per coin or the costs: those are the native "
    "guards' job. It places no order."
)


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    kind: str  # "trend" or "breakout"
    coins: tuple[str, ...]
    span: int = 50
    entry_days: int = 20
    exit_days: int = 10


TREND_50 = Rule(
    "trend50", "long while above the 50-day average", "trend", ("BTC", "ETH")
)
SOL_TREND_50 = Rule(
    "trend50_sol", "long while above the 50-day average", "trend", ("SOL",)
)
BREAKOUT_20_10 = Rule(
    "breakout20_10",
    "long after a close above the prior 20-day high, flat after a close below the "
    "prior 10-day low",
    "breakout",
    ("BTC", "ETH"),
)


def closed_daily(candles: Any, now: float | None = None) -> list[tuple]:
    """``[(t_ms, open, high, low, close), ...]`` oldest first, finished days only."""
    now_ms = (time.time() if now is None else now) * 1000
    bars: dict[int, tuple] = {}
    for row in candles if isinstance(candles, list) else []:
        try:
            t = int(float(row["t"]))
            o, h, l, c = (float(row[k]) for k in ("o", "h", "l", "c"))
        except (KeyError, TypeError, ValueError):
            continue
        if t < 10**12:  # seconds, not milliseconds
            t *= 1000
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)) or h < l:
            continue
        if t + DAY_MS > now_ms:  # today's candle is still open
            continue
        bars[t] = (o, h, l, c)
    return [(t, *bars[t]) for t in sorted(bars)]


def _ema(values: list[float], span: int) -> list[float]:
    k, out, prev = 2 / (span + 1), [], values[0]
    for v in values:
        prev = prev + k * (v - prev)
        out.append(prev)
    return out


def trend_states(bars: list[tuple], span: int = 50) -> tuple[list[bool], list[float]]:
    closes = [b[4] for b in bars]
    average = _ema(closes, span)
    return [i >= span and closes[i] > average[i] for i in range(len(closes))], average


def breakout_states(bars: list[tuple], entry: int = 20, leave: int = 10) -> list[bool]:
    on, out = False, []
    for i, bar in enumerate(bars):
        if i >= entry:
            high = max(b[2] for b in bars[i - entry : i])
            low = min(b[3] for b in bars[i - leave : i])
            if not on and bar[4] > high:
                on = True
            elif on and bar[4] < low:
                on = False
        out.append(on)
    return out


def _days_in_state(states: list[bool]) -> int:
    n = 0
    for state in reversed(states):
        if state != states[-1]:
            break
        n += 1
    return n


def evaluate(rule: Rule, bars: list[tuple], now: float | None = None) -> dict:
    """The rule's answer for one coin's closed daily candles."""
    now = time.time() if now is None else now
    if len(bars) < MIN_BARS:
        return {
            "signal": "no_data",
            "reason": f"{len(bars)} closed daily candles, need {MIN_BARS}",
        }
    last_t, close = bars[-1][0], bars[-1][4]
    age_days = (now * 1000 - (last_t + DAY_MS)) / DAY_MS
    if age_days > MAX_AGE_DAYS:
        return {
            "signal": "no_data",
            "reason": f"newest closed candle is {age_days:.0f} days old",
        }
    out: dict = {
        "close": close,
        "last_closed_day": datetime.fromtimestamp(last_t / 1000, timezone.utc).strftime(
            "%Y-%m-%d"
        ),
        "candles": len(bars),
    }
    if rule.kind == "trend":
        states, average = trend_states(bars, rule.span)
        out.update(
            average=round(average[-1], 6),
            gap_pct=round((close / average[-1] - 1) * 100, 2),
        )
    else:
        states = breakout_states(bars, rule.entry_days, rule.exit_days)
        out.update(
            prior_high=max(b[2] for b in bars[-1 - rule.entry_days : -1]),
            prior_low=min(b[3] for b in bars[-1 - rule.exit_days : -1]),
        )
    out["signal"] = "long" if states[-1] else "flat"
    out["days_in_state"] = _days_in_state(states)
    return out


def _held(positions: Mapping, coin: str, close: float | None) -> dict:
    position = positions.get(coin) if isinstance(positions, Mapping) else None
    try:
        units = Decimal(str((position or {}).get("units") or 0))
    except InvalidOperation:
        units = Decimal(0)
    value = float(units) * close if close else None
    held = units > 0 and (value is None or value >= DUST_VALUE)
    return {
        "held": held,
        "units": format(units.normalize(), "f") if units > 0 else "0",
        "held_value": None if value is None else round(value, 2),
    }


def action_for(signal: str, held: bool) -> str:
    if signal == "long" and not held:
        return "buy"
    if signal == "flat" and held:
        return "sell"
    if signal in ("long", "flat"):
        return "hold"
    return "none"


async def report(
    rule: Rule,
    fetch: Callable[[str], Awaitable[Any]],
    positions: Mapping,
    now: float | None = None,
) -> dict:
    """One answer per coin: the signal, what the bot holds and the action that follows."""
    now = time.time() if now is None else now
    coins = []
    for coin in rule.coins:
        item: dict = {"coin": coin}
        try:
            candles = await fetch(coin)
        except (
            Exception
        ) as error:  # noqa: BLE001 - a failed read is no data, never a guess
            item.update(
                signal="no_data", reason=f"candles unavailable ({type(error).__name__})"
            )
        else:
            item.update(evaluate(rule, closed_daily(candles, now), now))
        item.update(_held(positions, coin, item.get("close")))
        item["action"] = action_for(item["signal"], item["held"])
        coins.append(item)
    return {
        "rule": rule.key,
        "rule_text": rule.label,
        "as_of": datetime.fromtimestamp(now, timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
        "coins": coins,
        "note": NOTE,
    }


#: SOL the wallet keeps for fees (the swap policy enforces the same figure) and the
#: smallest swap worth its fixed costs, in USD.
SOL_RESERVE = 0.02
MIN_SWAP_VALUE = 3.0

SOLANA_NOTE = (
    "The signal comes from closed UTC daily SOL-USDT candles. The plan is only what the rule "
    "asks for: swaps are capped per swap and per tick by the swap policy, so a sale or "
    "purchase of the whole amount takes several ticks. It places nothing."
)


def from_normalized(rows: Any) -> list[dict]:
    """Hummingbot candles (``timestamp``, ``open``...) as the t/o/h/l/c rows ``closed_daily`` reads."""
    return [
        {
            "t": r.get("timestamp"),
            "o": r.get("open"),
            "h": r.get("high"),
            "l": r.get("low"),
            "c": r.get("close"),
        }
        for r in (rows if isinstance(rows, list) else [])
        if isinstance(r, dict)
    ]


def solana_plan(
    signal: str,
    sol_units: float,
    usdc_value: float,
    price: float | None,
    reserve: float = SOL_RESERVE,
    min_value: float = MIN_SWAP_VALUE,
) -> dict:
    """What the daily SOL rule asks of a wallet holding ``sol_units`` SOL and ``usdc_value`` USDC.

    Flat: sell the SOL above the fee reserve into USDC. Long: buy SOL with the USDC.
    Anything under ``min_value`` dollars is not worth a swap, so the wallet is left alone.
    """
    tradable = max(sol_units - reserve, 0.0)
    tradable_value = tradable * price if price else 0.0
    plan = {
        "action": "none",
        "sell_sol_units": 0.0,
        "buy_usdc": 0.0,
        "tradable_sol": round(tradable, 6),
        "reserve_sol": reserve,
    }
    if signal not in ("long", "flat"):
        return plan
    plan["action"] = "hold"
    if signal == "flat" and tradable_value >= min_value:
        plan.update(action="sell_sol", sell_sol_units=round(tradable, 6))
    elif signal == "long" and usdc_value >= min_value:
        plan.update(action="buy_sol", buy_usdc=round(usdc_value, 2))
    return plan
