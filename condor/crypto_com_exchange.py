"""Crypto.com spot API (v1); account keys never enter agent tool arguments.

Orders go to the Exchange order book, so a buy followed by an immediate sell costs
the spread plus the fee (a few hundredths of a percent on liquid coins), not the
several percent the Crypto.com App's quotation prices carry.

The ledger owns only this bot's allocation, in the account's quote currency
(USD by default; USDT, USDC or EUR when the account holds those). It is seeded
from the Exchange account by :meth:`ExchangeService.sync_from_account`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections import defaultdict, deque
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

from condor import crypto_com_scorecard as scorecard
from condor.fsutil import atomic_write_json
from condor.paths import data_dir

BASE_URLS = {
    "production": "https://api.crypto.com/exchange/v1/",
    "uat": "https://uat-api.3ona.co/exchange/v1/",
}
QUOTE_CURRENCIES = ("USD", "USDT", "USDC", "EUR")
SIDES = {"buy": "BUY", "sell": "SELL"}
FINAL_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
MAX_PARAM_LEVEL = 3
_UNCERTAIN = (
    "Order outcome needs reconciliation. Trading paused; check the Crypto.com order "
    "history. Do not resubmit."
)
_UNCERTAIN_MAKER = (
    _UNCERTAIN
    + " A maker order may still be resting on the book: reconcile cancels it."
)

# Below this total bot equity (quote units) the bot may use all of its money
# freely; only the equity floor and the single-asset cap apply. At or above it
# the reserve, position-count and one-sixth rules apply as well.
GUARD_THRESHOLD = Decimal("1000")
MAX_ASSET_FRACTION = Decimal("0.4")
FLOOR = Decimal("2")
TOP_COINS = 40
# Orders are immediate-or-cancel limits priced this far through the touch, so a
# fast market cannot fill them at a worse price than the caller accepted.
DEFAULT_SLIPPAGE_PCT = Decimal("0.3")
MAX_SLIPPAGE_PCT = Decimal("2")
MAX_SPREAD_PCT = Decimal("1")
ORDER_POLLS = 10
ORDER_POLL_WAIT_SEC = 1.0
RATE_LIMIT_PER_MINUTE = 90

# Maker mode (optional): a post-only limit order rests at the touch for a bounded
# time inside ONE call, is then cancelled, and reports what filled. Nothing here
# has been run against the live Exchange; see docs/crypto-com-exchange.md.
MODES = ("taker", "maker")
PURPOSES = ("entry", "target_exit", "stop_exit")
MAKER_WAIT_DEFAULT_SEC = 60
MAKER_WAIT_MIN_SEC = 5
# The MCP tool budget for an order or reconcile is 90 s; the wait, the cancel and
# the final read all have to fit inside it.
MAKER_WAIT_MAX_SEC = 75
MAKER_CALL_BUDGET_SEC = 72
MAKER_POLL_SEC = 2.5
MAKER_POLL_FAILURES = 3
MAKER_CANCEL_ATTEMPTS = 2
MAKER_FEE_DEFAULT_PCT = Decimal("0.25")
TAKER_FEE_DEFAULT_PCT = Decimal("0.5")
MAKER_LOG_LIMIT = 1000
# Documented Exchange reason codes: POST_ONLY_REJ (create refused because the
# post-only order was rejected) and NO_ACTIVE_ORDER (cancel of an order that is
# no longer open: it already filled, expired or was cancelled).
POST_ONLY_REJECTED_CODE = 43005
NO_ACTIVE_ORDER_CODE = 316
TIMEFRAMES = {
    "1m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "12h",
    "1D",
    "7D",
    "14D",
    "1M",
}


def amount(value) -> Decimal:
    text = value if isinstance(value, str) else str(value)
    if not re.fullmatch(r"\d{1,18}(?:\.\d{1,18})?", text):
        raise ValueError(
            "Use a positive decimal amount, without exponent or separators"
        )
    result = Decimal(text)
    if result <= 0:
        raise ValueError("Amount must be positive")
    return result


def symbol_of(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{1,24}", value):
        raise ValueError("Invalid coin symbol")
    return value.upper()


def fmt(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def params_string(obj, level=0) -> str:
    """The flattened parameter string the Exchange signs (official algorithm)."""
    if level >= MAX_PARAM_LEVEL:
        return str(obj)
    out = ""
    for key in sorted(obj):
        out += key
        value = obj[key]
        if value is None:
            out += "null"
        elif isinstance(value, list):
            for item in value:
                out += (
                    params_string(item, level + 1)
                    if isinstance(item, dict)
                    else str(item)
                )
        elif isinstance(value, dict):
            out += params_string(value, level + 1)
        else:
            out += str(value)
    return out


def signed_body(api_key, secret, method, params, nonce=None, request_id=None):
    nonce = int(nonce if nonce is not None else time.time() * 1000)
    request_id = int(request_id if request_id is not None else nonce)
    payload = method + str(request_id) + api_key + params_string(params) + str(nonce)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {
        "id": request_id,
        "method": method,
        "api_key": api_key,
        "params": params,
        "nonce": nonce,
        "sig": signature,
    }


def _root(user_id):
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid account owner")
    return data_dir() / "crypto_com_exchange" / str(user_id)


def credentials_path(user_id):
    return _root(user_id) / "credentials.json"


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, content)
    os.chmod(path, 0o600)


def new_ledger(quote="USD"):
    return {
        "quote": quote,
        "cash": "0",
        "positions": {},
        "uncertain_order": None,
        "enabled": False,
        "orders": [],
        "floor": str(FLOOR),
        "synced": False,
    }


def _read_ledger(user_id):
    path = _root(user_id) / "ledger.json"
    return json.loads(path.read_text()) if path.exists() else new_ledger()


def guards_active(equity):
    return equity >= GUARD_THRESHOLD


def check_purchase(ledger, symbol, spend, equity):
    if equity <= FLOOR:
        raise ValueError(f"Bot equity reached the {FLOOR} stop threshold")
    positions = ledger["positions"]
    held = Decimal(positions.get(symbol, {}).get("cost", "0"))
    if not guards_active(equity):
        if held + spend > equity * MAX_ASSET_FRACTION:
            raise ValueError("Asset would exceed 40% of current bot equity")
        return
    if symbol in positions:
        raise ValueError(
            "Close the existing position before opening another in this asset"
        )
    if len(positions) >= 3:
        raise ValueError("Maximum three open positions")
    if spend > equity / 6:
        raise ValueError("Position would exceed one sixth of current bot equity")
    if Decimal(ledger["cash"]) - spend < equity / 2:
        raise ValueError("Keep half of current bot equity as cash reserve")


def _decimal(value, default=None):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return default
    return result if result.is_finite() else default


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    down = _round_down(value, step)
    return down if down == value else down + step


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def _rates(group):
    count = len(group)
    if not count:
        return {"resolved": 0, "fill_rate": None, "any_fill_rate": None}
    full = sum(1 for r in group if r.get("outcome") == "filled")
    some = sum(1 for r in group if r.get("outcome") in ("filled", "partial"))
    return {
        "resolved": count,
        "fill_rate": round(full / count, 4),
        "any_fill_rate": round(some / count, 4),
    }


def maker_stats(rows):
    """Aggregate the maker attempt log; read-only and recomputed on every call."""
    # An attempt logged as uncertain is replaced by its reconciled row.
    settled = {
        r.get("client_oid")
        for r in rows
        if r.get("outcome") != "uncertain" and r.get("client_oid")
    }
    rows = [
        r
        for r in rows
        if not (r.get("outcome") == "uncertain" and r.get("client_oid") in settled)
    ]
    outcomes = {
        name: sum(1 for r in rows if r.get("outcome") == name)
        for name in ("filled", "partial", "unfilled", "rejected", "uncertain")
    }
    resolved = [
        r for r in rows if r.get("outcome") in ("filled", "partial", "unfilled")
    ]
    value = fee = saved = unfilled = fee_value = 0.0
    fee_known = priced = 0
    for row in resolved:
        notional = _number(row.get("value"))
        fee_quote = _number(row.get("fee_quote"))
        taker = _number(row.get("taker_fee_pct"))
        if notional and notional > 0:
            value += notional
            if fee_quote is not None:
                fee += fee_quote
                fee_value += notional
                fee_known += 1
                if taker is not None:
                    saved += notional * taker / 100 - fee_quote
                    priced += 1
        if row.get("outcome") == "unfilled":
            price = _number(row.get("resting_price"))
            quantity = _number(row.get("ordered_quantity"))
            if price and quantity:
                unfilled += price * quantity
    purposes = {}
    for purpose in sorted({r.get("purpose") for r in rows if r.get("purpose")}):
        purposes[purpose] = {
            "attempts": sum(1 for r in rows if r.get("purpose") == purpose),
            **_rates([r for r in resolved if r.get("purpose") == purpose]),
        }
    recent_keys = (
        "time",
        "pair",
        "side",
        "purpose",
        "resting_price",
        "outcome",
        "filled_fraction",
        "fee_quote",
        "waited_sec",
        "time_to_fill_sec",
    )
    return {
        "attempts": len(rows),
        "outcomes": outcomes,
        **_rates(resolved),
        "avg_filled_fraction": _mean(
            _number(r.get("filled_fraction")) for r in resolved
        ),
        "avg_seconds_to_fill": _mean(
            _number(r.get("time_to_fill_sec"))
            for r in resolved
            if r.get("outcome") == "filled"
        ),
        "filled_notional": round(value, 4),
        "avg_fee_pct": round(fee / fee_value * 100, 4) if fee_value else None,
        "fee_paid": round(fee, 6) if fee_known else None,
        "fee_saved_vs_taker": round(saved, 6) if priced else None,
        "unfilled_notional": round(unfilled, 4),
        "quote": rows[-1].get("quote") if rows else None,
        "first_attempt": rows[0].get("time") if rows else None,
        "last_attempt": rows[-1].get("time") if rows else None,
        "by_purpose": purposes,
        "recent": [{k: r.get(k) for k in recent_keys} for r in rows[-10:]][::-1],
        "note": (
            "fill_rate counts orders that filled completely among those that were "
            "placed and resolved (rejected and uncertain attempts are listed apart). "
            "fee_saved_vs_taker is the taker fee the filled value would have paid, "
            "minus the fee actually reported; it ignores the price moves missed by "
            "unfilled orders, so compare the unfilled entries with later prices "
            "before deciding whether maker mode pays."
        ),
    }


def _maker_settings_path(user_id):
    return _root(user_id) / "maker_settings.json"


def _maker_log_path(user_id):
    return _root(user_id) / "maker_log.json"


def _read_maker_settings(user_id):
    try:
        data = json.loads(_maker_settings_path(user_id).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    overrides = data.get("maker_fee_overrides")
    return {
        "maker_enabled": data.get("maker_enabled") is True,
        "maker_fee_overrides": overrides if isinstance(overrides, dict) else {},
    }


def _read_maker_log(user_id):
    try:
        data = json.loads(_maker_log_path(user_id).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    rows = data.get("attempts") if isinstance(data, dict) else None
    return [row for row in rows if isinstance(row, dict)] if rows else []


def _mode_args(side, mode, purpose, maker_wait_sec):
    """Validate the optional maker arguments; the taker path keeps its old inputs."""
    mode = "taker" if mode in (None, "") else str(mode).strip().lower()
    if mode not in MODES:
        raise ValueError("mode must be taker or maker")
    if purpose in (None, ""):
        purpose = None
    elif purpose not in PURPOSES:
        raise ValueError("purpose must be entry, target_exit or stop_exit")
    given = maker_wait_sec not in (None, "")
    if mode == "taker":
        if given:
            raise ValueError("maker_wait_sec applies to maker mode only")
        return "taker", purpose, None
    if purpose == "stop_exit":
        raise ValueError(
            "Maker mode is refused for a stop or invalidation exit: use taker mode "
            "so the exit completes"
        )
    if purpose is None:
        raise ValueError("Maker mode needs purpose entry or target_exit")
    side = side.lower() if isinstance(side, str) else ""
    if side not in SIDES:
        raise ValueError("Use side buy or sell")
    if (purpose == "entry") != (side == "buy"):
        raise ValueError("Maker purpose entry is a buy; target_exit is a sell")
    wait = MAKER_WAIT_DEFAULT_SEC
    if given:
        text = str(maker_wait_sec).strip()
        if not re.fullmatch(r"\d{1,3}", text) or not (
            MAKER_WAIT_MIN_SEC <= int(text) <= MAKER_WAIT_MAX_SEC
        ):
            raise ValueError(
                f"maker_wait_sec must be a whole number from {MAKER_WAIT_MIN_SEC} "
                f"to {MAKER_WAIT_MAX_SEC}"
            )
        wait = int(text)
    return "maker", purpose, wait


class _MakerUnresolved(Exception):
    """The maker order's final state could not be confirmed: reconcile decides."""


class ExchangeService:
    def __init__(self):
        self.locks = defaultdict(asyncio.Lock)
        self.calls = defaultdict(deque)
        self._instruments = (0.0, {})
        self._fees = {}

    # ── credentials and switches ──

    def _credential_file(self, user_id):
        try:
            return json.loads(credentials_path(user_id).read_text())
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            raise ValueError(
                "Connect a Crypto.com API key in Settings → Crypto.com"
            ) from None

    def credentials(self, user_id):
        data = self._credential_file(user_id)
        try:
            return data["api_key"], data["api_secret"]
        except KeyError:
            raise ValueError(
                "Connect a Crypto.com API key in Settings → Crypto.com"
            ) from None

    def environment(self, user_id):
        try:
            env = self._credential_file(user_id).get("environment", "production")
        except ValueError:
            return "production"
        return env if env in BASE_URLS else "production"

    def status(self, user_id):
        ledger = _read_ledger(user_id)
        return {
            "connected": credentials_path(user_id).exists(),
            "environment": self.environment(user_id),
            "enabled": ledger["enabled"] and not ledger["uncertain_order"],
            "ledger": ledger,
            "limits": {
                "floor": str(FLOOR),
                "reserve_fraction": "0.5",
                "position_fraction": "1/6",
                "max_positions": 3,
                "guards_apply_from": str(GUARD_THRESHOLD),
                "max_asset_fraction_below_threshold": str(MAX_ASSET_FRACTION),
                "default_slippage_pct": str(DEFAULT_SLIPPAGE_PCT),
            },
        }

    def save_credentials(self, user_id, key, secret, environment="production"):
        for value in (key, secret):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 4096
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Invalid API key or secret")
        if environment not in BASE_URLS:
            raise ValueError("Environment must be production or uat")
        self.set_enabled(user_id, False)
        _write(
            credentials_path(user_id),
            {
                "api_key": key.strip(),
                "api_secret": secret.strip(),
                "environment": environment,
            },
        )

    def set_enabled(self, user_id, enabled):
        ledger = _read_ledger(user_id)
        if enabled:
            self.credentials(user_id)
            if ledger["uncertain_order"]:
                raise ValueError(
                    "An order needs reconciliation before trading can resume"
                )
            if not ledger.get("synced"):
                raise ValueError(
                    "Sync the bot allocation from the Crypto.com account first"
                )
        ledger["enabled"] = bool(enabled)
        _write(_root(user_id) / "ledger.json", ledger)
        return self.status(user_id)

    def disconnect(self, user_id):
        self.set_enabled(user_id, False)
        credentials_path(user_id).unlink(missing_ok=True)
        return self.status(user_id)

    # ── HTTP ──

    async def _send(self, session, method, url, **kwargs):
        async with session.request(method, url, allow_redirects=False, **kwargs) as r:
            chunks, size = [], 0
            async for chunk in r.content.iter_chunked(65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise ValueError("Crypto.com response too large")
                chunks.append(chunk)
            return r.status, json.loads(b"".join(chunks))

    @staticmethod
    def _result(status, data, secret_values=()):
        if status == 200 and isinstance(data, dict) and data.get("code") == 0:
            return data.get("result") or {}
        code = data.get("code") if isinstance(data, dict) else None
        message = data.get("message") if isinstance(data, dict) else None
        text = message if isinstance(message, str) else ""
        # Error text is untrusted and can echo credentials; keep it short and plain.
        text = re.sub(r"[^A-Za-z0-9 _.,:;()/-]", "", text)[:80]
        if any(v and v in text for v in secret_values):
            text = ""
        raise ExchangeError(status, code, text)

    async def public(self, user_id, path, params=None):
        import aiohttp

        base = BASE_URLS[self.environment(user_id)]
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                status, data = await self._send(
                    session, "GET", base + "public/" + path, params=params or {}
                )
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
            raise ValueError("Crypto.com unavailable") from None
        return self._result(status, data)

    async def private(self, user_id, method, params=None):
        import aiohttp

        key, secret = self.credentials(user_id)
        now = time.monotonic()
        calls = self.calls[user_id]
        while calls and now - calls[0] >= 60:
            calls.popleft()
        if len(calls) >= RATE_LIMIT_PER_MINUTE:
            raise ValueError("API rate limit: wait 60 seconds")
        calls.append(now)
        body = signed_body(key, secret, "private/" + method, params or {})
        base = BASE_URLS[self.environment(user_id)]
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                status, data = await self._send(
                    session,
                    "POST",
                    base + "private/" + method,
                    json=body,
                    headers={"User-Agent": "Condor-CryptoComExchange/1.0"},
                )
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
            raise ValueError(
                "Crypto.com unavailable or returned an invalid response; "
                "order requests are never retried"
            ) from None
        return self._result(status, data, (key, secret))

    # ── market data ──

    async def instruments(self, user_id):
        stamp, cached = self._instruments
        if cached and time.monotonic() - stamp < 600:
            return cached
        result = await self.public(user_id, "get-instruments")
        spot = {}
        for row in result.get("data", []):
            if row.get("inst_type") == "CCY_PAIR" and row.get("tradable"):
                spot[row["symbol"]] = row
        self._instruments = (time.monotonic(), spot)
        return spot

    async def _tickers(self, user_id):
        result = await self.public(user_id, "get-tickers")
        return {row["i"]: row for row in result.get("data", []) if "i" in row}

    @staticmethod
    def _pair(ledger, symbol):
        return f"{symbol}_{ledger['quote']}"

    async def coins(self, user_id, keyword=""):
        if not isinstance(keyword, str) or len(keyword) > 100:
            raise ValueError("Enter a coin symbol or name")
        ledger = _read_ledger(user_id)
        quote = ledger["quote"]
        instruments = await self.instruments(user_id)
        tickers = await self._tickers(user_id)
        rows = []
        for pair, inst in instruments.items():
            if inst.get("quote_ccy") != quote or pair not in tickers:
                continue
            t = tickers[pair]
            bid, ask, last = (_decimal(t.get(k)) for k in ("b", "k", "a"))
            volume = _decimal(t.get("vv"), Decimal(0))
            if not bid or not ask or not last or last <= 0:
                continue
            change = _decimal(t.get("c"))
            rows.append(
                {
                    "symbol": inst["base_ccy"],
                    "pair": pair,
                    "price": fmt(last),
                    "bid": fmt(bid),
                    "ask": fmt(ask),
                    "spread_pct": str(round((ask - bid) / ask * 100, 3)),
                    "change_24h_pct": (
                        str(round(change * 100, 2)) if change is not None else None
                    ),
                    "volume_24h": str(round(volume, 2)),
                    "min_quantity_step": inst.get("qty_tick_size"),
                }
            )
        rows.sort(key=lambda r: Decimal(r["volume_24h"]), reverse=True)
        key = keyword.strip().upper()
        if key:
            rows = [r for r in rows if key in r["symbol"]][:10]
        else:
            rows = rows[:TOP_COINS]
            held = set(ledger["positions"])
            listed = {r["symbol"] for r in rows}
            for symbol in sorted(held - listed):
                extra = next(
                    (r for r in self._all_rows(instruments, tickers, quote, symbol)),
                    None,
                )
                if extra:
                    rows.append(extra)
        self._record_prices(user_id, rows, quote)
        return {"quote": quote, "coins": rows}

    @staticmethod
    def _all_rows(instruments, tickers, quote, symbol):
        pair = f"{symbol}_{quote}"
        t = tickers.get(pair)
        inst = instruments.get(pair)
        if not t or not inst:
            return []
        bid, ask, last = (_decimal(t.get(k)) for k in ("b", "k", "a"))
        if not bid or not ask or not last:
            return []
        return [
            {
                "symbol": symbol,
                "pair": pair,
                "price": fmt(last),
                "bid": fmt(bid),
                "ask": fmt(ask),
                "spread_pct": str(round((ask - bid) / ask * 100, 3)),
                "change_24h_pct": None,
                "volume_24h": str(round(_decimal(t.get("vv"), Decimal(0)), 2)),
                "min_quantity_step": inst.get("qty_tick_size"),
            }
        ]

    @staticmethod
    def _record_prices(user_id, rows, quote):
        """Feed the scorecard; it must never be able to break a market read."""
        try:
            scorecard.record_snapshot(
                user_id,
                [
                    {"symbol": r["symbol"], "price_native": {"amount": r["price"]}}
                    for r in rows
                ],
            )
        except Exception:  # noqa: BLE001 - analytics only
            pass

    async def candles(self, user_id, symbol, timeframe="1h", count=100):
        symbol = symbol_of(symbol)
        if timeframe not in TIMEFRAMES:
            raise ValueError(
                "timeframe must be one of " + ", ".join(sorted(TIMEFRAMES))
            )
        count = max(1, min(int(count), 300))
        pair = self._pair(_read_ledger(user_id), symbol)
        result = await self.public(
            user_id,
            "get-candlestick",
            {"instrument_name": pair, "timeframe": timeframe, "count": count},
        )
        return {
            "pair": pair,
            "timeframe": timeframe,
            "candles": [
                {k: row.get(k) for k in ("t", "o", "h", "l", "c", "v")}
                for row in result.get("data", [])
            ],
        }

    async def book(self, user_id, symbol, depth=10):
        symbol = symbol_of(symbol)
        pair = self._pair(_read_ledger(user_id), symbol)
        result = await self.public(
            user_id,
            "get-book",
            {"instrument_name": pair, "depth": max(1, min(int(depth), 50))},
        )
        data = (result.get("data") or [{}])[0]
        return {
            "pair": pair,
            "bids": data.get("bids", []),
            "asks": data.get("asks", []),
        }

    # ── account ──

    async def accounts(self, user_id):
        # private/get-account-summary answers ERR_INTERNAL (50001) on the live
        # Exchange; private/user-balance is the working balance read.
        result = await self.private(user_id, "user-balance")
        rows = (result.get("data") or [{}])[0].get("position_balances", [])
        funded = []
        for row in rows:
            quantity = _decimal(row.get("quantity"), Decimal(0))
            if quantity > 0:
                reserved = _decimal(row.get("reserved_qty"), Decimal(0))
                funded.append(
                    {
                        "currency": row.get("instrument_name"),
                        "balance": fmt(quantity),
                        "available": fmt(max(quantity - reserved, Decimal(0))),
                    }
                )
        return {"accounts": funded}

    async def fee_rates(self, user_id):
        """The account's effective spot maker/taker rates, in percent (cached)."""
        stamp, cached = self._fees.get(user_id, (0.0, None))
        if cached and time.monotonic() - stamp < 600:
            return cached
        result = await self.private(user_id, "get-fee-rate")
        rates = {}
        for name, key in (
            ("maker_pct", "effective_spot_maker_rate_bps"),
            ("taker_pct", "effective_spot_taker_rate_bps"),
        ):
            bps = _decimal(result.get(key))
            if bps is None:
                raise ValueError("Crypto.com returned no spot fee rate")
            rates[name] = bps / 100
        self._fees[user_id] = (time.monotonic(), rates)
        return rates

    async def portfolio(self, user_id):
        """Whole Exchange account at the live last price, for the dashboard."""
        accounts = (await self.accounts(user_id))["accounts"]
        tickers = await self._tickers(user_id)

        def last(pair):
            return _decimal((tickers.get(pair) or {}).get("a"))

        eur_usd = None
        btc_usd, btc_eur = last("BTC_USD"), last("BTC_EUR")
        if btc_usd and btc_eur:
            eur_usd = btc_usd / btc_eur
        balances, unpriced = [], []
        for row in accounts:
            currency = row["currency"]
            if currency in ("USD", "USDT", "USDC"):
                price = Decimal(1)
            elif currency == "EUR":
                price = eur_usd
            else:
                price = last(f"{currency}_USD") or last(f"{currency}_USDT")
            if not price:
                unpriced.append(currency)
                price = Decimal(0)
            total = Decimal(row["balance"])
            balances.append(
                {
                    "token": currency,
                    "total": float(total),
                    "available": float(Decimal(row["available"])),
                    "usd_value": float(total * price),
                }
            )
        note = "Whole Crypto.com account at the last traded price. Only the bot allocation may be traded."
        if unpriced:
            note += " No USD price for " + ", ".join(unpriced) + "."
        return {
            "connector": "Crypto.com",
            "balances": balances,
            "total_usd": sum(row["usd_value"] for row in balances),
            "note": note,
        }

    async def sync_from_account(self, user_id, quote=None):
        """Seed the bot allocation from the Exchange account (owner action).

        Cost basis for coins that are already in the account restarts at their
        current bid; the Exchange does not know what they cost elsewhere.
        """
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            if ledger["uncertain_order"]:
                raise ValueError("Reconcile the uncertain order before syncing")
            if ledger["enabled"]:
                raise ValueError("Pause trading before syncing the allocation")
            quote = quote or ledger["quote"]
            if quote not in QUOTE_CURRENCIES:
                raise ValueError(
                    "Quote currency must be one of " + ", ".join(QUOTE_CURRENCIES)
                )
            accounts = (await self.accounts(user_id))["accounts"]
            instruments = await self.instruments(user_id)
            tickers = await self._tickers(user_id)
            cash = Decimal(0)
            positions = {}
            skipped = []
            for row in accounts:
                currency, available = row["currency"], Decimal(row["available"])
                if currency == quote:
                    cash = available
                    continue
                pair = f"{currency}_{quote}"
                ticker = tickers.get(pair)
                if pair not in instruments or not ticker:
                    skipped.append(currency)
                    continue
                bid = _decimal(ticker.get("b"))
                units = Decimal(row["balance"])
                if not bid or units <= 0:
                    skipped.append(currency)
                    continue
                positions[currency] = {"units": fmt(units), "cost": fmt(units * bid)}
            ledger = new_ledger(quote)
            ledger["cash"] = fmt(cash)
            ledger["positions"] = positions
            ledger["synced"] = True
            _write(_root(user_id) / "ledger.json", ledger)
            return {**self.status(user_id), "skipped": skipped}

    # ── valuation ──

    async def equity(self, user_id, ledger):
        total = Decimal(ledger["cash"])
        quote = ledger["quote"]
        tickers = await self._tickers(user_id)
        for symbol, position in ledger["positions"].items():
            ticker = tickers.get(f"{symbol}_{quote}")
            bid = _decimal((ticker or {}).get("b"))
            if not bid:
                raise ValueError(f"No market price for {symbol}")
            total += bid * Decimal(position["units"])
        return total

    async def valuation(self, user_id):
        ledger = _read_ledger(user_id)
        if ledger["uncertain_order"]:
            raise ValueError(
                "Reconcile the uncertain order before valuing the bot allocation"
            )
        equity = await self.equity(user_id, ledger)
        if equity <= FLOOR and ledger["enabled"]:
            self.set_enabled(user_id, False)
        return {
            "equity": fmt(equity),
            "quote": ledger["quote"],
            "new_position_limit": fmt(
                equity / 6 if guards_active(equity) else equity * MAX_ASSET_FRACTION
            ),
            "minimum_cash_reserve": fmt(
                equity / 2 if guards_active(equity) else Decimal(0)
            ),
            **self.status(user_id),
        }

    # ── orders ──

    async def _plan(self, user_id, side, symbol, value, slippage_pct):
        """Size an immediate-or-cancel limit order from the live book."""
        side = side.lower() if isinstance(side, str) else ""
        if side not in SIDES:
            raise ValueError("Use side buy or sell")
        symbol = symbol_of(symbol)
        slip = amount(str(slippage_pct if slippage_pct else DEFAULT_SLIPPAGE_PCT))
        if slip > MAX_SLIPPAGE_PCT:
            raise ValueError(f"Slippage above {MAX_SLIPPAGE_PCT}% is not allowed")
        ledger = _read_ledger(user_id)
        quote = ledger["quote"]
        pair = f"{symbol}_{quote}"
        instruments = await self.instruments(user_id)
        inst = instruments.get(pair)
        if not inst:
            raise ValueError(f"{pair} is not a tradable Crypto.com pair")
        ticker = (await self._tickers(user_id)).get(pair) or {}
        bid, ask = _decimal(ticker.get("b")), _decimal(ticker.get("k"))
        if not bid or not ask or bid <= 0 or ask <= 0:
            raise ValueError(f"No live market for {pair}")
        spread = (ask - bid) / ask * 100
        if spread > MAX_SPREAD_PCT:
            raise ValueError(f"{pair} spread is {spread:.2f}%, above {MAX_SPREAD_PCT}%")
        price_step = _decimal(inst.get("price_tick_size"), Decimal(0))
        qty_step = _decimal(inst.get("qty_tick_size"), Decimal(0))
        factor = slip / 100
        if side == "buy":
            limit = _round_up(ask * (1 + factor), price_step)
            quantity = _round_down(amount(value) / limit, qty_step)
        else:
            limit = _round_down(bid * (1 - factor), price_step)
            quantity = _round_down(amount(value), qty_step)
        if quantity <= 0:
            raise ValueError("Amount is below the minimum order size for this pair")
        return {
            "side": side,
            "symbol": symbol,
            "pair": pair,
            "quote": quote,
            "quantity": quantity,
            "limit_price": limit,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread,
            "slippage_pct": slip,
        }

    async def preview(
        self,
        user_id,
        side,
        symbol,
        value,
        slippage_pct=None,
        mode="taker",
        purpose=None,
        maker_wait_sec=None,
    ):
        mode, purpose, wait = _mode_args(side, mode, purpose, maker_wait_sec)
        if mode == "maker":
            return await self._preview_maker(
                user_id, side, symbol, value, purpose, wait
            )
        plan = await self._plan(user_id, side, symbol, value, slippage_pct)
        book = await self.book(user_id, plan["symbol"], 20)
        remaining, cost = plan["quantity"], Decimal(0)
        for level in book["asks" if plan["side"] == "buy" else "bids"]:
            price, size = _decimal(level[0]), _decimal(level[1])
            take = min(remaining, size)
            cost += take * price
            remaining -= take
            if remaining <= 0:
                break
        average = (
            cost / (plan["quantity"] - remaining)
            if plan["quantity"] > remaining
            else None
        )
        reference = plan["ask"] if plan["side"] == "buy" else plan["bid"]
        impact = (
            abs(average - reference) / reference * 100 if average is not None else None
        )
        taker = (await self.fee_rates(user_id))["taker_pct"]
        round_trip = plan["spread_pct"] + 2 * taker
        return {
            "pair": plan["pair"],
            "side": plan["side"],
            "taker_fee_pct": fmt(taker),
            "estimated_round_trip_pct": str(round(round_trip, 3)),
            "quantity": fmt(plan["quantity"]),
            "limit_price": fmt(plan["limit_price"]),
            "bid": fmt(plan["bid"]),
            "ask": fmt(plan["ask"]),
            "spread_pct": str(round(plan["spread_pct"], 3)),
            "estimated_average_price": fmt(average) if average is not None else None,
            "estimated_impact_pct": (
                str(round(impact, 3)) if impact is not None else None
            ),
            "book_covers_order": remaining <= 0,
        }

    async def _check_order(self, user_id, ledger, plan):
        """The guards every order passes before anything is written or sent."""
        symbol, quote = plan["symbol"], plan["quote"]
        if plan["side"] == "buy":
            equity = await self.equity(user_id, ledger)
            if equity <= FLOOR:
                self.set_enabled(user_id, False)
                raise ValueError(
                    f"Bot equity reached the {FLOOR} stop threshold; trading paused"
                )
            spend = plan["quantity"] * plan["limit_price"]
            if spend > Decimal(ledger["cash"]):
                raise ValueError("Not enough bot cash for this purchase")
            check_purchase(ledger, symbol, spend, equity)
        else:
            position = ledger["positions"].get(symbol)
            if not position or plan["quantity"] > Decimal(position["units"]):
                raise ValueError("Cannot sell assets outside the bot allocation")
        accounts = {
            a["currency"]: a for a in (await self.accounts(user_id))["accounts"]
        }
        need_currency = quote if plan["side"] == "buy" else symbol
        need = (
            plan["quantity"] * plan["limit_price"]
            if plan["side"] == "buy"
            else plan["quantity"]
        )
        have = Decimal(accounts.get(need_currency, {}).get("available", "0"))
        if have < need:
            raise ValueError("Insufficient available Crypto.com balance")
        recent = [o for o in ledger["orders"] if time.time() - o["time"] < 60]
        if len(recent) >= 10:
            raise ValueError("Maximum ten orders per minute")
        current = _read_ledger(user_id)
        if not current["enabled"]:
            raise ValueError("Trading paused during checks")

    async def order(
        self,
        user_id,
        side,
        symbol,
        value,
        slippage_pct=None,
        mode="taker",
        purpose=None,
        maker_wait_sec=None,
    ):
        mode, purpose, wait = _mode_args(side, mode, purpose, maker_wait_sec)
        if mode == "maker":
            return await self._order_maker(user_id, side, symbol, value, purpose, wait)
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            if not ledger["enabled"] or ledger["uncertain_order"]:
                raise ValueError("Trading is paused")
            plan = await self._plan(user_id, side, symbol, value, slippage_pct)
            symbol, quote = plan["symbol"], plan["quote"]
            await self._check_order(user_id, ledger, plan)
            client_oid = str(uuid.uuid4())
            ledger["uncertain_order"] = {
                "client_oid": client_oid,
                "side": plan["side"],
                "symbol": symbol,
                "quantity": fmt(plan["quantity"]),
                "limit_price": fmt(plan["limit_price"]),
                "time": time.time(),
            }
            _write(_root(user_id) / "ledger.json", ledger)
            try:
                placed = await self.private(
                    user_id,
                    "create-order",
                    {
                        "instrument_name": plan["pair"],
                        "side": SIDES[plan["side"]],
                        "type": "LIMIT",
                        "price": fmt(plan["limit_price"]),
                        "quantity": fmt(plan["quantity"]),
                        "time_in_force": "IMMEDIATE_OR_CANCEL",
                        "client_oid": client_oid,
                    },
                )
                detail = await self._final_detail(
                    user_id, placed.get("order_id"), client_oid
                )
            except ExchangeError as error:
                if not error.definite:
                    raise ValueError(_UNCERTAIN) from None
                # The Exchange answered and refused: no order exists, so there
                # is nothing to reconcile and trading stays as it was.
                ledger["uncertain_order"] = None
                _write(_root(user_id) / "ledger.json", ledger)
                raise ValueError(f"Crypto.com rejected the order: {error}") from None
            except Exception:
                raise ValueError(_UNCERTAIN) from None
            self._apply_fill(ledger, plan["side"], symbol, quote, detail)
            ledger["enabled"] = (
                credentials_path(user_id).exists() and _read_ledger(user_id)["enabled"]
            )
            _write(_root(user_id) / "ledger.json", ledger)
            return {"order": self._summary(detail), "status": self.status(user_id)}

    async def _detail(self, user_id, order_id, client_oid):
        params = {"order_id": str(order_id)} if order_id else {"client_oid": client_oid}
        result = await self.private(user_id, "get-order-detail", params)
        detail = result.get("order_info", result)
        if not detail:
            # The Exchange answers code 0 with no result for an order it never saw.
            raise ExchangeError(200, None, "order not found")
        return detail

    async def _final_detail(self, user_id, order_id, client_oid):
        for attempt in range(ORDER_POLLS):
            detail = await self._detail(user_id, order_id, client_oid)
            if str(detail.get("status", "")).upper() in FINAL_STATUSES:
                return detail
            if attempt + 1 < ORDER_POLLS:
                await asyncio.sleep(ORDER_POLL_WAIT_SEC)
        raise ValueError("Order did not reach a final state")

    @staticmethod
    def _summary(detail):
        keys = (
            "order_id",
            "client_oid",
            "instrument_name",
            "side",
            "status",
            "quantity",
            "limit_price",
            "avg_price",
            "cumulative_quantity",
            "cumulative_value",
            "cumulative_fee",
            "fee_instrument_name",
        )
        return {k: detail.get(k) for k in keys if k in detail}

    def _apply_fill(self, ledger, side, symbol, quote, detail):
        status = str(detail.get("status", "")).upper()
        if status not in FINAL_STATUSES:
            raise ValueError("Order has not reached a final state")
        quantity = _decimal(detail.get("cumulative_quantity"), Decimal(0))
        value = _decimal(detail.get("cumulative_value"), Decimal(0))
        fee = _decimal(detail.get("cumulative_fee"), Decimal(0))
        fee_currency = detail.get("fee_instrument_name")
        if quantity > 0:
            if side == "buy":
                units = quantity - (fee if fee_currency == symbol else 0)
                spent = value + (fee if fee_currency == quote else 0)
                previous = ledger["positions"].get(symbol, {"units": "0", "cost": "0"})
                ledger["positions"][symbol] = {
                    "units": fmt(Decimal(previous["units"]) + units),
                    "cost": fmt(Decimal(previous["cost"]) + spent),
                }
                ledger["cash"] = fmt(Decimal(ledger["cash"]) - spent)
            else:
                position = ledger["positions"].get(symbol)
                if not position or quantity > Decimal(position["units"]):
                    raise ValueError("Filled sale exceeds the bot position")
                proceeds = value - (fee if fee_currency == quote else 0)
                fraction = quantity / Decimal(position["units"])
                cost = Decimal(position["cost"]) * fraction
                if fraction == 1:
                    del ledger["positions"][symbol]
                else:
                    position["units"] = fmt(Decimal(position["units"]) - quantity)
                    position["cost"] = fmt(Decimal(position["cost"]) - cost)
                ledger["cash"] = fmt(Decimal(ledger["cash"]) + proceeds)
        ledger["uncertain_order"] = None
        ledger["orders"] = (
            ledger["orders"]
            + [
                {
                    "time": time.time(),
                    "side": side,
                    "symbol": symbol,
                    "order_id": detail.get("order_id"),
                    "client_oid": detail.get("client_oid"),
                    "status": status,
                    "quantity": fmt(quantity),
                    "value": fmt(value),
                    "fee": fmt(fee),
                    "fee_currency": fee_currency,
                }
            ]
        )[-200:]

    async def history(self, user_id):
        result = await self.private(user_id, "get-order-history", {"limit": 50})
        return [self._summary(row) for row in result.get("data", [])][:50]

    async def reconcile(self, user_id):
        """Settle an order whose outcome was unknown. Leaves trading paused."""
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            pending = ledger["uncertain_order"]
            if not pending:
                return self.status(user_id)
            try:
                detail = await self._detail(user_id, None, pending["client_oid"])
            except ExchangeError:
                if time.time() - pending["time"] < 60:
                    raise ValueError("Wait a minute before reconciling") from None
                history = await self.history(user_id)
                if any(
                    row.get("client_oid") == pending["client_oid"] for row in history
                ):
                    raise ValueError(
                        "Order found in history but not readable; keep trading paused"
                    ) from None
                ledger["uncertain_order"] = None
                ledger["enabled"] = False
                _write(_root(user_id) / "ledger.json", ledger)
                return self.status(user_id)
            if pending.get("mode") == "maker" and (
                str(detail.get("status", "")).upper() not in FINAL_STATUSES
            ):
                # A maker order may still be resting: close it before settling.
                detail = await self._close_resting(
                    user_id, detail.get("order_id"), pending["client_oid"]
                )
            self._apply_fill(
                ledger, pending["side"], pending["symbol"], ledger["quote"], detail
            )
            ledger["enabled"] = False
            _write(_root(user_id) / "ledger.json", ledger)
            if pending.get("mode") == "maker":
                self._log_reconciled_maker(user_id, ledger, pending, detail)
            return self.status(user_id)

    # ── maker mode ──

    async def _sleep(self, seconds):
        await asyncio.sleep(seconds)

    def _monotonic(self):
        return time.monotonic()

    def maker_enabled(self, user_id):
        return _read_maker_settings(user_id)["maker_enabled"]

    def set_maker_enabled(self, user_id, enabled):
        """Owner switch (web API only, never an MCP action); off until turned on."""
        settings = _read_maker_settings(user_id)
        settings["maker_enabled"] = bool(enabled)
        _write(_maker_settings_path(user_id), settings)
        return self.maker_stats(user_id)

    def set_maker_fee_override(self, user_id, pair, pct):
        """Owner/operator only (no route, no MCP action): a per-pair maker rate in
        percent for estimates and fee labels, for example a promotion once its terms
        are read. ``None`` removes it. The ledger always uses the fee the Exchange
        reports, never this value."""
        if not isinstance(pair, str) or not re.fullmatch(
            r"[A-Z0-9]{1,24}_[A-Z0-9]{2,8}", pair
        ):
            raise ValueError("Use a pair such as SHIB_USD")
        settings = _read_maker_settings(user_id)
        if pct is None:
            settings["maker_fee_overrides"].pop(pair, None)
        else:
            rate = _decimal(pct)
            if rate is None or not Decimal(0) <= rate <= Decimal(1):
                raise ValueError("A maker fee override is a percent from 0 to 1")
            settings["maker_fee_overrides"][pair] = fmt(rate)
        _write(_maker_settings_path(user_id), settings)
        return settings

    async def _fee_view(self, user_id, pair):
        try:
            rates = await self.fee_rates(user_id)
            maker, taker = rates["maker_pct"], rates["taker_pct"]
            source = "exchange_get_fee_rate"
        except ValueError:
            maker, taker = MAKER_FEE_DEFAULT_PCT, TAKER_FEE_DEFAULT_PCT
            source = "default_estimate_not_read"
        maker_source = source
        overrides = _read_maker_settings(user_id)["maker_fee_overrides"]
        override = _decimal(overrides.get(pair))
        if override is not None and override >= 0:
            maker, maker_source = override, "owner_override"
        return {
            "maker_pct": maker,
            "taker_pct": taker,
            "maker_source": maker_source,
            "taker_source": source,
        }

    async def _plan_maker(self, user_id, side, symbol, value):
        """Size a post-only limit order that rests at the touch and never crosses it."""
        side = side.lower() if isinstance(side, str) else ""
        if side not in SIDES:
            raise ValueError("Use side buy or sell")
        symbol = symbol_of(symbol)
        ledger = _read_ledger(user_id)
        quote = ledger["quote"]
        pair = f"{symbol}_{quote}"
        inst = (await self.instruments(user_id)).get(pair)
        if not inst:
            raise ValueError(f"{pair} is not a tradable Crypto.com pair")
        book = await self.book(user_id, symbol, 10)
        bids, asks = book["bids"], book["asks"]
        bid = _decimal(bids[0][0]) if bids else None
        ask = _decimal(asks[0][0]) if asks else None
        if not bid or not ask or bid <= 0 or ask <= 0 or bid >= ask:
            raise ValueError(f"No live market for {pair}")
        spread = (ask - bid) / ask * 100
        if spread > MAX_SPREAD_PCT:
            raise ValueError(f"{pair} spread is {spread:.2f}%, above {MAX_SPREAD_PCT}%")
        price_step = _decimal(inst.get("price_tick_size"), Decimal(0))
        qty_step = _decimal(inst.get("qty_tick_size"), Decimal(0))
        if side == "buy":
            # Join the best bid, rounded down: it can never reach the ask.
            price = _round_down(bid, price_step)
            touch_size = _decimal(bids[0][1], Decimal(0))
            if price <= 0 or price >= ask:
                raise ValueError(f"No safe resting price for {pair}")
            quantity = _round_down(amount(value) / price, qty_step)
        else:
            price = _round_up(ask, price_step)
            touch_size = _decimal(asks[0][1], Decimal(0))
            if price <= bid:
                raise ValueError(f"No safe resting price for {pair}")
            quantity = _round_down(amount(value), qty_step)
        if quantity <= 0:
            raise ValueError("Amount is below the minimum order size for this pair")
        return {
            "side": side,
            "symbol": symbol,
            "pair": pair,
            "quote": quote,
            "quantity": quantity,
            "limit_price": price,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread,
            "touch_size": touch_size,
        }

    async def _preview_maker(self, user_id, side, symbol, value, purpose, wait):
        """Evidence for a resting post-only order; it places nothing."""
        plan = await self._plan_maker(user_id, side, symbol, value)
        fees = await self._fee_view(user_id, plan["pair"])
        maker, taker = fees["maker_pct"], fees["taker_pct"]
        price, quantity = plan["limit_price"], plan["quantity"]
        queue = plan["touch_size"]
        return {
            "mode": "maker",
            "purpose": purpose,
            "pair": plan["pair"],
            "side": plan["side"],
            "post_only": True,
            "time_in_force": "GOOD_TILL_CANCEL",
            "exec_inst": ["POST_ONLY"],
            "maker_mode_enabled": self.maker_enabled(user_id),
            "resting_price": fmt(price),
            "limit_price": fmt(price),
            "quantity": fmt(quantity),
            "notional": fmt(quantity * price),
            "bid": fmt(plan["bid"]),
            "ask": fmt(plan["ask"]),
            "spread_pct": str(round(plan["spread_pct"], 3)),
            "maker_wait_sec": wait,
            "maker_fee_pct": fmt(maker),
            "maker_fee_source": fees["maker_source"],
            "taker_fee_pct": fmt(taker),
            "taker_fee_source": fees["taker_source"],
            # Fees only: a maker order does not cross the spread, so a maker entry
            # and a taker exit at the same touch pay no spread. Spread captured by
            # a resting sell is not counted. The cost is paid only if the order fills.
            "estimated_round_trip_pct": fmt(round(maker + taker, 3)),
            "maker_taker_round_trip_pct": fmt(round(maker + taker, 3)),
            "maker_maker_round_trip_pct": fmt(round(2 * maker, 3)),
            "taker_taker_round_trip_pct": fmt(round(plan["spread_pct"] + 2 * taker, 3)),
            "round_trip_basis": (
                "Fees only, paid only if the order fills. maker_taker = a maker entry "
                "and a taker exit; maker_maker = both legs rest. A stop exit is always "
                "taker. The saving versus taker_taker is before adverse selection: a "
                "resting order tends to fill when the price moves against it."
            ),
            "touch_queue_quantity": fmt(queue),
            "touch_queue_value": fmt(queue * price),
            "fill_risk": (
                "Maker orders are NOT guaranteed to fill; the fee is paid only on what "
                "fills. Fill probability unknown (no trade-flow estimate is read). "
                f"The order joins the touch behind about {fmt(queue)} units and is "
                f"cancelled after {wait} s; an unfilled or partly filled order is not "
                "retried and is never resubmitted as a taker order automatically."
            ),
        }

    async def _order_maker(self, user_id, side, symbol, value, purpose, wait_sec):
        started = self._monotonic()
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            if not self.maker_enabled(user_id):
                raise ValueError(
                    "Maker mode is off for this account (the owner turns it on in the "
                    "Crypto.com panel). Nothing was placed."
                )
            if not ledger["enabled"] or ledger["uncertain_order"]:
                raise ValueError("Trading is paused")
            plan = await self._plan_maker(user_id, side, symbol, value)
            symbol, quote = plan["symbol"], plan["quote"]
            await self._check_order(user_id, ledger, plan)
            fees = await self._fee_view(user_id, plan["pair"])
            client_oid = str(uuid.uuid4())
            ledger["uncertain_order"] = {
                "client_oid": client_oid,
                "side": plan["side"],
                "symbol": symbol,
                "quantity": fmt(plan["quantity"]),
                "limit_price": fmt(plan["limit_price"]),
                "time": time.time(),
                "mode": "maker",
                "purpose": purpose,
            }
            _write(_root(user_id) / "ledger.json", ledger)
            try:
                placed = await self.private(
                    user_id,
                    "create-order",
                    {
                        "instrument_name": plan["pair"],
                        "side": SIDES[plan["side"]],
                        "type": "LIMIT",
                        "price": fmt(plan["limit_price"]),
                        "quantity": fmt(plan["quantity"]),
                        "time_in_force": "GOOD_TILL_CANCEL",
                        "exec_inst": ["POST_ONLY"],
                        "client_oid": client_oid,
                    },
                )
            except ExchangeError as error:
                if not error.definite:
                    raise self._uncertain(
                        user_id, plan, purpose, wait_sec, client_oid, fees
                    ) from None
                ledger["uncertain_order"] = None
                _write(_root(user_id) / "ledger.json", ledger)
                entry = self._maker_entry(
                    plan, purpose, wait_sec, "rejected", client_oid, fees
                )
                self._log_maker(user_id, entry)
                raise ValueError(self._refusal(error)) from None
            except Exception:
                raise self._uncertain(
                    user_id, plan, purpose, wait_sec, client_oid, fees
                ) from None
            order_id = placed.get("order_id")
            try:
                detail, context = await self._rest_and_close(
                    user_id, order_id, client_oid, wait_sec, started
                )
            except Exception:
                # Includes _MakerUnresolved: the final state is unknown, and the
                # write-before-send record keeps trading paused until reconcile.
                raise self._uncertain(
                    user_id, plan, purpose, wait_sec, client_oid, fees
                ) from None
            status = str(detail.get("status", "")).upper()
            filled = _decimal(detail.get("cumulative_quantity"), Decimal(0))
            if filled <= 0 and status == "REJECTED":
                ledger["uncertain_order"] = None
                _write(_root(user_id) / "ledger.json", ledger)
                entry = self._maker_entry(
                    plan, purpose, wait_sec, "rejected", client_oid, fees, detail
                )
                self._log_maker(user_id, entry)
                reason = re.sub(
                    r"[^A-Za-z0-9 _.,:;()/-]", "", str(detail.get("reason", ""))
                )
                raise ValueError(
                    "Crypto.com rejected the post-only order"
                    + (f" ({reason[:60]})" if reason else "")
                    + ": it would probably have crossed the book. Nothing traded and "
                    "nothing changed. The caller decides what to do; it is not "
                    "retried as a taker order."
                )
            if filled > 0:
                self._apply_fill(ledger, plan["side"], symbol, quote, detail)
            else:
                ledger["uncertain_order"] = None
            ledger["enabled"] = (
                credentials_path(user_id).exists() and _read_ledger(user_id)["enabled"]
            )
            _write(_root(user_id) / "ledger.json", ledger)
            maker = self._maker_result(plan, purpose, wait_sec, detail, context, fees)
            entry = self._maker_entry(
                plan,
                purpose,
                wait_sec,
                maker["outcome"],
                client_oid,
                fees,
                detail,
                context,
            )
            self._log_maker(user_id, entry)
            return {
                "order": self._summary(detail),
                "outcome": maker["outcome"],
                "maker": maker,
                "status": self.status(user_id),
            }

    def _uncertain(self, user_id, plan, purpose, wait_sec, client_oid, fees):
        entry = self._maker_entry(
            plan, purpose, wait_sec, "uncertain", client_oid, fees
        )
        self._log_maker(user_id, entry)
        return ValueError(_UNCERTAIN_MAKER)

    @staticmethod
    def _refusal(error):
        if error.code == POST_ONLY_REJECTED_CODE:
            return (
                "Crypto.com rejected the post-only order (it would have crossed the "
                "book). Nothing was placed and nothing changed. The caller decides "
                "what to do; it is not retried as a taker order."
            )
        return f"Crypto.com rejected the maker order, nothing was placed: {error}"

    async def _rest_and_close(self, user_id, order_id, client_oid, wait_sec, started):
        """Let the order rest, then cancel what is left and read the final state."""
        placed = self._monotonic()
        deadline = min(placed + wait_sec, started + MAKER_CALL_BUDGET_SEC)
        failures = 0
        first_fill = None
        while True:
            detail = None
            try:
                detail = await self._detail(user_id, order_id, client_oid)
                failures = 0
            except ValueError:
                failures += 1
            if detail is not None:
                if (
                    _decimal(detail.get("cumulative_quantity"), Decimal(0)) > 0
                    and first_fill is None
                ):
                    first_fill = self._monotonic() - placed
                status = str(detail.get("status", "")).upper()
                if status in FINAL_STATUSES:
                    return detail, {
                        "waited_sec": self._monotonic() - placed,
                        "time_to_fill_sec": first_fill,
                        "cancel_requested": False,
                        "cancelled_by": "exchange" if status != "FILLED" else None,
                    }
            remaining = deadline - self._monotonic()
            if remaining <= 0 or failures >= MAKER_POLL_FAILURES:
                break
            await self._sleep(min(MAKER_POLL_SEC, remaining))
        await self._cancel_resting(user_id, order_id, client_oid)
        detail = await self._settle(user_id, order_id, client_oid)
        waited = self._monotonic() - placed
        if first_fill is None and (
            _decimal(detail.get("cumulative_quantity"), Decimal(0)) > 0
        ):
            first_fill = waited
        return detail, {
            "waited_sec": waited,
            "time_to_fill_sec": first_fill,
            "cancel_requested": True,
            "cancelled_by": "us",
        }

    async def _cancel_resting(self, user_id, order_id, client_oid):
        """private/cancel-order is asynchronous: it only confirms the request, so the
        caller reads the order again. A cancel is idempotent, so it may be retried."""
        params = {"order_id": str(order_id)} if order_id else {"client_oid": client_oid}
        for attempt in range(MAKER_CANCEL_ATTEMPTS):
            try:
                await self.private(user_id, "cancel-order", params)
                return
            except ExchangeError as error:
                if error.code == NO_ACTIVE_ORDER_CODE:
                    return  # no longer open: the final read says why
            except ValueError:
                pass
            if attempt + 1 < MAKER_CANCEL_ATTEMPTS:
                await self._sleep(ORDER_POLL_WAIT_SEC)
        raise _MakerUnresolved

    async def _settle(self, user_id, order_id, client_oid):
        for attempt in range(ORDER_POLLS):
            try:
                detail = await self._detail(user_id, order_id, client_oid)
            except ValueError:
                detail = None
            if detail is not None and (
                str(detail.get("status", "")).upper() in FINAL_STATUSES
            ):
                return detail
            if attempt + 1 < ORDER_POLLS:
                await self._sleep(ORDER_POLL_WAIT_SEC)
        raise _MakerUnresolved

    async def _close_resting(self, user_id, order_id, client_oid):
        try:
            await self._cancel_resting(user_id, order_id, client_oid)
            return await self._settle(user_id, order_id, client_oid)
        except _MakerUnresolved:
            raise ValueError(
                "The maker order could not be confirmed closed on Crypto.com; "
                "trading stays paused. Check the order history and reconcile again."
            ) from None

    @staticmethod
    def _fee_figures(detail, symbol, quote):
        filled = _decimal(detail.get("cumulative_quantity"), Decimal(0))
        value = _decimal(detail.get("cumulative_value"), Decimal(0))
        fee = _decimal(detail.get("cumulative_fee"), Decimal(0))
        currency = detail.get("fee_instrument_name")
        average = _decimal(detail.get("avg_price"))
        if not average and filled > 0:
            average = value / filled
        fee_quote = None
        if currency == quote:
            fee_quote = fee
        elif currency == symbol and average:
            fee_quote = fee * average
        return filled, value, fee, currency, average, fee_quote

    def _maker_result(self, plan, purpose, wait_sec, detail, context, fees):
        filled, value, fee, currency, average, fee_quote = self._fee_figures(
            detail, plan["symbol"], plan["quote"]
        )
        ordered = plan["quantity"]
        status = str(detail.get("status", "")).upper()
        if filled <= 0:
            outcome = "unfilled"
        elif status == "FILLED" or filled >= ordered:
            outcome = "filled"
        else:
            outcome = "partial"
        effective = (
            fee_quote / value * 100 if fee_quote is not None and value > 0 else None
        )
        if effective is None:
            fee_class = "none" if filled <= 0 else "unknown"
        elif abs(effective - fees["maker_pct"]) <= Decimal("0.03"):
            fee_class = "maker"
        elif abs(effective - fees["taker_pct"]) <= Decimal("0.03"):
            fee_class = "taker"
        else:
            fee_class = "other"
        notes = {
            "filled": "The whole order filled as a maker order.",
            "partial": (
                "Part of the order filled; the rest was cancelled and is not retried. "
                "The ledger holds only what filled."
            ),
            "unfilled": (
                "No trade happened: the order did not fill and was cancelled. The "
                "ledger is unchanged. Do not retry as a taker order in the same tick."
            ),
        }
        note = notes[outcome]
        if context["cancelled_by"] == "exchange" and outcome != "filled":
            note += " Crypto.com closed the order before the deadline."
        return {
            "outcome": outcome,
            "mode": "maker",
            "purpose": purpose,
            "post_only": True,
            "resting_price": fmt(plan["limit_price"]),
            "ordered_quantity": fmt(ordered),
            "filled_quantity": fmt(filled),
            "unfilled_quantity": fmt(max(ordered - filled, Decimal(0))),
            "filled_fraction": fmt(round(filled / ordered, 4)) if ordered else "0",
            "average_price": fmt(average) if average else None,
            "value": fmt(value),
            "fee": fmt(fee),
            "fee_currency": currency,
            "effective_fee_pct": (
                fmt(round(effective, 4)) if effective is not None else None
            ),
            "fee_class": fee_class,
            "maker_fee_pct": fmt(fees["maker_pct"]),
            "maker_fee_source": fees["maker_source"],
            "taker_fee_pct": fmt(fees["taker_pct"]),
            "wait_sec_requested": wait_sec,
            "waited_sec": round(context["waited_sec"], 1),
            "time_to_fill_sec": (
                round(context["time_to_fill_sec"], 1)
                if context["time_to_fill_sec"] is not None
                else None
            ),
            "remainder_cancelled": context["cancel_requested"],
            "cancelled_by": context["cancelled_by"],
            "note": note,
        }

    def _maker_entry(
        self,
        plan,
        purpose,
        wait_sec,
        outcome,
        client_oid,
        fees,
        detail=None,
        context=None,
    ):
        entry = {
            "time": time.time(),
            "pair": plan["pair"],
            "quote": plan["quote"],
            "side": plan["side"],
            "purpose": purpose,
            "resting_price": fmt(plan["limit_price"]),
            "ordered_quantity": fmt(plan["quantity"]),
            "wait_sec": wait_sec,
            "outcome": outcome,
            "client_oid": client_oid,
            "taker_fee_pct": fmt(fees["taker_pct"]),
            "maker_fee_pct": fmt(fees["maker_pct"]),
            "filled_quantity": "0",
            "filled_fraction": 0.0,
        }
        if detail is not None:
            filled, value, fee, currency, average, fee_quote = self._fee_figures(
                detail, plan["symbol"], plan["quote"]
            )
            entry.update(
                {
                    "order_id": detail.get("order_id"),
                    "filled_quantity": fmt(filled),
                    "filled_fraction": (
                        float(round(filled / plan["quantity"], 4))
                        if plan["quantity"]
                        else 0.0
                    ),
                    "value": fmt(value),
                    "average_price": fmt(average) if average else None,
                    "fee": fmt(fee),
                    "fee_currency": currency,
                    "fee_quote": fmt(fee_quote) if fee_quote is not None else None,
                }
            )
        if context is not None:
            entry["waited_sec"] = round(context["waited_sec"], 1)
            if context["time_to_fill_sec"] is not None:
                entry["time_to_fill_sec"] = round(context["time_to_fill_sec"], 1)
        return entry

    @staticmethod
    def _log_maker(user_id, entry):
        """Measurement only: it must never be able to break an order."""
        try:
            rows = _read_maker_log(user_id)
            rows.append(entry)
            _write(_maker_log_path(user_id), {"attempts": rows[-MAKER_LOG_LIMIT:]})
        except Exception:  # noqa: BLE001 - analytics only
            pass

    def _log_reconciled_maker(self, user_id, ledger, pending, detail):
        try:
            symbol, quote = pending["symbol"], ledger["quote"]
            ordered = Decimal(pending["quantity"])
            plan = {
                "pair": f"{symbol}_{quote}",
                "quote": quote,
                "symbol": symbol,
                "side": pending["side"],
                "limit_price": Decimal(pending["limit_price"]),
                "quantity": ordered,
            }
            fees = {
                "maker_pct": MAKER_FEE_DEFAULT_PCT,
                "taker_pct": TAKER_FEE_DEFAULT_PCT,
            }
            filled = _decimal(detail.get("cumulative_quantity"), Decimal(0))
            if filled <= 0:
                outcome = "unfilled"
            elif filled >= ordered:
                outcome = "filled"
            else:
                outcome = "partial"
            entry = self._maker_entry(
                plan,
                pending.get("purpose"),
                None,
                outcome,
                pending["client_oid"],
                fees,
                detail,
            )
            entry["reconciled"] = True
            self._log_maker(user_id, entry)
        except Exception:  # noqa: BLE001 - analytics only
            pass

    def maker_stats(self, user_id):
        settings = _read_maker_settings(user_id)
        return {
            "maker_enabled": settings["maker_enabled"],
            "maker_fee_overrides": settings["maker_fee_overrides"],
            "stats": maker_stats(_read_maker_log(user_id)),
        }

    # ── scorecard ──

    async def predict(
        self, user_id, symbol, verdict, target_pct, stop_pct, horizon_hours, note=""
    ):
        symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
        if symbol and scorecard.latest_price(user_id, symbol) is None:
            await self.coins(user_id, symbol)
        return scorecard.record_prediction(
            user_id, symbol, verdict, target_pct, stop_pct, horizon_hours, note
        )

    def scorecard(self, user_id):
        return scorecard.report(user_id)


class ExchangeError(ValueError):
    """The Exchange answered with a non-zero code; only a bounded text is kept."""

    def __init__(self, status, code, text):
        super().__init__(
            f"Crypto.com rejected the request (HTTP {status}, code {code}"
            + (f": {text}" if text else "")
            + ")"
        )
        self.status = status
        self.code = code
        # A refusal that cannot have created an order: the Exchange processed the
        # request and said no. A server-side failure (HTTP 5xx or an internal-error
        # code) may still have created it, so that stays uncertain.
        self.definite = code == POST_ONLY_REJECTED_CODE or (
            status < 500 and not (isinstance(code, int) and code >= 50000)
        )


service = ExchangeService()
