"""Paper xStocks: market hours, public-data parsing, paper calls. No network, no money."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from condor import crypto_com_scorecard as sc
from condor import xstocks_paper as xs


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


class FakeXStocks(xs.XStocksPaper):
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

    async def test_coins_are_ranked_by_dollar_volume_with_spread_and_range(self):
        data = await self.svc.coins("")
        symbols = [row["symbol"] for row in data["stocks"]]
        self.assertEqual(symbols[0], "TSLA")
        tsla = data["stocks"][0]
        self.assertEqual(tsla["spread_pct"], "0.005")
        self.assertEqual(tsla["range_24h_pct"], "3.61")
        self.assertEqual(tsla["change_since_open_pct"], "0.86")
        self.assertIn("open", data["market"])
        found = await self.svc.coins("aa")
        self.assertEqual([r["symbol"] for r in found["stocks"]], ["AAPL"])

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

    async def test_a_paper_call_is_recorded_and_scored_apart_from_crypto(self):
        result = await self.svc.predict(1, "tsla", "trade", "2", "1", "3", "breakout")
        self.assertTrue(result["recorded"])
        again = await self.svc.predict(1, "TSLA", "pass", "2", "1", "3")
        self.assertFalse(again["recorded"])
        report = self.svc.scorecard(1)
        self.assertEqual(report["trade_calls"]["total"], 1)
        self.assertEqual(
            report["assumptions"]["round_trip_cost_pct"], xs.ROUND_TRIP_COST_PCT
        )
        crypto = sc.report(1)
        self.assertEqual(crypto["trade_calls"]["total"], 0)

    async def test_status_says_paper_and_reports_the_market(self):
        status = await self.svc.status()
        self.assertTrue(status["paper_only"])
        self.assertIn("open", status["market"])

    def test_symbols_are_validated(self):
        for bad in ("", "A B", "AAPL;x", "A" * 13):
            with self.assertRaises(ValueError):
                xs.symbol_of(bad)
        self.assertEqual(xs.symbol_of("nvda"), "NVDA")


if __name__ == "__main__":
    unittest.main()
