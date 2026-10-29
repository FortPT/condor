"""External cash flows (deposits and withdrawals) of the native venues.

A native venue's PnL used to be the change in whole-account equity, so a deposit
read as profit and a withdrawal as loss. This module keeps one small record of the
money that crossed the account boundary, per venue, so the PnL surfaces in
:mod:`condor.native_accounts` can subtract it ("trading result").

Where the rows come from:

* ``api``: the venue's own transfer history (:mod:`condor.native_flow_fetchers`),
  idempotent by ``(venue, external_id)``. Counted when ``confirmed``.
* ``detected``: a balance that moved with no matching bot order since the last
  reading. Always ``pending``: shown to the owner, **not** subtracted until the owner
  confirms it (or dismissed).
* ``manual``: entered by the owner (backfill, or a venue whose history cannot be read).

Only ``confirmed`` rows with a USD value are subtracted. ``usd_value_at_time`` is
stored when the row is written, so later rate moves never re-price history.

Storage: one sqlite file per user, ``$CONDOR_DATA_DIR/native_flows/<user>/flows.db``
(the style of :mod:`condor.portfolio_history`). Nothing here moves money and nothing
here talks to an exchange except the read-only history fetch in :func:`sync`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from condor import native_flow_fetchers as fetchers
from condor import native_venues
from condor.paths import data_dir

log = logging.getLogger(__name__)

SOURCES = ("api", "detected", "manual")
STATUSES = ("confirmed", "pending", "dismissed")
USD_LIKE = frozenset({"USD", "USDT", "USDC"})

#: The venue history is read at most this often per user and venue.
FETCH_EVERY_SEC = 900
DEFAULT_LOOKBACK_DAYS = 30
MAX_LOOKBACK_DAYS = 365
#: A fetch that succeeded this recently counts as "the venue's flows are tracked".
FETCH_FRESH_SEC = 86400

#: A balance change smaller than this (USD) is noise, never a flow ...
DETECT_MIN_USD = 0.5
#: ... and so is one below this share of what the token was worth at the last reading.
DETECT_FRACTION = 0.02
#: A recorded flow explains a balance move when it was recorded after the last
#: reading and is stamped after it, at most this far ahead of now (clock skew).
MATCH_SLACK_SEC = 120
#: A new api/manual row supersedes a pending detection of the same money when the
#: detection is at most this old and within this relative amount.
SUPERSEDE_WINDOW_SEC = 86400
SUPERSEDE_REL = 0.05
MAX_AMOUNT = Decimal("1000000000000")
FUTURE_SLACK_SEC = 300

_CURRENCY = re.compile(r"[A-Za-z0-9]{2,12}")
_ID = re.compile(r"[0-9a-f]{32}")


# ── storage ──


def _db(user_id: int) -> Path:
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid account owner")
    return data_dir() / "native_flows" / str(user_id) / "flows.db"


def _connect(user_id: int) -> sqlite3.Connection:
    path = _db(user_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS flows (
            id TEXT PRIMARY KEY, venue TEXT NOT NULL, ts REAL NOT NULL,
            currency TEXT NOT NULL, amount TEXT NOT NULL, usd_value REAL,
            source TEXT NOT NULL, status TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '', external_id TEXT, created REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS flows_external
            ON flows (venue, external_id) WHERE external_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS flows_venue_ts ON flows (venue, ts);
        CREATE TABLE IF NOT EXISTS marks (
            venue TEXT PRIMARY KEY, ts REAL NOT NULL, balances TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fetches (
            venue TEXT PRIMARY KEY, ts REAL NOT NULL, ok INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '', count INTEGER NOT NULL DEFAULT 0);
        """)
    return conn


def _venue(key: str) -> native_venues.NativeVenue:
    try:
        venue = native_venues.get(key)
    except KeyError:
        raise ValueError("Unknown venue") from None
    if not venue.account:
        raise ValueError("This venue holds no account, so it has no deposits")
    return venue


def _row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "venue": row["venue"],
        "ts": row["ts"],
        "currency": row["currency"],
        "amount": row["amount"],
        "usd_value_at_time": row["usd_value"],
        "source": row["source"],
        "status": row["status"],
        "note": row["note"],
        "external_id": row["external_id"],
    }


def _amount(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Enter an amount")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("Enter an amount as a plain number") from None
    if not result.is_finite() or result == 0:
        raise ValueError("The amount must be a non-zero number")
    if abs(result) > MAX_AMOUNT:
        raise ValueError("The amount is too large")
    return result


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _signed_usd(usd, amount: Decimal) -> float | None:
    """The USD value with the amount's sign, or ``None`` when there is none."""
    if usd is None:
        return None
    try:
        number = float(usd)
    except (TypeError, ValueError):
        raise ValueError("The USD value must be a number") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("The USD value must be a number")
    return abs(number) if amount > 0 else -abs(number)


def parse_ts(value, now: float | None = None) -> float:
    """A flow's time from epoch seconds or an ISO date/time (a bare date is noon UTC)."""
    now = time.time() if now is None else now
    if value is None or value == "":
        return float(now)
    if isinstance(value, bool):
        raise ValueError("Invalid time")
    if isinstance(value, (int, float)):
        ts = float(value)
    else:
        text = str(value).strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                moment = datetime.fromisoformat(text + "T12:00:00+00:00")
            else:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
            ts = moment.timestamp()
        except ValueError:
            raise ValueError("Use a date such as 2026-10-28") from None
    if not ts == ts or ts <= 0:
        raise ValueError("Invalid time")
    if ts > now + FUTURE_SLACK_SEC:
        raise ValueError("A deposit or withdrawal cannot be dated in the future")
    return ts


def _supersede(conn: sqlite3.Connection, row: dict, now: float) -> None:
    """Dismiss pending detections of the money a new confirmed/pending row already names."""
    if row["source"] == "detected" or row["status"] == "dismissed":
        return
    amount = Decimal(row["amount"])
    for other in conn.execute(
        "SELECT id, amount FROM flows WHERE venue = ? AND currency = ? AND"
        " source = 'detected' AND status = 'pending' AND id != ? AND ts >= ?",
        (row["venue"], row["currency"], row["id"], now - SUPERSEDE_WINDOW_SEC),
    ).fetchall():
        theirs = Decimal(other["amount"])
        if (theirs > 0) != (amount > 0):
            continue
        if abs(theirs - amount) <= abs(amount) * Decimal(str(SUPERSEDE_REL)):
            conn.execute(
                "UPDATE flows SET status = 'dismissed', note = note || ? WHERE id = ?",
                (
                    f" (superseded by {row['source']} entry {row['id'][:8]})",
                    other["id"],
                ),
            )


def add_flow(
    user_id: int,
    venue: str,
    *,
    amount,
    currency: str,
    ts: float,
    usd_value=None,
    source: str,
    status: str = "confirmed",
    note: str = "",
    external_id: str | None = None,
    now: float | None = None,
) -> tuple[dict, bool]:
    """Store one flow. Returns ``(row, created)``.

    A row with an ``external_id`` already stored is not duplicated: a ``pending``
    one that is now ``confirmed`` is promoted (amount, time and value refreshed),
    anything else (confirmed, or dismissed by the owner) is left exactly as it is.
    """
    _venue(venue)
    if source not in SOURCES or status not in ("confirmed", "pending"):
        raise ValueError("Invalid flow source or status")
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency.strip()):
        raise ValueError("Enter a currency code such as EUR")
    value = _amount(amount)
    usd = _signed_usd(usd_value, value)
    now = time.time() if now is None else now
    currency = currency.strip().upper()
    note = str(note or "").strip()[:200]
    with closing(_connect(user_id)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if external_id is not None:
            found = conn.execute(
                "SELECT * FROM flows WHERE venue = ? AND external_id = ?",
                (venue, external_id),
            ).fetchone()
            if found is not None:
                if found["status"] == "pending" and status == "confirmed":
                    conn.execute(
                        "UPDATE flows SET status = 'confirmed', ts = ?, amount = ?,"
                        " usd_value = COALESCE(?, usd_value) WHERE id = ?",
                        (float(ts), _fmt(value), usd, found["id"]),
                    )
                    promoted = _row(
                        conn.execute(
                            "SELECT * FROM flows WHERE id = ?", (found["id"],)
                        ).fetchone()
                    )
                    _supersede(conn, promoted, now)
                    conn.commit()
                    return promoted, False
                conn.commit()
                return _row(found), False
        flow_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO flows (id, venue, ts, currency, amount, usd_value, source,"
            " status, note, external_id, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                flow_id,
                venue,
                float(ts),
                currency,
                _fmt(value),
                usd,
                source,
                status,
                note,
                external_id,
                now,
            ),
        )
        created = _row(
            conn.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)).fetchone()
        )
        _supersede(conn, created, now)
        conn.commit()
    return created, True


def list_flows(
    user_id: int,
    venue: str | None = None,
    *,
    include_dismissed: bool = False,
    limit: int = 500,
) -> list[dict]:
    """Newest first. Read-only: a user without flows gets ``[]`` and no file."""
    if venue is not None:
        _venue(venue)
    if not _db(user_id).exists():
        return []
    sql, args = "SELECT * FROM flows WHERE 1=1", []
    if venue is not None:
        sql += " AND venue = ?"
        args.append(venue)
    if not include_dismissed:
        sql += " AND status != 'dismissed'"
    sql += " ORDER BY ts DESC, created DESC LIMIT ?"
    args.append(max(1, min(int(limit), 2000)))
    with closing(_connect(user_id)) as conn:
        return [_row(r) for r in conn.execute(sql, args).fetchall()]


def _get(conn: sqlite3.Connection, flow_id: str) -> sqlite3.Row:
    if not isinstance(flow_id, str) or not _ID.fullmatch(flow_id):
        raise KeyError(flow_id)
    row = conn.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)).fetchone()
    if row is None:
        raise KeyError(flow_id)
    return row


def set_status(
    user_id: int, flow_id: str, status: str, now: float | None = None
) -> dict:
    """Confirm or dismiss a flow (``KeyError`` when it does not exist)."""
    if status not in ("confirmed", "dismissed"):
        raise ValueError("Status must be confirmed or dismissed")
    if not _db(user_id).exists():
        raise KeyError(flow_id)
    now = time.time() if now is None else now
    with closing(_connect(user_id)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        found = _get(conn, flow_id)
        if found["status"] != status:
            conn.execute("UPDATE flows SET status = ? WHERE id = ?", (status, flow_id))
        row = _row(_get(conn, flow_id))
        if status == "confirmed":
            _supersede(conn, row, now)
        conn.commit()
    return row


def delete_manual(user_id: int, flow_id: str) -> None:
    if not _db(user_id).exists():
        raise KeyError(flow_id)
    with closing(_connect(user_id)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        found = _get(conn, flow_id)
        if found["source"] != "manual":
            raise ValueError(
                "Only entries you added by hand can be deleted; dismiss this one instead"
            )
        conn.execute("DELETE FROM flows WHERE id = ?", (flow_id,))
        conn.commit()


# ── the maths ──


@dataclass
class FlowWindow:
    """Confirmed flows with a time in ``(start, end]``."""

    #: Signed USD, + deposits, - withdrawals (confirmed, priced rows only).
    net_usd: float = 0.0
    count: int = 0
    #: Confirmed rows without a USD value: not subtracted, so the figure says so.
    unpriced: int = 0
    #: ``True`` when the store could not be read (the window then reads as empty).
    failed: bool = False


def window_net(user_id: int, venue: str, start: float, end: float) -> FlowWindow:
    """Net confirmed flows in ``(start, end]``.

    Half-open on purpose: a flow stamped exactly at ``start`` belongs to the window
    before it (the reading at ``start`` already includes it) and one stamped exactly
    at ``end`` belongs to this window, so consecutive windows count each flow once.
    The windows the PnL uses start and end at the equity readings actually compared,
    which are the session boundaries when a sample sits on them.
    """
    _venue(venue)
    out = FlowWindow()
    if end <= start or not _db(user_id).exists():
        return out
    with closing(_connect(user_id)) as conn:
        for row in conn.execute(
            "SELECT usd_value FROM flows WHERE venue = ? AND status = 'confirmed'"
            " AND ts > ? AND ts <= ?",
            (venue, float(start), float(end)),
        ).fetchall():
            out.count += 1
            if row["usd_value"] is None:
                out.unpriced += 1
            else:
                out.net_usd += float(row["usd_value"])
    return out


def pending(user_id: int, venue: str) -> list[dict]:
    _venue(venue)
    if not _db(user_id).exists():
        return []
    with closing(_connect(user_id)) as conn:
        return [
            _row(r)
            for r in conn.execute(
                "SELECT * FROM flows WHERE venue = ? AND status = 'pending'"
                " ORDER BY ts DESC",
                (venue,),
            ).fetchall()
        ]


def last_fetch(user_id: int, venue: str) -> dict | None:
    if not _db(user_id).exists():
        return None
    with closing(_connect(user_id)) as conn:
        row = conn.execute("SELECT * FROM fetches WHERE venue = ?", (venue,)).fetchone()
    if row is None:
        return None
    return {
        "ts": row["ts"],
        "ok": bool(row["ok"]),
        "reason": row["reason"],
        "count": row["count"],
    }


def summary(user_id: int, venue: str, now: float | None = None) -> dict:
    """What the PnL and the chat tool say about a venue's flows.

    ``tracked`` is what keeps the label honest: true when the venue's transfer
    history was read successfully in the last day, or when any flow is confirmed.
    """
    _venue(venue)
    now = time.time() if now is None else now
    out: dict[str, Any] = {
        "venue": venue,
        "recorded_net_flows_usd": 0.0,
        "confirmed_count": 0,
        "pending_count": 0,
        "pending_net_usd": 0.0,
        "unpriced_count": 0,
        "tracked": False,
        "fetch": None,
    }
    if not _db(user_id).exists():
        return out
    with closing(_connect(user_id)) as conn:
        for row in conn.execute(
            "SELECT status, usd_value FROM flows WHERE venue = ? AND status != 'dismissed'",
            (venue,),
        ).fetchall():
            if row["status"] == "confirmed":
                out["confirmed_count"] += 1
                if row["usd_value"] is None:
                    out["unpriced_count"] += 1
                else:
                    out["recorded_net_flows_usd"] += float(row["usd_value"])
            else:
                out["pending_count"] += 1
                out["pending_net_usd"] += float(row["usd_value"] or 0.0)
    fetch = last_fetch(user_id, venue)
    out["fetch"] = fetch
    out["tracked"] = bool(
        out["confirmed_count"]
        or (fetch and fetch["ok"] and now - fetch["ts"] < FETCH_FRESH_SEC)
    )
    out["recorded_net_flows_usd"] = round(out["recorded_net_flows_usd"], 2)
    out["pending_net_usd"] = round(out["pending_net_usd"], 2)
    return out


def pending_note(info: Mapping[str, Any]) -> str:
    """The visible line for unconfirmed movements, ``""`` when there are none."""
    count = int(info.get("pending_count") or 0)
    if not count:
        return ""
    net = float(info.get("pending_net_usd") or 0.0)
    sign = "+" if net >= 0 else "-"
    return (
        f"Unconfirmed cash movement of {sign}${abs(net):,.2f} "
        f"({count} item{'s' if count != 1 else ''}) is not subtracted until you confirm it."
    )


# ── pricing ──


async def usd_rate(service, currency: str, view: Mapping | None = None, user_id=None):
    """USD per unit of ``currency``, or ``None`` when it cannot be priced.

    Dollar stablecoins are 1; EUR uses the venue's own ``eur_usd`` (the path its
    portfolio uses), falling back to Kraken's public EURUSD; anything else is read from
    the account view's own price (value / units).
    """
    code = currency.upper()
    if code in USD_LIKE:
        return Decimal(1)
    if code == "EUR":
        sources = [service]
        try:
            from condor.kraken_xstocks import service as kraken

            sources.append(kraken)
        except Exception:  # noqa: BLE001
            pass
        for source in sources:
            fn = getattr(source, "eur_usd", None)
            if fn is None:
                continue
            try:
                rate = await fn()
            except Exception:  # noqa: BLE001 - try the next source
                continue
            rate = _decimal(rate)
            if rate and rate > 0:
                return rate
        return None
    if view is None and user_id is not None:
        try:
            view = await service.portfolio(user_id)
        except Exception:  # noqa: BLE001
            view = None
    for balance in (view or {}).get("balances") or []:
        if str(balance.get("token") or "").upper() == code:
            units, value = _decimal(balance.get("total")), _decimal(
                balance.get("usd_value")
            )
            if units and units > 0 and value is not None and value > 0:
                return value / units
    return None


def _decimal(value) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


async def add_manual(
    user_id: int,
    venue: str,
    *,
    amount,
    currency: str,
    ts=None,
    note: str = "",
    usd_value=None,
    service=None,
    now: float | None = None,
) -> dict:
    """The owner's entry. Priced at the rate now unless ``usd_value`` is given."""
    _venue(venue)
    now = time.time() if now is None else now
    stamp = parse_ts(ts, now)
    value = _amount(amount)
    if usd_value in (None, ""):
        usd = None
        service = service or native_venues.get(venue).service
        rate = await usd_rate(service, str(currency), user_id=user_id)
        if rate is None:
            raise ValueError(
                f"No USD price for {str(currency).upper()} is available; enter its USD value"
            )
        usd = float(abs(value) * rate)
    else:
        usd = usd_value
    row, _ = add_flow(
        user_id,
        venue,
        amount=value,
        currency=str(currency),
        ts=stamp,
        usd_value=usd,
        source="manual",
        status="confirmed",
        note=note,
        now=now,
    )
    return row


# ── history fetch ──


def _record_fetch(user_id, venue, now, ok, reason, count) -> None:
    with closing(_connect(user_id)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fetches (venue, ts, ok, reason, count)"
            " VALUES (?,?,?,?,?)",
            (venue, now, int(ok), reason[:300], count),
        )
        conn.commit()


async def sync(
    user_id: int,
    venue: str,
    service,
    *,
    view: Mapping | None = None,
    now: float | None = None,
    lookback_days: int | None = None,
    force: bool = False,
) -> dict:
    """Read the venue's transfer history and store what is new. Never raises.

    Throttled to once per ``FETCH_EVERY_SEC`` unless ``force``. The answer says what
    happened: ``{"venue", "ok", "reason", "fetched", "new", "skipped"}``; a failure is
    ``reason: "unavailable: ..."``.
    """
    now = time.time() if now is None else now
    out = {
        "venue": venue,
        "ok": False,
        "reason": "",
        "fetched": 0,
        "new": 0,
        "skipped": False,
    }
    try:
        _venue(venue)
        previous = last_fetch(user_id, venue)
        if not force and previous and now - previous["ts"] < FETCH_EVERY_SEC:
            out.update(skipped=True, ok=previous["ok"], reason=previous["reason"])
            return out
        if lookback_days is not None:
            days = max(1, min(int(lookback_days), MAX_LOOKBACK_DAYS))
            since = now - days * 86400
        elif previous and previous["ok"]:
            since = previous["ts"] - 2 * 86400
        else:
            since = now - DEFAULT_LOOKBACK_DAYS * 86400
        try:
            got = await fetchers.fetch(venue, service, user_id, since, now)
        except Exception as error:  # noqa: BLE001 - degrade, never raise
            reason = "unavailable: " + fetchers.reason_of(error)
            _record_fetch(user_id, venue, now, False, reason, 0)
            out["reason"] = reason
            return out
        rates: dict[str, Decimal | None] = {}
        # Adjacent request windows can both return a boundary row.
        unique = list({raw.external_id: raw for raw in got.flows}.values())
        for raw in unique:
            if raw.currency not in rates:
                rates[raw.currency] = await usd_rate(service, raw.currency, view)
            rate = rates[raw.currency]
            usd = float(raw.amount * rate) if rate is not None else None
            try:
                _, created = add_flow(
                    user_id,
                    venue,
                    amount=raw.amount,
                    currency=raw.currency,
                    ts=raw.ts,
                    usd_value=usd,
                    source="api",
                    status=raw.status,
                    note=raw.note,
                    external_id=raw.external_id,
                    now=now,
                )
            except ValueError as error:
                log.debug("Skipped a %s transfer row (%s)", venue, error)
                continue
            out["new"] += int(created)
        out["fetched"] = len(unique)
        if got.problems:
            out["reason"] = "unavailable: " + "; ".join(got.problems)
        out["ok"] = not got.problems
        _record_fetch(user_id, venue, now, out["ok"], out["reason"], out["fetched"])
    except Exception as error:  # noqa: BLE001 - the dashboard must never see this
        log.warning("%s flow sync failed (%s)", venue, type(error).__name__)
        out.update(ok=False, reason="unavailable: " + type(error).__name__)
    return out


# ── detection ──


def _tokens_match(token: str, symbol: str) -> bool:
    token, symbol = token.upper(), str(symbol).upper()
    # Kraken's xStock tokens are the symbol plus a trailing ``x``.
    return token == symbol or token == symbol + "X"


def expected_deltas(
    orders, quote: str, since: float, until: float
) -> dict[str, Decimal]:
    """Units each token moves because of the bot's own orders in ``(since, until]``."""
    out: dict[str, Decimal] = {}

    def add(token: str, units: Decimal) -> None:
        out[token] = out.get(token, Decimal(0)) + units

    for order in orders or []:
        try:
            when = float(order.get("time"))
        except (TypeError, ValueError):
            continue
        if not since < when <= until:
            continue
        quantity = _decimal(order.get("quantity")) or Decimal(0)
        value = _decimal(order.get("value")) or Decimal(0)
        fee = _decimal(order.get("fee")) or Decimal(0)
        fee_currency = str(order.get("fee_currency") or "").upper()
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").lower()
        if quantity <= 0 or not symbol or side not in ("buy", "sell"):
            continue
        fee_in_quote = fee_currency == quote.upper()
        fee_units = Decimal(0)
        if fee_currency and not fee_in_quote and _tokens_match(fee_currency, symbol):
            # Kraken states the fee in USD even when it is taken from the units.
            fee_units = (
                fee
                if fee_currency == symbol
                else fee * quantity / value if value else Decimal(0)
            )
        if side == "buy":
            add(quote.upper(), -(value + (fee if fee_in_quote else 0)))
            add(symbol, quantity - fee_units)
        else:
            add(quote.upper(), value - (fee if fee_in_quote else 0))
            add(symbol, -(quantity + fee_units))
    return out


def _balances(view: Mapping) -> dict[str, tuple[Decimal, float]]:
    out: dict[str, tuple[Decimal, float]] = {}
    for row in view.get("balances") or []:
        token = str(row.get("token") or "").upper()
        units = _decimal(row.get("total"))
        if not token or units is None:
            continue
        previous = out.get(token, (Decimal(0), 0.0))
        out[token] = (
            previous[0] + units,
            previous[1] + float(row.get("usd_value") or 0),
        )
    return out


def detect(
    user_id: int,
    venue: str,
    view: Mapping,
    ledger: Mapping | None,
    *,
    now: float | None = None,
) -> list[dict]:
    """Compare the account with the last reading and record unexplained moves as pending.

    A token's balance change since the last reading, minus what the bot's own orders
    (``ledger['orders']``) and recorded flows explain, is a candidate when it is above
    ``max(0.5 USD, 2% of the token's value at the last reading)``. Candidates that
    offset each other (a conversion between two of the account's own currencies, such
    as EUR to USD) are not flows and are dropped. The first reading only sets the
    reference. Nothing is subtracted from PnL until the owner confirms a row.
    Returns the rows it created.
    """
    _venue(venue)
    now = time.time() if now is None else now
    ledger = ledger or {}
    if ledger.get("uncertain_order"):
        # An order of unknown outcome may have moved balances; judge the next reading.
        return []
    current = _balances(view)
    stored = {t: [str(u), usd] for t, (u, usd) in current.items()}
    created: list[dict] = []

    with closing(_connect(user_id)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        mark = conn.execute("SELECT * FROM marks WHERE venue = ?", (venue,)).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO marks (venue, ts, balances) VALUES (?,?,?)",
            (venue, now, json.dumps(stored)),
        )
        if mark is None or now <= mark["ts"]:
            conn.commit()
            return []
        previous = {
            t: (Decimal(v[0]), float(v[1]))
            for t, v in json.loads(mark["balances"]).items()
        }
        since = float(mark["ts"])
        expected = expected_deltas(
            ledger.get("orders"), str(ledger.get("quote") or "USD"), since, now
        )
        explained: dict[str, Decimal] = {}
        for row in conn.execute(
            "SELECT currency, amount FROM flows WHERE venue = ? AND status = 'confirmed'"
            " AND source IN ('api','manual') AND created > ? AND ts > ? AND ts <= ?",
            (venue, since, since, now + MATCH_SLACK_SEC),
        ).fetchall():
            explained[row["currency"]] = explained.get(
                row["currency"], Decimal(0)
            ) + Decimal(row["amount"])
        candidates = []
        for token in sorted(set(current) | set(previous)):
            now_units, now_usd = current.get(token, (Decimal(0), 0.0))
            was_units, was_usd = previous.get(token, (Decimal(0), 0.0))
            expect = Decimal(0)
            for name, units in expected.items():
                if _tokens_match(token, name):
                    expect += units
            delta = now_units - was_units - expect - explained.get(token, Decimal(0))
            if delta == 0:
                continue
            if now_units > 0 and now_usd > 0:
                price: float | None = now_usd / float(now_units)
            elif was_units > 0 and was_usd > 0:
                price = was_usd / float(was_units)
            else:
                price = None
            usd = float(delta) * price if price is not None else None
            threshold = max(DETECT_MIN_USD, DETECT_FRACTION * was_usd)
            if usd is None:
                # Unpriced and moved: still worth the owner's eye, never subtracted.
                candidates.append((token, delta, None))
            elif abs(usd) > threshold:
                candidates.append((token, delta, usd))
        priced = [c for c in candidates if c[2] is not None]
        if (
            len(priced) >= 2
            and any(c[2] > 0 for c in priced)
            and any(c[2] < 0 for c in priced)
        ):
            total_was = sum(u for _, u in previous.values())
            if abs(sum(c[2] for c in priced)) <= max(
                DETECT_MIN_USD, DETECT_FRACTION * total_was
            ):
                candidates = [c for c in candidates if c[2] is None]
        for token, delta, usd in candidates:
            word = "rose" if delta > 0 else "fell"
            flow_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO flows (id, venue, ts, currency, amount, usd_value, source,"
                " status, note, external_id, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    flow_id,
                    venue,
                    now,
                    token,
                    _fmt(delta),
                    usd,
                    "detected",
                    "pending",
                    f"{token} balance {word} with no matching bot order",
                    None,
                    now,
                ),
            )
            created.append(
                _row(
                    conn.execute(
                        "SELECT * FROM flows WHERE id = ?", (flow_id,)
                    ).fetchone()
                )
            )
        conn.commit()
    return created


async def observe(
    user_id: int,
    venue: str,
    service,
    view: Mapping,
    *,
    fetch: bool = True,
    now: float | None = None,
) -> None:
    """One venue reading's worth of flow upkeep: history fetch (throttled), then detection.

    Called by the five-minute recorder (``fetch=True``) and the dashboard read
    (``fetch=False``: detection from the reading it already has, no private call).
    Never raises.
    """
    now = time.time() if now is None else now
    try:
        if fetch:
            await sync(user_id, venue, service, view=view, now=now)
        try:
            ledger = service.status(user_id).get("ledger")
        except Exception:  # noqa: BLE001
            ledger = None
        detect(user_id, venue, view, ledger, now=now)
    except Exception as error:  # noqa: BLE001 - flow upkeep must never break a read
        log.warning("%s flow upkeep skipped (%s)", venue, type(error).__name__)
