"""Crypto.com App spot API; account keys never enter agent tool arguments.

Contracts: crypto-com/crypto-agent-trading, revision 92e80c5527312d0acdf769f16511ba3c6db1f2fe.
The EUR ledger owns only this bot's allocation, not the user's other holdings.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from condor import crypto_com_scorecard as scorecard
from condor.fsutil import atomic_write_json
from condor.paths import data_dir

KINDS = {
    "purchase": "crypto-purchase",
    "sale": "crypto-sales",
    "exchange": "crypto-exchange",
}


def amount(value: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{1,18}(?:\.\d{1,18})?", value
    ):
        raise ValueError(
            "Use a positive decimal amount, without exponent or separators"
        )
    result = Decimal(value)
    if result <= 0:
        raise ValueError("Amount must be positive")
    return result


def currency(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9_]{1,24}", value):
        raise ValueError("Invalid currency code")
    return value


def signed_headers(key, secret, method, path, body="", timestamp=None):
    stamp = timestamp or str(int(time.time() * 1000))
    payload = stamp + method.upper() + path.split("?")[0] + body
    signature = base64.b64encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "Cdc-Api-Key": key,
        "Cdc-Api-Timestamp": stamp,
        "Cdc-Api-Signature": signature,
        "Content-Type": "application/json",
        "User-Agent": "Condor-CryptoComApp/1.0",
    }


def quote_body(kind, source, target, quantity):
    if kind not in KINDS:
        raise ValueError("Use purchase, sale or exchange")
    currency(source)
    currency(target)
    amount(quantity)
    if source == target:
        raise ValueError("Choose two different currencies")
    if kind == "exchange":
        return {"from": source, "to": target, "from_amount": quantity, "side": "buy"}
    body = {"from_currency": source, "to_currency": target, "from_amount": quantity}
    if kind == "sale":
        body["fixed_side"] = "from"
    return body


def _root(user_id):
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid account owner")
    return data_dir() / "crypto_com_app" / str(user_id)


def credentials_path(user_id):
    return _root(user_id) / "credentials.json"


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, content)
    os.chmod(path, 0o600)


def new_ledger():
    return {
        "cash_eur": "30",
        "positions": {},
        "uncertain_order": None,
        "enabled": False,
        "orders": [],
        "floor_eur": "2",
    }


def _read_ledger(user_id):
    path = _root(user_id) / "ledger.json"
    return json.loads(path.read_text()) if path.exists() else new_ledger()


# Owner decision 2026-10-27: below this total bot equity the bot may use all of its
# money freely (no reserve, position count or per-position cap). Only the equity
# floor below stays. At or above it the original guards apply unchanged.
GUARD_THRESHOLD_EUR = Decimal("1000")
# Below the threshold one asset may still never hold more than this share of
# total bot equity, so the whole wallet cannot end up in a single coin.
MAX_ASSET_FRACTION = Decimal("0.4")
TOP_COINS = 15
# Wallet balances lag a "done" order by a few seconds (seen with a EUR 10 HYPE buy).
WALLET_SETTLE_TRIES = 5
WALLET_SETTLE_WAIT_SEC = 2.0
TOP_COIN_FIELDS = {
    "rank",
    "symbol",
    "rails_id",
    "name",
    "is_tradable",
    "token_type",
    "price_native",
    "percent_change_1h_native",
    "percent_change_24h_native",
    "percent_change_1w_native",
    "percent_change_30d_native",
    "volume_24h_native",
    "market_cap_native",
    "updated_at",
}


def guards_active(equity):
    return equity >= GUARD_THRESHOLD_EUR


def check_purchase(ledger, token, spend, equity):
    if equity <= Decimal("2"):
        raise ValueError("Bot equity reached the EUR 2 stop threshold")
    if not guards_active(equity):
        held = Decimal(ledger["positions"].get(token, {}).get("cost_eur", "0"))
        if held + spend > equity * MAX_ASSET_FRACTION:
            raise ValueError("Asset would exceed 40% of current bot equity")
        return
    positions = ledger["positions"]
    if token in positions:
        raise ValueError(
            "Close the existing position before opening another in this asset"
        )
    if token not in positions and len(positions) >= 3:
        raise ValueError("Maximum three open positions")
    existing = Decimal(positions.get(token, {}).get("cost_eur", "0"))
    if existing + spend > equity / 6:
        raise ValueError("Position would exceed one sixth of current bot equity")
    if Decimal(ledger["cash_eur"]) - spend < equity / 2:
        raise ValueError("Keep half of current bot equity as cash reserve")


class AppService:
    def __init__(self):
        self.quotes = {}
        self.locks = defaultdict(asyncio.Lock)
        self.calls = defaultdict(deque)

    def credentials(self, user_id):
        try:
            result = json.loads(credentials_path(user_id).read_text())
            return result["api_key"], result["api_secret"]
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            raise ValueError(
                "Connect an App Agent Key in Settings → Crypto.com App"
            ) from None

    def status(self, user_id):
        ledger = _read_ledger(user_id)
        return {
            "connected": credentials_path(user_id).exists(),
            "enabled": ledger["enabled"] and not ledger["uncertain_order"],
            "ledger": ledger,
            "limits": {
                "initial_eur": "30",
                "floor_eur": "2",
                "reserve_fraction": "0.5",
                "position_fraction": "1/6",
                "max_positions": 3,
                "guards_apply_from_eur": str(GUARD_THRESHOLD_EUR),
                "max_asset_fraction_below_threshold": str(MAX_ASSET_FRACTION),
            },
        }

    def save_credentials(self, user_id, key, secret):
        for value in [key, secret]:
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 4096
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Invalid Agent Key or secret")
        self.set_enabled(user_id, False)
        _write(
            credentials_path(user_id),
            {"api_key": key.strip(), "api_secret": secret.strip()},
        )
        self.quotes = {k: v for k, v in self.quotes.items() if k[0] != user_id}

    def set_enabled(self, user_id, enabled):
        ledger = _read_ledger(user_id)
        if enabled:
            self.credentials(user_id)
            if ledger["uncertain_order"]:
                raise ValueError(
                    "An order needs reconciliation before trading can resume"
                )
        ledger["enabled"] = bool(enabled)
        _write(_root(user_id) / "ledger.json", ledger)
        if not enabled:
            self.quotes = {k: v for k, v in self.quotes.items() if k[0] != user_id}
        return self.status(user_id)

    def disconnect(self, user_id):
        self.set_enabled(user_id, False)
        credentials_path(user_id).unlink(missing_ok=True)
        return self.status(user_id)

    async def request(self, user_id, method, path, body=None):
        import aiohttp

        key, secret = self.credentials(user_id)
        now = time.monotonic()
        calls = self.calls[user_id]
        while calls and now - calls[0] >= 60:
            calls.popleft()
        if len(calls) >= 90:
            raise ValueError("API rate limit: wait 60 seconds")
        calls.append(now)
        encoded = (
            json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            if body is not None
            else ""
        )
        headers = signed_headers(key, secret, method, path, encoded)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.request(
                    method,
                    "https://wapi.crypto.com" + path,
                    data=encoded.encode() if body is not None else None,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > 8 * 1024 * 1024:
                            raise ValueError("Crypto.com response too large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    data = json.loads(raw)
                    if (
                        response.status != 200
                        or not isinstance(data, dict)
                        or data.get("ok") is not True
                    ):
                        code = (
                            data.get("error", "request_rejected")
                            if isinstance(data, dict)
                            else "invalid_response"
                        )
                        # Error text is untrusted and can echo credentials; only a bounded code is shown.
                        safe = (
                            code
                            if isinstance(code, str)
                            and re.fullmatch(r"[a-z_]{1,64}", code)
                            else "request_rejected"
                        )
                        if key in safe or secret in safe:
                            safe = "request_rejected"
                        raise ValueError(
                            f"Crypto.com rejected the request (HTTP {response.status}, {safe})"
                        )
                    return data
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
            raise ValueError(
                "Crypto.com unavailable or returned an invalid response; order requests are never retried"
            ) from None

    async def accounts(self, user_id):
        cash = await self.request(user_id, "GET", "/v1/fiat-account")
        crypto = await self.request(user_id, "GET", "/v1/crypto-account")

        def funded(rows, fields, field):
            return [
                {name: row[name] for name in fields if name in row}
                for row in rows
                if self._money_units(row.get(field, {})) > 0
            ]

        return {
            "cash": funded(
                cash.get("account", {}).get("balances", []),
                {"currency", "amount"},
                "amount",
            ),
            "crypto": funded(
                crypto.get("account", {}).get("wallets", []),
                {"currency", "balance", "available", "native_balance"},
                "balance",
            ),
        }

    async def portfolio(self, user_id):
        accounts = await self.accounts(user_id)
        rows = []
        for wallet in accounts["cash"] + accounts["crypto"]:
            balance = wallet.get("amount", wallet.get("balance"))
            native = wallet.get("native_balance", wallet.get("amount"))
            if not isinstance(native, dict) or not native.get("currency"):
                raise ValueError("Crypto.com wallet valuation is unavailable")
            rows.append((wallet, balance, native))
        rates = {"USD": Decimal(1)}
        for denomination in sorted(
            {native["currency"] for _, _, native in rows} - {"USD"}
        ):
            # Both prices come from one provider snapshot; never assume EUR = USD.
            coins = await self.coins(user_id, "BTC", denomination)
            bitcoin = next(
                (
                    coin
                    for coin in coins.get("coins", [])
                    if coin.get("rails_id") == "BTC"
                ),
                {},
            )
            native_price = bitcoin.get("price_native", {})
            usd_price = bitcoin.get("price_usd", {})
            if (
                native_price.get("currency") != denomination
                or usd_price.get("currency") != "USD"
            ):
                raise ValueError("Crypto.com portfolio conversion is unavailable")
            native_units = self._money_units(native_price)
            usd_units = self._money_units(usd_price)
            if native_units <= 0 or usd_units <= 0:
                raise ValueError("Crypto.com portfolio conversion is unavailable")
            rates[denomination] = usd_units / native_units
        balances = []
        for wallet, balance, native in rows:
            rate = rates[native["currency"]]
            balances.append(
                {
                    "token": wallet["currency"],
                    "total": float(self._money_units(balance)),
                    "available": float(
                        self._money_units(wallet.get("available", balance))
                    ),
                    "usd_value": float(self._money_units(native) * rate),
                }
            )
        return {
            "connector": "Crypto.com App",
            "balances": balances,
            "total_usd": sum(row["usd_value"] for row in balances),
            "note": "Whole App account at indicative Crypto.com prices. Trade proceeds may differ due to spread and fees. Only the separate bot allocation may be traded.",
        }

    @staticmethod
    def _money_units(entry):
        try:
            value = Decimal(str(entry.get("amount", "0")))
        except (InvalidOperation, AttributeError):
            raise ValueError("Provider returned an invalid balance") from None
        if not value.is_finite() or value < 0:
            raise ValueError("Provider returned an invalid balance")
        return value

    def _wallet_total(self, accounts, token):
        for row in accounts["cash"]:
            if row.get("currency") == token:
                return self._money_units(row.get("amount", {}))
        for row in accounts["crypto"]:
            if row.get("currency") == token:
                return self._money_units(row.get("balance", row.get("available", {})))
        return Decimal(0)

    async def history(self, user_id):
        transactions = (await self.request(user_id, "GET", "/v1/transactions")).get(
            "transactions", []
        )
        ledger = _read_ledger(user_id)
        owned_ids = {order["transaction_id"] for order in ledger["orders"]}
        pending = ledger["uncertain_order"] or {}
        owned_ids.add(pending.get("transaction_id"))
        fields = {
            "id",
            "status",
            "kind",
            "amount",
            "native_amount",
            "from_amount",
            "to_amount",
            "fee",
            "created_at",
            "updated_at",
            "quotation_id",
        }
        result = []
        for item in transactions:
            meta = item.get("meta") or {}
            if (
                item.get("kind")
                not in {"crypto_purchase", "crypto_sale", "crypto_exchange"}
                and item.get("id") not in owned_ids
                and meta.get("exchange_quotation_uuid")
                != pending.get("quotation_id", "missing")
            ):
                continue
            row = {key: value for key, value in item.items() if key in fields}
            if meta.get("exchange_quotation_uuid"):
                row["quotation_id"] = meta["exchange_quotation_uuid"]
            result.append(row)
        return result[:50]

    async def _verified_transaction(self, user_id, quote, transaction, before):
        if transaction.get("status") not in {
            "completed",
            "success",
            "successful",
            "done",
        }:
            raise ValueError("Order has not reached a verified completed state")
        if "from_amount" in transaction and "to_amount" in transaction:
            return transaction
        if not before:
            raise ValueError("Wallet snapshot missing; manual reconciliation required")
        source, spent = self._quote_amount(quote, "from")
        target, received = self._quote_amount(quote, "to")
        # Balances settle shortly after the order reports done: re-read before refusing.
        for attempt in range(WALLET_SETTLE_TRIES):
            after = await self.accounts(user_id)
            debit = Decimal(before["source"]) - self._wallet_total(after, source)
            credit = self._wallet_total(after, target) - Decimal(before["target"])
            if debit == spent and credit == received:
                break
            if attempt + 1 < WALLET_SETTLE_TRIES:
                await asyncio.sleep(WALLET_SETTLE_WAIT_SEC)
        else:
            raise ValueError(
                "Final wallet changes do not match the quote; keep trading paused"
            )
        return {
            **transaction,
            "from_amount": {"currency": source, "amount": format(debit, "f")},
            "to_amount": {"currency": target, "amount": format(credit, "f")},
        }

    async def coins(self, user_id, keyword, native_currency="EUR"):
        if not isinstance(keyword, str) or len(keyword) > 100:
            raise ValueError("Enter a coin symbol or name")
        top = not keyword.strip()
        params = {
            "sort_by": "rank",
            "sort_direction": "asc",
            "native_currency": currency(native_currency),
            "page_size": TOP_COINS if top else 10,
        }
        query = urlencode(params if top else {"keyword": keyword, **params})
        data = await self.request(user_id, "GET", "/v1/crypto/coins?" + query)
        if top:
            # An empty keyword lists the top coins by rank; descriptions and icons stay out.
            data = {
                **data,
                "coins": [
                    {
                        key: value
                        for key, value in coin.items()
                        if key in TOP_COIN_FIELDS
                    }
                    for coin in data.get("coins", [])
                ],
            }
        await self._record_prices(user_id, data, top)
        return data

    async def _record_prices(self, user_id, data, top):
        """Feed the scorecard; it must never be able to break a coin read."""
        try:
            listed = data.get("coins", [])
            scorecard.record_snapshot(user_id, listed)
            if not top:
                return
            # Held coins and coins with an open call may sit outside the top list.
            wanted = set(_read_ledger(user_id)["positions"]) | set(
                scorecard.tracked_symbols(user_id)
            )
            for symbol in sorted(wanted - {coin.get("symbol") for coin in listed}):
                if scorecard.latest_price(user_id, symbol, time.time() - 400):
                    continue
                query = urlencode(
                    {
                        "keyword": symbol,
                        "sort_by": "rank",
                        "sort_direction": "asc",
                        "native_currency": "EUR",
                        "page_size": 10,
                    }
                )
                found = await self.request(user_id, "GET", "/v1/crypto/coins?" + query)
                scorecard.record_snapshot(
                    user_id,
                    [c for c in found.get("coins", []) if c.get("symbol") == symbol],
                )
        except Exception:  # noqa: BLE001 - analytics only
            pass

    async def predict(
        self, user_id, symbol, verdict, target_pct, stop_pct, horizon_hours, note=""
    ):
        symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
        if symbol and scorecard.latest_price(user_id, symbol) is None:
            await self.coins(user_id, symbol)
        return scorecard.record_prediction(
            user_id, symbol, verdict, target_pct, stop_pct, horizon_hours, note
        )

    def scorecard(self, user_id):
        return scorecard.report(user_id)

    @staticmethod
    def _quote_amount(quote, side):
        entry = quote.get(side + "_amount", {})
        if not isinstance(entry, dict):
            raise ValueError("Invalid quotation amount")
        return currency(entry.get("currency", "")), amount(entry.get("amount", ""))

    async def _quotation(self, user_id, kind, source, target, quantity):
        body = quote_body(kind, source, target, quantity)
        data = await self.request(
            user_id, "POST", f"/v1/{KINDS[kind]}/quotations", body
        )
        quote = data.get("quotation")
        if not isinstance(quote, dict):
            raise ValueError("Crypto.com returned no quotation")
        from_currency, from_amount = self._quote_amount(quote, "from")
        to_currency, _ = self._quote_amount(quote, "to")
        if (from_currency, to_currency, from_amount) != (
            source,
            target,
            amount(quantity),
        ):
            raise ValueError("Quotation does not match requested currencies and amount")
        if not isinstance(quote.get("id"), str) or not quote["id"]:
            raise ValueError("Quotation has no ID")
        return quote

    async def equity(self, user_id, ledger):
        total = Decimal(ledger["cash_eur"])
        for token, position in ledger["positions"].items():
            quote = await self._quotation(
                user_id, "sale", token, "EUR", position["units"]
            )
            _, proceeds = self._quote_amount(quote, "to")
            total += proceeds
        return total

    async def quote(self, user_id, kind, source, target, quantity):
        digest = self._credential_digest(user_id)
        quote = await self._quotation(user_id, kind, source, target, quantity)
        if digest != self._credential_digest(user_id):
            raise ValueError("Account changed while the quotation was being requested")
        countdown = quote.get("countdown")
        if (
            isinstance(countdown, bool)
            or not isinstance(countdown, (int, float))
            or not math.isfinite(countdown)
            or countdown <= 0
        ):
            raise ValueError("Quotation has no valid expiry")
        now = time.monotonic()
        self.quotes = {k: v for k, v in self.quotes.items() if v["expires"] > now}
        if len(self.quotes) >= 100:
            raise ValueError("Too many pending quotations")
        self.quotes[(user_id, quote["id"])] = {
            "kind": kind,
            "quote": quote,
            "expires": now + min(countdown, 60),
            "credential_digest": digest,
        }
        return quote

    def _credential_digest(self, user_id):
        self.credentials(user_id)
        return hashlib.sha256(credentials_path(user_id).read_bytes()).hexdigest()

    async def valuation(self, user_id):
        ledger = _read_ledger(user_id)
        if ledger["uncertain_order"]:
            raise ValueError(
                "Reconcile the uncertain order before valuing the bot allocation"
            )
        equity = await self.equity(user_id, ledger)
        if equity <= Decimal("2"):
            self.set_enabled(user_id, False)
        return {
            "equity_eur": str(equity),
            "new_position_limit_eur": str(
                equity / 6 if guards_active(equity) else equity * MAX_ASSET_FRACTION
            ),
            "minimum_cash_reserve_eur": str(
                equity / 2 if guards_active(equity) else Decimal(0)
            ),
            **self.status(user_id),
        }

    async def execute(self, user_id, quote_id):
        async with self.locks[user_id]:
            entry = self.quotes.pop((user_id, quote_id), None)
            if entry is None or entry["expires"] <= time.monotonic():
                raise ValueError(
                    "Quotation expired, already used or belongs to another user"
                )
            ledger = _read_ledger(user_id)
            if not ledger["enabled"] or ledger["uncertain_order"]:
                raise ValueError("Trading is paused")
            kind, quote = entry["kind"], entry["quote"]
            source, spent = self._quote_amount(quote, "from")
            target, received = self._quote_amount(quote, "to")
            # A failed valuation of another asset must never block an exit.
            equity = await self.equity(user_id, ledger) if kind != "sale" else None
            if equity is not None and equity <= Decimal("2"):
                self.set_enabled(user_id, False)
                raise ValueError(
                    "Bot equity reached the EUR 2 stop threshold; trading paused"
                )
            if kind == "purchase":
                if source != "EUR":
                    raise ValueError("Bot purchases must use the allocated EUR cash")
                check_purchase(ledger, target, spent, equity)
            else:
                position = ledger["positions"].get(source)
                if not position or spent > Decimal(position["units"]):
                    raise ValueError(
                        "Cannot sell or exchange assets outside the bot allocation"
                    )
                if kind == "sale" and target != "EUR":
                    raise ValueError("Bot sales must return EUR")
                if kind == "exchange":
                    if equity <= Decimal("2"):
                        raise ValueError("Bot equity reached the EUR 2 stop threshold")
                    old_cost = (
                        Decimal(position["cost_eur"])
                        * spent
                        / Decimal(position["units"])
                    )
                    if not guards_active(equity):
                        held = Decimal(
                            ledger["positions"].get(target, {}).get("cost_eur", "0")
                        )
                        if held + old_cost > equity * MAX_ASSET_FRACTION:
                            raise ValueError(
                                "Exchange would exceed 40% of current bot equity"
                            )
                    if guards_active(equity) and (
                        target in ledger["positions"]
                        or old_cost > equity / 6
                        or (
                            spent < Decimal(position["units"])
                            and len(ledger["positions"]) >= 3
                        )
                    ):
                        raise ValueError(
                            "Exchange would exceed the position allocation"
                        )
            accounts = await self.accounts(user_id)
            if source == "EUR":
                available = next(
                    (
                        Decimal(b["amount"]["amount"])
                        for b in accounts["cash"]
                        if b.get("currency") == source
                    ),
                    Decimal(0),
                )
            else:
                available = next(
                    (
                        Decimal(b.get("available", {}).get("amount", "0"))
                        for b in accounts["crypto"]
                        if b.get("currency") == source
                    ),
                    Decimal(0),
                )
            if not available.is_finite() or available < spent:
                raise ValueError("Insufficient available account balance")
            if kind == "purchase" and available < Decimal(ledger["cash_eur"]):
                raise ValueError(
                    "EUR account cash does not back the bot allocation; fund or reconcile it first"
                )
            # Account checks await I/O: a stop/rotation may have arrived meanwhile.
            current = _read_ledger(user_id)
            if (
                not current["enabled"]
                or entry["expires"] <= time.monotonic()
                or entry["credential_digest"] != self._credential_digest(user_id)
            ):
                raise ValueError("Trading paused or quotation expired during checks")
            recent = [o for o in ledger["orders"] if time.time() - o["time"] < 60]
            if len(recent) >= 10:
                raise ValueError("Maximum ten orders per minute")
            ledger["uncertain_order"] = {
                "quotation_id": quote_id,
                "kind": kind,
                "time": time.time(),
                "quote": quote,
                "credential_digest": entry["credential_digest"],
                "before_balances": {
                    "source": str(self._wallet_total(accounts, source)),
                    "target": str(self._wallet_total(accounts, target)),
                },
            }
            _write(_root(user_id) / "ledger.json", ledger)
            body = {"quotation_id": quote_id}
            if kind == "exchange":
                body["side"] = "buy"
            try:
                data = await self.request(
                    user_id, "POST", f"/v1/{KINDS[kind]}/orders", body
                )
                transaction = data.get("transaction")
                if isinstance(transaction, dict) and transaction.get("id"):
                    ledger["uncertain_order"]["transaction_id"] = transaction["id"]
                    ledger["enabled"] = _read_ledger(user_id)["enabled"]
                    _write(_root(user_id) / "ledger.json", ledger)
                if not isinstance(transaction, dict) or not transaction.get("id"):
                    raise ValueError("Order response has no transaction ID")
                transaction = await self._verified_transaction(
                    user_id,
                    quote,
                    transaction,
                    ledger["uncertain_order"].get("before_balances"),
                )
            except Exception:
                raise ValueError(
                    "Order outcome needs reconciliation. Trading paused; inspect Crypto.com transaction history. Do not resubmit."
                ) from None
            self._apply_fill(ledger, kind, quote, transaction)
            # A stop or key rotation while the order was in flight stays effective.
            ledger["enabled"] = (
                credentials_path(user_id).exists() and _read_ledger(user_id)["enabled"]
            )
            _write(_root(user_id) / "ledger.json", ledger)
            return {"transaction": transaction, "status": self.status(user_id)}

    def _apply_fill(self, ledger, kind, quote, transaction):
        source, spent = self._quote_amount(quote, "from")
        target, _ = self._quote_amount(quote, "to")
        if transaction.get("status") not in {
            "completed",
            "success",
            "successful",
            "done",
        }:
            raise ValueError("Order has not reached a verified completed state")
        actual_source, actual_spent = self._quote_amount(transaction, "from")
        actual_target, received = self._quote_amount(transaction, "to")
        if (actual_source, actual_target) != (source, target) or actual_spent != spent:
            raise ValueError("Final debit differs from the accepted quote")
        if kind == "purchase":
            ledger["cash_eur"] = str(Decimal(ledger["cash_eur"]) - spent)
            previous = ledger["positions"].get(target, {"units": "0", "cost_eur": "0"})
            ledger["positions"][target] = {
                "units": format(Decimal(previous["units"]) + received, "f"),
                "cost_eur": str(Decimal(previous["cost_eur"]) + spent),
            }
        else:
            previous = ledger["positions"][source]
            fraction = spent / Decimal(previous["units"])
            cost = Decimal(previous["cost_eur"]) * fraction
            if fraction == 1:
                del ledger["positions"][source]
            else:
                previous["units"] = format(Decimal(previous["units"]) - spent, "f")
                previous["cost_eur"] = str(Decimal(previous["cost_eur"]) - cost)
            if kind == "sale":
                ledger["cash_eur"] = str(Decimal(ledger["cash_eur"]) + received)
            else:
                ledger["positions"][target] = {
                    "units": format(received, "f"),
                    "cost_eur": str(cost),
                }
        ledger["uncertain_order"] = None
        ledger["orders"] = (
            ledger["orders"]
            + [
                {
                    "time": time.time(),
                    "kind": kind,
                    "quotation_id": quote["id"],
                    "transaction_id": transaction["id"],
                    "quote": quote,
                }
            ]
        )[-200:]
        return ledger

    async def reconcile(self, user_id):
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            pending = ledger["uncertain_order"]
            if not pending:
                return self.status(user_id)
            if pending["credential_digest"] != self._credential_digest(user_id):
                raise ValueError(
                    "Reconnect the original account before reconciling this order"
                )
            transactions = await self.history(user_id)
            transaction = next(
                (
                    t
                    for t in transactions
                    if (
                        pending.get("transaction_id")
                        and t.get("id") == pending["transaction_id"]
                    )
                    or (t.get("quotation_id") == pending["quotation_id"])
                ),
                None,
            )
            if not transaction:
                raise ValueError(
                    "Order not found in recent provider history; keep trading paused"
                )
            if transaction.get("status") in {
                "failed",
                "rejected",
                "cancelled",
                "canceled",
                "expired",
            }:
                ledger["uncertain_order"] = None
            else:
                transaction = await self._verified_transaction(
                    user_id,
                    pending["quote"],
                    transaction,
                    pending.get("before_balances"),
                )
                if pending["credential_digest"] != self._credential_digest(user_id):
                    raise ValueError("Account changed during reconciliation")
                self._apply_fill(ledger, pending["kind"], pending["quote"], transaction)
            ledger["enabled"] = False
            _write(_root(user_id) / "ledger.json", ledger)
            return self.status(user_id)


service = AppService()
