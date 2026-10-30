"""Value history for the Exchange, Revolut X and Trading 212 accounts, and its merge into the chart."""

import os
import tempfile
import unittest
from dataclasses import dataclass, field
from unittest.mock import patch

from condor import native_venues
from condor import portfolio_history as ph

TRADING212 = native_venues.get("trading212").label


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
        self.assertTrue(ph.record(1, TRADING212, 9.0, now=T0 + 60))
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


class NeighbourTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_nearest_gives_the_sample_at_or_before_and_the_first_after(self):
        ph.record(1, ph.EXCHANGE, 10, now=T0)
        ph.record(1, ph.EXCHANGE, 11, now=T0 + 600)
        ph.record(1, TRADING212, 99, now=T0 + 300)
        before_both, mid, after_all = ph.nearest(
            1, ph.EXCHANGE, [T0 - 1, T0 + 300, T0 + 700]
        )
        self.assertEqual(before_both, (None, (T0, 10.0)))
        self.assertEqual(mid, ((T0, 10.0), (T0 + 600, 11.0)))
        self.assertEqual(after_all, ((T0 + 600, 11.0), None))
        # A sample exactly at the moment counts as at-or-before.
        self.assertEqual(ph.nearest(1, ph.EXCHANGE, [T0 + 600])[0][0], (T0 + 600, 11.0))

    def test_bounds_are_the_first_and_latest_sample_of_one_source(self):
        ph.record(1, ph.EXCHANGE, 10, now=T0)
        ph.record(1, ph.EXCHANGE, 11, now=T0 + 600)
        self.assertEqual(ph.bounds(1, ph.EXCHANGE), ((T0, 10.0), (T0 + 600, 11.0)))
        self.assertEqual(ph.bounds(1, TRADING212), (None, None))

    def test_reading_a_user_without_history_creates_nothing(self):
        self.assertEqual(ph.nearest(5, ph.EXCHANGE, [T0]), [(None, None)])
        self.assertEqual(ph.bounds(5, ph.EXCHANGE), (None, None))
        self.assertFalse((ph.data_dir() / "portfolio_history").exists())


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
        native = {TRADING212: [(T0, 9.4)], ph.EXCHANGE: [(T0, 48.0)]}
        result = self.merged(points, native, skip=(TRADING212,))
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
            1, exchange=FakeService(True, 48.2), trading212=FakeService(True, None)
        )
        rows = ph.series(1, since=0)
        self.assertEqual(list(rows), [ph.EXCHANGE])
        self.assertEqual(rows[ph.EXCHANGE][0][1], 48.2)

    async def test_a_skipped_reading_is_logged_with_its_reason(self):
        with self.assertLogs(ph.log, level="WARNING") as logged:
            await ph.sample_user(
                1, exchange=FakeService(True, 48.2), trading212=FakeService(True, None)
            )
        line = " ".join(logged.output)
        self.assertIn(TRADING212, line)
        self.assertIn("skipped (ValueError: unavailable)", line)

    async def test_revolut_x_is_sampled_as_its_own_source(self):
        await ph.sample_user(
            1,
            exchange=FakeService(False, 48.2),
            trading212=FakeService(True, 9.0),
            revolut_x=FakeService(True, 61.5),
        )
        rows = ph.series(1, since=0)
        self.assertEqual(set(rows), {TRADING212, ph.REVOLUT})
        self.assertEqual(ph.REVOLUT, "Revolut X")
        self.assertIn(ph.REVOLUT, ph.SOURCES)
        self.assertEqual(rows[ph.REVOLUT][0][1], 61.5)

    async def test_a_failing_revolut_x_does_not_stop_the_other_accounts(self):
        await ph.sample_user(
            1,
            exchange=FakeService(True, 48.2),
            trading212=FakeService(False, 0),
            revolut_x=FakeService(True, None),
        )
        self.assertEqual(list(ph.series(1, since=0)), [ph.EXCHANGE])

    def test_users_with_only_a_revolut_x_folder_are_recorded(self):
        folder = os.path.join(self.tmp.name, "revolut_x", "7")
        os.makedirs(folder)
        self.assertEqual(ph._users(), [7])

    async def test_a_disconnected_account_records_nothing(self):
        await ph.sample_user(
            1, exchange=FakeService(False, 48.2), trading212=FakeService(False, 9.0)
        )
        self.assertEqual(ph.series(1, since=0), {})


if __name__ == "__main__":
    unittest.main()


class StoredNameTests(unittest.TestCase):
    """The owner sees "Crypto.com"; history recorded as "Crypto.com Exchange" keeps working."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _stored_sources(self):
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(ph._db(1))) as conn:
            return {r[0] for r in conn.execute("SELECT DISTINCT source FROM samples")}

    def test_the_display_label_is_crypto_com(self):
        self.assertEqual(ph.EXCHANGE, "Crypto.com")
        self.assertIn("Crypto.com", ph.SOURCES)
        self.assertNotIn("Crypto.com Exchange", ph.SOURCES)

    def test_rows_recorded_under_the_old_name_are_read_under_the_new_one(self):
        import sqlite3
        from contextlib import closing

        ph.record(1, TRADING212, 9.0, now=T0)  # creates the file and schema
        with closing(sqlite3.connect(ph._db(1))) as conn:
            conn.execute(
                "INSERT INTO samples VALUES (?, 'Crypto.com Exchange', 40.0)",
                (T0 - 600,),
            )
            conn.execute(
                "INSERT INTO samples VALUES (?, 'Crypto.com Exchange', 41.0)",
                (T0 + 600,),
            )
            conn.commit()
        rows = ph.series(1, since=T0 - 1000, until=T0 + 1000)
        self.assertEqual(rows[ph.EXCHANGE], [(T0 - 600, 40.0), (T0 + 600, 41.0)])
        self.assertNotIn("Crypto.com Exchange", rows)
        first, last = ph.bounds(1, ph.EXCHANGE)
        self.assertEqual((first[0], last[0]), (T0 - 600, T0 + 600))
        before, after = ph.nearest(1, ph.EXCHANGE, [T0])[0]
        self.assertEqual((before[0], after[0]), (T0 - 600, T0 + 600))

    def test_new_samples_continue_the_same_series_without_orphaning_it(self):
        import sqlite3
        from contextlib import closing

        ph.record(1, TRADING212, 9.0, now=T0)
        with closing(sqlite3.connect(ph._db(1))) as conn:
            conn.execute(
                "INSERT INTO samples VALUES (?, 'Crypto.com Exchange', 40.0)", (T0,)
            )
            conn.commit()
        self.assertTrue(ph.record(1, ph.EXCHANGE, 41.0, now=T0 + 600))
        # Stored under the old key, so history and a rollback both keep one series.
        self.assertEqual(self._stored_sources(), {"Crypto.com Exchange", TRADING212})
        rows = ph.series(1, since=T0 - 1, until=T0 + 1000)
        self.assertEqual(rows[ph.EXCHANGE], [(T0, 40.0), (T0 + 600, 41.0)])
        # The throttle sees the old rows too.
        self.assertFalse(ph.record(1, ph.EXCHANGE, 42.0, now=T0 + 700))
