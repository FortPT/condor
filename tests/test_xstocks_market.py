"""Kraken xStocks public data: US session clock and public-data parsing. No network, no money."""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from condor import xstocks_market as xs


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


ASSET_PAIRS = {
    "AAPLxUSD": {"wsname": "AAPLx/USD", "status": "online"},
    "TSLAxUSD": {"wsname": "TSLAx/USD", "status": "online"},
    "SPYxUSD": {"wsname": "SPYx/USD", "status": "online"},
    "TSLASPVUSD": {"wsname": "TSLAx/USD", "status": "online"},
    "OLDxUSD": {"wsname": "OLDx/USD", "status": "cancel_only"},
}


def ticker(bid, ask, last, volume, high, low, opened):
    return {
        "b": [bid, "1", "1.0"],
        "a": [ask, "1", "1.0"],
        "c": [last, "0.5"],
        "v": ["0", volume],
        "h": ["0", high],
        "l": ["0", low],
        "o": opened,
    }


TICKERS = {
    "AAPLxUSD": ticker(
        "345.33", "345.54", "345.41", "117", "347.18", "344.54", "346.84"
    ),
    "TSLAxUSD": ticker("401.44", "401.46", "401.45", "1480", "415.0", "400.5", "398.0"),
    "SPYxUSD": ticker("771.14", "771.15", "771.15", "163", "772.0", "769.0", "770.0"),
}


class FakeXStocks(xs.XStocksMarket):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def _get(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "AssetPairs":
            return ASSET_PAIRS
        if path == "Ticker":
            return {p: TICKERS[p] for p in params["pair"].split(",") if p in TICKERS}
        if path == "OHLC":
            rows = [
                [1793200000 + i * 900, "1", "2", "0.5", "1.5", "1.2", "10", 3]
                for i in range(5)
            ]
            return {params["pair"]: rows, "last": 1}
        raise AssertionError(path)


class MarketHoursTests(unittest.TestCase):
    def test_open_in_daylight_time(self):
        # Wed 28 Oct 2026, 15:00 UTC = 11:00 EDT
        market = xs.us_market_open(utc(2026, 10, 28, 15, 0))
        self.assertTrue(market["open"])
        self.assertEqual(market["minutes_to_close"], 300)

    def test_closed_before_the_bell_and_after_it(self):
        self.assertFalse(xs.us_market_open(utc(2026, 10, 28, 13, 29))["open"])
        self.assertTrue(xs.us_market_open(utc(2026, 10, 28, 13, 30))["open"])
        self.assertTrue(xs.us_market_open(utc(2026, 10, 28, 19, 59))["open"])
        self.assertFalse(xs.us_market_open(utc(2026, 10, 28, 20, 0))["open"])

    def test_the_hour_shifts_when_daylight_saving_ends(self):
        # DST ends Sun 1 Nov 2026: Wed 4 Nov the session is 14:30-21:00 UTC
        self.assertFalse(xs.us_market_open(utc(2026, 11, 4, 14, 15))["open"])
        self.assertTrue(xs.us_market_open(utc(2026, 11, 4, 14, 45))["open"])
        self.assertTrue(xs.us_market_open(utc(2026, 11, 4, 20, 45))["open"])
        self.assertFalse(xs.us_market_open(utc(2026, 11, 4, 21, 0))["open"])

    def test_closed_at_the_weekend(self):
        # Sat 31 Oct 2026 and Sun 1 Nov 2026
        self.assertFalse(xs.us_market_open(utc(2026, 10, 31, 15, 0))["open"])
        self.assertFalse(xs.us_market_open(utc(2026, 11, 1, 15, 0))["open"])

    def test_the_spring_change_is_honoured_too(self):
        # DST starts Sun 8 Mar 2026: Mon 9 Mar 09:30 EDT = 13:30 UTC
        self.assertTrue(xs.us_market_open(utc(2026, 3, 9, 13, 30))["open"])
        self.assertFalse(xs.us_market_open(utc(2026, 3, 9, 13, 29))["open"])


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()
        self.svc = FakeXStocks()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def test_pairs_keep_only_online_usd_stock_tokens(self):
        pairs = await self.svc.pairs()
        self.assertEqual(
            pairs, {"AAPL": "AAPLxUSD", "TSLA": "TSLAxUSD", "SPY": "SPYxUSD"}
        )

    def test_a_ticker_row_has_spread_range_and_change_since_open(self):
        tsla = self.svc._row("TSLA", "TSLAxUSD", TICKERS["TSLAxUSD"])
        self.assertEqual(tsla["symbol"], "TSLA")
        self.assertEqual(tsla["spread_pct"], "0.005")
        self.assertEqual(tsla["range_24h_pct"], "3.61")
        self.assertEqual(tsla["change_since_open_pct"], "0.87")
        self.assertIsNone(self.svc._row("", "TSLAxUSD", TICKERS["TSLAxUSD"]))
        self.assertIsNone(self.svc._row("TSLA", "TSLAxUSD", {}))

    async def test_candles_map_the_interval_and_shape(self):
        data = await self.svc.candles("aapl", "15m", 3)
        self.assertEqual(
            self.svc.calls[-1], ("OHLC", {"pair": "AAPLxUSD", "interval": 15})
        )
        self.assertEqual(len(data["candles"]), 3)
        self.assertEqual(data["candles"][0]["t"] % 1000, 0)
        with self.assertRaises(ValueError):
            await self.svc.candles("AAPL", "2m")
        with self.assertRaises(ValueError):
            await self.svc.candles("ZZZZ", "15m")

    def test_symbols_are_validated(self):
        for bad in ("", "A B", "AAPL;x", "A" * 13):
            with self.assertRaises(ValueError):
                xs.symbol_of(bad)
        self.assertEqual(xs.symbol_of("nvda"), "NVDA")


class _FakeContent:
    """Like aiohttp's stream: ``read(n)`` hands back what has arrived, not the body."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def read(self, n=-1):
        return self._chunks[0]

    async def iter_chunked(self, size):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, chunks, status=200):
        self.content = _FakeContent(chunks)
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, chunks, status=200):
        self.chunks, self.status, self.requests = chunks, status, []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, params=None):
        self.requests.append((url, dict(params or {})))
        return _FakeResponse(self.chunks, self.status)


class PublicReadTests(unittest.IsolatedAsyncioTestCase):
    """The chart under the Kraken Trade tab read a big OHLC answer cut short."""

    def _patched(self, session):
        return patch("aiohttp.ClientSession", session)

    async def test_a_large_candle_answer_arriving_in_several_chunks_is_read_whole(self):
        rows = [
            [1793200000 + i * 3600, "1", "2", "0.5", "1.5", "1.2", "10", 3]
            for i in range(720)
        ]
        body = json.dumps(
            {"error": [], "result": {"MSTRxUSD": rows, "last": 1793200000}}
        ).encode()
        cut = [body[:10000], body[10000:25000], body[25000:]]
        session = _FakeSession(cut)
        with self._patched(session):
            result = await xs.XStocksMarket()._get(
                "OHLC", {"pair": "MSTRxUSD", "interval": 60}
            )
        self.assertEqual(len(result["MSTRxUSD"]), 720)
        self.assertEqual(session.requests[0][1]["asset_class"], xs.ASSET_CLASS)
        self.assertEqual(session.requests[0][1]["interval"], 60)

    async def test_the_selected_stock_candles_load_through_the_real_reader(self):
        market = xs.XStocksMarket()
        market._pairs = (time.monotonic(), {"MSTRxUSD": {"wsname": "MSTRx/USD"}})
        rows = [
            [1793200000 + i * 3600, "1", "2", "0.5", "1.5", "1.2", "10", 3]
            for i in range(720)
        ]
        body = json.dumps(
            {"error": [], "result": {"MSTRxUSD": rows, "last": 1}}
        ).encode()
        session = _FakeSession([body[:30000], body[30000:]])
        with self._patched(session):
            data = await market.candles("MSTR", "1h", 300)
        self.assertEqual(data["pair"], "MSTRxUSD")
        self.assertEqual(len(data["candles"]), 300)

    async def test_an_empty_series_is_an_empty_list_not_a_failure(self):
        market = xs.XStocksMarket()
        market._pairs = (time.monotonic(), {"MSTRxUSD": {"wsname": "MSTRx/USD"}})
        body = json.dumps({"error": [], "result": {"MSTRxUSD": [], "last": 0}}).encode()
        with self._patched(_FakeSession([body])):
            data = await market.candles("MSTR", "1h", 96)
        self.assertEqual(data["candles"], [])

    async def test_a_rate_limited_answer_says_so_instead_of_data_unavailable(self):
        with self._patched(_FakeSession([b'{"error":[],"result":{}}'], status=429)):
            with self.assertRaisesRegex(ValueError, "rate limiting"):
                await xs.XStocksMarket()._get("Ticker", {"pair": "AAPLxUSD"})

    async def test_a_body_that_is_not_json_is_still_reported_as_unavailable(self):
        with self._patched(_FakeSession([b"<html>"])):
            with self.assertRaisesRegex(ValueError, "public data unavailable"):
                await xs.XStocksMarket()._get("Ticker", {"pair": "AAPLxUSD"})


class SessionClockRouteTests(unittest.IsolatedAsyncioTestCase):
    """The stock scheduler reads ``market.open`` from the old path and the new one."""

    async def test_both_paths_answer_the_session_clock_without_any_account(self):
        from condor.web.models import WebUser
        from condor.web.routes import kraken_xstocks as route

        user = WebUser(id=1, role="admin")
        new = await route.market(user)
        old = await route.legacy_status(user)
        for body in (new, old):
            self.assertIn("open", body["market"])
            self.assertIn("minutes_to_close", body["market"])
            self.assertEqual(body["quote"], "USD")
        self.assertEqual(new["market"]["open"], old["market"]["open"])

    def test_the_old_path_is_still_registered_next_to_the_new_one(self):
        from condor.web.routes import kraken_xstocks as route

        paths = {r.path for r in route.router.routes} | {
            r.path for r in route.legacy_router.routes
        }
        self.assertIn("/kraken-xstocks/market", paths)
        self.assertIn("/xstocks-paper/status", paths)


if __name__ == "__main__":
    unittest.main()
