"""Revolut X service: signing, keys, sizing, guards, fills, fees and reconciliation. No network."""

import base64
import json
import os
import stat
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from condor import crypto_com_scorecard as sc
from condor import revolut_x as rx

API_KEY = "Ab1" * 21 + "Z"  # 64 letters and digits
assert len(API_KEY) == 64
NOW = 1_793_220_000.0  # seconds
OID = "7a52e92e-8639-4fe1-abaa-68d3a2d5234b"

PAIRS = {
    "BTC/EUR": {
        "base": "BTC",
        "quote": "EUR",
        "base_step": "0.00000001",
        "quote_step": "0.01",
        "min_order_size": "0.00000001",
        "max_order_size": "200",
        "min_order_size_quote": "0.1",
        "max_order_size_quote": "1000000",
        "status": "active",
        "slippage": 5,
    },
    "SUI/EUR": {
        "base": "SUI",
        "quote": "EUR",
        "base_step": "0.00001",
        "quote_step": "0.0001",
        "min_order_size": "0.00001",
        "max_order_size": "2000000",
        "min_order_size_quote": "0.1",
        "max_order_size_quote": "1000000",
        "status": "active",
        "slippage": 5,
    },
    "ETH/EUR": {
        "base": "ETH",
        "quote": "EUR",
        "base_step": "0.000001",
        "quote_step": "0.01",
        "min_order_size": "0.000001",
        "max_order_size": "5000",
        "min_order_size_quote": "0.1",
        "max_order_size_quote": "1000000",
        "status": "active",
        "slippage": 5,
    },
    "USDC/EUR": {
        "base": "USDC",
        "quote": "EUR",
        "base_step": "0.01",
        "quote_step": "0.0001",
        "min_order_size": "0.01",
        "max_order_size": "1000000",
        "min_order_size_quote": "0.1",
        "max_order_size_quote": "1000000",
        "status": "active",
        "slippage": 5,
    },
    "OLD/EUR": {"base": "OLD", "quote": "EUR", "status": "suspended"},
    "BTC/USD": {"base": "BTC", "quote": "USD", "status": "active"},
}


def ticker(symbol, bid, ask, last, quote_volume, change="0", region="EEA"):
    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "mid": str((Decimal(bid) + Decimal(ask)) / 2),
        "index_price": last,
        "last_price": last,
        "low_24h": str(Decimal(last) * Decimal("0.98")),
        "high_24h": str(Decimal(last) * Decimal("1.02")),
        "price_change_24h": change,
        "volume_24h": "1000",
        "quote_volume_24h": quote_volume,
        "region": region,
    }


def tickers():
    return {
        "data": [
            ticker("BTC/EUR", "72643.23", "72650.36", "72649.71", "9442733", "112.07"),
            ticker("BTC/EUR", "1", "2", "1", "1", "0", region="UK"),
            ticker("ETH/EUR", "2108.22", "2108.54", "2107.43", "3695683", "-21.07"),
            ticker("SUI/EUR", "3.0", "3.003", "3.001", "500000", "0.03"),
            ticker("USDC/EUR", "0.8950", "0.8956", "0.8956", "382478"),
            ticker("BTC/USD", "84000", "84010", "84005", "9999999999"),
        ],
        "metadata": {"timestamp": 1},
    }


def level(price, size):
    return {"price": price, "quantity": size, "count": 1}


def books():
    return {
        "BTC": {
            "asks": [level("72650.36", "0.5"), level("72700.00", "1")],
            "bids": [level("72643.23", "0.5"), level("72600.00", "1")],
        },
        "SUI": {
            "asks": [level("3.003", "100"), level("3.01", "1000")],
            "bids": [level("3.0", "100"), level("2.99", "1000")],
        },
        "ETH": {
            "asks": [level("2108.54", "10")],
            "bids": [level("2108.22", "10")],
        },
    }


def info(side, symbol, qty, filled, avg, fee="0", fee_ccy="EUR", status="filled", **x):
    """An order object as the OpenAPI spec describes it."""
    qty, filled, avg = Decimal(qty), Decimal(filled), Decimal(avg)
    return {
        "id": OID,
        "client_order_id": x.pop("cid", None),
        "symbol": f"{symbol}/EUR",
        "side": side,
        "type": "limit",
        "quantity": str(qty),
        "filled_quantity": str(filled),
        "leaves_quantity": str(qty - filled),
        "amount": str(qty * avg),
        "filled_amount": str(filled * avg),
        "price": str(avg),
        "average_fill_price": str(avg if filled else 0),
        "total_fee": fee,
        "fee_currency": fee_ccy,
        "status": status,
        "time_in_force": "ioc",
        "execution_instructions": ["allow_taker"],
        "created_date": 1_793_219_990_000,
        "updated_date": 1_793_219_991_000,
        **x,
    }


class FakeService(rx.RevolutXService):
    """Revolut X, with the public and private reads scripted. No network."""

    def __init__(self):
        super().__init__()
        self.clock = lambda: NOW
        self.sent = []
        self.public_sent = []
        self.pairs_raw = PAIRS
        self.tickers_raw = tickers()
        self.books = books()
        self.balances = [
            {"currency": "EUR", "available": "40", "reserved": "0", "total": "40"},
            {"currency": "SUI", "available": "5", "reserved": "0", "total": "5"},
            {"currency": "XYZ", "available": "1", "reserved": "0", "total": "1"},
            {"currency": "USDC", "available": "0", "reserved": "0", "total": "0"},
        ]
        self.reject_place = None  # RevolutXError args
        self.fail_place = False
        self.no_id = False
        self.record = None  # the order object GET /orders/{id} returns
        self.read_error = None  # RevolutXError args for the read-back
        self.trades = None  # the fills; None builds one trade from the record
        self.fills_error = False
        self.active = []
        self.historical = []
        self.historical_pages = None  # [rows, rows] to test pagination
        self.balances_after = None  # what GET /balances shows once an order was sent
        self.balances_after_error = False

    def cid(self):
        for method, path, query, body in reversed(self.sent):
            if method == "POST":
                return body["client_order_id"]
        return None

    async def public(self, path, params=None):
        self.public_sent.append((path, params))
        if path.endswith("/configuration/pairs"):
            return self.pairs_raw
        if path.endswith("/public/tickers"):
            return self.tickers_raw
        if "/order-book/" in path:
            symbol = path.rsplit("/", 1)[1].split("-")[0]
            book = self.books[symbol]
            limit = (params or {}).get("limit", 100)
            return {
                "data": {k: v[:limit] for k, v in book.items()},
                "metadata": {"region": "EEA", "timestamp": 1},
            }
        if "/public/candles/" in path:
            return {
                "data": [
                    {
                        "start": 1_793_000_000_000 + i * 900_000,
                        "open": "1",
                        "high": "2",
                        "low": "0.5",
                        "close": "1.5",
                        "volume": "10",
                    }
                    for i in reversed(range(5))
                ],
                "metadata": {"region": "EEA", "timestamp": 1},
            }
        raise AssertionError(path)

    async def private(self, user_id, method, path, query=None, body=None):
        self.credentials(user_id)
        self.sent.append((method, path, query, body))
        if path == "/1.0/balances":
            if any(m == "POST" for m, *_ in self.sent):
                if self.balances_after_error:
                    raise rx.RevolutXError(500, "boom")
                if self.balances_after is not None:
                    return self.balances_after
            return self.balances
        if path == "/1.0/orders" and method == "POST":
            if self.reject_place:
                raise rx.RevolutXError(*self.reject_place)
            if self.fail_place:
                raise ValueError("Revolut X unavailable")
            if self.no_id:
                return {"data": {}}
            return {
                "data": {
                    "venue_order_id": OID,
                    "client_order_id": body["client_order_id"],
                    "state": "new",
                }
            }
        if path == f"/1.0/orders/{OID}":
            if self.read_error:
                raise rx.RevolutXError(*self.read_error)
            if self.record is None:
                raise rx.RevolutXError(404, "Order not found")
            return {"data": {**self.record, "client_order_id": self.cid()}}
        if path == f"/1.0/orders/fills/{OID}":
            if self.fills_error:
                raise rx.RevolutXError(500, "boom")
            if self.trades is not None:
                return {"data": self.trades}
            r = self.record or {}
            if Decimal(r.get("filled_quantity", "0")) == 0:
                return {"data": []}
            return {
                "data": [
                    {
                        "p": r["average_fill_price"],
                        "q": r["filled_quantity"],
                        "oid": OID,
                        "s": r["side"],
                        "im": False,
                    }
                ]
            }
        if path == "/1.0/orders/active":
            return {"data": self.active, "metadata": {"timestamp": 1}}
        if path == "/1.0/orders/historical":
            if self.historical_pages:
                page = (query or {}).get("cursor", "0")
                index = int(page)
                meta = {"timestamp": 1}
                if index + 1 < len(self.historical_pages):
                    meta["next_cursor"] = str(index + 1)
                return {"data": self.historical_pages[index], "metadata": meta}
            return {"data": self.historical, "metadata": {"timestamp": 1}}
        raise AssertionError((method, path))


class RevolutXTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()
        self.polls = patch.multiple(rx, ORDER_POLLS=3, ORDER_POLL_WAIT_SEC=0)
        self.polls.start()
        self.svc = FakeService()
        self.svc.generate_signing_key(1)
        self.svc.save_api_key(1, API_KEY)

    def tearDown(self):
        self.polls.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def synced(self, enabled=True):
        await self.svc.sync_from_account(1)
        if enabled:
            self.svc.set_enabled(1, True)

    def ledger(self):
        return rx._read_ledger(1)

    def set_ledger(self, **fields):
        ledger = self.ledger()
        ledger.update(fields)
        rx._write(rx._root(1) / "ledger.json", ledger)

    def posts(self):
        return [body for m, p, q, body in self.svc.sent if m == "POST"]

    def private_pem(self):
        return rx.signing_key_path(1).read_text()

    # ── signing ──

    def test_ed25519_matches_the_rfc_8032_vectors(self):
        """The signer is the library's Ed25519: pinned to the published test vectors."""
        vectors = (
            (
                "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
                b"",
                "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
            ),
            (
                "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
                bytes.fromhex("72"),
                "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
                "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
            ),
        )
        for seed, message, signature in vectors:
            key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
            self.assertEqual(
                rx.sign(key, message),
                base64.b64encode(bytes.fromhex(signature)).decode(),
            )

    def test_signature_is_the_formula_computed_independently(self):
        seed = bytes(range(32))
        key = Ed25519PrivateKey.from_private_bytes(seed)
        # Assembled here from the spec's rule, not through the module's helper.
        message = (
            b"1793220000123"
            + b"POST"
            + b"/api/1.0/orders"
            + b""
            + b'{"client_order_id":"c","symbol":"BTC-EUR"}'
        )
        expected = base64.b64encode(
            Ed25519PrivateKey.from_private_bytes(seed).sign(message)
        ).decode()
        got = rx.sign(
            key,
            rx.signing_message(
                1793220000123,
                "post",
                "/api/1.0/orders",
                "",
                '{"client_order_id":"c","symbol":"BTC-EUR"}',
            ),
        )
        self.assertEqual(got, expected)
        # Ed25519 is deterministic: the same inputs give the same signature.
        self.assertEqual(
            got,
            rx.sign(
                key,
                rx.signing_message(
                    1793220000123,
                    "POST",
                    "/api/1.0/orders",
                    "",
                    '{"client_order_id":"c","symbol":"BTC-EUR"}',
                ),
            ),
        )
        key.public_key().verify(base64.b64decode(got), message)

    def test_the_signed_string_has_no_separators_and_no_question_mark(self):
        message = rx.signing_message(
            "1700000000000", "get", "/api/1.0/orders/active", "limit=5&cursor=a", ""
        )
        self.assertEqual(
            message, b"1700000000000GET/api/1.0/orders/activelimit=5&cursor=a"
        )

    def test_request_headers_carry_the_key_timestamp_and_a_signature_that_verifies(
        self,
    ):
        key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        body = {
            "client_order_id": "c",
            "side": "buy",
            "order_configuration": {"limit": {"price": "1"}},
        }
        url, headers, payload = rx.build_request(
            API_KEY, key, "POST", "/1.0/orders", None, body, 1793220000123
        )
        self.assertEqual(url, "https://revx.revolut.com/api/1.0/orders")
        self.assertEqual(headers["X-Revx-API-Key"], API_KEY)
        self.assertEqual(headers["X-Revx-Timestamp"], "1793220000123")
        self.assertEqual(headers["Content-Type"], "application/json")
        # minified: no spaces after separators, and the bytes sent are the bytes signed
        self.assertEqual(
            payload,
            b'{"client_order_id":"c","side":"buy","order_configuration":{"limit":{"price":"1"}}}',
        )
        signed = b"1793220000123POST/api/1.0/orders" + payload
        key.public_key().verify(base64.b64decode(headers["X-Revx-Signature"]), signed)

    def test_a_get_signs_the_exact_query_it_sends_and_has_no_body(self):
        key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        url, headers, payload = rx.build_request(
            API_KEY,
            key,
            "get",
            "/1.0/orders/historical",
            {"start_date": 1000, "end_date": 2000, "limit": 100},
            None,
            1793220000999,
        )
        self.assertIsNone(payload)
        self.assertNotIn("Content-Type", headers)
        parts = urlsplit(url)
        self.assertEqual(parts.path, "/api/1.0/orders/historical")
        self.assertEqual(
            dict(parse_qsl(parts.query)),
            {"start_date": "1000", "end_date": "2000", "limit": "100"},
        )
        signed = ("1793220000999GET" + parts.path + parts.query).encode()
        key.public_key().verify(base64.b64decode(headers["X-Revx-Signature"]), signed)

    def test_only_known_methods_and_plain_paths_can_be_signed(self):
        key = Ed25519PrivateKey.generate()
        for method, path in (
            ("PATCH", "/1.0/orders"),
            ("GET", "1.0/orders"),
            ("GET", "/1.0/../orders"),
            ("GET", "/1.0/orders?x=1"),
            ("GET", "/"),
        ):
            with self.assertRaises(ValueError):
                rx.build_request(API_KEY, key, method, path, None, None, 1)

    # ── keys ──

    def test_generating_a_key_writes_a_private_pem_with_mode_0600_and_returns_only_the_public_key(
        self,
    ):
        status = self.svc.generate_signing_key(2)
        path = rx.signing_key_path(2)
        self.assertTrue(path.is_file())
        pem = path.read_text()
        self.assertTrue(pem.startswith("-----BEGIN PRIVATE KEY-----"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertTrue(status["public_key"].startswith("-----BEGIN PUBLIC KEY-----"))
        self.assertTrue(status["has_signing_key"])
        self.assertFalse(status["has_api_key"])
        self.assertFalse(status["connected"])
        # the returned public key is the stored private key's public half
        private = serialization.load_pem_private_key(pem.encode(), password=None)
        public = serialization.load_pem_public_key(status["public_key"].encode())
        self.assertIsInstance(public, Ed25519PublicKey)
        signature = private.sign(b"x")
        public.verify(signature, b"x")
        # nothing that is private is in the answer
        body = "".join(pem.splitlines()[1:-1])
        text = json.dumps(status)
        self.assertNotIn(body, text)
        self.assertNotIn("PRIVATE", text)

    def test_status_and_errors_never_contain_the_private_key_or_the_api_key(self):
        pem = self.private_pem()
        body = "".join(pem.splitlines()[1:-1])
        seed = serialization.load_pem_private_key(
            pem.encode(), password=None
        ).private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        outputs = [
            json.dumps(self.svc.status(1)),
            repr(self.svc.status(1)),
            json.dumps(self.svc.generate_signing_key(1)),
        ]
        self.svc.save_api_key(1, API_KEY)
        outputs += [
            json.dumps(self.svc.status(1)),
            json.dumps(self.svc.set_enabled(1, False)),
        ]
        for bad in ("short", "x" * 64 + "!", " " * 64, "a b", ""):
            with self.assertRaises(ValueError) as caught:
                self.svc.save_api_key(1, bad)
            outputs.append(str(caught.exception))
        # an error body that echoes the API key is never repeated
        outputs.append(str(rx.RevolutXError(401, "bad key " + API_KEY, (API_KEY,))))
        outputs.append(str(rx.RevolutXError(401, "x" * 500 + "<script>")))
        new_body = "".join(self.private_pem().splitlines()[1:-1])
        for text in outputs:
            for secret in (
                body,
                new_body,
                API_KEY,
                base64.b64encode(seed).decode(),
                seed.hex(),
            ):
                self.assertNotIn(secret, text)
        self.assertLess(len(str(rx.RevolutXError(401, "x" * 500))), 200)
        self.assertNotIn("<", str(rx.RevolutXError(401, "<script>")))

    def test_saving_an_api_key_needs_a_signing_key_pauses_trading_and_is_never_echoed(
        self,
    ):
        with self.assertRaises(ValueError) as caught:
            self.svc.save_api_key(5, API_KEY)
        self.assertIn("signing key", str(caught.exception))
        self.set_ledger(synced=True, enabled=True)
        self.assertTrue(self.ledger()["enabled"])
        status = self.svc.save_api_key(1, API_KEY)
        self.assertFalse(self.ledger()["enabled"])
        self.assertTrue(status["connected"] and status["has_api_key"])
        self.assertNotIn(API_KEY, json.dumps(status))
        if os.name != "nt":
            self.assertEqual(
                stat.S_IMODE(os.stat(rx.credentials_path(1)).st_mode), 0o600
            )
        self.assertEqual(
            json.loads(rx.credentials_path(1).read_text()), {"api_key": API_KEY}
        )

    def test_connected_needs_both_the_key_pair_and_the_api_key(self):
        svc = FakeService()
        self.assertFalse(svc.status(7)["connected"])
        svc.generate_signing_key(7)
        self.assertFalse(svc.status(7)["connected"])
        svc.save_api_key(7, API_KEY)
        self.assertTrue(svc.status(7)["connected"])
        # a key pair alone, or an API key alone, is not a connection
        rx.credentials_path(7).unlink()
        self.assertFalse(svc.status(7)["connected"])
        rx._write(rx.credentials_path(7), {"api_key": API_KEY})
        rx.signing_key_path(7).unlink()
        self.assertFalse(svc.status(7)["connected"])
        with self.assertRaises(ValueError):
            svc.credentials(7)

    def test_regenerating_the_key_pauses_trading_and_drops_the_api_key_of_the_old_key(
        self,
    ):
        self.set_ledger(synced=True, enabled=True)
        before = self.svc.status(1)["public_key"]
        status = self.svc.generate_signing_key(1)
        self.assertNotEqual(status["public_key"], before)
        self.assertFalse(status["connected"])
        self.assertFalse(status["has_api_key"])
        self.assertFalse(self.ledger()["enabled"])

    def test_the_key_cannot_be_replaced_while_an_order_is_uncertain(self):
        self.set_ledger(uncertain_order={"client_oid": "x", "time": NOW})
        with self.assertRaises(ValueError) as caught:
            self.svc.generate_signing_key(1)
        self.assertIn("Reconcile", str(caught.exception))
        self.assertTrue(self.svc.status(1)["connected"])

    def test_disconnect_removes_both_keys_and_pauses(self):
        self.set_ledger(synced=True, enabled=True)
        status = self.svc.disconnect(1)
        self.assertFalse(rx.signing_key_path(1).exists())
        self.assertFalse(rx.credentials_path(1).exists())
        self.assertFalse(status["connected"])
        self.assertIsNone(status["public_key"])
        self.assertFalse(self.ledger()["enabled"])

    async def test_a_stored_key_that_is_not_ed25519_is_not_used(self):
        from cryptography.hazmat.primitives.asymmetric import ec

        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        rx.signing_key_path(1).write_bytes(pem)
        self.assertFalse(self.svc.status(1)["connected"])

    # ── symbols ──

    def test_symbols_normalise_in_both_spellings_and_only_euro_pairs_pass(self):
        for value in ("btc", "BTC-EUR", "btc/eur", "BTC/EUR"):
            self.assertEqual(rx.symbol_of(value), "BTC")
        self.assertEqual(rx.pair_of("BTC"), "BTC-EUR")
        self.assertEqual(rx.symbol_of("1inch-eur"), "1INCH")
        for bad in ("BTC-USD", "BTC/GBP", "", "BT C", "../x", "BTC-EUR-X", None, 5):
            with self.assertRaises(ValueError):
                rx.symbol_of(bad)

    # ── error classification ──

    def test_every_4xx_is_a_definite_refusal_including_429(self):
        for status in (400, 401, 403, 404, 409, 429):
            self.assertTrue(rx.RevolutXError(status, "no").definite, status)
        self.assertTrue(rx.RevolutXError(0, "local rate limit").definite)

    def test_server_errors_are_uncertain(self):
        for status in (500, 502, 503, 504):
            self.assertFalse(rx.RevolutXError(status, "boom").definite, status)
        self.assertFalse(rx.RevolutXError(302, "").definite)

    def test_result_parsing_follows_the_status(self):
        result = rx.RevolutXService._result
        self.assertEqual(result(200, {"data": 1}), {"data": 1})
        with self.assertRaises(rx.RevolutXError) as caught:
            result(429, {"message": "Rate Limit Exceeded"}, {"Retry-After": "5000"})
        self.assertTrue(caught.exception.definite)
        self.assertEqual(caught.exception.retry_after_ms, 5000)
        with self.assertRaises(rx.RevolutXError) as caught:
            result(503, {"message": "Something went wrong!"})
        self.assertFalse(caught.exception.definite)
        with self.assertRaises(rx.RevolutXError):
            result(200 + 300, ["not", "a", "dict"])

    # ── sync ──

    async def test_sync_seeds_cash_and_positions_from_the_account(self):
        result = await self.svc.sync_from_account(1)
        ledger = self.ledger()
        self.assertEqual(ledger["quote"], "EUR")
        self.assertEqual(ledger["cash"], "40")
        # cost basis restarts at the current bid
        self.assertEqual(ledger["positions"], {"SUI": {"units": "5", "cost": "15"}})
        self.assertTrue(ledger["synced"])
        self.assertFalse(ledger["enabled"])
        self.assertEqual(result["skipped"], ["XYZ"])  # no EUR pair

    async def test_sync_values_only_what_is_available_and_keeps_the_observed_fee(self):
        self.svc.balances[1] = {
            "currency": "SUI",
            "available": "4",
            "reserved": "1",
            "total": "5",
        }
        self.set_ledger(observed_taker_fee_pct="0.09")
        await self.svc.sync_from_account(1)
        self.assertEqual(self.ledger()["positions"]["SUI"]["units"], "4")
        self.assertEqual(self.ledger()["observed_taker_fee_pct"], "0.09")

    async def test_cannot_enable_before_the_first_sync_or_without_keys(self):
        with self.assertRaises(ValueError) as caught:
            self.svc.set_enabled(1, True)
        self.assertIn("Sync", str(caught.exception))
        await self.synced(enabled=False)
        self.svc.set_enabled(1, True)
        self.svc.disconnect(1)
        with self.assertRaises(ValueError):
            self.svc.set_enabled(1, True)

    async def test_cannot_sync_while_trading_is_on_or_an_order_is_uncertain(self):
        await self.synced()
        with self.assertRaises(ValueError) as caught:
            await self.svc.sync_from_account(1)
        self.assertIn("Pause", str(caught.exception))
        self.svc.set_enabled(1, False)
        self.set_ledger(uncertain_order={"client_oid": "x", "time": NOW})
        with self.assertRaises(ValueError) as caught:
            await self.svc.sync_from_account(1)
        self.assertIn("Reconcile", str(caught.exception))

    # ── sizing ──

    async def test_buy_is_an_ioc_limit_through_the_ask_rounded_to_price_step_and_lot(
        self,
    ):
        await self.synced()
        self.svc.record = info(
            "buy", "BTC", "0.00013723", "0.00013723", "72650.36", "0.009", cid=None
        )
        await self.svc.order(1, "buy", "BTC", "10")
        (body,) = self.posts()
        limit = (Decimal("72650.36") * Decimal("1.003")).quantize(
            Decimal("0.01"), rounding="ROUND_UP"
        )
        self.assertEqual(
            body["order_configuration"]["limit"],
            {
                "base_size": str(
                    (Decimal("10") / limit).quantize(
                        Decimal("1e-8"), rounding="ROUND_DOWN"
                    )
                ),
                "price": str(limit),
                "time_in_force": "ioc",
                "execution_instructions": ["allow_taker"],
            },
        )
        self.assertEqual(body["symbol"], "BTC-EUR")
        self.assertEqual(body["side"], "buy")
        self.assertRegex(
            body["client_order_id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )
        self.assertEqual(
            set(body), {"client_order_id", "symbol", "side", "order_configuration"}
        )

    async def test_sell_rounds_the_price_down_and_the_size_to_the_lot(self):
        await self.synced()
        self.svc.record = info("sell", "SUI", "2.5", "2.5", "3.0", "0.007")
        await self.svc.order(1, "sell", "SUI", "2.5000099", "0.5")
        (body,) = self.posts()
        limit = body["order_configuration"]["limit"]
        self.assertEqual(Decimal(limit["base_size"]), Decimal("2.50000"))
        # bid 3.0 through the touch by 0.5% = 2.985, on the 0.0001 price step
        self.assertEqual(Decimal(limit["price"]), Decimal("2.985"))
        self.assertEqual(body["side"], "sell")

    async def test_orders_below_the_minimums_are_refused_before_anything_is_sent(self):
        await self.synced()
        for side, value in (("buy", "0.05"), ("sell", "0.01")):
            with self.assertRaises(ValueError) as caught:
                await self.svc.order(1, side, "SUI" if side == "sell" else "BTC", value)
            self.assertIn("minimum", str(caught.exception))
        self.svc.pairs_raw = {
            **PAIRS,
            "BTC/EUR": {**PAIRS["BTC/EUR"], "min_order_size": "0.001"},
        }
        self.svc._pairs = (0.0, {})
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "BTC", "10")
        self.assertIn("minimum", str(caught.exception))
        self.svc.pairs_raw = {
            **PAIRS,
            "BTC/EUR": {**PAIRS["BTC/EUR"], "max_order_size_quote": "5"},
        }
        self.svc._pairs = (0.0, {})
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "BTC", "10")
        self.assertIn("maximum", str(caught.exception))
        self.assertFalse(self.posts())

    async def test_amounts_must_be_plain_positive_decimals(self):
        await self.synced()
        for bad in ("-1", "0", "1e3", "1,5", "abc", "", "0x10"):
            with self.assertRaises(ValueError):
                await self.svc.order(1, "buy", "BTC", bad)
        self.assertFalse(self.posts())

    async def test_slippage_above_two_percent_is_refused(self):
        await self.synced()
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "BTC", "10", "2.5")
        self.assertIn("Slippage", str(caught.exception))
        self.assertFalse(self.posts())

    async def test_only_euro_pairs_and_real_pairs_can_be_traded(self):
        await self.synced()
        for symbol in ("BTC-USD", "BTC/GBP", "OLD", "NOPE", "XYZ"):
            with self.assertRaises(ValueError):
                await self.svc.order(1, "buy", symbol, "8")
        with self.assertRaises(ValueError):
            await self.svc.preview(1, "buy", "BTC-USD", "8")
        self.assertFalse(self.posts())

    async def test_a_stablecoin_is_never_bought(self):
        await self.synced()
        self.svc.books["USDC"] = {
            "asks": [level("0.8956", "1000")],
            "bids": [level("0.8950", "1000")],
        }
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "USDC", "10")
        self.assertIn("stablecoin", str(caught.exception))
        self.assertFalse(self.posts())

    async def test_a_wide_spread_is_refused(self):
        await self.synced()
        self.svc.books["BTC"] = {
            "asks": [level("73500", "1")],
            "bids": [level("72500", "1")],
        }
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "BTC", "8")
        self.assertIn("spread", str(caught.exception))

    # ── fills ──

    async def test_a_full_buy_fill_adds_the_reported_fee_to_cost_and_takes_cash(self):
        await self.synced()
        self.svc.record = info(
            "buy",
            "BTC",
            "0.00013723",
            "0.00013723",
            "72650.36",
            "0.0090",
            "EUR",
            cid=None,
        )
        result = await self.svc.order(1, "buy", "BTC", "10")
        ledger = self.ledger()
        value = Decimal("0.00013723") * Decimal("72650.36")
        spent = value + Decimal("0.0090")
        self.assertEqual(
            Decimal(ledger["positions"]["BTC"]["units"]), Decimal("0.00013723")
        )
        self.assertEqual(Decimal(ledger["positions"]["BTC"]["cost"]), spent)
        self.assertEqual(Decimal(ledger["cash"]), Decimal("40") - spent)
        self.assertIsNone(ledger["uncertain_order"])
        self.assertTrue(ledger["enabled"])
        row = ledger["orders"][-1]
        self.assertEqual(
            (row["fee"], row["fee_currency"], row["fee_source"]),
            ("0.009", "EUR", "reported"),
        )
        self.assertEqual(result["order"]["status"], "filled")
        self.assertEqual(result["order"]["fee_source"], "reported")
        # the fills are read back as evidence and say who paid the taker fee
        self.assertEqual(result["order"]["fills"]["taker_trades"], 1)
        self.assertEqual(result["order"]["fills"]["maker_trades"], 0)

    async def test_the_real_fee_rate_is_learned_from_the_first_reported_fee(self):
        await self.synced()
        self.assertEqual(self.svc.fee_rates(1)["source"], "estimate")
        self.svc.record = info(
            "buy", "BTC", "0.0001", "0.0001", "72650.36", "0.00654", "EUR"
        )
        await self.svc.order(1, "buy", "BTC", "7.3")
        self.assertEqual(self.svc.fee_rates(1)["source"], "estimate")  # too small
        self.svc.record = info(
            "buy", "BTC", "0.001", "0.001", "72650.36", "0.0654", "EUR"
        )
        self.svc.balances[0]["available"] = "400"
        self.svc.set_enabled(1, False)
        await self.svc.sync_from_account(1)
        self.svc.set_enabled(1, True)
        await self.svc.order(1, "buy", "BTC", "73")
        rates = self.svc.fee_rates(1)
        self.assertEqual(rates["source"], "observed")
        self.assertEqual(rates["taker_pct"], Decimal("0.09"))
        preview = await self.svc.preview(1, "buy", "ETH", "10")
        self.assertEqual(preview["fee_source"], "observed")
        self.assertNotIn("ESTIMATE", preview["fee_note"])

    async def test_an_ioc_buy_that_filled_in_part_applies_only_what_executed(self):
        await self.synced()
        self.svc.record = info(
            "buy",
            "BTC",
            "0.00013723",
            "0.00005",
            "72650.36",
            "0.0033",
            "EUR",
            status="cancelled",
        )
        result = await self.svc.order(1, "buy", "BTC", "10")
        ledger = self.ledger()
        self.assertEqual(ledger["positions"]["BTC"]["units"], "0.00005")
        spent = Decimal("0.00005") * Decimal("72650.36") + Decimal("0.0033")
        self.assertEqual(Decimal(ledger["cash"]), Decimal("40") - spent)
        self.assertEqual(result["order"]["status"], "cancelled")
        self.assertEqual(result["order"]["filled_quantity"], "0.00005")
        self.assertIsNone(ledger["uncertain_order"])

    async def test_an_ioc_that_filled_nothing_leaves_the_ledger_alone(self):
        await self.synced()
        self.svc.record = info(
            "buy", "BTC", "0.00013723", "0", "72650.36", "0", status="cancelled"
        )
        before = self.ledger()
        result = await self.svc.order(1, "buy", "BTC", "10")
        ledger = self.ledger()
        self.assertEqual(ledger["cash"], before["cash"])
        self.assertEqual(ledger["positions"], before["positions"])
        self.assertIsNone(ledger["uncertain_order"])
        self.assertEqual(ledger["orders"][-1]["quantity"], "0")
        self.assertEqual(ledger["orders"][-1]["fee_source"], "none")
        self.assertNotIn("fills", result["order"])

    async def test_a_rejected_order_is_final_and_places_nothing_in_the_ledger(self):
        await self.synced()
        self.svc.record = info(
            "buy",
            "BTC",
            "0.0001",
            "0",
            "72650.36",
            status="rejected",
            reject_reason="Insufficient",
        )
        result = await self.svc.order(1, "buy", "BTC", "10")
        self.assertEqual(result["order"]["status"], "rejected")
        self.assertEqual(result["order"]["reject_reason"], "Insufficient")
        self.assertEqual(self.ledger()["cash"], "40")

    async def test_a_sale_nets_the_fee_off_the_proceeds_and_closes_the_position(self):
        await self.synced()
        self.svc.record = info("sell", "SUI", "5", "5", "2.995", "0.0135", "EUR")
        await self.svc.order(1, "sell", "SUI", "5")
        ledger = self.ledger()
        self.assertNotIn("SUI", ledger["positions"])
        self.assertEqual(
            Decimal(ledger["cash"]),
            Decimal("40") + Decimal("5") * Decimal("2.995") - Decimal("0.0135"),
        )

    async def test_a_partial_sale_keeps_the_remaining_units_and_cost(self):
        await self.synced()
        self.svc.record = info("sell", "SUI", "2", "2", "3.0", "0.0054")
        await self.svc.order(1, "sell", "SUI", "2")
        position = self.ledger()["positions"]["SUI"]
        self.assertEqual(position["units"], "3")
        self.assertEqual(position["cost"], "9")  # 15 * 3/5

    async def test_a_fee_taken_in_the_coin_reduces_the_units_not_the_cash(self):
        await self.synced()
        self.svc.record = info(
            "buy", "ETH", "0.002", "0.002", "2108.54", "0.0000018", "ETH"
        )
        await self.svc.order(1, "buy", "ETH", "4.3")
        ledger = self.ledger()
        self.assertEqual(
            Decimal(ledger["positions"]["ETH"]["units"]), Decimal("0.0019982")
        )
        self.assertEqual(
            Decimal(ledger["cash"]),
            Decimal("40") - Decimal("0.002") * Decimal("2108.54"),
        )
        self.assertEqual(ledger["orders"][-1]["fee_currency"], "ETH")
        self.assertEqual(ledger["orders"][-1]["fee_source"], "reported")

    async def test_a_missing_fee_is_an_estimate_at_the_published_rate_and_says_so(self):
        await self.synced()
        record = info("buy", "BTC", "0.0001", "0.0001", "72650.36")
        del record["total_fee"], record["fee_currency"]
        self.svc.record = record
        result = await self.svc.order(1, "buy", "BTC", "7.3")
        value = Decimal("0.0001") * Decimal("72650.36")
        ledger = self.ledger()
        # Revolut X charges a BUY fee in the coin: the estimate (0.09% of 0.0001 BTC =
        # 9e-8, rounded up to the lot) comes off the units, the cash moves by the fill.
        self.assertEqual(Decimal(ledger["cash"]), Decimal("40") - value)
        self.assertEqual(
            Decimal(ledger["positions"]["BTC"]["units"]), Decimal("0.00009991")
        )
        row = ledger["orders"][-1]
        self.assertEqual(
            (row["fee"], row["fee_source"], row["fee_currency"]),
            ("0.00000009", "estimate", "BTC"),
        )
        self.assertEqual(result["order"]["fee_source"], "estimate")
        self.assertEqual(result["order"]["estimated_fee"], "0.00000009")
        self.assertEqual(result["order"]["estimated_fee_currency"], "BTC")
        # an estimate never becomes the "observed" rate
        self.assertIsNone(ledger["observed_taker_fee_pct"])

    async def test_a_reported_zero_fee_on_a_fill_is_not_trusted_as_the_rate(self):
        await self.synced()
        self.svc.record = info("buy", "BTC", "0.0001", "0.0001", "72650.36", "0", "EUR")
        await self.svc.order(1, "buy", "BTC", "7.3")
        ledger = self.ledger()
        self.assertEqual(ledger["orders"][-1]["fee_source"], "estimate")
        self.assertIsNone(ledger["observed_taker_fee_pct"])
        self.assertEqual(
            Decimal(ledger["cash"]),
            Decimal("40") - Decimal("0.0001") * Decimal("72650.36"),
        )
        self.assertEqual(ledger["positions"]["BTC"]["units"], "0.00009991")

    async def test_a_fee_in_a_currency_the_ledger_cannot_use_is_an_estimate(self):
        await self.synced()
        self.svc.record = info(
            "buy", "BTC", "0.0001", "0.0001", "72650.36", "0.01", "USD"
        )
        await self.svc.order(1, "buy", "BTC", "7.3")
        self.assertEqual(self.ledger()["orders"][-1]["fee_source"], "estimate")

    async def test_the_order_object_decides_not_the_fills(self):
        await self.synced()
        self.svc.record = info("buy", "BTC", "0.0001", "0.0001", "72650.36", "0.0065")
        self.svc.trades = [{"p": "1", "q": "1", "im": True}]
        result = await self.svc.order(1, "buy", "BTC", "7.3")
        self.assertEqual(self.ledger()["positions"]["BTC"]["units"], "0.0001")
        self.assertEqual(result["order"]["fills"]["maker_trades"], 1)
        # a failing fills read never turns a known final order into an uncertain one
        self.svc.fills_error = True
        self.svc.record = info("sell", "SUI", "1", "1", "3.0", "0.0027")
        result = await self.svc.order(1, "sell", "SUI", "1")
        self.assertNotIn("fills", result["order"])
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_a_partly_filled_ioc_that_is_no_longer_active_is_final(self):
        await self.synced()
        self.svc.record = info(
            "buy",
            "BTC",
            "0.00013723",
            "0.00005",
            "72650.36",
            "0.0033",
            status="partially_filled",
        )
        self.svc.active = []
        await self.svc.order(1, "buy", "BTC", "10")
        self.assertEqual(self.ledger()["positions"]["BTC"]["units"], "0.00005")
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_a_partly_filled_order_that_is_still_active_stays_uncertain(self):
        await self.synced()
        self.svc.record = info(
            "buy",
            "BTC",
            "0.00013723",
            "0.00005",
            "72650.36",
            "0.0033",
            status="partially_filled",
        )
        self.svc.active = [{"id": OID}]
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "BTC", "10")
        self.assertIn("reconciliation", str(caught.exception))
        self.assertEqual(self.ledger()["cash"], "40")

    # ── guards ──

    async def test_one_coin_cannot_exceed_forty_percent_of_equity(self):
        await self.synced()  # equity 40 + 15 = 55, cap 22; SUI already costs 15
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "SUI", "10")
        self.assertIn("40%", str(caught.exception))
        self.assertFalse(self.posts())

    async def test_a_buy_must_leave_room_for_the_fee(self):
        await self.synced(enabled=False)
        self.set_ledger(cash="10", positions={})
        self.svc.balances[0] = {
            "currency": "EUR",
            "available": "10",
            "reserved": "0",
            "total": "10",
        }
        self.svc.set_enabled(1, True)
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "ETH", "10")  # all the cash
        self.assertIn("fee", str(caught.exception))

    async def test_the_equity_floor_pauses_trading(self):
        self.svc.balances = [
            {"currency": "EUR", "available": "1", "reserved": "0", "total": "1"}
        ]
        await self.synced()
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "BTC", "0.9")
        self.assertIn("stop threshold", str(caught.exception))
        self.assertFalse(self.ledger()["enabled"])
        self.assertFalse(self.posts())

    async def test_from_1000_the_position_count_and_reserve_rules_apply(self):
        self.svc.balances = [
            {"currency": "EUR", "available": "1100", "reserved": "0", "total": "1100"}
        ]
        await self.synced()
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "BTC", "200")  # more than one sixth
        self.assertIn("one sixth", str(caught.exception))
        self.svc.record = info("buy", "BTC", "0.0022", "0.0022", "72650.36", "0.14")
        await self.svc.order(1, "buy", "BTC", "160")
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "BTC", "10")
        self.assertIn("Close the existing position", str(caught.exception))

    async def test_paused_trading_places_nothing(self):
        await self.synced(enabled=False)
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "BTC", "8")
        self.assertFalse(self.posts())

    async def test_cannot_sell_what_the_bot_does_not_own(self):
        await self.synced()
        with self.assertRaises(ValueError):
            await self.svc.order(1, "sell", "BTC", "0.001")
        with self.assertRaises(ValueError):
            await self.svc.order(1, "sell", "SUI", "6")  # more than the position
        self.assertFalse(self.posts())

    async def test_the_account_must_actually_hold_what_the_ledger_says(self):
        await self.synced()
        self.svc.balances[1] = {
            "currency": "SUI",
            "available": "0",
            "reserved": "5",
            "total": "5",
        }
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "sell", "SUI", "5")
        self.assertIn("Insufficient available Revolut X balance", str(caught.exception))
        self.svc.balances[0] = {
            "currency": "EUR",
            "available": "5",
            "reserved": "0",
            "total": "5",
        }
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "ETH", "8")
        self.assertFalse(self.posts())

    async def test_at_most_ten_orders_a_minute(self):
        await self.synced()
        import time

        now = time.time()
        self.set_ledger(orders=[{"time": now - i} for i in range(10)])
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "ETH", "5")
        self.assertIn("ten orders", str(caught.exception))
        self.assertFalse(self.posts())

    async def test_there_is_no_market_hours_rule_it_trades_at_any_hour(self):
        await self.synced()
        self.svc.record = info("sell", "SUI", "1", "1", "3.0", "0.0027")
        for stamp in (NOW, NOW + 5 * 3600, NOW + 86400 * 3):  # a night and a weekend
            self.svc.clock = lambda stamp=stamp: stamp
            self.svc.record = info("sell", "SUI", "0.1", "0.1", "3.0", "0.0003")
            await self.svc.order(1, "sell", "SUI", "0.1")
        self.assertEqual(len(self.posts()), 3)

    # ── uncertain outcomes ──

    async def test_a_definite_rejection_is_not_an_uncertain_order(self):
        await self.synced()
        for status, message in (
            (400, "No such pair: BTC-BTC"),
            (401, "API key can only be used from whitelisted IP"),
            (403, "Forbidden"),
            (409, "Request timestamp is in the future"),
            (429, "Rate Limit Exceeded"),
        ):
            self.svc.reject_place = (status, message)
            with self.assertRaises(ValueError) as caught:
                await self.svc.order(1, "sell", "SUI", "1")
            self.assertIn("rejected", str(caught.exception))
            self.assertIn(message[:20], str(caught.exception))
            ledger = self.ledger()
            self.assertIsNone(ledger["uncertain_order"], status)
            self.assertTrue(ledger["enabled"], status)
            self.assertEqual(ledger["positions"]["SUI"]["units"], "5")
            self.assertEqual(ledger["cash"], "40")

    async def test_a_server_side_failure_stays_uncertain(self):
        await self.synced()
        for status in (500, 502, 503, 504):
            self.svc.reject_place = (status, "Something went wrong!")
            with self.assertRaises(ValueError) as caught:
                await self.svc.order(1, "sell", "SUI", "1")
            self.assertIn("reconciliation", str(caught.exception))
            self.assertIsNotNone(self.ledger()["uncertain_order"])
            self.set_ledger(uncertain_order=None)

    async def test_a_timeout_or_transport_error_pauses_trading_until_reconciled(self):
        await self.synced()
        self.svc.fail_place = True
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "sell", "SUI", "1")
        self.assertIn("reconciliation", str(caught.exception))
        ledger = self.ledger()
        self.assertEqual(ledger["uncertain_order"]["client_oid"], self.svc.cid())
        self.assertEqual(ledger["uncertain_order"]["side"], "sell")
        self.assertFalse(self.svc.status(1)["enabled"])
        with self.assertRaises(ValueError):
            await self.svc.order(1, "sell", "SUI", "1")
        self.assertEqual(len(self.posts()), 1)

    async def test_the_uncertain_entry_is_written_before_the_order_is_sent(self):
        await self.synced()
        seen = []
        original = self.svc.private

        async def spy(user_id, method, path, query=None, body=None):
            if method == "POST":
                seen.append(self.ledger()["uncertain_order"])
            return await original(user_id, method, path, query, body)

        self.svc.private = spy
        self.svc.record = info("sell", "SUI", "1", "1", "3.0", "0.0027")
        await self.svc.order(1, "sell", "SUI", "1")
        self.assertEqual(seen[0]["client_oid"], self.svc.cid())
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_an_accepted_order_without_an_id_is_uncertain(self):
        await self.synced()
        self.svc.no_id = True
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "sell", "SUI", "1")
        self.assertIn("reconciliation", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])

    async def test_an_order_that_never_reaches_a_final_state_stays_uncertain(self):
        await self.synced()
        self.svc.record = info("sell", "SUI", "1", "0", "3.0", status="new")
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "sell", "SUI", "1")
        self.assertIn("reconciliation", str(caught.exception))
        pending = self.ledger()["uncertain_order"]
        self.assertEqual(pending["order_id"], OID)

    async def test_a_refused_read_back_after_an_accepted_order_is_never_a_rejection(
        self,
    ):
        await self.synced()
        for args in ((429, "Rate Limit Exceeded"), (400, "bad"), (500, "boom")):
            self.svc.read_error = args
            with self.assertRaises(ValueError) as caught:
                await self.svc.order(1, "sell", "SUI", "1")
            self.assertIn("reconciliation", str(caught.exception), args)
            self.assertIsNotNone(self.ledger()["uncertain_order"], args)
            self.set_ledger(uncertain_order=None)

    async def test_an_order_cannot_be_sent_without_credentials_and_leaves_no_uncertain_entry(
        self,
    ):
        await self.synced()
        rx.credentials_path(1).unlink()
        with self.assertRaises(ValueError):
            await self.svc.order(1, "sell", "SUI", "1")
        self.assertIsNone(self.ledger()["uncertain_order"])
        self.assertFalse(self.posts())

    # ── buy fee in the coin, balances and whole-position exits (live 2026-10-29) ──

    def fine_eth_lot(self):
        pairs = json.loads(json.dumps(PAIRS))
        pairs["ETH/EUR"]["base_step"] = "0.00000001"
        pairs["ETH/EUR"]["min_order_size"] = "0.000001"
        self.svc.pairs_raw = pairs

    def balance(self, currency, available):
        return {
            "currency": currency,
            "available": available,
            "reserved": "0",
            "total": available,
        }

    def balance_reads(self):
        """GET /balances calls since the sync that seeded the ledger."""
        reads = [p for m, p, q, b in self.svc.sent if p == "/1.0/balances"]
        return reads[1:]

    async def test_the_live_eth_buy_without_a_reported_fee_matches_the_account(self):
        # 2026-10-29 14:22 UTC: the read-back carried no fee; the account held
        # 0.0057273 ETH for a fill of 0.00573246 (0.09% taker fee, charged in ETH).
        self.fine_eth_lot()
        await self.synced()
        record = info("buy", "ETH", "0.00573246", "0.00573246", "2108.54")
        del record["total_fee"], record["fee_currency"]
        self.svc.record = record
        self.svc.balances_after = [
            self.balance("EUR", "27.92"),
            self.balance("SUI", "5"),
            self.balance("ETH", "0.0057273"),
        ]
        result = await self.svc.order(1, "buy", "ETH", "12.1")
        ledger = self.ledger()
        value = Decimal("0.00573246") * Decimal("2108.54")
        self.assertEqual(result["order"]["estimated_fee"], "0.00000516")
        self.assertEqual(
            Decimal(ledger["positions"]["ETH"]["units"]), Decimal("0.0057273")
        )
        self.assertEqual(Decimal(ledger["positions"]["ETH"]["cost"]), value)
        self.assertEqual(Decimal(ledger["cash"]), Decimal("40") - value)
        self.assertEqual(
            (ledger["orders"][-1]["fee_currency"], ledger["orders"][-1]["fee_source"]),
            ("ETH", "estimate"),
        )
        # the estimate was right: nothing to correct, and it says so
        self.assertNotIn("balance_correction", result["order"])
        self.assertNotIn("units_source", ledger["positions"]["ETH"])
        self.assertEqual(len(self.balance_reads()), 2)  # before the order, once after

    async def test_a_buy_with_a_reported_fee_does_not_read_the_balances_again(self):
        await self.synced()
        self.svc.record = info(
            "buy", "ETH", "0.002", "0.002", "2108.54", "0.0000018", "ETH"
        )
        result = await self.svc.order(1, "buy", "ETH", "4.3")
        self.assertEqual(len(self.balance_reads()), 1)  # the pre-order check only
        self.assertNotIn("balance_check", result["order"])
        self.assertNotIn("balance_correction", result["order"])
        self.assertEqual(self.ledger()["positions"]["ETH"]["units"], "0.0019982")

    async def test_the_balance_read_after_an_estimated_buy_corrects_the_ledger(self):
        await self.synced()
        record = info("buy", "BTC", "0.0001", "0.0001", "72650.36")
        del record["total_fee"], record["fee_currency"]
        self.svc.record = record
        # the account holds less BTC than the estimate says, and less cash
        self.svc.balances_after = [
            self.balance("EUR", "30"),
            self.balance("SUI", "5"),
            self.balance("BTC", "0.00009"),
        ]
        result = await self.svc.order(1, "buy", "BTC", "7.3")
        ledger = self.ledger()
        position = ledger["positions"]["BTC"]
        self.assertEqual(position["units"], "0.00009")
        self.assertEqual(position["units_source"], "account_balance")
        self.assertEqual(ledger["cash"], "30")
        self.assertEqual(ledger["cash_source"], "account_balance")
        correction = result["order"]["balance_correction"]
        self.assertEqual(correction["units"]["ledger"], "0.00009991")
        self.assertEqual(correction["units"]["set_to"], "0.00009")
        self.assertEqual(correction["units_source"], "account_balance")
        self.assertEqual(ledger["orders"][-1]["balance_correction"], correction)

    async def test_a_ledger_within_one_lot_of_the_account_is_left_alone(self):
        await self.synced()
        record = info("buy", "BTC", "0.0001", "0.0001", "72650.36")
        del record["total_fee"], record["fee_currency"]
        self.svc.record = record
        self.svc.balances_after = [
            self.balance("EUR", "32.74"),
            self.balance("BTC", "0.00009990"),  # one lot (1e-8) below the ledger
        ]
        result = await self.svc.order(1, "buy", "BTC", "7.3")
        self.assertEqual(self.ledger()["positions"]["BTC"]["units"], "0.00009991")
        self.assertNotIn("balance_correction", result["order"])

    async def test_more_in_the_account_than_in_the_ledger_is_never_adopted(self):
        await self.synced()
        record = info("buy", "BTC", "0.0001", "0.0001", "72650.36")
        del record["total_fee"], record["fee_currency"]
        self.svc.record = record
        self.svc.balances_after = [
            self.balance("EUR", "900"),
            self.balance("BTC", "0.5"),  # the account has coins outside the allocation
        ]
        await self.svc.order(1, "buy", "BTC", "7.3")
        ledger = self.ledger()
        self.assertEqual(ledger["positions"]["BTC"]["units"], "0.00009991")
        self.assertEqual(
            Decimal(ledger["cash"]),
            Decimal("40") - Decimal("0.0001") * Decimal("72650.36"),
        )

    async def test_a_missing_balance_for_the_coin_just_bought_is_flagged_not_erased(
        self,
    ):
        await self.synced()
        record = info("buy", "BTC", "0.0001", "0.0001", "72650.36")
        del record["total_fee"], record["fee_currency"]
        self.svc.record = record  # the fake shows no BTC at all
        result = await self.svc.order(1, "buy", "BTC", "7.3")
        self.assertEqual(self.ledger()["positions"]["BTC"]["units"], "0.00009991")
        self.assertIn("no BTC balance", result["order"]["balance_check"])

    async def test_a_failed_balance_read_after_the_fill_never_undoes_the_fill(self):
        await self.synced()
        record = info("buy", "BTC", "0.0001", "0.0001", "72650.36")
        del record["total_fee"], record["fee_currency"]
        self.svc.record = record
        self.svc.balances_after_error = True
        result = await self.svc.order(1, "buy", "BTC", "7.3")
        ledger = self.ledger()
        self.assertIsNone(ledger["uncertain_order"])
        self.assertEqual(ledger["positions"]["BTC"]["units"], "0.00009991")
        self.assertEqual(result["order"]["balance_check"], "unavailable")

    async def test_selling_the_whole_position_is_sized_from_the_real_balance(self):
        # The ledger still holds the full fill (0.00573246 ETH); the account holds
        # 0.0057273. Asking for the ledger amount used to be refused as insufficient.
        self.fine_eth_lot()
        await self.synced()
        self.set_ledger(
            positions={"ETH": {"units": "0.00573246", "cost": "12.09"}}, cash="10"
        )
        self.svc.balances = [
            self.balance("EUR", "40"),
            self.balance("ETH", "0.0057273"),
        ]
        self.svc.balances_after = [
            self.balance("EUR", "52"),
            self.balance("ETH", "0"),
        ]
        self.svc.record = info("sell", "ETH", "0.0057273", "0.0057273", "2108.22")
        await self.svc.order(1, "sell", "ETH", "0.00573246")
        self.assertEqual(
            self.posts()[-1]["order_configuration"]["limit"]["base_size"], "0.0057273"
        )
        ledger = self.ledger()
        # nothing of the position is left on a ledger that has nothing to sell
        self.assertNotIn("ETH", ledger["positions"])
        self.assertIsNone(ledger["uncertain_order"])

    async def test_a_whole_exit_with_a_matching_balance_is_sent_unchanged(self):
        await self.synced()
        self.svc.record = info("sell", "SUI", "5", "5", "2.995", "0.0135", "EUR")
        await self.svc.order(1, "sell", "SUI", "5")
        self.assertEqual(
            self.posts()[-1]["order_configuration"]["limit"]["base_size"], "5"
        )
        self.assertEqual(len(self.balance_reads()), 1)

    async def test_a_whole_exit_below_the_pair_minimum_says_so_and_sends_nothing(self):
        self.fine_eth_lot()
        await self.synced()
        self.set_ledger(positions={"ETH": {"units": "0.00573246", "cost": "12.09"}})
        self.svc.balances = [
            self.balance("EUR", "40"),
            self.balance("ETH", "0.0000005"),  # below the 0.000001 ETH minimum
        ]
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "sell", "ETH", "0.00573246")
        self.assertIn("Insufficient available Revolut X balance", str(caught.exception))
        self.assertIn("minimum order size", str(caught.exception))
        self.assertFalse(self.posts())
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_a_partial_exit_is_never_resized_silently(self):
        await self.synced()
        self.svc.balances[1] = self.balance("SUI", "4")
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "sell", "SUI", "4.5")
        self.assertIn("Insufficient available Revolut X balance", str(caught.exception))
        self.assertFalse(self.posts())

    async def test_a_sale_without_a_reported_fee_keeps_the_estimate_in_euros(self):
        await self.synced()
        record = info("sell", "SUI", "2", "2", "3.0")
        del record["total_fee"], record["fee_currency"]
        self.svc.record = record
        self.svc.balances_after = [
            self.balance("EUR", "45.9946"),
            self.balance("SUI", "3"),
        ]
        result = await self.svc.order(1, "sell", "SUI", "2")
        ledger = self.ledger()
        fee = Decimal("6") * Decimal("0.09") / 100
        self.assertEqual(Decimal(ledger["cash"]), Decimal("40") + 6 - fee)
        self.assertEqual(ledger["positions"]["SUI"]["units"], "3")  # the lot is whole
        row = ledger["orders"][-1]
        self.assertEqual((row["fee_currency"], row["fee_source"]), ("EUR", "estimate"))
        self.assertEqual(result["order"]["estimated_fee_currency"], "EUR")
        self.assertNotIn("balance_correction", result["order"])

    async def test_reconcile_books_an_estimated_buy_fee_in_the_coin(self):
        await self.synced()
        self.svc.fail_place = True
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "BTC", "7.3")
        self.svc.fail_place = False
        self.age_pending()
        record = info("buy", "BTC", "0.0001", "0.0001", "72650.36", cid=self.svc.cid())
        del record["total_fee"], record["fee_currency"]
        self.svc.historical = [record]
        self.svc.balances = [
            self.balance("EUR", "40"),
            self.balance("BTC", "0.00009991"),
        ]
        await self.svc.reconcile(1)
        ledger = self.ledger()
        self.assertEqual(ledger["positions"]["BTC"]["units"], "0.00009991")
        self.assertEqual(
            Decimal(ledger["cash"]),
            Decimal("40") - Decimal("0.0001") * Decimal("72650.36"),
        )
        self.assertEqual(ledger["orders"][-1]["fee_currency"], "BTC")

    # ── reconcile ──

    async def uncertain_sell(self):
        await self.synced()
        self.svc.fail_place = True
        with self.assertRaises(ValueError):
            await self.svc.order(1, "sell", "SUI", "2")
        self.svc.fail_place = False

    def age_pending(self, seconds=120):
        pending = self.ledger()["uncertain_order"]
        pending["time"] = pending["time"] - seconds
        self.set_ledger(uncertain_order=pending)

    async def test_reconcile_applies_an_order_found_by_client_id_in_the_history(self):
        await self.uncertain_sell()
        self.svc.historical = [
            info("buy", "BTC", "1", "1", "1", cid="someone-else"),
            info("sell", "SUI", "2", "2", "2.99", "0.0054", cid=self.svc.cid()),
        ]
        status = await self.svc.reconcile(1)
        ledger = self.ledger()
        self.assertIsNone(ledger["uncertain_order"])
        self.assertEqual(ledger["positions"]["SUI"]["units"], "3")
        self.assertEqual(
            Decimal(ledger["cash"]), Decimal("40") + Decimal("5.98") - Decimal("0.0054")
        )
        self.assertFalse(status["enabled"])  # trading stays paused for the owner

    async def test_reconcile_finds_the_order_among_the_active_orders_first(self):
        await self.uncertain_sell()
        self.svc.active = [
            info("sell", "SUI", "2", "0", "2.99", status="new", cid=self.svc.cid())
        ]
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("still open", str(caught.exception))
        self.assertIsNotNone(self.ledger()["uncertain_order"])
        paths = [p for m, p, q, b in self.svc.sent]
        self.assertNotIn("/1.0/orders/historical", paths)

    async def test_reconcile_reads_the_order_by_id_when_it_is_known(self):
        await self.synced()
        self.svc.record = info("sell", "SUI", "2", "0", "2.99", status="new")
        with self.assertRaises(ValueError):
            await self.svc.order(1, "sell", "SUI", "2")
        self.assertEqual(self.ledger()["uncertain_order"]["order_id"], OID)
        self.svc.record = info("sell", "SUI", "2", "2", "2.99", "0.0054")
        self.svc.sent.clear()
        await self.svc.reconcile(1)
        self.assertIsNone(self.ledger()["uncertain_order"])
        self.assertEqual(self.ledger()["positions"]["SUI"]["units"], "3")
        paths = [p for m, p, q, b in self.svc.sent]
        self.assertNotIn("/1.0/orders/historical", paths)

    async def test_reconcile_settles_a_partly_filled_order_that_is_no_longer_active(
        self,
    ):
        await self.uncertain_sell()
        self.svc.historical = [
            info(
                "sell",
                "SUI",
                "2",
                "1",
                "2.99",
                "0.0027",
                status="partially_filled",
                cid=self.svc.cid(),
            )
        ]
        await self.svc.reconcile(1)
        self.assertEqual(self.ledger()["positions"]["SUI"]["units"], "4")
        self.assertIsNone(self.ledger()["uncertain_order"])

    async def test_reconcile_clears_an_order_never_seen_after_a_minute(self):
        await self.uncertain_sell()
        with self.assertRaises(ValueError) as caught:
            await self.svc.reconcile(1)
        self.assertIn("Wait a minute", str(caught.exception))
        self.age_pending()
        status = await self.svc.reconcile(1)
        self.assertIsNone(self.ledger()["uncertain_order"])
        self.assertEqual(self.ledger()["positions"]["SUI"]["units"], "5")
        self.assertFalse(status["enabled"])

    async def test_reconcile_searches_the_history_pages(self):
        await self.uncertain_sell()
        self.svc.historical_pages = [
            [info("buy", "BTC", "1", "1", "1", cid="a")],
            [info("sell", "SUI", "2", "2", "2.99", "0.0054", cid=self.svc.cid())],
        ]
        await self.svc.reconcile(1)
        self.assertEqual(self.ledger()["positions"]["SUI"]["units"], "3")
        historical = [
            q for m, p, q, b in self.svc.sent if p == "/1.0/orders/historical"
        ]
        self.assertEqual(len(historical), 2)
        self.assertEqual(historical[1]["cursor"], "1")
        self.assertLessEqual(
            historical[0]["end_date"] - historical[0]["start_date"], 30 * 86_400_000
        )

    async def test_reconcile_with_nothing_pending_changes_nothing(self):
        await self.synced()
        before = self.ledger()
        await self.svc.reconcile(1)
        self.assertEqual(self.ledger(), before)

    async def test_a_cent_rounded_fee_on_a_tiny_fill_is_not_learned_as_the_rate(self):
        await self.synced()
        self.svc.record = info(
            "buy", "BTC", "0.00001373", "0.00001373", "72650.36", "0.01", "EUR"
        )
        await self.svc.order(1, "buy", "BTC", "1", None, allow_costly_buy=True)
        self.assertIsNone(self.ledger()["observed_taker_fee_pct"])
        self.assertEqual(self.svc.fee_rates(1)["source"], "estimate")

    async def test_the_average_price_comes_from_the_fills_not_the_rounded_amount(self):
        # The real sale of 2026-10-28: fills value 0.99557 for 0.00001368 BTC, but the
        # order object reported the amount rounded down to 0.99 and a price of 72368.42.
        self.svc.balances.append(
            {
                "currency": "BTC",
                "available": "0.00001368",
                "reserved": "0",
                "total": "1",
            }
        )
        await self.synced()
        self.svc.record = info(
            "sell",
            "BTC",
            "0.00001368",
            "0.00001368",
            "72368.42",
            "0.01",
            "EUR",
            filled_amount="0.99",
        )
        self.svc.trades = [{"p": "72776", "q": "0.00001368", "im": False}]
        result = (await self.svc.order(1, "sell", "BTC", "0.00001368"))["order"]
        fills_value = Decimal("72776") * Decimal("0.00001368")
        self.assertEqual(result["average_fill_price"], "72776")
        self.assertEqual(result["reported_average_fill_price"], "72368.42")
        self.assertEqual(result["filled_amount"], "0.99")  # raw value kept
        self.assertIn("differs", result["price_note"])
        self.assertEqual(Decimal(result["fills_value"]), fills_value)
        self.assertEqual(
            Decimal(result["rounding_cost_eur"]), fills_value - Decimal("0.99")
        )
        self.assertEqual(
            Decimal(result["realised_cost_eur"]),
            fills_value - Decimal("0.99") + Decimal("0.01"),
        )
        # the balance really moved by the reported 0.99 less the 0.01 fee
        ledger = self.ledger()
        self.assertEqual(Decimal(ledger["cash"]), Decimal("40") + Decimal("0.98"))
        self.assertEqual(
            ledger["orders"][-1]["rounding_cost_eur"], result["rounding_cost_eur"]
        )

    async def test_a_buy_with_a_base_currency_fee_reports_its_rounding_cost(self):
        # The real buy of 2026-10-28: 1.00 EUR left the balance for fills worth 0.996945,
        # and the fee was reported in BTC.
        await self.synced()
        self.svc.record = info(
            "buy",
            "BTC",
            "0.00001373",
            "0.00001373",
            "72837.9",
            "0.00000002",
            "BTC",
            filled_amount="1.00",
        )
        self.svc.trades = [{"p": "72610", "q": "0.00001373", "im": False}]
        result = (
            await self.svc.order(1, "buy", "BTC", "1", None, allow_costly_buy=True)
        )["order"]
        fills_value = Decimal("72610") * Decimal("0.00001373")
        self.assertEqual(result["average_fill_price"], "72610")
        self.assertEqual(Decimal(result["rounding_cost_eur"]), 1 - fills_value)
        self.assertEqual(
            Decimal(result["realised_cost_eur"]),
            1 - fills_value + Decimal("0.00000002") * Decimal("72610"),
        )
        self.assertEqual(Decimal(self.ledger()["cash"]), Decimal("39"))

    async def test_matching_reported_and_fills_prices_add_no_note(self):
        await self.synced()
        self.svc.record = info("buy", "BTC", "0.0001", "0.0001", "72650.36", "0.0065")
        result = (await self.svc.order(1, "buy", "BTC", "7.3"))["order"]
        self.assertNotIn("price_note", result)
        self.assertEqual(result["average_fill_price"], "72650.36")
        self.assertEqual(result["rounding_cost_eur"], "0")

    # ── the cost guard ──

    async def test_a_small_buy_that_cent_rounding_would_eat_is_refused_before_sending(
        self,
    ):
        await self.synced()
        self.svc.sent.clear()
        preview = await self.svc.preview(1, "buy", "BTC", "1.5")
        self.assertTrue(preview["cost_guard_blocks_buy"])
        with self.assertRaises(ValueError) as caught:
            await self.svc.order(1, "buy", "BTC", "1.5")
        text = str(caught.exception)
        self.assertIn("Refused, no order placed", text)
        self.assertIn("EUR or more", text)
        self.assertEqual(self.posts(), [])
        self.assertIsNone(self.ledger()["uncertain_order"])
        self.assertTrue(self.ledger()["enabled"])  # nothing paused, nothing sent

    async def test_a_sale_is_never_blocked_by_the_cost_guard(self):
        await self.synced()
        self.svc.record = info("sell", "SUI", "0.5", "0.5", "3.0", "0.001")
        preview = await self.svc.preview(1, "sell", "SUI", "0.5")
        self.assertFalse(preview["cost_guard_blocks_buy"])
        self.assertGreater(Decimal(preview["estimated_round_trip_pct"]), 1)
        result = await self.svc.order(1, "sell", "SUI", "0.5")
        self.assertEqual(result["order"]["status"], "filled")
        self.assertEqual(len(self.posts()), 1)

    async def test_a_big_enough_buy_passes_and_the_owner_can_override_a_small_one(self):
        await self.synced()
        self.svc.record = info("buy", "BTC", "0.00013723", "0.00013723", "72650.36")
        big = await self.svc.preview(1, "buy", "BTC", "10")
        self.assertFalse(big["cost_guard_blocks_buy"])
        await self.svc.order(1, "buy", "BTC", "10")
        self.assertEqual(len(self.posts()), 1)
        self.svc.record = info("buy", "BTC", "0.00002", "0.00002", "72650.36")
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "ETH", "1.5")
        await self.svc.order(1, "buy", "BTC", "1.5", None, allow_costly_buy=True)
        self.assertEqual(len(self.posts()), 2)

    async def test_the_override_is_strictly_boolean_true(self):
        await self.synced()
        with self.assertRaises(ValueError):
            await self.svc.order(1, "buy", "BTC", "1.5", None, allow_costly_buy="yes")
        self.assertEqual(self.posts(), [])

    # ── preview and market data ──

    async def test_the_preview_adds_the_cent_rounding_allowance_at_small_sizes(self):
        for spend, allowance, blocked in (
            ("1.5", Decimal("1.334"), True),
            ("10", Decimal("0.2"), False),
            ("40", Decimal("0.05"), False),
        ):
            preview = await self.svc.preview(1, "buy", "BTC", spend)
            notional = Decimal(preview["notional_eur"])
            self.assertAlmostEqual(
                Decimal(preview["rounding_allowance_pct"]),
                allowance,
                delta=Decimal("0.01"),
            )
            self.assertEqual(preview["rounding_allowance_eur"], "0.02")
            self.assertEqual(
                Decimal(preview["rounding_allowance_pct"]),
                round(Decimal("0.02") / notional * 100, 3),
            )
            parts = preview["round_trip_components_pct"]
            self.assertAlmostEqual(
                Decimal(preview["estimated_round_trip_pct"]),
                sum(map(Decimal, parts.values())),
                delta=Decimal("0.002"),
            )
            self.assertEqual(Decimal(parts["taker_fees"]), Decimal("0.18"))
            self.assertEqual(preview["cost_guard_blocks_buy"], blocked)
            self.assertEqual(preview["cost_guard_max_round_trip_pct"], "1.0")
            self.assertIn("cent", preview["rounding_note"])
            self.assertEqual(preview["fee_source"], "estimate")  # still labelled

    async def test_preview_of_a_sale_includes_the_allowance_but_never_blocks(self):
        preview = await self.svc.preview(1, "sell", "BTC", "0.00001")
        self.assertGreater(Decimal(preview["rounding_allowance_pct"]), 1)
        self.assertFalse(preview["cost_guard_blocks_buy"])

    async def test_preview_prices_the_book_labels_the_fee_an_estimate_and_needs_no_key(
        self,
    ):
        await self.synced()
        self.svc.disconnect(1)
        self.svc.sent.clear()
        preview = await self.svc.preview(1, "buy", "SUI", "100")
        self.assertEqual(self.svc.sent, [])  # public data only
        self.assertEqual(preview["pair"], "SUI-EUR")
        self.assertEqual(preview["side"], "buy")
        self.assertEqual(preview["bid"], "3")
        self.assertEqual(preview["ask"], "3.003")
        limit = Decimal(preview["limit_price"])
        self.assertEqual(limit, Decimal("3.0121"))
        self.assertEqual(
            preview["quantity"],
            str(
                (Decimal(100) / limit).quantize(Decimal("1e-5"), rounding="ROUND_DOWN")
            ).rstrip("0"),
        )
        self.assertTrue(preview["book_covers_order"])
        self.assertEqual(preview["estimated_average_price"], "3.003")
        self.assertEqual(preview["taker_fee_pct"], "0.09")
        self.assertEqual(preview["fee_source"], "estimate")
        self.assertIn("ESTIMATE", preview["fee_note"])
        spread = (Decimal("3.003") - Decimal("3.0")) / Decimal("3.003") * 100
        allowance = Decimal("0.02") / Decimal(preview["notional_eur"]) * 100
        self.assertEqual(
            Decimal(preview["estimated_round_trip_pct"]),
            round(spread + Decimal("0.18") + allowance, 3),
        )
        self.assertEqual(preview["spread_pct"], str(round(spread, 3)))

    async def test_preview_walks_the_book_and_reports_impact_and_thin_books(self):
        await self.synced()
        deep = await self.svc.preview(
            1, "buy", "SUI", "400"
        )  # 130+ SUI, 100 at the touch
        self.assertGreater(Decimal(deep["estimated_average_price"]), Decimal("3.003"))
        self.assertGreater(Decimal(deep["estimated_impact_pct"]), 0)
        self.svc.books["SUI"]["asks"] = [level("3.003", "10")]
        thin = await self.svc.preview(1, "buy", "SUI", "400")
        self.assertFalse(thin["book_covers_order"])

    async def test_preview_of_a_sale_uses_the_bid_side(self):
        await self.synced()
        preview = await self.svc.preview(1, "sell", "SUI", "3")
        self.assertEqual(preview["estimated_average_price"], "3")
        self.assertEqual(Decimal(preview["limit_price"]), Decimal("2.991"))

    async def test_coins_are_ranked_by_euro_volume_skip_stablecoins_and_use_the_eea_rows(
        self,
    ):
        await self.synced()
        result = await self.svc.coins(1)
        symbols = [c["symbol"] for c in result["coins"]]
        self.assertEqual(
            symbols, ["BTC", "ETH", "SUI"]
        )  # no USDC, no BTC/USD, BTC once
        btc = result["coins"][0]
        self.assertEqual(btc["pair"], "BTC-EUR")
        self.assertEqual((btc["bid"], btc["ask"]), ("72643.23", "72650.36"))
        self.assertEqual(btc["spread_pct"], "0.010")
        # the 24h change is an absolute euro move in the feed, shown as a percent
        self.assertEqual(btc["change_24h_pct"], "0.15")
        eth = result["coins"][1]
        self.assertEqual(eth["change_24h_pct"], "-0.99")
        self.assertEqual(btc["min_quantity_step"], "0.00000001")
        self.assertEqual(btc["price_step"], "0.01")
        self.assertEqual(btc["min_order_value_eur"], "0.1")
        self.assertEqual(btc["volume_24h_eur"], "9442733.00")
        found = await self.svc.coins(1, "su")
        self.assertEqual([c["symbol"] for c in found["coins"]], ["SUI"])
        with self.assertRaises(ValueError):
            await self.svc.coins(1, "x" * 41)

    async def test_held_coins_are_always_listed_even_outside_the_top(self):
        await self.synced()
        with patch.object(rx, "TOP_COINS", 1):
            result = await self.svc.coins(1)
        self.assertEqual([c["symbol"] for c in result["coins"]], ["BTC", "SUI"])

    async def test_book_and_candles_come_from_the_public_data_in_the_hyphen_spelling(
        self,
    ):
        book = await self.svc.book(1, "btc/eur", 1)
        self.assertEqual(book["pair"], "BTC-EUR")
        self.assertEqual(book["bids"], [["72643.23", "0.5"]])
        self.assertEqual(book["asks"], [["72650.36", "0.5"]])
        self.assertTrue(
            self.svc.public_sent[-1][0].endswith("/2.0/public/order-book/BTC-EUR")
        )
        candles = await self.svc.candles(1, "BTC", "1h", 3)
        self.assertEqual(candles["pair"], "BTC-EUR")
        self.assertEqual(len(candles["candles"]), 3)
        times = [c["t"] for c in candles["candles"]]
        self.assertEqual(times, sorted(times))
        path, params = self.svc.public_sent[-1]
        self.assertTrue(path.endswith("/1.0/public/candles/BTC-EUR"))
        self.assertEqual(params["interval"], 60)
        self.assertEqual(params["until"] - params["since"], 5 * 60 * 60_000)
        with self.assertRaises(ValueError):
            await self.svc.candles(1, "BTC", "3m", 3)
        with self.assertRaises(ValueError):
            await self.svc.candles(1, "BTC-USD", "1h", 3)

    async def test_the_book_is_sorted_even_if_the_venue_returns_it_unsorted(self):
        self.svc.books["BTC"] = {
            "asks": [level("72700", "1"), level("72650.36", "0.5")],
            "bids": [level("72600", "1"), level("72643.23", "0.5")],
        }
        book = await self.svc.book(1, "BTC", 10)
        self.assertEqual(book["asks"][0][0], "72650.36")
        self.assertEqual(book["bids"][0][0], "72643.23")

    # ── valuation, portfolio, history ──

    async def test_valuation_marks_positions_at_the_bid(self):
        await self.synced()
        value = await self.svc.valuation(1)
        self.assertEqual(value["equity"], "55")  # 40 + 5 * 3.0
        self.assertEqual(value["quote"], "EUR")
        self.assertEqual(value["new_position_limit"], "22")
        self.assertEqual(value["minimum_cash_reserve"], "0")

    async def test_valuation_refuses_while_an_order_is_uncertain(self):
        await self.synced()
        self.set_ledger(uncertain_order={"client_oid": "x", "time": NOW})
        with self.assertRaises(ValueError):
            await self.svc.valuation(1)

    async def test_portfolio_values_the_whole_account_in_usd_through_usdc_eur(self):
        data = await self.svc.portfolio(1)
        self.assertEqual(data["connector"], "Revolut X")
        rate = Decimal(2) / (Decimal("0.8950") + Decimal("0.8956"))  # USD per EUR
        by = {b["token"]: b for b in data["balances"]}
        self.assertEqual(set(by), {"EUR", "SUI", "XYZ"})
        self.assertAlmostEqual(by["EUR"]["usd_value"], float(40 * rate), places=6)
        self.assertAlmostEqual(
            by["SUI"]["usd_value"], float(5 * Decimal("3.001") * rate), places=6
        )
        self.assertEqual(by["XYZ"]["usd_value"], 0)
        self.assertIn("No USD price for XYZ", data["note"])
        self.assertAlmostEqual(
            data["total_usd"], sum(b["usd_value"] for b in data["balances"]), places=9
        )

    def _without_usdc_eur(self):
        self.svc._tickers_cache = (0.0, {})  # the ticker answer is cached a few seconds
        self.svc.tickers_raw = {
            "data": [t for t in tickers()["data"] if t["symbol"] != "USDC/EUR"]
        }

    async def test_there_is_no_kraken_fallback_for_the_eur_usd_rate(self):
        from condor import xstocks_market

        self._without_usdc_eur()
        self.assertIsNone(await self.svc.eur_usd())
        self.assertIsNone(self.svc.recent_eur_usd())
        self.assertFalse(hasattr(xstocks_market.service, "eur_usd"))
        with self.assertRaisesRegex(ValueError, "No EUR/USD rate"):
            await self.svc.portfolio(1)

    async def test_a_rate_read_a_moment_ago_values_the_account_while_the_price_is_gone(
        self,
    ):
        first = await self.svc.portfolio(1)
        rate = self.svc.recent_eur_usd()
        self.assertIsNotNone(rate)
        self._without_usdc_eur()
        self.assertIsNone(await self.svc.eur_usd())  # fresh reads never reuse it
        second = await self.svc.portfolio(1)
        by = {b["token"]: b for b in second["balances"]}
        self.assertAlmostEqual(by["EUR"]["usd_value"], float(40 * rate), places=6)
        self.assertEqual(
            {b["token"] for b in first["balances"]},
            {b["token"] for b in second["balances"]},
        )

    async def test_a_rate_older_than_an_hour_values_nothing(self):
        await self.svc.portfolio(1)
        stamp, rate = self.svc._last_eur_usd
        self.svc._last_eur_usd = (stamp - rx.RATE_MAX_AGE_SEC - 1, rate)
        self.assertIsNone(self.svc.recent_eur_usd())
        self._without_usdc_eur()
        with self.assertRaisesRegex(ValueError, "No EUR/USD rate"):
            await self.svc.portfolio(1)

    async def test_portfolio_names_the_coins_it_could_not_price(self):
        self.svc.tickers_raw = {
            "data": [t for t in tickers()["data"] if t["symbol"] != "SUI/EUR"]
        }
        data = await self.svc.portfolio(1)
        by = {b["token"]: b for b in data["balances"]}
        self.assertEqual(by["SUI"]["usd_value"], 0)
        self.assertIn("No USD price for SUI, XYZ", data["note"])
        self.assertGreater(data["total_usd"], 0)

    async def test_portfolio_refuses_to_value_the_account_without_a_eur_usd_rate(self):
        self.svc.tickers_raw = {"data": []}
        with self.assertRaises(ValueError):
            await self.svc.portfolio(1)

    async def test_history_lists_final_orders_newest_first(self):
        old = info("buy", "BTC", "1", "1", "1", updated_date=1_000)
        new = info("sell", "SUI", "1", "1", "3", updated_date=2_000)
        self.svc.historical = [old, new]
        rows = await self.svc.history(1)
        self.assertEqual([r["pair"] for r in rows], ["SUI/EUR", "BTC/EUR"])
        query = next(q for m, p, q, b in self.svc.sent if p == "/1.0/orders/historical")
        self.assertEqual(query["end_date"] - query["start_date"], 7 * 86_400_000)

    async def test_accounts_lists_only_funded_balances(self):
        data = await self.svc.accounts(1)
        self.assertEqual(
            [(a["currency"], a["balance"], a["available"]) for a in data["accounts"]],
            [("EUR", "40", "40"), ("SUI", "5", "5"), ("XYZ", "1", "1")],
        )
        # the balances call may also come wrapped in data
        self.svc.balances = {"data": self.svc.balances}
        self.assertEqual(len((await self.svc.accounts(1))["accounts"]), 3)

    # ── scorecard ──

    async def test_the_live_scorecard_is_kept_apart_and_scored_at_twice_the_taker_fee_plus_spread(
        self,
    ):
        await self.svc.coins(1)
        self.assertIsNotNone(sc.latest_price(1, "BTC", venue="revolut_x"))
        self.assertIsNone(sc.latest_price(1, "BTC", venue="crypto_com_exchange"))
        call = await self.svc.predict(1, "btc-eur", "trade", "1.2", "0.6", "6", "test")
        self.assertIsInstance(call, dict)
        with patch.object(sc, "report", return_value={"ok": True}) as report:
            await self.svc.scorecard(1)
        cost = report.call_args.kwargs["cost_pct"]
        # median spread of the listed coins plus 2 * 0.09
        self.assertGreater(cost, 0.18)
        self.assertLess(cost, 0.5)
        self.assertEqual(report.call_args.kwargs["venue"], "revolut_x")

    async def test_the_scorecard_cost_defaults_to_035_when_no_spread_is_known(self):
        async def down(*args, **kwargs):
            raise ValueError("down")

        self.svc.coins = down
        with patch.object(sc, "report", return_value={}) as report:
            await self.svc.scorecard(1)
        self.assertEqual(report.call_args.kwargs["cost_pct"], 0.35)


class TransportService(rx.RevolutXService):
    """The real HTTP envelope with the wire replaced: what reaches the wire, and what comes back."""

    def __init__(self, replies):
        super().__init__()
        self.clock = lambda: NOW
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
        svc.generate_signing_key(1)
        svc.save_api_key(1, API_KEY)
        return svc

    async def test_a_private_call_sends_the_signed_headers_and_the_minified_body(self):
        svc = self.service((200, {"data": {"venue_order_id": OID}}, {}))
        body = {"client_order_id": "c", "symbol": "BTC-EUR"}
        result = await svc.private(1, "POST", "/1.0/orders", None, body)
        self.assertEqual(result, {"data": {"venue_order_id": OID}})
        ((method, url, headers, payload),) = svc.wire
        self.assertEqual(
            (method, url), ("POST", "https://revx.revolut.com/api/1.0/orders")
        )
        self.assertEqual(payload, b'{"client_order_id":"c","symbol":"BTC-EUR"}')
        self.assertEqual(headers["X-Revx-API-Key"], API_KEY)
        self.assertEqual(headers["X-Revx-Timestamp"], str(int(NOW * 1000)))
        public = serialization.load_pem_public_key(svc.status(1)["public_key"].encode())
        message = f"{int(NOW * 1000)}POST/api/1.0/orders".encode() + payload
        public.verify(base64.b64decode(headers["X-Revx-Signature"]), message)

    async def test_http_errors_are_classified_and_never_echo_the_api_key(self):
        svc = self.service(
            (429, {"message": "Rate Limit Exceeded"}, {"Retry-After": "5000"}),
            (503, {"message": "Something went wrong!"}, {}),
            (401, {"message": "bad key " + API_KEY}, {}),
        )
        with self.assertRaises(rx.RevolutXError) as caught:
            await svc.private(1, "GET", "/1.0/balances")
        self.assertTrue(caught.exception.definite)
        self.assertEqual(caught.exception.retry_after_ms, 5000)
        with self.assertRaises(rx.RevolutXError) as caught:
            await svc.private(1, "GET", "/1.0/balances")
        self.assertFalse(caught.exception.definite)
        with self.assertRaises(rx.RevolutXError) as caught:
            await svc.private(1, "GET", "/1.0/balances")
        self.assertTrue(caught.exception.definite)
        self.assertNotIn(API_KEY, str(caught.exception))

    async def test_a_full_order_over_the_wire_signs_every_call_and_applies_the_fill(
        self,
    ):
        cid = {}
        svc = self.service()

        async def script(method, url, headers, body):
            svc.wire.append((method, url, dict(headers), body))
            path = urlsplit(url).path
            if path.endswith("/1.0/balances"):
                return 200, FakeService().balances, {}
            if path.endswith("/public/configuration/pairs"):
                return 200, PAIRS, {}
            if path.endswith("/public/tickers"):
                return 200, tickers(), {}
            if "/order-book/" in path:
                b = books()["SUI"]
                return 200, {"data": b}, {}
            if method == "POST":
                cid["v"] = json.loads(body)["client_order_id"]
                return (
                    200,
                    {
                        "data": {
                            "venue_order_id": OID,
                            "client_order_id": cid["v"],
                            "state": "new",
                        }
                    },
                    {},
                )
            if path.endswith(f"/1.0/orders/{OID}"):
                return (
                    200,
                    {
                        "data": info(
                            "sell", "SUI", "1", "1", "3.0", "0.0027", cid=cid["v"]
                        )
                    },
                    {},
                )
            if "/orders/fills/" in path:
                return 200, {"data": [{"p": "3.0", "q": "1", "im": False}]}, {}
            raise AssertionError(path)

        svc._transport = script
        with patch.object(rx, "PUBLIC_GAP_SEC", 0):
            await svc.sync_from_account(1)
            svc.set_enabled(1, True)
            result = await svc.order(1, "sell", "SUI", "1")
        self.assertEqual(result["order"]["status"], "filled")
        self.assertEqual(rx._read_ledger(1)["positions"]["SUI"]["units"], "4")
        private = [w for w in svc.wire if "/public/" not in w[1]]
        self.assertGreaterEqual(len(private), 4)
        for method, url, headers, body in private:
            self.assertIn("X-Revx-Signature", headers)
            self.assertEqual(headers["X-Revx-API-Key"], API_KEY)
        public = [w for w in svc.wire if "/public/" in w[1]]
        for method, url, headers, body in public:
            self.assertNotIn("X-Revx-API-Key", headers)
            self.assertIn("region=EEA", url)

    async def test_public_reads_are_spaced_to_one_a_second(self):
        svc = self.service(
            (200, {"data": [], "metadata": {}}, {}),
            (200, {"data": [], "metadata": {}}, {}),
        )
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch("condor.revolut_x.asyncio.sleep", fake_sleep):
            await svc.public("/1.0/public/tickers")
            await svc.public("/1.0/public/tickers")
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)
        self.assertLessEqual(sleeps[0], rx.PUBLIC_GAP_SEC)

    async def test_a_public_429_is_reported_not_retried(self):
        svc = self.service(
            (429, {"message": "Rate Limit Exceeded"}, {"Retry-After": "1000"})
        )
        with self.assertRaises(ValueError) as caught:
            await svc.public("/1.0/public/tickers")
        self.assertIn("429", str(caught.exception))
        self.assertEqual(len(svc.wire), 1)

    async def test_the_local_rate_limit_refuses_without_sending(self):
        svc = self.service(*[(200, [], {})] * 3)
        with patch.object(rx, "RATE_LIMIT_PER_MINUTE", 2):
            await svc.private(1, "GET", "/1.0/balances")
            await svc.private(1, "GET", "/1.0/balances")
            with self.assertRaises(rx.RevolutXError) as caught:
                await svc.private(1, "GET", "/1.0/balances")
        self.assertTrue(caught.exception.definite)
        self.assertEqual(len(svc.wire), 2)

    async def test_the_daily_order_limit_refuses_before_anything_is_written(self):
        svc = FakeService()
        svc.generate_signing_key(1)
        svc.save_api_key(1, API_KEY)
        await svc.sync_from_account(1)
        svc.set_enabled(1, True)
        with patch.object(rx, "ORDERS_PER_DAY", 0):
            with self.assertRaises(ValueError) as caught:
                await svc.order(1, "sell", "SUI", "1")
        self.assertIn("Daily", str(caught.exception))
        self.assertIsNone(rx._read_ledger(1)["uncertain_order"])
        self.assertFalse([1 for m, p, q, b in svc.sent if m == "POST"])


if __name__ == "__main__":
    unittest.main()
