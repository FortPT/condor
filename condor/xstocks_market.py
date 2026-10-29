"""Kraken's public market data for the tokenized US stocks (xStocks), and the US session clock.

No exchange account, no key and no order: this reads Kraken's public endpoints (asset
pairs, ticker, OHLC) and answers whether the regular US session is open. Trading 212
(:mod:`condor.trading212`) has no quote endpoint, so it uses this for reference prices
and candles, and Revolut X and Trading 212 use its EUR/USD rate to value their
accounts in USD. The stock scheduler reads the session clock through Trading 212's
``/trading212/market`` route. Nothing here can move money.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

BASE = "https://api.kraken.com/0/public/"
ASSET_CLASS = "tokenized_asset"
QUOTE = "USD"
INTERVALS = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1D": 1440}
TICKER_CHUNK = 40
MAX_BYTES = 4 * 1024 * 1024


def symbol_of(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{1,12}", value):
        raise ValueError("Invalid stock symbol")
    return value.upper()


def _decimal(value, default=None):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return default
    return result if result.is_finite() else default


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    day = datetime(year, month, 1, tzinfo=timezone.utc)
    day += timedelta(days=(weekday - day.weekday()) % 7)
    return day + timedelta(weeks=n - 1)


def new_york_offset(utc: datetime) -> timedelta:
    """US Eastern offset from UTC, with the daylight-saving rule (no tz database needed)."""
    start = _nth_weekday(utc.year, 3, 6, 2).replace(
        hour=7
    )  # 2nd Sunday of March, 02:00 EST
    end = _nth_weekday(utc.year, 11, 6, 1).replace(
        hour=6
    )  # 1st Sunday of November, 02:00 EDT
    return timedelta(hours=-4) if start <= utc < end else timedelta(hours=-5)


def us_market_open(now: float | None = None) -> dict:
    """Is the regular US session (09:30-16:00 New York, Monday to Friday) open?

    Exchange holidays are not modelled; the field says so.
    """
    utc = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc)
    local = utc + new_york_offset(utc)
    minutes = local.hour * 60 + local.minute
    open_now = local.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60
    left = 16 * 60 - minutes if open_now else 0
    return {
        "open": open_now,
        "new_york_time": local.strftime("%a %H:%M"),
        "minutes_to_close": left,
        "note": "Regular session only; exchange holidays are not modelled.",
    }


class XStocksMarket:
    def __init__(self):
        self._pairs = (0.0, {})
        self._eur_usd = (0.0, None)

    async def eur_usd(self):
        """USD per euro from Kraken's plain EURUSD pair, or None (cached a minute)."""
        stamp, cached = self._eur_usd
        if cached and time.monotonic() - stamp < 60:
            return cached
        try:
            result = await self._get("Ticker", {"pair": "EURUSD"}, asset_class=False)
            ticker = next(iter(result.values()), {})
            rate = _decimal(ticker["c"][0])
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        if not rate or rate <= 0:
            return None
        self._eur_usd = (time.monotonic(), rate)
        return rate

    async def _get(self, path: str, params: dict, *, asset_class: bool = True):
        import aiohttp

        query = dict(params)
        if asset_class and path != "AssetPairs":  # that endpoint names it aclass_base
            query["asset_class"] = ASSET_CLASS
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(BASE + path, params=query) as response:
                    # The whole body, chunk by chunk. ``content.read(n)`` returns only
                    # what has arrived so far, so a large answer (an OHLC series of
                    # hundreds of candles) was cut short, failed to parse and surfaced
                    # as "Kraken public data unavailable" under a chart whose market
                    # table, a small answer, loaded fine.
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("Kraken response too large")
                        chunks.append(chunk)
                    if response.status == 429:
                        raise ValueError(
                            "Kraken is rate limiting public requests; try again in a moment"
                        )
                    data = json.loads(b"".join(chunks))
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
            raise ValueError("Kraken public data unavailable") from None
        if not isinstance(data, dict) or data.get("error"):
            errors = data.get("error") if isinstance(data, dict) else None
            safe = re.sub(
                r"[^A-Za-z0-9 :_.-]", "", ";".join(errors or ["bad response"])
            )
            raise ValueError(f"Kraken rejected the request: {safe[:80]}")
        return data.get("result") or {}

    async def pair_rows(self) -> dict:
        """``{"AAPLxUSD": <AssetPairs row>, ...}`` for the online USD xStocks (cached).

        The live service reads the lot, tick and minimum-order fields from these
        rows, so both share one cache and one definition of "an xStock pair".
        """
        stamp, cached = self._pairs
        if cached and time.monotonic() - stamp < 3600:
            return cached
        result = await self._get("AssetPairs", {"aclass_base": ASSET_CLASS})
        found = {}
        for name, row in result.items():
            base = str(row.get("wsname", "")).split("/")[0]
            if (
                name.endswith("xUSD")
                and "SPV" not in name
                and row.get("status") == "online"
                and base.endswith("x")
            ):
                found[name] = row
        self._pairs = (time.monotonic(), found)
        return found

    async def pairs(self) -> dict:
        """``{"AAPL": "AAPLxUSD", ...}`` for the online USD-quoted xStocks."""
        return {
            str(row["wsname"]).split("/")[0][:-1].upper(): name
            for name, row in (await self.pair_rows()).items()
        }

    @staticmethod
    def _row(symbol, pair, t):
        if not symbol:
            return None
        try:
            bid, ask, last = (_decimal(t[k][0]) for k in ("b", "a", "c"))
            volume = _decimal(t["v"][1], Decimal(0))
            high, low = _decimal(t["h"][1]), _decimal(t["l"][1])
            opened = _decimal(t.get("o"))
        except (KeyError, IndexError, TypeError):
            return None
        if not bid or not ask or not last or last <= 0:
            return None
        return {
            "symbol": symbol,
            "pair": pair,
            "price": str(last),
            "bid": str(bid),
            "ask": str(ask),
            "spread_pct": str(round((ask - bid) / ask * 100, 3)),
            "volume_24h_usd": round(float(volume * last)),
            "range_24h_pct": (
                str(round((high - low) / last * 100, 2)) if high and low else None
            ),
            "change_since_open_pct": (
                str(round((last / opened - 1) * 100, 2)) if opened else None
            ),
        }

    async def candles(self, symbol: str, timeframe: str = "15m", count: int = 96):
        symbol = symbol_of(symbol)
        if timeframe not in INTERVALS:
            raise ValueError("timeframe must be one of " + ", ".join(INTERVALS))
        pair = (await self.pairs()).get(symbol)
        if not pair:
            raise ValueError(f"{symbol} is not an online xStock pair")
        result = await self._get(
            "OHLC", {"pair": pair, "interval": INTERVALS[timeframe]}
        )
        series = next((v for k, v in result.items() if k != "last"), [])
        count = max(1, min(int(count), 300))
        return {
            "pair": pair,
            "timeframe": timeframe,
            "candles": [
                {
                    "t": int(row[0]) * 1000,
                    "o": row[1],
                    "h": row[2],
                    "l": row[3],
                    "c": row[4],
                    "v": row[6],
                }
                for row in series[-count:]
            ],
        }


service = XStocksMarket()
