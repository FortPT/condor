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


def check_purchase(ledger, token, spend, equity):
    if equity <= Decimal("2"):
        raise ValueError("Bot equity reached the EUR 2 stop threshold")
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
                        if size > 1_048_576:
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
        return {
            "cash": cash.get("account", {}).get("balances", []),
            "crypto": crypto.get("account", {}).get("wallets", []),
        }

    async def history(self, user_id):
        return (await self.request(user_id, "GET", "/v1/transactions")).get(
            "transactions", []
        )[:50]

    async def coins(self, user_id, keyword):
        if not isinstance(keyword, str) or not 1 <= len(keyword) <= 100:
            raise ValueError("Enter a coin symbol or name")
        query = urlencode(
            {
                "keyword": keyword,
                "sort_by": "rank",
                "sort_direction": "asc",
                "native_currency": "EUR",
                "page_size": 10,
            }
        )
        return await self.request(user_id, "GET", "/v1/crypto/coins?" + query)

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
            "new_position_limit_eur": str(equity / 6),
            "minimum_cash_reserve_eur": str(equity / 2),
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
                    if (
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
                if transaction.get("status") not in {
                    "completed",
                    "success",
                    "successful",
                }:
                    raise ValueError("Order has not reached a verified completed state")
                actual_source, actual_spent = self._quote_amount(transaction, "from")
                actual_target, actual_received = self._quote_amount(transaction, "to")
                if (actual_source, actual_target) != (
                    source,
                    target,
                ) or actual_spent != spent:
                    raise ValueError("Final debit differs from the accepted quote")
                received = actual_received
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
        if transaction.get("status") not in {"completed", "success", "successful"}:
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
                self._apply_fill(ledger, pending["kind"], pending["quote"], transaction)
            ledger["enabled"] = False
            _write(_root(user_id) / "ledger.json", ledger)
            return self.status(user_id)


service = AppService()
