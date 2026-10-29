"""The change in the portfolio's value over a history window, or an honest "unknown".

The Portfolio Evolution chart is the Hummingbot history plus the samples Condor
records itself for the Crypto.com Exchange, Kraken xStocks and Revolut X accounts.
Those two start at different times and deposits are not in either, so the first and
last point of the merged series are not a gain: an account that was connected three
hours ago reads as growth from nothing, and a deposit reads as profit.

The change is therefore only stated when it can be computed from sources that all
have a value at the start of the window and whose deposits and withdrawals are
known: current value minus value at the start minus the net confirmed flows
recorded in :mod:`condor.native_flows`. Anything else is reported as unavailable,
with the reason, and the page shows a dash instead of a number.

A simulated (paper) account is never part of it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Mapping, Sequence

from condor import native_flows, native_venues, portfolio_history

log = logging.getLogger(__name__)

HUMMINGBOT = "Hummingbot"
#: A source whose first sample is no later than this after the window start counts as
#: having a value at the start: two sample intervals, at least 15 minutes, and 2% of
#: the window (the Hummingbot history is cached, so its first point trails the start by
#: up to the cache age).
START_TOLERANCE_INTERVALS = 2
MIN_START_TOLERANCE_SEC = 900
START_TOLERANCE_FRACTION = 0.02
#: Below this a percentage is noise (or a division by nothing).
MIN_BASE_USD = 1.0

REASONS = {
    "no_history": "There is no value history in this window yet.",
    "different_start": (
        "History starts at different times: {source} has no value at the start of "
        "the window, so the gain cannot be told from the account being added."
    ),
    "flows_untracked": (
        "Deposits and withdrawals of {source} are not tracked, so a deposit would "
        "read as a gain."
    ),
    "flows_pending": (
        "{source} has deposits or withdrawals in this window that are not confirmed "
        "yet, so the change cannot be told from them."
    ),
    "flows_unpriced": (
        "A deposit or withdrawal of {source} in this window has no USD value."
    ),
    "flows_unreadable": "The deposits and withdrawals of {source} could not be read.",
    "error": "The change could not be computed.",
}


def unavailable(reason: str, source: str = "") -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "note": REASONS[reason].format(source=source or "a source"),
    }


def _venues_by_label() -> dict[str, native_venues.NativeVenue]:
    return {v.label: v for v in native_venues.account_venues()}


def compute(
    user_id: int,
    hummingbot_points: Sequence[Any],
    native: Mapping[str, Sequence[tuple[float, float]]],
    since: float,
    interval_seconds: int,
    skip: Iterable[str] = (),
    now: float | None = None,
) -> dict[str, Any]:
    """Change over ``[since, now]`` from the sources that can all be read at ``since``.

    ``hummingbot_points`` are the route's history points before the native sources
    are merged in (anything with ``timestamp`` and ``total_usd``); ``native`` is
    :func:`condor.portfolio_history.series`; ``skip`` names sources Hummingbot's
    history already counts. Returns a plain dict with ``available``; when true,
    ``change_usd`` is the trading result (net of flows) and ``change_pct`` is relative
    to the value at the start, or ``None`` when that base is too small, or smaller than
    the flows taken out of it, to give a meaningful percentage.
    """
    now = time.time() if now is None else now
    tolerance = max(
        START_TOLERANCE_INTERVALS * int(interval_seconds),
        MIN_START_TOLERANCE_SEC,
        START_TOLERANCE_FRACTION * (now - since),
    )
    deadline = since + tolerance
    excluded = {*skip, *portfolio_history.PAPER_SOURCES}
    start = end = flows = 0.0
    sources: list[str] = []
    not_netted: list[str] = []

    points = sorted(hummingbot_points, key=lambda p: p.timestamp)
    if points and (points[0].total_usd > 0 or points[-1].total_usd > 0):
        first, last = points[0], points[-1]
        if first.timestamp > deadline or first.total_usd <= 0:
            return unavailable("different_start", HUMMINGBOT)
        start += float(first.total_usd)
        end += float(last.total_usd)
        sources.append(HUMMINGBOT)
        # Hummingbot exposes no deposit history to net: the figure is the equity change.
        not_netted.append(HUMMINGBOT)

    venues = _venues_by_label()
    for name in sorted(native):
        rows = native[name]
        if name in excluded or not rows or name not in venues:
            continue
        (first_ts, first_total), (last_ts, last_total) = rows[0], rows[-1]
        if first_total <= 0 and last_total <= 0:
            continue
        if first_ts > deadline:
            return unavailable("different_start", name)
        key = venues[name].key
        try:
            info = native_flows.summary(user_id, key)
            if not info.get("tracked"):
                return unavailable("flows_untracked", name)
            if any(
                first_ts < float(row.get("ts") or 0) <= last_ts
                for row in native_flows.pending(user_id, key)
            ):
                return unavailable("flows_pending", name)
            window = native_flows.window_net(user_id, key, first_ts, last_ts)
        except Exception as error:  # noqa: BLE001 - never break the history read
            log.warning("%s flows unreadable (%s)", key, type(error).__name__)
            return unavailable("flows_unreadable", name)
        if window.unpriced or window.failed:
            return unavailable(
                "flows_unpriced" if window.unpriced else "flows_unreadable", name
            )
        start += float(first_total)
        end += float(last_total)
        flows += window.net_usd
        sources.append(name)

    if not sources:
        return unavailable("no_history")

    change = end - start - flows
    pct = (
        change / start * 100
        if start >= MIN_BASE_USD and abs(flows) <= start
        else None
    )
    out: dict[str, Any] = {
        "available": True,
        "reason": None,
        "note": None,
        "from_ts": float(since),
        "start_usd": round(start, 2),
        "end_usd": round(end, 2),
        "net_flows_usd": round(flows, 2),
        "change_usd": round(change, 2),
        "change_pct": None if pct is None else round(pct, 2),
        "sources": sources,
        "flows_not_netted": not_netted,
    }
    if not_netted:
        out["note"] = (
            "Deposits and withdrawals of Hummingbot-connected accounts are not "
            "recorded, so they are not taken out of this change."
        )
    return out
