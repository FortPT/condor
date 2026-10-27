"""App request contracts and trading limits; never contacts a financial account."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from condor import crypto_com_app as cdc


class AppTests(unittest.TestCase):
    def test_execution_is_gated_and_recorded_but_quotes_are_not_orders(self):
        from condor.runtime.danger import is_dangerous_tool_call, is_mutating_tool_call

        execute = {"tool": "crypto_com_app", "input": {"action": "execute"}}
        quote = {"tool": "crypto_com_app", "input": {"action": "quote"}}
        self.assertTrue(is_dangerous_tool_call(execute))
        self.assertTrue(is_mutating_tool_call(execute))
        self.assertFalse(is_dangerous_tool_call(quote))

    def test_signature_uses_exact_body_and_path_without_query(self):
        body = '{"from_currency":"EUR","to_currency":"BTC","from_amount":"5"}'
        expected = base64.b64encode(
            hmac.new(
                b"secret",
                ("123POST/v1/crypto-purchase/quotations" + body).encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        headers = cdc.signed_headers(
            "key",
            "secret",
            "POST",
            "/v1/crypto-purchase/quotations?q=ignored",
            body,
            "123",
        )
        self.assertEqual(headers["Cdc-Api-Signature"], expected)

    def test_amounts_refuse_nonpositive_nonfinite_and_non_decimal_input(self):
        for value in ["0", "-1", "NaN", "Infinity", "1e9", "1,5", "1" * 100]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                cdc.amount(value)
        self.assertEqual(str(cdc.amount("0.00000001")), "1E-8")

    def test_official_body_contracts(self):
        self.assertEqual(
            cdc.quote_body("exchange", "BTC", "ETH", "0.001"),
            {"from": "BTC", "to": "ETH", "from_amount": "0.001", "side": "buy"},
        )
        self.assertEqual(
            cdc.quote_body("sale", "BTC", "EUR", "0.001")["fixed_side"], "from"
        )
        with self.assertRaises(ValueError):
            cdc.quote_body("purchase", "EUR/path", "BTC", "5")

    def test_budget_reserve_position_limit_and_loss_stop(self):
        ledger = cdc.new_ledger()
        cdc.check_purchase(ledger, "BTC", cdc.amount("5"), cdc.amount("30"))
        for requested, equity in [("5.01", "30"), ("0.1", "2")]:
            with self.assertRaises(ValueError):
                cdc.check_purchase(
                    ledger, "BTC", cdc.amount(requested), cdc.amount(equity)
                )
        ledger["cash_eur"] = "19"
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "BTC", cdc.amount("5"), cdc.amount("30"))
        ledger = cdc.new_ledger()
        ledger["positions"] = {
            coin: {"units": "1", "cost_eur": "5"} for coin in ["BTC", "ETH", "SOL"]
        }
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "CRO", cdc.amount("1"), cdc.amount("30"))
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "BTC", cdc.amount("1"), cdc.amount("30"))

    def test_profits_increase_allocation_without_cumulative_loss_stop(self):
        ledger = cdc.new_ledger()
        ledger["cash_eur"] = "127"
        cdc.check_purchase(ledger, "BTC", cdc.amount("20"), cdc.amount("127"))

    def test_existing_position_is_not_topped_up_after_appreciation(self):
        ledger = cdc.new_ledger()
        ledger["positions"] = {"BTC": {"units": "1", "cost_eur": "1"}}
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "BTC", cdc.amount("1"), cdc.amount("30"))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CONDOR_DATA_DIR": self.tmp.name})
        self.env.start()
        self.service = cdc.AppService()
        self.service.save_credentials(1, "one-key", "one-secret")
        self.service.save_credentials(2, "two-key", "two-secret")
        self.calls = []

        async def request(user_id, method, path, body=None):
            self.calls.append((user_id, method, path, body))
            if path.endswith("quotations"):
                return {
                    "ok": True,
                    "quotation": {
                        "id": "q1",
                        "countdown": 15,
                        "from_amount": {"amount": "5", "currency": "EUR"},
                        "to_amount": {"amount": "0.001", "currency": "BTC"},
                    },
                }
            if path.endswith("orders"):
                return {
                    "ok": True,
                    "transaction": {
                        "id": "t1",
                        "status": "completed",
                        "from_amount": {"amount": "5", "currency": "EUR"},
                        "to_amount": {"amount": "0.001", "currency": "BTC"},
                    },
                }
            if path == "/v1/fiat-account":
                return {
                    "ok": True,
                    "account": {
                        "balances": [{"currency": "EUR", "amount": {"amount": "30"}}]
                    },
                }
            return {"ok": True, "account": {"wallets": []}}

        self.service.request = request

    async def asyncTearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def test_credentials_private_and_user_scoped(self):
        self.assertNotIn("key", json.dumps(self.service.status(1)))
        self.assertNotEqual(self.service.credentials(1), self.service.credentials(2))
        if os.name != "nt":
            self.assertEqual(cdc.credentials_path(1).stat().st_mode & 0o777, 0o600)

    async def test_unknown_expired_and_other_users_quote_never_submit(self):
        quote = await self.service.quote(1, "purchase", "EUR", "BTC", "5")
        for user_id, quote_id in [(2, quote["id"]), (1, "unknown")]:
            with self.assertRaises(ValueError):
                await self.service.execute(user_id, quote_id)
        self.service.quotes[(1, quote["id"])]["expires"] = 0
        with self.assertRaises(ValueError):
            await self.service.execute(1, quote["id"])
        self.assertFalse(any(call[2].endswith("orders") for call in self.calls))

    async def test_disabled_trading_and_rotation_invalidate_quotes(self):
        quote = await self.service.quote(1, "purchase", "EUR", "BTC", "5")
        with self.assertRaises(ValueError):
            await self.service.execute(1, quote["id"])
        self.service.save_credentials(1, "replacement", "secret")
        self.assertFalse(self.service.quotes)

    async def test_order_timeout_is_not_retried_and_blocks_more_trades(self):
        self.service.set_enabled(1, True)
        quote = await self.service.quote(1, "purchase", "EUR", "BTC", "5")
        original = self.service.request

        async def timeout(user_id, method, path, body=None):
            if path.endswith("orders"):
                self.calls.append((user_id, method, path, body))
                raise TimeoutError("upstream timeout")
            return await original(user_id, method, path, body)

        self.service.request = timeout
        with self.assertRaises(ValueError):
            await self.service.execute(1, quote["id"])
        with self.assertRaises(ValueError):
            await self.service.execute(1, quote["id"])
        self.assertEqual(sum(call[2].endswith("orders") for call in self.calls), 1)
        self.assertTrue(self.service.status(1)["ledger"]["uncertain_order"])
        self.assertFalse(self.service.status(1)["enabled"])

    async def test_quote_provider_amount_or_currency_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.service.quote(1, "purchase", "EUR", "ETH", "5")
        with self.assertRaises(ValueError):
            await self.service.quote(1, "purchase", "EUR", "BTC", "4")

    async def test_parallel_execution_submits_quote_once_and_records_allocation(self):
        self.service.set_enabled(1, True)
        quote = await self.service.quote(1, "purchase", "EUR", "BTC", "5")
        results = await asyncio.gather(
            self.service.execute(1, quote["id"]),
            self.service.execute(1, quote["id"]),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        ledger = self.service.status(1)["ledger"]
        self.assertEqual(ledger["cash_eur"], "25")
        self.assertEqual(ledger["positions"]["BTC"]["cost_eur"], "5")

    async def test_stop_while_order_in_flight_is_preserved(self):
        self.service.set_enabled(1, True)
        quote = await self.service.quote(1, "purchase", "EUR", "BTC", "5")
        original = self.service.request

        async def stop(user_id, method, path, body=None):
            if path.endswith("orders"):
                self.service.set_enabled(user_id, False)
            return await original(user_id, method, path, body)

        self.service.request = stop
        await self.service.execute(1, quote["id"])
        self.assertFalse(self.service.status(1)["enabled"])

    async def test_pending_transaction_does_not_become_a_filled_position(self):
        self.service.set_enabled(1, True)
        quote = await self.service.quote(1, "purchase", "EUR", "BTC", "5")
        original = self.service.request

        async def pending(user_id, method, path, body=None):
            if path.endswith("orders"):
                return {"ok": True, "transaction": {"id": "t1", "status": "pending"}}
            return await original(user_id, method, path, body)

        self.service.request = pending
        with self.assertRaises(ValueError):
            await self.service.execute(1, quote["id"])
        self.assertFalse(self.service.status(1)["enabled"])
        self.assertEqual(self.service.status(1)["ledger"]["positions"], {})

    async def test_rotation_during_quotation_does_not_rebind_old_account_quote(self):
        original = self.service.request

        async def rotate(user_id, method, path, body=None):
            if path.endswith("quotations"):
                self.service.save_credentials(user_id, "replacement-key", "new-secret")
            return await original(user_id, method, path, body)

        self.service.request = rotate
        with self.assertRaises(ValueError):
            await self.service.quote(1, "purchase", "EUR", "BTC", "5")

    async def test_reconciliation_uses_verified_fill_and_does_not_resubmit(self):
        self.service.set_enabled(1, True)
        quote = await self.service.quote(1, "purchase", "EUR", "BTC", "5")
        original = self.service.request
        filled = {
            "id": "t1",
            "status": "completed",
            "from_amount": quote["from_amount"],
            "to_amount": quote["to_amount"],
        }

        async def pending_then_history(user_id, method, path, body=None):
            if path.endswith("orders"):
                return {"ok": True, "transaction": {"id": "t1", "status": "pending"}}
            if path == "/v1/transactions":
                return {"ok": True, "transactions": [filled]}
            return await original(user_id, method, path, body)

        self.service.request = pending_then_history
        with self.assertRaises(ValueError):
            await self.service.execute(1, quote["id"])
        await self.service.reconcile(1)
        state = self.service.status(1)
        self.assertIsNone(state["ledger"]["uncertain_order"])
        self.assertEqual(state["ledger"]["cash_eur"], "25")
        self.assertFalse(state["enabled"])

    async def test_floor_valuation_persists_pause(self):
        self.service.set_enabled(1, True)
        ledger = cdc.new_ledger()
        ledger.update(cash_eur="2", enabled=True)
        cdc._write(cdc._root(1) / "ledger.json", ledger)
        self.assertEqual((await self.service.valuation(1))["equity_eur"], "2")
        self.assertFalse(self.service.status(1)["enabled"])

    async def test_missing_initial_funding_cannot_create_phantom_cash(self):
        self.service.set_enabled(1, True)
        quotation = await self.service.quote(1, "purchase", "EUR", "BTC", "5")
        original = self.service.request

        async def only_five_euros(user_id, method, path, body=None):
            if path == "/v1/fiat-account":
                return {
                    "ok": True,
                    "account": {
                        "balances": [{"currency": "EUR", "amount": {"amount": "5"}}]
                    },
                }
            return await original(user_id, method, path, body)

        self.service.request = only_five_euros
        with self.assertRaises(ValueError):
            await self.service.execute(1, quotation["id"])


class HttpContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_reply_and_redirects_do_not_leak_credentials(self):
        captured = []

        class Response:
            status = 200
            pieces = [b'{"ok":', b"true}"]

            @property
            def content(self):
                return self

            async def iter_chunked(self, size):
                for piece in self.pieces:
                    yield piece

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        response = Response()

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def request(self, method, url, **kwargs):
                captured.append((method, url, kwargs))
                return response

        service = cdc.AppService()
        with (
            patch.object(
                service, "credentials", return_value=("private_key", "private_secret")
            ),
            patch("aiohttp.ClientSession", return_value=Session()),
        ):
            self.assertEqual(
                await service.request(1, "GET", "/v1/fiat-account"), {"ok": True}
            )
            response.status = 302
            response.pieces = [b'{"ok":false,"error":"private_secret"}']
            with self.assertRaises(ValueError) as error:
                await service.request(1, "GET", "/v1/fiat-account")
            self.assertNotIn("private_secret", str(error.exception))
        self.assertFalse(captured[0][2]["allow_redirects"])
        self.assertEqual(len(captured), 2)


if __name__ == "__main__":
    unittest.main()
