"""Trading 212 service: auth, sizing, guards, fills, costs, errors and reconciliation. No network."""

import base64
import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from condor import crypto_com_scorecard as sc
from condor import native_flow_fetchers as fetchers
from condor import trading212 as t212
from condor.xstocks_market import XStocksMarket

API_KEY = "key-id-1234567890"
API_SECRET = "secret-abcdefghijklmnop"
NOW = 1_793_113_200.0  # Tue 2026-10-27 11:00 New York (EDT): inside the US session
CLOSED = 1_793_070_000.0  # Mon 2026-10-26 23:00 New York
FX = Decimal("1.10")  # USD per EUR, in the fake Kraken feed and in the fake account
OID = 555


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def instrument(ticker, short, isin="US0378331005", currency="USD", kind="STOCK", **x):
    return {
        "ticker": ticker,
        "type": kind,
        "isin": isin,
        "currencyCode": currency,
        "name": short + " Inc",
        "shortName": short,
        "workingScheduleId": 7,
        "maxOpenQuantity": 1000000,
        "extendedHours": True,
        "addedOn": "2020-01-01T00:00:00.000Z",
        **x,
    }


INSTRUMENTS = [
    instrument("AAPL_US_EQ", "AAPL"),
    instrument("MSFT_US_EQ", "MSFT", isin="US5949181045", minTradeQuantity=0.01),
    instrument("TSLA_US_EQ", "TSLA", isin="US88160R1014", minTradeQuantity=0.5),
    instrument("SAP_DE_EQ", "SAP", isin="DE0007164600", currency="EUR"),
    instrument("SPY_US_EQ", "SPY", kind="ETF"),
    instrument("NVDA_US_EQ", "NVDA", isin="US67066G1040"),  # not in the Kraken feed
    instrument("DUP_CA_EQ", "DUP"),
    instrument("DUP_US_EQ", "DUP"),
    instrument("AMB_AB_EQ", "AMB"),
    instrument("AMB_CD_EQ", "AMB"),
    instrument("BRK_B_US_EQ", "BRK.B"),
]


def kraken(bid, ask, last, volume):
    return {
        "b": [bid, "1", "1.0"],
        "a": [ask, "1", "1.0"],
        "c": [last, "1"],
        "v": ["0", volume],
        "h": ["0", str(Decimal(ask) * Decimal("1.01"))],
        "l": ["0", str(Decimal(bid) * Decimal("0.99"))],
        "o": str(Decimal(last) * Decimal("0.99")),
    }


TICKERS = {
    "AAPLxUSD": kraken("200.00", "200.10", "200.05", "50000"),
    "MSFTxUSD": kraken("400.00", "400.20", "400.10", "30000"),
    "TSLAxUSD": kraken("250.00", "250.10", "250.05", "40000"),
    "AMZNxUSD": kraken("180.00", "180.10", "180.05", "99999"),
    "DUPxUSD": kraken("20.00", "20.02", "20.01", "10"),
    "SPYxUSD": kraken("500.00", "500.05", "500.02", "70000"),
}


class FakeFeed(XStocksMarket):
    """Kraken's public xStocks data, scripted."""

    def __init__(self):
        super().__init__()
        self.tickers = dict(TICKERS)
        self.requests = []

    async def pairs(self):
        return {name[:-4]: name for name in self.tickers}

    async def _get(self, path, params, *, asset_class=True):
        self.requests.append((path, params))
        assert path == "Ticker"
        return {
            p: self.tickers[p] for p in params["pair"].split(",") if p in self.tickers
        }

    async def candles(self, symbol, timeframe="15m", count=96):
        return {"pair": symbol + "xUSD", "timeframe": timeframe, "candles": []}


def position(ticker, quantity, price, cost, available=None, **x):
    quantity = Decimal(quantity)
    return {
        "ticker": ticker,
        "quantity": quantity,
        "available": quantity if available is None else Decimal(available),
        "price_usd": Decimal(price),
        "cost_eur": Decimal(cost),
        **x,
    }


def history_item(oid, ticker, side, quantity, status, filled, price, created, **x):
    """One historical order item as the docs describe it (an order and its fill)."""
    signed = quantity if side == "buy" else -quantity
    item = {
        "order": {
            "id": oid,
            "ticker": ticker,
            "side": side.upper(),
            "status": status,
            "type": "LIMIT",
            "quantity": float(signed),
            "filledQuantity": float(filled),
            "limitPrice": 200.71,
            "initiatedFrom": x.pop("origin", "API"),
            "createdAt": iso(created),
        }
    }
    if filled:
        impact = {"currency": "EUR", "fxRate": 1.1, "realisedProfitLoss": 0}
        if "net" in x:
            impact["netValue"] = x["net"]
        if "fee" in x:
            impact["taxes"] = [
                {
                    "name": "CURRENCY_CONVERSION_FEE",
                    "quantity": x["fee"],
                    "currency": "EUR",
                    "chargedAt": iso(created),
                }
            ]
        item["fill"] = {
            "id": oid * 10,
            "price": float(price),
            "quantity": float(filled),
            "filledAt": iso(created + 1),
            "type": "TRADE",
            "walletImpact": impact,
        }
    return item


class FakeService(t212.Trading212Service):
    """Trading 212 with the private reads and the order endpoints scripted. No network."""

    def __init__(self):
        super().__init__()
        self.clock = lambda: NOW
        self.gaps = {name: 0 for name in t212.GAPS}
        self._ticks = 0

        def mono():
            # Every read is a fresh one: time moves on 10 s between calls, past the
            # account caches, while the long instrument cache still holds.
            self._ticks += 10
            return float(self._ticks)

        self.mono = mono

        async def no_sleep(_):
            return None

        self.sleep = no_sleep
        self.market_feed = FakeFeed()
        self.sent = []
        self.currency = "EUR"
        self.cash = Decimal("40")
        self.reserved = Decimal("0")
        self.held = {
            "MSFT_US_EQ": position("MSFT_US_EQ", "0.05", "400", "17.5"),
            "SAP_DE_EQ": position("SAP_DE_EQ", "1", "100", "90", currency="EUR"),
            "AAPL_US_EQ_PIE": None,
        }
        del self.held["AAPL_US_EQ_PIE"]
        self.instruments_raw = INSTRUMENTS
        self.exchanges_raw = [
            {
                "id": 1,
                "name": "NYSE",
                "workingSchedules": [
                    {
                        "id": 7,
                        "timeEvents": [
                            {"date": iso(NOW - 3600), "type": "OPEN"},
                            {"date": iso(NOW + 3 * 3600), "type": "CLOSE"},
                        ],
                    }
                ],
            }
        ]
        self.exchanges_error = None
        self.place_error = None  # a Trading212Error
        self.place_fail = False  # a transport failure
        self.no_id = False
        self.fill = {"quantity": None, "price": "200.10"}  # None: the whole order
        self.pending_polls = 0
        self.stuck = False
        self.cancelled = False
        self.delete_error = None
        self.lists_unfilled = False
        self.records = (
            []
        )  # (order id, ticker, side, qty, filled, price, created, extra)
        self.pending_list = []
        self.history_extra = {}
        self.summary_error = None
        self.positions_after_cancel = None

    async def eur_usd(self):
        return FX

    def posts(self):
        return [body for method, path, query, body in self.sent if method == "POST"]

    def deletes(self):
        return [path for method, path, query, body in self.sent if method == "DELETE"]

    def _record(self):
        out = []
        for oid, ticker, side, qty, filled, price, created, extra in self.records:
            if filled == 0 and not self.lists_unfilled:
                continue
            if filled == qty:
                status = "FILLED"
            else:
                status = "CANCELLED" if self.cancelled or not self.stuck else "NEW"
            out.append(
                history_item(
                    oid, ticker, side, qty, status, filled, price, created, **extra
                )
            )
        return out

    async def private(self, user_id, method, path, query=None, body=None):
        self.credentials(user_id)
        path, params = self._split(path, query)
        t212.request_class(method, path)
        self.sent.append((method, path, params or query, body))
        if path == "/equity/account/summary":
            if self.summary_error:
                raise self.summary_error
            return {
                "id": 1,
                "currency": self.currency,
                "totalValue": 0,
                "cash": {
                    "availableToTrade": float(self.cash),
                    "inPies": 0,
                    "reservedForOrders": float(self.reserved),
                },
                "investments": {},
            }
        if path == "/equity/positions":
            rows = []
            for ticker, p in self.held.items():
                value = p["quantity"] * p["price_usd"] / FX
                rows.append(
                    {
                        "createdAt": iso(NOW - 86400),
                        "currentPrice": float(p["price_usd"]),
                        "averagePricePaid": float(p["price_usd"]),
                        "quantity": float(p["quantity"]),
                        "quantityAvailableForTrading": float(p["available"]),
                        "quantityInPies": 0,
                        "instrument": {
                            "ticker": ticker,
                            "currency": p.get("currency", "USD"),
                            "name": ticker,
                            "isin": "x",
                        },
                        "walletImpact": {
                            "currency": "EUR",
                            "currentValue": float(value),
                            "totalCost": float(p["cost_eur"]),
                            "unrealizedProfitLoss": 0,
                            "fxImpact": None,
                        },
                    }
                )
            return rows
        if path == "/equity/metadata/instruments":
            return self.instruments_raw
        if path == "/equity/metadata/exchanges":
            if self.exchanges_error:
                raise self.exchanges_error
            return self.exchanges_raw
        if path == "/equity/orders" and method == "GET":
            return self.pending_list
        if path in ("/equity/orders/limit", "/equity/orders/market"):
            return self._place(path, body)
        if path == f"/equity/orders/{OID}" and method == "GET":
            if self.stuck and not self.cancelled:
                return self._pending_order()
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return self._pending_order()
            raise t212.Trading212Error(404, "not found")
        if method == "DELETE" and path.startswith("/equity/orders/"):
            if self.delete_error:
                raise self.delete_error
            self.cancelled = True
            return {}
        if path == "/equity/history/orders":
            return {
                "items": self._record() + self.history_extra.get("items", []),
                "nextPagePath": None,
            }
        raise AssertionError((method, path))

    def _pending_order(self):
        record = self.records[-1]
        return {
            "id": OID,
            "ticker": record[1],
            "side": record[2].upper(),
            "status": "NEW",
            "type": "LIMIT",
            "quantity": float(record[3]),
            "filledQuantity": 0,
            "initiatedFrom": "API",
            "createdAt": iso(NOW),
        }

    def _place(self, path, body):
        if self.place_error:
            raise self.place_error
        if self.place_fail:
            raise ValueError("Trading 212 unavailable")
        if self.no_id:
            return {}
        quantity = Decimal(str(body["quantity"]))
        side = "buy" if quantity > 0 else "sell"
        quantity = abs(quantity)
        fill = self.fill
        filled = quantity if fill["quantity"] is None else Decimal(fill["quantity"])
        price = Decimal(fill["price"])
        ticker = body["ticker"]
        if filled:
            eur = filled * price / FX
            if side == "buy":
                eur *= Decimal("1.0015")
                self.cash -= eur
                old = self.held.get(ticker)
                total = (old["quantity"] if old else 0) + filled
                cost = (old["cost_eur"] if old else 0) + eur
                self.held[ticker] = position(ticker, total, price, cost)
            else:
                eur *= Decimal("0.9985")
                self.cash += eur
                old = self.held[ticker]
                left = old["quantity"] - filled
                if left <= 0:
                    del self.held[ticker]
                else:
                    self.held[ticker] = position(ticker, left, price, old["cost_eur"])
        extra = dict(fill.get("extra", {}))
        self.records.append((OID, ticker, side, quantity, filled, price, NOW, extra))
        return {"id": OID, "ticker": ticker, "status": "NEW", "type": "LIMIT"}


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()
        self.polls = patch.multiple(
            t212, PENDING_POLLS=3, CANCEL_POLLS=2, HISTORY_POLLS=2, POLL_WAIT_SEC=0
        )
        self.polls.start()
        self.svc = FakeService()
        self.svc.save_credentials(1, API_KEY, API_SECRET)

    def tearDown(self):
        self.polls.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def synced(self, enabled=True):
        await self.svc.sync_from_account(1)
        if enabled:
            self.svc.set_enabled(1, True)

    def ledger(self):
        return t212._read_ledger(1)

    def set_ledger(self, **fields):
        ledger = self.ledger()
        ledger.update(fields)
        t212._write(t212._root(1) / "ledger.json", ledger)


class AuthAndCredentialTests(Base):
    def test_basic_auth_is_the_base64_of_key_and_secret_and_legacy_is_the_bare_key(
        self,
    ):
        header = t212.auth_header(API_KEY, API_SECRET)
        self.assertTrue(header.startswith("Basic "))
        decoded = base64.b64decode(header[6:]).decode()
        self.assertEqual(decoded, f"{API_KEY}:{API_SECRET}")
        self.assertEqual(t212.auth_header(API_KEY, ""), API_KEY)

    def test_credentials_are_stored_0600_and_never_shown(self):
        path = t212.credentials_path(1)
        data = json.loads(path.read_text())
        self.assertEqual(data, {"api_key": API_KEY, "api_secret": API_SECRET})
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        status = self.svc.status(1)
        text = json.dumps(status)
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(API_SECRET, text)
        self.assertTrue(status["connected"] and status["has_secret"])
        self.assertEqual(status["quote"], "EUR")
        self.assertIsNone(status["account_currency"])
        self.assertNotIn(API_KEY, json.dumps(self.ledger()))

    def test_a_key_without_a_secret_is_a_legacy_connection(self):
        self.svc.save_credentials(1, API_KEY, "")
        self.assertFalse(self.svc.status(1)["has_secret"])
        self.assertTrue(self.svc.status(1)["connected"])

    def test_invalid_credentials_are_refused_without_echoing_them(self):
        for key, secret in (("", "x"), ("a:b", "x"), ("k" * 5000, ""), ("ok\n", "")):
            with self.assertRaises(ValueError) as caught:
                self.svc.save_credentials(1, key, secret)
            self.assertNotIn("k" * 20, str(caught.exception))
        with self.assertRaises(ValueError):
            self.svc.save_credentials(1, API_KEY, "bad\x00secret")

    async def test_saving_new_credentials_pauses_trading_and_disconnect_removes_them(
        self,
    ):
        await self.synced()
        self.svc.save_credentials(1, API_KEY, API_SECRET)
        self.assertFalse(self.ledger()["enabled"])
        status = self.svc.disconnect(1)
        self.assertFalse(status["connected"])
        self.assertFalse(t212.credentials_path(1).exists())
        with self.assertRaises(ValueError):
            self.svc.credentials(1)

    async def test_trading_cannot_be_enabled_before_a_synced_eur_account(self):
        with self.assertRaises(ValueError) as caught:
            self.svc.set_enabled(1, True)
        self.assertIn("Sync", str(caught.exception))
        self.set_ledger(synced=True)
        with self.assertRaises(ValueError) as caught:
            self.svc.set_enabled(1, True)
        self.assertIn("EUR", str(caught.exception))


class TransportService(t212.Trading212Service):
    """The real HTTP envelope with the wire replaced: what reaches the wire, and what comes back."""

    def __init__(self, replies):
        super().__init__()
        self.clock = lambda: NOW
        self.gaps = {name: 0 for name in t212.GAPS}
        self.replies = list(replies)
        self.wire = []

    async def _transport(self, method, url, headers, body):
        self.wire.append((method, url, dict(headers), body))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def service(self, *replies):
        svc = TransportService(replies)
        svc.save_credentials(1, API_KEY, API_SECRET)
        return svc

    async def test_a_call_goes_to_the_live_host_with_basic_auth_and_a_compact_body(
        self,
    ):
        svc = self.service((200, {"id": 9}, {}))
        body = {"ticker": "AAPL_US_EQ", "quantity": -0.5, "limitPrice": 200.5}
        result = await svc.private(1, "POST", "/equity/orders/limit", None, body)
        self.assertEqual(result, {"id": 9})
        ((method, url, headers, payload),) = svc.wire
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://live.trading212.com/api/v0/equity/orders/limit")
        self.assertIn("live.trading212.com", url)
        self.assertNotIn("demo", url)
        self.assertEqual(
            payload,
            '{"ticker":"AAPL_US_EQ","quantity":-0.5,"limitPrice":200.5}',
        )
        token = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
        self.assertEqual(headers["Authorization"], "Basic " + token)

    async def test_a_legacy_key_is_sent_as_the_bare_authorization_value(self):
        svc = TransportService([(200, [], {})])
        svc.save_credentials(1, API_KEY, "")
        await svc.private(1, "GET", "/equity/positions")
        self.assertEqual(svc.wire[0][2]["Authorization"], API_KEY)

    async def test_a_next_page_path_is_followed_on_the_same_host_only(self):
        svc = self.service((200, {"items": []}, {}))
        await svc.private(1, "GET", "/api/v0/equity/history/orders?cursor=5&limit=50")
        self.assertEqual(
            svc.wire[0][1],
            "https://live.trading212.com/api/v0/equity/history/orders?cursor=5&limit=50",
        )
        for bad in ("https://evil.example/api/v0/equity/positions", "//evil/x"):
            with self.assertRaises(ValueError):
                await svc.private(1, "GET", bad)
        self.assertEqual(len(svc.wire), 1)

    async def test_only_the_endpoints_the_module_needs_can_be_reached(self):
        svc = self.service()
        for method, path in (
            ("GET", "/equity/pies"),
            ("POST", "/equity/orders/stop"),
            ("POST", "/equity/orders/stop_limit"),
            ("POST", "/equity/history/exports"),
            ("DELETE", "/equity/positions"),
            ("GET", "/equity/orders/abc"),
            ("PUT", "/equity/orders/limit"),
            ("POST", "/equity/account/withdraw"),
        ):
            with self.assertRaises(ValueError):
                await svc.private(1, method, path)
        self.assertEqual(svc.wire, [])

    async def test_a_4xx_is_definite_and_a_5xx_408_or_timeout_is_not(self):
        svc = self.service(
            (400, {"message": "Bad quantity"}, {}),
            (401, {"message": "bad key " + API_KEY}, {}),
            (403, {"code": "scope"}, {}),
            (408, {}, {}),
            (500, {}, {}),
            (503, {}, {}),
        )
        verdicts = []
        for _ in range(6):
            with self.assertRaises(t212.Trading212Error) as caught:
                await svc.private(1, "GET", "/equity/positions")
            verdicts.append(caught.exception.definite)
            self.assertNotIn(API_KEY, str(caught.exception))
        self.assertEqual(verdicts, [True, True, True, False, False, False])

    async def test_429_is_definite_honours_retry_after_and_blocks_the_class_locally(
        self,
    ):
        svc = self.service(
            (429, {"message": "slow"}, {"Retry-After": "30"}),
        )
        with self.assertRaises(t212.Trading212Error) as caught:
            await svc.private(1, "POST", "/equity/orders/market", None, {"a": 1})
        self.assertTrue(caught.exception.definite)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 30)
        with self.assertRaises(t212.Trading212Error) as again:
            await svc.private(1, "POST", "/equity/orders/market", None, {"a": 1})
        self.assertEqual(again.exception.status, 0)
        self.assertTrue(again.exception.definite)
        self.assertEqual(len(svc.wire), 1)  # nothing more was sent
        # Another endpoint class is not blocked by it.
        svc.replies.append((200, [], {}))
        await svc.private(1, "GET", "/equity/positions")

    async def test_429_falls_back_to_the_reset_timestamp_header(self):
        svc = self.service(
            (429, {}, {"x-ratelimit-reset": str(int(NOW) + 20)}),
        )
        with self.assertRaises(t212.Trading212Error) as caught:
            await svc.private(1, "GET", "/equity/positions")
        self.assertEqual(caught.exception.retry_after, 20)

    async def test_a_transport_failure_is_not_a_trading212_error(self):
        svc = self.service(ValueError("Trading 212 unavailable"))
        with self.assertRaises(ValueError) as caught:
            await svc.private(1, "GET", "/equity/positions")
        self.assertNotIsInstance(caught.exception, t212.Trading212Error)

    async def test_local_pacing_waits_a_little_and_refuses_a_long_wait(self):
        svc = self.service((200, {}, {}), (200, {}, {}), (200, [], {}), (200, [], {}))
        svc.gaps = dict(t212.GAPS)
        slept = []

        async def sleep(seconds):
            slept.append(seconds)

        svc.sleep = sleep
        svc.mono = lambda: 100.0
        await svc.private(1, "GET", "/equity/account/summary")
        await svc.private(1, "GET", "/equity/account/summary")
        self.assertEqual(slept, [5.5])
        await svc.private(1, "GET", "/equity/metadata/instruments")
        with self.assertRaises(t212.Trading212Error) as caught:
            await svc.private(1, "GET", "/equity/metadata/instruments")
        self.assertEqual(caught.exception.status, 0)
        self.assertTrue(caught.exception.definite)
        self.assertEqual(len(svc.wire), 3)


class MappingTests(unittest.TestCase):
    def test_us_stocks_are_read_from_the_metadata_not_guessed(self):
        chosen = {
            i["symbol"]: i
            for i in t212.Trading212Service.parse_instruments(INSTRUMENTS)
        }
        self.assertEqual(chosen["AAPL"]["ticker"], "AAPL_US_EQ")
        self.assertEqual(chosen["MSFT"]["min_quantity"], Decimal("0.01"))
        # Not a stock, not USD, not a plain symbol, or ambiguous: none of them map.
        for absent in ("SPY", "SAP", "BRK", "AMB"):
            self.assertNotIn(absent, chosen)
        # Two listings of one symbol: only the plain _US_EQ one is chosen.
        self.assertEqual(chosen["DUP"]["ticker"], "DUP_US_EQ")

    def test_a_short_name_that_disagrees_with_the_ticker_is_dropped(self):
        rows = [instrument("ABC_US_EQ", "XYZ")]
        self.assertEqual(t212.Trading212Service.parse_instruments(rows), [])
        self.assertEqual(t212.Trading212Service.parse_instruments("nope"), [])

    def test_a_non_us_isin_is_not_a_us_stock(self):
        rows = [instrument("ABC_US_EQ", "ABC", isin="GB0001")]
        self.assertEqual(t212.Trading212Service.parse_instruments(rows), [])


class SyncAndValuationTests(Base):
    async def test_sync_seeds_cash_and_the_mapped_positions_from_the_account(self):
        status = await self.svc.sync_from_account(1)
        ledger = status["ledger"]
        self.assertEqual(ledger["cash"], "40")
        self.assertEqual(ledger["account_currency"], "EUR")
        self.assertTrue(ledger["synced"])
        self.assertEqual(
            ledger["positions"], {"MSFT": {"units": "0.05", "cost": "17.5"}}
        )
        self.assertEqual(ledger["tickers"], {"MSFT": "MSFT_US_EQ"})
        self.assertEqual(status["skipped"], ["SAP_DE_EQ"])
        self.assertEqual(status["notes"], [])

    async def test_a_position_without_a_reported_cost_uses_its_value_and_says_so(self):
        self.svc.held["MSFT_US_EQ"]["cost_eur"] = Decimal("0")
        status = await self.svc.sync_from_account(1)
        self.assertEqual(
            status["notes"], ["MSFT: cost basis is the current value (none reported)"]
        )
        self.assertTrue(Decimal(status["ledger"]["positions"]["MSFT"]["cost"]) > 0)

    async def test_shares_in_a_pie_are_not_part_of_the_allocation(self):
        self.svc.held["MSFT_US_EQ"] = position(
            "MSFT_US_EQ", "0.05", "400", "17.5", available="0"
        )
        status = await self.svc.sync_from_account(1)
        self.assertEqual(status["ledger"]["positions"], {})
        self.assertIn("MSFT_US_EQ", status["skipped"])

    async def test_a_non_eur_account_is_refused_everywhere_and_says_why(self):
        self.svc.currency = "GBP"
        self.svc.save_credentials(1, API_KEY, API_SECRET)
        for call in (
            self.svc.sync_from_account(1),
            self.svc.accounts(1),
            self.svc.valuation(1),
            self.svc.portfolio(1),
        ):
            with self.assertRaises(ValueError) as caught:
                await call
            self.assertIn("GBP", str(caught.exception))
            self.assertIn("EUR", str(caught.exception))
        self.assertFalse(self.ledger()["synced"])

    async def test_a_currency_change_after_sync_refuses_orders(self):
        await self.synced()
        self.svc.currency = "USD"
        self.svc.cash = Decimal("40")
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "AAPL", "8")
        self.assertIn("USD", str(caught.exception))
        self.assertEqual(self.svc.posts(), [])

    async def test_sync_is_refused_while_trading_or_with_an_uncertain_order(self):
        await self.synced()
        with self.assertRaises(ValueError):
            await self.svc.sync_from_account(1)
        self.svc.set_enabled(1, False)
        self.set_ledger(uncertain_order={"time": NOW})
        with self.assertRaises(ValueError):
            await self.svc.sync_from_account(1)

    async def test_valuation_uses_trading212_own_prices_in_eur(self):
        await self.synced()
        result = await self.svc.valuation(1)
        price_eur = Decimal("400") / FX
        expected = Decimal("40") + price_eur * Decimal("0.05")
        self.assertAlmostEqual(Decimal(result["equity"]), expected, places=6)
        row = result["positions"][0]
        self.assertEqual(row["symbol"], "MSFT")
        self.assertEqual(row["ticker"], "MSFT_US_EQ")
        self.assertEqual(row["price_usd"], "400")
        self.assertEqual(result["quote"], "EUR")
        self.assertEqual(
            Decimal(result["new_position_limit"]),
            Decimal(result["equity"]) * Decimal("0.4"),
        )

    async def test_a_ledger_position_missing_from_the_account_refuses_to_value(self):
        await self.synced()
        del self.svc.held["MSFT_US_EQ"]
        with self.assertRaises(ValueError) as caught:
            await self.svc.valuation(1)
        self.assertIn("sync", str(caught.exception).lower())

    async def test_valuation_pauses_trading_at_the_equity_floor(self):
        await self.synced()
        self.svc.cash = Decimal("0.5")
        self.svc.held.clear()
        self.set_ledger(cash="0.5", positions={}, tickers={})
        result = await self.svc.valuation(1)
        self.assertFalse(result["enabled"])

    async def test_valuation_refuses_with_an_uncertain_order(self):
        await self.synced()
        self.set_ledger(uncertain_order={"time": NOW})
        with self.assertRaises(ValueError):
            await self.svc.valuation(1)

    async def test_accounts_lists_cash_and_positions_with_their_eur_value(self):
        result = await self.svc.accounts(1)
        rows = {r["currency"]: r for r in result["accounts"]}
        self.assertEqual(rows["EUR"]["balance"], "40")
        self.assertEqual(rows["MSFT"]["ticker"], "MSFT_US_EQ")
        self.assertAlmostEqual(
            Decimal(rows["MSFT"]["value_eur"]), Decimal("0.05") * 400 / FX, places=6
        )

    async def test_the_dashboard_value_is_in_usd_and_needs_the_eur_usd_rate(self):
        view = await self.svc.portfolio(1)
        self.assertEqual(view["connector"], "Trading 212")
        by_token = {b["token"]: b for b in view["balances"]}
        self.assertAlmostEqual(by_token["EUR"]["usd_value"], 40 * 1.10, places=6)
        self.assertAlmostEqual(by_token["MSFT"]["usd_value"], 0.05 * 400, places=6)
        self.assertAlmostEqual(by_token["SAP"]["usd_value"], 100, places=6)
        self.assertAlmostEqual(view["total_usd"], 40 * 1.10 + 20 + 100, places=6)

        async def no_rate():
            return None

        self.svc.eur_usd = no_rate
        with self.assertRaises(ValueError) as caught:
            await self.svc.portfolio(1)
        self.assertIn("EUR/USD", str(caught.exception))


class MarketTests(Base):
    async def test_stocks_are_the_intersection_of_trading212_and_the_reference_feed(
        self,
    ):
        result = await self.svc.stocks(1)
        symbols = [r["symbol"] for r in result["stocks"]]
        # AMZN is on the feed but not on Trading 212; NVDA is the reverse; SPY is an ETF.
        self.assertEqual(symbols, ["MSFT", "AAPL", "TSLA", "DUP"])  # by dollar volume
        self.assertNotIn("AMZN", symbols)
        self.assertNotIn("NVDA", symbols)
        self.assertNotIn("SPY", symbols)
        self.assertIn("Kraken xStocks public data", result["reference_note"])
        self.assertEqual(result["price_currency"], "USD")
        aapl = result["stocks"][1]
        self.assertEqual(aapl["ticker"], "AAPL_US_EQ")
        self.assertEqual(aapl["price"], "200.05")
        self.assertIn("volume_24h_usd", aapl)
        self.assertNotIn("unfiltered", result)

    async def test_held_stocks_are_always_listed_and_a_keyword_filters(self):
        with patch.object(t212, "TOP_STOCKS", 1):
            result = await self.svc.stocks(1)
        self.assertEqual([r["symbol"] for r in result["stocks"]], ["MSFT"])
        self.svc.held["AAPL_US_EQ"] = position("AAPL_US_EQ", "0.01", "200", "1.8")
        await self.svc.sync_from_account(1)
        with patch.object(t212, "TOP_STOCKS", 1):
            result = await self.svc.stocks(1)
        # The top one plus every stock the bot holds, whatever its rank.
        self.assertEqual({r["symbol"] for r in result["stocks"]}, {"AAPL", "MSFT"})
        found = await self.svc.stocks(1, "ts")
        self.assertEqual([r["symbol"] for r in found["stocks"]], ["TSLA"])

    async def test_not_connected_the_list_is_the_unfiltered_reference_and_says_so(self):
        self.svc.disconnect(1)
        result = await self.svc.stocks(1)
        self.assertTrue(result["unfiltered"])
        self.assertIn("not confirmed as tradable", result["universe_note"])
        self.assertIn("AMZN", [r["symbol"] for r in result["stocks"]])

    async def test_connected_an_unreadable_instrument_list_is_an_error_not_a_guess(
        self,
    ):
        self.svc.instruments_raw = []
        with self.assertRaises(ValueError):
            await self.svc.stocks(1)

    async def test_prices_feed_the_scorecard_under_this_venue(self):
        await self.svc.stocks(1)
        self.assertEqual(sc.latest_price(1, "AAPL", venue="trading212")[1], 200.05)
        self.assertIsNone(sc.latest_price(1, "AAPL"))

    async def test_predict_and_scorecard_use_the_trading212_store(self):
        result = await self.svc.predict(1, "AAPL", "trade", "1.5", "1", "4", "note")
        self.assertIsNotNone(result)
        report = await self.svc.scorecard(1)
        self.assertGreater(report["assumptions"]["round_trip_cost_pct"], 0.3)
        self.assertTrue((t212._root(1) / "scorecard.db").exists())

    async def test_candles_are_labelled_as_reference_data(self):
        result = await self.svc.candles(1, "AAPL")
        self.assertTrue(result["reference"])
        self.assertEqual(result["price_currency"], "USD")


class PreviewTests(Base):
    async def test_a_buy_preview_sizes_from_the_reference_and_labels_every_estimate(
        self,
    ):
        await self.synced()
        before = len(self.svc.sent)
        result = await self.svc.preview(1, "buy", "AAPL", "8")
        self.assertEqual(self.svc.posts(), [])
        self.assertTrue(all(m == "GET" for m, *_ in self.svc.sent[before:]))
        self.assertEqual(result["ticker"], "AAPL_US_EQ")
        self.assertEqual(result["order_type"], "limit")
        self.assertEqual(
            result["limit_price"], "200.71"
        )  # ask 200.10 + 0.3%, up to the cent
        self.assertEqual(result["quantity"], "0.0435")
        self.assertEqual(result["price_currency"], "USD")
        notional = Decimal(result["notional_eur"])
        self.assertTrue(Decimal("7.8") < notional <= Decimal("8"))
        sources = result["round_trip_sources"]
        self.assertEqual(sources["fx_fee"], "published")
        self.assertTrue(sources["spread"].startswith("estimate"))
        self.assertEqual(sources["slippage"], "assumption")
        parts = {k: Decimal(v) for k, v in result["round_trip_components_pct"].items()}
        self.assertEqual(parts["fx_fee"], Decimal("0.3"))
        total = sum(parts.values())
        self.assertAlmostEqual(
            Decimal(result["estimated_round_trip_pct"]), total, delta=Decimal("0.01")
        )
        self.assertFalse(result["cost_guard_blocks_buy"])
        self.assertEqual(result["cost_guard_max_round_trip_pct"], "1.0")
        self.assertIn("ESTIMATE", result["cost_label"])
        self.assertEqual(result["schedule_check"], "open")
        self.assertTrue(result["market_open"])
        self.assertEqual(result["fx_eur_usd"], "1.1")
        self.assertEqual(result["reference_source"], "Kraken xStocks public data")

    async def test_a_tiny_buy_is_flagged_by_the_cost_guard_in_the_preview(self):
        await self.synced()
        result = await self.svc.preview(1, "buy", "AAPL", "3")
        self.assertTrue(result["cost_guard_blocks_buy"])
        self.assertGreater(Decimal(result["estimated_round_trip_pct"]), Decimal("1.0"))

    async def test_an_observed_fx_fee_replaces_the_published_one_and_is_labelled(self):
        self.set_ledger(observed_fx_fee_pct="0.16")
        result = await self.svc.preview(1, "buy", "AAPL", "8")
        self.assertEqual(result["fee_source"], "observed")
        self.assertEqual(
            Decimal(result["round_trip_components_pct"]["fx_fee"]), Decimal("0.32")
        )
        self.assertEqual(self.svc.status(1)["limits"]["fx_fee_pct_each_way"], "0.16")

    async def test_a_sale_is_sized_in_units_with_the_account_price_as_reference(self):
        await self.synced()
        result = await self.svc.preview(1, "sell", "MSFT", "0.05")
        self.assertEqual(result["quantity"], "0.05")  # the whole holding, exactly
        # Reference 400.00 vs Trading 212's own 400: limit 0.3% under, down to the cent.
        self.assertEqual(result["limit_price"], "398.8")
        self.assertIn("Trading 212 position price", result["reference_source"])
        part = await self.svc.preview(1, "sell", "MSFT", "0.03339")
        self.assertEqual(part["quantity"], "0.0333")  # rounded DOWN to the step

    async def test_instrument_minimums_and_the_one_euro_floor_are_enforced(self):
        await self.synced()
        with self.assertRaises(ValueError) as caught:
            await self.svc.preview(1, "buy", "TSLA", "8")  # 0.03 shares < 0.5 minimum
        self.assertIn("minimum", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            await self.svc.preview(1, "buy", "AAPL", "0.5")
        self.assertIn("minimum", str(caught.exception))
        with self.assertRaises(ValueError):
            await self.svc.preview(1, "buy", "MSFT", "0.001")  # rounds to zero shares

    async def test_only_listed_us_stocks_with_a_reference_price_can_be_sized(self):
        for symbol in ("AMZN", "NVDA", "SPY", "SAP", "ZZZ"):
            with self.assertRaises(ValueError):
                await self.svc.preview(1, "buy", symbol, "8")
        with self.assertRaises(ValueError):
            await self.svc.preview(1, "buy", "AAPL_US_EQ", "8")

    async def test_bad_arguments_and_a_wide_reference_spread_are_refused(self):
        with self.assertRaises(ValueError):
            await self.svc.preview(1, "hold", "AAPL", "8")
        with self.assertRaises(ValueError):
            await self.svc.preview(1, "buy", "AAPL", "8", order_type="stop")
        with self.assertRaises(ValueError):
            await self.svc.preview(1, "buy", "AAPL", "8", slippage_pct="2.5")
        with self.assertRaises(ValueError):
            await self.svc.preview(1, "buy", "AAPL", "1e3")
        self.svc.market_feed.tickers["AAPLxUSD"] = kraken("100", "105", "102", "1")
        with self.assertRaises(ValueError) as caught:
            await self.svc.preview(1, "buy", "AAPL", "8")
        self.assertIn("spread", str(caught.exception))

    async def test_no_eur_usd_rate_means_no_sizing(self):
        async def no_rate():
            return None

        self.svc.eur_usd = no_rate
        with self.assertRaises(ValueError) as caught:
            await self.svc.preview(1, "buy", "AAPL", "8")
        self.assertIn("EUR/USD", str(caught.exception))

    async def test_the_exchange_schedule_cross_checks_the_session_clock(self):
        self.svc.exchanges_raw[0]["workingSchedules"][0]["timeEvents"] = [
            {"date": iso(NOW - 600), "type": "CLOSE"}
        ]
        result = await self.svc.preview(1, "buy", "AAPL", "8")
        self.assertEqual(result["schedule_check"], "closed")
        self.svc.exchanges_raw = []
        self.svc._exchanges.clear()
        self.assertEqual(
            (await self.svc.preview(1, "buy", "AAPL", "8"))["schedule_check"], "unknown"
        )
        self.svc.exchanges_raw = [
            {
                "id": 1,
                "workingSchedules": [
                    {
                        "id": 7,
                        "timeEvents": [{"date": iso(NOW - 6 * 86400), "type": "OPEN"}],
                    }
                ],
            }
        ]
        self.svc._exchanges.clear()
        self.assertEqual(
            (await self.svc.preview(1, "buy", "AAPL", "8"))["schedule_check"], "unknown"
        )


class OrderTests(Base):
    async def buy(self, amount="8", **kwargs):
        return await self.svc.order(1, "buy", "AAPL", amount, **kwargs)

    async def test_a_filled_buy_posts_one_day_limit_and_moves_the_ledger_by_the_account_cash(
        self,
    ):
        await self.synced()
        before = Decimal(self.ledger()["cash"])
        result = await self.buy()
        (body,) = self.svc.posts()
        self.assertEqual(
            body,
            {
                "ticker": "AAPL_US_EQ",
                "quantity": 0.0435,
                "limitPrice": 200.71,
                "timeValidity": "DAY",
            },
        )
        order = result["order"]
        self.assertEqual(order["status"], "filled")
        self.assertEqual(order["filled_quantity"], "0.0435")
        self.assertEqual(order["average_fill_price"], "200.1")
        self.assertEqual(order["value_source"], "account cash change")
        self.assertFalse(order["cancelled_by_condor"])
        self.assertIn("reference_price", order)
        spent = Decimal("0.0435") * Decimal("200.10") / FX * Decimal("1.0015")
        ledger = self.ledger()
        self.assertAlmostEqual(before - Decimal(ledger["cash"]), spent, places=6)
        position = ledger["positions"]["AAPL"]
        self.assertEqual(position["units"], "0.0435")
        self.assertAlmostEqual(Decimal(position["cost"]), spent, places=6)
        self.assertEqual(ledger["tickers"]["AAPL"], "AAPL_US_EQ")
        self.assertIsNone(ledger["uncertain_order"])
        self.assertTrue(ledger["enabled"])
        self.assertEqual(ledger["orders"][-1]["status"], "filled")
        self.assertEqual(ledger["orders"][-1]["fee_currency"], "EUR")
        self.assertEqual(len(self.svc.deletes()), 0)

    async def test_a_reported_fx_fee_is_split_out_of_the_cash_change(self):
        await self.synced()
        self.svc.fill = {"quantity": None, "price": "200.10", "extra": {"fee": 0.0119}}
        result = await self.buy()
        self.assertEqual(result["order"]["fee_source"], "reported")
        self.assertEqual(result["order"]["fee_eur"], "0.0119")
        order = self.ledger()["orders"][-1]
        spent = Decimal("0.0435") * Decimal("200.10") / FX * Decimal("1.0015")
        self.assertAlmostEqual(
            Decimal(order["value"]) + Decimal(order["fee"]), spent, places=6
        )

    async def test_the_fx_fee_is_learned_only_from_a_fill_big_enough_to_read(self):
        await self.synced()
        self.set_ledger(cash="400")
        self.svc.cash = Decimal("400")
        self.svc.fill = {"quantity": None, "price": "200.10", "extra": {"fee": 0.2}}
        await self.svc.order(1, "buy", "AAPL", "160")
        self.assertIsNotNone(self.ledger()["observed_fx_fee_pct"])
        self.assertAlmostEqual(
            Decimal(self.ledger()["observed_fx_fee_pct"]),
            Decimal("0.125"),
            delta=Decimal("0.02"),
        )

    async def test_a_market_order_posts_a_market_body_and_never_extended_hours(self):
        await self.synced()
        result = await self.buy(order_type="market")
        (body,) = self.svc.posts()
        self.assertEqual(
            body, {"ticker": "AAPL_US_EQ", "quantity": 0.0435, "extendedHours": False}
        )
        self.assertEqual(self.svc.sent[-1][0] != "POST", True)
        self.assertEqual(result["order"]["order_type"], "market")

    async def test_a_market_fill_far_from_the_reference_is_called_out(self):
        await self.synced()
        self.svc.fill = {"quantity": None, "price": "210"}
        result = await self.buy(order_type="market")
        self.assertIn("market order filled", result["order"]["price_note"])

    async def test_a_partial_fill_cancels_the_rest_and_books_only_what_filled(self):
        await self.synced()
        self.svc.fill = {"quantity": "0.02", "price": "200.10"}
        self.svc.stuck = True
        result = await self.buy()
        order = result["order"]
        self.assertTrue(order["cancelled_by_condor"])
        self.assertEqual(order["filled_quantity"], "0.02")
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(self.svc.deletes(), [f"/equity/orders/{OID}"])
        self.assertEqual(self.ledger()["positions"]["AAPL"]["units"], "0.02")
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_a_zero_fill_that_stays_pending_is_cancelled_and_moves_no_money(self):
        await self.synced()
        self.svc.fill = {"quantity": "0", "price": "200.10"}
        self.svc.stuck = True
        self.svc.lists_unfilled = True
        before = self.ledger()
        result = await self.buy()
        self.assertEqual(result["order"]["status"], "cancelled")
        self.assertEqual(result["order"]["note"], "Nothing was filled; no money moved.")
        after = self.ledger()
        self.assertEqual(after["cash"], before["cash"])
        self.assertEqual(after["positions"], before["positions"])
        self.assertEqual(len(self.svc.deletes()), 1)

    async def test_a_zero_fill_missing_from_the_history_is_inferred_only_after_a_cancel(
        self,
    ):
        await self.synced()
        self.svc.fill = {"quantity": "0", "price": "200.10"}
        self.svc.stuck = True
        result = await self.buy()
        self.assertIn("position unchanged", result["order"]["inferred"])
        self.assertEqual(self.ledger()["positions"].get("AAPL"), None)

    async def test_an_order_that_is_pending_for_a_while_then_fills_needs_no_cancel(
        self,
    ):
        await self.synced()
        self.svc.pending_polls = 2
        result = await self.buy()
        self.assertEqual(result["order"]["status"], "filled")
        self.assertEqual(self.svc.deletes(), [])

    async def test_a_cancel_that_does_not_take_leaves_the_order_uncertain(self):
        await self.synced()
        self.svc.fill = {"quantity": "0", "price": "200.10"}
        self.svc.stuck = True
        self.svc.delete_error = t212.Trading212Error(500, "boom")
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("reconciliation", str(caught.exception))
        pending = self.ledger()["uncertain_order"]
        self.assertEqual(pending["order_id"], OID)
        self.assertFalse(self.svc.status(1)["enabled"])

    async def test_a_filled_sale_books_the_proceeds_and_closes_the_position(self):
        await self.synced()
        self.svc.fill = {"quantity": None, "price": "399.5"}
        result = await self.svc.order(1, "sell", "MSFT", "0.05")
        (body,) = self.svc.posts()
        self.assertEqual(body["quantity"], -0.05)
        self.assertEqual(body["limitPrice"], 398.8)
        self.assertEqual(result["order"]["filled_quantity"], "0.05")
        ledger = self.ledger()
        self.assertEqual(ledger["positions"], {})
        self.assertNotIn("MSFT", ledger["tickers"])
        proceeds = Decimal("0.05") * Decimal("399.5") / FX * Decimal("0.9985")
        self.assertAlmostEqual(
            Decimal(ledger["cash"]), Decimal("40") + proceeds, places=6
        )

    async def test_a_partial_sale_keeps_the_rest_of_the_position_at_its_cost(self):
        await self.synced()
        self.svc.fill = {"quantity": None, "price": "399.5"}
        await self.svc.order(1, "sell", "MSFT", "0.02")
        position = self.ledger()["positions"]["MSFT"]
        self.assertEqual(position["units"], "0.03")
        self.assertAlmostEqual(
            Decimal(position["cost"]), Decimal("17.5") * Decimal("0.6"), places=6
        )

    async def test_selling_more_than_the_bot_holds_or_what_is_not_in_the_bot_is_refused(
        self,
    ):
        await self.synced()
        for symbol, units in (("MSFT", "0.06"), ("AAPL", "1")):
            with self.assertRaises(ValueError):
                await self.svc.order(1, "sell", symbol, units)
        self.assertEqual(self.svc.posts(), [])

    async def test_an_account_that_holds_fewer_shares_than_the_ledger_refuses_the_sale(
        self,
    ):
        await self.synced()
        self.svc.held["MSFT_US_EQ"] = position(
            "MSFT_US_EQ", "0.05", "400", "17.5", available="0.01"
        )
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "sell", "MSFT", "0.05")
        self.assertIn("shares", str(caught.exception))

    async def test_the_forty_percent_cap_applies_below_one_thousand(self):
        await self.synced()
        self.svc.fill = {"quantity": None, "price": "200.10"}
        with self.assertRaises(ValueError) as caught:
            await self.buy("30")  # equity is ~58; 30 EUR exceeds 40% of it
        self.assertIn("40%", str(caught.exception))
        self.assertEqual(self.svc.posts(), [])
        await self.buy("20")  # inside the cap
        with self.assertRaises(ValueError):
            await self.buy("20")  # the second would take the asset above the cap

    async def test_the_equity_floor_pauses_trading_and_places_nothing(self):
        await self.synced()
        self.svc.held.clear()
        self.svc.cash = Decimal("1")
        self.set_ledger(cash="1", positions={}, tickers={})
        with self.assertRaises(ValueError) as caught:
            await self.buy("1.5")
        self.assertIn("stop threshold", str(caught.exception))
        self.assertFalse(self.ledger()["enabled"])
        self.assertEqual(self.svc.posts(), [])

    async def test_above_one_thousand_the_reserve_and_one_sixth_rules_apply(self):
        ledger = {"cash": "1000", "positions": {}}
        with self.assertRaises(ValueError):
            t212.check_purchase(ledger, "AAPL", Decimal("200"), Decimal("1000"))
        t212.check_purchase(ledger, "AAPL", Decimal("160"), Decimal("1000"))
        with self.assertRaises(ValueError):
            t212.check_purchase(
                {"cash": "1000", "positions": {"AAPL": {"cost": "10"}}},
                "AAPL",
                Decimal("10"),
                Decimal("1000"),
            )

    async def test_not_enough_cash_in_the_ledger_or_the_account_places_nothing(self):
        await self.synced()
        self.set_ledger(cash="3")
        with self.assertRaises(ValueError):
            await self.buy()
        self.set_ledger(cash="40")
        self.svc.cash = Decimal("3")
        self.svc._cache.clear()
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("Insufficient", str(caught.exception))
        self.assertEqual(self.svc.posts(), [])

    async def test_a_paused_or_unsynced_bot_never_orders(self):
        with self.assertRaises(ValueError):
            await self.buy()
        await self.synced(enabled=False)
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("paused", str(caught.exception))
        self.assertEqual(self.svc.posts(), [])

    async def test_nothing_is_ordered_outside_the_regular_session(self):
        await self.synced()
        self.svc.clock = lambda: CLOSED
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("regular US session", str(caught.exception))
        self.assertEqual(self.svc.posts(), [])

    async def test_a_closed_exchange_schedule_refuses_the_order_even_inside_the_clock_session(
        self,
    ):
        await self.synced()
        self.svc.exchanges_raw[0]["workingSchedules"][0]["timeEvents"] = [
            {"date": iso(NOW - 600), "type": "CLOSE"}
        ]
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("working schedule", str(caught.exception))
        self.assertEqual(self.svc.posts(), [])

    async def test_an_unreadable_schedule_does_not_block_the_clock_checked_session(
        self,
    ):
        await self.synced()
        self.svc.exchanges_error = t212.Trading212Error(403, "scope")
        result = await self.buy()
        self.assertEqual(result["order"]["status"], "filled")

    async def test_at_most_ten_orders_a_minute(self):
        await self.synced()
        now = time.time()
        self.set_ledger(orders=[{"time": now - i} for i in range(10)])
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("ten orders", str(caught.exception))
        self.assertEqual(self.svc.posts(), [])

    async def test_the_cost_guard_refuses_a_costly_buy_but_never_a_sale(self):
        await self.synced()
        with self.assertRaises(ValueError) as caught:
            await self.buy("3")
        message = str(caught.exception)
        self.assertIn("Refused, no order placed", message)
        self.assertIn("round-trip cost", message)
        self.assertIn("Selling a position is never blocked", message)
        self.assertEqual(self.svc.posts(), [])
        self.assertIsNone(self.ledger()["uncertain_order"])
        # A tiny sale (about 1.5 EUR) goes through whatever it costs.
        self.svc.fill = {"quantity": None, "price": "399.5"}
        await self.svc.order(1, "sell", "MSFT", "0.004")
        self.assertEqual(len(self.svc.posts()), 1)

    async def test_the_order_is_written_before_it_is_sent(self):
        await self.synced()
        seen = {}
        original = self.svc.private

        async def spy(user_id, method, path, query=None, body=None):
            if method == "POST":
                seen["ledger"] = t212._read_ledger(1)["uncertain_order"]
            return await original(user_id, method, path, query, body)

        self.svc.private = spy
        await self.buy()
        pending = seen["ledger"]
        self.assertEqual(pending["ticker"], "AAPL_US_EQ")
        self.assertEqual(pending["side"], "buy")
        self.assertEqual(pending["quantity"], "0.0435")
        self.assertNotIn("order_id", pending)

    async def test_a_definite_refusal_clears_the_pending_entry_and_keeps_trading_on(
        self,
    ):
        await self.synced()
        self.svc.place_error = t212.Trading212Error(400, "Invalid quantity")
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("rejected the order", str(caught.exception))
        ledger = self.ledger()
        self.assertIsNone(ledger["uncertain_order"])
        self.assertTrue(ledger["enabled"])

    async def test_a_429_on_placement_is_definite_too(self):
        await self.synced()
        self.svc.place_error = t212.Trading212Error(429, "slow", retry_after=3)
        with self.assertRaises(ValueError):
            await self.buy()
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_a_5xx_408_or_transport_failure_is_uncertain_and_pauses_trading(self):
        for failure in (
            {"place_error": t212.Trading212Error(503, "down")},
            {"place_error": t212.Trading212Error(408, "")},
            {"place_fail": True},
            {"no_id": True},
        ):
            self.tearDown()
            self.setUp()
            await self.synced()
            for key, value in failure.items():
                setattr(self.svc, key, value)
            with self.assertRaises(ValueError) as caught:
                await self.buy()
            self.assertIn("reconciliation", str(caught.exception), failure)
            self.assertIn("Do not resubmit", str(caught.exception))
            ledger = self.ledger()
            self.assertEqual(ledger["uncertain_order"]["ticker"], "AAPL_US_EQ")
            self.assertFalse(self.svc.status(1)["enabled"])
            with self.assertRaises(ValueError):
                await self.buy()  # paused until reconciled

    async def test_errors_never_carry_the_credentials(self):
        await self.synced()
        self.svc.place_error = t212.Trading212Error(
            400, f"bad {API_KEY} {API_SECRET}", (API_KEY, API_SECRET)
        )
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertNotIn(API_KEY, str(caught.exception))
        self.assertNotIn(API_SECRET, str(caught.exception))

    async def test_an_unreadable_fill_after_a_real_order_is_uncertain_not_ignored(self):
        await self.synced()
        self.svc.history_extra = {}
        original = self.svc._history_detail

        async def none(*args, **kwargs):
            return None

        self.svc._history_detail = none
        with self.assertRaises(ValueError) as caught:
            await self.buy()
        self.assertIn("reconciliation", str(caught.exception))
        self.assertEqual(self.ledger()["uncertain_order"]["order_id"], OID)
        self.svc._history_detail = original


class ReconcileTests(Base):
    async def uncertain(self, **failure):
        await self.synced()
        for key, value in failure.items():
            setattr(self.svc, key, value)
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "AAPL", "8")
        pending = self.ledger()["uncertain_order"]
        self.assertIsNotNone(pending)
        return pending

    def age(self, seconds):
        pending = self.ledger()["uncertain_order"]
        pending["time"] = time.time() - seconds
        self.set_ledger(uncertain_order=pending)
        return pending["time"]

    def record(self, when, **x):
        quantity = Decimal("0.0435")
        self.svc.records.append(
            (
                OID + 1,
                "AAPL_US_EQ",
                "buy",
                quantity,
                quantity,
                Decimal("200.10"),
                when,
                x,
            )
        )

    async def test_an_order_found_pending_is_cancelled_not_left_resting(self):
        pending = await self.uncertain(place_fail=True)
        when = self.age(120)
        self.svc.pending_list = [
            {
                "id": 777,
                "ticker": "AAPL_US_EQ",
                "side": "BUY",
                "status": "NEW",
                "type": "LIMIT",
                "quantity": float(pending["quantity"]),
                "initiatedFrom": "API",
                "createdAt": iso(when + 1),
            }
        ]
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("cancel was requested", str(caught.exception))
        self.assertEqual(self.svc.deletes(), ["/equity/orders/777"])
        self.assertIsNotNone(self.ledger()["uncertain_order"])  # still not settled

    async def test_an_order_found_in_the_history_is_applied_and_trading_stays_paused(
        self,
    ):
        await self.uncertain(place_fail=True)
        when = self.age(120)
        self.record(when + 1, net=7.92, fee=0.0119)
        self.svc.held["AAPL_US_EQ"] = position("AAPL_US_EQ", "0.0435", "200.10", "7.93")
        status = await self.svc.reconcile(1)
        ledger = status["ledger"]
        self.assertIsNone(ledger["uncertain_order"])
        self.assertFalse(ledger["enabled"])
        self.assertEqual(ledger["positions"]["AAPL"]["units"], "0.0435")
        order = ledger["orders"][-1]
        self.assertEqual(order["value_source"], "reported netValue of the fills")
        self.assertEqual(order["fee_source"], "reported")
        self.assertEqual(Decimal(order["value"]), Decimal("7.92"))
        self.assertEqual(
            Decimal(ledger["cash"]), Decimal("40") - Decimal("7.92") - Decimal("0.0119")
        )

    async def test_a_known_order_id_is_looked_up_directly_and_applied(self):
        await self.synced()

        async def none(*args, **kwargs):
            return None

        original = self.svc._history_detail
        self.svc._history_detail = none
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "AAPL", "8")
        self.svc._history_detail = original
        self.assertEqual(self.ledger()["uncertain_order"]["order_id"], OID)
        self.assertEqual(self.ledger()["positions"].get("AAPL"), None)
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertFalse(status["ledger"]["enabled"])
        self.assertEqual(status["ledger"]["positions"]["AAPL"]["units"], "0.0435")
        self.assertEqual(self.svc.deletes(), [])

    async def test_an_order_never_seen_stays_until_a_minute_has_passed_then_clears(
        self,
    ):
        await self.uncertain(place_fail=True)
        self.age(10)
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("Wait a minute", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        self.age(120)
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertFalse(status["ledger"]["enabled"])
        self.assertEqual(status["ledger"]["positions"].get("AAPL"), None)

    async def test_two_matching_orders_are_never_guessed_between(self):
        await self.uncertain(place_fail=True)
        when = self.age(120)
        self.record(when + 1)
        self.svc.records.append(
            (
                OID + 2,
                "AAPL_US_EQ",
                "buy",
                Decimal("0.0435"),
                Decimal("0.0435"),
                Decimal("200.1"),
                when + 2,
                {},
            )
        )
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("More than one", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_an_order_placed_by_hand_in_the_app_is_not_ours(self):
        await self.uncertain(place_fail=True)
        when = self.age(120)
        self.record(when + 1, origin="IOS")
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertEqual(status["ledger"]["positions"].get("AAPL"), None)

    async def test_an_older_or_different_order_does_not_match(self):
        await self.uncertain(place_fail=True)
        when = self.age(120)
        self.record(when - 3600)
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])

    async def test_reconcile_with_nothing_pending_changes_nothing(self):
        await self.synced()
        before = self.ledger()
        status = await self.svc.reconcile(1)
        self.assertEqual(status["ledger"], before)


class FlowsTests(unittest.IsolatedAsyncioTestCase):
    def test_deposits_and_withdrawals_are_signed_by_their_type(self):
        data = {
            "items": [
                {
                    "type": "DEPOSIT",
                    "amount": 50,
                    "currency": "EUR",
                    "dateTime": "2026-10-20T10:00:00Z",
                    "reference": "a",
                },
                {
                    "type": "WITHDRAW",
                    "amount": 10,
                    "currency": "EUR",
                    "dateTime": "2026-10-21T10:00:00+02:00",
                    "reference": "b",
                },
                {
                    "type": "WITHDRAW",
                    "amount": -5,
                    "currency": "EUR",
                    "dateTime": "2026-10-22T10:00:00Z",
                    "reference": "c",
                },
                {
                    "type": "FEE",
                    "amount": -1,
                    "currency": "EUR",
                    "dateTime": "2026-10-22T10:00:00Z",
                    "reference": "d",
                },
                {
                    "type": "INTEREST_ON_FREE_CASH",
                    "amount": 1,
                    "currency": "EUR",
                    "dateTime": "2026-10-22T10:00:00Z",
                    "reference": "e",
                },
                {
                    "type": "TRANSFER",
                    "amount": 1,
                    "currency": "EUR",
                    "dateTime": "2026-10-22T10:00:00Z",
                    "reference": "f",
                },
                {
                    "type": "DEPOSIT",
                    "amount": 0,
                    "currency": "EUR",
                    "dateTime": "2026-10-22T10:00:00Z",
                    "reference": "g",
                },
                {
                    "type": "DEPOSIT",
                    "amount": 3,
                    "currency": "EUR",
                    "dateTime": "bad",
                    "reference": "h",
                },
                "junk",
            ]
        }
        flows = fetchers.parse_trading212_transactions(data)
        self.assertEqual(
            [(f.external_id, f.amount, f.currency, f.status) for f in flows],
            [
                ("transaction:a", Decimal(50), "EUR", "confirmed"),
                ("transaction:b", Decimal(-10), "EUR", "confirmed"),
                ("transaction:c", Decimal(-5), "EUR", "confirmed"),
            ],
        )
        self.assertEqual(
            flows[0].ts, datetime(2026, 10, 20, 10, tzinfo=timezone.utc).timestamp()
        )
        self.assertEqual(
            flows[1].ts, datetime(2026, 10, 21, 8, tzinfo=timezone.utc).timestamp()
        )

    def test_an_unexpected_shape_is_unavailable_not_a_crash(self):
        with self.assertRaises(fetchers.FetchUnavailable):
            fetchers.parse_trading212_transactions({"nope": []})

    async def test_the_fetcher_follows_the_cursor_and_degrades_on_an_error(self):
        class Service:
            def __init__(self):
                self.calls = []

            async def private(self, user_id, method, path, query=None, body=None):
                self.calls.append((method, path, query))
                if len(self.calls) == 1:
                    return {
                        "items": [
                            {
                                "type": "DEPOSIT",
                                "amount": 5,
                                "currency": "EUR",
                                "dateTime": "2026-10-20T10:00:00Z",
                                "reference": "a",
                            }
                        ],
                        "nextPagePath": "/api/v0/equity/history/transactions?cursor=abc&limit=50",
                    }
                return {
                    "items": [
                        {
                            "type": "WITHDRAW",
                            "amount": 2,
                            "currency": "EUR",
                            "dateTime": "2026-10-21T10:00:00Z",
                            "reference": "b",
                        }
                    ],
                    "nextPagePath": None,
                }

        service = Service()
        out = await fetchers.fetch("trading212", service, 1, NOW - 86400, NOW)
        self.assertEqual(
            [f.external_id for f in out.flows], ["transaction:a", "transaction:b"]
        )
        self.assertEqual(out.problems, [])
        self.assertEqual(service.calls[0][2]["limit"], 50)
        self.assertEqual(
            service.calls[1][1],
            "/api/v0/equity/history/transactions?cursor=abc&limit=50",
        )

        class Broken:
            async def private(self, *args, **kwargs):
                raise t212.Trading212Error(403, "no scope")

        out = await fetchers.fetch("trading212", Broken(), 1, NOW - 86400, NOW)
        self.assertEqual(out.flows, [])
        self.assertTrue(out.problems and out.problems[0].startswith("transactions:"))

    async def test_a_foreign_next_page_path_is_never_followed(self):
        class Service:
            async def private(self, *args, **kwargs):
                return {
                    "items": [
                        {
                            "type": "DEPOSIT",
                            "amount": 5,
                            "currency": "EUR",
                            "dateTime": "2026-10-20T10:00:00Z",
                            "reference": "a",
                        }
                    ],
                    "nextPagePath": "https://evil.example/x",
                }

        out = await fetchers.fetch("trading212", Service(), 1, NOW - 86400, NOW)
        self.assertTrue(out.problems)


class HistoryTests(Base):
    async def test_history_rows_are_eur_figures_in_the_shape_the_other_venues_use(self):
        self.svc.lists_unfilled = True
        self.svc.records = [
            (
                1,
                "AAPL_US_EQ",
                "buy",
                Decimal("0.05"),
                Decimal("0.05"),
                Decimal("200.1"),
                NOW - 100,
                {"net": 9.1, "fee": 0.014},
            ),
            (
                2,
                "AAPL_US_EQ",
                "sell",
                Decimal("0.05"),
                Decimal("0"),
                Decimal("0"),
                NOW - 50,
                {},
            ),
        ]
        rows = await self.svc.history(1)
        self.assertEqual([r["order_id"] for r in rows], [2, 1])  # newest first
        bought = rows[1]
        self.assertEqual(bought["pair"], "AAPL_US_EQ")
        self.assertEqual(bought["side"], "buy")
        self.assertEqual(bought["status"], "filled")
        self.assertEqual(bought["filled_quantity"], "0.05")
        self.assertEqual(bought["average_fill_price"], "200.1")
        self.assertEqual(bought["filled_amount"], "9.1")
        self.assertEqual(bought["total_fee"], "0.014")
        self.assertEqual(bought["fee_currency"], "EUR")
        self.assertEqual(bought["updated"], int((NOW - 100 + 1) * 1000))
        self.assertNotIn("average_fill_price", rows[0])

    async def test_one_corrupt_record_does_not_hide_the_rest_of_the_history_panel(self):
        bad = history_item(
            1, "AAPL_US_EQ", "buy", Decimal("1"), "FILLED", Decimal("1"), "200", NOW
        )
        bad["order"]["filledQuantity"] = 5
        good = history_item(
            2, "AAPL_US_EQ", "buy", Decimal("1"), "FILLED", Decimal("1"), "200", NOW
        )
        self.svc.history_extra = {"items": [bad, good]}
        rows = await self.svc.history(1)
        self.assertEqual([r["order_id"] for r in rows], [2])

    async def test_fill_records_that_disagree_with_the_order_are_refused(self):
        bad = history_item(
            1, "AAPL_US_EQ", "buy", Decimal("1"), "FILLED", Decimal("1"), "200", NOW
        )
        bad["order"]["filledQuantity"] = 5
        with self.assertRaises(ValueError):
            t212.Trading212Service.group_history([bad])


if __name__ == "__main__":
    unittest.main()


def test_response_cap_fits_the_full_instrument_list():
    # The live instrument list is several MB (about 17k rows); a 4 MiB cap made every sync fail.
    from condor import trading212

    assert trading212.MAX_BYTES >= 16 * 1024 * 1024
