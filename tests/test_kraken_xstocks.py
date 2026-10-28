"""Kraken xStocks service: signing, sizing, guards, fills and reconciliation. No network."""

import base64
import hashlib
import hmac
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qsl

from condor import crypto_com_scorecard as sc
from condor import kraken_xstocks as ks
from condor import xstocks_paper as xs

SECRET = base64.b64encode(b"a-test-secret-that-is-not-real").decode()
OPEN_TS = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc).timestamp()  # 11:00 EDT
CLOSED_TS = datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc).timestamp()
SATURDAY_TS = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc).timestamp()


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
        "costmin": "0.5",
        **extra,
    }


ASSET_PAIRS = {
    "AAPLxUSD": pair_row("AAPL"),
    "TSLAxUSD": pair_row("TSLA"),
    "SPYxUSD": pair_row("SPY"),
    "TSLASPVUSD": {"wsname": "TSLAx/USD", "status": "online"},
    "AAPLxEUR": pair_row("AAPL", wsname="AAPLx/EUR"),
    "OLDxUSD": pair_row("OLD", status="cancel_only"),
}


def ticker(bid, ask, last, volume="1000"):
    return {
        "b": [bid, "1", "1.0"],
        "a": [ask, "1", "1.0"],
        "c": [last, "0.5"],
        "v": ["0", volume],
        "h": ["0", str(Decimal(last) * Decimal("1.02"))],
        "l": ["0", str(Decimal(last) * Decimal("0.98"))],
        "o": last,
    }


class FakePaper(xs.XStocksPaper):
    """Kraken's public API, as the paper test already reads it."""

    def __init__(self):
        super().__init__()
        self.tickers = {
            "AAPLxUSD": ticker("345.33", "345.54", "345.41", "117"),
            "TSLAxUSD": ticker("401.44", "401.46", "401.45", "1480"),
            "SPYxUSD": ticker("771.14", "771.15", "771.15", "163"),
        }

    async def _get(self, path, params):
        if path == "AssetPairs":
            return ASSET_PAIRS
        if path == "Ticker":
            # Kraken may key a ticker by the ws name; both spellings must work.
            return {
                ("TSLAx/USD" if p == "TSLAxUSD" else p): self.tickers[p]
                for p in params["pair"].split(",")
                if p in self.tickers
            }
        if path == "Depth":
            t = self.tickers[params["pair"]]
            return {
                params["pair"]: {
                    "bids": [[t["b"][0], "1000", 1793200000]],
                    "asks": [[t["a"][0], "1000", 1793200000]],
                }
            }
        if path == "OHLC":
            rows = [
                [1793200000 + i * 900, "1", "2", "0.5", "1.5", "1.2", "10", 3]
                for i in range(5)
            ]
            return {params["pair"]: rows, "last": 1}
        raise AssertionError(path)


def info(side, vol, vol_exec, cost, fee, status="closed", cid=None, **extra):
    return {
        "status": status,
        "vol": vol,
        "vol_exec": vol_exec,
        "cost": cost,
        "fee": fee,
        "price": (
            "0" if Decimal(vol_exec) == 0 else str(Decimal(cost) / Decimal(vol_exec))
        ),
        "oflags": "fciq",
        "cl_ord_id": cid,
        "opentm": OPEN_TS,
        "closetm": OPEN_TS + 1,
        "descr": {"pair": "AAPLxUSD", "type": side, "price": "346.58"},
        **extra,
    }


class FakeService(ks.KrakenXStocksService):
    def __init__(self):
        super().__init__()
        self.paper = FakePaper()
        self.clock = lambda: OPEN_TS
        self.sent = []
        self.balances = {
            "ZUSD": {"balance": "40", "hold_trade": "0"},
            "AAPLx": {"balance": "0.1", "hold_trade": "0"},
            "XXBT": {"balance": "0.001", "hold_trade": "0"},
        }
        self.balanceex_refused = False
        self.fees = {"fee": "0.4000"}  # taker percent; None means TradeVolume fails
        self.reject_add = None  # KrakenError args
        self.fail_add = False
        self.no_txid = False
        self.record = None  # the order record QueryOrders returns for our txid
        self.query_error = None
        self.open_orders = {}
        self.closed = {}

    def credentials(self, user_id):
        return "key", SECRET

    def cid(self):
        for method, params in reversed(self.sent):
            if method == "AddOrder":
                return params["cl_ord_id"]
        return None

    async def private(self, user_id, method, params=None):
        self.sent.append((method, params))
        if method == "BalanceEx":
            if self.balanceex_refused:
                raise ks.KrakenError(200, ["EGeneral:Invalid arguments"])
            return self.balances
        if method == "Balance":
            return {k: v["balance"] for k, v in self.balances.items()}
        if method == "TradeVolume":
            if self.fees is None:
                raise ks.KrakenError(200, ["EGeneral:Permission denied"])
            return {
                "currency": "ZUSD",
                "volume": "0",
                "fees": {params["pair"]: self.fees},
                "fees_maker": {params["pair"]: {"fee": "0.2500"}},
            }
        if method == "AddOrder":
            if self.reject_add:
                raise ks.KrakenError(*self.reject_add)
            if self.fail_add:
                raise ValueError("Kraken unavailable")
            return {} if self.no_txid else {"txid": ["OABC-123"]}
        if method == "QueryOrders":
            if self.query_error:
                raise ks.KrakenError(*self.query_error)
            if self.record and params["txid"] == "OABC-123":
                return {"OABC-123": {**self.record, "cl_ord_id": self.cid()}}
            return {}
        if method == "OpenOrders":
            return {"open": self.open_orders}
        if method == "ClosedOrders":
            return {"closed": self.closed, "count": len(self.closed)}
        raise AssertionError(method)


class KrakenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()
        self.polls = patch.multiple(ks, ORDER_POLLS=3, ORDER_POLL_WAIT_SEC=0)
        self.polls.start()
        self.svc = FakeService()
        ks._write(ks.credentials_path(1), {"api_key": "key", "api_secret": SECRET})

    def tearDown(self):
        self.polls.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def synced(self, enabled=True):
        await self.svc.sync_from_account(1)
        if enabled:
            self.svc.set_enabled(1, True)

    def ledger(self):
        return ks._read_ledger(1)

    def adds(self):
        return [p for m, p in self.svc.sent if m == "AddOrder"]

    # ── signing and requests ──

    def test_signature_matches_the_vector_published_in_krakens_docs(self):
        secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
        postdata = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
        self.assertEqual(
            ks.sign(secret, "/0/private/AddOrder", 1616492376594, postdata),
            "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==",
        )

    def test_signature_is_the_formula_computed_independently(self):
        urlpath, postdata, headers = ks.signed_request(
            "the-key", SECRET, "Balance", {"asset": "ZUSD"}, 1700000000123
        )
        self.assertEqual(urlpath, "/0/private/Balance")
        self.assertEqual(postdata, "nonce=1700000000123&asset=ZUSD")
        digest = hashlib.sha256(("1700000000123" + postdata).encode()).digest()
        message = urlpath.encode() + digest
        expected = base64.b64encode(
            hmac.new(base64.b64decode(SECRET), message, hashlib.sha512).digest()
        ).decode()
        self.assertEqual(headers, {"API-Key": "the-key", "API-Sign": expected})

    def test_body_is_urlencoded_with_the_nonce_and_escapes_values(self):
        _, postdata, _ = ks.signed_request(
            "k", SECRET, "AddOrder", {"pair": "AAPLxUSD", "note": "a b&c"}, 7
        )
        self.assertEqual(
            dict(parse_qsl(postdata)),
            {"nonce": "7", "pair": "AAPLxUSD", "note": "a b&c"},
        )
        self.assertNotIn(" ", postdata)

    def test_only_a_plain_method_name_can_be_signed(self):
        for bad in ("", "Add/Order", "../x", "A" * 40, "Add Order"):
            with self.assertRaises(ValueError):
                ks.signed_request("k", SECRET, bad, {}, 1)

    def test_nonces_only_increase(self):
        values = [self.svc._nonce() for _ in range(5)]
        self.assertEqual(values, sorted(set(values)))

    def test_credentials_need_a_base64_secret_and_pause_trading(self):
        with self.assertRaises(ValueError):
            self.svc.save_credentials(1, "key", "not base64!")
        with self.assertRaises(ValueError):
            self.svc.save_credentials(1, "  ", SECRET)
        self.svc.save_credentials(1, " key ", f" {SECRET} ")
        data = json.loads(ks.credentials_path(1).read_text())
        self.assertEqual(data, {"api_key": "key", "api_secret": SECRET})
        if os.name != "nt":
            mode = stat.S_IMODE(os.stat(ks.credentials_path(1)).st_mode)
            self.assertEqual(mode, 0o600)
        status = self.svc.status(1)
        self.assertNotIn(SECRET, repr(status))
        self.assertFalse(status["enabled"])

    # ── error classification ──

    def test_errors_that_precede_an_order_are_definite(self):
        for code in (
            "EAPI:Invalid key",
            "EAPI:Invalid signature",
            "EAPI:Invalid nonce",
            "EGeneral:Permission denied",
            "EGeneral:Invalid arguments",
            "EOrder:Insufficient funds",
            "EOrder:Order minimum not met",
            "EQuery:Unknown asset pair",
            "EAPI:Rate limit exceeded",
        ):
            self.assertTrue(ks.KrakenError(200, [code]).definite, code)

    def test_errors_that_may_have_created_an_order_are_uncertain(self):
        for status, codes in (
            (200, ["EService:Unavailable"]),
            (200, ["EService:Busy"]),
            (200, ["EGeneral:Internal error"]),
            (200, ["EGeneral:Temporary lockout"]),
            (200, ["EWhatever:new code"]),
            (200, ["EOrder:Insufficient funds", "EService:Busy"]),
            (500, ["EOrder:Insufficient funds"]),
            (502, []),
            (200, []),
        ):
            self.assertFalse(ks.KrakenError(status, codes).definite, (status, codes))

    def test_error_text_is_bounded_plain_and_never_echoes_secrets(self):
        error = ks.KrakenError(200, ["EAPI:Invalid key <script>" + "x" * 500])
        self.assertLess(len(str(error)), 200)
        self.assertNotIn("<", str(error))
        leaked = ks.KrakenError(200, ["EAPI:Invalid " + SECRET], (SECRET,))
        self.assertNotIn(SECRET, str(leaked))

    def test_result_parsing_follows_the_response_envelope(self):
        result = ks.KrakenXStocksService._result
        self.assertEqual(result(200, {"error": [], "result": {"a": 1}}), {"a": 1})
        # warnings are not errors
        self.assertEqual(
            result(200, {"error": ["WOrder:Heads up"], "result": {"a": 1}}), {"a": 1}
        )
        with self.assertRaises(ks.KrakenError) as caught:
            result(200, {"error": ["EOrder:Insufficient funds"], "result": {}})
        self.assertTrue(caught.exception.definite)
        with self.assertRaises(ks.KrakenError) as caught:
            result(503, {"error": []})
        self.assertFalse(caught.exception.definite)
        with self.assertRaises(ks.KrakenError):
            result(200, ["not", "a", "dict"])

    # ── sync ──

    async def test_sync_seeds_cash_and_positions_from_the_account(self):
        self.svc.balances["ZUSD"] = {"balance": "40", "hold_trade": "5"}
        result = await self.svc.sync_from_account(1)
        ledger = self.ledger()
        self.assertEqual(ledger["quote"], "USD")
        self.assertEqual(ledger["cash"], "35")  # available, not held
        self.assertEqual(ledger["positions"]["AAPL"]["units"], "0.1")
        self.assertEqual(
            Decimal(ledger["positions"]["AAPL"]["cost"]), Decimal("34.533")
        )
        self.assertTrue(ledger["synced"])
        self.assertFalse(ledger["enabled"])
        self.assertEqual(result["skipped"], ["XXBT"])

    async def test_sync_falls_back_to_plain_balance_when_balanceex_is_refused(self):
        self.svc.balanceex_refused = True
        await self.svc.sync_from_account(1)
        self.assertEqual(self.ledger()["cash"], "40")
        self.assertIn("Balance", [m for m, _ in self.svc.sent])

    async def test_cannot_enable_before_the_first_sync(self):
        with self.assertRaises(ValueError):
            self.svc.set_enabled(1, True)

    async def test_cannot_sync_while_trading_is_on(self):
        await self.synced()
        with self.assertRaises(ValueError):
            await self.svc.sync_from_account(1)

    # ── sizing ──

    async def test_buy_is_an_ioc_limit_through_the_ask_rounded_to_tick_and_lot(self):
        await self.synced()
        self.svc.record = info("buy", "0.01986738", "0.01986738", "7.9", "0.0316")
        await self.svc.order(1, "buy", "TSLA", "8")
        (params,) = self.adds()
        self.assertEqual(params["pair"], "TSLAxUSD")
        self.assertEqual(params["type"], "buy")
        self.assertEqual(params["ordertype"], "limit")
        self.assertEqual(params["timeinforce"], "IOC")
        self.assertEqual(params["oflags"], "fciq")
        self.assertEqual(params["asset_class"], "tokenized_asset")
        self.assertRegex(params["cl_ord_id"], r"^[0-9a-f-]{36}$")
        price, volume = Decimal(params["price"]), Decimal(params["volume"])
        self.assertEqual(price, Decimal("402.67"))  # 401.46 * 1.003 rounded UP to 0.01
        self.assertEqual(price % Decimal("0.01"), 0)
        self.assertEqual(volume, Decimal("0.01986738"))  # 8 / 402.67 rounded DOWN
        self.assertLessEqual(price * volume, Decimal("8"))

    async def test_sell_rounds_down_in_price_and_to_the_lot_size(self):
        await self.synced()
        self.svc.record = info("sell", "0.1", "0.1", "34.5", "0.138")
        await self.svc.order(1, "sell", "AAPL", "0.1")
        (params,) = self.adds()
        self.assertEqual(params["type"], "sell")
        self.assertEqual(params["volume"], "0.1")
        self.assertEqual(params["price"], "344.29")  # 345.33 * 0.997 rounded DOWN
        # a quantity finer than the lot is cut, never rounded up
        self.svc.balances["AAPLx"]["balance"] = "0.123456789"
        ledger = self.ledger()
        ledger["positions"] = {"AAPL": {"units": "0.123456789", "cost": "40"}}
        ledger["enabled"] = True
        ks._write(ks._root(1) / "ledger.json", ledger)
        self.svc.sent.clear()
        self.svc.record = info("sell", "0.12345678", "0.12345678", "42", "0.1")
        await self.svc.order(1, "sell", "AAPL", "0.123456789")
        self.assertEqual(self.adds()[0]["volume"], "0.12345678")

    async def test_orders_below_krakens_minimum_volume_or_cost_are_refused(self):
        await self.synced()
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "AAPL", "0.4")  # 0.4 USD < costmin 0.5
        self.assertIn("minimum cost", str(caught.exception))
        rows = await self.svc.paper.pair_rows()
        rows["AAPLxUSD"] = {**rows["AAPLxUSD"], "ordermin": "0.5"}
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "AAPL", "8")  # 0.0230 units < ordermin
        self.assertIn("minimum volume", str(caught.exception))
        self.assertFalse(self.adds())

    async def test_amounts_must_be_plain_positive_decimals(self):
        await self.synced()
        for bad in ("", "-5", "1e3", "0", "1,5", "abc"):
            with self.assertRaises(ValueError):
                await self.svc.order(1, "buy", "AAPL", bad)
        self.assertFalse(self.adds())

    # ── fills are read back, never assumed ──

    async def test_a_full_buy_fill_adds_the_fee_to_cost_and_takes_cash(self):
        await self.synced()
        self.svc.record = info("buy", "0.0231", "0.0231", "7.9", "0.0316")
        result = await self.svc.order(1, "buy", "TSLA", "8")
        ledger = self.ledger()
        self.assertEqual(ledger["positions"]["TSLA"]["units"], "0.0231")
        self.assertEqual(ledger["positions"]["TSLA"]["cost"], "7.9316")
        self.assertEqual(Decimal(ledger["cash"]), Decimal("40") - Decimal("7.9316"))
        self.assertIsNone(ledger["uncertain_order"])
        self.assertTrue(ledger["enabled"])
        self.assertEqual(result["order"]["status"], "closed")
        self.assertEqual(ledger["orders"][-1]["fee_currency"], "USD")
        # the fill came from a QueryOrders read, by transaction id
        method, params = [s for s in self.svc.sent if s[0] == "QueryOrders"][0]
        self.assertEqual(params["txid"], "OABC-123")

    async def test_a_partial_fill_applies_only_what_executed(self):
        await self.synced()
        self.svc.record = info(
            "buy", "0.0231", "0.01", "3.4", "0.0136", status="canceled"
        )
        await self.svc.order(1, "buy", "TSLA", "8")
        ledger = self.ledger()
        self.assertEqual(ledger["positions"]["TSLA"]["units"], "0.01")
        self.assertEqual(ledger["cash"], "36.5864")
        self.assertEqual(ledger["orders"][-1]["status"], "canceled")

    async def test_an_ioc_that_filled_nothing_leaves_the_ledger_alone(self):
        await self.synced()
        self.svc.record = info("buy", "0.0231", "0", "0", "0", status="canceled")
        await self.svc.order(1, "buy", "TSLA", "8")
        ledger = self.ledger()
        self.assertEqual(ledger["cash"], "40")
        self.assertNotIn("TSLA", ledger["positions"])
        self.assertEqual(ledger["orders"][-1]["quantity"], "0")
        self.assertIsNone(ledger["uncertain_order"])
        self.assertTrue(ledger["enabled"])

    async def test_a_sale_nets_the_fee_off_the_proceeds_and_closes_the_position(self):
        await self.synced()
        self.svc.record = info("sell", "0.1", "0.1", "34.5", "0.138")
        await self.svc.order(1, "sell", "AAPL", "0.1")
        ledger = self.ledger()
        self.assertNotIn("AAPL", ledger["positions"])
        self.assertEqual(Decimal(ledger["cash"]), Decimal("40") + Decimal("34.362"))

    async def test_a_partial_sale_keeps_the_remaining_units_and_cost(self):
        await self.synced()
        self.svc.record = info("sell", "0.04", "0.04", "13.8", "0.05")
        await self.svc.order(1, "sell", "AAPL", "0.04")
        position = self.ledger()["positions"]["AAPL"]
        self.assertEqual(position["units"], "0.06")
        self.assertEqual(Decimal(position["cost"]), Decimal("34.533") * Decimal("0.6"))

    async def test_a_fee_taken_in_the_stock_reduces_the_units_not_the_cash(self):
        await self.synced()
        self.svc.record = info(
            "buy", "0.0231", "0.0231", "9.2", "0.0368", oflags="fcib"
        )
        await self.svc.order(1, "buy", "TSLA", "8")
        ledger = self.ledger()
        units = Decimal(ledger["positions"]["TSLA"]["units"])
        self.assertLess(units, Decimal("0.0231"))
        self.assertEqual(ledger["cash"], "30.8")  # 40 - cost 9.2, fee not added
        self.assertEqual(ledger["orders"][-1]["fee_currency"], "TSLAx")

    # ── guards ──

    async def test_one_stock_cannot_exceed_forty_percent_of_equity(self):
        await self.synced()  # equity 40 + 34.533 = 74.533, cap 29.81
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "TSLA", "30")
        self.assertIn("40%", str(caught.exception))
        self.assertFalse(self.adds())

    async def test_a_buy_must_leave_room_for_the_fee(self):
        await self.synced(enabled=False)
        ledger = self.ledger()
        ledger.update(cash="10", positions={})
        ks._write(ks._root(1) / "ledger.json", ledger)
        self.svc.set_enabled(1, True)
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "TSLA", "10")  # 100% of cash
        self.assertIn("fee", str(caught.exception))

    async def test_the_equity_floor_pauses_trading(self):
        self.svc.balances = {"ZUSD": {"balance": "1", "hold_trade": "0"}}
        await self.synced()
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "TSLA", "0.9")
        self.assertIn("stop threshold", str(caught.exception))
        self.assertFalse(self.ledger()["enabled"])
        self.assertFalse(self.adds())

    async def test_from_1000_the_position_count_and_reserve_rules_apply(self):
        self.svc.balances = {"ZUSD": {"balance": "1000", "hold_trade": "0"}}
        await self.synced()
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "TSLA", "200")  # > one sixth of 1000
        self.assertIn("one sixth", str(caught.exception))
        self.svc.record = info("buy", "0.4", "0.4", "160", "0")
        await self.svc.order(1, "buy", "TSLA", "160")
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "TSLA", "10")
        self.assertIn("Close the existing position", str(caught.exception))

    async def test_orders_are_refused_outside_the_regular_session(self):
        await self.synced()
        for stamp in (CLOSED_TS, SATURDAY_TS):
            self.svc.clock = lambda stamp=stamp: stamp
            for side, amount in (("buy", "8"), ("sell", "0.1")):
                with self.assertRaises(ValueError) as caught:
                    await self.svc.order(1, side, "AAPL", amount)
                self.assertIn("outside the regular US session", str(caught.exception))
        self.assertFalse(self.adds())
        self.assertIsNone(self.ledger()["uncertain_order"])
        self.assertTrue(self.ledger()["enabled"])

    async def test_only_xstocks_pairs_in_usd_can_be_traded(self):
        await self.synced()
        for symbol in ("BTC", "XBT", "ETH", "TSLASPV", "AAPLXEUR", "OLD", "AAPL/USD"):
            with self.assertRaises(ValueError):
                await self.svc.order(1, "buy", symbol, "8")
        self.assertFalse(self.adds())
        with self.assertRaises(ValueError):
            await self.svc.preview(1, "buy", "BTC", "8")

    async def test_a_wide_spread_is_refused(self):
        await self.synced()
        self.svc.paper.tickers["TSLAxUSD"] = ticker("390", "410", "400")
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "TSLA", "8")
        self.assertIn("spread", str(caught.exception))

    async def test_paused_trading_places_nothing(self):
        await self.synced(enabled=False)
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "TSLA", "8")
        self.assertFalse(self.adds())

    async def test_cannot_sell_what_the_bot_does_not_own(self):
        await self.synced()
        with self.assertRaises(ValueError):
            await self.svc.order(1, "sell", "TSLA", "0.1")
        with self.assertRaises(ValueError):
            await self.svc.order(1, "sell", "AAPL", "0.5")  # more than the position
        self.assertFalse(self.adds())

    async def test_the_account_must_actually_hold_what_the_ledger_says(self):
        await self.synced()
        self.svc.balances["AAPLx"] = {"balance": "0.1", "hold_trade": "0.1"}
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "sell", "AAPL", "0.1")
        self.assertIn("Insufficient available Kraken balance", str(caught.exception))
        self.svc.balances["ZUSD"] = {"balance": "5", "hold_trade": "0"}
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "TSLA", "8")
        self.assertFalse(self.adds())

    # ── uncertain outcomes ──

    async def test_a_definite_rejection_is_not_an_uncertain_order(self):
        await self.synced()
        self.svc.reject_add = (200, ["EOrder:Insufficient funds"])
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "sell", "AAPL", "0.1")
        self.assertIn("rejected", str(caught.exception))
        self.assertIn("Insufficient funds", str(caught.exception))
        ledger = self.ledger()
        self.assertIsNone(ledger["uncertain_order"])
        self.assertTrue(ledger["enabled"])
        self.assertEqual(ledger["positions"]["AAPL"]["units"], "0.1")
        self.assertEqual(ledger["cash"], "40")

    async def test_an_invalid_key_is_definite_too(self):
        await self.synced()
        self.svc.reject_add = (200, ["EAPI:Invalid key"])
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "TSLA", "8")
        self.assertIn("rejected", str(caught.exception))
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_a_server_side_failure_stays_uncertain(self):
        await self.synced()
        for args in (
            (200, ["EService:Unavailable"]),
            (502, []),
            (200, ["EGeneral:Internal error"]),
        ):
            self.svc.reject_add = args
            with self.assertRaises(ValueError) as caught:
                await self.svc.order(1, "buy", "TSLA", "8")
            self.assertIn("reconciliation", str(caught.exception))
            self.assertIsNotNone(self.ledger()["uncertain_order"])
            ledger = self.ledger()
            ledger["uncertain_order"] = None
            ks._write(ks._root(1) / "ledger.json", ledger)

    async def test_a_failed_send_pauses_trading_until_reconciled(self):
        await self.synced()
        self.svc.fail_add = True
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "TSLA", "8")
        self.assertIn("reconciliation", str(caught.exception))
        pending = self.ledger()["uncertain_order"]
        self.assertEqual(pending["symbol"], "TSLA")
        self.assertEqual(pending["client_oid"], self.svc.cid())
        self.assertFalse(self.svc.status(1)["enabled"])
        with self.assertRaises(ValueError):
            self.svc.set_enabled(1, True)
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "TSLA", "8")

    async def test_an_accepted_order_without_a_txid_is_uncertain(self):
        await self.synced()
        self.svc.no_txid = True
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "TSLA", "8")
        self.assertIn("reconciliation", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_an_order_that_never_reaches_a_final_state_stays_uncertain(self):
        await self.synced()
        self.svc.record = info("buy", "0.0231", "0", "0", "0", status="open")
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "TSLA", "8")
        pending = self.ledger()["uncertain_order"]
        self.assertEqual(pending["txid"], "OABC-123")
        self.assertFalse(self.svc.status(1)["enabled"])

    async def test_a_refused_read_back_after_an_accepted_order_is_never_a_rejection(
        self,
    ):
        """AddOrder succeeded, so an error from the read-back must not clear the entry."""
        await self.synced()
        self.svc.query_error = (200, ["EQuery:Unknown order"])
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "TSLA", "8")
        self.assertIn("reconciliation", str(caught.exception))
        self.assertNotIn("rejected", str(caught.exception))
        pending = self.ledger()["uncertain_order"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["txid"], "OABC-123")

    # ── reconcile ──

    async def uncertain(self, **fail):
        await self.synced()
        self.svc.fail_add = True
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "TSLA", "8")
        self.svc.fail_add = False

    def age(self, seconds):
        ledger = self.ledger()
        ledger["uncertain_order"]["time"] -= seconds
        ks._write(ks._root(1) / "ledger.json", ledger)

    async def test_reconcile_applies_an_order_found_by_client_id_that_did_fill(self):
        await self.uncertain()
        cid = self.svc.cid()
        self.svc.closed = {
            "OZ-1": info(
                "buy",
                "0.0231",
                "0.0231",
                "9.2",
                "0.0368",
                cid=cid,
                descr={"pair": "TSLAxUSD", "type": "buy", "price": "402"},
            )
        }
        status = await self.svc.reconcile(1)
        ledger = status["ledger"]
        self.assertIsNone(ledger["uncertain_order"])
        self.assertEqual(ledger["positions"]["TSLA"]["units"], "0.0231")
        self.assertEqual(ledger["orders"][-1]["order_id"], "OZ-1")
        self.assertFalse(ledger["enabled"])  # trading stays paused

    async def test_reconcile_reads_the_order_by_txid_when_it_is_known(self):
        await self.synced()
        self.svc.record = info(
            "buy", "0.0231", "0.0231", "9.2", "0.0368", status="open"
        )
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "TSLA", "8")
        self.svc.record = info("buy", "0.0231", "0.0231", "9.2", "0.0368")
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertEqual(status["ledger"]["positions"]["TSLA"]["units"], "0.0231")

    async def test_reconcile_finds_a_still_open_order_but_does_not_settle_it(self):
        await self.uncertain()
        self.svc.open_orders = {
            "OZ-2": info(
                "buy",
                "0.02",
                "0",
                "0",
                "0",
                status="open",
                cid=self.svc.cid(),
                descr={"pair": "TSLAxUSD", "type": "buy", "price": "402"},
            )
        }
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("still open", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_reconcile_clears_an_order_kraken_never_saw_after_a_minute(self):
        await self.uncertain()
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("Wait a minute", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        self.age(600)
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertEqual(status["ledger"]["cash"], "40")
        self.assertFalse(status["ledger"]["enabled"])
        self.assertIn(
            "ClosedOrders", [m for m, _ in self.svc.sent]
        )  # history was checked

    async def test_reconcile_will_not_clear_when_a_lookalike_has_no_client_id(self):
        """If Kraken omits cl_ord_id from records, never conclude the order did not exist."""
        await self.uncertain()
        self.age(600)
        self.svc.closed = {
            "OZ-3": info(
                "buy",
                "0.02",
                "0.02",
                "8",
                "0.03",
                cid=None,
                descr={"pair": "TSLAxUSD", "type": "buy", "price": "402"},
                opentm=self.ledger()["uncertain_order"]["time"] + 1,
            )
        }
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("by hand", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_reconcile_with_nothing_pending_changes_nothing(self):
        await self.synced()
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])

    # ── preview and fees ──

    async def test_preview_prices_the_book_and_reports_the_accounts_fee(self):
        await self.synced()
        preview = await self.svc.preview(1, "buy", "TSLA", "8")
        self.assertTrue(preview["book_covers_order"])
        self.assertEqual(preview["estimated_average_price"], "401.46")
        self.assertEqual(preview["taker_fee_pct"], "0.4")
        spread = Decimal(preview["spread_pct"])
        self.assertAlmostEqual(
            float(preview["estimated_round_trip_pct"]), float(spread) + 0.8, places=2
        )
        self.assertIsNone(preview["fee_note"])
        self.assertTrue(preview["market_open"])
        self.assertEqual(preview["pair"], "TSLAxUSD")
        self.assertFalse(self.adds())

    async def test_preview_with_a_zero_fee_plan_is_just_the_spread(self):
        await self.synced()
        self.svc.fees = {"fee": "0.0000"}
        preview = await self.svc.preview(1, "buy", "TSLA", "8")
        self.assertEqual(preview["taker_fee_pct"], "0")
        self.assertEqual(
            Decimal(preview["estimated_round_trip_pct"]), Decimal(preview["spread_pct"])
        )

    async def test_preview_without_fee_info_says_unknown_and_never_invents_zero(self):
        await self.synced()
        self.svc.fees = None
        preview = await self.svc.preview(1, "buy", "TSLA", "8")
        self.assertIsNone(preview["taker_fee_pct"])
        self.assertIsNone(preview["estimated_round_trip_pct"])
        self.assertIn("fee unknown", preview["fee_note"])
        self.assertTrue(preview["book_covers_order"])

    async def test_fee_rates_are_cached_for_a_while(self):
        await self.synced()
        await self.svc.preview(1, "buy", "TSLA", "8")
        await self.svc.preview(1, "buy", "TSLA", "9")
        self.assertEqual([m for m, _ in self.svc.sent].count("TradeVolume"), 1)
        sent = [p for m, p in self.svc.sent if m == "TradeVolume"][0]
        self.assertEqual(sent, {"pair": "TSLAxUSD", "fee-info": "true"})

    async def test_preview_is_allowed_while_the_market_is_closed_but_says_so(self):
        await self.synced()
        self.svc.clock = lambda: CLOSED_TS
        preview = await self.svc.preview(1, "buy", "TSLA", "8")
        self.assertFalse(preview["market_open"])

    # ── market data ──

    async def test_coins_are_ranked_by_dollar_volume_and_keep_the_pair_facts(self):
        await self.synced(enabled=False)
        data = await self.svc.coins(1, "")
        self.assertEqual([c["symbol"] for c in data["stocks"]][:2], ["TSLA", "SPY"])
        self.assertEqual(data["stocks"][0]["pair"], "TSLAxUSD")
        self.assertEqual(data["stocks"][0]["cost_min_usd"], "0.5")
        self.assertEqual(data["stocks"][0]["min_quantity_step"], "0.00000001")
        found = await self.svc.coins(1, "aa")
        self.assertEqual([c["symbol"] for c in found["stocks"]], ["AAPL"])
        # listed prices feed the live scorecard, apart from the paper test's
        self.assertIsNotNone(sc.latest_price(1, "TSLA", venue="kraken_xstocks"))
        self.assertIsNone(sc.latest_price(1, "TSLA", venue="xstocks_paper"))

    async def test_book_and_candles_come_from_krakens_public_data(self):
        book = await self.svc.book(1, "aapl", 5)
        self.assertEqual(book["pair"], "AAPLxUSD")
        self.assertEqual(book["asks"][0][0], "345.54")
        candles = await self.svc.candles(1, "AAPL", "15m", 3)
        self.assertEqual(len(candles["candles"]), 3)
        with self.assertRaises(ValueError):
            await self.svc.book(1, "BTC")

    # ── account views ──

    async def test_valuation_marks_positions_at_the_bid(self):
        await self.synced(enabled=False)
        value = await self.svc.valuation(1)
        self.assertEqual(Decimal(value["equity"]), Decimal("40") + Decimal("34.533"))
        self.assertEqual(
            Decimal(value["new_position_limit"]), Decimal("74.533") * Decimal("0.4")
        )
        self.assertEqual(value["minimum_cash_reserve"], "0")

    async def test_portfolio_values_the_whole_account_in_usd(self):
        data = await self.svc.portfolio(1)
        by = {b["token"]: b for b in data["balances"]}
        self.assertEqual(by["USD"]["usd_value"], 40)
        self.assertAlmostEqual(by["AAPLx"]["usd_value"], 0.1 * 345.41, places=4)
        self.assertEqual(by["XXBT"]["usd_value"], 0)
        self.assertIn("XXBT", data["note"])
        self.assertEqual(data["connector"], "Kraken xStocks")
        self.assertAlmostEqual(data["total_usd"], 40 + 0.1 * 345.41, places=4)

    async def test_history_lists_closed_orders_newest_first(self):
        self.svc.closed = {
            "O1": info("buy", "1", "1", "10", "0.04", closetm=100),
            "O2": info("sell", "1", "1", "11", "0.04", closetm=200),
        }
        rows = await self.svc.history(1)
        self.assertEqual([r["order_id"] for r in rows], ["O2", "O1"])
        self.assertEqual(rows[0]["side"], "sell")

    # ── scorecard ──

    async def test_the_live_scorecard_is_kept_apart_and_scored_at_the_accounts_fee(
        self,
    ):
        result = await self.svc.predict(1, "tsla", "trade", "2", "1", "3", "breakout")
        self.assertTrue(result["recorded"])
        report = await self.svc.scorecard(1)
        self.assertEqual(report["trade_calls"]["total"], 1)
        self.assertAlmostEqual(report["assumptions"]["round_trip_cost_pct"], 0.85)
        self.svc.fees = {"fee": "0.2500"}
        self.svc._fees.clear()
        report = await self.svc.scorecard(1)
        self.assertAlmostEqual(report["assumptions"]["round_trip_cost_pct"], 0.55)
        self.svc.fees = None
        self.svc._fees.clear()
        report = await self.svc.scorecard(1)
        self.assertEqual(
            report["assumptions"]["round_trip_cost_pct"], ks.DEFAULT_ROUND_TRIP_COST_PCT
        )
        self.assertEqual(sc.report(1)["trade_calls"]["total"], 0)  # not the Exchange's
        self.assertEqual(
            sc.report(1, venue="xstocks_paper")["trade_calls"]["total"], 0
        )  # not the paper test's


if __name__ == "__main__":
    unittest.main()
