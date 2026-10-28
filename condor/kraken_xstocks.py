"""Live Kraken xStocks (tokenized US stocks) trading on Kraken's spot REST API.

Account keys never enter agent tool arguments. The bot only ever trades the online
``<TICKER>xUSD`` pairs of Kraken's ``tokenized_asset`` class, in USD, and only while
the regular US session is open. Orders are immediate-or-cancel limits priced through
the touch, and every fill is read back from Kraken, never assumed.

The ledger owns only this bot's allocation, in USD, keyed by the plain ticker
(``AAPL``) while Kraken's own names are ``AAPLx`` (asset) and ``AAPLxUSD`` (pair). It
is seeded from the Kraken account by :meth:`KrakenXStocksService.sync_from_account`.

VERIFIED (public data, via the paper test in :mod:`condor.xstocks_paper`): the pair
list, ``asset_class=tokenized_asset`` on the public endpoints, tick and lot sizes.

NOT VERIFIED against a real account (comments say so where it matters): the request
signature, the field names of ``AddOrder`` / ``QueryOrders`` / ``ClosedOrders`` /
``BalanceEx`` / ``TradeVolume`` for tokenized pairs, whether ``asset_class`` belongs
on ``AddOrder``, and whether a Kraken+ zero-fee plan covers API orders. The first real
order must be tiny; see ``docs/kraken-xstocks.md``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections import defaultdict, deque
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from urllib.parse import urlencode

from condor import crypto_com_scorecard as scorecard
from condor import xstocks_paper
from condor.fsutil import atomic_write_json
from condor.paths import data_dir

VENUE = "kraken_xstocks"
BASE_URL = "https://api.kraken.com"
ASSET_CLASS = xstocks_paper.ASSET_CLASS
QUOTE = "USD"
# Kraken's name for US dollars in balances; "USD" is accepted defensively.
USD_ASSETS = ("ZUSD", "USD")
SIDES = ("buy", "sell")
# QueryOrders / ClosedOrders `status`: pending | open | closed | canceled | expired.
FINAL_STATUSES = {"closed", "canceled", "cancelled", "expired"}
_UNCERTAIN = (
    "Order outcome needs reconciliation. Trading paused; check the Kraken order "
    "history. Do not resubmit."
)

# Below this total bot equity (USD) the bot may use all of its money freely; only
# the equity floor and the single-asset cap apply. At or above it the reserve,
# position-count and one-sixth rules apply as well (same rules as the Exchange bot).
GUARD_THRESHOLD = Decimal("1000")
MAX_ASSET_FRACTION = Decimal("0.4")
FLOOR = Decimal("2")
TOP_COINS = 40
# Orders are immediate-or-cancel limits priced this far through the touch, so a fast
# market cannot fill them at a worse price than the caller accepted.
DEFAULT_SLIPPAGE_PCT = Decimal("0.3")
MAX_SLIPPAGE_PCT = Decimal("2")
MAX_SPREAD_PCT = Decimal("1")
# A buy must leave this much cash beyond its notional for the fee.
FEE_HEADROOM = Decimal("0.01")
ORDER_POLLS = 10
ORDER_POLL_WAIT_SEC = 1.0
# Kraken's private-call counter (starter tier: 15, decaying 0.33 a second) is the
# binding limit; stay well inside it.
RATE_LIMIT_PER_MINUTE = 18
# Round-trip cost when the account's fee is unknown: the paper test's 0.40% taker fee
# each way plus a little spread. With a known taker fee the scorecard uses
# 2 * fee + SPREAD_ALLOWANCE_PCT.
DEFAULT_ROUND_TRIP_COST_PCT = 0.85
SPREAD_ALLOWANCE_PCT = 0.05
MAX_BYTES = 4 * 1024 * 1024

# An error Kraken raised BEFORE an order could exist. "EOrder:" covers validation,
# funds and minimums; "EQuery:" unknown pair/asset; "EAPI:Invalid" key, signature,
# nonce or arguments; "EGeneral:Permission denied"/"Invalid arguments"; and a rate
# limit stops the request at the door. Anything else, EService: (busy, unavailable,
# market modes), EGeneral:Internal error, EGeneral:Temporary lockout, an unknown
# code, HTTP 5xx or a transport failure, may have created the order.
DEFINITE_PREFIXES = (
    "EAPI:Invalid",
    "EAPI:Rate limit",
    "EGeneral:Permission denied",
    "EGeneral:Invalid",
    "EOrder:",
    "EQuery:",
)


def amount(value) -> Decimal:
    text = value if isinstance(value, str) else str(value)
    if not re.fullmatch(r"\d{1,18}(?:\.\d{1,18})?", text):
        raise ValueError(
            "Use a positive decimal amount, without exponent or separators"
        )
    result = Decimal(text)
    if result <= 0:
        raise ValueError("Amount must be positive")
    return result


def symbol_of(value: str) -> str:
    return xstocks_paper.symbol_of(value)


def fmt(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _decimal(value, default=None):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return default
    return result if result.is_finite() else default


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    down = _round_down(value, step)
    return down if down == value else down + step


# ── signing ──


def sign(secret: str, urlpath: str, nonce, postdata: str) -> str:
    """Kraken's ``API-Sign``: base64(HMAC-SHA512(b64decode(secret), path + SHA256(nonce + body)))."""
    digest = hashlib.sha256((str(nonce) + postdata).encode()).digest()
    mac = hmac.new(base64.b64decode(secret), urlpath.encode() + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def signed_request(api_key, secret, method, params, nonce):
    """``(urlpath, urlencoded body, headers)`` for a private call; ``nonce`` rides in the body."""
    if not re.fullmatch(r"[A-Za-z]{3,30}", method):
        raise ValueError("Invalid Kraken method")
    urlpath = "/0/private/" + method
    postdata = urlencode({"nonce": nonce, **params})
    return (
        urlpath,
        postdata,
        {
            "API-Key": api_key,
            "API-Sign": sign(secret, urlpath, nonce, postdata),
        },
    )


# ── storage ──


def _root(user_id):
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid account owner")
    return data_dir() / VENUE / str(user_id)


def credentials_path(user_id):
    return _root(user_id) / "credentials.json"


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, content)
    os.chmod(path, 0o600)


def new_ledger():
    return {
        "quote": QUOTE,
        "cash": "0",
        "positions": {},
        "uncertain_order": None,
        "enabled": False,
        "orders": [],
        "floor": str(FLOOR),
        "synced": False,
    }


def _read_ledger(user_id):
    path = _root(user_id) / "ledger.json"
    return json.loads(path.read_text()) if path.exists() else new_ledger()


def guards_active(equity):
    return equity >= GUARD_THRESHOLD


def check_purchase(ledger, symbol, spend, equity):
    if equity <= FLOOR:
        raise ValueError(f"Bot equity reached the {FLOOR} stop threshold")
    positions = ledger["positions"]
    held = Decimal(positions.get(symbol, {}).get("cost", "0"))
    if not guards_active(equity):
        if held + spend > equity * MAX_ASSET_FRACTION:
            raise ValueError("Asset would exceed 40% of current bot equity")
        return
    if symbol in positions:
        raise ValueError(
            "Close the existing position before opening another in this asset"
        )
    if len(positions) >= 3:
        raise ValueError("Maximum three open positions")
    if spend > equity / 6:
        raise ValueError("Position would exceed one sixth of current bot equity")
    if Decimal(ledger["cash"]) - spend < equity / 2:
        raise ValueError("Keep half of current bot equity as cash reserve")


class KrakenError(ValueError):
    """Kraken answered with an ``error`` list; only a bounded, sanitized text is kept."""

    def __init__(self, status, errors, secret_values=()):
        text = ";".join(str(e) for e in errors)
        # Error text is untrusted and can echo credentials; keep it short and plain.
        if any(v and v in text for v in secret_values):
            text = ""
        text = re.sub(r"[^A-Za-z0-9 _.,:;()/-]", "", text)[:120]
        super().__init__(
            f"Kraken rejected the request (HTTP {status}"
            + (f": {text}" if text else "")
            + ")"
        )
        self.status = status
        self.errors = [str(e)[:80] for e in errors]
        # A refusal that cannot have created an order: Kraken processed the request
        # and said no, with a code known to precede order creation. Everything else
        # (a server-side failure, an unknown or empty code) may still have created it.
        self.definite = (
            status < 500
            and bool(errors)
            and all(str(e).startswith(DEFINITE_PREFIXES) for e in errors)
        )


def _clean(text) -> str:
    return re.sub(r"[^A-Za-z0-9 _.,:;()/-]", "", str(text))[:120]


class KrakenXStocksService:
    def __init__(self):
        self.locks = defaultdict(asyncio.Lock)
        self.calls = defaultdict(deque)
        self.paper = xstocks_paper.service
        self.clock = time.time
        self._fees = {}
        self._last_nonce = 0

    # ── credentials and switches ──

    def _credential_file(self, user_id):
        try:
            return json.loads(credentials_path(user_id).read_text())
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            raise ValueError(
                "Connect a Kraken API key in Settings → Kraken xStocks"
            ) from None

    def credentials(self, user_id):
        data = self._credential_file(user_id)
        try:
            return data["api_key"], data["api_secret"]
        except KeyError:
            raise ValueError(
                "Connect a Kraken API key in Settings → Kraken xStocks"
            ) from None

    def _market(self):
        return xstocks_paper.us_market_open(self.clock())

    def status(self, user_id):
        ledger = _read_ledger(user_id)
        return {
            "connected": credentials_path(user_id).exists(),
            "venue": "Kraken xStocks",
            "quote": QUOTE,
            "enabled": ledger["enabled"] and not ledger["uncertain_order"],
            "ledger": ledger,
            "market": self._market(),
            "limits": {
                "floor": str(FLOOR),
                "reserve_fraction": "0.5",
                "position_fraction": "1/6",
                "max_positions": 3,
                "guards_apply_from": str(GUARD_THRESHOLD),
                "max_asset_fraction_below_threshold": str(MAX_ASSET_FRACTION),
                "default_slippage_pct": str(DEFAULT_SLIPPAGE_PCT),
                "pairs": "Kraken xStocks quoted in USD only",
                "session": "regular US session only (Mon-Fri 09:30-16:00 New York)",
            },
        }

    def save_credentials(self, user_id, key, secret):
        for value in (key, secret):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 4096
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Invalid API key or secret")
        try:
            base64.b64decode(secret.strip(), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("Invalid API secret: Kraken secrets are base64") from None
        self.set_enabled(user_id, False)
        _write(
            credentials_path(user_id),
            {"api_key": key.strip(), "api_secret": secret.strip()},
        )

    def set_enabled(self, user_id, enabled):
        ledger = _read_ledger(user_id)
        if enabled:
            self.credentials(user_id)
            if ledger["uncertain_order"]:
                raise ValueError(
                    "An order needs reconciliation before trading can resume"
                )
            if not ledger.get("synced"):
                raise ValueError(
                    "Sync the bot allocation from the Kraken account first"
                )
        ledger["enabled"] = bool(enabled)
        _write(_root(user_id) / "ledger.json", ledger)
        return self.status(user_id)

    def disconnect(self, user_id):
        self.set_enabled(user_id, False)
        credentials_path(user_id).unlink(missing_ok=True)
        return self.status(user_id)

    # ── HTTP ──

    def _nonce(self):
        # Kraken wants a strictly increasing integer per key; milliseconds, never repeated.
        self._last_nonce = max(int(time.time() * 1000), self._last_nonce + 1)
        return self._last_nonce

    async def _send(self, session, url, **kwargs):
        async with session.request("POST", url, allow_redirects=False, **kwargs) as r:
            chunks, size = [], 0
            async for chunk in r.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("Kraken response too large")
                chunks.append(chunk)
            return r.status, json.loads(b"".join(chunks))

    @staticmethod
    def _result(status, data, secret_values=()):
        if not isinstance(data, dict):
            raise KrakenError(status, [], secret_values)
        # Entries starting with "W" are warnings, not errors.
        errors = [e for e in (data.get("error") or []) if not str(e).startswith("W")]
        if status != 200 or errors:
            raise KrakenError(status, errors, secret_values)
        return data.get("result") or {}

    async def public(self, path, params=None):
        """A public Kraken read; the paper test's reader adds ``asset_class``."""
        return await self.paper._get(path, params or {})

    async def private(self, user_id, method, params=None):
        import aiohttp

        key, secret = self.credentials(user_id)
        now = time.monotonic()
        calls = self.calls[user_id]
        while calls and now - calls[0] >= 60:
            calls.popleft()
        if len(calls) >= RATE_LIMIT_PER_MINUTE:
            # Nothing was sent, so this is a definite refusal.
            raise KrakenError(0, ["EAPI:Rate limit exceeded (local)"])
        calls.append(now)
        urlpath, postdata, headers = signed_request(
            key, secret, method, params or {}, self._nonce()
        )
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["User-Agent"] = "Condor-KrakenXStocks/1.0"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                status, data = await self._send(
                    session, BASE_URL + urlpath, data=postdata, headers=headers
                )
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
            raise ValueError(
                "Kraken unavailable or returned an invalid response; "
                "order requests are never retried"
            ) from None
        return self._result(status, data, (key, secret))

    # ── market data ──

    async def markets(self):
        """``{"AAPL": {"symbol", "pair", "asset", "row"}}`` for the online xStocks pairs."""
        found = {}
        for pair, row in (await self.paper.pair_rows()).items():
            wsbase = str(row.get("wsname", "")).split("/")[0]
            symbol = wsbase[:-1].upper()
            # `base` is Kraken's asset code for the token; it is what balances are
            # keyed by. Its exact spelling is not assumed: fall back to the ws name.
            asset = row.get("base") if isinstance(row.get("base"), str) else wsbase
            found[symbol] = {
                "symbol": symbol,
                "pair": pair,
                "asset": asset,
                "wsbase": wsbase,
                "row": row,
            }
        return found

    async def _market_for(self, symbol):
        symbol = symbol_of(symbol)
        market = (await self.markets()).get(symbol)
        if not market:
            raise ValueError(
                f"{symbol} is not an online Kraken xStocks pair quoted in USD"
            )
        return market

    async def _tickers(self, pairs):
        """``{pair: raw Kraken ticker}`` for the named pairs, one request per 40."""
        names = list(dict.fromkeys(pairs))
        found = {}
        step = xstocks_paper.TICKER_CHUNK
        for start in range(0, len(names), step):
            chunk = names[start : start + step]
            result = await self.public("Ticker", {"pair": ",".join(chunk)})
            for key, ticker in result.items():
                name = key if key in chunk else key.replace("/", "")
                if name in chunk:
                    found[name] = ticker
                elif len(chunk) == 1 and len(result) == 1:
                    found[chunk[0]] = ticker
        return found

    @staticmethod
    def _touch(ticker):
        """``(bid, ask, last)`` from a Kraken ticker (``b``/``a``/``c`` hold [price, ...])."""
        try:
            return tuple(_decimal(ticker[k][0]) for k in ("b", "a", "c"))
        except (KeyError, IndexError, TypeError):
            return None, None, None

    @staticmethod
    def _steps(row):
        def places(name, default):
            try:
                return int(row.get(name))
            except (TypeError, ValueError):
                return default

        price = _decimal(row.get("tick_size"))
        if not price or price <= 0:
            price = Decimal(1).scaleb(-places("pair_decimals", 2))
        lot = Decimal(1).scaleb(-places("lot_decimals", 8))
        return {
            "price_step": price,
            "qty_step": lot,
            "ordermin": _decimal(row.get("ordermin"), Decimal(0)),
            "costmin": _decimal(row.get("costmin"), Decimal(0)),
        }

    def _record_prices(self, user_id, rows):
        """Feed the live scorecard; it must never be able to break a market read."""
        try:
            scorecard.record_snapshot(
                user_id,
                [
                    {"symbol": r["symbol"], "price_native": {"amount": r["price"]}}
                    for r in rows
                ],
                venue=VENUE,
            )
        except Exception:  # noqa: BLE001 - analytics only
            pass

    async def coins(self, user_id, keyword=""):
        if not isinstance(keyword, str) or len(keyword) > 40:
            raise ValueError("Enter a stock symbol")
        key = keyword.strip().upper()
        markets = await self.markets()
        wanted = {s: m for s, m in markets.items() if not key or key in s}
        tickers = await self._tickers([m["pair"] for m in wanted.values()])
        rows = []
        for symbol, market in wanted.items():
            ticker = tickers.get(market["pair"])
            row = self.paper._row(symbol, market["pair"], ticker) if ticker else None
            if row:
                steps = self._steps(market["row"])
                row["min_quantity_step"] = fmt(steps["qty_step"])
                row["order_min"] = fmt(steps["ordermin"])
                row["cost_min_usd"] = fmt(steps["costmin"])
                rows.append(row)
        rows.sort(key=lambda r: r["volume_24h_usd"], reverse=True)
        if key:
            shown = rows[:10]
        else:
            shown = rows[:TOP_COINS]
            held = set(_read_ledger(user_id)["positions"])
            listed = {r["symbol"] for r in shown}
            shown += [r for r in rows if r["symbol"] in held - listed]
        self._record_prices(user_id, shown)
        return {"quote": QUOTE, "market": self._market(), "stocks": shown}

    async def candles(self, user_id, symbol, timeframe="15m", count=96):
        return await self.paper.candles(symbol, timeframe, count)

    async def book(self, user_id, symbol, depth=10):
        market = await self._market_for(symbol)
        result = await self.public(
            "Depth", {"pair": market["pair"], "count": max(1, min(int(depth), 50))}
        )
        data = next(iter(result.values()), {}) if isinstance(result, dict) else {}
        return {
            "pair": market["pair"],
            "bids": [[lvl[0], lvl[1]] for lvl in data.get("bids", [])],
            "asks": [[lvl[0], lvl[1]] for lvl in data.get("asks", [])],
        }

    # ── account ──

    async def accounts(self, user_id):
        # BalanceEx has the held amount; plain Balance is the fallback when Kraken
        # refuses it definitively (a permission or method difference).
        try:
            result = await self.private(user_id, "BalanceEx")
        except KrakenError as error:
            if not error.definite:
                raise
            result = await self.private(user_id, "Balance")
        funded = []
        for asset, value in sorted(result.items()):
            if isinstance(value, dict):
                balance = _decimal(value.get("balance"), Decimal(0))
                hold = _decimal(value.get("hold_trade"), Decimal(0))
            else:
                balance, hold = _decimal(value, Decimal(0)), Decimal(0)
            if balance > 0:
                funded.append(
                    {
                        "currency": asset,
                        "balance": fmt(balance),
                        "available": fmt(max(balance - hold, Decimal(0))),
                    }
                )
        return {"accounts": funded}

    async def fee_rates(self, user_id, pair):
        """The account's effective maker/taker rates for ``pair``, in percent (cached)."""
        stamp, cached = self._fees.get((user_id, pair), (0.0, None))
        if cached and time.monotonic() - stamp < 600:
            return cached
        result = await self.private(
            user_id, "TradeVolume", {"pair": pair, "fee-info": "true"}
        )

        def pick(section):
            rows = result.get(section)
            if not isinstance(rows, dict) or not rows:
                return None
            row = rows.get(pair) or (
                next(iter(rows.values())) if len(rows) == 1 else None
            )
            return _decimal((row or {}).get("fee"))

        taker = pick("fees")
        if taker is None:
            raise ValueError("Kraken returned no taker fee for this pair")
        rates = {"maker_pct": pick("fees_maker"), "taker_pct": taker}
        self._fees[(user_id, pair)] = (time.monotonic(), rates)
        return rates

    async def _taker_or_none(self, user_id, pair):
        """``(taker_pct or None, note)``: a missing fee is reported, never read as zero."""
        try:
            return (await self.fee_rates(user_id, pair))["taker_pct"], None
        except ValueError as error:
            return None, "fee unknown: " + _clean(error)[:80]

    async def portfolio(self, user_id):
        """Whole Kraken account at the live last price, for the dashboard (USD)."""
        accounts = (await self.accounts(user_id))["accounts"]
        markets = await self.markets()
        by_asset = {}
        for market in markets.values():
            by_asset[market["asset"]] = market
            by_asset.setdefault(market["wsbase"], market)
        held = [by_asset[a["currency"]] for a in accounts if a["currency"] in by_asset]
        tickers = await self._tickers([m["pair"] for m in held])
        balances, unpriced = [], []
        for row in accounts:
            currency = row["currency"]
            market = by_asset.get(currency)
            token = currency
            if currency in USD_ASSETS:
                token, price = "USD", Decimal(1)
            elif market:
                token = market["wsbase"]
                price = self._touch(tickers.get(market["pair"]) or {})[2]
            else:
                price = None
            if not price:
                unpriced.append(token)
                price = Decimal(0)
            total = Decimal(row["balance"])
            balances.append(
                {
                    "token": token,
                    "total": float(total),
                    "available": float(Decimal(row["available"])),
                    "usd_value": float(total * price),
                }
            )
        note = (
            "Whole Kraken account at the last traded price. "
            "Only the bot allocation may be traded."
        )
        if unpriced:
            note += " No USD price for " + ", ".join(unpriced) + "."
        return {
            "connector": "Kraken xStocks",
            "balances": balances,
            "total_usd": sum(row["usd_value"] for row in balances),
            "note": note,
        }

    async def sync_from_account(self, user_id):
        """Seed the bot allocation from the Kraken account (owner action).

        Cost basis for stocks that are already in the account restarts at their
        current bid; Kraken does not report what they cost elsewhere.
        """
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            if ledger["uncertain_order"]:
                raise ValueError("Reconcile the uncertain order before syncing")
            if ledger["enabled"]:
                raise ValueError("Pause trading before syncing the allocation")
            accounts = (await self.accounts(user_id))["accounts"]
            markets = await self.markets()
            by_asset = {}
            for market in markets.values():
                by_asset[market["asset"]] = market
                by_asset.setdefault(market["wsbase"], market)
            usd = {}
            candidates, skipped = [], []
            for row in accounts:
                currency = row["currency"]
                if currency in USD_ASSETS:
                    usd[currency] = Decimal(row["available"])
                elif currency in by_asset:
                    candidates.append((by_asset[currency], Decimal(row["balance"])))
                else:
                    skipped.append(currency)
            cash = next((usd[c] for c in USD_ASSETS if c in usd), Decimal(0))
            tickers = await self._tickers([m["pair"] for m, _ in candidates])
            positions = {}
            for market, units in candidates:
                bid = self._touch(tickers.get(market["pair"]) or {})[0]
                if not bid or units <= 0:
                    skipped.append(market["symbol"])
                    continue
                positions[market["symbol"]] = {
                    "units": fmt(units),
                    "cost": fmt(units * bid),
                }
            ledger = new_ledger()
            ledger["cash"] = fmt(cash)
            ledger["positions"] = positions
            ledger["synced"] = True
            _write(_root(user_id) / "ledger.json", ledger)
            return {**self.status(user_id), "skipped": skipped}

    # ── valuation ──

    async def equity(self, user_id, ledger):
        total = Decimal(ledger["cash"])
        markets = await self.markets()
        pairs = {}
        for symbol in ledger["positions"]:
            if symbol not in markets:
                raise ValueError(f"No market price for {symbol}")
            pairs[symbol] = markets[symbol]["pair"]
        tickers = await self._tickers(pairs.values()) if pairs else {}
        for symbol, position in ledger["positions"].items():
            bid = self._touch(tickers.get(pairs[symbol]) or {})[0]
            if not bid:
                raise ValueError(f"No market price for {symbol}")
            total += bid * Decimal(position["units"])
        return total

    async def valuation(self, user_id):
        ledger = _read_ledger(user_id)
        if ledger["uncertain_order"]:
            raise ValueError(
                "Reconcile the uncertain order before valuing the bot allocation"
            )
        equity = await self.equity(user_id, ledger)
        if equity <= FLOOR and ledger["enabled"]:
            self.set_enabled(user_id, False)
        return {
            "equity": fmt(equity),
            "quote": QUOTE,
            "new_position_limit": fmt(
                equity / 6 if guards_active(equity) else equity * MAX_ASSET_FRACTION
            ),
            "minimum_cash_reserve": fmt(
                equity / 2 if guards_active(equity) else Decimal(0)
            ),
            **self.status(user_id),
        }

    # ── orders ──

    async def _plan(self, user_id, side, symbol, value, slippage_pct):
        """Size an immediate-or-cancel limit order from the live touch."""
        side = side.lower() if isinstance(side, str) else ""
        if side not in SIDES:
            raise ValueError("Use side buy or sell")
        market = await self._market_for(symbol)
        symbol, pair = market["symbol"], market["pair"]
        # The owner's rule: only xStocks pairs in USD, nothing else, ever.
        if not pair.endswith("xUSD") or "SPV" in pair:
            raise ValueError("Only Kraken xStocks pairs quoted in USD can be traded")
        slip = amount(str(slippage_pct if slippage_pct else DEFAULT_SLIPPAGE_PCT))
        if slip > MAX_SLIPPAGE_PCT:
            raise ValueError(f"Slippage above {MAX_SLIPPAGE_PCT}% is not allowed")
        ticker = (await self._tickers([pair])).get(pair) or {}
        bid, ask, _ = self._touch(ticker)
        if not bid or not ask or bid <= 0 or ask <= 0:
            raise ValueError(f"No live market for {pair}")
        spread = (ask - bid) / ask * 100
        if spread > MAX_SPREAD_PCT:
            raise ValueError(f"{pair} spread is {spread:.2f}%, above {MAX_SPREAD_PCT}%")
        steps = self._steps(market["row"])
        factor = slip / 100
        if side == "buy":
            limit = _round_up(ask * (1 + factor), steps["price_step"])
            quantity = _round_down(amount(value) / limit, steps["qty_step"])
        else:
            limit = _round_down(bid * (1 - factor), steps["price_step"])
            quantity = _round_down(amount(value), steps["qty_step"])
        if quantity <= 0:
            raise ValueError("Amount is below the minimum order size for this pair")
        if quantity < steps["ordermin"]:
            raise ValueError(
                f"Quantity {fmt(quantity)} is below Kraken's minimum volume "
                f"{fmt(steps['ordermin'])} for {pair}"
            )
        if quantity * limit < steps["costmin"]:
            raise ValueError(
                f"Order value {fmt(quantity * limit)} USD is below Kraken's minimum "
                f"cost {fmt(steps['costmin'])} USD for {pair}"
            )
        return {
            "side": side,
            "symbol": symbol,
            "pair": pair,
            "asset": market["asset"],
            "wsbase": market["wsbase"],
            "quantity": quantity,
            "limit_price": limit,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread,
            "slippage_pct": slip,
        }

    async def preview(self, user_id, side, symbol, value, slippage_pct=None):
        plan = await self._plan(user_id, side, symbol, value, slippage_pct)
        book = await self.book(user_id, plan["symbol"], 20)
        remaining, cost = plan["quantity"], Decimal(0)
        for level in book["asks" if plan["side"] == "buy" else "bids"]:
            price, size = _decimal(level[0]), _decimal(level[1])
            take = min(remaining, size)
            cost += take * price
            remaining -= take
            if remaining <= 0:
                break
        average = (
            cost / (plan["quantity"] - remaining)
            if plan["quantity"] > remaining
            else None
        )
        reference = plan["ask"] if plan["side"] == "buy" else plan["bid"]
        impact = (
            abs(average - reference) / reference * 100 if average is not None else None
        )
        taker, fee_note = await self._taker_or_none(user_id, plan["pair"])
        round_trip = None if taker is None else plan["spread_pct"] + 2 * taker
        market = self._market()
        return {
            "pair": plan["pair"],
            "side": plan["side"],
            "taker_fee_pct": fmt(taker) if taker is not None else None,
            "estimated_round_trip_pct": (
                str(round(round_trip, 3)) if round_trip is not None else None
            ),
            "fee_note": fee_note,
            "quantity": fmt(plan["quantity"]),
            "limit_price": fmt(plan["limit_price"]),
            "notional_usd": fmt(plan["quantity"] * plan["limit_price"]),
            "bid": fmt(plan["bid"]),
            "ask": fmt(plan["ask"]),
            "spread_pct": str(round(plan["spread_pct"], 3)),
            "estimated_average_price": fmt(average) if average is not None else None,
            "estimated_impact_pct": (
                str(round(impact, 3)) if impact is not None else None
            ),
            "book_covers_order": remaining <= 0,
            "market_open": market["open"],
            "minutes_to_close": market["minutes_to_close"],
        }

    @staticmethod
    def _holdings(accounts, plan):
        """Available units of the stock in the Kraken account (asset code not assumed)."""
        for code in (plan["asset"], plan["wsbase"]):
            if code in accounts:
                return Decimal(accounts[code]["available"])
        return Decimal(0)

    async def order(self, user_id, side, symbol, value, slippage_pct=None):
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            if not ledger["enabled"] or ledger["uncertain_order"]:
                raise ValueError("Trading is paused")
            market = self._market()
            if not market["open"]:
                raise ValueError(
                    "Kraken xStocks orders are refused outside the regular US session "
                    f"(Mon-Fri 09:30-16:00 New York); it is {market['new_york_time']} "
                    "in New York"
                )
            plan = await self._plan(user_id, side, symbol, value, slippage_pct)
            symbol = plan["symbol"]
            if plan["side"] == "buy":
                equity = await self.equity(user_id, ledger)
                if equity <= FLOOR:
                    self.set_enabled(user_id, False)
                    raise ValueError(
                        f"Bot equity reached the {FLOOR} stop threshold; trading paused"
                    )
                spend = plan["quantity"] * plan["limit_price"]
                if spend * (1 + FEE_HEADROOM) > Decimal(ledger["cash"]):
                    raise ValueError(
                        "Not enough bot cash for this purchase and its fee"
                    )
                check_purchase(ledger, symbol, spend, equity)
            else:
                position = ledger["positions"].get(symbol)
                if not position or plan["quantity"] > Decimal(position["units"]):
                    raise ValueError("Cannot sell assets outside the bot allocation")
            accounts = {
                a["currency"]: a for a in (await self.accounts(user_id))["accounts"]
            }
            if plan["side"] == "buy":
                have = Decimal(
                    next(
                        (accounts[c]["available"] for c in USD_ASSETS if c in accounts),
                        "0",
                    )
                )
                need = plan["quantity"] * plan["limit_price"] * (1 + FEE_HEADROOM)
            else:
                have, need = self._holdings(accounts, plan), plan["quantity"]
            if have < need:
                raise ValueError("Insufficient available Kraken balance")
            recent = [o for o in ledger["orders"] if time.time() - o["time"] < 60]
            if len(recent) >= 10:
                raise ValueError("Maximum ten orders per minute")
            current = _read_ledger(user_id)
            if not current["enabled"]:
                raise ValueError("Trading paused during checks")
            client_oid = str(uuid.uuid4())
            # Written BEFORE the order is sent: a crash or timeout leaves the order
            # marked uncertain and trading paused, never silently forgotten.
            ledger["uncertain_order"] = {
                "client_oid": client_oid,
                "side": plan["side"],
                "symbol": symbol,
                "quantity": fmt(plan["quantity"]),
                "limit_price": fmt(plan["limit_price"]),
                "time": time.time(),
            }
            _write(_root(user_id) / "ledger.json", ledger)
            try:
                placed = await self.private(
                    user_id,
                    "AddOrder",
                    {
                        "pair": plan["pair"],
                        "type": plan["side"],
                        "ordertype": "limit",
                        "price": fmt(plan["limit_price"]),
                        "volume": fmt(plan["quantity"]),
                        "timeinforce": "IOC",
                        # Fee in USD on both sides, so the ledger's quote-currency
                        # arithmetic holds (Kraken treats this as a preference).
                        "oflags": "fciq",
                        "cl_ord_id": client_oid,
                        # Unverified for AddOrder: the public endpoints need it for
                        # tokenized pairs, so it is sent here too.
                        "asset_class": ASSET_CLASS,
                    },
                )
            except KrakenError as error:
                if not error.definite:
                    raise ValueError(_UNCERTAIN) from None
                # Kraken answered and refused: no order exists, so there is nothing
                # to reconcile and trading stays as it was.
                ledger["uncertain_order"] = None
                _write(_root(user_id) / "ledger.json", ledger)
                raise ValueError(f"Kraken rejected the order: {error}") from None
            except Exception:
                raise ValueError(_UNCERTAIN) from None
            # From here an order may exist: nothing below clears the uncertain entry
            # except a read-back that shows the final state.
            txids = placed.get("txid") if isinstance(placed, dict) else None
            txid = txids[0] if isinstance(txids, list) and txids else None
            if not isinstance(txid, str) or not txid:
                raise ValueError(_UNCERTAIN) from None
            ledger["uncertain_order"]["txid"] = txid
            _write(_root(user_id) / "ledger.json", ledger)
            try:
                detail = await self._final_detail(user_id, txid)
            except Exception:
                raise ValueError(_UNCERTAIN) from None
            self._apply_fill(ledger, plan["side"], symbol, detail)
            ledger["enabled"] = (
                credentials_path(user_id).exists() and _read_ledger(user_id)["enabled"]
            )
            _write(_root(user_id) / "ledger.json", ledger)
            return {"order": self._summary(detail), "status": self.status(user_id)}

    @staticmethod
    def _normalize(txid, info):
        """The fields this module reads from a Kraken order record, in one shape."""
        descr = info.get("descr") if isinstance(info.get("descr"), dict) else {}
        return {
            "order_id": txid,
            "client_oid": info.get("cl_ord_id"),
            "pair": descr.get("pair"),
            "side": descr.get("type"),
            "status": str(info.get("status", "")).lower(),
            "volume": info.get("vol"),
            "vol_exec": info.get("vol_exec"),
            "limit_price": descr.get("price"),
            "avg_price": info.get("price"),
            "cost": info.get("cost"),
            "fee": info.get("fee"),
            "oflags": info.get("oflags"),
            "opentm": info.get("opentm"),
            "closetm": info.get("closetm"),
        }

    async def _detail(self, user_id, txid):
        """One order by transaction id, or None when Kraken does not know it."""
        result = await self.private(
            user_id, "QueryOrders", {"txid": txid, "trades": "true"}
        )
        info = result.get(txid) if isinstance(result, dict) else None
        if not isinstance(info, dict):
            return None
        return self._normalize(txid, info)

    async def _final_detail(self, user_id, txid):
        for attempt in range(ORDER_POLLS):
            try:
                detail = await self._detail(user_id, txid)
            except ValueError:
                # A refused or failed read (rate limit, transport) is not an answer
                # about the order; keep polling and fall through to "uncertain".
                detail = None
            if detail and detail["status"] in FINAL_STATUSES:
                return detail
            if attempt + 1 < ORDER_POLLS:
                await asyncio.sleep(ORDER_POLL_WAIT_SEC)
        raise ValueError("Order did not reach a final state")

    @staticmethod
    def _summary(detail):
        keys = (
            "order_id",
            "client_oid",
            "pair",
            "side",
            "status",
            "volume",
            "vol_exec",
            "limit_price",
            "avg_price",
            "cost",
            "fee",
            "closetm",
        )
        return {k: detail.get(k) for k in keys if detail.get(k) is not None}

    def _apply_fill(self, ledger, side, symbol, detail):
        status = detail["status"]
        if status not in FINAL_STATUSES:
            raise ValueError("Order has not reached a final state")
        quantity = _decimal(detail.get("vol_exec"), Decimal(0))
        value = _decimal(detail.get("cost"), Decimal(0))
        # Kraken reports the fee in the quote currency (USD). The order asks for the
        # fee in USD ("fciq"); if the record says it was taken in the base asset
        # ("fcib") the fee is converted at the average price and taken off the units.
        fee = _decimal(detail.get("fee"), Decimal(0))
        flags = str(detail.get("oflags") or "").split(",")
        fee_in_base = "fcib" in flags and "fciq" not in flags
        average = _decimal(detail.get("avg_price")) or (
            value / quantity if quantity > 0 else None
        )
        fee_units = fee / average if fee_in_base and average else Decimal(0)
        if quantity > 0:
            if side == "buy":
                units = quantity - fee_units
                spent = value + (0 if fee_in_base else fee)
                previous = ledger["positions"].get(symbol, {"units": "0", "cost": "0"})
                ledger["positions"][symbol] = {
                    "units": fmt(Decimal(previous["units"]) + units),
                    "cost": fmt(Decimal(previous["cost"]) + spent),
                }
                ledger["cash"] = fmt(Decimal(ledger["cash"]) - spent)
            else:
                position = ledger["positions"].get(symbol)
                if not position or quantity > Decimal(position["units"]):
                    raise ValueError("Filled sale exceeds the bot position")
                proceeds = value - (0 if fee_in_base else fee)
                out = min(quantity + fee_units, Decimal(position["units"]))
                fraction = out / Decimal(position["units"])
                cost = Decimal(position["cost"]) * fraction
                if fraction == 1:
                    del ledger["positions"][symbol]
                else:
                    position["units"] = fmt(Decimal(position["units"]) - out)
                    position["cost"] = fmt(Decimal(position["cost"]) - cost)
                ledger["cash"] = fmt(Decimal(ledger["cash"]) + proceeds)
        ledger["uncertain_order"] = None
        ledger["orders"] = (
            ledger["orders"]
            + [
                {
                    "time": time.time(),
                    "side": side,
                    "symbol": symbol,
                    "order_id": detail.get("order_id"),
                    "client_oid": detail.get("client_oid"),
                    "status": status,
                    "quantity": fmt(quantity),
                    "value": fmt(value),
                    "fee": fmt(fee),
                    "fee_currency": symbol + "x" if fee_in_base else QUOTE,
                }
            ]
        )[-200:]

    async def history(self, user_id):
        """The most recent closed orders (Kraken returns them newest first)."""
        result = await self.private(user_id, "ClosedOrders")
        closed = result.get("closed") if isinstance(result, dict) else None
        rows = [
            self._normalize(txid, info)
            for txid, info in (closed or {}).items()
            if isinstance(info, dict)
        ]
        rows.sort(key=lambda r: _decimal(r.get("closetm"), Decimal(0)), reverse=True)
        return [self._summary(row) for row in rows][:50]

    async def _find_order(self, user_id, pending):
        """Look for the uncertain order in Kraken's open and closed orders.

        Matched by ``cl_ord_id`` (and the transaction id when it is known). Returns
        ``(detail or None, ambiguous)``: ``ambiguous`` is True when no record carries
        the client id but an order of the same pair and side was opened since, which
        could be ours if Kraken omits ``cl_ord_id`` from order records (unverified).
        """
        cid, txid = pending["client_oid"], pending.get("txid")
        since = float(pending["time"]) - 10
        side = pending["side"]
        pair = (await self._market_for(pending["symbol"]))["pair"]
        similar = False

        def scan(orders):
            nonlocal similar
            for key, info in (orders or {}).items():
                if not isinstance(info, dict):
                    continue
                if info.get("cl_ord_id") == cid or (txid and key == txid):
                    return self._normalize(key, info)
                row = self._normalize(key, info)
                opened = _decimal(row.get("opentm"), Decimal(0))
                if row["pair"] == pair and row["side"] == side and opened >= since:
                    similar = True
            return None

        found = scan((await self.private(user_id, "OpenOrders")).get("open"))
        offset = 0
        for _ in range(4):
            if found:
                break
            page = await self.private(
                user_id,
                "ClosedOrders",
                {"start": int(pending["time"]) - 300, "ofs": offset},
            )
            closed = page.get("closed") or {}
            found = scan(closed)
            offset += len(closed)
            total = _decimal(page.get("count"), Decimal(0))
            if not closed or offset >= total:
                break
        return found, (found is None and similar)

    async def reconcile(self, user_id):
        """Settle an order whose outcome was unknown. Leaves trading paused."""
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            pending = ledger["uncertain_order"]
            if not pending:
                return self.status(user_id)
            detail, ambiguous = None, False
            if pending.get("txid"):
                detail = await self._detail(user_id, pending["txid"])
            if detail is None:
                detail, ambiguous = await self._find_order(user_id, pending)
            if detail is None:
                if ambiguous:
                    raise ValueError(
                        "An order like this exists in Kraken's history without our "
                        "client id; keep trading paused, check the Kraken order "
                        "history and clear the uncertain order by hand"
                    )
                if time.time() - pending["time"] < 60:
                    raise ValueError("Wait a minute before reconciling")
                # Not in the open orders and not in the closed history after a
                # minute: Kraken never created it.
                ledger["uncertain_order"] = None
                ledger["enabled"] = False
                _write(_root(user_id) / "ledger.json", ledger)
                return self.status(user_id)
            if detail["status"] not in FINAL_STATUSES:
                raise ValueError("The order is still open; reconcile again shortly")
            self._apply_fill(ledger, pending["side"], pending["symbol"], detail)
            ledger["enabled"] = False
            _write(_root(user_id) / "ledger.json", ledger)
            return self.status(user_id)

    # ── scorecard ──

    async def predict(
        self, user_id, symbol, verdict, target_pct, stop_pct, horizon_hours, note=""
    ):
        symbol = symbol_of(symbol)
        if scorecard.latest_price(user_id, symbol, venue=VENUE) is None:
            await self.coins(user_id, symbol)
        return scorecard.record_prediction(
            user_id,
            symbol,
            verdict,
            target_pct,
            stop_pct,
            horizon_hours,
            note,
            venue=VENUE,
        )

    async def scorecard(self, user_id):
        """The live calls scored at this account's own cost when Kraken reports its fee."""
        cost = DEFAULT_ROUND_TRIP_COST_PCT
        try:
            markets = await self.markets()
            market = markets.get("AAPL") or next(iter(markets.values()), None)
            if market:
                taker = (await self.fee_rates(user_id, market["pair"]))["taker_pct"]
                cost = float(2 * taker) + SPREAD_ALLOWANCE_PCT
        except ValueError:
            pass  # no key, or no fee answer: keep the stated default
        return scorecard.report(user_id, cost_pct=cost, venue=VENUE)


service = KrakenXStocksService()
