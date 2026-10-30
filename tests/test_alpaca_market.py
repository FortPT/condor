"""Alpaca IEX market data: credentials, snapshot rows, candles and error handling. No network."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from condor import alpaca_market as am

NOW = datetime(2026, 10, 30, 15, 0, 0, tzinfo=timezone.utc).timestamp()
KEY, SECRET = "PKTESTKEYID12345678", "secretvalue0123456789abcdefghijklmnop"


def snap(bid="199.98", ask="200.02", last="200.00", age=5, **daily):
    stamp = datetime.fromtimestamp(NOW - age, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.123456789Z"
    )
    bar = {"o": "198", "h": "202", "l": "197", "c": "200", "v": 1000, "vw": "199.5"}
    bar.update(daily)
    return {
        "latestQuote": {"bp": bid, "ap": ask, "t": stamp},
        "latestTrade": {"p": last},
        "dailyBar": bar,
    }


class FakeAlpaca(am.AlpacaMarket):
    def __init__(self, answers=None, http=200):
        super().__init__()
        self.clock = lambda: NOW
        self.answers, self.http, self.sent = answers or {}, http, []

    async def _fetch(self, path, params, headers):
        self.sent.append((path, dict(params), dict(headers)))
        answer = self.answers(path, params) if callable(self.answers) else self.answers
        return self.http, answer


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def service(self, answers=None, http=200):
        svc = FakeAlpaca(answers, http)
        svc.save_credentials(1, KEY, SECRET)
        return svc


class CredentialTests(Base):
    def test_the_key_is_saved_privately_and_never_returned(self):
        svc = FakeAlpaca()
        self.assertFalse(svc.configured(1))
        out = svc.save_credentials(1, KEY, SECRET)
        self.assertEqual(out, {"configured": True, "feed": "iex"})
        self.assertNotIn(SECRET, json.dumps(svc.status(1)))
        path = am._credentials_path(1)
        self.assertEqual(
            json.loads(path.read_text()), {"key_id": KEY, "secret": SECRET}
        )
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(svc.disconnect(1), {"configured": False, "feed": "iex"})
        self.assertFalse(path.exists())

    def test_a_malformed_key_or_owner_is_refused(self):
        svc = FakeAlpaca()
        for key, secret in (
            ("", SECRET),
            (KEY, ""),
            ("short", SECRET),
            (KEY, "has space in it 0123456789"),
            (KEY + "\n", SECRET + "\x00"),
            (None, SECRET),
        ):
            with self.assertRaisesRegex(ValueError, "Invalid Alpaca key"):
                svc.save_credentials(1, key, secret)
        with self.assertRaises(ValueError):
            svc.save_credentials(0, KEY, SECRET)
        self.assertFalse(svc.configured(1))

    async def test_without_a_key_nothing_is_requested(self):
        svc = FakeAlpaca({"AAPL": snap()})
        with self.assertRaisesRegex(ValueError, "Add an Alpaca market data key"):
            await svc.rows(1, ["AAPL"])
        self.assertEqual(svc.sent, [])


class RowTests(Base):
    async def test_a_snapshot_becomes_the_reference_row_the_crew_reads(self):
        svc = self.service({"AAPL": snap()})
        rows = await svc.rows(1, ["AAPL"])
        row = rows["AAPL"]
        self.assertEqual(
            (row["symbol"], row["pair"], row["price"], row["bid"], row["ask"]),
            ("AAPL", "AAPL", "200.00", "199.98", "200.02"),
        )
        self.assertEqual(row["spread_pct"], "0.020")
        self.assertEqual(row["volume_24h_usd"], round(1000 * 199.5))
        self.assertEqual(row["range_24h_pct"], "2.50")  # (202 - 197) / 200
        self.assertEqual(row["change_since_open_pct"], "1.01")  # 200 / 198
        self.assertEqual(row["quote_age_sec"], 5)
        path, params, headers = svc.sent[0]
        self.assertEqual(path, "/v2/stocks/snapshots")
        self.assertEqual(params, {"symbols": "AAPL", "feed": "iex"})
        self.assertEqual(headers["APCA-API-KEY-ID"], KEY)
        self.assertEqual(headers["APCA-API-SECRET-KEY"], SECRET)

    async def test_a_quote_that_is_missing_or_crossed_is_no_price(self):
        svc = self.service(
            {
                "OK": snap(),
                "CROSSED": snap(bid="201", ask="200"),
                "NOASK": snap(ask="0"),
                "NOQUOTE": {"latestTrade": {"p": "10"}},
                "JUNK": "not a snapshot",
            }
        )
        rows = await svc.rows(1, ["OK", "CROSSED", "NOASK", "NOQUOTE", "JUNK"])
        self.assertEqual(list(rows), ["OK"])

    async def test_a_snapshot_without_todays_bar_still_prices_with_zero_volume(self):
        bare = snap()
        del bare["dailyBar"]
        row = (await self.service({"AAPL": bare}).rows(1, ["AAPL"]))["AAPL"]
        self.assertEqual(row["volume_24h_usd"], 0)
        self.assertIsNone(row["range_24h_pct"])
        self.assertIsNone(row["change_since_open_pct"])

    async def test_the_answer_may_be_nested_under_snapshots(self):
        svc = self.service({"snapshots": {"AAPL": snap()}})
        self.assertEqual(list(await svc.rows(1, ["AAPL"])), ["AAPL"])

    async def test_symbols_are_requested_a_hundred_at_a_time_and_only_asked_ones_return(
        self,
    ):
        names = [f"S{i:03d}" for i in range(250)]
        svc = self.service(
            lambda path, params: {
                **{s: snap() for s in params["symbols"].split(",")},
                "STRAY": snap(),
            }
        )
        rows = await svc.rows(1, names + names[:5])
        self.assertEqual(len(svc.sent), 3)
        self.assertEqual(
            [len(p["symbols"].split(",")) for _, p, _ in svc.sent], [100, 100, 50]
        )
        self.assertEqual(set(rows), set(names))

    async def test_touch_gives_bid_ask_last_and_refuses_a_stale_quote(self):
        svc = self.service(
            {"AAPL": snap(age=10), "OLD": snap(age=am.MAX_QUOTE_AGE_SEC + 1)}
        )
        from decimal import Decimal

        self.assertEqual(
            await svc.touch(1, "AAPL"),
            (Decimal("199.98"), Decimal("200.02"), Decimal("200.00")),
        )
        self.assertEqual(await svc.touch(1, "OLD"), (None, None, None))
        self.assertEqual(await svc.touch(1, "NONE"), (None, None, None))


class CandleTests(Base):
    def bars(self, n):
        return {
            "bars": {
                "AAPL": [
                    {
                        "t": f"2026-10-30T14:{59 - i:02d}:00Z",
                        "o": 1,
                        "h": 2,
                        "l": 0.5,
                        "c": 1.5,
                        "v": 10 + i,
                    }
                    for i in range(n)
                ]
            }
        }

    async def test_candles_come_back_oldest_first_in_the_kraken_shape(self):
        svc = self.service(self.bars(3))  # the newest first, as requested
        out = await svc.candles(1, "AAPL", "15m", 3)
        self.assertEqual((out["pair"], out["timeframe"]), ("AAPL", "15m"))
        stamps = [c["t"] for c in out["candles"]]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(set(out["candles"][0]), {"t", "o", "h", "l", "c", "v"})
        path, params, _ = svc.sent[0]
        self.assertEqual(path, "/v2/stocks/bars")
        self.assertEqual(params["timeframe"], "15Min")
        self.assertEqual(params["limit"], 3)
        self.assertEqual(params["sort"], "desc")
        self.assertEqual(params["feed"], "iex")
        self.assertRegex(params["start"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    async def test_the_lookback_covers_the_bars_asked_for(self):
        svc = self.service(self.bars(1))
        await svc.candles(1, "AAPL", "15m", 96)
        start = datetime.fromisoformat(svc.sent[0][1]["start"].replace("Z", "+00:00"))
        days = (NOW - start.timestamp()) / 86400
        self.assertGreaterEqual(days, 96 / 36)  # 36 fifteen-minute IEX bars a day
        self.assertLessEqual(days, 12)

    async def test_every_timeframe_maps_and_an_unknown_one_is_refused(self):
        svc = self.service(self.bars(1))
        for ours, theirs in (
            ("5m", "5Min"),
            ("30m", "30Min"),
            ("1h", "1Hour"),
            ("4h", "4Hour"),
            ("1D", "1Day"),
        ):
            await svc.candles(1, "AAPL", ours, 5)
            self.assertEqual(svc.sent[-1][1]["timeframe"], theirs)
        with self.assertRaisesRegex(ValueError, "timeframe must be one of"):
            await svc.candles(1, "AAPL", "2m", 5)

    async def test_no_bars_is_an_empty_list_and_the_count_is_capped(self):
        svc = self.service({"bars": {}})
        out = await svc.candles(1, "AAPL", "15m", 9999)
        self.assertEqual(out["candles"], [])
        self.assertEqual(svc.sent[0][1]["limit"], 300)


class ErrorTests(Base):
    async def test_a_rejected_key_says_so_without_echoing_it(self):
        svc = self.service({"message": f"forbidden {KEY} {SECRET}"}, http=403)
        with self.assertRaises(ValueError) as caught:
            await svc.rows(1, ["AAPL"])
        text = str(caught.exception)
        self.assertIn("Alpaca rejected the key", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn(SECRET, text)

    async def test_rate_limits_and_outages_have_their_own_messages(self):
        with self.assertRaisesRegex(ValueError, "rate limiting"):
            await self.service({}, http=429).rows(1, ["AAPL"])
        with self.assertRaisesRegex(ValueError, r"failed \(HTTP 500\)"):
            await self.service({"message": "boom <script>"}, http=500).rows(1, ["AAPL"])

    async def test_the_call_budget_stops_before_alpacas_own_limit(self):
        svc = self.service({"AAPL": snap()})
        for _ in range(am.CALLS_PER_MINUTE):
            await svc.rows(1, ["AAPL"])
        with self.assertRaisesRegex(ValueError, "call limit"):
            await svc.rows(1, ["AAPL"])
        svc.clock = lambda: NOW + 61  # a minute later the window has emptied
        self.assertEqual(list(await svc.rows(1, ["AAPL"])), ["AAPL"])


class CheckTests(Base):
    async def test_a_good_key_is_checked_with_one_snapshot_and_nothing_is_saved(self):
        svc = FakeAlpaca({"SPY": snap()})
        self.assertEqual(
            await svc.check(KEY, SECRET), {"ok": True, "feed": "iex", "checked": "SPY"}
        )
        self.assertEqual(svc.sent[0][1], {"symbols": "SPY", "feed": "iex"})
        self.assertFalse(svc.configured(1))

    async def test_a_bad_key_and_an_empty_answer_fail_the_check(self):
        with self.assertRaisesRegex(ValueError, "rejected the key"):
            await FakeAlpaca({}, http=401).check(KEY, SECRET)
        with self.assertRaisesRegex(ValueError, "no price for SPY"):
            await FakeAlpaca({}).check(KEY, SECRET)
        with self.assertRaisesRegex(ValueError, "Invalid Alpaca key"):
            await FakeAlpaca({"SPY": snap()}).check("x", "y")


class WatchlistTests(unittest.TestCase):
    def test_the_universe_is_distinct_upper_case_symbols(self):
        names = am.service.symbols()
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(n.isalpha() and n == n.upper() for n in names))
        self.assertGreaterEqual(len(names), 80)
        self.assertIn("SPY", names)
        self.assertNotIn("MRNA", names)  # an owner holding the bot must never trade


if __name__ == "__main__":
    unittest.main()
