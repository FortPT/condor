"""The portfolio's change over a window: net of flows, or an honest "unknown".

No network and no money: the Hummingbot history is a fake, the data dir a temp folder.
The case that started it: Hummingbot's Kraken (about 9 EUR) was the only source with a
value at the start of the 1D window, the native venues' recorders began later, and the
first-to-last difference of the merged series read as +491%.
"""

import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from condor import native_flows
from condor import portfolio_change as pc
from condor import portfolio_history as ph
from condor.web.models import WebUser
from condor.web.routes import portfolio as portfolio_route

USER = 1
DAY = 86400
NOW = 1_800_000_000.0
SINCE = NOW - DAY


def hb(*rows):
    return [SimpleNamespace(timestamp=ts, total_usd=total) for ts, total in rows]


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def flow(self, amount, ts, status="confirmed", venue="crypto_com_exchange"):
        native_flows.add_flow(
            USER,
            venue,
            amount=str(amount),
            currency="USD",
            ts=ts,
            usd_value=str(amount),
            source="manual",
            status=status,
        )

    def compute(self, points, native, **kwargs):
        return pc.compute(USER, points, native, SINCE, 300, now=NOW, **kwargs)


class ChangeTests(Base):
    def test_a_clean_window_is_the_change_net_of_the_recorded_flows(self):
        # 9 + 50 at the start, 9.5 + 70 now; 15 of the 20 gained on the exchange was
        # a deposit, so the trading result is 20.5 - 15 = 5.5.
        self.flow(5, SINCE - 3600)  # an older deposit makes the venue's flows tracked
        self.flow(15, SINCE + 3600)
        out = self.compute(
            hb((SINCE + 60, 9.0), (NOW, 9.5)),
            {ph.EXCHANGE: [(SINCE + 120, 50.0), (NOW - 60, 70.0)]},
        )
        self.assertTrue(out["available"])
        self.assertEqual(out["start_usd"], 59.0)
        self.assertEqual(out["end_usd"], 79.5)
        self.assertEqual(out["net_flows_usd"], 15.0)
        self.assertEqual(out["change_usd"], 5.5)
        self.assertEqual(out["change_pct"], round(5.5 / 59.0 * 100, 2))
        self.assertEqual(out["sources"], ["Hummingbot", ph.EXCHANGE])
        self.assertEqual(out["flows_not_netted"], ["Hummingbot"])

    def test_sources_with_different_start_times_give_no_figure(self):
        # The live case: only Hummingbot's Kraken (9.36) existed at the window start,
        # the Exchange was first sampled ten hours later and holds 46.93 now.
        self.flow(5, SINCE - 3600)
        out = self.compute(
            hb((SINCE + 60, 9.36), (NOW, 10.19)),
            {
                ph.EXCHANGE: [(SINCE + 36000, 46.0), (NOW - 60, 46.93)],
                ph.REVOLUT: [(SINCE + 40000, 4.7), (NOW - 60, 4.75)],
            },
        )
        self.assertFalse(out["available"])
        self.assertEqual(out["reason"], "different_start")
        self.assertIn(ph.EXCHANGE, out["note"])
        self.assertNotIn("change_pct", out)
        self.assertNotIn("change_usd", out)

    def test_a_hummingbot_history_that_starts_late_gives_no_figure(self):
        out = self.compute(hb((SINCE + 40000, 9.0), (NOW, 9.5)), {})
        self.assertEqual(out["reason"], "different_start")
        self.assertIn("Hummingbot", out["note"])

    def test_hummingbot_growing_from_nothing_is_not_a_gain(self):
        out = self.compute(hb((SINCE + 60, 0.0), (NOW, 9.5)), {})
        self.assertEqual(out["reason"], "different_start")

    def test_untracked_flows_give_no_figure_instead_of_a_deposit_read_as_profit(self):
        out = self.compute(
            hb((SINCE + 60, 9.0), (NOW, 9.0)),
            {ph.EXCHANGE: [(SINCE + 120, 50.0), (NOW - 60, 70.0)]},
        )
        self.assertEqual(out["reason"], "flows_untracked")
        self.assertIn(ph.EXCHANGE, out["note"])

    def test_an_unconfirmed_movement_in_the_window_gives_no_figure(self):
        self.flow(5, SINCE - 3600)
        self.flow(20, SINCE + 3600, status="pending")
        out = self.compute([], {ph.EXCHANGE: [(SINCE + 120, 50.0), (NOW - 60, 70.0)]})
        self.assertEqual(out["reason"], "flows_pending")

    def test_a_pending_movement_outside_the_window_is_not_in_the_way(self):
        self.flow(5, SINCE - 3600)
        self.flow(20, SINCE - 7200, status="pending")
        out = self.compute([], {ph.EXCHANGE: [(SINCE + 120, 50.0), (NOW - 60, 70.0)]})
        self.assertTrue(out["available"])
        self.assertEqual(out["change_usd"], 20.0)

    def test_a_source_hummingbot_already_counts_is_skipped(self):
        out = self.compute(
            hb((SINCE + 60, 9.0), (NOW, 9.5)),
            {ph.KRAKEN: [(SINCE + 36000, 9.4), (NOW - 60, 9.5)]},
            skip=(ph.KRAKEN,),
        )
        self.assertTrue(out["available"])
        self.assertEqual(out["sources"], ["Hummingbot"])

    def test_a_deposit_larger_than_the_base_gives_the_amount_but_no_percentage(self):
        self.flow(5, SINCE - 3600)
        self.flow(100, SINCE + 3600)
        out = self.compute([], {ph.EXCHANGE: [(SINCE + 120, 2.0), (NOW - 60, 105.0)]})
        self.assertTrue(out["available"])
        self.assertEqual(out["change_usd"], 3.0)
        self.assertIsNone(out["change_pct"])

    def test_nothing_recorded_is_unavailable(self):
        out = self.compute([], {})
        self.assertEqual((out["available"], out["reason"]), (False, "no_history"))

    def test_an_empty_source_is_ignored(self):
        out = self.compute(
            hb((SINCE + 60, 0.0), (NOW, 0.0)),
            {ph.REVOLUT: [(SINCE + 60, 0.0), (NOW - 60, 0.0)]},
        )
        self.assertEqual(out["reason"], "no_history")


class HistoryRouteTests(Base):
    """The real route: the figure is the server's, never first-vs-last."""

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.since = self.now - DAY
        sds = SimpleNamespace(get_or_fetch=AsyncMock(return_value=self.history()))
        self.patch = patch.object(
            portfolio_route, "get_server_data_service", lambda: sds
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def history(self):
        def snapshot(ts, eur):
            return {
                "timestamp": ts,
                "state": {"master": {"kraken": [{"token": "EUR", "value": eur}]}},
            }

        return [snapshot(self.since + 600, 9.0), snapshot(self.now - 300, 9.6)]

    async def get(self):
        return await portfolio_route.get_portfolio_history(
            name="vm08",
            range="1D",
            breakdown=False,
            user=WebUser(id=USER, role="admin"),
        )

    async def test_a_hummingbot_only_window_is_a_figure_with_the_caveat(self):
        out = await self.get()
        self.assertEqual([round(p.total_usd, 2) for p in out.points], [9.0, 9.6])
        self.assertTrue(out.change.available)
        self.assertEqual(out.change.sources, ["Hummingbot"])
        self.assertEqual(out.change.end_usd, 9.6)
        self.assertEqual(out.change.change_usd, 0.6)
        self.assertEqual(out.change.flows_not_netted, ["Hummingbot"])

    async def test_a_late_native_account_gives_a_dash_not_a_gain(self):
        ph.record(USER, ph.EXCHANGE, 46.0, now=self.since + 36000)
        ph.record(USER, ph.EXCHANGE, 46.9, now=self.now - 300)
        out = await self.get()
        # The chart still shows the account from when it was first recorded...
        self.assertGreater(out.points[-1].total_usd, 50)
        # ...but the change does not pretend it was there all day.
        self.assertFalse(out.change.available)
        self.assertEqual(out.change.reason, "different_start")
        self.assertIsNone(out.change.change_pct)

    async def test_the_change_is_additive_and_the_contract_is_kept(self):
        body = (await self.get()).model_dump()
        for key in ("server", "points", "interval", "top_tokens", "change"):
            self.assertIn(key, body)


if __name__ == "__main__":
    unittest.main()
