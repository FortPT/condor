"""Paper-only xStocks test on Kraken's public market data.

No exchange account, no key and no real orders: this reads Kraken's public
endpoints for the tokenized US stocks (xStocks, quoted in USD), lets an agent log
paper calls, and scores them against later prices after a conservative round-trip
cost. The simulated account that fills paper orders on the live order book is in
:mod:`condor.xstocks_paper_ledger`. Nothing here can move money.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from condor import crypto_com_scorecard as scorecard

VENUE = "xstocks_paper"
BASE = "https://api.kraken.com/0/public/"
ASSET_CLASS = "tokenized_asset"
QUOTE = "USD"
# A taker fee of 0.40% each way (per Kraken Pro's schedule as reported by a support
# article, not verified) plus a little spread. The multiples in the scorecard
# re-score the history at 1.5x, 2x and 3x this cost.
ROUND_TRIP_COST_PCT = 0.85
TOP_COINS = 40
MIN_VOLUME_USD = 50_000
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


class XStocksPaper:
    def __init__(self):
        self._pairs = (0.0, {})

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
                    raw = await response.content.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Kraken response too large")
                    data = json.loads(raw)
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

    async def status(self) -> dict:
        return {
            "paper_only": True,
            "venue": "Kraken xStocks (public data)",
            "quote": QUOTE,
            "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
            "market": us_market_open(),
            "note": (
                "No exchange account, no key and no real orders: every call is "
                "paper, and orders fill on a simulated account."
            ),
        }

    async def coins(self, keyword: str = "", user_id: int = 1) -> dict:
        if not isinstance(keyword, str) or len(keyword) > 40:
            raise ValueError("Enter a stock symbol")
        pairs = await self.pairs()
        key = keyword.strip().upper()
        wanted = {s: p for s, p in pairs.items() if not key or key in s}
        names = list(wanted.values())
        rows = []
        for start in range(0, len(names), TICKER_CHUNK):
            chunk = names[start : start + TICKER_CHUNK]
            tickers = await self._get("Ticker", {"pair": ",".join(chunk)})
            by_pair = {pair: symbol for symbol, pair in wanted.items()}
            for pair, t in tickers.items():
                symbol = by_pair.get(pair) or by_pair.get(pair.replace("/", ""))
                row = self._row(symbol, pair, t)
                if row:
                    rows.append(row)
        rows.sort(key=lambda r: r["volume_24h_usd"], reverse=True)
        rows = rows[: 10 if key else TOP_COINS]
        self._record(rows, user_id)
        return {"quote": QUOTE, "market": us_market_open(), "stocks": rows}

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

    @staticmethod
    def _record(rows, user_id):
        """Feed the scorecard; it must never be able to break a market read."""
        try:
            scorecard.record_snapshot(
                user_id,
                [
                    {"symbol": r["symbol"], "price_native": {"amount": r["price"]}}
                    for r in rows
                ],
                venue=VENUE,
            )
        except Exception:  # noqa: BLE001 - analytics only
            pass

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

    async def predict(
        self, user_id, symbol, verdict, target_pct, stop_pct, horizon_hours, note=""
    ):
        symbol = symbol_of(symbol)
        if scorecard.latest_price(user_id, symbol, venue=VENUE) is None:
            await self.coins(symbol, user_id)
        return scorecard.record_prediction(
            user_id,
            symbol,
            verdict,
            target_pct,
            stop_pct,
            horizon_hours,
            note,
            venue=VENUE,
        )

    def scorecard(self, user_id) -> dict:
        return scorecard.report(user_id, cost_pct=ROUND_TRIP_COST_PCT, venue=VENUE)


service = XStocksPaper()
