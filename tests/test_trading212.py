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
from tests import test_alpaca_market as alp

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
        self.total_value = Decimal("0")  # the summary's own account total
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
        self.history_hides = False  # the history omits API orders (seen live)
        self.after_place = None  # a callable run right after an order was placed
        self.object_final = None  # (status, filled) the by-id read answers with

    async def eur_usd(self):
        return FX

    def posts(self):
        return [body for method, path, query, body in self.sent if method == "POST"]

    def deletes(self):
        return [path for method, path, query, body in self.sent if method == "DELETE"]

    def _record(self):
        out = []
        if self.history_hides:
            return out
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
                "totalValue": float(self.total_value),
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
            if self.object_final:
                status, filled = self.object_final
                record = self.records[-1]
                return {
                    "id": OID,
                    "ticker": record[1],
                    "side": record[2].upper(),
                    "status": status,
                    "type": "LIMIT",
                    "quantity": float(record[3]),
                    "filledQuantity": float(filled),
                    "initiatedFrom": "API",
                    "createdAt": iso(NOW),
                }
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
        if self.after_place:
            self.after_place()
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

    async def test_the_eur_usd_rate_is_revolut_xs_public_price_never_krakens(self):
        calls = []

        async def revolut_rate():
            calls.append(1)
            return Decimal("1.12")

        with patch("condor.revolut_x.service.eur_usd", revolut_rate):
            first = await t212.Trading212Service.eur_usd(self.svc)
            second = await t212.Trading212Service.eur_usd(self.svc)
        self.assertEqual((first, second), (Decimal("1.12"), Decimal("1.12")))
        self.assertEqual(len(calls), 1)  # cached a minute
        self.assertFalse(hasattr(t212.xstocks_market.service, "eur_usd"))

    async def test_a_rate_read_a_moment_ago_keeps_the_dashboard_value_while_the_price_is_gone(
        self,
    ):
        async def no_rate():
            return None

        self.svc.eur_usd = no_rate
        self.svc._eur_usd = (time.monotonic(), FX)
        view = await self.svc.portfolio(1)
        self.assertAlmostEqual(view["total_usd"], 40 * 1.10 + 20 + 100, places=6)
        self.assertIsNone(await self.svc.eur_usd())  # sizing and orders never reuse it
        stale = time.monotonic() - t212.revolut_x.RATE_MAX_AGE_SEC - 1
        self.svc._eur_usd = (stale, FX)
        with self.assertRaisesRegex(ValueError, "EUR/USD"):
            await self.svc.portfolio(1)


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


ALPACA_SNAPS = {
    "AAPL": alp.snap("200.00", "200.04", "200.02", v=1000),
    "MSFT": alp.snap("400.00", "400.08", "400.04", v=9000),
    "TSLA": alp.snap("250.00", "250.05", "250.02", v=7000),
    "NVDA": alp.snap("130.00", "130.03", "130.01", v=20000),
    "DUP": alp.snap("20.00", "20.02", "20.01", v=100),
    "AMZN": alp.snap("180.00", "180.04", "180.02", v=99999),  # not on Trading 212
    "SPY": alp.snap("500.00", "500.05", "500.02", v=70000),  # an ETF: not listed
    "OLD": alp.snap(age=alp.am.MAX_QUOTE_AGE_SEC + 60),
}


def alpaca_answers(path, params):
    if path == "/v2/stocks/snapshots":
        return {
            s: ALPACA_SNAPS[s]
            for s in params["symbols"].split(",")
            if s in ALPACA_SNAPS
        }
    if path == "/v2/stocks/bars":
        bar = {"t": "2026-10-30T14:45:00Z", "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 9}
        return {"bars": {params["symbols"]: [bar]}}
    return {}


class AlpacaFeedTests(Base):
    """With an Alpaca key saved, the reference prices are Alpaca's IEX data, not Kraken's."""

    def use_alpaca(self, http=200):
        self.alpaca = alp.FakeAlpaca(alpaca_answers, http)
        self.svc.alpaca = self.alpaca
        self.alpaca.save_credentials(1, alp.KEY, alp.SECRET)

    def kraken_requests(self):
        return self.svc.market_feed.requests

    async def test_without_a_key_nothing_changes_and_the_status_says_kraken(self):
        self.svc.alpaca = alp.FakeAlpaca(alpaca_answers)
        self.assertEqual(
            self.svc.status(1)["market_data"],
            {
                "source": "kraken",
                "label": "Kraken xStocks public data",
                "alpaca_configured": False,
            },
        )
        result = await self.svc.stocks(1)
        self.assertIn("Kraken xStocks public data", result["reference_note"])
        self.assertEqual(self.svc.alpaca.sent, [])

    async def test_a_saved_key_prices_the_stock_table_from_alpaca_only(self):
        self.use_alpaca()
        self.assertEqual(self.svc.status(1)["market_data"]["source"], "alpaca")
        result = await self.svc.stocks(1)
        symbols = [r["symbol"] for r in result["stocks"]]
        # the watchlist on Trading 212, by today's IEX dollar volume; AMZN is not listed
        # there, SPY is an ETF and DUP is outside the watchlist
        self.assertEqual(symbols, ["NVDA", "MSFT", "TSLA", "AAPL"])
        self.assertIn("Alpaca", result["reference_note"])
        self.assertNotIn("Kraken", result["reference_note"])
        msft = result["stocks"][1]
        self.assertEqual(msft["ticker"], "MSFT_US_EQ")
        self.assertEqual(msft["min_trade_quantity"], "0.01")
        self.assertEqual(msft["price"], "400.04")
        self.assertEqual(self.kraken_requests(), [])

    async def test_a_stock_named_outright_is_priced_even_outside_the_watchlist(self):
        self.use_alpaca()
        found = await self.svc.stocks(1, "DUP")
        self.assertEqual([r["symbol"] for r in found["stocks"]], ["DUP"])
        nothing = await self.svc.stocks(1, "NOPE")
        self.assertEqual(nothing["stocks"], [])

    async def test_candles_come_from_alpaca_under_the_stocks_own_symbol(self):
        self.use_alpaca()
        out = await self.svc.candles(1, "aapl", "15m", 5)
        self.assertEqual(out["pair"], "AAPL")
        self.assertTrue(out["reference"])
        self.assertEqual(len(out["candles"]), 1)
        self.assertEqual(self.alpaca.sent[-1][0], "/v2/stocks/bars")
        with self.assertRaises(ValueError):
            await self.svc.candles(1, "not a symbol!", "15m", 5)

    async def test_a_preview_is_priced_from_the_alpaca_quote_and_says_so(self):
        await self.synced()
        self.use_alpaca()
        result = await self.svc.preview(1, "buy", "AAPL", "8")
        self.assertEqual(result["reference_source"], "Alpaca real-time IEX data")
        limit = Decimal(result["limit_price"])
        self.assertTrue(Decimal("200.64") <= limit <= Decimal("200.66"))  # ask + 0.3%
        self.assertEqual(self.kraken_requests(), [])

    async def test_a_listed_stock_outside_the_watchlist_can_still_be_previewed(self):
        await self.synced()
        self.use_alpaca()
        result = await self.svc.preview(1, "buy", "DUP", "8")
        self.assertEqual(result["reference_source"], "Alpaca real-time IEX data")

    async def test_a_quote_too_old_to_be_live_refuses_the_preview(self):
        await self.synced()
        self.use_alpaca()
        ALPACA_SNAPS["AAPL"] = alp.snap(age=alp.am.MAX_QUOTE_AGE_SEC + 60)
        try:
            with self.assertRaisesRegex(ValueError, "No live reference price"):
                await self.svc.preview(1, "buy", "AAPL", "8")
        finally:
            ALPACA_SNAPS["AAPL"] = alp.snap("200.00", "200.04", "200.02", v=1000)

    async def test_the_key_is_checked_before_it_is_saved_and_a_bad_one_is_not_kept(
        self,
    ):
        self.svc.alpaca = alp.FakeAlpaca(alpaca_answers, 403)
        with self.assertRaisesRegex(ValueError, "rejected the key"):
            await self.svc.save_feed_credentials(1, alp.KEY, alp.SECRET)
        self.assertFalse(self.svc.alpaca.configured(1))
        self.svc.alpaca = alp.FakeAlpaca(alpaca_answers)
        status = await self.svc.save_feed_credentials(1, alp.KEY, alp.SECRET)
        self.assertEqual(status["market_data"]["source"], "alpaca")
        self.assertNotIn(alp.SECRET, json.dumps(status))
        status = self.svc.disconnect_feed(1)
        self.assertEqual(status["market_data"]["source"], "kraken")

    async def test_saving_the_price_key_never_touches_the_trading_switch(self):
        await self.synced()
        self.svc.alpaca = alp.FakeAlpaca(alpaca_answers)
        await self.svc.save_feed_credentials(1, alp.KEY, alp.SECRET)
        self.assertTrue(self.svc.status(1)["enabled"])


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
        # The account snapshot is written with it, before anything is sent.
        self.assertEqual(pending["evidence_before"]["cash"], "40")
        self.assertEqual(pending["evidence_before"]["quantity"], "0")
        self.assertGreater(Decimal(pending["expected_eur"]), Decimal("7"))

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
        self.svc.fill = {"quantity": "0", "price": "200.10"}  # nothing moves
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
        # The positions read lags behind the fill (only the cash moved yet).
        self.svc.after_place = lambda: self.svc.held.pop("AAPL_US_EQ")
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

    async def test_an_order_never_seen_stays_until_three_minutes_have_passed_then_clears(
        self,
    ):
        await self.uncertain(place_fail=True)
        self.age(10)
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("Wait a minute", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        # A minute on, the history is silent and the account unchanged: still not
        # enough to say Trading 212 never created it.
        self.age(120)
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("No trace", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        self.age(t212.NOT_SEEN_AFTER_SEC + 20)
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
        when = self.age(200)
        self.record(when + 1, origin="IOS")
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertEqual(status["ledger"]["positions"].get("AAPL"), None)

    async def test_an_older_or_different_order_does_not_match(self):
        await self.uncertain(place_fail=True)
        when = self.age(200)
        self.record(when - 3600)
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])

    async def test_reconcile_with_nothing_pending_changes_nothing(self):
        await self.synced()
        before = self.ledger()
        status = await self.svc.reconcile(1)
        self.assertEqual(status["ledger"], before)


class AccountEvidenceTests(Base):
    """The live sequence of 2026-10-29: the order filled, the history did not list it.

    Trading 212's own account (free cash and positions) is the ground truth: the
    outcome is read from what changed there, never guessed and never cleared on the
    history's silence alone.
    """

    async def live_state(self):
        """Cash 30, no bot positions, and the owner's own MRNA that the ledger lacks."""
        self.svc.held.clear()
        self.svc.cash = Decimal("30")
        await self.synced()
        self.svc.held["MRNA_US_EQ"] = position(
            "MRNA_US_EQ", "0.0467626", "216.5", "9.1"
        )
        self.svc.history_hides = True

    def account_matches(self, view):
        self.assertTrue(view["ledger_matches_account"], view["mismatch"])
        self.assertEqual(view["mismatch"], [])

    async def test_a_fill_the_history_omits_is_booked_from_the_account(self):
        await self.live_state()
        result = await self.svc.order(1, "buy", "AAPL", "6")
        order = result["order"]
        self.assertEqual(order["fill_source"], "account_evidence")
        self.assertEqual(order["value_source"], "account_cash_change")
        self.assertEqual(order["fee_source"], "estimate")
        self.assertIn("EUR/USD", order["average_price_source"])
        self.assertEqual(order["status"], "filled")
        self.assertIn("history did not list", order["inferred"])
        held = self.svc.held["AAPL_US_EQ"]["quantity"]
        ledger = self.ledger()
        self.assertEqual(Decimal(ledger["positions"]["AAPL"]["units"]), held)
        self.assertEqual(Decimal(order["filled_quantity"]), held)
        # Ledger == reality: cash is the account's cash, the order is not uncertain.
        self.assertAlmostEqual(Decimal(ledger["cash"]), self.svc.cash, places=6)
        self.assertIsNone(ledger["uncertain_order"])
        self.assertTrue(ledger["enabled"])  # a conclusive fill does not pause
        self.assertEqual(ledger["orders"][-1]["fill_source"], "account_evidence")
        self.assertEqual(ledger["orders"][-1]["ticker"], "AAPL_US_EQ")
        self.assertEqual(self.svc.deletes(), [])
        # The fee is split out of the cash change: value + fee is exactly what left.
        spent = Decimal("30") - self.svc.cash
        self.assertAlmostEqual(
            Decimal(order["filled_value_eur"]) + Decimal(order["fee_eur"]),
            spent,
            places=6,
        )
        # (f) The owner's MRNA (not in the ledger, unchanged) is no mismatch.
        view = await self.svc.valuation(1)
        self.account_matches(view)
        self.assertEqual(
            [(r["ticker"], r["excluded"]) for r in view["outside_allocation"]],
            [("MRNA_US_EQ", False)],
        )

    async def test_the_average_price_is_the_eur_value_over_the_quantity_at_the_reference_rate(
        self,
    ):
        await self.live_state()
        order = (await self.svc.order(1, "buy", "AAPL", "6"))["order"]
        expected = (
            Decimal(order["filled_value_eur"]) * FX / Decimal(order["filled_quantity"])
        )
        self.assertAlmostEqual(
            Decimal(order["average_fill_price"]), expected, delta=Decimal("0.001")
        )

    async def test_only_the_cash_moved_stays_uncertain_with_the_evidence_listed(self):
        await self.live_state()
        self.svc.after_place = lambda: self.svc.held.pop("AAPL_US_EQ")
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "AAPL", "6")
        message = str(caught.exception)
        self.assertIn("reconciliation", message)
        self.assertIn("only the cash moved", message)
        self.assertIn("AAPL_US_EQ quantity 0 -> 0", message)
        pending = self.ledger()["uncertain_order"]
        self.assertEqual(pending["order_id"], OID)
        self.assertEqual(self.ledger()["positions"], {})
        self.assertFalse(self.svc.status(1)["enabled"])
        # Reconcile much later still does not guess: the evidence stays mixed.
        pending["time"] = time.time() - 900
        self.set_ledger(uncertain_order=pending)
        with self.assertRaises(ValueError) as again:
            await self.svc.reconcile(1)
        self.assertIn("mixed or ambiguous", str(again.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_only_the_position_moved_stays_uncertain(self):
        await self.live_state()

        def only_position():
            self.svc.cash = Decimal("30")

        self.svc.after_place = only_position
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "AAPL", "6")
        self.assertIn("only the position moved", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_nothing_moved_is_uncertain_until_three_minutes_then_cleared(self):
        await self.live_state()
        self.svc.fill = {"quantity": "0", "price": "200.10"}  # accepted, no fill
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "AAPL", "6")
        self.assertIn("reconciliation", str(caught.exception))
        pending = self.ledger()["uncertain_order"]
        self.assertEqual(pending["order_id"], OID)

        def age(seconds):
            entry = self.ledger()["uncertain_order"]
            entry["time"] = time.time() - seconds
            self.set_ledger(uncertain_order=entry)

        age(30)
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("Wait a minute", str(caught.exception))
        age(t212.NOT_SEEN_AFTER_SEC - 20)
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("No trace", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        age(t212.NOT_SEEN_AFTER_SEC + 5)
        status = await self.svc.reconcile(1)
        self.assertIsNone(status["ledger"]["uncertain_order"])
        self.assertFalse(status["ledger"]["enabled"])
        self.assertEqual(status["ledger"]["positions"], {})
        self.assertEqual(Decimal(status["ledger"]["cash"]), Decimal("30"))

    async def test_a_reconcile_books_a_fill_that_only_the_account_shows(self):
        """The live failure: reconcile used to clear this order and leave ledger != reality."""
        await self.live_state()
        # The order call could not read the account back: the fill is not booked.
        original_cash = self.svc.cash
        with patch.object(
            t212.Trading212Service,
            "_poll_verdict",
            side_effect=ValueError("account unreadable"),
        ):
            with self.assertRaises(ValueError):
                await self.svc.order(1, "buy", "AAPL", "6")
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        self.assertLess(self.svc.cash, original_cash)  # the buy really filled
        entry = self.ledger()["uncertain_order"]
        entry["time"] = time.time() - 90  # past the first reconcile's one minute
        self.set_ledger(uncertain_order=entry)
        status = await self.svc.reconcile(1)
        ledger = status["ledger"]
        self.assertIsNone(ledger["uncertain_order"])
        self.assertFalse(ledger["enabled"])  # a reconcile always leaves it paused
        held = self.svc.held["AAPL_US_EQ"]["quantity"]
        self.assertEqual(Decimal(ledger["positions"]["AAPL"]["units"]), held)
        self.assertAlmostEqual(Decimal(ledger["cash"]), self.svc.cash, places=6)
        self.assertEqual(ledger["orders"][-1]["fill_source"], "account_evidence")
        self.account_matches(await self.svc.valuation(1))

    async def test_a_partial_fill_is_booked_from_the_account_once_it_reads_the_same_twice(
        self,
    ):
        await self.live_state()
        self.svc.fill = {"quantity": "0.01", "price": "200.10"}
        self.svc.stuck = True  # the rest rests; Condor cancels it
        result = await self.svc.order(1, "buy", "AAPL", "6")
        order = result["order"]
        self.assertTrue(order["cancelled_by_condor"])
        self.assertEqual(order["fill_source"], "account_evidence")
        self.assertEqual(order["status"], "partially_filled")
        self.assertEqual(order["filled_quantity"], "0.01")
        self.assertEqual(self.ledger()["positions"]["AAPL"]["units"], "0.01")
        self.assertIsNone(self.ledger()["uncertain_order"])
        self.assertEqual(self.svc.deletes(), [f"/equity/orders/{OID}"])
        self.account_matches(await self.svc.valuation(1))

    async def test_a_partial_move_without_an_order_id_is_never_guessed(self):
        await self.live_state()
        self.svc.no_id = True  # accepted but no id came back
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "AAPL", "6")
        entry = self.ledger()["uncertain_order"]
        self.assertNotIn("order_id", entry)
        # Half of the order's quantity shows up in the account, with proportional cash.
        quantity = Decimal(entry["quantity"])
        half = (quantity / 2).quantize(Decimal("0.0001"))
        price = Decimal("200.10")
        self.svc.held["AAPL_US_EQ"] = position(
            "AAPL_US_EQ", half, price, half * price / FX
        )
        self.svc.cash -= half * price / FX
        entry["time"] = time.time() - 90
        self.set_ledger(uncertain_order=entry)
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("without its order id", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_a_sale_the_history_omits_is_booked_from_the_account_too(self):
        await self.live_state()
        self.svc.held["MSFT_US_EQ"] = position("MSFT_US_EQ", "0.05", "400", "17.5")
        self.svc.cash = Decimal("30")
        self.svc.set_enabled(1, False)
        await self.svc.sync_from_account(1)
        self.svc.set_enabled(1, True)
        self.svc.history_hides = True
        self.svc.fill = {"quantity": None, "price": "399.5"}
        result = await self.svc.order(1, "sell", "MSFT", "0.05")
        order = result["order"]
        self.assertEqual(order["fill_source"], "account_evidence")
        self.assertEqual(order["filled_quantity"], "0.05")
        ledger = self.ledger()
        self.assertEqual(ledger["positions"].get("MSFT"), None)
        self.assertAlmostEqual(Decimal(ledger["cash"]), self.svc.cash, places=6)
        # The sale's fee is added back to the proceeds: value - fee is what arrived.
        arrived = self.svc.cash - Decimal("30")
        self.assertAlmostEqual(
            Decimal(order["filled_value_eur"]) - Decimal(order["fee_eur"]),
            arrived,
            places=6,
        )
        self.account_matches(await self.svc.valuation(1))

    async def test_a_concurrent_manual_buy_of_the_same_ticker_is_not_booked(self):
        await self.live_state()

        def manual_extra():
            row = self.svc.held["AAPL_US_EQ"]
            more = row["quantity"] + Decimal("0.05")
            self.svc.held["AAPL_US_EQ"] = position(
                "AAPL_US_EQ", more, "200.10", row["cost_eur"]
            )
            self.svc.cash -= Decimal("9")

        self.svc.after_place = manual_extra
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "AAPL", "6")
        self.assertIn("more than this order", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        self.assertEqual(self.ledger()["positions"], {})

    async def test_a_cash_change_that_does_not_fit_the_order_is_not_booked(self):
        await self.live_state()

        def deposit_and_buy():
            self.svc.cash += Decimal("3")  # a deposit lands at the same moment

        self.svc.after_place = deposit_and_buy
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "AAPL", "6")
        self.assertIn("does not match the order", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_reserved_cash_changing_means_a_resting_order_and_is_ambiguous(self):
        await self.live_state()

        def reserve():
            self.svc.reserved = Decimal("3")

        self.svc.after_place = reserve
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "AAPL", "6")
        self.assertIn("reserved", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_the_owners_other_holding_moving_does_not_decide_this_order(self):
        """A different ticker changing is not evidence about this ticker."""
        await self.live_state()
        self.svc.after_place = lambda: self.svc.held["MRNA_US_EQ"].update(
            {"quantity": Decimal("0.5"), "available": Decimal("0.5")}
        )
        result = await self.svc.order(1, "buy", "AAPL", "6")
        self.assertEqual(result["order"]["fill_source"], "account_evidence")

    async def test_the_account_is_snapshotted_before_any_order_is_sent(self):
        await self.live_state()
        sequence = []
        original = self.svc.private

        async def spy(user_id, method, path, query=None, body=None):
            sequence.append((method, path))
            return await original(user_id, method, path, query, body)

        self.svc.private = spy
        await self.svc.order(1, "buy", "AAPL", "6")
        post = sequence.index(("POST", "/equity/orders/limit"))
        before = sequence[:post]
        self.assertIn(("GET", "/equity/account/summary"), before)
        self.assertIn(("GET", "/equity/positions"), before)
        # ... and an account that cannot be read means no order at all.
        self.svc.set_enabled(1, False)
        await self.svc.sync_from_account(1)
        self.svc.set_enabled(1, True)
        before_posts = len(self.svc.posts())

        async def broken(*args, **kwargs):
            raise ValueError("unreadable")

        self.svc._snapshot = broken
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "MSFT", "8")
        self.assertIn("no order placed", str(caught.exception))
        self.assertEqual(len(self.svc.posts()), before_posts)
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_a_final_order_object_read_by_id_is_booked_with_the_account_figures(
        self,
    ):
        await self.live_state()

        async def none(*args, **kwargs):
            return None

        self.svc._history_detail = none
        removed = {}

        def lag():
            removed["row"] = self.svc.held.pop("AAPL_US_EQ")

        self.svc.after_place = lag
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "AAPL", "6")
        entry = self.ledger()["uncertain_order"]
        self.svc.held["AAPL_US_EQ"] = removed["row"]  # the positions read caught up
        self.svc.object_final = ("FILLED", removed["row"]["quantity"])
        entry["time"] = time.time() - 90
        self.set_ledger(uncertain_order=entry)
        status = await self.svc.reconcile(1)
        ledger = status["ledger"]
        self.assertIsNone(ledger["uncertain_order"])
        self.assertEqual(self.svc.deletes(), [])  # a filled order is not "cancelled"
        self.assertEqual(
            Decimal(ledger["positions"]["AAPL"]["units"]), removed["row"]["quantity"]
        )
        self.assertEqual(ledger["orders"][-1]["fill_source"], "account_evidence")
        self.assertEqual(ledger["orders"][-1]["status"], "filled")

    async def test_the_history_saying_unfilled_while_the_account_moved_stays_uncertain(
        self,
    ):
        await self.live_state()
        self.svc.history_hides = False
        self.svc.after_place = lambda: self.svc.held.pop("AAPL_US_EQ")
        original = self.svc._history_detail

        async def none(*args, **kwargs):
            return None

        self.svc._history_detail = none
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "AAPL", "6")
        self.svc._history_detail = original
        # Now the history lists the order as cancelled with no fill, yet cash left.
        record = self.svc.records[-1]
        self.svc.records[-1] = record[:4] + (Decimal("0"),) + record[5:]
        self.svc.lists_unfilled = True
        entry = self.ledger()["uncertain_order"]
        entry["time"] = time.time() - 90
        self.set_ledger(uncertain_order=entry)
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn(
            "history shows no fill but the account changed", str(caught.exception)
        )
        self.assertIsNotNone(self.ledger()["uncertain_order"])


class LedgerAgainstAccountTests(Base):
    async def test_a_ledger_that_does_not_match_the_account_cannot_enable_trading(
        self,
    ):
        await self.synced(enabled=False)
        # The live state: the order filled, the ledger never booked it.
        self.svc.cash = Decimal("34.05")
        self.svc.held["AAPL_US_EQ"] = position("AAPL_US_EQ", "0.0188", "352.29", "5.95")
        self.set_ledger(
            orders=[{"time": 1, "side": "buy", "symbol": "AAPL", "status": "filled"}]
        )
        with self.assertRaises(ValueError) as caught:
            await self.svc.enable_checked(1)
        message = str(caught.exception)
        self.assertIn("disagree", message)
        self.assertIn("cash: ledger 40 EUR, account 34.05 EUR", message)
        self.assertIn("AAPL: ledger 0 units, account 0.0188 units", message)
        self.assertIn("Sync", message)
        self.assertFalse(self.ledger()["enabled"])
        # A direct switch is refused too: it needs a fresh, matching check.
        with self.assertRaises(ValueError):
            self.svc.set_enabled(1, True)
        self.assertFalse(self.ledger()["enabled"])
        status = self.svc.status(1)
        self.assertFalse(status["account_check"]["ledger_matches_account"])
        # After a sync (the "Pause trading to sync" step) the ledger is the account.
        await self.svc.sync_from_account(1)
        self.assertEqual(self.ledger()["positions"]["AAPL"]["units"], "0.0188")
        status = await self.svc.enable_checked(1)
        self.assertTrue(status["enabled"])
        self.assertTrue(status["account_check"]["ledger_matches_account"])

    async def test_a_position_difference_for_a_held_ticker_blocks_enabling(self):
        await self.synced(enabled=False)
        self.svc.held["MSFT_US_EQ"] = position("MSFT_US_EQ", "0.03", "400", "10")
        with self.assertRaises(ValueError) as caught:
            await self.svc.enable_checked(1)
        self.assertIn(
            "MSFT: ledger 0.05 units, account 0.03 units", str(caught.exception)
        )

    async def test_valuation_reports_the_check_and_lists_outside_holdings_not_as_errors(
        self,
    ):
        await self.synced()
        self.svc.held["MRNA_US_EQ"] = position(
            "MRNA_US_EQ", "0.0467626", "216.5", "9.1"
        )
        view = await self.svc.valuation(1)
        self.assertTrue(view["ledger_matches_account"])
        self.assertEqual(view["mismatch"], [])
        self.assertEqual(
            [r["ticker"] for r in view["outside_allocation"]],
            ["MRNA_US_EQ", "SAP_DE_EQ"],
        )
        self.svc.cash = Decimal("10")
        view = await self.svc.valuation(1)
        self.assertFalse(view["ledger_matches_account"])
        self.assertEqual([m["kind"] for m in view["mismatch"]], ["cash"])
        self.assertEqual(view["mismatch"][0]["difference"], "30")

    async def test_cash_within_five_cents_is_not_a_mismatch(self):
        await self.synced()
        self.svc.cash = Decimal("40.04")
        self.assertTrue((await self.svc.valuation(1))["ledger_matches_account"])
        self.svc.cash = Decimal("40.06")
        self.assertFalse((await self.svc.valuation(1))["ledger_matches_account"])

    async def test_enabling_needs_the_check_even_right_after_a_restart(self):
        await self.synced(enabled=False)
        self.svc._verified.clear()  # a new process knows nothing of an earlier check
        with self.assertRaises(ValueError) as caught:
            self.svc.set_enabled(1, True)
        self.assertIn("checked against the Trading 212 account", str(caught.exception))
        status = await self.svc.enable_checked(1)
        self.assertTrue(status["enabled"])


class ExclusionTests(Base):
    MRNA = instrument("MRNA_US_EQ", "MRNA", isin="US60770K1079")

    def owner_holds_mrna(self):
        self.svc.instruments_raw = INSTRUMENTS + [self.MRNA]
        self.svc.held["MRNA_US_EQ"] = position(
            "MRNA_US_EQ", "0.0467626", "216.5", "9.1"
        )

    async def test_by_default_sync_adopts_a_position_the_bot_did_not_open(self):
        self.owner_holds_mrna()
        status = await self.svc.sync_from_account(1)
        self.assertIn("MRNA", status["ledger"]["positions"])
        self.assertEqual(status["excluded_tickers"], [])

    async def test_an_excluded_ticker_is_never_adopted_and_stays_across_syncs(self):
        self.owner_holds_mrna()
        status = await self.svc.set_excluded(1, ["mrna_us_eq", "MRNA", " tsla "])
        self.assertEqual(status["excluded_tickers"], ["MRNA", "TSLA"])
        status = await self.svc.sync_from_account(1)
        self.assertNotIn("MRNA", status["ledger"]["positions"])
        self.assertIn("MRNA_US_EQ", status["skipped"])
        self.assertTrue(any("exclusion list" in n for n in status["notes"]))
        self.assertEqual(status["ledger"]["excluded_tickers"], ["MRNA", "TSLA"])
        # The owner's holding is outside the allocation, listed and flagged, no error.
        self.svc.set_enabled(1, True)
        view = await self.svc.valuation(1)
        self.assertTrue(view["ledger_matches_account"])
        mrna = [r for r in view["outside_allocation"] if r["symbol"] == "MRNA"]
        self.assertEqual(mrna[0]["excluded"], True)

    async def test_the_order_path_refuses_an_excluded_ticker_definitely(self):
        self.owner_holds_mrna()
        await self.svc.set_excluded(1, ["MRNA"])
        await self.synced()
        for call in (
            self.svc.order(1, "buy", "MRNA", "8"),
            self.svc.order(1, "sell", "MRNA", "0.04"),
            self.svc.preview(1, "buy", "MRNA", "8"),
        ):
            with self.assertRaises(ValueError) as caught:
                await call
            self.assertIn("exclusion list", str(caught.exception))
            self.assertIn("no order placed", str(caught.exception))
        self.assertEqual(self.svc.posts(), [])
        self.assertIsNone(self.ledger()["uncertain_order"])
        self.assertTrue(self.ledger()["enabled"])  # a refusal does not pause trading

    async def test_changing_the_list_pauses_trading_and_a_held_symbol_must_be_synced_out(
        self,
    ):
        self.owner_holds_mrna()
        await self.synced()
        self.assertIn("MRNA", self.ledger()["positions"])
        status = await self.svc.set_excluded(1, ["MRNA"])
        self.assertFalse(status["ledger"]["enabled"])
        with self.assertRaises(ValueError) as caught:
            await self.svc.enable_checked(1)
        self.assertIn(
            "MRNA is on the exclusion list but still in the bot ledger",
            str(caught.exception),
        )
        await self.svc.sync_from_account(1)
        self.assertNotIn("MRNA", self.ledger()["positions"])
        self.assertTrue((await self.svc.enable_checked(1))["enabled"])

    async def test_bad_exclusions_are_refused(self):
        for bad in ("nope", ["a b"], [5], ["x" * 20], ["A"] * 51):
            with self.assertRaises(ValueError):
                await self.svc.set_excluded(1, bad)
        status = await self.svc.set_excluded(1, [])
        self.assertEqual(status["excluded_tickers"], [])


class WholeAccountTests(Base):
    """The dashboard/history valuation counts ALL account money, never the bot's slice."""

    MRNA = instrument("MRNA_US_EQ", "MRNA", isin="US60770K1079")

    def positions_eur(self):
        return sum(
            (p["quantity"] * p["price_usd"] / FX for p in self.svc.held.values()),
            Decimal(0),
        )

    def eur_row(self, view):
        return {b["token"]: b for b in view["balances"]}["EUR"]

    async def test_cash_reserved_for_a_pending_order_is_part_of_the_account(self):
        self.svc.reserved = Decimal("9.11")
        view = await self.svc.portfolio(1)
        row = self.eur_row(view)
        self.assertAlmostEqual(row["total"], 49.11, places=6)
        self.assertAlmostEqual(row["usd_value"], 49.11 * 1.10, places=6)
        # Only the free cash can be traded.
        self.assertAlmostEqual(row["available"], 40.0, places=6)
        self.assertEqual(view["composition_eur"]["reserved_for_orders"], "9.11")
        self.assertIn("reserved", view["note"])

    async def test_a_pending_order_does_not_make_value_vanish_and_reappear(self):
        # Before the fill: 9.11 EUR reserved. After it: the cash is a position.
        self.svc.reserved = Decimal("9.11")
        before = (await self.svc.portfolio(1))["total_usd"]
        self.svc.reserved = Decimal("0")
        self.svc.held["MRNA_US_EQ"] = position(
            "MRNA_US_EQ",
            "0.0467626",
            str(Decimal("9.11") * FX / Decimal("0.0467626")),
            "9.11",
        )
        after = (await self.svc.portfolio(1))["total_usd"]
        self.assertAlmostEqual(before, after, places=4)

    async def test_cash_the_summary_does_not_itemise_is_counted_from_the_account_total(
        self,
    ):
        # Seen live: the pending MRNA order's 9.11 EUR was in no cash field. The
        # account's own total still held it.
        self.svc.total_value = self.svc.cash + self.positions_eur() + Decimal("9.11")
        view = await self.svc.portfolio(1)
        self.assertAlmostEqual(self.eur_row(view)["total"], 49.11, places=4)
        self.assertEqual(view["composition_eur"]["not_itemised"], "9.11")
        self.assertIn("not itemise", view["note"])

    async def test_a_total_that_is_only_the_investments_or_rounding_adds_nothing(self):
        for total in (
            self.positions_eur(),  # the spec's wording: the investments' value
            self.svc.cash + self.positions_eur() + Decimal("0.04"),  # rounding
            Decimal("0"),  # not reported
        ):
            self.svc.total_value = total
            view = await self.svc.portfolio(1)
            self.assertAlmostEqual(self.eur_row(view)["total"], 40.0, places=6)
            self.assertEqual(view["composition_eur"]["not_itemised"], "0")
            self.svc._cache.clear()

    async def test_the_owners_excluded_holdings_stay_in_the_total_and_are_reported(
        self,
    ):
        self.svc.instruments_raw = INSTRUMENTS + [self.MRNA]
        self.svc.held["MRNA_US_EQ"] = position(
            "MRNA_US_EQ", "0.0467626", "216.5", "9.1"
        )
        await self.svc.set_excluded(1, ["MRNA"])
        view = await self.svc.portfolio(1)
        by_token = {b["token"]: b for b in view["balances"]}
        mrna_usd = 0.0467626 * 216.5
        self.assertAlmostEqual(by_token["MRNA"]["usd_value"], mrna_usd, places=6)
        self.assertAlmostEqual(view["excluded_value_usd"], mrna_usd, places=6)
        self.assertEqual(view["excluded_tokens"], ["MRNA"])
        self.assertAlmostEqual(
            view["total_usd"], 40 * 1.10 + 20 + 100 + mrna_usd, places=6
        )

    async def test_without_an_exclusion_list_nothing_is_excluded(self):
        view = await self.svc.portfolio(1)
        self.assertEqual(view["excluded_value_usd"], 0.0)
        self.assertEqual(view["excluded_tokens"], [])


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
            calls = 0

            async def private(self, *args, **kwargs):
                self.calls += 1
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

        service = Service()
        out = await fetchers.fetch("trading212", service, 1, NOW - 86400, NOW)
        self.assertTrue(out.problems)
        self.assertEqual(service.calls, 1)

    async def test_the_time_filter_carries_milliseconds_and_the_page_is_the_documented_max(
        self,
    ):
        # The first live call sent "...:59Z" and got HTTP 400 invalid-request; the
        # published examples write 2026-01-12T11:12:31.337Z.
        class Service:
            def __init__(self):
                self.calls = []

            async def private(self, user_id, method, path, query=None, body=None):
                self.calls.append((method, path, query))
                return {"items": [], "nextPagePath": None}

        service = Service()
        await fetchers.fetch("trading212", service, 1, 1_793_113_200.25, 1_793_200_000)
        method, path, query = service.calls[0]
        self.assertEqual((method, path), ("GET", "/equity/history/transactions"))
        self.assertEqual(query, {"limit": 50, "time": "2026-10-27T15:00:00.250Z"})

    async def test_a_400_on_the_time_filter_is_retried_once_with_only_the_limit(self):
        since = NOW - 5 * 86400

        class Service:
            def __init__(self):
                self.calls = []

            async def private(self, user_id, method, path, query=None, body=None):
                self.calls.append(query)
                if query and "time" in query:
                    raise t212.Trading212Error(400, "/api-errors/invalid-request")
                return {
                    "items": [
                        {
                            "type": "DEPOSIT",
                            "amount": 10,
                            "currency": "EUR",
                            "dateTime": "2026-10-20T10:00:00Z",  # older than since
                            "reference": "old",
                        },
                        {
                            "type": "DEPOSIT",
                            "amount": 9.11,
                            "currency": "EUR",
                            "dateTime": "2026-10-26T10:00:00Z",
                            "reference": "new",
                        },
                    ],
                }

        service = Service()
        out = await fetchers.fetch("trading212", service, 1, since, NOW)
        self.assertEqual(
            service.calls,
            [{"limit": 50, "time": fetchers._trading212_time(since)}, {"limit": 50}],
        )
        self.assertEqual([f.external_id for f in out.flows], ["transaction:new"])
        self.assertEqual(out.problems, [])

    async def test_a_400_that_persists_stays_unavailable(self):
        class Service:
            def __init__(self):
                self.calls = 0

            async def private(self, *args, **kwargs):
                self.calls += 1
                raise t212.Trading212Error(400, "/api-errors/invalid-request")

        service = Service()
        out = await fetchers.fetch("trading212", service, 1, NOW - 86400, NOW)
        self.assertEqual(service.calls, 2)
        self.assertEqual(out.flows, [])
        self.assertEqual(len(out.problems), 1)
        self.assertIn("HTTP 400", out.problems[0])

    async def test_a_later_page_failing_keeps_the_rows_already_read_and_says_so(self):
        class Service:
            def __init__(self):
                self.calls = 0

            async def private(self, *args, **kwargs):
                self.calls += 1
                if self.calls > 1:
                    raise t212.Trading212Error(404, "not found")
                return {
                    "items": [
                        {
                            "type": "DEPOSIT",
                            "amount": 5,
                            "currency": "EUR",
                            "dateTime": "2026-10-28T10:00:00Z",
                            "reference": "a",
                        }
                    ],
                    "nextPagePath": "/api/v0/equity/history/transactions?cursor=x",
                }

        out = await fetchers.fetch("trading212", Service(), 1, NOW - 5 * 86400, NOW)
        self.assertEqual([f.external_id for f in out.flows], ["transaction:a"])
        self.assertTrue(out.problems[0].startswith("transactions:"))

    async def test_a_query_only_next_page_path_is_followed_on_this_endpoint(self):
        class Service:
            def __init__(self):
                self.calls = []

            async def private(self, user_id, method, path, query=None, body=None):
                self.calls.append(path)
                if len(self.calls) == 1:
                    return {
                        "items": [
                            {
                                "type": "DEPOSIT",
                                "amount": 5,
                                "currency": "EUR",
                                "dateTime": "2026-10-28T10:00:00Z",
                                "reference": "a",
                            }
                        ],
                        "nextPagePath": "limit=50&cursor=abc&time=2026-10-20T00:00:00.000Z",
                    }
                return {"items": [], "nextPagePath": None}

        service = Service()
        out = await fetchers.fetch("trading212", service, 1, NOW - 5 * 86400, NOW)
        self.assertEqual(
            service.calls[1],
            "/api/v0/equity/history/transactions?limit=50&cursor=abc&time=2026-10-20T00:00:00.000Z",
        )
        self.assertEqual(out.problems, [])

    async def test_a_full_page_with_no_next_page_is_not_presented_as_complete(self):
        row = {
            "type": "DEPOSIT",
            "amount": 1,
            "currency": "EUR",
            "dateTime": "2026-10-28T10:00:00Z",
        }

        class Service:
            async def private(self, *args, **kwargs):
                return {
                    "items": [dict(row, reference=f"r{i}") for i in range(50)],
                    "nextPagePath": None,
                }

        out = await fetchers.fetch("trading212", Service(), 1, NOW - 5 * 86400, NOW)
        self.assertEqual(len(out.flows), 50)
        self.assertTrue(any("no nextPagePath" in p for p in out.problems))

    async def test_only_deposits_and_withdrawals_count_whatever_the_documented_type(
        self,
    ):
        types = [
            "DEPOSIT",
            "WITHDRAW",
            "TRANSFER",
            "FEE",
            "INTEREST_ON_FREE_CASH",
            "LENDING_INTEREST",
        ]

        class Service:
            async def private(self, *args, **kwargs):
                return {
                    "items": [
                        {
                            "type": kind,
                            "amount": 3 + i,
                            "currency": "EUR",
                            "dateTime": "2026-10-28T10:00:00Z",
                            "reference": f"r{i}",
                        }
                        for i, kind in enumerate(types)
                    ],
                    "nextPagePath": None,
                }

        out = await fetchers.fetch("trading212", Service(), 1, NOW - 86400, NOW)
        self.assertEqual(
            [(f.external_id, f.amount) for f in out.flows],
            [("transaction:r0", Decimal(3)), ("transaction:r1", Decimal(-4))],
        )


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
