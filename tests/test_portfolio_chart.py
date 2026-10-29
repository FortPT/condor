"""The chart's honest series: one baseline, breaks at gaps, deposit markers, net result.

No network and no money: Hummingbot's history is a fake, readings and flows go into a
temp data dir. The live case it is built around: Hummingbot's Kraken (about 9 EUR) was
the only thing recorded until the Exchange, Kraken xStocks and Revolut X (after a
4.27 EUR deposit) started being sampled, so the total stepped 9 -> 52 -> 55 and read as
performance.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from condor import native_flows
from condor import portfolio_chart as chart
from condor import portfolio_history as ph
from condor.web.models import WebUser
from condor.web.routes import portfolio as portfolio_route

USER = 1
T0 = 1_800_000_000  # a multiple of every interval used here
STEP = 300
NOW = T0 + 6 * 3600


def hb(*rows, tokens=None):
    return [
        SimpleNamespace(timestamp=ts, total_usd=total, tokens=dict(tokens or {}))
        for ts, total in rows
    ]


def record(source, start, end, value, step=STEP):
    """Record ``value`` (a number or ``f(ts)``) every ``step`` from ``start`` to ``end``."""
    ts = start
    while ts <= end:
        ph.record(USER, source, value(ts) if callable(value) else value, now=ts)
        ts += step


def hb_series(start, end, value, step=STEP):
    ts, rows = start, []
    while ts <= end:
        rows.append((ts, value(ts) if callable(value) else value))
        ts += step
    return hb(*rows)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def build(self, points=(), since=T0, now=NOW, skip=(), connected=None, step=STEP):
        native = ph.series(USER, since=since - chart.EDGE_MARGIN_SEC, until=now)
        return chart.build(
            USER, list(points), native, since, now, step, skip=skip, connected=connected
        )

    def flow(
        self, amount, ts, status="confirmed", venue="crypto_com_exchange", usd=True
    ):
        native_flows.add_flow(
            USER,
            venue,
            amount=str(amount),
            currency="EUR",
            ts=ts,
            usd_value=str(abs(amount)) if usd else None,
            source="manual",
            status=status,
        )

    def track(self, venue="crypto_com_exchange"):
        """An old confirmed flow makes a venue's flows 'tracked'."""
        self.flow(1, T0 - 10 * 86400, venue=venue)


class BaselineTests(Base):
    def test_the_series_starts_when_every_account_is_recording(self):
        # The live staircase: Hummingbot 9, then the Exchange 43 an hour in, then
        # Revolut X 4 two hours in.
        points = hb_series(T0, NOW, 9.0)
        record(ph.EXCHANGE, T0 + 3600, NOW, 43.0)
        record(ph.REVOLUT, T0 + 7200, NOW, 4.0)
        out = self.build(points)
        self.assertEqual(out["baseline_ts"], T0 + 7200)
        self.assertEqual(out["series_start_ts"], T0 + 7200)
        self.assertEqual(out["series_start_reason"], chart.START_REASON)
        series = out["chart_points"]
        self.assertEqual(series[0]["timestamp"], T0 + 7200)
        self.assertTrue(all(p["timestamp"] >= T0 + 7200 for p in series))
        # No point is a partial total: every one holds all three accounts.
        self.assertEqual({p["total_usd"] for p in series}, {56.0})

    def test_sources_report_their_first_reading(self):
        points = hb_series(T0, NOW, 9.0)
        record(ph.EXCHANGE, T0 + 3600, NOW, 43.0)
        out = self.build(points)
        by_name = {s["source"]: s for s in out["sources"]}
        self.assertEqual(by_name[ph.EXCHANGE]["first_ts"], T0 + 3600)
        self.assertEqual(by_name[ph.EXCHANGE]["status"], "recording")
        self.assertEqual(by_name[chart.HUMMINGBOT]["status"], "recording")

    def test_the_first_reading_is_the_account_first_ever_not_the_first_in_the_window(
        self,
    ):
        record(ph.EXCHANGE, T0 - 86400, NOW, 43.0, step=3600)
        record(ph.EXCHANGE, NOW - 1800, NOW, 43.0)
        out = self.build(since=T0)
        # The Exchange has been recording for a day: nothing to explain.
        self.assertEqual(out["baseline_ts"], T0 - 86400)
        self.assertIsNone(out["series_start_reason"])
        self.assertEqual(out["series_start_ts"], T0)

    def test_a_connected_account_without_readings_is_pending_not_waited_for(self):
        points = hb_series(T0, NOW, 9.0)
        record(ph.EXCHANGE, T0, NOW, 43.0)
        out = self.build(points, connected={ph.EXCHANGE, ph.REVOLUT})
        by_name = {s["source"]: s for s in out["sources"]}
        self.assertEqual(
            by_name[ph.REVOLUT],
            {"source": ph.REVOLUT, "first_ts": None, "status": "pending"},
        )
        self.assertEqual(out["baseline_ts"], T0)
        self.assertEqual(out["chart_points"][0]["total_usd"], 52.0)

    def test_an_account_that_is_not_connected_and_has_no_readings_is_not_listed(self):
        record(ph.EXCHANGE, T0, NOW, 43.0)
        out = self.build(connected={ph.EXCHANGE})
        self.assertEqual([s["source"] for s in out["sources"]], [ph.EXCHANGE])

    def test_a_disconnected_account_with_old_readings_is_left_out(self):
        record(ph.EXCHANGE, T0, NOW, 43.0)
        record(ph.REVOLUT, T0, T0 + 3600, 4.0)
        out = self.build(connected={ph.EXCHANGE})
        by_name = {s["source"]: s["status"] for s in out["sources"]}
        self.assertEqual(by_name[ph.REVOLUT], "disconnected")
        self.assertEqual({p["total_usd"] for p in out["chart_points"]}, {43.0})

    def test_a_skipped_source_is_not_counted_twice_when_hummingbot_has_it(self):
        # Hummingbot's own connector (9.4) and the native reading are the same account.
        points = hb_series(T0, NOW, 9.4)
        record(ph.REVOLUT, T0 + 5400, NOW, 9.4)
        record(ph.EXCHANGE, T0, NOW, 43.0)
        out = self.build(points, skip=(ph.REVOLUT,))
        names = [s["source"] for s in out["sources"]]
        self.assertNotIn(ph.REVOLUT, names)
        # Neither the late xStocks readings move the baseline nor its value is added.
        self.assertEqual(out["baseline_ts"], T0)
        self.assertEqual({p["total_usd"] for p in out["chart_points"]}, {52.4})

    def test_a_native_source_counts_when_hummingbot_does_not_have_it(self):
        record(ph.REVOLUT, T0 + 5400, NOW, 9.4)
        record(ph.EXCHANGE, T0, NOW, 43.0)
        out = self.build()
        self.assertEqual(out["baseline_ts"], T0 + 5400)
        self.assertEqual({p["total_usd"] for p in out["chart_points"]}, {52.4})

    def test_a_window_shorter_than_the_baseline_starts_with_the_window(self):
        record(ph.EXCHANGE, T0 - 7200, NOW, 43.0)
        record(ph.REVOLUT, T0 - 3600, NOW, 4.0)
        out = self.build(since=T0 + 3600)
        self.assertEqual(out["baseline_ts"], T0 - 3600)
        self.assertEqual(out["series_start_ts"], T0 + 3600)
        self.assertIsNone(out["series_start_reason"])
        self.assertEqual(out["chart_points"][0]["timestamp"], T0 + 3600)
        self.assertEqual(out["chart_points"][0]["total_usd"], 47.0)

    def test_hummingbot_history_trailing_the_window_start_is_not_a_late_account(self):
        # The history is served for the window and cached: its first point is a few
        # minutes in, which is not "started recording later".
        points = hb_series(T0 + 420, NOW, 9.0)
        out = self.build(points)
        self.assertEqual(out["baseline_ts"], T0)
        self.assertIsNone(out["series_start_reason"])

    def test_one_reading_is_a_series_of_one_point(self):
        record(ph.EXCHANGE, NOW, NOW, 43.0)
        out = self.build()
        self.assertEqual(len(out["chart_points"]), 1)
        self.assertIsNone(out["result_points"])

    def test_nothing_recorded_is_an_empty_series_with_no_baseline(self):
        out = self.build(connected={ph.EXCHANGE})
        self.assertEqual(out["chart_points"], [])
        self.assertIsNone(out["baseline_ts"])
        self.assertEqual(
            out["sources"],
            [{"source": ph.EXCHANGE, "first_ts": None, "status": "pending"}],
        )


class GapTests(Base):
    def test_a_stretch_without_hummingbot_readings_is_a_break_not_a_fill(self):
        # Hummingbot's Kraken was empty for hours: its points read 0 (no reading).
        gap_from, gap_to = T0 + 3600, T0 + 3 * 3600
        points = hb_series(T0, NOW, lambda ts: 0.0 if gap_from < ts < gap_to else 9.0)
        record(ph.EXCHANGE, T0, NOW, 43.0)
        out = self.build(points)
        inside = [
            p
            for p in out["chart_points"]
            if gap_from + 900 < p["timestamp"] < gap_to - 900
        ]
        self.assertTrue(inside)
        self.assertTrue(all(p["total_usd"] is None for p in inside))
        # Nothing was carried over: the 43 of the Exchange alone is not a point either.
        values = {p["total_usd"] for p in out["chart_points"]}
        self.assertEqual(values, {None, 52.0})
        gaps = [g for g in out["gaps"] if g["source"] == chart.HUMMINGBOT]
        self.assertEqual(
            gaps, [{"source": chart.HUMMINGBOT, "from_ts": gap_from, "to_ts": gap_to}]
        )

    def test_a_gap_at_the_baseline_moves_the_start_to_when_the_line_can_begin(self):
        # Revolut X starts at +2h, but Hummingbot has no reading until +3h: the first
        # honest point is where every account is back.
        points = hb_series(
            T0, NOW, lambda ts: 0.0 if T0 + 3600 < ts < T0 + 3 * 3600 else 9.0
        )
        record(ph.EXCHANGE, T0, NOW, 43.0)
        record(ph.REVOLUT, T0 + 7200, NOW, 4.0)
        out = self.build(points)
        self.assertEqual(out["baseline_ts"], T0 + 7200)
        self.assertGreaterEqual(out["series_start_ts"], T0 + 3 * 3600 - 900)
        self.assertEqual(out["series_start_ts"], out["chart_points"][0]["timestamp"])
        self.assertIsNotNone(out["chart_points"][0]["total_usd"])
        self.assertEqual(out["series_start_reason"], chart.START_REASON)
        self.assertEqual(
            len([g for g in out["gaps"] if g["source"] == chart.HUMMINGBOT]), 1
        )

    def test_a_gap_is_cut_even_when_no_source_recorded_anything_in_it(self):
        record(ph.EXCHANGE, T0, T0 + 3600, 43.0)
        record(ph.EXCHANGE, T0 + 3 * 3600, NOW, 43.0)
        out = self.build()
        nulls = [p for p in out["chart_points"] if p["total_usd"] is None]
        self.assertEqual(len(nulls), 1)
        self.assertEqual(nulls[0]["timestamp"], T0 + 2 * 3600)
        self.assertEqual(out["gaps"][0]["source"], ph.EXCHANGE)

    def test_a_short_pause_inside_two_intervals_is_not_a_gap(self):
        record(ph.EXCHANGE, T0, T0 + 3600, 43.0)
        record(ph.EXCHANGE, T0 + 3600 + 2 * STEP, NOW, 43.0)
        out = self.build()
        self.assertEqual(out["gaps"], [])
        self.assertNotIn(None, [p["total_usd"] for p in out["chart_points"]])

    def test_a_source_that_stopped_recording_is_a_trailing_gap_and_the_line_stops(self):
        record(ph.EXCHANGE, T0, NOW, 43.0)
        record(ph.REVOLUT, T0, NOW - 3600, 4.0)
        out = self.build()
        self.assertEqual(
            out["gaps"],
            [{"source": ph.REVOLUT, "from_ts": NOW - 3600, "to_ts": NOW}],
        )
        self.assertEqual(out["chart_points"][-1]["total_usd"], 47.0)
        self.assertLessEqual(out["chart_points"][-1]["timestamp"], NOW - 3600 + 600)

    def test_a_gap_before_the_series_start_is_not_reported(self):
        record(ph.EXCHANGE, T0 - 10 * 3600, T0 - 8 * 3600, 43.0)
        record(ph.EXCHANGE, T0, NOW, 43.0)
        out = self.build(since=T0)
        self.assertEqual(out["gaps"], [])


class FlowTests(Base):
    def setUp(self):
        super().setUp()
        record(ph.EXCHANGE, T0, NOW, 43.0)
        record(ph.REVOLUT, T0, NOW, lambda ts: 4.0 if ts < T0 + 7200 else 8.27)

    def test_confirmed_flows_inside_the_window_are_markers(self):
        self.flow(4.27, T0 + 7200 + 100, venue="revolut_x")
        self.flow(-3, T0 + 4000, venue="crypto_com_exchange")
        self.flow(9, T0 - 3600, venue="crypto_com_exchange")  # before the window
        out = self.build()
        self.assertEqual(
            [
                (f["ts"], f["venue"], f["amount"], f["usd_value_at_time"])
                for f in out["flows"]
            ],
            [
                (T0 + 4000, ph.EXCHANGE, "-3", -3.0),
                (T0 + 7300, ph.REVOLUT, "4.27", 4.27),
            ],
        )
        self.assertEqual(out["flows"][1]["currency"], "EUR")
        self.assertEqual(out["flows"][1]["source"], "manual")
        self.assertEqual(out["pending_flows"], 0)

    def test_pending_and_dismissed_flows_are_never_markers(self):
        self.flow(5, T0 + 4000, status="pending")
        self.flow(6, T0 + 4500, status="pending", venue="revolut_x")
        out = self.build()
        self.assertEqual(out["flows"], [])
        self.assertEqual(out["pending_flows"], 2)

    def test_a_flow_exactly_at_the_series_start_belongs_before_it(self):
        self.flow(5, T0)
        self.assertEqual(self.build()["flows"], [])

    def test_hummingbot_accounts_have_no_flow_data_and_say_so(self):
        out = self.build(hb_series(T0, NOW, 9.0))
        self.assertIn("Hummingbot", out["flows_note"])
        self.assertNotIn("Hummingbot", [f["venue"] for f in out["flows"]])

    def test_untracked_venues_are_named_in_the_note(self):
        out = self.build()
        self.assertIn(ph.EXCHANGE, out["flows_note"])
        self.assertIn("not tracked", out["flows_note"])

    def test_tracked_venues_without_hummingbot_have_no_note(self):
        self.track("crypto_com_exchange")
        self.track("revolut_x")
        self.assertIsNone(self.build()["flows_note"])


class ResultTests(Base):
    def setUp(self):
        super().setUp()
        self.track("crypto_com_exchange")
        self.track("revolut_x")

    def test_a_deposit_is_taken_out_so_the_result_stays_flat(self):
        # 4 EUR on Revolut X, 4.27 deposited at +2h: the line steps, the result does not.
        record(ph.EXCHANGE, T0, NOW, 43.0)
        record(ph.REVOLUT, T0, NOW, lambda ts: 4.0 if ts < T0 + 7200 else 8.27)
        self.flow(4.27, T0 + 7200 - 100, venue="revolut_x")
        out = self.build()
        totals = {p["total_usd"] for p in out["chart_points"]}
        self.assertEqual(totals, {47.0, 51.27})
        results = [p["result_usd"] for p in out["result_points"]]
        self.assertTrue(all(abs(r) < 1e-9 for r in results))
        self.assertEqual(out["result_points"][0]["result_usd"], 0.0)
        self.assertIsNone(out["result_reason"])
        self.assertEqual(out["result_not_netted"], [])
        self.assertIsNone(out["result_note"])

    def test_a_real_gain_is_the_result(self):
        record(ph.EXCHANGE, T0, NOW, lambda ts: 43.0 if ts < T0 + 3600 else 45.5)
        out = self.build()
        results = [p["result_usd"] for p in out["result_points"]]
        self.assertEqual(results[0], 0.0)
        self.assertEqual(results[-1], 2.5)

    def test_the_result_is_aligned_with_the_chart_points(self):
        record(ph.EXCHANGE, T0, NOW, 43.0)
        out = self.build()
        self.assertEqual(
            [p["timestamp"] for p in out["result_points"]],
            [p["timestamp"] for p in out["chart_points"]],
        )

    def test_a_withdrawal_is_added_back(self):
        record(ph.EXCHANGE, T0, NOW, lambda ts: 43.0 if ts < T0 + 3600 else 33.0)
        self.flow(-10, T0 + 3600 - 100)
        out = self.build()
        self.assertEqual(out["result_points"][-1]["result_usd"], 0.0)

    def test_a_gap_is_null_in_the_result_too(self):
        record(ph.EXCHANGE, T0, T0 + 3600, 43.0)
        record(ph.EXCHANGE, T0 + 3 * 3600, NOW, 44.0)
        out = self.build()
        pairs = [
            (c["total_usd"], r["result_usd"])
            for c, r in zip(out["chart_points"], out["result_points"])
        ]
        self.assertIn((None, None), pairs)
        self.assertEqual(pairs[-1], (44.0, 1.0))

    def test_hummingbot_is_included_with_a_caveat(self):
        record(ph.EXCHANGE, T0, NOW, 43.0)
        out = self.build(hb_series(T0, NOW, lambda ts: 9.0 if ts < T0 + 3600 else 9.6))
        self.assertEqual(out["result_points"][-1]["result_usd"], 0.6)
        self.assertEqual(out["result_not_netted"], [chart.HUMMINGBOT])
        self.assertIn("Hummingbot", out["result_note"])

    def test_untracked_flows_give_no_result_and_a_reason(self):
        # A fresh data dir for a venue whose flows were never read.
        with tempfile.TemporaryDirectory() as other:
            with patch.dict(os.environ, {"CONDOR_DATA_DIR": other}):
                record(ph.EXCHANGE, T0, NOW, 43.0)
                out = self.build()
        self.assertIsNone(out["result_points"])
        self.assertEqual(out["result_reason"], "flows_untracked")
        self.assertIn(ph.EXCHANGE, out["result_note"])
        # The value series itself is untouched.
        self.assertTrue(out["chart_points"])

    def test_an_unconfirmed_flow_in_the_window_gives_no_result(self):
        record(ph.EXCHANGE, T0, NOW, 43.0)
        self.flow(20, T0 + 3600, status="pending")
        out = self.build()
        self.assertIsNone(out["result_points"])
        self.assertEqual(out["result_reason"], "flows_pending")

    def test_a_pending_flow_before_the_window_is_not_in_the_way(self):
        record(ph.EXCHANGE, T0, NOW, 43.0)
        self.flow(20, T0 - 7200, status="pending")
        self.assertIsNotNone(self.build()["result_points"])

    def test_a_flow_without_a_usd_value_gives_no_result(self):
        record(ph.EXCHANGE, T0, NOW, 43.0)
        self.flow(20, T0 + 3600, usd=False)
        out = self.build()
        self.assertIsNone(out["result_points"])
        self.assertEqual(out["result_reason"], "flows_unpriced")

    def test_unreadable_flows_give_no_result_and_are_not_taken_for_none(self):
        record(ph.EXCHANGE, T0, NOW, 43.0)
        with patch.object(native_flows, "list_flows", side_effect=OSError("disk")):
            out = self.build()
        self.assertIsNone(out["result_points"])
        self.assertEqual(out["result_reason"], "flows_unreadable")
        self.assertIn("could not be read", out["flows_note"])

    def test_a_single_reading_has_no_result(self):
        record(ph.EXCHANGE, NOW, NOW, 43.0)
        out = self.build()
        self.assertIsNone(out["result_points"])
        self.assertEqual(out["result_reason"], "no_history")


class OutlierTests(Base):
    def rows(self, *values, start=T0, step=STEP):
        return [(start + i * step, v) for i, v in enumerate(values)]

    def test_a_single_row_jumping_away_from_agreeing_neighbours_is_excluded(self):
        kept, anomalies = chart.exclude_outliers(
            self.rows(10.19, 10.19, 20.62, 10.19, 10.19), 900
        )
        self.assertEqual([v for _, v in kept], [10.19] * 4)
        self.assertEqual(
            anomalies,
            [
                {
                    "source": chart.HUMMINGBOT,
                    "ts": T0 + 2 * STEP,
                    "value": 20.62,
                    "neighbour_value": 10.19,
                }
            ],
        )

    def test_a_real_step_is_kept(self):
        for values in (
            (10.0, 10.0, 25.0, 25.0, 25.0),  # a deposit that stays
            (10.0, 25.0, 40.0),  # neighbours that disagree
            (10.0, 10.0, 11.5, 10.0),  # a wobble inside the threshold
            (100.0, 100.0, 108.0, 100.0),  # under 10% of the neighbours
        ):
            kept, anomalies = chart.exclude_outliers(self.rows(*values), 900)
            self.assertEqual([v for _, v in kept], list(values), values)
            self.assertEqual(anomalies, [])

    def test_neighbours_that_are_far_apart_in_time_are_not_neighbours(self):
        rows = [(T0, 10.0), (T0 + 5 * 3600, 30.0), (T0 + 10 * 3600, 10.0)]
        self.assertEqual(chart.exclude_outliers(rows, 900)[1], [])

    def test_the_first_and_last_readings_are_never_dropped(self):
        kept, anomalies = chart.exclude_outliers(self.rows(30.0, 10.0, 10.0, 30.0), 900)
        self.assertEqual(len(kept), 4)
        self.assertEqual(anomalies, [])

    def test_without_native_the_bad_row_is_a_break_and_is_reported(self):
        values = [10.19] * 12
        values[6] = 20.62
        points = hb(*self.rows(*values))
        out = self.build(points, now=T0 + 12 * STEP)
        totals = [p["total_usd"] for p in out["chart_points"]]
        self.assertNotIn(20.62, totals)
        # Not smoothed and not interpolated: the point is a break.
        self.assertEqual(totals.count(None), 1)
        self.assertEqual(
            [p["timestamp"] for p in out["chart_points"] if p["total_usd"] is None],
            [T0 + 6 * STEP],
        )
        self.assertEqual(
            out["anomalies"],
            [
                {
                    "source": chart.HUMMINGBOT,
                    "ts": T0 + 6 * STEP,
                    "value": 20.62,
                    "neighbour_value": 10.19,
                }
            ],
        )
        self.assertEqual(out["gaps"], [])

    def test_with_native_present_the_hummingbot_row_is_still_guarded(self):
        values = [9.0] * 12
        values[6] = 20.62
        record(ph.EXCHANGE, T0, T0 + 12 * STEP, 43.0)
        out = self.build(hb(*self.rows(*values)), now=T0 + 12 * STEP)
        totals = [p["total_usd"] for p in out["chart_points"]]
        self.assertEqual(set(totals), {52.0, None})
        self.assertEqual(len(out["anomalies"]), 1)
        # The result series carries the break too.
        self.assertTrue(
            any(
                r is not None and r["result_usd"] is None
                for r in out["result_points"] or []
            )
            or out["result_points"] is None
        )

    def test_a_deposit_inside_hummingbot_history_is_not_removed(self):
        values = [10.0] * 5 + [25.0] * 7
        out = self.build(hb(*self.rows(*values)), now=T0 + 12 * STEP)
        self.assertEqual(out["anomalies"], [])
        self.assertEqual([p["total_usd"] for p in out["chart_points"]][-1], 25.0)
        self.assertNotIn(None, [p["total_usd"] for p in out["chart_points"]])


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()
        import time

        self.now = time.time()
        self.since = self.now - 86400

        def snapshot(ts, eur):
            return {
                "timestamp": ts,
                "state": {"master": {"kraken": [{"token": "EUR", "value": eur}]}},
            }

        history = [
            snapshot(ts, 9.0 + (ts - self.since) / 1e6)
            for ts in range(int(self.since) + 600, int(self.now) - 200, 300)
        ]
        self.history = history
        self.sds = SimpleNamespace(get_or_fetch=AsyncMock(return_value=history))
        self.patch = patch.object(
            portfolio_route, "get_server_data_service", lambda: self.sds
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def get(self):
        return await portfolio_route.get_portfolio_history(
            name="vm08",
            range="1D",
            breakdown=False,
            user=WebUser(id=USER, role="admin"),
        )

    async def test_the_response_keeps_its_old_fields_and_adds_the_chart(self):
        for k in range(40):
            ph.record(USER, ph.EXCHANGE, 43.0, now=int(self.now) - 300 * (40 - k))
        out = await self.get()
        body = out.model_dump()
        for key in ("server", "points", "interval", "top_tokens", "change"):
            self.assertIn(key, body)
        # The old series still steps (9.x -> 52.x): kept as it was.
        self.assertGreater(out.points[-1].total_usd, 50)
        # The chart series does not: it starts when the Exchange did.
        self.assertEqual(out.series_start_reason, "all_accounts_recording_from")
        self.assertEqual(out.baseline_ts, out.series_start_ts)
        self.assertGreater(out.chart_points[0].total_usd, 50)
        self.assertEqual(out.interval, "5m")
        self.assertEqual({s.source for s in out.sources}, {"Hummingbot", ph.EXCHANGE})

    async def test_a_connector_the_server_no_longer_has_adds_nothing_to_the_chart(self):
        for k in range(40):
            ph.record(USER, ph.EXCHANGE, 43.0, now=int(self.now) - 300 * (40 - k))
        history = self.history

        async def fetch(name, data_type, **kwargs):
            if data_type == portfolio_route.ServerDataType.PORTFOLIO:
                # The kraken key was removed: only another connector is live now.
                return {"master": {"binance": [{"token": "EUR", "value": 1.0}]}}
            return history

        self.sds.get_or_fetch = AsyncMock(side_effect=fetch)
        out = await self.get()
        # Nothing recorded under kraken counts any more: the old series is empty and
        # the chart is the Exchange alone, never the Exchange plus a stale ~9.
        self.assertEqual(out.points[-1].total_usd, 43.0)
        self.assertAlmostEqual(out.chart_points[-1].total_usd, 43.0)

    async def test_connectors_that_are_still_live_keep_their_history(self):
        for k in range(40):
            ph.record(USER, ph.EXCHANGE, 43.0, now=int(self.now) - 300 * (40 - k))
        history = self.history

        async def fetch(name, data_type, **kwargs):
            if data_type == portfolio_route.ServerDataType.PORTFOLIO:
                return {"master": {"kraken": [{"token": "EUR", "value": 9.5}]}}
            return history

        self.sds.get_or_fetch = AsyncMock(side_effect=fetch)
        out = await self.get()
        self.assertGreater(out.points[-1].total_usd, 50)

    async def test_a_failing_chart_build_leaves_the_old_response_intact(self):
        with patch.object(
            portfolio_route.portfolio_chart, "build", side_effect=ValueError
        ):
            out = await self.get()
        self.assertTrue(out.points)
        self.assertIsNone(out.chart_points)
        self.assertEqual(out.flows, [])


if __name__ == "__main__":
    unittest.main()
