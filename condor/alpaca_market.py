"""Alpaca's real-time IEX market data for US stocks: the Trading 212 crew's reference prices.

Trading 212 has no quote or candle endpoint, so the crew reads prices elsewhere. This module
reads Alpaca's free Basic market data (the IEX feed, the only one a free account may use):
no brokerage order, no money movement, only ``GET`` on ``data.alpaca.markets``. The key is
write-only like every other credential here: saved to a private file, never returned.

IEX is one exchange, so its volume is a small share of the whole market and its best bid and
offer can be wider than the national one for quiet names; the rows say so through their own
fields (``volume_24h_usd`` is today's IEX dollar volume, ``quote_age_sec`` the age of the quote).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import ceil

from condor.fsutil import atomic_write_json
from condor.paths import data_dir

BASE = "https://data.alpaca.markets"
FEED = "iex"
FEED_LABEL = "Alpaca real-time IEX data"
#: Alpaca publishes 200 historical calls a minute for the free plan; stay well inside it.
CALLS_PER_MINUTE = 150
SNAPSHOT_CHUNK = 100
MAX_BYTES = 4 * 1024 * 1024
#: A quote older than this is not a live price (checked where an order is priced).
MAX_QUOTE_AGE_SEC = 300
INTERVALS = {
    "5m": ("5Min", 5),
    "15m": ("15Min", 15),
    "30m": ("30Min", 30),
    "1h": ("1Hour", 60),
    "4h": ("4Hour", 240),
    "1D": ("1Day", 1440),
}

#: The liquid US stocks and ETFs the crew scans. Trading 212 lists far more, but a scan of
#: thousands of names every five minutes is neither wanted nor within the free plan's limits;
#: the crew trades the most active names, so this is the universe (what Trading 212 does not
#: list simply drops out of it).
WATCHLIST = (
    "AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO ORCL ADBE CRM AMD INTC QCOM TXN MU AMAT LRCX "
    "NFLX CSCO IBM NOW INTU PANW UBER SHOP PLTR SNOW COIN HOOD DELL ARM SMCI MRVL "
    "JPM BAC WFC C GS MS V MA AXP PYPL BLK SCHW "
    "UNH LLY JNJ PFE MRK ABBV TMO ABT BMY AMGN GILD CVS ISRG "
    "WMT COST HD LOW TGT NKE SBUX MCD KO PEP PG DIS BKNG ABNB CMG LULU "
    "BA CAT GE HON UPS RTX LMT XOM CVX COP SLB OXY "
    "T VZ TMUS CMCSA F GM RIVN LCID "
    "SPY QQQ IWM DIA XLK XLF XLE SMH ARKK TLT GLD SLV"
).split()

_ID = re.compile(r"[A-Za-z0-9_\-]{8,128}")


def _decimal(value, default=None):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return default
    return result if result.is_finite() else default


def _credentials_path(user_id):
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid account owner")
    return data_dir() / "alpaca_data" / str(user_id) / "credentials.json"


def _check_pair(key_id, secret):
    for value in (key_id, secret):
        if not isinstance(value, str) or not _ID.fullmatch(value.strip()):
            raise ValueError("Invalid Alpaca key ID or secret")
    return key_id.strip(), secret.strip()


def _timestamp(value):
    """Seconds since the epoch of an RFC 3339 time (nanosecond fractions allowed), or None."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", value.strip().replace("Z", "+00:00"))
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


class AlpacaMarket:
    def __init__(self):
        self.clock = time.time
        self._calls = deque()

    # ── credentials ──

    def _read(self, user_id):
        try:
            data = json.loads(_credentials_path(user_id).read_text())
        except (FileNotFoundError, ValueError, OSError):
            return None
        key_id, secret = data.get("key_id"), data.get("secret")
        if isinstance(key_id, str) and isinstance(secret, str) and key_id and secret:
            return key_id, secret
        return None

    def configured(self, user_id):
        try:
            return self._read(user_id) is not None
        except ValueError:
            return False

    def status(self, user_id):
        return {"configured": self.configured(user_id), "feed": FEED}

    def save_credentials(self, user_id, key_id, secret):
        key_id, secret = _check_pair(key_id, secret)
        path = _credentials_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(path, {"key_id": key_id, "secret": secret})
        os.chmod(path, 0o600)
        return self.status(user_id)

    def disconnect(self, user_id):
        _credentials_path(user_id).unlink(missing_ok=True)
        return self.status(user_id)

    # ── transport ──

    async def _fetch(self, path, params, headers):
        """``(status, parsed JSON)`` of one GET; the only network call in this module."""
        import aiohttp

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(
                    BASE + path, params=params, headers=headers
                ) as response:
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("Alpaca response too large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    try:
                        data = json.loads(raw) if raw.strip() else {}
                    except ValueError:
                        data = {}
                    return response.status, data
        except (aiohttp.ClientError, TimeoutError):
            raise ValueError("Alpaca market data unavailable") from None

    def _pace(self):
        now = self.clock()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= CALLS_PER_MINUTE:
            raise ValueError(
                "Alpaca market data call limit reached; try again in a moment"
            )
        self._calls.append(now)

    @staticmethod
    def _headers(key_id, secret):
        return {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        }

    @staticmethod
    def _answer(status, data):
        if status == 200:
            return data if isinstance(data, dict) else {}
        if status in (401, 403):
            raise ValueError(
                "Alpaca rejected the key: check the key ID and secret in Settings"
            )
        if status == 429:
            raise ValueError(
                "Alpaca is rate limiting market data; try again in a moment"
            )
        message = data.get("message") if isinstance(data, dict) else ""
        safe = re.sub(r"[^A-Za-z0-9 :_.,-]", "", str(message or ""))[:80]
        raise ValueError(
            f"Alpaca market data request failed (HTTP {status}) {safe}".strip()
        )

    async def _get(self, user_id, path, params):
        pair = self._read(user_id)
        if not pair:
            raise ValueError("Add an Alpaca market data key in Settings → Trading 212")
        self._pace()
        status, data = await self._fetch(path, params, self._headers(*pair))
        return self._answer(status, data)

    async def check(self, key_id, secret):
        """Is this key accepted for market data? Nothing is saved; one snapshot is read."""
        key_id, secret = _check_pair(key_id, secret)
        self._pace()
        status, data = await self._fetch(
            "/v2/stocks/snapshots",
            {"symbols": "SPY", "feed": FEED},
            self._headers(key_id, secret),
        )
        snaps = self._snapshots(self._answer(status, data))
        if "SPY" not in snaps:
            raise ValueError("Alpaca accepted the key but returned no price for SPY")
        return {"ok": True, "feed": FEED, "checked": "SPY"}

    # ── prices ──

    def symbols(self):
        return list(WATCHLIST)

    @staticmethod
    def _snapshots(answer):
        found = (
            answer.get("snapshots")
            if isinstance(answer.get("snapshots"), dict)
            else answer
        )
        return {
            k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, dict)
        }

    def _row(self, symbol, snap):
        quote = snap.get("latestQuote") or {}
        trade = snap.get("latestTrade") or {}
        daily = snap.get("dailyBar") or {}
        bid, ask = _decimal(quote.get("bp")), _decimal(quote.get("ap"))
        last = _decimal(trade.get("p")) or _decimal(daily.get("c"))
        if (
            not bid
            or not ask
            or not last
            or bid <= 0
            or ask <= 0
            or last <= 0
            or bid > ask
        ):
            return None
        volume = _decimal(daily.get("v"), Decimal(0))
        average = _decimal(daily.get("vw")) or last
        high, low = _decimal(daily.get("h")), _decimal(daily.get("l"))
        opened = _decimal(daily.get("o"))
        stamp = _timestamp(quote.get("t"))
        return {
            "symbol": symbol,
            "pair": symbol,
            "price": str(last),
            "bid": str(bid),
            "ask": str(ask),
            "spread_pct": str(round((ask - bid) / ask * 100, 3)),
            "volume_24h_usd": round(float(volume * average)),
            "range_24h_pct": (
                str(round((high - low) / last * 100, 2)) if high and low else None
            ),
            "change_since_open_pct": (
                str(round((last / opened - 1) * 100, 2)) if opened else None
            ),
            "quote_age_sec": (
                max(0, round(self.clock() - stamp)) if stamp is not None else None
            ),
        }

    async def rows(self, user_id, symbols):
        """``{symbol: row}`` for the symbols Alpaca priced, one request per 100."""
        names = list(dict.fromkeys(s for s in symbols if isinstance(s, str)))
        found = {}
        for start in range(0, len(names), SNAPSHOT_CHUNK):
            chunk = names[start : start + SNAPSHOT_CHUNK]
            answer = await self._get(
                user_id,
                "/v2/stocks/snapshots",
                {"symbols": ",".join(chunk), "feed": FEED},
            )
            for symbol, snap in self._snapshots(answer).items():
                row = self._row(symbol, snap)
                if row and symbol in chunk:
                    found[symbol] = row
        return found

    async def touch(self, user_id, symbol):
        """``(bid, ask, last)`` of one stock; a quote too old to be live is no price."""
        row = (await self.rows(user_id, [symbol])).get(symbol)
        if not row:
            return None, None, None
        age = row["quote_age_sec"]
        if age is None or age > MAX_QUOTE_AGE_SEC:
            return None, None, None
        return Decimal(row["bid"]), Decimal(row["ask"]), Decimal(row["price"])

    async def candles(self, user_id, symbol, timeframe="15m", count=96):
        if timeframe not in INTERVALS:
            raise ValueError("timeframe must be one of " + ", ".join(INTERVALS))
        count = max(1, min(int(count), 300))
        frame, minutes = INTERVALS[timeframe]
        # IEX trades about nine hours a day, five days a week: ask far enough back for
        # ``count`` bars, newest first, then hand them over oldest first.
        per_day = 1 if minutes >= 1440 else 540 // minutes
        days = min(ceil(count / per_day * 7 / 5) + 3, 365)
        start = datetime.fromtimestamp(
            self.clock() - days * 86400, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        answer = await self._get(
            user_id,
            "/v2/stocks/bars",
            {
                "symbols": symbol,
                "timeframe": frame,
                "start": start,
                "limit": count,
                "sort": "desc",
                "feed": FEED,
                "adjustment": "raw",
            },
        )
        series = (answer.get("bars") or {}).get(symbol) or []
        rows = []
        for bar in reversed(series):
            stamp = _timestamp(bar.get("t"))
            if stamp is None:
                continue
            rows.append(
                {
                    "t": int(stamp * 1000),
                    "o": str(bar.get("o")),
                    "h": str(bar.get("h")),
                    "l": str(bar.get("l")),
                    "c": str(bar.get("c")),
                    "v": str(bar.get("v")),
                }
            )
        return {"pair": symbol, "timeframe": timeframe, "candles": rows}


service = AlpacaMarket()
