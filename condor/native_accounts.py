"""One reading of the accounts Condor trades itself (Crypto.com Exchange, Kraken xStocks).

Hummingbot sees none of this money, so three surfaces used to show it as zero or
not at all. They now share this module, on the venue registry in
:mod:`condor.native_venues`:

* the dashboard portfolio (``collect_views``),
* the loop cards' PnL, which for a native crew is the change in account equity
  (``performance``), and
* the chat agent's read-only ``native_portfolio`` tool (``overview``).

Values are whole-account USD at the last traded price, the same figure the
recorder in :mod:`condor.portfolio_history` samples every five minutes. Nothing
here moves money.

**What "PnL" means here.** It is the change in account equity between two
readings, net of the deposits and withdrawals recorded in
:mod:`condor.native_flows` (confirmed rows only, in the window between the two
readings). The venue ledgers keep cash, positions and orders, not external
transfers, so those come from the venue's transfer history, from detection that
the owner confirms, or from manual entry. For a venue with no flow recorded and
no history that could be read, the figure is still the plain equity change and the
label says deposits are not tracked. Either way the whole account is measured, not
only the bot allocation, and prices of held assets move it too.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple

from condor import native_flows, native_venues, portfolio_history

log = logging.getLogger(__name__)

#: A live whole-account valuation is reused this long (one exchange round trip).
LIVE_TTL_SEC = 30.0
#: A recorded sample this close to a session boundary counts as "at" it. The
#: recorder samples every 5 minutes, so three intervals leaves room for a miss.
SAMPLE_TOLERANCE_SEC = 900
#: Tokens valued as cash in the chat overview.
CASH_TOKENS = frozenset({"USD", "USDT", "USDC", "EUR"})

BASIS = "account_equity"
BASIS_LABEL = "Change in account equity (deposits and withdrawals are not tracked)"
BASIS_LABEL_NET = (
    "Trading result: equity change net of recorded deposits and withdrawals"
)


def basis_label(tracked: bool) -> str:
    """The honest wording: net of flows only when the venue's flows are tracked."""
    return BASIS_LABEL_NET if tracked else BASIS_LABEL


_live: dict[tuple[str, int], tuple[float, Any]] = {}


def _service(venue: native_venues.NativeVenue, services: Mapping[str, Any] | None):
    return (services or {}).get(venue.key) or venue.service


def _connected(service: Any, user_id: int) -> bool:
    try:
        return bool(service.status(user_id)["connected"])
    except Exception:  # noqa: BLE001 - an unreadable ledger is "not connected" here
        return False


async def value_account(
    venue: native_venues.NativeVenue,
    user_id: int,
    *,
    services: Mapping[str, Any] | None = None,
    max_age: float = LIVE_TTL_SEC,
) -> dict:
    """The venue's whole-account USD view, reused for ``max_age`` seconds.

    A failure is reused for the same time, so a venue that is down is not asked
    again on every poll of the loop cards. ``max_age=0`` always asks.
    """
    key = (venue.key, user_id)
    cached = _live.get(key)
    if max_age > 0 and cached and time.monotonic() - cached[0] < max_age:
        if isinstance(cached[1], Exception):
            raise cached[1]
        return cached[1]
    try:
        view = await _service(venue, services).portfolio(user_id)
    except Exception as error:
        _live[key] = (time.monotonic(), error)
        raise
    _live[key] = (time.monotonic(), view)
    return view


def forget() -> None:
    """Drop the live-valuation cache (tests)."""
    _live.clear()


# ── dashboard ──


@dataclass
class NativeViews:
    views: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


async def collect_views(
    user_id: int,
    *,
    skip: Any = (),
    services: Mapping[str, Any] | None = None,
) -> NativeViews:
    """Each connected account's dashboard view, in registry order.

    The samples the history chart reads are recorded as a side effect (the
    recorder throttles). ``skip`` names venue keys already counted elsewhere
    (Hummingbot's own ``kraken`` connector values the same account). One venue
    failing becomes a note and never hides another.
    """
    result = NativeViews()
    for venue in native_venues.account_venues():
        if venue.key in skip:
            continue
        service = _service(venue, services)
        if not service.status(user_id)["connected"]:
            continue
        try:
            # Credentials belong to the user, never the shared server cache.
            view = await value_account(venue, user_id, services=services, max_age=0)
            portfolio_history.record(user_id, venue.label, view["total_usd"])
            # Detection only: the dashboard read makes no private history call.
            await native_flows.observe(user_id, venue.key, service, view, fetch=False)
            result.views.append(view)
        except Exception as error:  # noqa: BLE001 - one venue never hides another
            log.warning(
                "%s portfolio unavailable (%s)", venue.label, type(error).__name__
            )
            result.notes.append(venue.unvalued_note)
    return result


# ── equity history ──


class Reading(NamedTuple):
    """One equity figure and how far it is from the time it stands for."""

    total: float
    ts: float
    #: ``ts`` minus the time asked for: negative = sampled before it.
    gap: float

    @property
    def exact(self) -> bool:
        return abs(self.gap) <= SAMPLE_TOLERANCE_SEC


def pick(before, after, ts: float) -> Reading | None:
    """The sample that best stands for ``ts``.

    The latest sample at-or-before it, when it is recent enough; otherwise the
    nearer of the two neighbours (the first after wins only when strictly
    nearer), and whatever exists when there is just one. ``None`` without any
    sample. The caller flags a result whose ``exact`` is false.
    """
    candidates = [
        Reading(float(row[1]), float(row[0]), float(row[0]) - ts)
        for row in (before, after)
        if row is not None
    ]
    if not candidates:
        return None
    first = candidates[0] if before is not None else None
    if first is not None and first.exact:
        return first
    return min(candidates, key=lambda r: (abs(r.gap), r.gap > 0))


def _span_text(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 120:
        return f"{int(seconds)} s"
    if seconds < 7200:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def _boundary_note(reading: Reading, what: str) -> str:
    if reading.exact:
        return ""
    side = "before" if reading.gap < 0 else "after"
    return (
        f"{what} estimated from the equity sample {_span_text(reading.gap)} {side} it."
    )


@dataclass
class SessionSpan:
    """One session's time window. ``live`` means it has no end yet."""

    num: int
    start: float
    end: float = 0.0
    live: bool = False


@dataclass
class SessionPnl:
    num: int
    #: ``None`` when it cannot be stated (its start time is not on disk).
    pnl: float | None
    estimated: bool = False
    note: str = ""
    #: Net confirmed deposits/withdrawals (USD) already taken out of ``pnl``.
    flows_usd: float = 0.0


@dataclass
class NativePerformance:
    sessions: list[SessionPnl] = field(default_factory=list)
    #: Equity change from the first session's start to now.
    total_pnl: float = 0.0
    #: When that total's window starts (unix seconds), 0.0 if unknown.
    since: float = 0.0
    estimated: bool = False
    note: str = ""
    open_positions: int = 0
    #: ``""`` when priced; ``no_history`` or ``unreachable``.
    unavailable: str = ""
    #: Net confirmed flows (USD) taken out of ``total_pnl``.
    flows_usd: float = 0.0
    #: The venue's flows are tracked (history read or any flow confirmed), so the
    #: figures are a trading result; ``False`` keeps the "not tracked" wording.
    flows_tracked: bool = False
    #: Unconfirmed (pending) movements: not subtracted, but never hidden.
    pending_count: int = 0
    pending_usd: float = 0.0


def _flow_state(user_id: int, venue_key: str) -> dict:
    """The venue's flow summary, or an untracked, empty one when the store cannot be read."""
    try:
        return native_flows.summary(user_id, venue_key)
    except Exception as error:  # noqa: BLE001 - flows never break the PnL
        log.warning("%s flows unreadable (%s)", venue_key, type(error).__name__)
        return {"tracked": False, "pending_count": 0, "pending_net_usd": 0.0}


def _flows_between(
    user_id: int, venue_key: str, start: float, end: float
) -> native_flows.FlowWindow:
    try:
        return native_flows.window_net(user_id, venue_key, start, end)
    except Exception as error:  # noqa: BLE001
        log.warning("%s flows unreadable (%s)", venue_key, type(error).__name__)
        return native_flows.FlowWindow(failed=True)


def _unpriced_note(window: native_flows.FlowWindow) -> str:
    if not window.unpriced:
        return ""
    return (
        f"{window.unpriced} recorded flow{'s' if window.unpriced != 1 else ''} "
        "without a USD value is not subtracted."
    )


def _ledger_positions(service: Any, user_id: int) -> int:
    try:
        return len(service.status(user_id)["ledger"].get("positions") or {})
    except Exception:  # noqa: BLE001
        return 0


async def performance(
    venue: native_venues.NativeVenue,
    user_id: int,
    spans: list[SessionSpan],
    *,
    services: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> NativePerformance:
    """Equity change per session and overall for a native crew's loop.

    A session's figure is the venue's equity at its end (now, while it runs)
    minus the equity at its start, both from the value history; the start is the
    latest sample at-or-before it, or, when none is that close, the nearest
    sample, flagged as an estimate. No history at all is ``no_history``, never a
    made-up zero.
    """
    now = time.time() if now is None else now
    out = NativePerformance()
    service = _service(venue, services)
    out.open_positions = _ledger_positions(service, user_id)
    state = _flow_state(user_id, venue.key)
    out.flows_tracked = bool(state.get("tracked"))
    out.pending_count = int(state.get("pending_count") or 0)
    out.pending_usd = float(state.get("pending_net_usd") or 0.0)
    first, last = portfolio_history.bounds(user_id, venue.label)
    if first is None or last is None:
        out.unavailable = "no_history"
        return out

    # "Now": the live account when it can be read, else the newest sample.
    now_note = ""
    live: Reading | None = None
    if _connected(service, user_id):
        try:
            view = await value_account(venue, user_id, services=services)
            live = Reading(float(view["total_usd"]), now, 0.0)
        except Exception as error:  # noqa: BLE001 - fall back to the recorded sample
            log.warning("%s live value skipped (%s)", venue.label, type(error).__name__)
    if live is None:
        live = Reading(float(last[1]), float(last[0]), float(last[0]) - now)
        now_note = (
            f"Account not read live; latest value is {_span_text(now - last[0])} old."
        )
    stale_now = bool(now_note)

    valid = [s for s in spans if s.start > 0]
    ends = [s.end for s in valid if not s.live and s.end > 0]
    pairs = portfolio_history.nearest(
        user_id,
        venue.label,
        [s.start for s in valid] + ends,
    )
    starts = {s.num: pick(*pairs[i], s.start) for i, s in enumerate(valid)}
    end_readings = dict(
        zip(ends, (pick(*p, e) for p, e in zip(pairs[len(valid) :], ends)))
    )

    for span in sorted(spans, key=lambda s: s.num):
        begin = starts.get(span.num)
        if begin is None:
            note = (
                "Session start time is not recorded."
                if span.start <= 0
                else "No equity sample near the session start."
            )
            out.sessions.append(SessionPnl(span.num, None, True, note))
            continue
        notes = [_boundary_note(begin, "Session start")]
        estimated = not begin.exact
        if span.live:
            finish = live
            if stale_now:
                notes.append(now_note)
                estimated = True
        elif span.end > 0:
            finish = end_readings.get(span.end)
            if finish is None:
                out.sessions.append(
                    SessionPnl(
                        span.num, None, True, "No equity sample near the session end."
                    )
                )
                continue
            notes.append(_boundary_note(finish, "Session end"))
            estimated = estimated or not finish.exact
        else:
            out.sessions.append(
                SessionPnl(span.num, None, True, "The session's end is not recorded.")
            )
            continue
        # The equity change is between the two readings actually compared, so the
        # flows taken out are those stamped in (begin.ts, finish.ts].
        window = _flows_between(user_id, venue.key, begin.ts, finish.ts)
        notes.append(_unpriced_note(window))
        out.sessions.append(
            SessionPnl(
                span.num,
                finish.total - begin.total - window.net_usd,
                estimated,
                " ".join(n for n in notes if n),
                window.net_usd,
            )
        )

    # All sessions: from the first one's start (or the first recorded sample when
    # no session start is on disk) to the account's value now.
    if valid:
        out.since = min(s.start for s in valid)
        origin = starts[min(valid, key=lambda s: s.start).num] or Reading(
            float(first[1]), float(first[0]), float(first[0]) - out.since
        )
        total_note = _boundary_note(origin, "Start of the first session")
    else:
        out.since = float(first[0])
        origin = Reading(float(first[1]), float(first[0]), 0.0)
        total_note = "Since the first recorded equity sample."
    total_flows = _flows_between(user_id, venue.key, origin.ts, live.ts)
    out.flows_usd = total_flows.net_usd
    out.total_pnl = live.total - origin.total - total_flows.net_usd
    out.estimated = not origin.exact or stale_now
    out.note = " ".join(
        n for n in (total_note, now_note, _unpriced_note(total_flows)) if n
    )
    return out


# ── chat tool ──


def _round(value: float) -> float:
    return round(float(value), 2)


def _equity_change(
    user_id: int, label: str, total_now: float, now: float, venue_key: str = ""
) -> dict:
    """Equity change per window, net of confirmed flows when the venue's are tracked.

    ``change_usd`` is the trading result; ``equity_change_usd`` the raw difference
    and ``net_flows_usd`` what was taken out of it (the window is
    ``(sample time, now]``).
    """
    tracked = bool(venue_key) and bool(_flow_state(user_id, venue_key).get("tracked"))
    first, last = portfolio_history.bounds(user_id, label)
    if first is None:
        return {
            "available": False,
            "reason": "no_history",
            "basis": basis_label(tracked),
        }
    day_ago = pick(
        *portfolio_history.nearest(user_id, label, [now - 86400])[0], now - 86400
    )

    def window(from_ts: float, from_total: float) -> dict:
        flows = (
            _flows_between(user_id, venue_key, from_ts, now)
            if venue_key
            else native_flows.FlowWindow()
        )
        part = {
            "from_usd": _round(from_total),
            "change_usd": _round(total_now - from_total - flows.net_usd),
            "equity_change_usd": _round(total_now - from_total),
            "net_flows_usd": _round(flows.net_usd),
        }
        if flows.unpriced:
            part["unpriced_flows"] = flows.unpriced
        return part

    change = {
        "available": True,
        "basis": basis_label(tracked),
        "since_first_sample": {
            "recorded_at": int(first[0]),
            **window(float(first[0]), float(first[1])),
        },
    }
    if day_ago is not None and day_ago.exact:
        change["last_24h"] = window(float(day_ago.ts), day_ago.total)
    return change


async def overview(
    user_id: int,
    *,
    services: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict:
    """What the chat agent needs to answer "what is my portfolio" for native venues.

    Per venue: connected, the whole account's USD value split into cash and
    positions, the bot allocation's ledger (cash, positions at cost, synced,
    enabled, uncertain order) and the equity change from the value history. A
    venue that is not connected or cannot be valued says so instead of reading
    as zero.
    """
    now = time.time() if now is None else now
    venues, total = [], 0.0
    for venue in native_venues.account_venues():
        service = _service(venue, services)
        entry: dict[str, Any] = {"venue": venue.key, "connector": venue.label}
        try:
            status = service.status(user_id)
        except Exception as error:  # noqa: BLE001
            entry.update(connected=False, error=type(error).__name__)
            venues.append(entry)
            continue
        entry["connected"] = bool(status["connected"])
        if not entry["connected"]:
            entry["note"] = "Not connected; the owner adds the API key in Settings."
            venues.append(entry)
            continue
        ledger = status.get("ledger") or {}
        entry["ledger"] = {
            "quote": ledger.get("quote"),
            "synced": bool(ledger.get("synced")),
            "trading_enabled": bool(status.get("enabled")),
            "uncertain_order": bool(ledger.get("uncertain_order")),
            "cash": ledger.get("cash"),
            "positions": [
                {"symbol": symbol, "units": p.get("units"), "cost": p.get("cost")}
                for symbol, p in (ledger.get("positions") or {}).items()
            ],
        }
        try:
            view = await value_account(venue, user_id, services=services, max_age=0)
        except Exception as error:  # noqa: BLE001
            log.warning(
                "%s overview unavailable (%s)", venue.label, type(error).__name__
            )
            entry["error"] = (
                f"Could not be valued ({type(error).__name__}); excluded from the total."
            )
            venues.append(entry)
            continue
        balances = view.get("balances") or []
        cash = [b for b in balances if b.get("token") in CASH_TOKENS]
        held = [b for b in balances if b.get("token") not in CASH_TOKENS]
        value = float(view["total_usd"])
        total += value
        entry["account"] = {
            "total_usd": _round(value),
            "cash_usd": _round(sum(b["usd_value"] for b in cash)),
            "positions": [
                {
                    "token": b["token"],
                    "units": b["total"],
                    "available": b["available"],
                    "usd_value": _round(b["usd_value"]),
                }
                for b in sorted(held, key=lambda b: -b["usd_value"])
                if b["usd_value"] >= 0.01
            ],
            "note": view.get("note", ""),
        }
        try:
            entry["equity_change"] = _equity_change(
                user_id, venue.label, value, now, venue.key
            )
        except Exception as error:  # noqa: BLE001 - history never blocks the balances
            entry["equity_change"] = {
                "available": False,
                "reason": type(error).__name__,
            }
        info = _flow_state(user_id, venue.key)
        entry["flows"] = {
            "recorded_net_flows_usd": info.get("recorded_net_flows_usd", 0.0),
            "confirmed_count": info.get("confirmed_count", 0),
            "pending_count": info.get("pending_count", 0),
            "pending_net_usd": info.get("pending_net_usd", 0.0),
            "tracked": bool(info.get("tracked")),
            "history_read": info.get("fetch"),
        }
        if info.get("pending_count"):
            entry["flows"]["note"] = native_flows.pending_note(info)
        venues.append(entry)
    return {
        "venues": venues,
        "total_usd": _round(total),
        "notes": [
            "Whole accounts at the last traded price, in USD. Only the bot allocation (ledger) may be traded.",
            "Hummingbot-connected accounts are not included; get_portfolio_overview covers those.",
            "Kraken xStocks is the same Kraken account that Hummingbot's `kraken` connector may also report; do not add both.",
            "Equity change is the change in account value between readings, net of the confirmed deposits and withdrawals recorded for that venue (see its flows block). When a venue's basis says deposits are not tracked, a deposit still reads as a gain. Pending (unconfirmed) movements are not subtracted; mention them to the owner, who confirms or dismisses them in the Trade page.",
        ],
    }
