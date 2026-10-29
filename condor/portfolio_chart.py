"""The Portfolio Evolution chart's honest series: one baseline, visible gaps, flows.

The merged ``points`` series (Hummingbot history plus the accounts Condor records
itself) steps up whenever an account starts being recorded and whenever money is
deposited, which reads as performance but is only accounts being added. This module
works out a second view of the same readings, additive to the route's response:

* **Baseline.** ``baseline_ts`` is the first moment every included source has a
  recorded reading. The chart series starts at ``max(window start, baseline_ts)`` so
  the line never contains the "accounts being added" staircase. A source that is
  connected but has no reading yet is reported as ``pending``, never waited for.
* **Gaps.** A source with no reading within two sampling intervals of a point is
  *missing* there. The point's total is ``None`` (a break in the line) and the gap is
  reported. Nothing is carried forward to cover it.
* **Flows.** Confirmed deposits and withdrawals of the included native venues inside
  the window, as markers, plus a count of the unconfirmed ones.
* **Trading result.** ``total(t) - total(start)`` minus the net confirmed flows since the
  start, under the same rules as :mod:`condor.portfolio_change`; when the flows cannot
  be netted honestly the series is ``None`` with the reason, never a guess.

Everything here is a read of values already recorded: nothing is estimated, converted
or filled, and nothing touches an exchange.
"""

from __future__ import annotations

import bisect
import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from condor import native_flows, native_venues, portfolio_change, portfolio_history

log = logging.getLogger(__name__)

HUMMINGBOT = portfolio_change.HUMMINGBOT
#: A reading counts as "around" a point when it is at most this many sampling
#: intervals away from it.
TOLERANCE_INTERVALS = 2
#: Hummingbot's history is served at the range's interval, never finer than this.
MIN_CADENCE_SEC = portfolio_history.SAMPLE_EVERY_SEC
#: How far before the window the native readings are fetched, so a point at the
#: window start can see the reading just before it.
EDGE_MARGIN_SEC = TOLERANCE_INTERVALS * portfolio_history.SAMPLE_EVERY_SEC

#: A single Hummingbot reading that differs from both neighbours by more than
#: ``max(OUTLIER_REL * neighbour, OUTLIER_MIN_USD)`` while the neighbours agree with each
#: other within ``AGREE_REL`` is a bad row, not a move: it is left out and reported.
OUTLIER_REL = 0.10
OUTLIER_MIN_USD = 2.0
AGREE_REL = 0.02

START_REASON = "all_accounts_recording_from"
FLOW_LIMIT = 2000

FLOWS_NOTE_HUMMINGBOT = (
    "Deposits and withdrawals are only tracked for Condor's own accounts; accounts "
    "connected through Hummingbot have no deposit data, so a deposit there is not "
    "marked."
)
FLOWS_NOTE_UNREADABLE = "Deposits and withdrawals of {source} could not be read."
FLOWS_NOTE_UNTRACKED = "Deposits and withdrawals of {source} are not tracked yet."
RESULT_NOTE_HUMMINGBOT = (
    "Deposits and withdrawals of Hummingbot-connected accounts are not recorded, so "
    "they are not taken out of this result."
)


@dataclass
class _Source:
    name: str
    key: str | None  # native venue key, None for Hummingbot
    rows: list[tuple[float, float]]
    first_ts: float | None
    status: str  # recording | pending | disconnected
    tolerance: float
    stamps: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.stamps = [ts for ts, _ in self.rows]

    def reading(self, at: float) -> tuple[float, float] | None:
        """The reading nearest ``at`` within the tolerance, ``None`` when there is none."""
        if not self.rows:
            return None
        i = bisect.bisect_left(self.stamps, at)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.rows):
                if best is None or abs(self.rows[j][0] - at) < abs(best[0] - at):
                    best = self.rows[j]
        if best is not None and abs(best[0] - at) <= self.tolerance:
            return best
        return None


def connected_sources(user_id: int) -> set[str]:
    """Labels of the native accounts that are connected right now (never raises)."""
    out: set[str] = set()
    for venue in native_venues.account_venues():
        try:
            if venue.service.status(user_id)["connected"]:
                out.add(venue.label)
        except Exception:  # noqa: BLE001 - unreadable means "not known connected"
            continue
    return out


def _cadence(interval_seconds: int) -> float:
    return float(max(int(interval_seconds), MIN_CADENCE_SEC))


def exclude_outliers(
    rows: Sequence[tuple[float, float]], tolerance: float
) -> tuple[list[tuple[float, float]], list[dict[str, Any]]]:
    """Drop single rows that jump away from both neighbours and straight back.

    A row is dropped when it differs from the row before *and* the row after by more
    than ``max(10%, 2 USD)`` while those two agree within 2%: a real move does not
    undo itself in one reading, and a deposit leaves the neighbours disagreeing. Each
    drop is returned as an anomaly (never smoothed away silently, never replaced by an
    interpolated value). Neighbours more than ``tolerance`` away are not neighbours.
    """
    kept: list[tuple[float, float]] = []
    anomalies: list[dict[str, Any]] = []
    for i, (ts, value) in enumerate(rows):
        if 0 < i < len(rows) - 1:
            (p_ts, prev), (n_ts, nxt) = rows[i - 1], rows[i + 1]
            near = ts - p_ts <= tolerance and n_ts - ts <= tolerance
            jumps = abs(value - prev) > max(
                OUTLIER_REL * prev, OUTLIER_MIN_USD
            ) and abs(value - nxt) > max(OUTLIER_REL * nxt, OUTLIER_MIN_USD)
            agree = abs(prev - nxt) <= AGREE_REL * max(prev, nxt)
            if near and jumps and agree:
                anomalies.append(
                    {
                        "source": HUMMINGBOT,
                        "ts": ts,
                        "value": value,
                        "neighbour_value": (prev + nxt) / 2,
                    }
                )
                continue
        kept.append((ts, value))
    return kept, anomalies


def _sources(
    user_id: int,
    hummingbot_points: Sequence[Any],
    native: Mapping[str, Sequence[tuple[float, float]]],
    since: float,
    now: float,
    interval_seconds: int,
    skip: Iterable[str],
    connected: set[str] | None,
) -> tuple[list[_Source], list[dict[str, Any]], dict[str, float], list[dict[str, Any]]]:
    """The sources in the series, the source report, each one's baseline time and the
    Hummingbot readings left out as outliers."""
    excluded = set(skip)
    labels = {v.label: v for v in native_venues.account_venues()}
    native_tolerance = TOLERANCE_INTERVALS * float(portfolio_history.SAMPLE_EVERY_SEC)
    included: list[_Source] = []
    report: list[dict[str, Any]] = []
    effective_first: dict[str, float] = {}
    anomalies: list[dict[str, Any]] = []

    hb_rows = sorted(
        (float(p.timestamp), float(p.total_usd))
        for p in hummingbot_points
        if float(p.total_usd) > 0  # a reading of nothing is no reading
    )
    hb_tol = max(
        TOLERANCE_INTERVALS * _cadence(interval_seconds),
        float(portfolio_change.MIN_START_TOLERANCE_SEC),
    )
    hb_rows, anomalies = exclude_outliers(hb_rows, hb_tol)
    if hb_rows:
        hb = _Source(HUMMINGBOT, None, hb_rows, hb_rows[0][0], "recording", hb_tol)
        included.append(hb)
        report.append(
            {"source": HUMMINGBOT, "first_ts": hb.first_ts, "status": "recording"}
        )
        # The history is served for the window only (and cached), so its first point
        # trails the window start a little: that is not "started recording later".
        lag = max(
            portfolio_change.START_TOLERANCE_INTERVALS * int(interval_seconds),
            portfolio_change.MIN_START_TOLERANCE_SEC,
            portfolio_change.START_TOLERANCE_FRACTION * (now - since),
        )
        effective_first[HUMMINGBOT] = (
            since if hb_rows[0][0] <= since + lag else hb_rows[0][0]
        )

    for name in sorted(labels):
        if name in excluded:
            continue
        rows = [(float(ts), float(v)) for ts, v in native.get(name, ())]
        first, _ = portfolio_history.bounds(user_id, name)
        first_ts = float(first[0]) if first else (rows[0][0] if rows else None)
        is_connected = connected is None or name in connected
        if first_ts is None:
            if is_connected:
                report.append({"source": name, "first_ts": None, "status": "pending"})
            continue
        stale = not rows or now - rows[-1][0] > native_tolerance
        if stale and not is_connected:
            report.append(
                {"source": name, "first_ts": first_ts, "status": "disconnected"}
            )
            continue
        src = _Source(
            name, labels[name].key, rows, first_ts, "recording", native_tolerance
        )
        included.append(src)
        report.append({"source": name, "first_ts": first_ts, "status": "recording"})
        effective_first[name] = first_ts
    return included, report, effective_first, anomalies


def _gaps(sources: Sequence[_Source], start: float, now: float) -> list[dict[str, Any]]:
    """Stretches after ``start`` in which a source has no reading around a point."""
    out: list[dict[str, Any]] = []
    for src in sources:
        tol = src.tolerance
        prev = None
        for ts, _ in src.rows:
            if prev is not None and ts - prev > 2 * tol and ts - tol > start:
                out.append({"source": src.name, "from_ts": prev, "to_ts": ts})
            prev = ts
        last = prev
        if last is None:
            out.append({"source": src.name, "from_ts": start, "to_ts": now})
        elif now - last > tol and now > start:
            out.append({"source": src.name, "from_ts": last, "to_ts": now})
    out.sort(key=lambda g: (g["from_ts"], g["source"]))
    return out


def _stamps(
    sources: Sequence[_Source],
    start: float,
    step: int,
    gaps: Sequence[Mapping[str, Any]],
    end: float,
) -> list[float]:
    """One point per interval at the latest reading in it, plus the start and each gap."""
    step = max(int(step), 1)
    buckets: dict[int, float] = {}
    for src in sources:
        for ts in src.stamps:
            if ts > start:
                b = int(ts) // step
                buckets[b] = max(buckets.get(b, 0.0), ts)
    stamps = {start, *buckets.values()}
    for gap in gaps:
        middle = (gap["from_ts"] + gap["to_ts"]) / 2
        if start < middle and gap["to_ts"] < end:
            # Somewhere inside the gap, so the line is cut even when nothing else
            # was recorded in it.
            stamps.add(middle)
    return sorted(stamps)


def _flows(
    user_id: int, sources: Sequence[_Source]
) -> tuple[dict[str, list[dict]], set[str]]:
    """Every flow row per native source (newest first, dismissed ones left out).

    Also the sources whose flows could not be read: their window reads as empty, which
    must never be taken for "no deposits".
    """
    rows: dict[str, list[dict]] = {}
    unreadable: set[str] = set()
    for src in sources:
        if src.key is None:
            continue
        try:
            rows[src.name] = native_flows.list_flows(user_id, src.key, limit=FLOW_LIMIT)
        except Exception as error:  # noqa: BLE001 - never lose the chart over flows
            log.warning("%s flows unreadable (%s)", src.key, type(error).__name__)
            rows[src.name] = []
            unreadable.add(src.name)
    return rows, unreadable


def build(
    user_id: int,
    hummingbot_points: Sequence[Any],
    native: Mapping[str, Sequence[tuple[float, float]]],
    since: float,
    now: float,
    interval_seconds: int,
    skip: Iterable[str] = (),
    connected: set[str] | None = None,
) -> dict[str, Any]:
    """The chart series and everything the page needs to explain it.

    ``hummingbot_points`` are the route's points before the native sources are
    merged in (``timestamp``, ``total_usd``, ``tokens``); ``native`` is
    :func:`condor.portfolio_history.series` (fetch it from ``since - EDGE_MARGIN_SEC``);
    ``skip`` names sources Hummingbot already counts (never counted twice);
    ``connected`` is :func:`connected_sources`, or ``None`` to treat all as connected.
    """
    sources, report, effective_first, anomalies = _sources(
        user_id,
        hummingbot_points,
        native,
        since,
        now,
        interval_seconds,
        skip,
        connected,
    )
    out: dict[str, Any] = {
        "chart_points": [],
        "baseline_ts": None,
        "series_start_ts": None,
        "series_start_reason": None,
        "sources": report,
        "gaps": [],
        "anomalies": [],
        "flows": [],
        "pending_flows": 0,
        "flows_note": None,
        "result_points": None,
        "result_reason": "no_history",
        "result_note": portfolio_change.REASONS["no_history"],
        "result_not_netted": [],
    }
    if not sources:
        return out

    baseline = max(effective_first[s.name] for s in sources)
    start = max(float(since), baseline)
    out["baseline_ts"] = baseline
    out["series_start_ts"] = start
    if baseline > since:
        out["series_start_reason"] = START_REASON

    gaps = _gaps(sources, start, now)
    # Readings left out as outliers stay in the series as breaks: the line is cut at
    # them and they are reported, never smoothed over or replaced by a made-up value.
    bad = [a for a in anomalies if a["ts"] >= start]
    forced_null = {a["ts"] for a in bad}
    stamps = sorted(
        {*_stamps(sources, start, interval_seconds, gaps, now), *forced_null}
    )

    hb = next((s for s in sources if s.key is None), None)
    hb_tokens = {float(p.timestamp): dict(p.tokens or {}) for p in hummingbot_points}
    points: list[dict[str, Any]] = []
    readings: list[dict[str, tuple[float, float]] | None] = []
    for ts in stamps:
        got = {s.name: s.reading(ts) for s in sources}
        if ts in forced_null or any(r is None for r in got.values()):
            points.append({"timestamp": ts, "total_usd": None, "tokens": {}})
            readings.append(None)
            continue
        have = {name: r for name, r in got.items() if r is not None}
        tokens: dict[str, float] = {}
        if hb is not None:
            tokens = dict(hb_tokens.get(have[HUMMINGBOT][0], {}))
        points.append(
            {
                "timestamp": ts,
                "total_usd": round(sum(r[1] for r in have.values()), 6),
                "tokens": tokens,
            }
        )
        readings.append(have)

    # A line that has not started yet, or has stopped, is not a break in it.
    lo, hi = 0, len(points)
    while lo < hi and points[lo]["total_usd"] is None:
        lo += 1
    while hi > lo and points[hi - 1]["total_usd"] is None:
        hi -= 1
    points, readings = points[lo:hi], readings[lo:hi]
    out["chart_points"] = points
    out["gaps"] = gaps
    out["anomalies"] = bad

    if not points:
        out["result_reason"] = "no_history"
        return out
    # The line starts where every account has a reading *and* there is one to draw: a
    # source that was still in a gap at the baseline pushes the start to its return.
    start = out["series_start_ts"] = points[0]["timestamp"]

    flow_rows, unreadable = _flows(user_id, sources)
    window_flows: list[dict[str, Any]] = []
    pending = 0
    notes: list[str] = []
    if hb is not None:
        notes.append(FLOWS_NOTE_HUMMINGBOT)
    for src in sources:
        if src.key is None:
            continue
        try:
            tracked = bool(native_flows.summary(user_id, src.key).get("tracked"))
        except Exception:  # noqa: BLE001 - never lose the chart over flows
            tracked = False
        if src.name in unreadable:
            notes.append(FLOWS_NOTE_UNREADABLE.format(source=src.name))
        elif not tracked:
            notes.append(FLOWS_NOTE_UNTRACKED.format(source=src.name))
        for row in flow_rows.get(src.name, []):
            ts = float(row.get("ts") or 0)
            if not start < ts <= now:
                continue
            if row.get("status") == "pending":
                pending += 1
            elif row.get("status") == "confirmed":
                window_flows.append(
                    {
                        "ts": ts,
                        "venue": src.name,
                        "currency": row["currency"],
                        "amount": row["amount"],
                        "usd_value_at_time": row["usd_value_at_time"],
                        "source": row["source"],
                        "status": "confirmed",
                    }
                )
    window_flows.sort(key=lambda f: (f["ts"], f["venue"]))
    out["flows"] = window_flows
    out["pending_flows"] = pending
    out["flows_note"] = " ".join(notes) or None

    result = _result(
        user_id,
        sources,
        points,
        readings,
        flow_rows,
        unreadable,
    )
    out.update(result)
    return out


def _result(
    user_id: int,
    sources: Sequence[_Source],
    points: Sequence[Mapping[str, Any]],
    readings: Sequence[Mapping[str, tuple[float, float]] | None],
    flow_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    unreadable: set[str],
) -> dict[str, Any]:
    """``result_points`` (or why not): the value change net of confirmed flows."""

    def no(reason: str, source: str = "") -> dict[str, Any]:
        info = portfolio_change.unavailable(reason, source)
        return {
            "result_points": None,
            "result_reason": reason,
            "result_note": info["note"],
            "result_not_netted": [],
        }

    if len(points) < 2:
        return no("no_history")

    base = readings[0]
    if base is None:
        return no("no_history")

    # Per source: its confirmed, priced flows as (ts, signed USD), after the checks
    # that decide whether a deposit could be told from a gain at all.
    flows: dict[str, list[tuple[float, float]]] = {}
    for src in sources:
        if src.key is None:
            continue
        if src.name in unreadable:
            return no("flows_unreadable", src.name)
        try:
            info = native_flows.summary(user_id, src.key)
        except Exception as error:  # noqa: BLE001
            log.warning("%s flows unreadable (%s)", src.key, type(error).__name__)
            return no("flows_unreadable", src.name)
        if not info.get("tracked"):
            return no("flows_untracked", src.name)
        rows = flow_rows.get(src.name, [])
        if len(rows) >= FLOW_LIMIT:
            return no("flows_unreadable", src.name)
        # What matters is the span of readings actually compared for this source.
        lo = base[src.name][0]
        hi = max(r[src.name][0] for r in readings if r is not None)
        mine: list[tuple[float, float]] = []
        for row in rows:
            ts = float(row.get("ts") or 0)
            if not lo < ts <= hi:
                continue
            if row.get("status") == "pending":
                return no("flows_pending", src.name)
            if row.get("status") != "confirmed":
                continue
            if row.get("usd_value_at_time") is None:
                return no("flows_unpriced", src.name)
            mine.append((ts, float(row["usd_value_at_time"])))
        flows[src.name] = sorted(mine)

    def net(name: str, after: float, upto: float) -> float:
        return sum(v for ts, v in flows.get(name, []) if after < ts <= upto)

    out: list[dict[str, Any]] = []
    for point, have in zip(points, readings):
        if have is None or point["total_usd"] is None:
            out.append({"timestamp": point["timestamp"], "result_usd": None})
            continue
        gain = 0.0
        for name, (r_ts, r_val) in have.items():
            b_ts, b_val = base[name]
            gain += r_val - b_val - net(name, b_ts, r_ts)
        out.append({"timestamp": point["timestamp"], "result_usd": round(gain, 6)})

    not_netted = [s.name for s in sources if s.key is None]
    return {
        "result_points": out,
        "result_reason": None,
        "result_note": RESULT_NOTE_HUMMINGBOT if not_netted else None,
        "result_not_netted": not_netted,
    }
