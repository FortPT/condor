"""The Gateway's Solana wallet, read from the chain: its deposits and what it has earned.

Condor tracks deposits only for its native venues, and the Gateway wallet is not one, so SOL sent
into it would read as profit. A deposit here is a transaction on the wallet that the wallet did not
sign (the loop's own swaps are signed by it), found with the public Solana RPC and valued once, at
the SOL price when it was first seen, then remembered. Read-only: no key is touched and nothing is
sent.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable

from condor.fsutil import atomic_write_json
from condor.paths import data_dir

logger = logging.getLogger(__name__)

SOLANA_CONNECTOR = "solana-mainnet-beta"
#: USDC and USDT: a transfer of these in is worth its face value in USD.
STABLE_MINTS = frozenset(
    {
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    }
)
#: Transactions looked up per refresh, so a wallet with a long history is caught up over a few reads.
MAX_NEW_TX = 5
SIGNATURE_LIMIT = 100

Rpc = Callable[[str, list], Awaitable[Any]]


def rpc_url() -> str:
    return os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"


def state_path():
    return data_dir() / "solana_wallet" / "state.json"


def load_state(address: str) -> dict:
    import json

    try:
        state = json.loads(state_path().read_text())
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict) or state.get("address") != address:
        return {"address": address, "txs": {}}
    state.setdefault("txs", {})
    return state


def save_state(state: dict) -> None:
    try:
        atomic_write_json(state_path(), state)
    except OSError:
        logger.warning("Could not save the Solana wallet state", exc_info=True)


async def http_rpc(method: str, params: list) -> Any:
    """One JSON-RPC call to the public Solana endpoint."""
    import aiohttp

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20)
    ) as session:
        async with session.post(
            rpc_url(),
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        ) as response:
            response.raise_for_status()
            return (await response.json()).get("result")


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def deposit_usd(tx: dict, address: str, sol_price: float | None) -> float | None:
    """The USD value of what a transaction put into ``address``, or ``None`` if it cannot be valued yet.

    A transaction the wallet signed itself is not a deposit and is worth 0 here. Native SOL is valued
    at ``sol_price``; USDC and USDT at face value. A negative result is a transfer out that the
    wallet did not sign, which the same rule nets off.
    """
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = [
        k.get("pubkey") if isinstance(k, dict) else k
        for k in message.get("accountKeys") or []
    ]
    if not keys or keys[0] == address:
        return 0.0
    meta = tx.get("meta") or {}
    usd = 0.0
    if address in keys:
        index = keys.index(address)
        delta = (
            _number(meta.get("postBalances", [0] * (index + 1))[index])
            - _number(meta.get("preBalances", [0] * (index + 1))[index])
        ) / 1e9
        if delta and not sol_price:
            return None
        usd += delta * (sol_price or 0.0)
    pre = {
        b.get("accountIndex"): b
        for b in meta.get("preTokenBalances") or []
        if b.get("owner") == address
    }
    post = {
        b.get("accountIndex"): b
        for b in meta.get("postTokenBalances") or []
        if b.get("owner") == address
    }
    for index, balance in post.items():
        if balance.get("mint") in STABLE_MINTS:
            now = _number((balance.get("uiTokenAmount") or {}).get("uiAmount"))
            before = _number(
                ((pre.get(index) or {}).get("uiTokenAmount") or {}).get("uiAmount")
            )
            usd += now - before
    return round(usd, 4)


async def refresh_deposits(
    address: str, sol_price: float | None, rpc: Rpc | None = None
) -> dict:
    """Read new transactions on ``address`` and remember each one's deposit value. Best effort.

    A failing RPC leaves what is already known; a transaction that cannot be valued yet (no SOL price)
    is left for the next refresh rather than recorded as zero.
    """
    rpc = rpc or http_rpc
    state = load_state(address)
    txs = state["txs"]
    try:
        signatures = (
            await rpc("getSignaturesForAddress", [address, {"limit": SIGNATURE_LIMIT}])
            or []
        )
        fetched = 0
        for item in signatures:
            signature = item.get("signature")
            if not signature or item.get("err") is not None or signature in txs:
                continue
            if fetched >= MAX_NEW_TX:
                break
            fetched += 1
            tx = await rpc(
                "getTransaction",
                [
                    signature,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                ],
            )
            if not tx:
                continue
            usd = deposit_usd(tx, address, sol_price)
            if usd is None:
                continue
            txs[signature] = {"ts": tx.get("blockTime"), "usd": usd}
            if usd == 0.0:
                txs[signature]["own"] = True
    except Exception:
        logger.warning(
            "Solana RPC read failed; using the deposits already known", exc_info=True
        )
    save_state(state)
    return state


def deposits_of(state: dict) -> list[dict]:
    """The recorded deposits and withdrawals, newest first, without the wallet's own transactions."""
    rows = [
        {"signature": signature, "ts": tx.get("ts"), "usd": tx.get("usd")}
        for signature, tx in (state.get("txs") or {}).items()
        if tx.get("ts") and tx.get("usd")
    ]
    return sorted(rows, key=lambda row: row["ts"], reverse=True)


def overview(state: dict, value_usd: float, now: float | None = None) -> dict:
    """The wallet's value, deposits and result, net of deposits, with today's result.

    ``today`` is the change in (value minus deposits) since the first read of the UTC day; the first
    ever read counts the whole history as before today, so a wallet opened today starts at zero.
    """
    now = time.time() if now is None else now
    deposits = deposits_of(state)
    net_deposits = round(sum(row["usd"] for row in deposits), 4)
    since_start = value_usd - net_deposits
    date = time.strftime("%Y-%m-%d", time.gmtime(now))
    day = state.get("day") or {}
    if day.get("date") != date:
        day = {"date": date, "base": since_start if day else 0.0}
        state["day"] = day
    return {
        "address": state.get("address"),
        "value_usd": round(value_usd, 4),
        "deposits_usd": net_deposits,
        "since_start_usd": round(since_start, 4),
        "today_usd": round(since_start - day["base"], 4),
        "deposits": deposits,
    }


def wallet_from_portfolio(state: Any) -> tuple[float, float | None]:
    """``(value in USD, SOL price in USD)`` of the Solana connector in a ``portfolio.get_state`` payload.

    ``{account: {connector: [rows]}}``, a row carrying ``token``, ``units`` and ``value`` (USD). The
    price is ``None`` when the wallet holds no SOL, so a deposit that needs it waits.
    """
    value, price = 0.0, None
    if not isinstance(state, dict):
        return value, price
    for account in state.values():
        rows = account.get(SOLANA_CONNECTOR) if isinstance(account, dict) else None
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            worth = _number(row.get("value"))
            value += worth
            units = _number(row.get("units"))
            if str(row.get("token", "")).upper() == "SOL" and units > 0 and worth > 0:
                price = worth / units
    return round(value, 4), price
