"""Exchange maker mode: post-only orders, the bounded wait, cancel, ledger and stats.

Fakes only: no private endpoint is contacted and no clock really sleeps.
"""

import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from condor import crypto_com_exchange as ex
from tests.test_crypto_com_exchange import FakeService


def detail(status, quantity="0", value="0", fee="0", fee_currency="USD", **extra):
    return {
        "order_id": "o1",
        "status": status,
        "side": "BUY",
        "cumulative_quantity": quantity,
        "cumulative_value": value,
        "cumulative_fee": fee,
        "fee_instrument_name": fee_currency,
        **extra,
    }


class MakerFake(FakeService):
    """A fake Exchange whose order progresses on a script, on a virtual clock."""

    def __init__(self):
        super().__init__()
        self.clock = 1000.0
        self.script = [detail("ACTIVE")]
        self.polls = 0
        self.after_cancel = detail("CANCELED")
        self.cancelled = False
        self.cancel_error = None
        self.cancel_effective = True
        self.detail_error = None
        self.after_cancel_error = None
        self.book_sizes = ("500", "800")
        self.book_delay = 0
        self.fee_error = None

    async def _sleep(self, seconds):
        self.clock += seconds

    def _monotonic(self):
        return self.clock

    async def public(self, user_id, path, params=None):
        if path == "get-book":
            self.clock += self.book_delay
            t = self.tickers[params["instrument_name"]]
            return {
                "data": [
                    {
                        "bids": [[t["b"], self.book_sizes[0], "3"]],
                        "asks": [[t["k"], self.book_sizes[1], "2"]],
                    }
                ]
            }
        return await super().public(user_id, path, params)

    async def private(self, user_id, method, params=None):
        if method == "get-fee-rate" and self.fee_error:
            self.sent.append((method, params))
            raise self.fee_error
        if method == "cancel-order":
            self.sent.append((method, params))
            if self.cancel_error:
                self.cancelled = self.cancelled or self.cancel_effective
                raise self.cancel_error
            self.cancelled = True
            return {"order_id": "o1", "client_oid": self._oid()}
        if method == "get-order-detail":
            self.sent.append((method, params))
            if self.cancelled:
                if self.after_cancel_error:
                    raise self.after_cancel_error
                return {**self.after_cancel, "client_oid": self._oid()}
            if self.detail_error:
                raise self.detail_error
            row = self.script[min(self.polls, len(self.script) - 1)]
            self.polls += 1
            return {**row, "client_oid": self._oid()}
        return await super().private(user_id, method, params)

    def sent_to(self, method):
        return [params for name, params in self.sent if name == method]


class MakerBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()
        self.svc = MakerFake()
        ex._write(ex.credentials_path(1), {"api_key": "key", "api_secret": "secret"})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def ready(self, maker=True):
        await self.svc.sync_from_account(1)
        self.svc.set_enabled(1, True)
        if maker:
            self.svc.set_maker_enabled(1, True)

    def ledger(self):
        return ex._read_ledger(1)

    def stats(self):
        return self.svc.maker_stats(1)["stats"]

    async def buy(self, amount="8", **extra):
        return await self.svc.order(
            1, "buy", "ETH", amount, mode="maker", purpose="entry", **extra
        )


class MakerOrderTests(MakerBase):
    async def test_a_buy_rests_post_only_at_the_best_bid_and_fills(self):
        await self.ready()
        self.svc.script = [
            detail("ACTIVE"),
            detail("FILLED", "0.0034", "7.90483", "0.019762", avg_price="2324.95"),
        ]
        result = await self.buy()
        (params,) = self.svc.sent_to("create-order")
        self.assertEqual(params["exec_inst"], ["POST_ONLY"])
        self.assertEqual(params["time_in_force"], "GOOD_TILL_CANCEL")
        self.assertEqual(params["type"], "LIMIT")
        self.assertEqual(params["side"], "BUY")
        # the best bid, never the ask or through it
        self.assertEqual(params["price"], "2324.95")
        self.assertEqual(params["quantity"], "0.0034")
        self.assertEqual(self.svc.sent_to("cancel-order"), [])
        self.assertEqual(result["outcome"], "filled")
        maker = result["maker"]
        self.assertEqual(maker["fee_class"], "maker")
        self.assertEqual(maker["effective_fee_pct"], "0.25")
        self.assertEqual(maker["unfilled_quantity"], "0")
        self.assertFalse(maker["remainder_cancelled"])
        self.assertEqual(maker["waited_sec"], 2.5)
        ledger = self.ledger()
        self.assertEqual(ledger["positions"]["ETH"]["units"], "0.0034")
        self.assertEqual(Decimal(ledger["cash"]), Decimal("40") - Decimal("7.924592"))
        self.assertIsNone(ledger["uncertain_order"])
        self.assertTrue(ledger["enabled"])
        stats = self.stats()
        self.assertEqual((stats["attempts"], stats["fill_rate"]), (1, 1.0))
        self.assertEqual(stats["avg_seconds_to_fill"], 2.5)

    async def test_a_target_exit_rests_at_the_best_ask(self):
        await self.ready()
        self.svc.script = [detail("FILLED", "1210000", "5.94", "0.01485")]
        result = await self.svc.order(
            1, "sell", "SHIB", "1215909", mode="maker", purpose="target_exit"
        )
        (params,) = self.svc.sent_to("create-order")
        self.assertEqual(params["side"], "SELL")
        self.assertEqual(params["price"], "0.000004914")
        self.assertEqual(params["quantity"], "1210000")
        self.assertEqual(result["outcome"], "filled")
        self.assertEqual(self.ledger()["positions"]["SHIB"]["units"], "5909")

    async def test_partial_fill_is_cancelled_and_updates_the_ledger(self):
        await self.ready()
        part = dict(quantity="0.001", value="2.32495", fee="0.005812")
        self.svc.script = [detail("ACTIVE"), detail("ACTIVE", **part)]
        self.svc.after_cancel = detail("CANCELED", **part)
        result = await self.buy()
        self.assertEqual(self.svc.sent_to("cancel-order"), [{"order_id": "o1"}])
        self.assertEqual(result["outcome"], "partial")
        maker = result["maker"]
        self.assertEqual(maker["filled_quantity"], "0.001")
        self.assertEqual(maker["unfilled_quantity"], "0.0024")
        self.assertEqual(maker["filled_fraction"], "0.2941")
        self.assertTrue(maker["remainder_cancelled"])
        self.assertEqual(maker["cancelled_by"], "us")
        self.assertEqual(maker["waited_sec"], 60.0)
        ledger = self.ledger()
        self.assertEqual(ledger["positions"]["ETH"]["units"], "0.001")
        self.assertEqual(ledger["orders"][-1]["status"], "CANCELED")
        self.assertIsNone(ledger["uncertain_order"])
        stats = self.stats()
        self.assertEqual(stats["outcomes"]["partial"], 1)
        self.assertEqual(stats["any_fill_rate"], 1.0)
        self.assertEqual(stats["fill_rate"], 0.0)

    async def test_an_unfilled_order_is_a_normal_result_and_changes_nothing(self):
        await self.ready()
        before = self.ledger()
        result = await self.buy()
        self.assertEqual(result["outcome"], "unfilled")
        self.assertEqual(result["maker"]["filled_quantity"], "0")
        self.assertEqual(result["maker"]["unfilled_quantity"], "0.0034")
        self.assertIn("No trade happened", result["maker"]["note"])
        self.assertEqual(len(self.svc.sent_to("cancel-order")), 1)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.ledger()["cash"], "40")
        self.assertEqual(self.ledger()["orders"], [])
        stats = self.stats()
        self.assertEqual(stats["outcomes"]["unfilled"], 1)
        self.assertEqual(stats["fill_rate"], 0.0)
        self.assertEqual(stats["unfilled_notional"], round(0.0034 * 2324.95, 4))

    async def test_a_zero_fill_cancel_by_the_exchange_is_unfilled_not_an_error(self):
        await self.ready()
        self.svc.script = [detail("CANCELED")]
        result = await self.buy()
        self.assertEqual(result["outcome"], "unfilled")
        self.assertEqual(result["maker"]["cancelled_by"], "exchange")
        self.assertEqual(self.svc.sent_to("cancel-order"), [])
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_the_taker_path_is_unchanged(self):
        await self.ready()
        self.svc.script = [detail("FILLED", "0.0034", "7.9", "0.005")]
        await self.svc.order(1, "buy", "ETH", "8")
        (params,) = self.svc.sent_to("create-order")
        self.assertEqual(params["time_in_force"], "IMMEDIATE_OR_CANCEL")
        self.assertNotIn("exec_inst", params)
        self.assertGreater(Decimal(params["price"]), Decimal("2324.96"))
        self.assertEqual(self.stats()["attempts"], 0)


class MakerRefusalTests(MakerBase):
    async def test_a_post_only_that_would_cross_is_a_definite_refusal(self):
        await self.ready()
        before = self.ledger()
        self.svc.reject_create = (500, 43005, "POST_ONLY_REJ")
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        message = str(caught.exception)
        self.assertIn("post-only", message)
        self.assertIn("crossed", message)
        self.assertIn("not retried as a taker order", message)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.svc.sent_to("cancel-order"), [])
        self.assertEqual(self.svc.sent_to("get-order-detail"), [])
        self.assertEqual(len(self.svc.sent_to("create-order")), 1)
        self.assertEqual(self.stats()["outcomes"]["rejected"], 1)

    async def test_a_post_only_rejected_after_acceptance_is_a_refusal_too(self):
        await self.ready()
        before = self.ledger()
        self.svc.script = [detail("REJECTED", reason="POST_ONLY_REJ")]
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("rejected the post-only order", str(caught.exception))
        self.assertIn("not retried as a taker order", str(caught.exception))
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.svc.sent_to("cancel-order"), [])
        self.assertEqual(self.stats()["outcomes"]["rejected"], 1)

    async def test_error_classification_for_429_and_5xx(self):
        self.assertTrue(ex.ExchangeError(500, 43005, "").definite)
        self.assertTrue(ex.ExchangeError(429, 42901, "").definite)
        self.assertTrue(ex.ExchangeError(400, 308, "").definite)
        self.assertFalse(ex.ExchangeError(503, None, "").definite)
        self.assertFalse(ex.ExchangeError(500, 306, "").definite)
        self.assertFalse(ex.ExchangeError(400, 50001, "").definite)
        await self.ready()
        before = self.ledger()
        self.svc.reject_create = (429, 42901, "TOO_MANY_REQUESTS")
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("rejected", str(caught.exception))
        self.assertEqual(self.ledger(), before)
        self.svc.reject_create = (503, None, "")
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("reconciliation", str(caught.exception))
        pending = self.ledger()["uncertain_order"]
        self.assertEqual(pending["mode"], "maker")
        self.assertFalse(self.svc.status(1)["enabled"])
        self.assertEqual(self.svc.sent_to("get-order-detail"), [])

    async def test_a_failed_send_stays_uncertain(self):
        await self.ready()
        self.svc.fail_create = True
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("reconciliation", str(caught.exception))
        self.assertIn("resting", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_stop_exit_is_refused_in_maker_mode_but_not_in_taker_mode(self):
        await self.ready()
        for call in (self.svc.preview, self.svc.order):
            with self.assertRaises(ValueError) as caught:
                await call(
                    1, "sell", "SHIB", "1215909", mode="maker", purpose="stop_exit"
                )
            self.assertIn("stop", str(caught.exception))
            self.assertIn("taker", str(caught.exception))
        self.assertEqual(self.svc.sent_to("create-order"), [])
        self.svc.script = [detail("FILLED", "1210000", "5.94", "0.01485")]
        await self.svc.order(1, "sell", "SHIB", "1215909", purpose="stop_exit")
        (params,) = self.svc.sent_to("create-order")
        self.assertEqual(params["time_in_force"], "IMMEDIATE_OR_CANCEL")

    async def test_maker_arguments_are_validated(self):
        await self.ready()
        bad = [
            dict(mode="maker"),  # purpose is required
            dict(mode="maker", purpose="target_exit"),  # a buy is an entry
            dict(mode="maker", purpose="entry", maker_wait_sec="4"),
            dict(mode="maker", purpose="entry", maker_wait_sec="76"),
            dict(mode="maker", purpose="entry", maker_wait_sec="1e1"),
            dict(mode="taker", maker_wait_sec="30"),
            dict(mode="limit", purpose="entry"),
            dict(mode="maker", purpose="exit"),
        ]
        for arguments in bad:
            with self.subTest(arguments), self.assertRaises(ValueError):
                await self.svc.order(1, "buy", "ETH", "8", **arguments)
        with self.assertRaises(ValueError):
            await self.svc.order(
                1, "sell", "SHIB", "100000", mode="maker", purpose="entry"
            )
        self.assertEqual(self.svc.sent_to("create-order"), [])

    async def test_maker_mode_is_off_until_the_owner_turns_it_on(self):
        await self.ready(maker=False)
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("Maker mode is off", str(caught.exception))
        self.assertEqual(self.svc.sent_to("create-order"), [])
        preview = await self.svc.preview(
            1, "buy", "ETH", "8", mode="maker", purpose="entry"
        )
        self.assertFalse(preview["maker_mode_enabled"])

    async def test_the_same_guards_apply_in_maker_mode(self):
        await self.ready()
        with self.assertRaises(ValueError) as caught:
            await self.buy("30")
        self.assertIn("40%", str(caught.exception))
        with self.assertRaises(ValueError):
            await self.svc.order(
                1, "sell", "ETH", "0.1", mode="maker", purpose="target_exit"
            )
        self.svc.set_enabled(1, False)
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("paused", str(caught.exception))
        self.assertEqual(self.svc.sent_to("create-order"), [])

    async def test_a_wide_spread_or_crossed_book_is_refused(self):
        await self.ready()
        self.svc.tickers["ETH_USD"]["b"] = "2300"
        self.svc.tickers["ETH_USD"]["k"] = "2400"
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("spread", str(caught.exception))
        self.svc.tickers["ETH_USD"]["b"] = "2400"
        self.svc.tickers["ETH_USD"]["k"] = "2399"
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("No live market", str(caught.exception))
        self.assertEqual(self.svc.sent_to("create-order"), [])


class MakerUncertainTests(MakerBase):
    async def test_a_cancel_failure_is_uncertain_and_reconcile_cancels_it(self):
        await self.ready()
        self.svc.cancel_error = ValueError("Crypto.com unavailable")
        self.svc.cancel_effective = False
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("reconciliation", str(caught.exception))
        self.assertEqual(
            len(self.svc.sent_to("cancel-order")), ex.MAKER_CANCEL_ATTEMPTS
        )
        pending = self.ledger()["uncertain_order"]
        self.assertEqual((pending["mode"], pending["purpose"]), ("maker", "entry"))
        self.assertFalse(self.svc.status(1)["enabled"])
        self.assertEqual(self.ledger()["cash"], "40")
        with self.assertRaises(ValueError):
            self.svc.set_enabled(1, True)
        # the order is still resting; one part filled meanwhile
        self.svc.cancel_error = None
        self.svc.after_cancel = detail(
            "CANCELED", quantity="0.001", value="2.32495", fee="0.005812"
        )
        self.svc.script = [detail("ACTIVE", quantity="0.001", value="2.32495")]
        status = await self.svc.reconcile(1)
        self.assertEqual(self.svc.sent_to("cancel-order")[-1], {"order_id": "o1"})
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertEqual(status["ledger"]["positions"]["ETH"]["units"], "0.001")
        self.assertFalse(status["ledger"]["enabled"])
        rows = ex._read_maker_log(1)
        self.assertTrue(rows[-1]["reconciled"])
        self.assertEqual(rows[-1]["outcome"], "partial")

    async def test_reconcile_leaves_the_order_uncertain_if_it_cannot_cancel(self):
        await self.ready()
        self.svc.cancel_error = ValueError("unavailable")
        self.svc.cancel_effective = False
        with self.assertRaises(ValueError):
            await self.buy()
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("could not be confirmed closed", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        self.assertEqual(self.ledger()["cash"], "40")

    async def test_an_unreadable_final_state_after_the_cancel_is_uncertain(self):
        await self.ready()
        self.svc.after_cancel_error = ex.ExchangeError(200, None, "order not found")
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("reconciliation", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        self.assertEqual(len(self.svc.sent_to("cancel-order")), 1)
        self.assertEqual(self.stats()["outcomes"]["uncertain"], 1)
        # reconcile can then settle it once the Exchange answers again
        self.svc.after_cancel_error = None
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertEqual(status["ledger"]["cash"], "40")
        # the uncertain row is replaced by the reconciled one, not counted twice
        stats = self.stats()
        self.assertEqual((stats["attempts"], stats["outcomes"]["unfilled"]), (1, 1))
        self.assertEqual(stats["outcomes"]["uncertain"], 0)

    async def test_an_order_that_filled_before_the_cancel_is_read_as_filled(self):
        await self.ready()
        self.svc.cancel_error = ex.ExchangeError(500, 316, "NO_ACTIVE_ORDER")
        self.svc.after_cancel = detail("FILLED", "0.0034", "7.90483", "0.019762")
        result = await self.buy()
        self.assertEqual(result["outcome"], "filled")
        self.assertEqual(len(self.svc.sent_to("cancel-order")), 1)
        self.assertEqual(self.ledger()["positions"]["ETH"]["units"], "0.0034")
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_poll_failures_cut_the_wait_short_and_still_cancel(self):
        await self.ready()
        self.svc.detail_error = ex.ExchangeError(200, None, "order not found")
        result = await self.buy()
        self.assertEqual(result["outcome"], "unfilled")
        self.assertEqual(len(self.svc.sent_to("cancel-order")), 1)
        self.assertLess(result["maker"]["waited_sec"], 60)

    async def test_the_wait_never_outlasts_the_tool_budget(self):
        await self.ready()
        self.svc.book_delay = 20  # slow pre-checks eat into the 90 s tool budget
        result = await self.buy(maker_wait_sec="75")
        self.assertLessEqual(result["maker"]["waited_sec"], 52.5)
        self.assertEqual(result["outcome"], "unfilled")


class MakerPreviewTests(MakerBase):
    async def test_the_preview_prices_a_resting_order_without_placing_it(self):
        await self.ready()
        p = await self.svc.preview(1, "buy", "ETH", "8", mode="maker", purpose="entry")
        self.assertEqual(p["resting_price"], "2324.95")
        self.assertEqual(p["quantity"], "0.0034")
        self.assertTrue(p["post_only"])
        self.assertEqual(p["maker_fee_pct"], "0.25")
        self.assertEqual(p["maker_fee_source"], "exchange_get_fee_rate")
        self.assertEqual(p["taker_fee_pct"], "0.5")
        self.assertEqual(p["maker_taker_round_trip_pct"], "0.75")
        self.assertEqual(p["maker_maker_round_trip_pct"], "0.5")
        self.assertEqual(p["taker_taker_round_trip_pct"], "1")
        self.assertEqual(p["estimated_round_trip_pct"], "0.75")
        self.assertEqual(p["touch_queue_quantity"], "500")
        self.assertEqual(p["maker_wait_sec"], 60)
        self.assertIn("NOT guaranteed to fill", p["fill_risk"])
        self.assertIn("Fill probability unknown", p["fill_risk"])
        self.assertTrue(p["maker_mode_enabled"])
        self.assertEqual(self.svc.sent_to("create-order"), [])
        sell = await self.svc.preview(
            1,
            "sell",
            "SHIB",
            "1215909",
            mode="maker",
            purpose="target_exit",
            maker_wait_sec="30",
        )
        self.assertEqual(sell["resting_price"], "0.000004914")
        self.assertEqual(sell["touch_queue_quantity"], "800")
        self.assertEqual(sell["maker_wait_sec"], 30)

    async def test_the_default_fee_is_labelled_when_the_rate_was_not_read(self):
        await self.ready()
        self.svc.fee_error = ValueError("Crypto.com unavailable")
        p = await self.svc.preview(1, "buy", "ETH", "8", mode="maker", purpose="entry")
        self.assertEqual(p["maker_fee_pct"], "0.25")
        self.assertEqual(p["maker_fee_source"], "default_estimate_not_read")
        self.assertEqual(p["taker_fee_source"], "default_estimate_not_read")

    async def test_a_per_pair_maker_fee_override_is_used_for_estimates_only(self):
        await self.ready()
        self.svc.set_maker_fee_override(1, "ETH_USD", "0")
        p = await self.svc.preview(1, "buy", "ETH", "8", mode="maker", purpose="entry")
        self.assertEqual(p["maker_fee_pct"], "0")
        self.assertEqual(p["maker_fee_source"], "owner_override")
        self.assertEqual(p["maker_taker_round_trip_pct"], "0.5")
        self.assertEqual(p["taker_fee_pct"], "0.5")
        other = await self.svc.preview(
            1, "sell", "SHIB", "1215909", mode="maker", purpose="target_exit"
        )
        self.assertEqual(other["maker_fee_pct"], "0.25")
        for bad in ("ETHUSD", "eth_usd"):
            with self.assertRaises(ValueError):
                self.svc.set_maker_fee_override(1, bad, "0")
        with self.assertRaises(ValueError):
            self.svc.set_maker_fee_override(1, "ETH_USD", "5")
        self.svc.set_maker_fee_override(1, "ETH_USD", None)
        self.assertEqual(self.svc.maker_stats(1)["maker_fee_overrides"], {})

    async def test_the_taker_preview_is_unchanged(self):
        await self.ready()
        p = await self.svc.preview(1, "buy", "ETH", "8")
        self.assertEqual(p["taker_fee_pct"], "0.5")
        self.assertNotIn("mode", p)
        self.assertNotIn("resting_price", p)


class MakerStatsTests(MakerBase):
    def seed(self, rows):
        ex._write(ex._maker_log_path(1), {"attempts": rows})

    def row(self, outcome, **extra):
        base = {
            "time": 1.0,
            "pair": "ETH_USD",
            "quote": "USD",
            "side": "buy",
            "purpose": "entry",
            "resting_price": "100",
            "ordered_quantity": "1",
            "outcome": outcome,
            "taker_fee_pct": "0.5",
            "filled_fraction": 0.0,
        }
        return {**base, **extra}

    def test_no_attempts_gives_empty_not_invented_numbers(self):
        stats = ex.maker_stats([])
        self.assertEqual(stats["attempts"], 0)
        self.assertIsNone(stats["fill_rate"])
        self.assertIsNone(stats["avg_fee_pct"])
        self.assertEqual(stats["recent"], [])

    def test_the_aggregates(self):
        rows = [
            self.row(
                "filled",
                value="100",
                fee_quote="0.25",
                filled_fraction=1.0,
                time_to_fill_sec=10.0,
                time=2.0,
            ),
            self.row(
                "filled",
                value="100",
                fee_quote="0.25",
                filled_fraction=1.0,
                time_to_fill_sec=20.0,
                purpose="target_exit",
                side="sell",
                time=3.0,
            ),
            self.row(
                "partial", value="50", fee_quote="0.125", filled_fraction=0.5, time=4.0
            ),
            self.row("unfilled", time=5.0),
            self.row("rejected", time=6.0),
            self.row("uncertain", time=7.0),
        ]
        self.seed(rows)
        stats = self.stats()
        self.assertEqual(stats["attempts"], 6)
        self.assertEqual(
            stats["outcomes"],
            {"filled": 2, "partial": 1, "unfilled": 1, "rejected": 1, "uncertain": 1},
        )
        self.assertEqual(stats["resolved"], 4)
        self.assertEqual(stats["fill_rate"], 0.5)
        self.assertEqual(stats["any_fill_rate"], 0.75)
        self.assertEqual(stats["avg_filled_fraction"], 0.625)
        self.assertEqual(stats["avg_seconds_to_fill"], 15.0)
        self.assertEqual(stats["filled_notional"], 250.0)
        self.assertEqual(stats["avg_fee_pct"], 0.25)
        self.assertEqual(stats["fee_paid"], 0.625)
        # 250 * 0.5% taker = 1.25, minus 0.625 actually paid
        self.assertEqual(stats["fee_saved_vs_taker"], 0.625)
        self.assertEqual(stats["unfilled_notional"], 100.0)
        self.assertEqual(stats["by_purpose"]["entry"]["attempts"], 5)
        self.assertEqual(stats["by_purpose"]["target_exit"]["fill_rate"], 1.0)
        self.assertEqual(stats["recent"][0]["outcome"], "uncertain")
        self.assertEqual(stats["first_attempt"], 2.0)
        self.assertEqual(stats["last_attempt"], 7.0)
        self.assertEqual(stats["quote"], "USD")
        full = self.svc.maker_stats(1)
        self.assertFalse(full["maker_enabled"])

    async def test_every_attempt_is_recorded_with_its_fee_and_timing(self):
        await self.ready()
        self.svc.script = [
            detail("ACTIVE"),
            detail("FILLED", "0.0034", "7.90483", "0.019762"),
        ]
        await self.buy()
        (row,) = ex._read_maker_log(1)
        for key in (
            "time",
            "pair",
            "side",
            "purpose",
            "resting_price",
            "wait_sec",
            "outcome",
            "filled_fraction",
            "fee",
            "fee_currency",
            "fee_quote",
            "taker_fee_pct",
            "time_to_fill_sec",
            "waited_sec",
        ):
            self.assertIn(key, row)
        self.assertEqual(row["pair"], "ETH_USD")
        self.assertEqual(row["wait_sec"], 60)
        self.assertEqual(row["filled_fraction"], 1.0)
        raw = json.loads(ex._maker_log_path(1).read_text())
        self.assertEqual(len(raw["attempts"]), 1)

    async def test_a_log_failure_never_breaks_an_order(self):
        await self.ready()
        self.svc.script = [detail("FILLED", "0.0034", "7.9", "0.005")]
        real = ex._write

        def failing(path, content):
            if path.name == "maker_log.json":
                raise OSError("disk full")
            return real(path, content)

        with patch.object(ex, "_write", failing):
            result = await self.buy()
        self.assertEqual(result["outcome"], "filled")
        self.assertEqual(self.ledger()["positions"]["ETH"]["units"], "0.0034")


if __name__ == "__main__":
    unittest.main()
