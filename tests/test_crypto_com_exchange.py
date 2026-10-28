"""Exchange service: signing, sizing, guards, fills and reconciliation. No network."""

import hashlib
import hmac
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from condor import crypto_com_exchange as ex

INSTRUMENTS = [
    {
        "symbol": "SHIB_USD",
        "inst_type": "CCY_PAIR",
        "base_ccy": "SHIB",
        "quote_ccy": "USD",
        "tradable": True,
        "price_tick_size": "0.000000001",
        "qty_tick_size": "10000",
    },
    {
        "symbol": "ETH_USD",
        "inst_type": "CCY_PAIR",
        "base_ccy": "ETH",
        "quote_ccy": "USD",
        "tradable": True,
        "price_tick_size": "0.01",
        "qty_tick_size": "0.0001",
    },
]


def ticker(pair, bid, ask, volume="1000000"):
    return {"i": pair, "b": bid, "k": ask, "a": ask, "c": "0.01", "vv": volume}


class FakeService(ex.ExchangeService):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.tickers = {
            "SHIB_USD": ticker("SHIB_USD", "0.000004912", "0.000004914"),
            "ETH_USD": ticker("ETH_USD", "2324.95", "2324.96"),
        }
        self.balances = [
            {"currency": "USD", "balance": "40", "available": "40"},
            {"currency": "SHIB", "balance": "1215909", "available": "1215909"},
        ]
        self.order_detail = None
        self.fail_create = False
        self.reject_create = None
        self.detail_missing = False

    def environment(self, user_id):
        return "production"

    def credentials(self, user_id):
        return "key", "secret"

    async def public(self, user_id, path, params=None):
        if path == "get-instruments":
            return {"data": INSTRUMENTS}
        if path == "get-tickers":
            return {"data": list(self.tickers.values())}
        if path == "get-book":
            pair = params["instrument_name"]
            t = self.tickers[pair]
            return {
                "data": [
                    {
                        "bids": [[t["b"], "100000000000", "1"]],
                        "asks": [[t["k"], "100000000000", "1"]],
                    }
                ]
            }
        raise AssertionError(path)

    async def private(self, user_id, method, params=None):
        self.sent.append((method, params))
        if method == "user-balance":
            return {
                "data": [
                    {
                        "position_balances": [
                            {
                                "instrument_name": b["currency"],
                                "quantity": b["balance"],
                                "reserved_qty": str(
                                    Decimal(b["balance"]) - Decimal(b["available"])
                                ),
                            }
                            for b in self.balances
                        ]
                    }
                ]
            }
        if method == "get-fee-rate":
            return {
                "effective_spot_maker_rate_bps": "25.0",
                "effective_spot_taker_rate_bps": "50.0",
            }
        if method == "create-order":
            if self.reject_create:
                raise ex.ExchangeError(*self.reject_create)
            if self.fail_create:
                raise ValueError("Crypto.com Exchange unavailable")
            return {"order_id": "o1", "client_oid": params["client_oid"]}
        if method == "get-order-detail":
            if self.detail_missing:
                raise ex.ExchangeError(200, 213, "not found")
            if not self.order_detail:
                return {}
            return {**self.order_detail, "client_oid": self._oid()}
        if method == "get-order-history":
            return {"data": []}
        raise AssertionError(method)

    def _oid(self):
        for method, params in reversed(self.sent):
            if method == "create-order":
                return params["client_oid"]
        return None


def filled(side, quantity, value, fee="0", fee_currency="USD", status="FILLED"):
    return {
        "order_id": "o1",
        "status": status,
        "side": side,
        "cumulative_quantity": quantity,
        "cumulative_value": value,
        "cumulative_fee": fee,
        "fee_instrument_name": fee_currency,
    }


class ExchangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()
        self.svc = FakeService()
        ex._write(ex.credentials_path(1), {"api_key": "key", "api_secret": "secret"})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def synced(self, enabled=True):
        await self.svc.sync_from_account(1)
        if enabled:
            self.svc.set_enabled(1, True)

    def ledger(self):
        return ex._read_ledger(1)

    # ── signing ──

    def test_params_are_flattened_in_key_order(self):
        self.assertEqual(
            ex.params_string({"side": "BUY", "instrument_name": "BTC_USD"}),
            "instrument_nameBTC_USDsideBUY",
        )
        self.assertEqual(ex.params_string({"x": None}), "xnull")
        self.assertEqual(ex.params_string({"o": {"b": "1", "a": "2"}}), "oa2b1")

    def test_signature_covers_method_id_key_params_and_nonce(self):
        body = ex.signed_body(
            "key",
            "secret",
            "private/create-order",
            {"side": "BUY"},
            nonce=5,
            request_id=7,
        )
        payload = "private/create-order" + "7" + "key" + "sideBUY" + "5"
        expected = hmac.new(b"secret", payload.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(body["sig"], expected)
        self.assertEqual((body["id"], body["nonce"]), (7, 5))

    # ── sync ──

    async def test_sync_seeds_cash_and_positions_from_the_account(self):
        result = await self.svc.sync_from_account(1)
        ledger = self.ledger()
        self.assertEqual(ledger["cash"], "40")
        self.assertEqual(ledger["positions"]["SHIB"]["units"], "1215909")
        self.assertTrue(ledger["synced"])
        self.assertFalse(ledger["enabled"])
        self.assertEqual(result["skipped"], [])

    async def test_cannot_enable_before_the_first_sync(self):
        with self.assertRaises(ValueError):
            self.svc.set_enabled(1, True)

    # ── orders ──

    async def test_buy_is_an_immediate_limit_through_the_ask_in_whole_lots(self):
        await self.synced()
        self.svc.order_detail = filled("BUY", "0.0034", "7.9", "0.005")
        await self.svc.order(1, "buy", "ETH", "8")
        method, params = next(s for s in self.svc.sent if s[0] == "create-order")
        self.assertEqual(params["type"], "LIMIT")
        self.assertEqual(params["time_in_force"], "IMMEDIATE_OR_CANCEL")
        self.assertEqual(params["side"], "BUY")
        self.assertGreater(Decimal(params["price"]), Decimal("2324.96"))
        self.assertEqual(Decimal(params["quantity"]) % Decimal("0.0001"), 0)
        ledger = self.ledger()
        self.assertEqual(ledger["positions"]["ETH"]["units"], "0.0034")
        self.assertEqual(Decimal(ledger["cash"]), Decimal("40") - Decimal("7.905"))
        self.assertIsNone(ledger["uncertain_order"])
        self.assertTrue(ledger["enabled"])

    async def test_sell_rounds_down_to_the_lot_size(self):
        await self.synced()
        self.svc.order_detail = filled("SELL", "1210000", "5.94", "0.003")
        await self.svc.order(1, "sell", "SHIB", "1215909")
        _, params = next(s for s in self.svc.sent if s[0] == "create-order")
        self.assertEqual(params["quantity"], "1210000")
        self.assertLess(Decimal(params["price"]), Decimal("0.000004912"))
        position = self.ledger()["positions"]["SHIB"]
        self.assertEqual(position["units"], "5909")
        self.assertEqual(
            Decimal(self.ledger()["cash"]), Decimal("40") + Decimal("5.937")
        )

    async def test_an_order_that_does_not_fill_leaves_the_ledger_alone(self):
        await self.synced()
        self.svc.order_detail = filled("BUY", "0", "0", status="CANCELED")
        await self.svc.order(1, "buy", "ETH", "8")
        ledger = self.ledger()
        self.assertEqual(ledger["cash"], "40")
        self.assertNotIn("ETH", ledger["positions"])
        self.assertEqual(ledger["orders"][-1]["status"], "CANCELED")

    async def test_one_asset_cannot_exceed_forty_percent_of_equity(self):
        await self.synced()
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "ETH", "30")
        self.assertIn("40%", str(caught.exception))
        self.assertFalse([s for s in self.svc.sent if s[0] == "create-order"])

    async def test_a_wide_spread_is_refused(self):
        await self.synced()
        self.svc.tickers["ETH_USD"] = ticker("ETH_USD", "2300", "2400")
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "ETH", "8")
        self.assertIn("spread", str(caught.exception))

    async def test_paused_trading_places_nothing(self):
        await self.synced(enabled=False)
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "ETH", "8")
        self.assertFalse([s for s in self.svc.sent if s[0] == "create-order"])

    async def test_cannot_sell_what_the_bot_does_not_own(self):
        await self.synced()
        with self.assertRaises(ValueError):
            await self.svc.order(1, "sell", "ETH", "0.1")

    # ── uncertain outcomes ──

    async def test_a_definite_rejection_is_not_an_uncertain_order(self):
        await self.synced()
        self.svc.reject_create = (401, 40101, "Authentication failure")
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "sell", "SHIB", "1000000")
        self.assertIn("rejected", str(caught.exception))
        self.assertIn("40101", str(caught.exception))
        ledger = self.ledger()
        self.assertIsNone(ledger["uncertain_order"])
        self.assertTrue(ledger["enabled"])
        self.assertEqual(ledger["positions"]["SHIB"]["units"], "1215909")

    async def test_a_server_side_failure_stays_uncertain(self):
        await self.synced()
        self.svc.reject_create = (400, 50001, "ERR_INTERNAL")
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "ETH", "8")
        self.assertIn("reconciliation", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_an_empty_order_detail_means_the_order_does_not_exist(self):
        await self.synced()
        self.svc.fail_create = True
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "ETH", "8")
        ledger = self.ledger()
        ledger["uncertain_order"]["time"] -= 600
        ex._write(ex._root(1) / "ledger.json", ledger)
        self.svc.order_detail = {}
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])

    async def test_a_failed_send_pauses_trading_until_reconciled(self):
        await self.synced()
        self.svc.fail_create = True
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "ETH", "8")
        self.assertIn("reconciliation", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        self.assertFalse(self.svc.status(1)["enabled"])
        with self.assertRaises(ValueError):
            self.svc.set_enabled(1, True)

    async def test_reconcile_applies_an_order_that_did_fill(self):
        await self.synced()
        self.svc.fail_create = True
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "ETH", "8")
        self.svc.order_detail = filled("BUY", "0.0034", "7.9")
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertEqual(status["ledger"]["positions"]["ETH"]["units"], "0.0034")
        self.assertFalse(status["ledger"]["enabled"])

    async def test_reconcile_clears_an_order_the_exchange_never_saw(self):
        await self.synced()
        self.svc.fail_create = True
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "ETH", "8")
        ledger = self.ledger()
        ledger["uncertain_order"]["time"] -= 600
        ex._write(ex._root(1) / "ledger.json", ledger)
        self.svc.detail_missing = True
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertEqual(status["ledger"]["cash"], "40")

    # ── market data ──

    async def test_coins_lists_the_quote_currency_pairs_by_volume(self):
        await self.synced(enabled=False)
        self.svc.tickers["ETH_USD"]["vv"] = "9000000"
        data = await self.svc.coins(1, "")
        self.assertEqual([c["symbol"] for c in data["coins"]], ["ETH", "SHIB"])
        self.assertEqual(data["coins"][1]["min_quantity_step"], "10000")
        found = await self.svc.coins(1, "shi")
        self.assertEqual([c["symbol"] for c in found["coins"]], ["SHIB"])

    async def test_preview_prices_the_book_without_placing_anything(self):
        await self.synced()
        preview = await self.svc.preview(1, "buy", "ETH", "8")
        self.assertTrue(preview["book_covers_order"])
        self.assertEqual(preview["estimated_average_price"], "2324.96")
        self.assertEqual(preview["taker_fee_pct"], "0.5")
        # one spread plus a taker fee each way
        self.assertGreaterEqual(
            Decimal(preview["estimated_round_trip_pct"]), Decimal("1")
        )
        self.assertFalse([s for s in self.svc.sent if s[0] == "create-order"])


if __name__ == "__main__":
    unittest.main()


class PortfolioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def test_portfolio_values_the_account_in_usd(self):
        svc = FakeService()
        svc.tickers["BTC_USD"] = ticker("BTC_USD", "60000", "60010")
        svc.tickers["BTC_EUR"] = ticker("BTC_EUR", "55000", "55010")
        svc.balances = [
            {"currency": "USD", "balance": "40", "available": "30"},
            {"currency": "EUR", "balance": "10", "available": "10"},
            {"currency": "ETH", "balance": "0.5", "available": "0.5"},
            {"currency": "ZZZ", "balance": "3", "available": "3"},
        ]
        data = await svc.portfolio(1)
        by = {b["token"]: b for b in data["balances"]}
        self.assertEqual(by["USD"]["usd_value"], 40)
        self.assertAlmostEqual(by["EUR"]["usd_value"], 10 * 60010 / 55010, places=4)
        self.assertAlmostEqual(by["ETH"]["usd_value"], 0.5 * 2324.96, places=4)
        self.assertEqual(by["ZZZ"]["usd_value"], 0)
        self.assertIn("ZZZ", data["note"])
