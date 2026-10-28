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
from unittest.mock import AsyncMock, patch

from condor import crypto_com_app as cdc


class AppTests(unittest.TestCase):
    def test_execution_is_gated_and_recorded_but_quotes_are_not_orders(self):
        from condor.runtime.danger import is_dangerous_tool_call, is_mutating_tool_call

        execute = {"tool": "crypto_com_app", "input": {"action": "execute"}}
        quote = {"tool": "crypto_com_app", "input": {"action": "quote"}}
        self.assertTrue(is_dangerous_tool_call(execute))
        self.assertTrue(is_mutating_tool_call(execute))
        self.assertFalse(is_dangerous_tool_call(quote))
        for action in ("predict", "scorecard"):
            logged = {"tool": "crypto_com_app", "input": {"action": action}}
            self.assertFalse(is_dangerous_tool_call(logged))
            self.assertFalse(is_mutating_tool_call(logged))

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
        ledger["cash_eur"] = "1000"
        cdc.check_purchase(ledger, "BTC", cdc.amount("166"), cdc.amount("1000"))
        for requested, equity in [("167", "1000"), ("0.1", "2")]:
            with self.assertRaises(ValueError):
                cdc.check_purchase(
                    ledger, "BTC", cdc.amount(requested), cdc.amount(equity)
                )
        ledger["cash_eur"] = "600"
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "BTC", cdc.amount("166"), cdc.amount("1000"))
        ledger = cdc.new_ledger()
        ledger["cash_eur"] = "1000"
        ledger["positions"] = {
            coin: {"units": "1", "cost_eur": "5"} for coin in ["BTC", "ETH", "SOL"]
        }
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "CRO", cdc.amount("1"), cdc.amount("1000"))
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "BTC", cdc.amount("1"), cdc.amount("1000"))

    def test_guards_are_lifted_below_the_threshold_but_the_floor_stays(self):
        ledger = cdc.new_ledger()
        cdc.check_purchase(ledger, "BTC", cdc.amount("12"), cdc.amount("30"))
        ledger["positions"] = {
            coin: {"units": "1", "cost_eur": "5"} for coin in ["BTC", "ETH", "SOL"]
        }
        cdc.check_purchase(ledger, "CRO", cdc.amount("5"), cdc.amount("43"))
        cdc.check_purchase(ledger, "BTC", cdc.amount("5"), cdc.amount("43"))
        cdc.check_purchase(ledger, "BTC", cdc.amount("394"), cdc.amount("999.99"))
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "BTC", cdc.amount("396"), cdc.amount("999.99"))
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "BTC", cdc.amount("0.1"), cdc.amount("2"))
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "CRO", cdc.amount("1"), cdc.amount("1000"))
        self.assertFalse(cdc.guards_active(cdc.amount("999.99")))
        self.assertTrue(cdc.guards_active(cdc.amount("1000")))

    def test_no_single_asset_above_40_percent_below_the_threshold(self):
        ledger = cdc.new_ledger()
        ledger["cash_eur"] = "26"
        ledger["positions"] = {
            "CRO": {"units": "216", "cost_eur": "9.98"},
            "SHIB": {"units": "1215909", "cost_eur": "5.06"},
            "ETH": {"units": "0.000933", "cost_eur": "1.91"},
        }
        equity = cdc.amount("42.95")
        cdc.check_purchase(ledger, "SOL", cdc.amount("17.18"), equity)
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "SOL", cdc.amount("17.19"), equity)
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "SOL", cdc.amount("26"), equity)
        cdc.check_purchase(ledger, "CRO", cdc.amount("7.2"), equity)
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "CRO", cdc.amount("7.3"), equity)

    def test_profits_increase_allocation_without_cumulative_loss_stop(self):
        ledger = cdc.new_ledger()
        ledger["cash_eur"] = "1270"
        cdc.check_purchase(ledger, "BTC", cdc.amount("200"), cdc.amount("1270"))

    def test_existing_position_is_not_topped_up_after_appreciation(self):
        ledger = cdc.new_ledger()
        ledger["cash_eur"] = "1000"
        ledger["positions"] = {"BTC": {"units": "1", "cost_eur": "1"}}
        with self.assertRaises(ValueError):
            cdc.check_purchase(ledger, "BTC", cdc.amount("1"), cdc.amount("1000"))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_portfolio_uses_live_account_values_not_bot_allocation(self):
        self.service.accounts = AsyncMock(
            return_value={
                "cash": [
                    {"currency": "EUR", "amount": {"amount": "30", "currency": "EUR"}}
                ],
                "crypto": [
                    {
                        "currency": "CRO",
                        "balance": {"amount": "200"},
                        "available": {"amount": "190"},
                        "native_balance": {"amount": "20", "currency": "EUR"},
                    }
                ],
            }
        )
        self.service.coins = AsyncMock(
            return_value={
                "coins": [
                    {
                        "rails_id": "BTC",
                        "price_native": {"amount": "100", "currency": "EUR"},
                        "price_usd": {"amount": "110", "currency": "USD"},
                    }
                ]
            }
        )
        result = await self.service.portfolio(1)
        self.service.accounts.assert_awaited_once_with(1)
        self.service.coins.assert_awaited_once_with(1, "BTC", "EUR")
        self.assertEqual(result["connector"], "Crypto.com App")
        self.assertAlmostEqual(result["total_usd"], 55)
        self.assertEqual(result["balances"][1]["total"], 200)
        self.assertEqual(result["balances"][1]["available"], 190)
        self.assertAlmostEqual(result["balances"][1]["usd_value"], 22)
        self.assertFalse(self.service.status(1)["enabled"])

    async def test_portfolio_refuses_missing_fx_or_wallet_valuation(self):
        self.service.accounts = AsyncMock(
            return_value={
                "cash": [
                    {"currency": "EUR", "amount": {"amount": "30", "currency": "EUR"}}
                ],
                "crypto": [],
            }
        )
        self.service.coins = AsyncMock(return_value={"coins": []})
        with self.assertRaises(ValueError):
            await self.service.portfolio(1)
        self.service.accounts.return_value = {
            "cash": [],
            "crypto": [{"currency": "CRO", "balance": {"amount": "200"}}],
        }
        with self.assertRaises(ValueError):
            await self.service.portfolio(1)

    async def test_coin_reads_feed_the_scorecard_without_ever_breaking_a_read(self):
        from condor import crypto_com_scorecard as sc

        ledger = cdc._read_ledger(1)
        ledger["positions"] = {"CRO": {"units": "216", "cost_eur": "9.98"}}
        cdc._write(cdc._root(1) / "ledger.json", ledger)

        def row(symbol, price):
            return {
                "symbol": symbol,
                "rank": 1,
                "price_native": {"currency": "EUR", "amount": price},
            }

        async def request(user_id, method, path, body=None):
            if "keyword=CRO" in path:
                return {"ok": True, "coins": [row("CRO", "0.05"), row("CROX", "9")]}
            return {"ok": True, "coins": [row("BTC", "73000")]}

        self.service.request = request
        await self.service.coins(1, "")
        self.assertEqual(sc.latest_price(1, "BTC")[1], 73000.0)
        # a held coin outside the top list is still priced, and only the exact symbol
        self.assertEqual(sc.latest_price(1, "CRO")[1], 0.05)
        self.assertIsNone(sc.latest_price(1, "CROX"))
        with patch.object(sc, "record_snapshot", side_effect=RuntimeError("db down")):
            result = await self.service.coins(1, "")
        self.assertEqual(result["coins"][0]["symbol"], "BTC")

    async def test_predict_looks_the_coin_up_then_logs_without_trading(self):
        calls = []

        async def request(user_id, method, path, body=None):
            calls.append((method, path))
            return {
                "ok": True,
                "coins": [
                    {
                        "symbol": "SOL",
                        "price_native": {"currency": "EUR", "amount": "92"},
                    }
                ],
            }

        self.service.request = request
        result = await self.service.predict(
            1, "sol", "trade", "10", "5", "24", "breakout"
        )
        self.assertTrue(result["recorded"])
        self.assertEqual(result["entry_eur"], 92.0)
        self.assertTrue(all(method == "GET" for method, _ in calls))
        again = await self.service.predict(1, "SOL", "trade", "10", "5", "24")
        self.assertFalse(again["recorded"])
        with self.assertRaises(ValueError):
            await self.service.predict(1, "SOL", "buy", "10", "5", "24")
        self.assertEqual(self.service.scorecard(1)["trade_calls"]["open"], 1)

    async def test_empty_keyword_lists_top_coins_without_descriptions(self):
        row = {
            "rank": 1,
            "symbol": "BTC",
            "rails_id": "BTC",
            "is_tradable": True,
            "price_native": {"currency": "EUR", "amount": "73000"},
            "description": "long text",
            "icon_image_url": "https://example.invalid/x.png",
        }
        self.service.request = AsyncMock(return_value={"ok": True, "coins": [row]})
        result = await self.service.coins(1, "")
        path = self.service.request.await_args.args[2]
        self.assertIn("page_size=15", path)
        self.assertNotIn("keyword", path)
        self.assertEqual(result["coins"][0]["symbol"], "BTC")
        self.assertNotIn("description", result["coins"][0])
        self.assertNotIn("icon_image_url", result["coins"][0])
        await self.service.coins(1, "SOL")
        self.assertIn("keyword=SOL", self.service.request.await_args.args[2])

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

    async def test_real_app_done_status_uses_verified_wallet_deltas(self):
        self.service.set_enabled(1, True)
        quotation = await self.service.quote(1, "purchase", "EUR", "BTC", "5")
        original = self.service.request
        filled = False

        async def app_contract(user_id, method, path, body=None):
            nonlocal filled
            if path.endswith("orders"):
                filled = True
                return {
                    "ok": True,
                    "transaction": {
                        "id": "t1",
                        "status": "done",
                        "kind": "crypto_purchase",
                    },
                }
            if path == "/v1/fiat-account":
                return {
                    "ok": True,
                    "account": {
                        "balances": [
                            {
                                "currency": "EUR",
                                "amount": {"amount": "25" if filled else "30"},
                            }
                        ]
                    },
                }
            if path == "/v1/crypto-account":
                return {
                    "ok": True,
                    "account": {
                        "wallets": [
                            {
                                "currency": "BTC",
                                "available": {"amount": "0.001" if filled else "0"},
                                "balance": {"amount": "0.001" if filled else "0"},
                            }
                        ]
                    },
                }
            return await original(user_id, method, path, body)

        self.service.request = app_contract
        await self.service.execute(1, quotation["id"])
        state = self.service.status(1)
        self.assertEqual(state["ledger"]["cash_eur"], "25")
        self.assertEqual(state["ledger"]["positions"]["BTC"]["units"], "0.001")

    async def test_wallet_deltas_wait_for_balances_to_settle(self):
        self.service.set_enabled(1, True)
        quotation = await self.service.quote(1, "purchase", "EUR", "BTC", "5")
        original = self.service.request
        filled = False
        reads_after_fill = 0

        async def lagging_wallets(user_id, method, path, body=None):
            nonlocal filled, reads_after_fill
            if path.endswith("orders"):
                filled = True
                return {
                    "ok": True,
                    "transaction": {
                        "id": "t1",
                        "status": "done",
                        "kind": "viban_purchase",
                    },
                }
            if path == "/v1/fiat-account":
                reads_after_fill += 1 if filled else 0
                settled = filled and reads_after_fill > 2
                return {
                    "ok": True,
                    "account": {
                        "balances": [
                            {
                                "currency": "EUR",
                                "amount": {"amount": "25" if settled else "30"},
                            }
                        ]
                    },
                }
            if path == "/v1/crypto-account":
                settled = filled and reads_after_fill > 2
                return {
                    "ok": True,
                    "account": {
                        "wallets": [
                            {
                                "currency": "BTC",
                                "available": {"amount": "0.001" if settled else "0"},
                                "balance": {"amount": "0.001" if settled else "0"},
                            }
                        ]
                    },
                }
            return await original(user_id, method, path, body)

        self.service.request = lagging_wallets
        with patch.object(cdc, "WALLET_SETTLE_WAIT_SEC", 0):
            await self.service.execute(1, quotation["id"])
        state = self.service.status(1)
        self.assertEqual(state["ledger"]["cash_eur"], "25")
        self.assertEqual(state["ledger"]["positions"]["BTC"]["units"], "0.001")
        self.assertIsNone(state["ledger"]["uncertain_order"])

    async def test_accounts_do_not_expose_zero_wallets_or_address_metadata(self):
        async def account_reply(user_id, method, path, body=None):
            return {
                "ok": True,
                "account": {
                    "balances": [],
                    "wallets": [
                        {
                            "currency": "BTC",
                            "available": {"amount": "0.001"},
                            "balance": {"amount": "0.001"},
                            "address": "private-address",
                        },
                        {
                            "currency": "ETH",
                            "available": {"amount": "0"},
                            "balance": {"amount": "0"},
                        },
                    ],
                },
            }

        self.service.request = account_reply
        accounts = await self.service.accounts(1)
        self.assertEqual(len(accounts["crypto"]), 1)
        self.assertNotIn("address", json.dumps(accounts))


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

    async def test_real_wallet_catalogue_larger_than_one_megabyte_is_accepted(self):
        class Response:
            status = 200

            @property
            def content(self):
                return self

            async def iter_chunked(self, size):
                yield b'{"ok":true,"wallet_metadata":"'
                for _ in range(24):
                    yield b"x" * 65536
                yield b'"}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session(Response):
            def request(self, *args, **kwargs):
                return Response()

        service = cdc.AppService()
        with (
            patch.object(service, "credentials", return_value=("key", "secret")),
            patch("aiohttp.ClientSession", return_value=Session()),
        ):
            self.assertTrue(
                (await service.request(1, "GET", "/v1/crypto-account"))["ok"]
            )


if __name__ == "__main__":
    unittest.main()
