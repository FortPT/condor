"""Value history for the accounts Condor reads itself.

Hummingbot records its own portfolio history, but the Crypto.com Exchange, the Kraken
xStocks account and Revolut X are read by Condor's native modules, so the dashboard's
Portfolio Evolution chart used to leave them out. This keeps one small value
sample per account every few minutes (USD, whole account at the last traded
price) so the chart route can add them. Nothing here touches money.

History starts when recording starts: there is no way to reconstruct what an
account was worth before that.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from condor import native_venues
from condor.paths import data_dir

log = logging.getLogger(__name__)

EXCHANGE = native_venues.get("crypto_com_exchange").label
KRAKEN = native_venues.get("kraken_xstocks").label
REVOLUT = native_venues.get("revolut_x").label
SOURCES = tuple(v.label for v in native_venues.account_venues())
SAMPLE_EVERY_SEC = 300
# A second sample inside this gap (an open dashboard plus the timer) is dropped.
MIN_GAP_SEC = 240
RETENTION_SEC = 200 * 86400
INTERVAL_SECONDS = {"5m": 300, "1h": 3600, "4h": 14400, "1d": 86400}
_SOURCE = re.compile(r"[A-Za-z0-9 ._-]{1,40}")

_task: asyncio.Task | None = None


def _db(user_id: int) -> Path:
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid account owner")
    return data_dir() / "portfolio_history" / f"{user_id}.db"


def _connect(user_id: int) -> sqlite3.Connection:
    path = _db(user_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
            ts INTEGER NOT NULL, source TEXT NOT NULL, total_usd REAL NOT NULL,
            PRIMARY KEY (source, ts));
        """)
    return conn


def record(user_id: int, source: str, total_usd, now: float | None = None) -> bool:
    """Store one sample; False when it was skipped (bad value or too soon)."""
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise ValueError("Invalid history source")
    try:
        total = float(total_usd)
    except (TypeError, ValueError):
        return False
    if not total == total or total < 0:  # NaN or negative
        return False
    stamp = int(now if now is not None else time.time())
    with closing(_connect(user_id)) as conn:
        last = conn.execute(
            "SELECT MAX(ts) FROM samples WHERE source = ?", (source,)
        ).fetchone()[0]
        if last is not None and 0 <= stamp - last < MIN_GAP_SEC:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO samples VALUES (?, ?, ?)", (stamp, source, total)
        )
        conn.execute("DELETE FROM samples WHERE ts < ?", (stamp - RETENTION_SEC,))
        conn.commit()
    return True


def series(
    user_id: int, since: float, until: float | None = None
) -> dict[str, list[tuple[int, float]]]:
    """``{source: [(ts, total_usd), ...]}`` ascending, from ``since`` to ``until``."""
    end = int(until if until is not None else time.time())
    out: dict[str, list[tuple[int, float]]] = {}
    with closing(_connect(user_id)) as conn:
        rows = conn.execute(
            "SELECT source, ts, total_usd FROM samples WHERE ts BETWEEN ? AND ?"
            " ORDER BY ts",
            (int(since), end),
        ).fetchall()
    for source, ts, total in rows:
        out.setdefault(source, []).append((ts, total))
    return out


def nearest(
    user_id: int, source: str, stamps
) -> list[tuple[tuple[int, float] | None, tuple[int, float] | None]]:
    """For each timestamp, the closest sample at-or-before it and the first after it.

    One connection for the whole batch. Read-only: a user with no history gets
    ``(None, None)`` pairs and no database file is created for them.
    """
    stamps = [int(s) for s in stamps]
    if not _db(user_id).exists():
        return [(None, None) for _ in stamps]
    out = []
    with closing(_connect(user_id)) as conn:
        for stamp in stamps:
            before = conn.execute(
                "SELECT ts, total_usd FROM samples WHERE source = ? AND ts <= ?"
                " ORDER BY ts DESC LIMIT 1",
                (source, stamp),
            ).fetchone()
            after = conn.execute(
                "SELECT ts, total_usd FROM samples WHERE source = ? AND ts > ?"
                " ORDER BY ts ASC LIMIT 1",
                (source, stamp),
            ).fetchone()
            out.append((before, after))
    return out


def bounds(
    user_id: int, source: str
) -> tuple[tuple[int, float] | None, tuple[int, float] | None]:
    """``(first sample, latest sample)`` of one source; ``(None, None)`` without history."""
    if not _db(user_id).exists():
        return None, None
    with closing(_connect(user_id)) as conn:
        first = conn.execute(
            "SELECT ts, total_usd FROM samples WHERE source = ? ORDER BY ts ASC LIMIT 1",
            (source,),
        ).fetchone()
        last = conn.execute(
            "SELECT ts, total_usd FROM samples WHERE source = ? ORDER BY ts DESC LIMIT 1",
            (source,),
        ).fetchone()
    return first, last


def merge(points, native, interval_seconds: int, skip=(), point_type=None):
    """Add the native accounts to the Hummingbot history points.

    ``points`` are the route's ``PortfolioHistoryPoint`` objects. Each native
    source is thinned to one sample per interval, then the two timelines are
    joined and every value is carried forward until the next sample, so an
    account never drops to zero between two readings. A source before its first
    sample counts as zero. ``skip`` names sources the Hummingbot history already
    counts (the same account must not be added twice).
    """
    sources = {name: rows for name, rows in native.items() if rows and name not in skip}
    if not sources:
        return list(points)
    step = max(int(interval_seconds), 1)
    thinned: dict[str, list[tuple[float, float]]] = {}
    for name, rows in sources.items():
        buckets: dict[int, tuple[float, float]] = {}
        for ts, total in rows:
            buckets[int(ts) // step] = (float(ts), float(total))
        thinned[name] = sorted(buckets.values())
    base = sorted(points, key=lambda p: p.timestamp)
    stamps = sorted(
        {p.timestamp for p in base}
        | {ts for rows in thinned.values() for ts, _ in rows}
    )
    if point_type is None:
        from condor.web.models import PortfolioHistoryPoint as point_type

    merged = []
    bi = 0
    cursors = {name: 0 for name in thinned}
    last_base = None
    last_native = {name: 0.0 for name in thinned}
    for ts in stamps:
        while bi < len(base) and base[bi].timestamp <= ts:
            last_base = base[bi]
            bi += 1
        for name, rows in thinned.items():
            i = cursors[name]
            while i < len(rows) and rows[i][0] <= ts:
                last_native[name] = rows[i][1]
                i += 1
            cursors[name] = i
        total = (last_base.total_usd if last_base else 0.0) + sum(last_native.values())
        merged.append(
            point_type(
                timestamp=ts,
                total_usd=total,
                tokens=dict(last_base.tokens) if last_base else {},
            )
        )
    return merged


async def sample_user(user_id: int, exchange=None, kraken=None, **services) -> None:
    """Take one sample of each connected native account for the user.

    ``exchange``/``kraken`` (and any other venue key) override that venue's
    service, which tests use to run without credentials.
    """
    overrides = {"crypto_com_exchange": exchange, "kraken_xstocks": kraken, **services}
    for venue in native_venues.account_venues():
        try:
            service = overrides.get(venue.key) or venue.service
            if not service.status(user_id)["connected"]:
                continue
            data = await service.portfolio(user_id)
            record(user_id, venue.label, data["total_usd"])
        except Exception as error:  # noqa: BLE001 - history must never break a read
            log.debug(
                "Portfolio sample for %s skipped (%s)",
                venue.label,
                type(error).__name__,
            )


def _users() -> list[int]:
    found: set[int] = set()
    for venue in native_venues.account_venues():
        root = data_dir() / venue.key
        if root.is_dir():
            found.update(int(p.name) for p in root.iterdir() if p.name.isdigit())
    return sorted(found)


async def run_recorder() -> None:
    while True:
        for user_id in _users():
            await sample_user(user_id)
        await asyncio.sleep(SAMPLE_EVERY_SEC)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(run_recorder())


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None
