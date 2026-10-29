"""The paper xStocks crew's simulated account: fills, guards, valuation, reset, PnL.

No network and no money: Kraken's public API is a fake and the data dir is a temp
folder. The live Kraken module is only read, never called.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from condor import native_accounts as na
from condor import native_flows, native_venues
from condor import portfolio_history as ph
from condor import xstocks_paper as xs
from condor import xstocks_paper_ledger as ledger
from condor.crypto_com_exchange import service as exchange_service
from condor.runtime import danger
from condor.web.models import WebUser
from condor.web.routes import portfolio as portfolio_route
from condor.web.routes import xstocks_paper as paper_route

OPEN_TS = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc).timestamp()  # 11:00 EDT
CLOSED_TS = datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc).timestamp()
PAPER = native_venues.get("xstocks_paper")
USER = 1


def pair_row(base, **extra):
    return {
        "wsname": f"{base}x/USD",
        "status": "online",
        "base": f"{base}x",
        "quote": "ZUSD",
        "pair_decimals": 2,
        "lot_decimals": 8,
        "tick_size": "0.01",
        "ordermin": "0.0001",
        "costmin": "0",
        **extra,
    }


ASSET_PAIRS = {"AAPLxUSD": pair_row("AAPL"), "TSLAxUSD": pair_row("TSLA")}


def ticker(bid, ask):
    return {
        "b": [bid, "1", "1.0"],
        "a": [ask, "1", "1.0"],
        "c": [bid, "0.5"],
        "v": ["0", "1000"],
        "h": ["0", "350"],
        "l": ["0", "340"],
        "o": bid,
    }


class FakeMarket(xs.XStocksPaper):
    """Kraken's public API: pairs, ticker and an order book that can be thin."""

    def __init__(self):
        super().__init__()
        self.tickers = {
            "AAPLxUSD": ticker("345.33", "345.54"),
            "TSLAxUSD": ticker("401.44", "401.46"),
        }
        self.depth = {
            "AAPLxUSD": {
                "asks": [["345.54", "0.05"], ["345.60", "0.10"], ["346.00", "10"]],
                "bids": [["345.33", "0.1"], ["345.20", "0.2"], ["344.50", "10"]],
            },
            "TSLAxUSD": {
                "asks": [["401.46", "100"]],
                "bids": [["401.44", "100"]],
            },
        }
        self.down = False
        self.private_calls = []

    async def _get(self, path, params, *, asset_class=True):
        if self.down:
            raise ValueError("Kraken public data unavailable")
        if path == "AssetPairs":
            return ASSET_PAIRS
        if path == "Ticker":
            return {p: self.tickers[p] for p in params["pair"].split(",")}
        if path == "Depth":
            return {params["pair"]: self.depth[params["pair"]]}
        raise AssertionError(path)


def make_account(market=None, now=OPEN_TS):
    account = ledger.XStocksPaperAccount(market or FakeMarket())
    account.clock = lambda: now
    return account


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()
        os.environ.pop(ledger.SLIPPAGE_ENV, None)
        self.market = FakeMarket()
        self.acct = make_account(self.market)
        na.forget()

    def tearDown(self):
        na.forget()
        self.env.stop()
        self.tmp.cleanup()

    def stored(self):
        return ledger.read_ledger(USER)


def d(value):
    return Decimal(str(value))


class OpeningTests(Base):
    async def test_nothing_exists_until_the_account_is_used(self):
        status = self.acct.status(USER)
        self.assertFalse(status["connected"])
        self.assertTrue(status["paper"] and status["simulated"])
        self.assertIsNone(self.stored())
        self.assertEqual((await self.acct.accounts(USER))["accounts"], [])
        self.assertEqual(await self.acct.history(USER), [])

    async def test_valuation_opens_it_with_100_usd_at_the_bid(self):
        view = await self.acct.valuation(USER)
        self.assertEqual(view["equity"], "100")
        self.assertTrue(view["connected"] and view["enabled"])
        self.assertEqual(self.stored()["cash"], "100")
        self.assertEqual(self.stored()["positions"], {})
        # Below 1000 a stock may take 40% of equity.
        self.assertEqual(view["new_position_limit"], "40")
        self.assertEqual(self.stored()["uncertain_order"], None)

    async def test_the_status_states_the_assumptions_and_the_banner(self):
        status = self.acct.status(USER)
        self.assertEqual(status["assumptions"]["taker_fee_pct"], "0.08")
        self.assertEqual(status["assumptions"]["start_cash"], "100")
        self.assertIn("SIMULATED", status["banner"])
        self.assertEqual(status["limits"]["min_order_usd"], "0.5")


class BuyTests(Base):
    async def test_a_buy_walks_the_book_and_pays_the_taker_fee(self):
        result = await self.acct.order(USER, "buy", "AAPL", "30")
        order = result["order"]
        # 0.3% through the 345.54 ask, rounded up to the cent: 346.58.
        self.assertEqual(order["limit_price"], "346.58")
        quantity = d(30) / d("346.58")
        quantity = quantity.quantize(Decimal("1e-8"), rounding="ROUND_DOWN")
        self.assertEqual(d(order["requested_quantity"]), quantity)
        self.assertEqual(order["status"], "closed")
        self.assertFalse(order.get("partial"))
        self.assertEqual(order["levels"], 2)
        # Two levels, each moved 0.02% against the buyer.
        first = d("345.54") * d("1.0002")
        second = d("345.60") * d("1.0002")
        value = d("0.05") * first + (quantity - d("0.05")) * second
        self.assertEqual(d(order["value"]), value.quantize(Decimal("1e-8")))
        fee = (value * d("0.08") / 100).quantize(Decimal("1e-8"))
        self.assertEqual(d(order["fee"]), fee)
        self.assertEqual(order["fee_currency"], "USD")
        stored = self.stored()
        self.assertEqual(d(stored["cash"]), d(100) - d(order["value"]) - fee)
        position = stored["positions"]["AAPL"]
        self.assertEqual(d(position["units"]), quantity)
        self.assertEqual(d(position["cost"]), d(order["value"]) + fee)
        self.assertTrue(result["simulated"])

    async def test_equity_is_cash_plus_positions_at_the_best_bid(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        equity = d((await self.acct.valuation(USER))["equity"])
        stored = self.stored()
        expected = d(stored["cash"]) + d(stored["positions"]["AAPL"]["units"]) * d(
            "345.33"
        )
        self.assertEqual(equity, expected)
        # The spread, the slippage allowance and the fee are a real cost.
        self.assertLess(equity, 100)

    async def test_a_thin_book_gives_a_partial_fill_and_a_canceled_remainder(self):
        self.market.depth["AAPLxUSD"]["asks"] = [["345.54", "0.02"]]
        order = (await self.acct.order(USER, "buy", "AAPL", "30"))["order"]
        self.assertEqual(order["status"], "canceled")
        self.assertTrue(order["partial"])
        self.assertEqual(order["quantity"], "0.02")
        self.assertEqual(self.stored()["positions"]["AAPL"]["units"], "0.02")
        cash = d(self.stored()["cash"])
        self.assertEqual(cash, d(100) - d(order["value"]) - d(order["fee"]))

    async def test_levels_outside_the_limit_are_not_taken(self):
        self.market.depth["AAPLxUSD"]["asks"] = [["350.00", "10"]]
        order = (await self.acct.order(USER, "buy", "AAPL", "30"))["order"]
        self.assertEqual(order["quantity"], "0")
        self.assertEqual(order["status"], "canceled")
        self.assertEqual(self.stored()["cash"], "100")
        self.assertEqual(self.stored()["positions"], {})
        # It is still on the record, with nothing paid.
        self.assertEqual(len(self.stored()["orders"]), 1)

    async def test_a_wider_slippage_limit_reaches_deeper_levels(self):
        self.market.depth["AAPLxUSD"]["asks"] = [["346.50", "10"]]
        wide = await self.acct.order(USER, "buy", "AAPL", "30", "1")
        self.assertEqual(wide["order"]["status"], "closed")

    async def test_the_slippage_allowance_is_configurable_and_recorded(self):
        with patch.dict(os.environ, {ledger.SLIPPAGE_ENV: "0.1"}):
            order = (await self.acct.order(USER, "buy", "AAPL", "30"))["order"]
        self.assertEqual(order["slippage_allowance_pct"], "0.1")
        self.assertGreater(d(order["avg_price"]), d("345.88"))
        self.assertLess(d(order["avg_price"]), d("345.95"))

    async def test_a_bad_allowance_falls_back_to_the_default(self):
        for bad in ("abc", "-1", "50", "nan"):
            with patch.dict(os.environ, {ledger.SLIPPAGE_ENV: bad}):
                self.assertEqual(
                    ledger.slippage_allowance_pct(),
                    ledger.DEFAULT_SLIPPAGE_ALLOWANCE_PCT,
                )

    async def test_an_asset_may_not_exceed_forty_percent_of_equity(self):
        with self.assertRaisesRegex(ValueError, "40%"):
            await self.acct.order(USER, "buy", "AAPL", "50")
        self.assertEqual(self.stored()["orders"], [])
        # 40 is allowed, and a second 40 in the same stock is not.
        await self.acct.order(USER, "buy", "AAPL", "30")
        with self.assertRaisesRegex(ValueError, "40%"):
            await self.acct.order(USER, "buy", "AAPL", "20")

    async def test_cash_must_cover_the_purchase(self):
        await self.acct.order(USER, "buy", "AAPL", "35")
        await self.acct.order(USER, "buy", "TSLA", "35")
        with self.assertRaisesRegex(ValueError, "Not enough bot cash"):
            await self.acct.order(USER, "buy", "TSLA", "35")

    async def test_the_equity_floor_stops_new_buys_and_pauses_the_account(self):
        self.acct._open(USER)
        data = self.stored()
        data["cash"] = "1.5"
        ledger._write(ledger.ledger_path(USER), data)
        with self.assertRaisesRegex(ValueError, "stop threshold"):
            await self.acct.order(USER, "buy", "AAPL", "1")
        self.assertFalse(self.stored()["enabled"])
        with self.assertRaisesRegex(ValueError, "paused"):
            await self.acct.order(USER, "buy", "AAPL", "1")
        self.assertFalse(self.acct.status(USER)["enabled"])

    async def test_valuation_pauses_the_account_at_the_floor(self):
        self.acct._open(USER)
        data = self.stored()
        data["cash"] = "2"
        ledger._write(ledger.ledger_path(USER), data)
        await self.acct.valuation(USER)
        self.assertFalse(self.stored()["enabled"])

    async def test_guards_change_from_a_thousand_of_equity(self):
        self.acct._open(USER)
        data = self.stored()
        data["cash"] = "1100"
        ledger._write(ledger.ledger_path(USER), data)
        # One sixth of 1100 is 183.33; 200 is too much, 100 is fine.
        with self.assertRaisesRegex(ValueError, "one sixth"):
            await self.acct.order(USER, "buy", "TSLA", "200")
        await self.acct.order(USER, "buy", "TSLA", "100")
        with self.assertRaisesRegex(ValueError, "Close the existing position"):
            await self.acct.order(USER, "buy", "TSLA", "10")

    async def test_the_session_must_be_open(self):
        closed = make_account(self.market, CLOSED_TS)
        with self.assertRaisesRegex(ValueError, "outside the regular US session"):
            await closed.order(USER, "buy", "AAPL", "10")
        saturday = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc).timestamp()
        with self.assertRaisesRegex(ValueError, "outside the regular US session"):
            await make_account(self.market, saturday).order(USER, "buy", "AAPL", "10")
        self.assertEqual(self.stored()["orders"] if self.stored() else [], [])

    async def test_the_minimum_order_is_half_a_dollar(self):
        with self.assertRaisesRegex(ValueError, "minimum order"):
            await self.acct.order(USER, "buy", "AAPL", "0.4")
        await self.acct.order(USER, "buy", "AAPL", "0.6")

    async def test_only_online_usd_xstocks_and_valid_input(self):
        with self.assertRaisesRegex(ValueError, "not an online"):
            await self.acct.order(USER, "buy", "NOPE", "10")
        with self.assertRaisesRegex(ValueError, "side"):
            await self.acct.order(USER, "hold", "AAPL", "10")
        with self.assertRaisesRegex(ValueError, "Slippage above"):
            await self.acct.order(USER, "buy", "AAPL", "10", "3")
        with self.assertRaisesRegex(ValueError, "decimal"):
            await self.acct.order(USER, "buy", "AAPL", "1e3")

    async def test_ten_orders_a_minute_at_most(self):
        for _ in range(10):
            await self.acct.order(USER, "buy", "TSLA", "1")
        with self.assertRaisesRegex(ValueError, "ten orders per minute"):
            await self.acct.order(USER, "buy", "TSLA", "1")


class SellTests(Base):
    async def test_a_sale_pays_proceeds_net_of_the_fee_and_reports_the_result(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        units = d(self.stored()["positions"]["AAPL"]["units"])
        cost = d(self.stored()["positions"]["AAPL"]["cost"])
        cash = d(self.stored()["cash"])
        order = (await self.acct.order(USER, "sell", "AAPL", str(units)))["order"]
        # 0.1 at 345.33 covers it; each fill moved 0.02% against the seller.
        price = d("345.33") * d("0.9998")
        value = (units * price).quantize(Decimal("1e-8"))
        self.assertEqual(d(order["value"]), value)
        fee = (value * d("0.08") / 100).quantize(Decimal("1e-8"))
        stored = self.stored()
        self.assertNotIn("AAPL", stored["positions"])
        self.assertEqual(d(stored["cash"]), cash + value - fee)
        self.assertEqual(d(order["realized_pnl"]), value - fee - cost)
        self.assertLess(d(order["realized_pnl"]), 0)  # a round trip costs money

    async def test_a_partial_sale_keeps_the_rest_at_its_cost(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        position = self.stored()["positions"]["AAPL"]
        half = d(position["units"]) / 2
        await self.acct.order(USER, "sell", "AAPL", str(half))
        left = self.stored()["positions"]["AAPL"]
        self.assertAlmostEqual(
            float(d(left["units"])), float(d(position["units"]) - half), places=7
        )
        self.assertAlmostEqual(
            float(d(left["cost"])), float(d(position["cost"]) / 2), places=6
        )

    async def test_nothing_can_be_sold_that_the_account_does_not_hold(self):
        with self.assertRaisesRegex(ValueError, "outside the bot allocation"):
            await self.acct.order(USER, "sell", "AAPL", "0.01")
        await self.acct.order(USER, "buy", "AAPL", "10")
        with self.assertRaisesRegex(ValueError, "outside the bot allocation"):
            await self.acct.order(USER, "sell", "AAPL", "5")

    async def test_a_thin_bid_book_sells_only_what_it_can(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        units = d(self.stored()["positions"]["AAPL"]["units"])
        self.market.depth["AAPLxUSD"]["bids"] = [["345.33", "0.01"]]
        order = (await self.acct.order(USER, "sell", "AAPL", str(units)))["order"]
        self.assertEqual(order["status"], "canceled")
        self.assertEqual(order["quantity"], "0.01")
        left = d(self.stored()["positions"]["AAPL"]["units"])
        self.assertEqual(left, units - d("0.01"))


class IdempotencyAndPreviewTests(Base):
    async def test_the_same_client_id_is_never_filled_twice(self):
        first = await self.acct.order(USER, "buy", "AAPL", "10", client_id="tick-1")
        cash = self.stored()["cash"]
        again = await self.acct.order(USER, "buy", "AAPL", "10", client_id="tick-1")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["order"]["order_id"], first["order"]["order_id"])
        self.assertEqual(self.stored()["cash"], cash)
        self.assertEqual(len(self.stored()["orders"]), 1)

    async def test_a_client_id_must_be_plain(self):
        with self.assertRaisesRegex(ValueError, "client_id"):
            await self.acct.order(USER, "buy", "AAPL", "10", client_id="bad id!")

    async def test_the_preview_writes_nothing_and_shows_the_simulated_fill(self):
        preview = await self.acct.preview(USER, "buy", "AAPL", "30")
        self.assertIsNone(self.stored())
        self.assertTrue(preview["simulated"])
        self.assertEqual(preview["taker_fee_pct"], "0.08")
        self.assertTrue(preview["book_covers_order"])
        # Spread 0.061% plus (0.08 + 0.02) on each of two legs.
        self.assertEqual(preview["estimated_round_trip_pct"], "0.261")
        self.assertIsNone(preview["blocked_reason"])
        self.assertTrue(preview["market_open"])
        # The preview and the order agree on the fill.
        order = (await self.acct.order(USER, "buy", "AAPL", "30"))["order"]
        self.assertEqual(preview["estimated_average_price"], order["avg_price"])
        self.assertEqual(preview["estimated_fee_usd"], order["fee"])

    async def test_a_preview_says_why_the_order_would_be_refused(self):
        await self.acct.valuation(USER)
        preview = await self.acct.preview(USER, "buy", "AAPL", "60")
        self.assertIn("40%", preview["blocked_reason"])
        closed = make_account(self.market, CLOSED_TS)
        out = await closed.preview(USER, "buy", "AAPL", "10")
        self.assertFalse(out["market_open"])
        self.assertIn("outside the regular US session", out["blocked_reason"])

    async def test_a_preview_flags_a_book_that_cannot_cover_the_order(self):
        self.market.depth["AAPLxUSD"]["asks"] = [["345.54", "0.02"]]
        preview = await self.acct.preview(USER, "buy", "AAPL", "30")
        self.assertFalse(preview["book_covers_order"])
        self.assertEqual(preview["estimated_filled_quantity"], "0.02")


class ViewTests(Base):
    async def test_accounts_history_and_portfolio_views(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        accounts = (await self.acct.accounts(USER))["accounts"]
        self.assertEqual(accounts[0]["currency"], "USD")
        self.assertEqual(accounts[1]["currency"], "AAPLx")
        history = await self.acct.history(USER)
        self.assertEqual(history[0]["side"], "buy")
        self.assertEqual(history[0]["pair"], "AAPLxUSD")
        self.assertTrue(history[0]["simulated"])
        self.assertEqual(history[0]["fee_currency"], "USD")
        view = await self.acct.portfolio(USER)
        self.assertTrue(view["paper"])
        self.assertEqual(view["connector"], "Kraken xStocks (paper)")
        self.assertEqual([b["token"] for b in view["balances"]], ["USD", "AAPLx"])
        equity = d((await self.acct.valuation(USER))["equity"])
        self.assertAlmostEqual(view["total_usd"], float(equity), places=6)

    async def test_the_view_needs_an_opened_account_and_a_price(self):
        with self.assertRaises(ValueError):
            await self.acct.portfolio(USER)
        await self.acct.order(USER, "buy", "AAPL", "10")
        self.market.down = True
        with self.assertRaises(ValueError):
            await self.acct.portfolio(USER)

    async def test_users_do_not_share_an_account(self):
        await self.acct.order(1, "buy", "AAPL", "10")
        self.assertIsNone(ledger.read_ledger(2))
        self.assertFalse(self.acct.status(2)["connected"])
        with self.assertRaises(ValueError):
            ledger.read_ledger(0)


class ResetTests(Base):
    async def test_a_reset_archives_the_old_ledger_and_starts_again(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        old = self.stored()
        result = await self.acct.reset(USER)
        new = self.stored()
        self.assertEqual(new["cash"], "100")
        self.assertEqual(new["positions"], {})
        self.assertEqual(new["orders"], [])
        self.assertTrue(new["enabled"])
        entry = result["reset"]
        archive = ledger._root(USER) / "archive" / entry["archive"]
        saved = json.loads(archive.read_text())
        self.assertEqual(saved["ledger"], old)
        self.assertAlmostEqual(entry["equity_before"], saved["equity_at_reset"])
        self.assertLess(entry["equity_before"], 100)
        self.assertFalse(entry["estimated"])
        self.assertEqual(entry["equity_after"], 100.0)

    async def test_a_reset_lifts_a_pause(self):
        await self.acct.valuation(USER)
        data = self.stored()
        data["cash"] = "1"
        data["enabled"] = False
        ledger._write(ledger.ledger_path(USER), data)
        await self.acct.reset(USER)
        self.assertTrue(self.stored()["enabled"])

    async def test_a_reset_does_not_need_the_market(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        cost = d(self.stored()["cash"]) + d(self.stored()["positions"]["AAPL"]["cost"])
        self.market.down = True
        entry = (await self.acct.reset(USER))["reset"]
        self.assertTrue(entry["estimated"])
        self.assertAlmostEqual(entry["equity_before"], float(cost), places=6)

    async def test_the_first_opening_is_not_an_adjustment(self):
        await self.acct.reset(USER)
        self.assertEqual(self.acct.adjustments(USER, 0, OPEN_TS + 10), 0.0)
        self.assertEqual(self.stored()["cash"], "100")

    async def test_adjustments_count_only_resets_inside_the_window(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        entry = (await self.acct.reset(USER))["reset"]
        move = 100 - entry["equity_before"]
        self.assertGreater(move, 0)
        self.assertAlmostEqual(self.acct.adjustments(USER, OPEN_TS - 1, OPEN_TS), move)
        # Half-open: a reset at the start belongs to the earlier window.
        self.assertEqual(self.acct.adjustments(USER, OPEN_TS, OPEN_TS + 5), 0.0)
        self.assertEqual(self.acct.adjustments(USER, OPEN_TS - 9, OPEN_TS - 1), 0.0)

    async def test_the_reset_log_survives_a_second_reset(self):
        await self.acct.reset(USER)  # the first opening archives nothing
        await self.acct.order(USER, "buy", "AAPL", "10")
        await self.acct.reset(USER)
        await self.acct.reset(USER)
        self.assertEqual(len(ledger.read_resets(USER)), 3)
        self.assertEqual(len(list((ledger._root(USER) / "archive").iterdir())), 2)


class RegistryAndPnlTests(Base):
    def services(self):
        return {"xstocks_paper": self.acct}

    async def test_the_paper_venue_is_a_ledger_venue_but_never_a_real_one(self):
        self.assertTrue(PAPER.paper and PAPER.account)
        self.assertIn(PAPER, native_venues.ledger_venues())
        self.assertNotIn(PAPER, native_venues.account_venues())
        self.assertEqual(
            native_venues.paper_labels(), frozenset({"Kraken xStocks (paper)"})
        )
        self.assertEqual(PAPER.service, ledger.service)
        self.assertNotIn(PAPER.label, ph.SOURCES)

    async def test_every_valuation_is_an_equity_sample_for_the_loop_card(self):
        await self.acct.valuation(USER)
        first, last = ph.bounds(USER, PAPER.label)
        self.assertEqual(tuple(first), (int(OPEN_TS), 100.0))
        self.assertEqual(first, last)

    async def test_the_session_pnl_is_the_equity_change(self):
        await self.acct.valuation(USER)
        ph.record(USER, PAPER.label, 100.0, now=OPEN_TS - 60)
        await self.acct.order(USER, "buy", "AAPL", "30")
        equity = float((await self.acct.valuation(USER))["equity"])
        out = await na.performance(
            PAPER,
            USER,
            [na.SessionSpan(1, OPEN_TS - 60, live=True)],
            services=self.services(),
            now=OPEN_TS,
        )
        self.assertEqual(out.unavailable, "")
        self.assertAlmostEqual(out.sessions[0].pnl, equity - 100.0, places=6)
        self.assertAlmostEqual(out.total_pnl, equity - 100.0, places=6)
        self.assertLess(out.total_pnl, 0)
        self.assertEqual(out.open_positions, 1)
        # Flows are known (none by design) and the figure says it is simulated.
        self.assertTrue(out.flows_tracked)
        self.assertEqual(out.flows_usd, 0.0)
        self.assertIn("Paper test only", out.sessions[0].note)
        self.assertIn("Paper test only", out.note)

    async def test_a_reset_is_a_new_baseline_not_a_gain_or_a_loss(self):
        await self.acct.valuation(USER)
        ph.record(USER, PAPER.label, 100.0, now=OPEN_TS - 600)
        await self.acct.order(USER, "buy", "AAPL", "30")
        traded = float((await self.acct.valuation(USER))["equity"])
        ph.record(USER, PAPER.label, traded, now=OPEN_TS - 300)
        self.acct.clock = lambda: OPEN_TS - 100
        await self.acct.reset(USER)
        self.acct.clock = lambda: OPEN_TS
        out = await na.performance(
            PAPER,
            USER,
            [na.SessionSpan(1, OPEN_TS - 600, live=True)],
            services=self.services(),
            now=OPEN_TS,
        )
        # Now 100 again; the session's trading cost stays, the reset adds nothing.
        self.assertAlmostEqual(out.sessions[0].pnl, traded - 100.0, places=6)
        self.assertAlmostEqual(out.flows_usd, 100.0 - traded, places=6)

    async def test_a_session_after_a_reset_starts_from_the_baseline(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        self.acct.clock = lambda: OPEN_TS - 500
        await self.acct.reset(USER)  # records the 100 baseline sample
        ph.record(USER, PAPER.label, 100.0, now=OPEN_TS - 200)
        self.acct.clock = lambda: OPEN_TS
        out = await na.performance(
            PAPER,
            USER,
            [na.SessionSpan(2, OPEN_TS - 200, live=True)],
            services=self.services(),
            now=OPEN_TS,
        )
        self.assertAlmostEqual(out.sessions[0].pnl, 0.0, places=6)

    async def test_the_dashboard_lists_it_flagged_and_records_its_history(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        result = await na.collect_views(USER, services=self.services())
        paper = [v for v in result.views if v.get("paper")]
        self.assertEqual(len(paper), 1)
        self.assertEqual(paper[0]["connector"], "Kraken xStocks (paper)")
        history = ph.series(USER, 0, include_paper=True)
        self.assertIn(PAPER.label, history)

    async def test_an_unopened_account_is_not_shown(self):
        result = await na.collect_views(USER, services=self.services())
        self.assertEqual(result.views, [])
        self.assertEqual(result.notes, [])

    async def test_the_paper_account_is_in_the_overview_but_not_the_total(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        out = await na.overview(USER, services=self.services(), now=OPEN_TS)
        entry = next(v for v in out["venues"] if v["venue"] == "xstocks_paper")
        self.assertTrue(entry["paper"])
        self.assertTrue(entry["connected"])
        self.assertLess(entry["account"]["total_usd"], 100)
        self.assertIn("simulated", entry["account"]["note"].lower())
        self.assertIn("no deposits or withdrawals", entry["flows"]["note"].lower())
        self.assertEqual(out["total_usd"], 0.0)
        self.assertTrue(any("paper" in n for n in out["notes"]))

    async def test_a_paper_sample_never_enters_the_real_chart_series(self):
        ph.record(USER, PAPER.label, 100.0, now=OPEN_TS)
        ph.record(USER, ph.EXCHANGE, 50.0, now=OPEN_TS)
        self.assertEqual(list(ph.series(USER, 0)), [ph.EXCHANGE])
        self.assertEqual(
            set(ph.series(USER, 0, include_paper=True)), {ph.EXCHANGE, PAPER.label}
        )
        raw = ph.series(USER, 0, include_paper=True)
        base = SimpleNamespace(timestamp=OPEN_TS - 10, total_usd=5.0, tokens={})
        merged = ph.merge([base], raw, 300, point_type=SimpleNamespace)
        self.assertEqual(merged[-1].total_usd, 55.0)

    async def test_the_recorder_samples_it_under_its_own_label(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        await ph.sample_user(USER, xstocks_paper=self.acct)
        self.assertIn(PAPER.label, ph.series(USER, 0, include_paper=True))
        self.assertIn(USER, ph._users())

    async def test_it_has_no_deposits_by_design(self):
        with self.assertRaisesRegex(ValueError, "no deposits"):
            native_flows.window_net(USER, "xstocks_paper", 0, 10)
        with self.assertRaisesRegex(ValueError, "no deposits"):
            native_flows.add_flow(
                USER,
                "xstocks_paper",
                amount="5",
                currency="USD",
                ts=OPEN_TS,
                usd_value="5",
                source="manual",
                status="confirmed",
            )


class PortfolioRouteTests(Base):
    async def test_the_route_flags_it_and_keeps_it_out_of_the_real_total(self):
        await self.acct.order(USER, "buy", "AAPL", "30")
        sds = SimpleNamespace(
            get_or_fetch=AsyncMock(
                return_value={
                    "master": {"binance": [{"token": "BTC", "units": 1, "value": 100}]}
                }
            )
        )
        with (
            patch.object(portfolio_route, "get_server_data_service", lambda: sds),
            patch.object(ledger, "service", self.acct),
            patch.object(exchange_service, "status", lambda uid: {"connected": False}),
        ):
            response = await portfolio_route.get_portfolio(
                "vm08", False, WebUser(id=USER, role="admin")
            )
        connectors = {c.connector: c for c in response.connectors}
        paper = connectors["Kraken xStocks (paper)"]
        self.assertTrue(paper.paper)
        self.assertGreater(paper.total_usd, 90)
        self.assertEqual(response.total_usd, 100)
        self.assertFalse(connectors["binance"].paper)


class RouteTests(Base):
    def setUp(self):
        super().setUp()
        self.user = WebUser(id=USER, username="o", first_name="O", role="admin")
        self.patches = [
            patch.object(paper_route, "account", self.acct),
            patch.object(paper_route, "service", self.market),
            patch.object(paper_route, "require_admin", lambda user: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    async def test_status_merges_the_public_service_and_the_account(self):
        status = await paper_route.status(self.user)
        self.assertTrue(status["paper_only"])
        self.assertIn("ledger", status)
        self.assertFalse(status["connected"])

    async def test_preview_order_history_and_valuation_routes(self):
        body = paper_route.OrderRequest(side="buy", symbol="AAPL", amount="20")
        preview = await paper_route.preview(body, self.user)
        self.assertTrue(preview["simulated"])
        body.client_id = "t1"
        placed = await paper_route.order(body, self.user)
        self.assertEqual(placed["order"]["status"], "closed")
        again = await paper_route.order(body, self.user)
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(await paper_route.history(self.user)), 1)
        self.assertIn("equity", await paper_route.valuation(self.user))
        self.assertEqual(
            (await paper_route.accounts(self.user))["accounts"][0]["currency"], "USD"
        )

    async def test_a_refusal_is_a_400(self):
        body = paper_route.OrderRequest(side="buy", symbol="AAPL", amount="90")
        with self.assertRaises(HTTPException) as caught:
            await paper_route.order(body, self.user)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("40%", caught.exception.detail)

    async def test_reset_is_gated_on_admin(self):
        denied = HTTPException(status_code=403, detail="Admin access required")

        def refuse(user):
            raise denied

        with patch.object(paper_route, "require_admin", refuse):
            with self.assertRaises(HTTPException) as caught:
                await paper_route.reset(self.user)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIsNone(self.stored())  # a refused reset opened and changed nothing
        out = await paper_route.reset(self.user)
        self.assertEqual(out["reset"]["equity_after"], 100.0)

    async def test_the_reset_route_is_not_a_tool_action(self):
        from mcp_servers.condor import server as condor_server

        tool = type(condor_server.mcp)("probe")
        condor_server.register_tools(tool, "full")
        listed = {t.name: t for t in await tool.list_tools()}
        actions = listed["xstocks_paper"].model_dump(by_alias=True)["inputSchema"][
            "properties"
        ]["action"]["enum"]
        self.assertNotIn("reset", actions)
        self.assertEqual(
            set(actions),
            {
                "status",
                "valuation",
                "history",
                "coins",
                "candles",
                "preview",
                "order",
                "predict",
                "scorecard",
            },
        )


class DangerClassTests(unittest.TestCase):
    def call(self, action):
        return {"tool": "xstocks_paper", "input": {"action": action}}

    def test_an_order_is_recorded_but_needs_no_human_because_it_is_simulated(self):
        self.assertFalse(danger.is_dangerous_tool_call(self.call("order")))
        self.assertTrue(danger.is_mutating_tool_call(self.call("order")))

    def test_reads_and_scorecard_calls_are_not_recorded_as_changes(self):
        for action in (
            "status",
            "valuation",
            "history",
            "coins",
            "candles",
            "preview",
            "predict",
            "scorecard",
        ):
            self.assertFalse(danger.is_mutating_tool_call(self.call(action)), action)

    def test_an_unknown_action_fails_open(self):
        self.assertTrue(danger.is_mutating_tool_call(self.call("reset")))

    def test_the_live_tool_is_still_a_confirmed_order(self):
        live_call = {"tool": "kraken_xstocks", "input": {"action": "order"}}
        self.assertTrue(danger.is_dangerous_tool_call(live_call))


if __name__ == "__main__":
    unittest.main()
