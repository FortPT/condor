"""Paper scorecard for the Crypto.com App trader.

It answers one question before real money rides on it: do the trader's calls
beat the cost of trading? Two things are stored, and nothing else:

* price snapshots of App coins (taken whenever the coin list is read), and
* predictions the trader logs (coin, target %, stop %, time window).

Outcomes are never stored. They are recomputed from the snapshots on every
report, so changing the cost assumption re-scores the whole history. Nothing
here can move money.
"""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from condor.paths import data_dir

# Measured on the live App on 2026-10-27: buys fill within 0.3% of the listed
# price, sales come out 3.6-7.1% below it, the 0.8% trading fee is waived.
ROUND_TRIP_COST_PCT = 5.0
# A coin's price is stored at most this often, so a 5-minute loop plus an open
# dashboard do not multiply rows.
SNAPSHOT_MIN_GAP_SEC = 240
ENTRY_MAX_AGE_SEC = 900
PRICE_RETENTION_SEC = 90 * 86400
MULTIPLES = (1.0, 1.5, 2.0, 3.0)
MIN_RESOLVED_FOR_VERDICT = 30
# Not a bet on the market (stablecoins, wrapped and tokenized assets): no baseline, no tracking.
# Stablecoins, wrapped and tokenized assets do not trade like market coins.
NOT_MARKET = {
    "USDT",
    "USDC",
    "USDS",
    "DAI",
    "WBTC",
    "USD1",
    "USDG",
    "PYUSD",
    "BUIDL",
    "XAUT",
}
VERDICTS = {"trade", "pass"}
SYMBOL = re.compile(r"[A-Z0-9]{1,24}")


def _db(user_id: int) -> Path:
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid account owner")
    return data_dir() / "crypto_com_app" / str(user_id) / "scorecard.db"


def _connect(user_id: int) -> sqlite3.Connection:
    path = _db(user_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prices (
            ts INTEGER NOT NULL, symbol TEXT NOT NULL, eur REAL NOT NULL,
            PRIMARY KEY (ts, symbol));
        CREATE INDEX IF NOT EXISTS prices_symbol_ts ON prices (symbol, ts);
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
            symbol TEXT NOT NULL, verdict TEXT NOT NULL, entry REAL NOT NULL,
            target_pct REAL NOT NULL, stop_pct REAL NOT NULL,
            horizon_h REAL NOT NULL, note TEXT NOT NULL DEFAULT '');
        """)
    return conn


def _number(value, name, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def record_snapshot(user_id: int, coins, now: float | None = None) -> int:
    """Store the listed EUR price of each coin; returns how many rows were added."""
    now = int(now if now is not None else time.time())
    rows = []
    for coin in coins or []:
        symbol = coin.get("symbol") if isinstance(coin, dict) else None
        price = (coin.get("price_native") or {}).get("amount") if symbol else None
        try:
            eur = float(price)
        except (TypeError, ValueError):
            continue
        if symbol and SYMBOL.fullmatch(symbol) and eur > 0:
            rows.append((symbol, eur))
    if not rows:
        return 0
    with closing(_connect(user_id)) as conn:
        recent = dict(
            conn.execute(
                "SELECT symbol, MAX(ts) FROM prices WHERE ts > ? GROUP BY symbol",
                (now - SNAPSHOT_MIN_GAP_SEC,),
            ).fetchall()
        )
        fresh = [(now, s, p) for s, p in rows if s not in recent]
        conn.executemany("INSERT OR IGNORE INTO prices VALUES (?, ?, ?)", fresh)
        conn.execute("DELETE FROM prices WHERE ts < ?", (now - PRICE_RETENTION_SEC,))
        conn.commit()
    return len(fresh)


def latest_price(user_id: int, symbol: str, now: float | None = None):
    now = int(now if now is not None else time.time())
    with closing(_connect(user_id)) as conn:
        row = conn.execute(
            "SELECT ts, eur FROM prices WHERE symbol = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (symbol, now),
        ).fetchone()
    return row if row and now - row[0] <= ENTRY_MAX_AGE_SEC else None


def tracked_symbols(user_id: int, now: float | None = None) -> list[str]:
    """Coins with a still-open prediction, so their prices keep being recorded."""
    now = int(now if now is not None else time.time())
    with closing(_connect(user_id)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM predictions WHERE ts + horizon_h * 3600 > ?",
            (now,),
        ).fetchall()
    return [row[0] for row in rows]


def record_prediction(
    user_id, symbol, verdict, target_pct, stop_pct, horizon_hours, note="", now=None
):
    """Log one call. Needs a recent listed price; one open call per coin."""
    now = int(now if now is not None else time.time())
    symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not SYMBOL.fullmatch(symbol) or symbol in NOT_MARKET:
        raise ValueError("Use a tradable coin symbol such as SOL")
    if verdict not in VERDICTS:
        raise ValueError("verdict must be trade or pass")
    target = _number(target_pct, "target_pct", 0.1, 200)
    stop = _number(stop_pct, "stop_pct", 0.1, 100)
    horizon = _number(horizon_hours, "horizon_hours", 1, 168)
    note = " ".join(str(note or "").split())[:200]
    price = latest_price(user_id, symbol, now)
    if price is None:
        raise ValueError(f"No recent listed price for {symbol}; list the coin first")
    with closing(_connect(user_id)) as conn:
        open_call = conn.execute(
            "SELECT id FROM predictions WHERE symbol = ? AND ts + horizon_h * 3600 > ?",
            (symbol, now),
        ).fetchone()
        if open_call:
            return {
                "recorded": False,
                "reason": "open prediction exists",
                "id": open_call[0],
            }
        cursor = conn.execute(
            "INSERT INTO predictions (ts, symbol, verdict, entry, target_pct, stop_pct, horizon_h, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, symbol, verdict, price[1], target, stop, horizon, note),
        )
        conn.commit()
    return {"recorded": True, "id": cursor.lastrowid, "entry_eur": price[1]}


def evaluate(prediction: dict, series, now: float, cost_pct: float) -> dict:
    """Score one call from the prices after it. Pure: no clock, no storage.

    ``series`` is ``[(ts, eur), ...]`` ascending. A target is hit when the price
    reaches ``entry * (1 + target%)`` before it falls to ``entry * (1 - stop%)``;
    the first snapshot to cross a line decides, and a call that does neither by
    the end of its window is scored at the last price inside the window.
    """
    start = prediction["ts"]
    end = start + prediction["horizon_h"] * 3600
    entry = prediction["entry"]
    inside = [(ts, price) for ts, price in series if start < ts <= end]
    outcome, gross = None, None
    for _, price in inside:
        move = (price / entry - 1) * 100
        if move >= prediction["target_pct"]:
            outcome, gross = "target", prediction["target_pct"]
            break
        if move <= -prediction["stop_pct"]:
            outcome, gross = "stopped", -prediction["stop_pct"]
            break
    if outcome is None:
        if now < end:
            return {"status": "open", "outcome": "open"}
        if not inside:
            return {"status": "nodata", "outcome": "nodata"}
        outcome, gross = "expired", (inside[-1][1] / entry - 1) * 100
    return {
        "status": "resolved",
        "outcome": outcome,
        "gross_pct": gross,
        "net_pct": gross - cost_pct,
    }


def _baseline(conn, prediction, now):
    """Average gross move of every listed coin over the same window: the 'did
    anything at all go up' yardstick the call has to beat."""
    start, end = prediction["ts"], prediction["ts"] + prediction["horizon_h"] * 3600
    if now < end:
        return None
    moves = []
    for (symbol,) in conn.execute(
        "SELECT DISTINCT symbol FROM prices WHERE ts BETWEEN ? AND ?",
        (start - 600, start + 600),
    ).fetchall():
        if symbol in NOT_MARKET:
            continue
        first = conn.execute(
            "SELECT eur FROM prices WHERE symbol = ? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 1",
            (symbol, start - 600, start + 600),
        ).fetchone()
        last = conn.execute(
            "SELECT eur FROM prices WHERE symbol = ? AND ts <= ? AND ts > ? ORDER BY ts DESC LIMIT 1",
            (symbol, end, start),
        ).fetchone()
        if first and last:
            moves.append((last[0] / first[0] - 1) * 100)
    return sum(moves) / len(moves) if moves else None


def _mean(values):
    return sum(values) / len(values) if values else None


def _summary(rows):
    resolved = [r for r in rows if r["status"] == "resolved"]
    nets = [r["net_pct"] for r in resolved]
    return {
        "total": len(rows),
        "resolved": len(resolved),
        "open": sum(r["status"] == "open" for r in rows),
        "hit_target": sum(r["outcome"] == "target" for r in resolved),
        "stopped": sum(r["outcome"] == "stopped" for r in resolved),
        "expired": sum(r["outcome"] == "expired" for r in resolved),
        "avg_gross_pct": _mean([r["gross_pct"] for r in resolved]),
        "avg_net_pct": _mean(nets),
        "beat_costs": sum(n > 0 for n in nets),
    }


def report(
    user_id: int, now: float | None = None, cost_pct: float = ROUND_TRIP_COST_PCT
) -> dict:
    now = int(now if now is not None else time.time())
    with closing(_connect(user_id)) as conn:
        conn.row_factory = sqlite3.Row
        predictions = [
            dict(row) for row in conn.execute("SELECT * FROM predictions ORDER BY ts")
        ]
        scored = []
        for prediction in predictions:
            series = conn.execute(
                "SELECT ts, eur FROM prices WHERE symbol = ? AND ts > ? AND ts <= ? ORDER BY ts",
                (prediction["symbol"], prediction["ts"], now),
            ).fetchall()
            result = evaluate(prediction, [(r[0], r[1]) for r in series], now, cost_pct)
            if result["status"] == "resolved":
                result["baseline_pct"] = _baseline(conn, prediction, now)
            scored.append({**prediction, **result})
        stats = conn.execute(
            "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts), COUNT(DISTINCT symbol) FROM prices"
        ).fetchone()
    resolved = [r for r in scored if r["status"] == "resolved"]
    buckets = []
    for multiple in MULTIPLES:
        sel = [r for r in resolved if r["target_pct"] >= multiple * cost_pct]
        nets = [r["net_pct"] for r in sel]
        buckets.append(
            {
                "multiple": multiple,
                "min_target_pct": multiple * cost_pct,
                "resolved": len(sel),
                "avg_net_pct": _mean(nets),
                "win_rate": (sum(n > 0 for n in nets) / len(nets)) if nets else None,
            }
        )
    baselines = [
        r["baseline_pct"] for r in resolved if r.get("baseline_pct") is not None
    ]
    gross = _mean([r["gross_pct"] for r in resolved])
    base = _mean(baselines)
    days = (stats[2] - stats[1]) / 86400 if stats[1] is not None else 0
    return {
        "assumptions": {
            "round_trip_cost_pct": cost_pct,
            "basis": "buys ~listed price, sales 3.6-7.1% below it, trading fee waived (measured 2026-10-27)",
            "outcome_rule": "target or stop, whichever the price reaches first; otherwise the last price in the window",
        },
        "prices": {"snapshots": stats[0], "coins": stats[3], "days": round(days, 2)},
        "trade_calls": _summary([r for r in scored if r["verdict"] == "trade"]),
        "pass_calls": _summary([r for r in scored if r["verdict"] == "pass"]),
        "by_cost_multiple": buckets,
        "baseline_gross_pct": base,
        "edge_vs_baseline_pct": (
            (gross - base) if gross is not None and base is not None else None
        ),
        "enough_data": len(resolved) >= MIN_RESOLVED_FOR_VERDICT,
        "recent": [
            {
                k: r.get(k)
                for k in (
                    "ts",
                    "symbol",
                    "verdict",
                    "target_pct",
                    "stop_pct",
                    "horizon_h",
                    "outcome",
                    "net_pct",
                    "note",
                )
            }
            for r in scored[-10:][::-1]
        ],
    }
