"""Scorecard rules: prices in, calls logged, outcomes recomputed. Never touches money."""

import os
import tempfile
import unittest
from unittest.mock import patch

from condor import crypto_com_scorecard as sc

T0 = 1_800_000_000
HOUR = 3600


def coin(symbol, price):
    return {"symbol": symbol, "price_native": {"currency": "USD", "amount": str(price)}}


class ScorecardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def prices(self, symbol, points):
        for offset, price in points:
            sc.record_snapshot(1, [coin(symbol, price)], now=T0 + offset)

    def call(self, symbol="SOL", verdict="trade", target=10, stop=5, hours=24, now=T0):
        return sc.record_prediction(
            1, symbol, verdict, target, stop, hours, "note", now=now
        )

    def test_snapshots_are_throttled_per_coin_and_bad_rows_ignored(self):
        rows = [
            coin("SOL", 90),
            coin("BTC", 70000),
            {"symbol": "X"},
            coin("bad sym", 1),
            coin("ETH", 0),
        ]
        self.assertEqual(sc.record_snapshot(1, rows, now=T0), 2)
        self.assertEqual(sc.record_snapshot(1, rows, now=T0 + 60), 0)
        self.assertEqual(
            sc.record_snapshot(1, rows, now=T0 + sc.SNAPSHOT_MIN_GAP_SEC + 1), 2
        )

    def test_a_call_needs_a_recent_price_and_valid_inputs(self):
        with self.assertRaises(ValueError):
            self.call()
        self.prices("SOL", [(0, 90)])
        for bad in (
            {"symbol": "USDT"},
            {"verdict": "buy"},
            {"target": 0},
            {"target": "abc"},
            {"stop": 101},
            {"hours": 0},
            {"hours": 1000},
        ):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.call(**bad)
        with self.assertRaises(ValueError):
            self.call(now=T0 + sc.ENTRY_MAX_AGE_SEC + 5)
        self.assertTrue(self.call()["recorded"])

    def test_one_open_call_per_coin(self):
        self.prices("SOL", [(0, 90)])
        first = self.call()
        again = self.call(now=T0 + 60)
        self.assertFalse(again["recorded"])
        self.assertEqual(again["id"], first["id"])
        self.prices("SOL", [(25 * HOUR, 90)])
        self.assertTrue(self.call(now=T0 + 25 * HOUR)["recorded"])

    def test_target_stop_expiry_and_open_outcomes(self):
        pred = {
            "ts": 0,
            "entry": 100.0,
            "target_pct": 10,
            "stop_pct": 5,
            "horizon_h": 24,
        }
        end = 24 * HOUR

        def run(series, now=end + 1):
            return sc.evaluate(pred, series, now, cost_pct=5)

        hit = run([(HOUR, 103), (2 * HOUR, 111), (3 * HOUR, 90)])
        self.assertEqual(
            (hit["outcome"], hit["gross_pct"], hit["net_pct"]), ("target", 10, 5)
        )
        stopped = run([(HOUR, 94), (2 * HOUR, 120)])
        self.assertEqual(
            (stopped["outcome"], stopped["gross_pct"], stopped["net_pct"]),
            ("stopped", -5, -10),
        )
        expired = run([(HOUR, 101), (end, 104), (end + HOUR, 150)])
        self.assertEqual(expired["outcome"], "expired")
        self.assertAlmostEqual(expired["gross_pct"], 4.0)
        self.assertAlmostEqual(expired["net_pct"], -1.0)
        self.assertEqual(run([(HOUR, 101)], now=2 * HOUR)["status"], "open")
        self.assertEqual(run([])["status"], "nodata")
        # a line already crossed settles the call even before the window ends
        self.assertEqual(run([(HOUR, 112)], now=2 * HOUR)["outcome"], "target")

    def test_report_scores_calls_against_costs_and_a_baseline(self):
        for symbol, entry, later in (
            ("SOL", 100, 112),
            ("BTC", 100, 101),
            ("ETH", 100, 99),
            ("USDT", 1, 1),
        ):
            self.prices(symbol, [(0, entry), (12 * HOUR, later)])
        self.assertTrue(self.call("SOL", "trade", target=10, stop=5)["recorded"])
        self.assertTrue(self.call("ETH", "pass", target=6, stop=5)["recorded"])
        before = sc.report(1, now=T0 + HOUR)
        self.assertEqual(before["trade_calls"]["open"], 1)
        report = sc.report(1, now=T0 + 25 * HOUR, cost_pct=5)
        trade = report["trade_calls"]
        self.assertEqual(
            (trade["resolved"], trade["hit_target"], trade["beat_costs"]), (1, 1, 1)
        )
        self.assertAlmostEqual(trade["avg_net_pct"], 5.0)
        passed = report["pass_calls"]
        self.assertEqual(passed["resolved"], 1)
        self.assertLess(passed["avg_net_pct"], 0)
        # baseline averages SOL/BTC/ETH (USDT excluded): (12 + 1 - 1) / 3 = 4
        self.assertAlmostEqual(report["baseline_gross_pct"], 4.0)
        self.assertFalse(report["enough_data"])
        buckets = {b["multiple"]: b for b in report["by_cost_multiple"]}
        self.assertEqual(buckets[1.0]["resolved"], 2)
        self.assertEqual(
            buckets[2.0]["resolved"], 1
        )  # only the 10% call clears 2x of 5%
        self.assertAlmostEqual(buckets[2.0]["avg_net_pct"], 5.0)
        # a cheaper cost assumption re-scores the whole history
        cheaper = sc.report(1, now=T0 + 25 * HOUR, cost_pct=2)
        self.assertAlmostEqual(cheaper["trade_calls"]["avg_net_pct"], 8.0)

    def test_the_reported_cost_has_at_most_two_decimals_and_scoring_keeps_the_exact_one(
        self,
    ):
        # 0.1 + 0.11 is 0.21000000000000002 as a float; that must never reach the screen.
        noisy = 0.1 + 0.11
        self.assertNotEqual(noisy, 0.21)
        report = sc.report(1, now=T0, cost_pct=noisy, venue="kraken_xstocks")
        self.assertEqual(report["assumptions"]["round_trip_cost_pct"], 0.21)
        self.assertEqual(repr(report["assumptions"]["round_trip_cost_pct"]), "0.21")
        self.assertEqual(
            sc.report(1, now=T0, cost_pct=1.25)["assumptions"]["round_trip_cost_pct"],
            1.25,
        )
        for bucket in report["by_cost_multiple"]:
            self.assertLessEqual(len(repr(bucket["min_target_pct"]).split(".")[-1]), 4)

    def test_the_basis_names_the_venue_it_describes(self):
        crypto = sc.report(1, now=T0)["assumptions"]["basis"]
        self.assertTrue(crypto.startswith("Crypto.com estimate"))
        self.assertNotIn("Exchange", crypto)
        other = sc.report(1, now=T0, venue="revolut_x")["assumptions"]["basis"]
        self.assertNotIn("Crypto.com", other)

    def test_default_cost_is_the_exchange_round_trip_estimate(self):
        self.assertEqual(sc.ROUND_TRIP_COST_PCT, 1.2)

    def test_only_open_calls_keep_a_coin_tracked(self):
        self.prices("SOL", [(0, 90)])
        self.call(hours=2)
        self.assertEqual(sc.tracked_symbols(1, now=T0 + HOUR), ["SOL"])
        self.assertEqual(sc.tracked_symbols(1, now=T0 + 3 * HOUR), [])

    def test_empty_history_reports_cleanly(self):
        report = sc.report(1, now=T0)
        self.assertEqual(report["trade_calls"]["total"], 0)
        self.assertIsNone(report["baseline_gross_pct"])
        self.assertIsNone(report["edge_vs_baseline_pct"])
        self.assertEqual(report["prices"]["snapshots"], 0)


if __name__ == "__main__":
    unittest.main()
