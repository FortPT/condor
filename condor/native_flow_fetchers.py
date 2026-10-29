"""Read each native venue's own deposit and withdrawal history (best effort).

One fetcher per venue, all read-only and all degrading the same way: a failure is
a :class:`FetchUnavailable` with a short reason, never an exception into the
dashboard. The parsers are pure functions of the venue's answer so the tests can
feed them fabricated fixtures built from each venue's documentation.

**Verification status (read docs/native-flows.md before trusting any of this).**
None of these shapes has been exercised against a live account yet:

* Crypto.com Exchange ``private/get-deposit-history`` / ``get-withdrawal-history``:
  param and field names (``start_ts``/``end_ts``/``page_size`` in milliseconds,
  ``result.deposit_list`` / ``withdrawal_list``, numeric status codes) come from a
  third-party client (ccxt) because the official reference page could not be
  fetched as text. The status code meanings are the ones ccxt maps.
* Kraken ``/0/private/Ledgers`` with ``type=deposit`` / ``type=withdrawal``: the
  endpoint is long established; the key needs only ``Data - Query ledger entries``.
* Trading 212 ``GET /api/v0/equity/history/transactions``: the page lists the item
  fields (``type`` DEPOSIT / WITHDRAW / FEE / TRANSFER / interest, ``amount``,
  ``currency``, ``dateTime``, ``reference``) and cursor pagination (``nextPagePath``),
  but not the SIGN of a withdrawal's amount, nor whether a card deposit's fee is a
  separate row; both are handled defensively (the sign follows the type). Needs the
  ``history:transactions`` scope; a 403 degrades to "unavailable".
* Revolut X ``GET /1.0/transactions``: listed in the OpenAPI spec with types
  ``receive`` / ``send``, but the spec does not say whether a fiat top-up from the
  owner's Revolut account appears there. The ``revolut`` and ``external_bank_account``
  account types exist, so it probably does. Treat an empty answer as "nothing
  found", not as proof that nothing was moved: detection and manual entry remain.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

DAY = 86400
#: Revolut X refuses a transactions range longer than 30 days.
REVOLUT_WINDOW_SEC = 29 * DAY
MAX_PAGES = 20


class FetchUnavailable(Exception):
    """The history could not be read; the message is a short, plain reason."""


@dataclass(frozen=True)
class RawFlow:
    """One transfer as a venue reports it. ``amount`` is signed (+ in, - out)."""

    external_id: str
    ts: float
    currency: str
    amount: Decimal
    #: ``confirmed`` (credited or debited) or ``pending`` (not final yet).
    status: str
    note: str = ""


@dataclass
class Fetched:
    flows: list[RawFlow]
    #: Parts that could not be read, as ``"<part>: <reason>"``; empty = complete.
    problems: list[str]


def _decimal(value) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def reason_of(error: BaseException) -> str:
    """A bounded reason. Service errors are already sanitised, others show their type."""
    if isinstance(error, FetchUnavailable):
        return str(error)[:160]
    if isinstance(error, ValueError):
        # The services raise ValueError subclasses with bounded, key-free text.
        return str(error).replace("\n", " ")[:160]
    return type(error).__name__


# ── Crypto.com Exchange ──

CRYPTO_COM_DEPOSIT_STATUS = {"0": "pending", "1": "confirmed", "3": "pending"}
CRYPTO_COM_WITHDRAWAL_STATUS = {
    "0": "pending",
    "1": "pending",
    "3": "pending",
    "5": "confirmed",
}


def _ms_to_s(value) -> float | None:
    number = _decimal(value)
    if number is None or number <= 0:
        return None
    return float(number) / 1000.0


def parse_crypto_com(result: Any, deposit: bool) -> list[RawFlow]:
    """Rows of ``deposit_list`` / ``withdrawal_list``. A failed or cancelled row is dropped."""
    key = "deposit_list" if deposit else "withdrawal_list"
    rows = result.get(key) if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise FetchUnavailable(f"unexpected response (no {key})")
    statuses = CRYPTO_COM_DEPOSIT_STATUS if deposit else CRYPTO_COM_WITHDRAWAL_STATUS
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = statuses.get(str(row.get("status")))
        amount = _decimal(row.get("amount"))
        currency = str(row.get("currency") or "").upper()
        ident = row.get("id")
        ts = _ms_to_s(row.get("update_time")) or _ms_to_s(row.get("create_time"))
        if (
            not status
            or not amount
            or amount <= 0
            or not currency
            or not ident
            or not ts
        ):
            continue
        kind = "deposit" if deposit else "withdrawal"
        found.append(
            RawFlow(
                external_id=f"{kind}:{ident}",
                ts=ts,
                currency=currency,
                amount=amount if deposit else -amount,
                status=status,
                note=f"Crypto.com Exchange {kind}"
                + (" (fee not included)" if not deposit else ""),
            )
        )
    return found


async def fetch_crypto_com(service, user_id: int, since: float, now: float) -> Fetched:
    out = Fetched([], [])
    for method, deposit in (
        ("get-deposit-history", True),
        ("get-withdrawal-history", False),
    ):
        part = "deposits" if deposit else "withdrawals"
        try:
            result = await service.private(
                user_id,
                method,
                {
                    "start_ts": int(since * 1000),
                    "end_ts": int(now * 1000) + 1000,
                    "page_size": 200,
                },
            )
            out.flows += parse_crypto_com(result, deposit)
        except Exception as error:  # noqa: BLE001 - one part never hides the other
            out.problems.append(f"{part}: {reason_of(error)}")
    return out


# ── Kraken ──

_KRAKEN_ASSETS = {"ZUSD": "USD", "ZEUR": "EUR", "XXBT": "BTC", "XBT": "BTC"}


def kraken_asset(asset: str) -> str:
    """Kraken's ledger asset code as a plain ticker (``ZEUR`` -> ``EUR``)."""
    asset = str(asset or "").upper()
    if asset in _KRAKEN_ASSETS:
        return _KRAKEN_ASSETS[asset]
    if len(asset) == 4 and asset[0] in "XZ":
        return _KRAKEN_ASSETS.get(asset[1:], asset[1:])
    return asset


def parse_kraken_ledger(result: Any) -> list[RawFlow]:
    """Deposit and withdrawal entries of a ``Ledgers`` answer; the net move is amount - fee."""
    ledger = result.get("ledger") if isinstance(result, dict) else None
    if not isinstance(ledger, dict):
        raise FetchUnavailable("unexpected response (no ledger)")
    found = []
    for ident, row in ledger.items():
        if not isinstance(row, dict) or row.get("type") not in (
            "deposit",
            "withdrawal",
        ):
            continue
        amount = _decimal(row.get("amount"))
        fee = _decimal(row.get("fee")) or Decimal(0)
        ts = _decimal(row.get("time"))
        currency = kraken_asset(row.get("asset"))
        if amount is None or ts is None or ts <= 0 or not currency:
            continue
        net = amount - fee
        if net == 0:
            continue
        found.append(
            RawFlow(
                external_id=f"ledger:{ident}",
                ts=float(ts),
                currency=currency,
                amount=net,
                status="confirmed",
                note=f"Kraken ledger {row['type']}",
            )
        )
    return found


async def fetch_kraken(service, user_id: int, since: float, now: float) -> Fetched:
    out = Fetched([], [])
    for kind in ("deposit", "withdrawal"):
        try:
            seen, offset = 0, 0
            for _ in range(MAX_PAGES):
                result = await service.private(
                    user_id,
                    "Ledgers",
                    {"type": kind, "start": int(since), "ofs": offset},
                )
                out.flows += parse_kraken_ledger(result)
                page = (
                    len(result.get("ledger") or {}) if isinstance(result, dict) else 0
                )
                seen += page
                total = (
                    _decimal(result.get("count")) if isinstance(result, dict) else None
                )
                offset += page
                if not page or total is None or seen >= total:
                    break
        except Exception as error:  # noqa: BLE001
            out.problems.append(f"{kind}s: {reason_of(error)}")
    return out


# ── Revolut X ──

REVOLUT_STATUS = {"completed": "confirmed", "pending": "pending"}


def parse_revolut_transactions(data: Any) -> list[RawFlow]:
    """``receive`` and ``send`` transactions (the spec's transfer types); trades are ignored."""
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise FetchUnavailable("unexpected response (no data list)")
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        status = REVOLUT_STATUS.get(str(row.get("status")))
        if kind not in ("receive", "send") or not status:
            continue
        leg = row.get("destination" if kind == "receive" else "source")
        other = row.get("source" if kind == "receive" else "destination")
        if not isinstance(leg, dict):
            continue
        amount = _decimal(leg.get("amount"))
        currency = str(leg.get("currency") or "").upper()
        ts = _ms_to_s(row.get("processed_date")) or _ms_to_s(row.get("created_date"))
        ident = row.get("id")
        if not amount or amount <= 0 or not currency or not ts or not ident:
            continue
        account = (other or {}).get("account") if isinstance(other, dict) else None
        peer = account.get("type") if isinstance(account, dict) else None
        direction = "from" if kind == "receive" else "to"
        found.append(
            RawFlow(
                external_id=f"transaction:{ident}",
                ts=ts,
                currency=currency,
                amount=amount if kind == "receive" else -amount,
                status=status,
                note=f"Revolut X {kind}" + (f" {direction} {peer}" if peer else ""),
            )
        )
    return found


async def fetch_revolut(service, user_id: int, since: float, now: float) -> Fetched:
    out = Fetched([], [])
    try:
        start = since
        while start < now:
            end = min(start + REVOLUT_WINDOW_SEC, now)
            query = {
                "start_date": int(start * 1000),
                "end_date": int(end * 1000),
                "types": "receive,send",
                "limit": 1900,
            }
            for _ in range(MAX_PAGES):
                data = await service.private(
                    user_id, "GET", "/1.0/transactions", dict(query)
                )
                out.flows += parse_revolut_transactions(data)
                meta = data.get("metadata") if isinstance(data, dict) else None
                cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
                if not cursor:
                    break
                query["cursor"] = cursor
            start = end
    except Exception as error:  # noqa: BLE001
        out.problems.append(f"transactions: {reason_of(error)}")
    return out


# ── Trading 212 ──


def _iso_to_s(value) -> float | None:
    """Epoch seconds of an ISO 8601 timestamp (``Z`` or an offset), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def parse_trading212_transactions(data: Any) -> list[RawFlow]:
    """``DEPOSIT`` and ``WITHDRAW`` rows; fees, interest and transfers are not external flows.

    The sign comes from the type, whatever sign the amount carries.
    """
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise FetchUnavailable("unexpected response (no items list)")
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        amount = _decimal(row.get("amount"))
        currency = str(row.get("currency") or "").upper()
        ts = _iso_to_s(row.get("dateTime"))
        ident = row.get("reference")
        if (
            kind not in ("DEPOSIT", "WITHDRAW")
            or not amount
            or not currency
            or ts is None
            or not ident
        ):
            continue
        found.append(
            RawFlow(
                external_id=f"transaction:{ident}",
                ts=ts,
                currency=currency,
                amount=abs(amount) if kind == "DEPOSIT" else -abs(amount),
                status="confirmed",
                note=f"Trading 212 {kind.lower()}",
            )
        )
    return found


async def fetch_trading212(service, user_id: int, since: float, now: float) -> Fetched:
    out = Fetched([], [])
    try:
        start = datetime.fromtimestamp(since, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        path, query = "/equity/history/transactions", {"limit": 50, "time": start}
        for _ in range(MAX_PAGES):
            data = await service.private(user_id, "GET", path, query)
            out.flows += parse_trading212_transactions(data)
            more = data.get("nextPagePath") if isinstance(data, dict) else None
            if not more or not data.get("items"):
                break
            if not str(more).startswith("/api/v0/equity/history/transactions"):
                raise FetchUnavailable("unexpected next page path")
            path, query = more, None
        else:
            out.problems.append("transactions: more pages than were read")
    except Exception as error:  # noqa: BLE001
        out.problems.append(f"transactions: {reason_of(error)}")
    return out


FETCHERS = {
    "crypto_com_exchange": fetch_crypto_com,
    "kraken_xstocks": fetch_kraken,
    "revolut_x": fetch_revolut,
    "trading212": fetch_trading212,
}


async def fetch(
    venue_key: str, service, user_id: int, since: float, now: float | None = None
) -> Fetched:
    """The venue's transfers since ``since`` (unix seconds). Never raises past this call."""
    now = time.time() if now is None else now
    fetcher = FETCHERS.get(venue_key)
    if fetcher is None:
        raise FetchUnavailable("no transfer history is available for this venue")
    if not hasattr(service, "private"):
        raise FetchUnavailable("this venue's service cannot read private history")
    return await fetcher(service, user_id, since, now)
