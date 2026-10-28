"""Value history for the Exchange and Kraken accounts, and its merge into the chart."""

import os
import tempfile
import unittest
from dataclasses import dataclass, field
from unittest.mock import patch

from condor import portfolio_history as ph


@dataclass
class Point:
    timestamp: float
    total_usd: float = 0.0
    tokens: dict = field(default_factory=dict)


T0 = 1_800_000_000


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_samples_are_throttled_per_source(self):
        self.assertTrue(ph.record(1, ph.EXCHANGE, 48.0, now=T0))
        self.assertFalse(ph.record(1, ph.EXCHANGE, 48.5, now=T0 + 60))
        self.assertTrue(ph.record(1, ph.KRAKEN, 9.0, now=T0 + 60))
        self.assertTrue(ph.record(1, ph.EXCHANGE, 49.0, now=T0 + ph.MIN_GAP_SEC + 1))

    def test_bad_values_are_ignored(self):
        for bad in (None, "x", float("nan"), -1):
            self.assertFalse(ph.record(1, ph.EXCHANGE, bad, now=T0))
        with self.assertRaises(ValueError):
            ph.record(1, "bad/source", 1, now=T0)

    def test_series_returns_ascending_rows_inside_the_window(self):
        ph.record(1, ph.EXCHANGE, 10, now=T0)
        ph.record(1, ph.EXCHANGE, 11, now=T0 + 600)
        ph.record(1, ph.EXCHANGE, 12, now=T0 + 1200)
        rows = ph.series(1, since=T0 + 300, until=T0 + 1300)
        self.assertEqual(rows, {ph.EXCHANGE: [(T0 + 600, 11.0), (T0 + 1200, 12.0)]})

    def test_users_do_not_see_each_others_history(self):
        ph.record(1, ph.EXCHANGE, 10, now=T0)
        self.assertEqual(ph.series(2, since=0, until=T0 + 10), {})

    def test_old_samples_are_pruned(self):
        ph.record(1, ph.EXCHANGE, 10, now=T0)
        ph.record(1, ph.EXCHANGE, 11, now=T0 + ph.RETENTION_SEC + 1000)
        rows = ph.series(1, since=0, until=T0 + ph.RETENTION_SEC + 2000)
        self.assertEqual(len(rows[ph.EXCHANGE]), 1)


class MergeTests(unittest.TestCase):
    def merged(self, points, native, **kwargs):
        return ph.merge(points, native, 3600, point_type=Point, **kwargs)

    def test_revolut_x_history_joins_the_chart_without_replacing_the_others(self):
        points = [Point(T0, 100.0), Point(T0 + 3600, 101.0)]
        native = {
            ph.EXCHANGE: [(T0, 10.0)],
            ph.REVOLUT: [(T0, 20.0), (T0 + 3600, 22.0)],
        }
        result = self.merged(points, native)
        self.assertEqual([p.total_usd for p in result], [130.0, 133.0])

    def test_native_accounts_are_added_to_the_hummingbot_total(self):
        points = [Point(T0, 9.0, {"EUR": 9.0}), Point(T0 + 3600, 9.5, {"EUR": 9.5})]
        native = {ph.EXCHANGE: [(T0, 48.0), (T0 + 3600, 49.0)]}
        result = self.merged(points, native)
        self.assertEqual([p.total_usd for p in result], [57.0, 58.5])
        self.assertEqual(result[1].tokens, {"EUR": 9.5})

    def test_a_source_counts_zero_before_its_first_sample_then_carries_forward(self):
        points = [Point(T0, 9.0), Point(T0 + 3600, 9.0), Point(T0 + 7200, 9.0)]
        native = {ph.EXCHANGE: [(T0 + 3600, 48.0)]}
        result = self.merged(points, native)
        self.assertEqual([p.total_usd for p in result], [9.0, 57.0, 57.0])

    def test_native_only_timestamps_carry_the_last_hummingbot_value(self):
        points = [Point(T0, 9.0)]
        native = {ph.EXCHANGE: [(T0 + 1800, 48.0)]}
        result = self.merged(points, native)
        self.assertEqual(
            [(p.timestamp, p.total_usd) for p in result], [(T0, 9.0), (T0 + 1800, 57.0)]
        )

    def test_samples_are_thinned_to_one_per_interval(self):
        native = {ph.EXCHANGE: [(T0 + i * 300, 40.0 + i) for i in range(12)]}
        result = self.merged([], native)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].total_usd, 51.0)  # the last sample of the hour

    def test_a_source_the_hummingbot_history_already_counts_is_skipped(self):
        points = [Point(T0, 9.0)]
        native = {ph.KRAKEN: [(T0, 9.4)], ph.EXCHANGE: [(T0, 48.0)]}
        result = self.merged(points, native, skip=(ph.KRAKEN,))
        self.assertEqual(result[0].total_usd, 57.0)

    def test_without_native_history_the_points_pass_through(self):
        points = [Point(T0, 9.0)]
        self.assertEqual(self.merged(points, {}), points)


class FakeService:
    def __init__(self, connected, total):
        self.connected = connected
        self.total = total

    def status(self, user_id):
        return {"connected": self.connected}

    async def portfolio(self, user_id):
        if self.total is None:
            raise ValueError("unavailable")
        return {"total_usd": self.total}


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def test_only_connected_accounts_are_sampled_and_failures_are_swallowed(self):
        await ph.sample_user(
            1, exchange=FakeService(True, 48.2), kraken=FakeService(True, None)
        )
        rows = ph.series(1, since=0)
        self.assertEqual(list(rows), [ph.EXCHANGE])
        self.assertEqual(rows[ph.EXCHANGE][0][1], 48.2)

    async def test_revolut_x_is_sampled_as_its_own_source(self):
        await ph.sample_user(
            1,
            exchange=FakeService(False, 48.2),
            kraken=FakeService(True, 9.0),
            revolut=FakeService(True, 61.5),
        )
        rows = ph.series(1, since=0)
        self.assertEqual(set(rows), {ph.KRAKEN, ph.REVOLUT})
        self.assertEqual(ph.REVOLUT, "Revolut X")
        self.assertIn(ph.REVOLUT, ph.SOURCES)
        self.assertEqual(rows[ph.REVOLUT][0][1], 61.5)

    async def test_a_failing_revolut_x_does_not_stop_the_other_accounts(self):
        await ph.sample_user(
            1,
            exchange=FakeService(True, 48.2),
            kraken=FakeService(False, 0),
            revolut=FakeService(True, None),
        )
        self.assertEqual(list(ph.series(1, since=0)), [ph.EXCHANGE])

    def test_users_with_only_a_revolut_x_folder_are_recorded(self):
        folder = os.path.join(self.tmp.name, "revolut_x", "7")
        os.makedirs(folder)
        self.assertEqual(ph._users(), [7])

    async def test_a_disconnected_account_records_nothing(self):
        await ph.sample_user(
            1, exchange=FakeService(False, 48.2), kraken=FakeService(False, 9.0)
        )
        self.assertEqual(ph.series(1, since=0), {})


if __name__ == "__main__":
    unittest.main()
