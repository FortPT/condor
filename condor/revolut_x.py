"""Live Revolut X (crypto exchange) trading on its REST API, in euros.

The owner's money is euros, so the ledger quote currency is EUR and the bot only ever
trades the ``<COIN>-EUR`` pairs. Orders are immediate-or-cancel limits priced through
the touch, and every fill is read back from Revolut X, never assumed.

Keys never enter agent tool arguments, the browser's state, logs or responses. Revolut X
signs requests with Ed25519: the keypair is generated HERE, on the server. The private
key is written to ``CONDOR_DATA_DIR/revolut_x/<user>/signing_key.pem`` (mode 0600) and is
never returned; only the PUBLIC key is shown, for the owner to register in the Revolut X
web app, which hands back the 64-character API key.

The ledger owns only this bot's allocation, in EUR, keyed by the plain coin (``BTC``)
while Revolut X writes the pair ``BTC-EUR`` (orders, paths) or ``BTC/EUR`` (responses,
configuration). It is seeded from the account by :meth:`RevolutXService.sync_from_account`.

VERIFIED (official OpenAPI spec and the PUBLIC endpoints, read live): the signing
formula (against an independently computed Ed25519 signature), the public pair
configuration, tickers (EEA region), order book v2 and candles (interval in minutes: 1,
5, 15, 30, 60, 240, 1440), and the documented shapes of the order object, balances,
fills (``p``, ``q``, ``im``) and errors.

VERIFIED by a real 1 EUR BTC buy and sell on 2026-10-28: the place-order body below is
accepted, ``GET /orders/{id}`` answers ``{"data": order}`` with ``total_fee`` and
``fee_currency``, the fills read works and balances are a list. Revolut X rounds EUR
amounts and fees to the cent (the order's ``filled_amount`` and ``average_fill_price``
are rounded artefacts; the fills carry the real price), the buy fee is reported in the
coin and the sell fee in EUR rounded to a cent, so at small sizes cent rounding, not the
published rate, dominates the cost: see ``ROUNDING_ALLOWANCE_EUR_PER_LEG``.

NOT VERIFIED against a real account (the comments say so where it matters): partial
fills, refused orders, 429 behaviour and how the signature treats a percent-encoded
query. See ``docs/revolut-x.md``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
import uuid
from collections import defaultdict, deque
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from urllib.parse import urlencode

from condor import crypto_com_scorecard as scorecard
from condor.fsutil import atomic_write_json
from condor.paths import data_dir

VENUE = "revolut_x"
BASE_URL = "https://revx.revolut.com/api"
API_PATH_PREFIX = "/api"  # the signed path starts here
QUOTE = "EUR"
REGION = "EEA"
SIDES = ("buy", "sell")
# Order statuses: pending_new, new, partially_filled, filled, cancelled, rejected,
# replaced. A resting status means the order may still trade.
FINAL_STATUSES = {"filled", "cancelled", "canceled", "rejected", "replaced"}
_UNCERTAIN = (
    "Order outcome needs reconciliation. Trading paused; check the Revolut X order "
    "history. Do not resubmit."
)
# The documented key: 64 characters, letters and digits.
API_KEY_FORMAT = re.compile(r"[A-Za-z0-9]{64}")
SYMBOL = re.compile(r"([A-Za-z0-9]{1,24})(?:[/-]([A-Za-z]{3,5}))?")

# Below this total bot equity (EUR) the bot may use all of its money freely; only
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
# Revolut X publishes no fee endpoint. Until a real fill reports the truth the cost is
# an ESTIMATE: the published EEA taker rate (maker is 0%). Always labelled as one.
PUBLISHED_TAKER_FEE_PCT = Decimal("0.09")
# Revolut X rounds EUR amounts and EUR fees to the cent. A real 1 EUR round trip (BTC/EUR,
# 2026-10-28) lost 0.02 EUR (about 2%) against a modelled 0.19%. Assume up to this much
# EUR lost to rounding on each leg, so small orders look as expensive as they are.
ROUNDING_ALLOWANCE_EUR_PER_LEG = Decimal("0.01")
# A BUY whose estimated round-trip cost (rounding allowance included) is above this many
# percent is refused unless the owner's API passes ``allow_costly_buy``. Sells are never
# blocked by it: an exit must always be possible.
MAX_BUY_ROUND_TRIP_PCT = Decimal("1.0")
# A cent-rounded EUR fee says little about the rate on a small fill (0.01 on 1 EUR reads as
# 1%): the taker rate is learned only from a fill at least this large (EUR).
OBSERVED_FEE_MIN_VALUE_EUR = Decimal("50")
# The fills are trusted for the price only when they add up to the order's filled quantity
# within this fraction, and a reported price further than this many percent from the
# fills-derived one is called out.
FILLS_QUANTITY_TOLERANCE = Decimal("0.005")
PRICE_NOTE_PCT = Decimal("0.1")
# Round-trip cost used by the scorecard when no spread is known.
DEFAULT_ROUND_TRIP_COST_PCT = 0.35
MAX_BYTES = 4 * 1024 * 1024
# Published limits: public 1 request/second; authenticated reads 100/s and
# 1,000/minute; place order 10/s and 1,000/day. Stay well inside them.
PUBLIC_GAP_SEC = 1.1
RATE_LIMIT_PER_MINUTE = 600
ORDERS_PER_DAY = 900
MAX_ACTIVE_ORDERS = 300
INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1D": 1440}
# Not a bet on the market: stablecoins and wrapped or tokenized assets are never bought.
NOT_MARKET = scorecard.NOT_MARKET | {
    "EURC",
    "EURT",
    "FDUSD",
    "TUSD",
    "USDE",
    "USDP",
    "WETH",
    "STETH",
    "WSTETH",
    "CBBTC",
    "CBETH",
    "PAXG",
}


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
    """``BTC`` from ``btc``, ``BTC-EUR`` or ``BTC/EUR``; any other quote is refused."""
    match = SYMBOL.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Invalid coin symbol")
    quote = match.group(2)
    if quote and quote.upper() != QUOTE:
        raise ValueError(f"Only {QUOTE} pairs can be traded; {value} is not one")
    return match.group(1).upper()


def pair_of(symbol: str) -> str:
    """The hyphenated pair Revolut X wants in paths and order bodies."""
    return f"{symbol}-{QUOTE}"


def _base_of(symbol) -> str | None:
    """The coin of a response symbol (``BTC/EUR`` or ``BTC-EUR``) quoted in EUR, else None."""
    if not isinstance(symbol, str):
        return None
    for sep in ("/", "-"):
        base, found, quote = symbol.partition(sep)
        if found and quote.upper() == QUOTE and base:
            return base.upper()
    return None


def fmt(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _decimal(value, default=None):
    if isinstance(value, bool):
        return default
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


def signing_message(timestamp, method, path, query="", body="") -> bytes:
    """What Revolut X signs: timestamp + METHOD + path (from ``/api``) + query + body.

    Concatenated with no separators; the query has no leading ``?`` and the body is
    the minified JSON actually sent.
    """
    return f"{timestamp}{str(method).upper()}{path}{query}{body}".encode()


def sign(private_key, message: bytes) -> str:
    """base64 of the Ed25519 signature (deterministic for a key and message)."""
    return base64.b64encode(private_key.sign(message)).decode()


def minified(body) -> str:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True)


def build_request(api_key, private_key, method, path, query, body, timestamp):
    """``(url, headers, body bytes or None)`` for one authenticated call.

    ``path`` is relative to the API root (``/1.0/orders``); the signed path adds
    ``/api``. The query string signed is byte for byte the one sent.
    """
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE"):
        raise ValueError("Invalid Revolut X method")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]{1,200}", path) or ".." in path:
        raise ValueError("Invalid Revolut X path")
    query_string = urlencode(query) if query else ""
    body_text = minified(body) if body is not None else ""
    signed_path = API_PATH_PREFIX + path
    headers = {
        "X-Revx-API-Key": api_key,
        "X-Revx-Timestamp": str(timestamp),
        "X-Revx-Signature": sign(
            private_key,
            signing_message(timestamp, method, signed_path, query_string, body_text),
        ),
        "Accept": "application/json",
        "User-Agent": "Condor-RevolutX/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    url = BASE_URL + path + ("?" + query_string if query_string else "")
    return url, headers, body_text.encode() if body is not None else None


# ── keys ──


def new_signing_key() -> tuple[bytes, str]:
    """``(private PEM, public PEM)`` of a fresh Ed25519 keypair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public.decode()


def load_private_key(pem: bytes):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("The stored signing key is not an Ed25519 key")
    return key


def public_pem(private_key) -> str:
    from cryptography.hazmat.primitives import serialization

    return (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


# ── storage ──


def _root(user_id):
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid account owner")
    return data_dir() / VENUE / str(user_id)


def credentials_path(user_id):
    return _root(user_id) / "credentials.json"


def signing_key_path(user_id):
    return _root(user_id) / "signing_key.pem"


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, content)
    os.chmod(path, 0o600)


def _write_private(path, data: bytes):
    """Create ``path`` mode 0600 from the first byte, then rename it into place."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".signing.", suffix=".tmp")
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


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
        # The taker fee rate (percent) the last real fill reported; None until then.
        "observed_taker_fee_pct": None,
    }


def _read_ledger(user_id):
    path = _root(user_id) / "ledger.json"
    return json.loads(path.read_text()) if path.exists() else new_ledger()


def guards_active(equity):
    return equity >= GUARD_THRESHOLD


def taker_rate(ledger):
    """The taker rate in percent and where it comes from. There is no fee endpoint.

    The rate the last real fill reported when there is one; otherwise the published
    EEA taker rate, as an ESTIMATE.
    """
    observed = _decimal(ledger.get("observed_taker_fee_pct"))
    if observed is not None and observed > 0:
        return {
            "taker_pct": observed,
            "source": "observed",
            "note": "taker fee observed on the last real fill",
        }
    return {
        "taker_pct": PUBLISHED_TAKER_FEE_PCT,
        "source": "estimate",
        "note": (
            f"ESTIMATE: Revolut X's published EEA taker fee of "
            f"{PUBLISHED_TAKER_FEE_PCT}% (maker 0%); the real rate is confirmed "
            "only by a real fill"
        ),
    }


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


def _clean(text) -> str:
    return re.sub(r"[^A-Za-z0-9 _.,:;()/-]", "", str(text))[:120]


class RevolutXError(ValueError):
    """Revolut X answered with an error status; only a bounded, sanitized text is kept.

    ``definite`` means the request was refused before any order could exist: every
    HTTP 4xx (a 429 included) was processed and rejected at the door, and a local
    rate-limit refusal sent nothing. HTTP 5xx, a timeout or a transport error may have
    created the order and stay uncertain.
    """

    def __init__(self, status, message="", secret_values=(), retry_after_ms=None):
        text = str(message or "")
        # Error text is untrusted and can echo credentials; keep it short and plain.
        if any(v and v in text for v in secret_values):
            text = ""
        text = _clean(text)
        super().__init__(
            f"Revolut X rejected the request (HTTP {status}"
            + (f": {text}" if text else "")
            + ")"
        )
        self.status = status
        self.message = text
        self.retry_after_ms = retry_after_ms
        self.definite = int(status) == 0 or 400 <= int(status) < 500


class RevolutXService:
    def __init__(self):
        self.locks = defaultdict(asyncio.Lock)
        self.calls = defaultdict(deque)
        self.placed = defaultdict(deque)
        self.clock = time.time
        self._public_lock = asyncio.Lock()
        self._public_at = 0.0
        self._pairs = (0.0, {})
        self._tickers_cache = (0.0, {})

    # ── credentials and switches ──

    def _api_key(self, user_id):
        try:
            key = json.loads(credentials_path(user_id).read_text())["api_key"]
        except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError):
            return None
        return key if isinstance(key, str) and API_KEY_FORMAT.fullmatch(key) else None

    def _has_signing_key(self, user_id):
        return signing_key_path(user_id).is_file()

    def _signing_key(self, user_id):
        try:
            return load_private_key(signing_key_path(user_id).read_bytes())
        except (FileNotFoundError, ValueError):
            return None

    def credentials(self, user_id):
        """``(api key, private key)``; the owner must have generated one and saved the other."""
        api_key, private = self._api_key(user_id), self._signing_key(user_id)
        if not private:
            raise ValueError(
                "Generate a signing key in Settings → Revolut X and register its "
                "public key in the Revolut X web app"
            )
        if not api_key:
            raise ValueError(
                "Paste the API key Revolut X gave you into Settings → Revolut X"
            )
        return api_key, private

    def connected(self, user_id):
        return bool(self._api_key(user_id) and self._signing_key(user_id))

    def status(self, user_id):
        ledger = _read_ledger(user_id)
        private = self._signing_key(user_id)
        return {
            "connected": self.connected(user_id),
            "has_signing_key": private is not None,
            "has_api_key": self._api_key(user_id) is not None,
            # The public half only: it is what the owner registers at Revolut X.
            "public_key": public_pem(private) if private else None,
            "venue": "Revolut X",
            "quote": QUOTE,
            "enabled": ledger["enabled"] and not ledger["uncertain_order"],
            "ledger": ledger,
            "limits": {
                "floor": str(FLOOR),
                "reserve_fraction": "0.5",
                "position_fraction": "1/6",
                "max_positions": 3,
                "guards_apply_from": str(GUARD_THRESHOLD),
                "max_asset_fraction_below_threshold": str(MAX_ASSET_FRACTION),
                "default_slippage_pct": str(DEFAULT_SLIPPAGE_PCT),
                "pairs": "Revolut X pairs quoted in EUR only",
                "session": "24/7",
                "taker_fee_pct_estimate": str(PUBLISHED_TAKER_FEE_PCT),
            },
        }

    def generate_signing_key(self, user_id):
        """Create a new keypair on the server; only the PUBLIC key is ever returned.

        A new key invalidates the API key registered for the old one, so that is
        removed too and trading is paused until the owner pastes a new one.
        """
        if _read_ledger(user_id)["uncertain_order"]:
            raise ValueError(
                "Reconcile the uncertain order before replacing the signing key"
            )
        self.set_enabled(user_id, False)
        private, _ = new_signing_key()
        _write_private(signing_key_path(user_id), private)
        credentials_path(user_id).unlink(missing_ok=True)
        return self.status(user_id)

    def save_api_key(self, user_id, key):
        if not isinstance(key, str) or not API_KEY_FORMAT.fullmatch(key.strip()):
            raise ValueError(
                "Invalid API key: Revolut X keys are 64 letters and digits"
            )
        if not self._has_signing_key(user_id):
            raise ValueError("Generate a signing key first")
        self.set_enabled(user_id, False)
        _write(credentials_path(user_id), {"api_key": key.strip()})
        return self.status(user_id)

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
                    "Sync the bot allocation from the Revolut X account first"
                )
        ledger["enabled"] = bool(enabled)
        _write(_root(user_id) / "ledger.json", ledger)
        return self.status(user_id)

    def disconnect(self, user_id):
        self.set_enabled(user_id, False)
        credentials_path(user_id).unlink(missing_ok=True)
        signing_key_path(user_id).unlink(missing_ok=True)
        return self.status(user_id)

    # ── HTTP ──

    def _timestamp(self):
        return int(self.clock() * 1000)

    async def _transport(self, method, url, headers, body):
        """One HTTP exchange: ``(status, parsed JSON, response headers)``.

        A timeout, a transport failure or an unreadable body raises ValueError: for
        an order that is an UNCERTAIN outcome, never a refusal.
        """
        import aiohttp
        from yarl import URL

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.request(
                    method,
                    URL(url, encoded=True),
                    data=body,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("Revolut X response too large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    data = json.loads(raw) if raw.strip() else {}
                    return response.status, data, dict(response.headers)
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
            raise ValueError(
                "Revolut X unavailable or returned an invalid response; "
                "order requests are never retried"
            ) from None

    @staticmethod
    def _result(status, data, headers=None, secret_values=()):
        if status not in (200, 201, 204):
            message = data.get("message") if isinstance(data, dict) else ""
            retry = None
            try:
                retry = int((headers or {}).get("Retry-After"))
            except (TypeError, ValueError):
                pass
            raise RevolutXError(status, message, secret_values, retry)
        return data

    async def public(self, path, params=None):
        """A public read (no key, 1 request a second): spaced out, never retried."""
        query = {**(params or {}), "region": REGION}
        url = BASE_URL + path + "?" + urlencode(query)
        async with self._public_lock:
            wait = PUBLIC_GAP_SEC - (time.monotonic() - self._public_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                status, data, headers = await self._transport(
                    "GET", url, {"Accept": "application/json"}, None
                )
            finally:
                self._public_at = time.monotonic()
        try:
            return self._result(status, data, headers)
        except RevolutXError as error:
            raise ValueError(f"Revolut X public data: {error}") from None

    async def private(self, user_id, method, path, query=None, body=None):
        api_key, private_key = self.credentials(user_id)
        now = time.monotonic()
        calls = self.calls[user_id]
        while calls and now - calls[0] >= 60:
            calls.popleft()
        if len(calls) >= RATE_LIMIT_PER_MINUTE:
            # Nothing was sent, so this is a definite refusal.
            raise RevolutXError(0, "local rate limit")
        calls.append(now)
        url, headers, payload = build_request(
            api_key, private_key, method, path, query, body, self._timestamp()
        )
        status, data, response_headers = await self._transport(
            method, url, headers, payload
        )
        return self._result(status, data, response_headers, (api_key,))

    # ── market data (public) ──

    async def pairs(self):
        """``{"BTC": config row}`` for the active EUR pairs (cached an hour)."""
        stamp, cached = self._pairs
        if cached and time.monotonic() - stamp < 3600:
            return cached
        data = await self.public("/1.0/public/configuration/pairs")
        found = {}
        for name, row in (data if isinstance(data, dict) else {}).items():
            base = _base_of(name)
            if not base or not isinstance(row, dict) or row.get("status") != "active":
                continue
            found[base] = {
                "symbol": base,
                "pair": pair_of(base),
                "base_step": _decimal(row.get("base_step"), Decimal("0.00000001")),
                "price_step": _decimal(row.get("quote_step"), Decimal("0.01")),
                "min_quantity": _decimal(row.get("min_order_size"), Decimal(0)),
                "max_quantity": _decimal(row.get("max_order_size")),
                "min_value": _decimal(row.get("min_order_size_quote"), Decimal(0)),
                "max_value": _decimal(row.get("max_order_size_quote")),
            }
        self._pairs = (time.monotonic(), found)
        return found

    async def _market_for(self, symbol):
        symbol = symbol_of(symbol)
        market = (await self.pairs()).get(symbol)
        if not market:
            raise ValueError(f"{symbol} is not an active Revolut X pair quoted in EUR")
        return market

    async def tickers(self):
        """``{"BTC": ticker row}`` for the EEA EUR tickers (cached a few seconds)."""
        stamp, cached = self._tickers_cache
        if cached and time.monotonic() - stamp < 5:
            return cached
        data = await self.public("/1.0/public/tickers")
        rows = data.get("data") if isinstance(data, dict) else None
        found = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("region", REGION) != REGION:
                continue
            base = _base_of(row.get("symbol"))
            if base:
                found[base] = row
        self._tickers_cache = (time.monotonic(), found)
        return found

    @staticmethod
    def _touch(ticker):
        """``(bid, ask, last)`` from a ticker row; ``last`` falls back to the mid."""
        bid, ask = _decimal(ticker.get("bid")), _decimal(ticker.get("ask"))
        last = _decimal(ticker.get("last_price")) or _decimal(ticker.get("mid"))
        return bid, ask, last

    async def eur_usd(self):
        """USD per euro: Revolut X has no EUR/USD pair, so USDC/EUR stands in, else Kraken's."""
        try:
            usdc = (await self.tickers()).get("USDC") or {}
            bid, ask, _ = self._touch(usdc)
            if bid and ask and bid > 0 and ask > 0:
                return 2 / (bid + ask)
        except ValueError:
            pass
        try:
            from condor import xstocks_market

            return await xstocks_market.service.eur_usd()
        except (
            Exception
        ):  # noqa: BLE001 - a missing rate only drops the dashboard value
            return None

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

    def _row(self, market, ticker):
        bid, ask, last = self._touch(ticker)
        if not bid or not ask or not last or last <= 0 or bid <= 0 or ask <= 0:
            return None
        volume = _decimal(ticker.get("quote_volume_24h"), Decimal(0))
        change = _decimal(ticker.get("price_change_24h"))
        low, high = _decimal(ticker.get("low_24h")), _decimal(ticker.get("high_24h"))
        before = last - change if change is not None else None
        return {
            "symbol": market["symbol"],
            "pair": market["pair"],
            "price": fmt(last),
            "bid": fmt(bid),
            "ask": fmt(ask),
            "spread_pct": str(round((ask - bid) / ask * 100, 3)),
            # price_change_24h is an absolute move in euros, not a percentage.
            "change_24h_pct": (
                str(round(change / before * 100, 2)) if before and before > 0 else None
            ),
            "range_24h_pct": (
                str(round((high - low) / last * 100, 2)) if high and low else None
            ),
            "volume_24h_eur": str(round(volume, 2)),
            "min_quantity_step": fmt(market["base_step"]),
            "price_step": fmt(market["price_step"]),
            "min_order_value_eur": fmt(market["min_value"]),
        }

    async def coins(self, user_id, keyword=""):
        if not isinstance(keyword, str) or len(keyword) > 40:
            raise ValueError("Enter a coin symbol")
        key = keyword.strip().upper()
        pairs, tickers = await self.pairs(), await self.tickers()
        rows = {}
        for symbol, market in pairs.items():
            ticker = tickers.get(symbol)
            row = self._row(market, ticker) if ticker else None
            if row:
                rows[symbol] = row
        ranked = sorted(
            (
                r
                for s, r in rows.items()
                if s not in NOT_MARKET and (not key or key in s)
            ),
            key=lambda r: Decimal(r["volume_24h_eur"]),
            reverse=True,
        )
        if key:
            shown = ranked[:10]
        else:
            shown = ranked[:TOP_COINS]
            held = set(_read_ledger(user_id)["positions"])
            listed = {r["symbol"] for r in shown}
            shown += [rows[s] for s in sorted(held - listed) if s in rows]
        self._record_prices(user_id, shown)
        return {"quote": QUOTE, "coins": shown}

    async def candles(self, user_id, symbol, timeframe="15m", count=96):
        market = await self._market_for(symbol)
        if timeframe not in INTERVALS:
            raise ValueError("timeframe must be one of " + ", ".join(INTERVALS))
        count = max(1, min(int(count), 300))
        minutes = INTERVALS[timeframe]
        until = self._timestamp()
        since = until - (count + 2) * minutes * 60_000
        data = await self.public(
            f"/1.0/public/candles/{market['pair']}",
            {"interval": minutes, "since": since, "until": until},
        )
        rows = data.get("data") if isinstance(data, dict) else None
        series = sorted(
            (r for r in rows or [] if isinstance(r, dict) and "start" in r),
            key=lambda r: int(r["start"]),
        )
        return {
            "pair": market["pair"],
            "timeframe": timeframe,
            "candles": [
                {
                    "t": int(r["start"]),
                    "o": r.get("open"),
                    "h": r.get("high"),
                    "l": r.get("low"),
                    "c": r.get("close"),
                    "v": r.get("volume"),
                }
                for r in series[-count:]
            ],
        }

    async def book(self, user_id, symbol, depth=10):
        market = await self._market_for(symbol)
        depth = max(1, min(int(depth), 50))
        data = await self.public(
            f"/2.0/public/order-book/{market['pair']}", {"limit": depth}
        )
        side = data.get("data") if isinstance(data, dict) else None
        side = side if isinstance(side, dict) else {}

        def levels(rows, descending):
            parsed = []
            for row in rows if isinstance(rows, list) else []:
                price = _decimal(row.get("price")) if isinstance(row, dict) else None
                size = _decimal(row.get("quantity")) if isinstance(row, dict) else None
                if price and price > 0 and size and size > 0:
                    parsed.append((price, size))
            parsed.sort(key=lambda lvl: lvl[0], reverse=descending)
            return [[fmt(p), fmt(q)] for p, q in parsed[:depth]]

        return {
            "pair": market["pair"],
            "bids": levels(side.get("bids"), True),
            "asks": levels(side.get("asks"), False),
        }

    # ── account ──

    async def accounts(self, user_id):
        data = await self.private(user_id, "GET", "/1.0/balances")
        rows = data.get("data") if isinstance(data, dict) else data
        funded = []
        for row in sorted(
            (r for r in rows or [] if isinstance(r, dict)),
            key=lambda r: str(r.get("currency")),
        ):
            currency = str(row.get("currency") or "").upper()
            total = _decimal(row.get("total"), Decimal(0))
            available = _decimal(row.get("available"), Decimal(0))
            if currency and total > 0:
                funded.append(
                    {
                        "currency": currency,
                        "balance": fmt(total),
                        "available": fmt(max(available, Decimal(0))),
                    }
                )
        return {"accounts": funded}

    def fee_rates(self, user_id):
        return taker_rate(_read_ledger(user_id))

    async def portfolio(self, user_id):
        """Whole Revolut X account at the last traded price, for the dashboard (USD)."""
        accounts = (await self.accounts(user_id))["accounts"]
        tickers = await self.tickers()
        rate = await self.eur_usd()
        if accounts and not rate:
            # Every holding is priced in euros and converted here, so without the
            # rate the whole account would read as 0 USD, and that false drop would
            # be recorded as equity. Fail instead: the dashboard shows a note.
            raise ValueError("No EUR/USD rate is available to value Revolut X")
        balances, unpriced = [], []
        for row in accounts:
            token = row["currency"]
            if token == QUOTE:
                price_eur = Decimal(1)
            else:
                price_eur = self._touch(tickers.get(token) or {})[2]
            price = price_eur * Decimal(str(rate)) if price_eur and rate else None
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
            "Whole Revolut X account at the last traded price, euros converted at "
            "the USDC/EUR rate. Only the bot allocation may be traded."
        )
        if unpriced:
            note += " No USD price for " + ", ".join(unpriced) + "."
        return {
            "connector": "Revolut X",
            "balances": balances,
            "total_usd": sum(row["usd_value"] for row in balances),
            "note": note,
        }

    async def sync_from_account(self, user_id):
        """Seed the bot allocation from the Revolut X account (owner action).

        Cost basis for coins that are already in the account restarts at their
        current bid; Revolut X does not report what they cost elsewhere.
        """
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            if ledger["uncertain_order"]:
                raise ValueError("Reconcile the uncertain order before syncing")
            if ledger["enabled"]:
                raise ValueError("Pause trading before syncing the allocation")
            accounts = (await self.accounts(user_id))["accounts"]
            pairs, tickers = await self.pairs(), await self.tickers()
            cash, positions, skipped = Decimal(0), {}, []
            for row in accounts:
                currency = row["currency"]
                if currency == QUOTE:
                    cash = Decimal(row["available"])
                    continue
                units = Decimal(row["available"])
                bid = self._touch(tickers.get(currency) or {})[0]
                if currency not in pairs or not bid or units <= 0:
                    skipped.append(currency)
                    continue
                positions[currency] = {"units": fmt(units), "cost": fmt(units * bid)}
            fresh = new_ledger()
            fresh["cash"] = fmt(cash)
            fresh["positions"] = positions
            fresh["synced"] = True
            # The fee a real fill showed is not forgotten by a re-sync.
            fresh["observed_taker_fee_pct"] = ledger.get("observed_taker_fee_pct")
            _write(_root(user_id) / "ledger.json", fresh)
            return {**self.status(user_id), "skipped": skipped}

    # ── valuation ──

    async def equity(self, user_id, ledger):
        total = Decimal(ledger["cash"])
        tickers = await self.tickers() if ledger["positions"] else {}
        for symbol, position in ledger["positions"].items():
            bid = self._touch(tickers.get(symbol) or {})[0]
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
        """Size an immediate-or-cancel limit order from the live order book."""
        side = side.lower() if isinstance(side, str) else ""
        if side not in SIDES:
            raise ValueError("Use side buy or sell")
        market = await self._market_for(symbol)
        symbol, pair = market["symbol"], market["pair"]
        if side == "buy" and symbol in NOT_MARKET:
            raise ValueError(
                f"{symbol} is a stablecoin, wrapped or tokenized asset: not traded"
            )
        slip = amount(str(slippage_pct if slippage_pct else DEFAULT_SLIPPAGE_PCT))
        if slip > MAX_SLIPPAGE_PCT:
            raise ValueError(f"Slippage above {MAX_SLIPPAGE_PCT}% is not allowed")
        book = await self.book(user_id, symbol, 20)
        if not book["bids"] or not book["asks"]:
            raise ValueError(f"No live market for {pair}")
        bid, ask = Decimal(book["bids"][0][0]), Decimal(book["asks"][0][0])
        if bid <= 0 or ask <= 0 or bid > ask:
            raise ValueError(f"No live market for {pair}")
        spread = (ask - bid) / ask * 100
        if spread > MAX_SPREAD_PCT:
            raise ValueError(f"{pair} spread is {spread:.2f}%, above {MAX_SPREAD_PCT}%")
        factor = slip / 100
        if side == "buy":
            limit = _round_up(ask * (1 + factor), market["price_step"])
            quantity = _round_down(amount(value) / limit, market["base_step"])
        else:
            limit = _round_down(bid * (1 - factor), market["price_step"])
            quantity = _round_down(amount(value), market["base_step"])
        if quantity <= 0 or limit <= 0:
            raise ValueError("Amount is below the minimum order size for this pair")
        notional = quantity * limit
        if quantity < market["min_quantity"]:
            raise ValueError(
                f"Quantity {fmt(quantity)} is below Revolut X's minimum "
                f"{fmt(market['min_quantity'])} for {pair}"
            )
        if notional < market["min_value"]:
            raise ValueError(
                f"Order value {fmt(notional)} EUR is below Revolut X's minimum "
                f"{fmt(market['min_value'])} EUR for {pair}"
            )
        if market["max_quantity"] and quantity > market["max_quantity"]:
            raise ValueError(f"Quantity is above Revolut X's maximum for {pair}")
        if market["max_value"] and notional > market["max_value"]:
            raise ValueError(f"Order value is above Revolut X's maximum for {pair}")
        return {
            "side": side,
            "symbol": symbol,
            "pair": pair,
            "quantity": quantity,
            "base_step": market["base_step"],
            "min_quantity": market["min_quantity"],
            "min_value": market["min_value"],
            "limit_price": limit,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread,
            "slippage_pct": slip,
            "book": book,
        }

    async def preview(self, user_id, side, symbol, value, slippage_pct=None):
        """Evidence of cost and depth from the live public book; it places nothing."""
        plan = await self._plan(user_id, side, symbol, value, slippage_pct)
        return self._estimate(user_id, plan)["preview"]

    def _estimate(self, user_id, plan):
        """The preview of a planned order, plus the unrounded numbers the guard needs."""
        remaining, cost = plan["quantity"], Decimal(0)
        for level in plan["book"]["asks" if plan["side"] == "buy" else "bids"]:
            price, size = Decimal(level[0]), Decimal(level[1])
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
        fee = self.fee_rates(user_id)
        notional = plan["quantity"] * plan["limit_price"]
        allowance_eur = 2 * ROUNDING_ALLOWANCE_EUR_PER_LEG
        allowance_pct = allowance_eur / notional * 100
        others = plan["spread_pct"] + 2 * fee["taker_pct"] + (impact or Decimal(0))
        round_trip = others + allowance_pct
        blocked = plan["side"] == "buy" and round_trip > MAX_BUY_ROUND_TRIP_PCT
        preview = {
            "pair": plan["pair"],
            "side": plan["side"],
            "taker_fee_pct": fmt(fee["taker_pct"]),
            "fee_source": fee["source"],
            "fee_note": fee["note"],
            "estimated_round_trip_pct": str(round(round_trip, 3)),
            "round_trip_components_pct": {
                "taker_fees": str(round(2 * fee["taker_pct"], 3)),
                "spread": str(round(plan["spread_pct"], 3)),
                "impact": str(round(impact, 3)) if impact is not None else "0",
                "rounding_allowance": str(round(allowance_pct, 3)),
            },
            "rounding_allowance_eur": fmt(allowance_eur),
            "rounding_allowance_pct": str(round(allowance_pct, 3)),
            "rounding_note": (
                f"Revolut X rounds EUR amounts and fees to the cent; up to "
                f"{fmt(ROUNDING_ALLOWANCE_EUR_PER_LEG)} EUR per leg "
                f"({fmt(allowance_eur)} EUR per round trip) is added to the cost, which "
                "is why small orders look expensive"
            ),
            "cost_guard_max_round_trip_pct": str(MAX_BUY_ROUND_TRIP_PCT),
            "cost_guard_blocks_buy": blocked,
            "quantity": fmt(plan["quantity"]),
            "limit_price": fmt(plan["limit_price"]),
            "notional_eur": fmt(notional),
            "bid": fmt(plan["bid"]),
            "ask": fmt(plan["ask"]),
            "spread_pct": str(round(plan["spread_pct"], 3)),
            "estimated_average_price": fmt(average) if average is not None else None,
            "estimated_impact_pct": (
                str(round(impact, 3)) if impact is not None else None
            ),
            "book_covers_order": remaining <= 0,
        }
        return {
            "preview": preview,
            "round_trip": round_trip,
            "others": others,
            "notional": notional,
            "allowance_pct": allowance_pct,
        }

    def _note_placed(self, user_id):
        now = time.monotonic()
        placed = self.placed[user_id]
        while placed and now - placed[0] >= 86400:
            placed.popleft()
        if len(placed) >= ORDERS_PER_DAY:
            raise ValueError("Daily Revolut X order limit reached")
        placed.append(now)

    async def order(
        self, user_id, side, symbol, value, slippage_pct=None, allow_costly_buy=False
    ):
        """Place one order. ``allow_costly_buy`` is the owner's override of the cost guard
        and is offered by the owner's API only, never by the MCP tool."""
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            if not ledger["enabled"] or ledger["uncertain_order"]:
                raise ValueError("Trading is paused")
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
                if allow_costly_buy is not True:
                    self._check_cost(self._estimate(user_id, plan))
            else:
                position = ledger["positions"].get(symbol)
                if not position or plan["quantity"] > Decimal(position["units"]):
                    raise ValueError("Cannot sell assets outside the bot allocation")
            accounts = {
                a["currency"]: a for a in (await self.accounts(user_id))["accounts"]
            }
            clamped = False
            if plan["side"] == "buy":
                have = Decimal(accounts.get(QUOTE, {}).get("available", "0"))
                need = plan["quantity"] * plan["limit_price"] * (1 + FEE_HEADROOM)
            else:
                have = Decimal(accounts.get(symbol, {}).get("available", "0"))
                need = plan["quantity"]
                units = Decimal(position["units"])
                whole = plan["quantity"] >= _round_down(units, plan["base_step"])
                if whole and have < need:
                    # Selling the WHOLE position: the ledger can read a little more
                    # than the account holds (a taker fee charged in the coin, rounding),
                    # and asking for more than is available is refused. Size the exit
                    # from what the account really has.
                    clamped = True
                    self._size_whole_exit(plan, symbol, have)
                    need = plan["quantity"]
            if have < need:
                raise ValueError("Insufficient available Revolut X balance")
            recent = [o for o in ledger["orders"] if time.time() - o["time"] < 60]
            if len(recent) >= 10:
                raise ValueError("Maximum ten orders per minute")
            current = _read_ledger(user_id)
            if not current["enabled"]:
                raise ValueError("Trading paused during checks")
            # Anything that can fail without sending must fail BEFORE the uncertain
            # entry is written, or it would read as an order that may exist.
            self.credentials(user_id)
            self._note_placed(user_id)
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
            # VERIFIED by a real order (2026-10-28): client_order_id, symbol, side and
            # order_configuration; the limit takes price plus base_size, time_in_force
            # ioc and execution_instructions ["allow_taker"].
            body = {
                "client_order_id": client_oid,
                "symbol": plan["pair"],
                "side": plan["side"],
                "order_configuration": {
                    "limit": {
                        "base_size": fmt(plan["quantity"]),
                        "price": fmt(plan["limit_price"]),
                        "time_in_force": "ioc",
                        "execution_instructions": ["allow_taker"],
                    }
                },
            }
            try:
                placed = await self.private(user_id, "POST", "/1.0/orders", None, body)
            except RevolutXError as error:
                if not error.definite:
                    raise ValueError(_UNCERTAIN) from None
                # Revolut X answered and refused: no order exists, so there is nothing
                # to reconcile and trading stays as it was.
                ledger["uncertain_order"] = None
                _write(_root(user_id) / "ledger.json", ledger)
                raise ValueError(f"Revolut X rejected the order: {error}") from None
            except Exception:
                raise ValueError(_UNCERTAIN) from None
            # From here an order may exist: nothing below clears the uncertain entry
            # except a read-back that shows the final state.
            data = placed.get("data") if isinstance(placed, dict) else None
            order_id = data.get("venue_order_id") if isinstance(data, dict) else None
            if not isinstance(order_id, str) or not order_id:
                raise ValueError(_UNCERTAIN) from None
            ledger["uncertain_order"]["order_id"] = order_id
            _write(_root(user_id) / "ledger.json", ledger)
            try:
                detail = await self._final_detail(user_id, order_id)
                detail["fills"] = await self._fills(user_id, order_id, detail)
            except Exception:
                raise ValueError(_UNCERTAIN) from None
            self._apply_fill(
                ledger, plan["side"], symbol, detail, base_step=plan["base_step"]
            )
            await self._check_balances(
                user_id,
                ledger,
                plan["side"],
                symbol,
                detail,
                plan["base_step"],
                clamped,
            )
            ledger["enabled"] = (
                self.connected(user_id) and _read_ledger(user_id)["enabled"]
            )
            _write(_root(user_id) / "ledger.json", ledger)
            return {"order": self._summary(detail), "status": self.status(user_id)}

    @staticmethod
    def _size_whole_exit(plan, symbol, available):
        """Resize a whole-position sale to min(ledger units, available balance)."""
        pair = plan["pair"]
        quantity = _round_down(min(plan["quantity"], available), plan["base_step"])
        if quantity <= 0 or quantity < plan["min_quantity"]:
            raise ValueError(
                f"Insufficient available Revolut X balance: {fmt(available)} {symbol} "
                f"is available, below the {pair} minimum order size of "
                f"{fmt(plan['min_quantity'])} {symbol}; nothing was sent"
            )
        if quantity * plan["limit_price"] < plan["min_value"]:
            raise ValueError(
                f"Insufficient available Revolut X balance: the {fmt(available)} "
                f"{symbol} available is worth less than the {pair} minimum order "
                f"value of {fmt(plan['min_value'])} EUR; nothing was sent"
            )
        plan["quantity"] = quantity

    async def _check_balances(
        self, user_id, ledger, side, symbol, detail, base_step, force=False
    ):
        """After a fill whose fee was only an estimate (or a whole-position exit that
        had to be resized), read the real balances ONCE and never let the ledger claim
        more than the account holds.

        Revolut X charges the BUY fee in the coin, so an estimated fee can be a few
        units off. Only a downward correction is made: the bot allocation may be a part
        of the account, so more in the account than in the ledger is not touched.
        Best effort: a failed read leaves the ledger as the fill set it.
        """
        if detail.get("fee_source") != "estimate" and not force:
            return
        if _decimal(detail.get("filled_quantity"), Decimal(0)) <= 0:
            return
        try:
            accounts = {
                a["currency"]: a for a in (await self.accounts(user_id))["accounts"]
            }
        except Exception:  # noqa: BLE001 - the fill is already booked
            detail["balance_check"] = "unavailable"
            return
        correction = {}
        position = ledger["positions"].get(symbol)
        available = _decimal(accounts.get(symbol, {}).get("available"), Decimal(0))
        if position:
            units = Decimal(position["units"])
            if units - available > base_step:
                if available <= 0 and side == "buy":
                    # The coin just bought shows no balance at all: more likely a
                    # stale read than a lost fill. Say so, do not erase the position.
                    detail["balance_check"] = (
                        f"no {symbol} balance read after the buy; ledger not changed"
                    )
                else:
                    new_units = max(min(units, available), Decimal(0))
                    correction["units"] = {
                        "ledger": fmt(units),
                        "account_available": fmt(available),
                        "set_to": fmt(new_units),
                    }
                    if new_units <= 0:
                        del ledger["positions"][symbol]
                    else:
                        position["units"] = fmt(new_units)
                        position["units_source"] = "account_balance"
        cash = Decimal(ledger["cash"])
        eur = _decimal(accounts.get(QUOTE, {}).get("available"), Decimal(0))
        if QUOTE in accounts and cash > eur:
            correction["cash"] = {
                "ledger": fmt(cash),
                "account_available": fmt(eur),
                "set_to": fmt(eur),
            }
            ledger["cash"] = fmt(eur)
            ledger["cash_source"] = "account_balance"
        if correction:
            correction["units_source"] = "account_balance"
            detail["balance_correction"] = correction
            if ledger["orders"]:
                ledger["orders"][-1]["balance_correction"] = correction
        elif "balance_check" not in detail:
            detail["balance_check"] = "ledger within one lot of the account balance"

    @staticmethod
    def _check_cost(estimate):
        """Refuse a buy that cent rounding and fees would eat. Nothing has been sent."""
        round_trip = estimate["round_trip"]
        if round_trip <= MAX_BUY_ROUND_TRIP_PCT:
            return
        room = MAX_BUY_ROUND_TRIP_PCT - estimate["others"]
        if room > 0:
            needed = (2 * ROUNDING_ALLOWANCE_EUR_PER_LEG / room * 100).quantize(
                Decimal("1"), rounding="ROUND_UP"
            )
            advice = f"an order of about {needed} EUR or more would be cheaper"
        else:
            advice = "fees and spread alone are already above the limit"
        raise ValueError(
            f"Refused, no order placed: the estimated round-trip cost of this buy is "
            f"{round_trip:.2f}% (cent-rounding allowance "
            f"{estimate['allowance_pct']:.2f}% of a {estimate['notional']:.2f} EUR "
            f"order), above the {MAX_BUY_ROUND_TRIP_PCT}% limit; {advice}. "
            "Selling a position is never blocked by this."
        )

    @staticmethod
    def _normalize(info):
        """The fields this module reads from a Revolut X order object, in one shape."""
        return {
            "order_id": info.get("id"),
            "client_oid": info.get("client_order_id"),
            "symbol": _base_of(info.get("symbol")),
            "pair": info.get("symbol"),
            "side": str(info.get("side") or "").lower(),
            "type": info.get("type"),
            "status": str(info.get("status") or "").lower(),
            "quantity": info.get("quantity"),
            "filled_quantity": info.get("filled_quantity"),
            "leaves_quantity": info.get("leaves_quantity"),
            "amount": info.get("amount"),
            "filled_amount": info.get("filled_amount"),
            "limit_price": info.get("price"),
            "average_fill_price": info.get("average_fill_price"),
            "total_fee": info.get("total_fee"),
            "fee_currency": info.get("fee_currency"),
            "reject_reason": info.get("reject_reason"),
            "created": info.get("created_date"),
            "updated": info.get("updated_date"),
        }

    async def _detail(self, user_id, order_id):
        """One order by its Revolut X id, or None when Revolut X does not know it."""
        try:
            data = await self.private(user_id, "GET", f"/1.0/orders/{order_id}")
        except RevolutXError as error:
            if error.status == 404:
                return None
            raise
        info = data.get("data") if isinstance(data, dict) else None
        return self._normalize(info) if isinstance(info, dict) else None

    async def _is_active(self, user_id, order_id):
        for info in await self._pages(user_id, "/1.0/orders/active", {"limit": 300}):
            if info.get("id") == order_id:
                return True
        return False

    async def _final_detail(self, user_id, order_id):
        detail = None
        for attempt in range(ORDER_POLLS):
            try:
                detail = await self._detail(user_id, order_id)
            except ValueError:
                # A refused or failed read (rate limit, transport) is not an answer
                # about the order; keep polling and fall through to "uncertain".
                detail = None
            if detail and detail["status"] in FINAL_STATUSES:
                return detail
            if attempt + 1 < ORDER_POLLS:
                await asyncio.sleep(ORDER_POLL_WAIT_SEC)
        # An immediate-or-cancel order cannot rest. If it traded in part and is no
        # longer among the active orders, that is its final state (UNVERIFIED: which
        # status Revolut X reports for an IOC partial fill).
        if (
            detail
            and detail["status"] == "partially_filled"
            and not await self._is_active(user_id, order_id)
        ):
            return detail
        raise ValueError("Order did not reach a final state")

    async def _fills(self, user_id, order_id, detail):
        """The trades behind an order, best effort: evidence, never the source of truth."""
        quantity = _decimal(detail.get("filled_quantity"), Decimal(0))
        if quantity <= 0:
            return None
        try:
            data = await self.private(user_id, "GET", f"/1.0/orders/fills/{order_id}")
        except Exception:  # noqa: BLE001 - evidence only; the order object decides
            return None
        rows = data.get("data") if isinstance(data, dict) else None
        count, base, value, makers, takers = 0, Decimal(0), Decimal(0), 0, 0
        for row in rows if isinstance(rows, list) else []:
            price, size = _decimal(row.get("p")), _decimal(row.get("q"))
            if not isinstance(row, dict) or not price or not size:
                continue
            count += 1
            base += size
            value += price * size
            if row.get("im") is True:
                makers += 1
            elif row.get("im") is False:
                takers += 1
        if not count:
            return None
        return {
            "trades": count,
            "quantity": fmt(base),
            "value": fmt(value),
            "average_price": fmt(value / base),
            "maker_trades": makers,
            "taker_trades": takers,
        }

    @staticmethod
    def _summary(detail):
        keys = (
            "order_id",
            "client_oid",
            "pair",
            "side",
            "status",
            "quantity",
            "filled_quantity",
            "limit_price",
            "average_fill_price",
            "reported_average_fill_price",
            "price_note",
            "filled_amount",
            "fills_value",
            "rounding_cost_eur",
            "realised_cost_eur",
            "total_fee",
            "fee_currency",
            "fee_source",
            "estimated_fee",
            "estimated_fee_currency",
            "balance_correction",
            "balance_check",
            "fills",
            "reject_reason",
            "updated",
        )
        return {k: detail.get(k) for k in keys if detail.get(k) is not None}

    def _apply_fill(
        self, ledger, side, symbol, detail, base_step=Decimal("0.00000001")
    ):
        status = detail["status"]
        if status not in FINAL_STATUSES and not (
            status == "partially_filled"
            and _decimal(detail.get("filled_quantity"), Decimal(0)) > 0
        ):
            raise ValueError("Order has not reached a final state")
        quantity = _decimal(detail.get("filled_quantity"), Decimal(0))
        average = _decimal(detail.get("average_fill_price"))
        value = _decimal(detail.get("filled_amount"), Decimal(0))
        # The fills carry the real price. The order's filled_amount and
        # average_fill_price are rounded to the cent (a sale at 72776 reported 72368),
        # so the price comes from the fills when they add up to the filled quantity.
        fills = detail.get("fills") if isinstance(detail.get("fills"), dict) else {}
        fills_qty = _decimal(fills.get("quantity"), Decimal(0))
        fills_value = _decimal(fills.get("value"), Decimal(0))
        fills_price = None
        if (
            quantity > 0
            and fills_qty > 0
            and fills_value > 0
            and abs(fills_qty - quantity) <= quantity * FILLS_QUANTITY_TOLERANCE
        ):
            fills_price = fills_value / fills_qty
        if value <= 0 and fills_price is not None:
            value = fills_value
        elif value <= 0 and quantity > 0 and average:
            value = quantity * average
        # The fee: the order object carries total_fee and fee_currency, but it may not
        # carry them yet when it is read back. When the real figure is missing, the
        # published taker rate is applied and LABELLED an estimate; it never silently
        # becomes zero. VERIFIED live (2026-10-29): a BUY's fee is charged in the BASE
        # currency (the balance is the filled quantity minus the fee), a SELL's in EUR,
        # so an estimated buy fee reduces the position units and not the cash.
        reported = _decimal(detail.get("total_fee"))
        fee_currency = str(detail.get("fee_currency") or "").upper()
        fee_in_base = fee_currency == symbol
        # A reported zero is not trusted on a fill that crossed the book (a taker pays
        # something unless the plan waives it, and an unfilled field also reads 0):
        # the estimate is charged to the ledger, which errs on the side of less cash.
        usable = (
            reported is not None and reported > 0 and fee_currency in (QUOTE, symbol)
        )
        if quantity > 0 and usable:
            fee, fee_source = reported, "reported"
        elif quantity > 0:
            rate = taker_rate(ledger)["taker_pct"]
            fee_source = "estimate"
            if side == "buy":
                # Rounded UP to the lot: the ledger must never hold more than the account.
                fee = min(_round_up(quantity * rate / 100, base_step), quantity)
                fee_currency, fee_in_base = symbol, True
            else:
                fee = value * rate / 100
                fee_currency, fee_in_base = QUOTE, False
            detail["estimated_fee"] = fmt(fee)
            detail["estimated_fee_currency"] = fee_currency
        else:
            fee, fee_source, fee_currency = Decimal(0), "none", QUOTE
        fee_units = fee if fee_in_base else Decimal(0)
        fee_eur = Decimal(0) if fee_in_base else fee
        if quantity > 0:
            if side == "buy":
                units = quantity - fee_units
                spent = value + fee_eur
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
                proceeds = value - fee_eur
                out = min(quantity + fee_units, Decimal(position["units"]))
                fraction = out / Decimal(position["units"])
                cost = Decimal(position["cost"]) * fraction
                if fraction == 1:
                    del ledger["positions"][symbol]
                else:
                    position["units"] = fmt(Decimal(position["units"]) - out)
                    position["cost"] = fmt(Decimal(position["cost"]) - cost)
                ledger["cash"] = fmt(Decimal(ledger["cash"]) + proceeds)
            if (
                fee_source == "reported"
                and fee_currency == QUOTE
                and value >= OBSERVED_FEE_MIN_VALUE_EUR
            ):
                # The first real fee on a fill big enough to read tells the truth about
                # the plan's rate; a cent-rounded fee on a tiny fill does not.
                ledger["observed_taker_fee_pct"] = fmt(
                    (fee / value * 100).quantize(Decimal("0.0001"))
                )
        detail["fee_source"] = fee_source
        evidence = {}
        if quantity > 0 and fills_price is not None:
            # The ledger moves by the EUR amounts Revolut X reports (they are what the
            # balance really did); the realised cost is those amounts, against the
            # fills' value, plus the fee.
            rounding = value - fills_value if side == "buy" else fills_value - value
            evidence = {
                "fills_value": fmt(fills_value),
                "rounding_cost_eur": fmt(rounding),
                "realised_cost_eur": fmt(rounding + fee_eur + fee_units * fills_price),
            }
            detail["reported_average_fill_price"] = detail.get("average_fill_price")
            detail["average_fill_price"] = fmt(fills_price)
            detail.update(evidence)
            if average and abs(average - fills_price) / fills_price * 100 > (
                PRICE_NOTE_PCT
            ):
                detail["price_note"] = (
                    f"the order's reported average_fill_price {fmt(average)} differs "
                    f"from the fills-derived {fmt(fills_price)}: Revolut X rounds EUR "
                    "amounts to the cent, so the fills-derived price is used"
                )
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
                    "fee_currency": fee_currency,
                    "fee_source": fee_source,
                    **evidence,
                }
            ]
        )[-200:]

    async def _pages(self, user_id, path, query, pages=4):
        """Orders from a paginated list (``data`` plus ``metadata.next_cursor``)."""
        found, params = [], dict(query)
        for _ in range(pages):
            data = await self.private(user_id, "GET", path, params)
            rows = data.get("data") if isinstance(data, dict) else None
            found += [r for r in rows or [] if isinstance(r, dict)]
            meta = data.get("metadata") if isinstance(data, dict) else None
            cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
            if not cursor or not rows:
                break
            params["cursor"] = cursor
        return found

    async def history(self, user_id):
        """The most recent final orders (Revolut X lists the last seven days by default)."""
        end = self._timestamp()
        rows = await self._pages(
            user_id,
            "/1.0/orders/historical",
            {"start_date": end - 7 * 86_400_000, "end_date": end, "limit": 100},
            pages=1,
        )
        details = [self._normalize(row) for row in rows]
        details.sort(key=lambda r: int(r.get("updated") or 0), reverse=True)
        return [self._summary(row) for row in details][:50]

    async def _find_order(self, user_id, pending):
        """Look for the uncertain order among the active and the historical orders.

        Matched by ``client_order_id``, which every order record carries.
        """
        cid = pending["client_oid"]
        for row in await self._pages(user_id, "/1.0/orders/active", {"limit": 300}):
            if row.get("client_order_id") == cid:
                return self._normalize(row)
        start = int(float(pending["time"]) * 1000) - 300_000
        end = self._timestamp() + 60_000
        rows = await self._pages(
            user_id,
            "/1.0/orders/historical",
            {"start_date": start, "end_date": end, "limit": 1900},
        )
        for row in rows:
            if row.get("client_order_id") == cid:
                return self._normalize(row)
        return None

    async def reconcile(self, user_id):
        """Settle an order whose outcome was unknown. Leaves trading paused."""
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            pending = ledger["uncertain_order"]
            if not pending:
                return self.status(user_id)
            detail = None
            if pending.get("order_id"):
                detail = await self._detail(user_id, pending["order_id"])
            if detail is None:
                detail = await self._find_order(user_id, pending)
            if detail is None:
                if time.time() - pending["time"] < 60:
                    raise ValueError("Wait a minute before reconciling")
                # Not among the active orders and not in the history after a minute:
                # Revolut X never created it.
                ledger["uncertain_order"] = None
                ledger["enabled"] = False
                _write(_root(user_id) / "ledger.json", ledger)
                return self.status(user_id)
            if detail["status"] not in FINAL_STATUSES:
                settled = detail[
                    "status"
                ] == "partially_filled" and not await self._is_active(
                    user_id, detail["order_id"]
                )
                if not settled:
                    raise ValueError("The order is still open; reconcile again shortly")
            detail["fills"] = await self._fills(user_id, detail["order_id"], detail)
            try:
                base_step = (await self._market_for(pending["symbol"]))["base_step"]
            except Exception:  # noqa: BLE001 - the lot only rounds an estimated fee
                base_step = Decimal("0.00000001")
            self._apply_fill(
                ledger, pending["side"], pending["symbol"], detail, base_step=base_step
            )
            await self._check_balances(
                user_id, ledger, pending["side"], pending["symbol"], detail, base_step
            )
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
        """The live calls scored at 2 x the taker fee plus the market's typical spread."""
        cost = DEFAULT_ROUND_TRIP_COST_PCT
        try:
            spreads = sorted(
                (
                    Decimal(row["spread_pct"])
                    for row in (await self.coins(user_id))["coins"][:15]
                )
            )
            if spreads:
                taker = self.fee_rates(user_id)["taker_pct"]
                cost = float(2 * taker + spreads[len(spreads) // 2])
        except ValueError:
            pass  # no market answer: keep the stated default
        return scorecard.report(user_id, cost_pct=cost, venue=VENUE)


service = RevolutXService()
