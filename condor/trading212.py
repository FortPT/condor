"""Live Trading 212 (US stocks, Invest account in EUR) trading on Trading 212's public API.

Account keys never enter agent tool arguments. The bot trades only US stocks that
Trading 212 lists AND the Kraken xStocks public feed covers (that feed is where the
reference prices come from), only in an account whose currency is EUR, and only while
the regular US session is open. Every order is one call, idempotent by construction
(the API has no client order id): the order is written to the ledger BEFORE it is
sent, and an outcome that is not certain pauses trading until it is reconciled.

Trading 212 has no market-data endpoint. Prices used to size and limit an order, the
candles and the "stocks" table are REFERENCE prices in USD from Kraken's public
xStocks data (a tokenized copy of the same stocks), labelled as such everywhere. The
real fill price, quantity and fees come only from Trading 212's own order history and
account, never from the reference.

The ledger owns only this bot's allocation, in EUR, keyed by the plain ticker
(``AAPL``) while Trading 212's own names are ``AAPL_US_EQ``. It is seeded from the
Trading 212 account by :meth:`Trading212Service.sync_from_account`.

VERIFIED against the public documentation (docs.trading212.com, read 2026-10-29, see
``docs/trading212.md``): the base URLs, HTTP Basic auth with API key and secret, the
endpoint paths and their published rate limits, the fields of the account summary,
positions, pending/historical order and transaction answers, the ``orders:execute``
scope, ``DAY``/``GOOD_TILL_CANCEL`` time validity, negative quantity to sell, and that
the order endpoints are NOT idempotent.

VERIFIED against the real account by the first live order (2026-10-29, see
``docs/trading212.md``): a limit order with a four-decimal fractional quantity is
accepted and fills instantly; ``GET /equity/orders`` answers a bare array; the account
summary and positions reflect a fill at once and are the ground truth; the history can
OMIT an API order for over a minute, so an order's outcome is never concluded from the
history's silence (the account's cash and the ticker's quantity are read against a
snapshot taken before the order); the history's shape for a fill, incl. the negative
``CURRENCY_CONVERSION_FEE`` tax of -0.01 EUR and ``fxRate`` 1.1125.

NOT VERIFIED against a real account (comments say so where it matters): everything
else that only a real call can show, notably a sale and a partial fill, what the
history lists for a cancelled order, whether ``netValue`` includes the fee, the error
bodies and rate-limit headers, and the fee and FX numbers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit

from condor import crypto_com_scorecard as scorecard
from condor import xstocks_market
from condor.fsutil import atomic_write_json
from condor.paths import data_dir

VENUE = "trading212"
# LIVE only. There is deliberately no demo mode: the owner trades real money.
BASE_URL = "https://live.trading212.com/api/v0"
API_PREFIX = "/api/v0"
QUOTE = "EUR"
#: The currency instruments are priced in, and the reference feed too.
PRICE_CURRENCY = "USD"
SIDES = ("buy", "sell")
ORDER_TYPES = ("limit", "market")
#: Order lifecycle states of the documented enum that can no longer trade.
FINAL_STATUSES = {"filled", "cancelled", "canceled", "rejected", "replaced"}
_UNCERTAIN = (
    "Order outcome needs reconciliation. Trading paused; check the Trading 212 order "
    "history. Do not resubmit."
)
REFERENCE_NOTE = (
    "Reference prices in USD from Kraken xStocks public data (a tokenized copy of the "
    "stock), not Trading 212 quotes: Trading 212 has no quote endpoint. Orders fill "
    "at Trading 212's own price."
)

# Below this total bot equity (EUR) the bot may use all of its money freely; only
# the equity floor and the single-asset cap apply. At or above it the reserve,
# position-count and one-sixth rules apply as well (same rules as the other venues).
GUARD_THRESHOLD = Decimal("1000")
MAX_ASSET_FRACTION = Decimal("0.4")
FLOOR = Decimal("2")
TOP_STOCKS = 40
#: Limit orders are priced this far through the reference price, so a fast market
#: cannot fill them at a worse price than the caller accepted.
DEFAULT_SLIPPAGE_PCT = Decimal("0.3")
MAX_SLIPPAGE_PCT = Decimal("2")
MAX_SPREAD_PCT = Decimal("1")
#: A buy must leave this much cash beyond its notional (FX fee, price drift).
FEE_HEADROOM = Decimal("0.01")
#: Sizing divides the money by the price in EUR AND this margin: the EUR/USD rate
#: used here (Kraken's public EURUSD) is not Trading 212's own conversion rate.
FX_BUFFER = Decimal("0.005")
#: The least a buy may be, whatever the instrument says.
MIN_ORDER_EUR = Decimal("1")
#: Quantities are rounded DOWN to this step. ASSUMPTION (not verified): the API accepts
#: fractional quantities with at least four decimals. A full close sells the exact
#: quantity the account reports instead, so no dust is left behind.
QTY_STEP = Decimal("0.0001")
PRICE_STEP = Decimal("0.01")
MIN_PRICE_USD = Decimal("1")

# ── the cost model (percent of the order value) ──
#: Trading 212's published FX fee, charged on each leg when the instrument's currency
#: differs from the account's (docs: helpcentre "What is the FX fee?"). Zero commission.
PUBLISHED_FX_FEE_PCT = Decimal("0.15")
#: ESTIMATES. The market spread is not published by Trading 212: the Kraken reference
#: spread stands in for it, never below this floor.
SPREAD_FLOOR_PCT = Decimal("0.02")
#: ASSUMPTION: what a fill may cost beyond the touch, per leg.
SLIPPAGE_ALLOWANCE_PCT_PER_LEG = Decimal("0.03")
#: ESTIMATE: US regulatory fees on a sale (SEC and FINRA charges are well below this).
REGULATORY_ALLOWANCE_PCT = Decimal("0.003")
#: ASSUMPTION carried over from the Revolut X lesson: EUR amounts and fees are rounded
#: to the cent, so a tiny order looks as expensive as it is.
ROUNDING_ALLOWANCE_EUR_PER_LEG = Decimal("0.01")
#: A BUY whose estimated round trip is above this many percent is refused. A sale is
#: never blocked by it: an exit must always be possible.
MAX_BUY_ROUND_TRIP_PCT = Decimal("1.0")
#: A cent-rounded fee says little about the rate on a small fill: the FX rate is learned
#: only from a fill at least this large (EUR).
OBSERVED_FEE_MIN_VALUE_EUR = Decimal("50")
#: A market order filled this far from the reference is flagged.
MARKET_DEVIATION_PCT = Decimal("2")
#: Fills and the account's cash move must agree within this fraction to be trusted.
CASH_AGREEMENT = Decimal("0.10")
FILLS_QUANTITY_TOLERANCE = Decimal("0.005")

PENDING_POLLS = 6
CANCEL_POLLS = 5
HISTORY_POLLS = 3
POLL_WAIT_SEC = 2.0
#: Account-evidence polls (positions and account summary) when the history does not
#: list an order the API accepted; the account is the ground truth and updates at once.
EVIDENCE_POLLS = 3
EVIDENCE_WAIT_SEC = 2.0
#: Reconcile may clear an order as "never created" only when the pending list, the
#: history AND the account (cash and the ticker's position) all show no trace of it for
#: this long. Reconcile's own minimum wait before its first answer stays 60 s.
MIN_RECONCILE_WAIT_SEC = 60
NOT_SEEN_AFTER_SEC = 180
#: Cash (EUR) and quantity differences below these are noise, not movement.
CASH_TOLERANCE_EUR = Decimal("0.05")
QTY_EPSILON = Decimal("0.00001")
#: How long a successful ledger-vs-account check allows trading to be enabled.
VERIFY_TTL_SEC = 120.0
# The full instrument list (about 17k rows) is far larger than any other answer; it is fetched
# at most once per INSTRUMENT_TTL_SEC and Trading 212 allows it once per 50 s.
MAX_BYTES = 32 * 1024 * 1024
MAX_PAGES = 4
INSTRUMENT_TTL_SEC = 1800
EXCHANGE_TTL_SEC = 1800
ORDERS_PER_MINUTE = 10

#: Published per-endpoint limits (per account): summary 1/5s, pending orders 1/5s,
#: order by id 1/1s, positions 1/1s, instruments 1/50s, exchanges 1/30s, limit order
#: 1/2s, market order 50/min, cancel 50/min, history and transactions 20/min. The gaps
#: are a little wider than those, so Condor never reaches them.
GAPS = {
    "summary": 5.5,
    "positions": 1.2,
    "orders": 5.5,
    "order": 1.2,
    "place_limit": 2.2,
    "place_market": 1.3,
    "cancel": 1.3,
    "instruments": 55.0,
    "exchanges": 33.0,
    "history": 3.2,
    "transactions": 3.2,
}
#: The longest Condor waits for its own pacing; a longer wait is a refusal, not a stall.
MAX_LOCAL_WAIT_SEC = 12.0
MAX_BLOCK_SEC = 300.0

#: The only requests Condor will ever make: ``(method, path pattern) -> pacing class``.
#: There is no way to reach a withdrawal (none exists), pies or reports through here.
_ROUTES = (
    ("GET", re.compile(r"/equity/account/summary"), "summary"),
    ("GET", re.compile(r"/equity/positions"), "positions"),
    ("GET", re.compile(r"/equity/orders"), "orders"),
    ("GET", re.compile(r"/equity/orders/\d{1,20}"), "order"),
    ("POST", re.compile(r"/equity/orders/limit"), "place_limit"),
    ("POST", re.compile(r"/equity/orders/market"), "place_market"),
    ("DELETE", re.compile(r"/equity/orders/\d{1,20}"), "cancel"),
    ("GET", re.compile(r"/equity/metadata/instruments"), "instruments"),
    ("GET", re.compile(r"/equity/metadata/exchanges"), "exchanges"),
    ("GET", re.compile(r"/equity/history/orders"), "history"),
    ("GET", re.compile(r"/equity/history/transactions"), "transactions"),
)

TICKER = re.compile(r"([A-Z0-9]{1,12})_([A-Z0-9]{2,6})_EQ")


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
    return xstocks_market.symbol_of(value)


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


def _clean(text) -> str:
    return re.sub(r"[^A-Za-z0-9 _.,:;()/-]", "", str(text))[:120]


def _number(value) -> float:
    """A Decimal as a JSON number (Decimal itself is not serializable)."""
    return float(value)


def _parse_time(value) -> float | None:
    """Epoch seconds of an ISO 8601 timestamp, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def auth_header(api_key: str, api_secret: str) -> str:
    """The ``Authorization`` header: HTTP Basic ``key:secret``, or the bare key (legacy)."""
    if api_secret:
        token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        return "Basic " + token
    return api_key


def request_class(method: str, path: str) -> str:
    """The pacing class of an allowed request; anything else is refused outright."""
    for verb, pattern, name in _ROUTES:
        if verb == method and pattern.fullmatch(path):
            return name
    raise ValueError("Unsupported Trading 212 request")


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
        # Confirmed from the account at the last sync; trading needs it to be EUR.
        "account_currency": None,
        # The plain ticker -> Trading 212's own ticker of every position the bot holds.
        "tickers": {},
        # The FX fee rate (percent) the last real fill reported; None until then.
        "observed_fx_fee_pct": None,
        # Plain symbols of the owner's own holdings the bot must never adopt or trade.
        "excluded_tickers": [],
    }


def _read_ledger(user_id):
    path = _root(user_id) / "ledger.json"
    ledger = json.loads(path.read_text()) if path.exists() else new_ledger()
    for key, value in new_ledger().items():
        ledger.setdefault(key, value)
    return ledger


def guards_active(equity):
    return equity >= GUARD_THRESHOLD


def fx_fee_rate(ledger):
    """The FX fee per leg in percent, and where it comes from.

    The rate the last real fill reported when there is one; otherwise Trading 212's
    published rate, as an ESTIMATE until a real fill confirms it.
    """
    observed = _decimal(ledger.get("observed_fx_fee_pct"))
    if observed is not None and observed > 0:
        return {
            "fx_fee_pct": observed,
            "source": "observed",
            "note": "FX fee observed on the last real fill",
        }
    return {
        "fx_fee_pct": PUBLISHED_FX_FEE_PCT,
        "source": "published",
        "note": (
            f"Trading 212's published FX fee of {PUBLISHED_FX_FEE_PCT}% per leg "
            "(zero commission); confirmed only by a real fill"
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


class Trading212Error(ValueError):
    """Trading 212 answered with an error status; only a bounded, sanitized text is kept.

    ``definite`` means the request was refused before any order could exist: every
    HTTP 4xx except 408 (a 429 included) was processed and rejected at the door, and a
    local refusal sent nothing. HTTP 5xx, a 408, a timeout or a transport error may
    have created the order and stay uncertain.
    """

    def __init__(self, status, message="", secret_values=(), retry_after=None):
        text = str(message or "")
        # Error text is untrusted and can echo credentials; keep it short and plain.
        if any(v and v in text for v in secret_values):
            text = ""
        text = _clean(text)
        super().__init__(
            f"Trading 212 rejected the request (HTTP {status}"
            + (f": {text}" if text else "")
            + ")"
        )
        self.status = int(status)
        self.message = text
        self.retry_after = retry_after
        self.definite = self.status == 0 or (
            400 <= self.status < 500 and self.status != 408
        )


class _NotInHistory(ValueError):
    """The order left the pending list but the history does not list it."""

    def __init__(self, cancelled=False):
        super().__init__("Order is not in the Trading 212 history")
        self.cancelled = cancelled


class _Inconclusive(ValueError):
    """The account evidence does not settle an order's outcome; it stays uncertain."""


class Trading212Service:
    def __init__(self):
        self.locks = defaultdict(asyncio.Lock)
        self.clock = time.time
        self.mono = time.monotonic
        self.sleep = asyncio.sleep
        self.gaps = dict(GAPS)
        self.market_feed = xstocks_market.service
        self._next_ok = {}
        self._blocked = {}
        self._cache = {}
        self._instruments = {}
        self._exchanges = {}
        self._eur_usd = (0.0, None)
        #: ``user -> clock time`` of the last ledger-vs-account check that matched.
        self._verified = {}
        #: ``user -> the last ledger-vs-account check`` (memory only: never written
        #: from a read path, so it cannot overwrite a concurrent ledger write).
        self._checks = {}

    # ── credentials and switches ──

    def _credential_file(self, user_id):
        try:
            data = json.loads(credentials_path(user_id).read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = None
        key = data.get("api_key") if isinstance(data, dict) else None
        if not isinstance(key, str) or not key:
            raise ValueError("Connect a Trading 212 API key in Settings → Trading 212")
        secret = data.get("api_secret")
        return key, secret if isinstance(secret, str) else ""

    def credentials(self, user_id):
        return self._credential_file(user_id)

    def connected(self, user_id):
        try:
            self._credential_file(user_id)
        except ValueError:
            return False
        return True

    def _market(self):
        return xstocks_market.us_market_open(self.clock())

    def status(self, user_id):
        ledger = _read_ledger(user_id)
        rate = fx_fee_rate(ledger)
        has_secret = False
        if self.connected(user_id):
            has_secret = bool(self._credential_file(user_id)[1])
        return {
            "connected": self.connected(user_id),
            "has_secret": has_secret,
            "venue": "Trading 212",
            "quote": QUOTE,
            "account_currency": ledger.get("account_currency"),
            "enabled": ledger["enabled"] and not ledger["uncertain_order"],
            "ledger": ledger,
            "excluded_tickers": list(ledger.get("excluded_tickers") or []),
            "account_check": self._checks.get(user_id),
            "market": self._market(),
            "limits": {
                "floor": str(FLOOR),
                "reserve_fraction": "0.5",
                "position_fraction": "1/6",
                "max_positions": 3,
                "guards_apply_from": str(GUARD_THRESHOLD),
                "max_asset_fraction_below_threshold": str(MAX_ASSET_FRACTION),
                "default_slippage_pct": str(DEFAULT_SLIPPAGE_PCT),
                "pairs": "US stocks listed on Trading 212 and covered by the "
                "Kraken xStocks reference feed, in a EUR account",
                "session": "regular US session only (Mon-Fri 09:30-16:00 New York)",
                "fx_fee_pct_each_way": str(rate["fx_fee_pct"]),
                "fx_fee_source": rate["source"],
                "max_buy_round_trip_pct": str(MAX_BUY_ROUND_TRIP_PCT),
                "min_order_eur": str(MIN_ORDER_EUR),
                "max_orders_per_minute": ORDERS_PER_MINUTE,
            },
        }

    def save_credentials(self, user_id, key, secret=""):
        secret = secret if secret is not None else ""
        for value, required in ((key, True), (secret, False)):
            if (
                not isinstance(value, str)
                or (required and not value.strip())
                or len(value) > 4096
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Invalid API key or secret")
        key, secret = key.strip(), secret.strip()
        if ":" in key:
            raise ValueError("Invalid API key")
        self.set_enabled(user_id, False)
        self._forget(user_id)
        _write(credentials_path(user_id), {"api_key": key, "api_secret": secret})
        return self.status(user_id)

    def _forget(self, user_id):
        for table in (self._cache, self._next_ok, self._blocked):
            for key in [k for k in table if k[0] == user_id]:
                del table[key]
        self._instruments.pop(user_id, None)
        self._exchanges.pop(user_id, None)
        self._verified.pop(user_id, None)
        self._checks.pop(user_id, None)

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
                    "Sync the bot allocation from the Trading 212 account first"
                )
            if ledger.get("account_currency") != QUOTE:
                raise ValueError(
                    "The Trading 212 account currency is not confirmed as EUR; sync again"
                )
            verified = self._verified.get(user_id)
            if verified is None or not 0 <= self.clock() - verified <= VERIFY_TTL_SEC:
                raise ValueError(
                    "The bot ledger has not just been checked against the Trading 212 "
                    "account: use Enable trading in Settings (it checks first) or sync "
                    "the allocation"
                )
        ledger["enabled"] = bool(enabled)
        _write(_root(user_id) / "ledger.json", ledger)
        return self.status(user_id)

    async def enable_checked(self, user_id):
        """Enable trading only when the bot ledger and the real account agree.

        The ledger owns the bot's cash and positions; the account is the ground truth.
        A disagreement (an order the ledger never booked, a deposit, a manual trade of
        a ticker the bot trades) means the ledger cannot be trusted to size orders, so
        the owner must sync first (the same step as "Pause trading to sync").
        """
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            self.credentials(user_id)
            if ledger["uncertain_order"]:
                raise ValueError(
                    "An order needs reconciliation before trading can resume"
                )
            if not ledger.get("synced"):
                raise ValueError(
                    "Sync the bot allocation from the Trading 212 account first"
                )
            if ledger.get("account_currency") != QUOTE:
                raise ValueError(
                    "The Trading 212 account currency is not confirmed as EUR; sync again"
                )
            summary = await self._summary(user_id, fresh=True)
            self._require_eur(summary)
            positions = await self._positions(user_id, fresh=True)
            check = self.account_check(user_id, ledger, summary, positions)
            if not check["ledger_matches_account"]:
                raise ValueError(
                    "The bot ledger and the Trading 212 account disagree, so trading "
                    "cannot be enabled: "
                    + "; ".join(m["message"] for m in check["mismatch"])
                    + ". Pause trading and use Sync allocation to re-seed the ledger "
                    "from the account (positions listed under outside_allocation or "
                    "in the exclusion list stay out of the bot allocation), then "
                    "enable again."
                )
            self._verified[user_id] = self.clock()
            return self.set_enabled(user_id, True)

    async def set_excluded(self, user_id, tickers):
        """Replace the owner's exclusion list: holdings the bot never adopts or trades.

        Plain symbols (``MRNA``) or Trading 212 tickers (``MRNA_US_EQ``). Changing the
        list pauses trading, because a symbol the ledger holds is no longer allowed.
        """
        if not isinstance(tickers, list) or len(tickers) > 50:
            raise ValueError("Send a list of at most 50 tickers")
        cleaned = []
        for item in tickers:
            text = item.strip().upper() if isinstance(item, str) else ""
            match = TICKER.fullmatch(text)
            symbol = match.group(1) if match else text
            if not re.fullmatch(r"[A-Z0-9]{1,12}", symbol):
                raise ValueError("Use plain stock symbols such as MRNA")
            if symbol not in cleaned:
                cleaned.append(symbol)
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            ledger["excluded_tickers"] = sorted(cleaned)
            ledger["enabled"] = False
            self._verified.pop(user_id, None)
            _write(_root(user_id) / "ledger.json", ledger)
        return self.status(user_id)

    def disconnect(self, user_id):
        self.set_enabled(user_id, False)
        credentials_path(user_id).unlink(missing_ok=True)
        self._forget(user_id)
        return self.status(user_id)

    # ── HTTP ──

    async def _transport(self, method, url, headers, body):
        """One HTTP exchange: ``(status, parsed JSON, response headers)``.

        A timeout, a transport failure or an unreadable body raises ValueError: for
        an order that is an UNCERTAIN outcome, never a refusal.
        """
        import aiohttp

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.request(
                    method,
                    url,
                    data=body,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("Trading 212 response too large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    data = json.loads(raw) if raw.strip() else {}
                    return response.status, data, dict(response.headers)
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
            raise ValueError(
                "Trading 212 unavailable or returned an invalid response; "
                "order requests are never retried"
            ) from None

    async def _pace(self, user_id, cls):
        """Keep inside the published limits; a wait that is too long is a refusal.

        Nothing has been sent when this refuses, so it is a definite local refusal.
        """
        key = (user_id, cls)
        now = self.mono()
        blocked = self._blocked.get(key, 0.0)
        if blocked > now:
            raise Trading212Error(0, f"local rate limit, wait {blocked - now:.0f}s")
        start = max(now, self._next_ok.get(key, 0.0))
        wait = start - now
        if wait > MAX_LOCAL_WAIT_SEC:
            raise Trading212Error(0, f"local rate limit, wait {wait:.0f}s")
        self._next_ok[key] = start + self.gaps[cls]
        if wait > 0:
            await self.sleep(wait)

    def _result(self, user_id, cls, status, data, headers, secret_values=()):
        if status in (200, 201, 204):
            return data
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        retry = None
        if status == 429:
            # Honour Retry-After, else the x-ratelimit-reset timestamp (both unverified).
            try:
                retry = float(headers.get("retry-after"))
            except (TypeError, ValueError):
                try:
                    retry = float(headers.get("x-ratelimit-reset")) - self.clock()
                except (TypeError, ValueError):
                    retry = None
            retry = min(max(retry if retry is not None else 5.0, 1.0), MAX_BLOCK_SEC)
            self._blocked[(user_id, cls)] = self.mono() + retry
        message = ""
        if isinstance(data, dict):
            message = data.get("message") or data.get("code") or data.get("type") or ""
        raise Trading212Error(status, message, secret_values, retry)

    @staticmethod
    def _split(path, query):
        """``(path under /api/v0, query dict)``; a ``nextPagePath`` may carry both."""
        parts = urlsplit(str(path))
        if parts.scheme or parts.netloc:
            raise ValueError("Unsupported Trading 212 request")
        base = parts.path
        if base.startswith(API_PREFIX + "/"):
            base = base[len(API_PREFIX) :]
        merged = dict(parse_qsl(parts.query))
        merged.update({k: v for k, v in (query or {}).items() if v is not None})
        return base, merged

    async def private(self, user_id, method, path, query=None, body=None):
        """One authenticated call to an allowed endpoint; never retried."""
        key, secret = self.credentials(user_id)
        path, params = self._split(path, query)
        cls = request_class(method, path)
        await self._pace(user_id, cls)
        url = BASE_URL + path + ("?" + urlencode(params) if params else "")
        headers = {
            "Authorization": auth_header(key, secret),
            "Accept": "application/json",
            "User-Agent": "Condor-Trading212/1.0",
        }
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"))
            headers["Content-Type"] = "application/json"
        status, data, response_headers = await self._transport(
            method, url, headers, payload
        )
        secrets = (key, secret, headers["Authorization"])
        return self._result(user_id, cls, status, data, response_headers, secrets)

    async def _read(self, user_id, name, path, ttl, query=None, fresh=False):
        """A cached GET: reads are shared for ``ttl`` seconds unless ``fresh``."""
        cached = self._cache.get((user_id, name))
        if not fresh and cached and self.mono() - cached[0] < ttl:
            return cached[1]
        data = await self.private(user_id, "GET", path, query)
        self._cache[(user_id, name)] = (self.mono(), data)
        return data

    # ── account ──

    async def _summary(self, user_id, fresh=False):
        data = await self._read(
            user_id, "summary", "/equity/account/summary", 6.0, fresh=fresh
        )
        if not isinstance(data, dict):
            raise ValueError("Trading 212 returned an unexpected account summary")
        cash = data.get("cash") if isinstance(data.get("cash"), dict) else {}
        available = _decimal(cash.get("availableToTrade"))
        if available is None:
            raise ValueError("Trading 212 reported no available cash")
        reserved = _decimal(cash.get("reservedForOrders"), Decimal(0))
        in_pies = _decimal(cash.get("inPies"), Decimal(0))
        return {
            "currency": str(data.get("currency") or "").upper(),
            "available": available,
            "reserved": reserved,
            "in_pies": in_pies,
            "cash_total": available + reserved + in_pies,
        }

    @staticmethod
    def _require_eur(summary):
        if summary["currency"] != QUOTE:
            raise ValueError(
                f"This Trading 212 account is in {summary['currency'] or 'an unknown currency'}: "
                "Condor trades only EUR accounts, because orders execute in the "
                "account's own currency and every amount here is read in EUR"
            )

    async def _positions(self, user_id, fresh=False):
        """``{ticker: position}`` of the whole account, in EUR and the instrument's currency."""
        data = await self._read(
            user_id, "positions", "/equity/positions", 2.0, fresh=fresh
        )
        if not isinstance(data, list):
            raise ValueError("Trading 212 returned an unexpected positions answer")
        found = {}
        for row in data:
            instrument = row.get("instrument") if isinstance(row, dict) else None
            ticker = instrument.get("ticker") if isinstance(instrument, dict) else None
            quantity = _decimal(row.get("quantity")) if isinstance(row, dict) else None
            if not isinstance(ticker, str) or not quantity or quantity <= 0:
                continue
            impact = row.get("walletImpact")
            impact = impact if isinstance(impact, dict) else {}
            if impact.get("currency") not in (None, QUOTE):
                raise ValueError("Trading 212 reports positions outside EUR")
            in_pies = _decimal(row.get("quantityInPies"), Decimal(0))
            available = _decimal(row.get("quantityAvailableForTrading"))
            if available is None:
                available = quantity - in_pies
            found[ticker] = {
                "ticker": ticker,
                "quantity": quantity,
                "available": max(available, Decimal(0)),
                "in_pies": in_pies,
                "price_usd": _decimal(row.get("currentPrice")),
                "avg_price_usd": _decimal(row.get("averagePricePaid")),
                "value_eur": _decimal(impact.get("currentValue")),
                "cost_eur": _decimal(impact.get("totalCost")),
                "currency": (instrument or {}).get("currency"),
            }
        return found

    @staticmethod
    def _price_eur(position):
        """EUR per share at Trading 212's own current price, or None."""
        if position and position["value_eur"] is not None and position["quantity"]:
            return position["value_eur"] / position["quantity"]
        return None

    async def accounts(self, user_id):
        summary = await self._summary(user_id)
        self._require_eur(summary)
        positions = await self._positions(user_id)
        try:
            by_ticker = {
                i["ticker"]: i["symbol"] for i in await self._universe(user_id)
            }
        except ValueError:
            by_ticker = {}
        funded = []
        if summary["cash_total"] > 0:
            funded.append(
                {
                    "currency": QUOTE,
                    "balance": fmt(summary["cash_total"]),
                    "available": fmt(max(summary["available"], Decimal(0))),
                }
            )
        for ticker, row in sorted(positions.items()):
            funded.append(
                {
                    "currency": by_ticker.get(ticker) or ticker,
                    "ticker": ticker,
                    "balance": fmt(row["quantity"]),
                    "available": fmt(row["available"]),
                    "value_eur": (
                        fmt(row["value_eur"]) if row["value_eur"] is not None else None
                    ),
                }
            )
        return {"accounts": funded}

    async def eur_usd(self):
        """USD per euro from Kraken's public EURUSD pair, or None (cached a minute)."""
        stamp, cached = self._eur_usd
        if cached and time.monotonic() - stamp < 60:
            return cached
        try:
            from condor.kraken_xstocks import service as kraken

            rate = await kraken.eur_usd()
        except (
            Exception
        ):  # noqa: BLE001 - a missing rate only drops the dashboard value
            return None
        rate = _decimal(rate)
        if not rate or rate <= 0:
            return None
        self._eur_usd = (time.monotonic(), rate)
        return rate

    async def portfolio(self, user_id):
        """Whole Trading 212 account at Trading 212's own prices, for the dashboard (USD)."""
        summary = await self._summary(user_id)
        self._require_eur(summary)
        positions = await self._positions(user_id)
        rate = await self.eur_usd()
        if (summary["cash_total"] > 0 or positions) and not rate:
            # Cash is held in euros and converted here, so without the rate the whole
            # account would read as 0 USD, and that false drop would be recorded as
            # equity. Fail instead: the dashboard shows a note.
            raise ValueError("No EUR/USD rate is available to value Trading 212")
        try:
            by_ticker = {
                i["ticker"]: i["symbol"] for i in await self._universe(user_id)
            }
        except ValueError:
            by_ticker = {}
        balances, unpriced = [], []
        if summary["cash_total"] > 0:
            balances.append(
                {
                    "token": QUOTE,
                    "total": float(summary["cash_total"]),
                    "available": float(max(summary["available"], Decimal(0))),
                    "usd_value": float(summary["cash_total"] * rate),
                }
            )
        for ticker, row in sorted(positions.items()):
            token = by_ticker.get(ticker) or ticker.split("_")[0]
            if row["price_usd"] and row["currency"] == PRICE_CURRENCY:
                usd = row["quantity"] * row["price_usd"]
            elif row["value_eur"] is not None and rate:
                usd = row["value_eur"] * rate
            else:
                unpriced.append(token)
                usd = Decimal(0)
            balances.append(
                {
                    "token": token,
                    "total": float(row["quantity"]),
                    "available": float(row["available"]),
                    "usd_value": float(usd),
                }
            )
        note = (
            "Whole Trading 212 account at Trading 212's own current prices, euros "
            "converted at Kraken's public EURUSD rate. Only the bot allocation may "
            "be traded."
        )
        if unpriced:
            note += " No price for " + ", ".join(unpriced) + "."
        return {
            "connector": "Trading 212",
            "balances": balances,
            "total_usd": sum(row["usd_value"] for row in balances),
            "note": note,
        }

    async def sync_from_account(self, user_id):
        """Seed the bot allocation from the Trading 212 account (owner action).

        Cash is the available-to-trade cash. A position's cost is the total cost the
        API reports in EUR (``walletImpact.totalCost``); when it reports none, the
        current value stands in and the row is named in ``notes``.

        Positions the bot did not open are ADOPTED (the other venues do the same),
        except the tickers on the owner's exclusion list (``excluded_tickers``): those
        stay the owner's own holdings, outside the bot allocation, and the bot refuses
        to trade them.
        """
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            if ledger["uncertain_order"]:
                raise ValueError("Reconcile the uncertain order before syncing")
            if ledger["enabled"]:
                raise ValueError("Pause trading before syncing the allocation")
            summary = await self._summary(user_id, fresh=True)
            self._require_eur(summary)
            positions = await self._positions(user_id, fresh=True)
            universe = {i["ticker"]: i for i in await self._universe(user_id)}
            held, tickers, skipped, notes = {}, {}, [], []
            excluded = set(ledger.get("excluded_tickers") or [])
            for ticker, row in positions.items():
                item = universe.get(ticker)
                units = row["available"]
                if not item or units <= 0:
                    skipped.append(ticker)
                    continue
                if item["symbol"] in excluded:
                    skipped.append(ticker)
                    notes.append(
                        f"{item['symbol']}: on the exclusion list, kept out of the bot "
                        "allocation"
                    )
                    continue
                cost, basis = row["cost_eur"], "reported"
                if not cost or cost <= 0:
                    cost, basis = row["value_eur"], "current value"
                if not cost or cost <= 0:
                    skipped.append(ticker)
                    continue
                if basis != "reported":
                    notes.append(
                        f"{item['symbol']}: cost basis is the current value (none reported)"
                    )
                held[item["symbol"]] = {"units": fmt(units), "cost": fmt(cost)}
                tickers[item["symbol"]] = ticker
            fresh = new_ledger()
            fresh["cash"] = fmt(summary["available"])
            fresh["positions"] = held
            fresh["tickers"] = tickers
            fresh["synced"] = True
            fresh["account_currency"] = summary["currency"]
            # The fee a real fill showed is not forgotten by a re-sync.
            fresh["observed_fx_fee_pct"] = ledger.get("observed_fx_fee_pct")
            fresh["excluded_tickers"] = list(ledger.get("excluded_tickers") or [])
            _write(_root(user_id) / "ledger.json", fresh)
            # The ledger was just seeded from this very read of the account.
            check = self.account_check(user_id, fresh, summary, positions)
            if check["ledger_matches_account"]:
                self._verified[user_id] = self.clock()
            else:
                self._verified.pop(user_id, None)
                notes += [m["message"] for m in check["mismatch"]]
            return {**self.status(user_id), "skipped": skipped, "notes": notes}

    # ── ledger against the account ──

    def account_check(self, user_id, ledger, summary, positions):
        """Does the bot ledger agree with the real account? Pure: no request is made.

        The ledger's cash must equal the account's free cash and, for every ticker the
        ledger holds or the bot has traded, its units must equal the account's
        quantity of that ticker (shares in a pie excluded). The owner's other holdings
        are not errors: they are listed under ``outside_allocation``.
        """
        excluded = set(ledger.get("excluded_tickers") or [])
        mismatch, outside = [], []
        cash_ledger, cash_account = Decimal(ledger["cash"]), summary["available"]
        if abs(cash_ledger - cash_account) > CASH_TOLERANCE_EUR:
            mismatch.append(
                {
                    "kind": "cash",
                    "ledger": fmt(cash_ledger),
                    "account": fmt(cash_account),
                    "difference": fmt(cash_ledger - cash_account),
                    "message": (
                        f"cash: ledger {fmt(cash_ledger)} EUR, account "
                        f"{fmt(cash_account)} EUR"
                    ),
                }
            )
        cached = self._instruments.get(user_id, (0.0, None))[1]
        bot = {}  # Trading 212 ticker -> plain symbol, for every ticker the bot owns
        for symbol in ledger["positions"]:
            ticker = self._ledger_ticker(ledger, symbol, cached)
            if ticker:
                bot[ticker] = symbol
        for order in ledger.get("orders") or []:
            symbol = order.get("symbol")
            ticker = order.get("ticker") or self._ledger_ticker(ledger, symbol, cached)
            if ticker and symbol:
                bot.setdefault(ticker, symbol)
        for ticker, symbol in sorted(bot.items()):
            held = ledger["positions"].get(symbol)
            if symbol in excluded:
                if held:
                    mismatch.append(
                        {
                            "kind": "excluded_in_ledger",
                            "symbol": symbol,
                            "ticker": ticker,
                            "message": (
                                f"{symbol} is on the exclusion list but still in the "
                                "bot ledger"
                            ),
                        }
                    )
                continue
            units = Decimal(held["units"]) if held else Decimal(0)
            row = positions.get(ticker)
            account = Decimal(0)
            if row:
                account = max(row["quantity"] - row["in_pies"], Decimal(0))
            tolerance = max(Decimal("0.0001"), max(units, account) * Decimal("0.001"))
            if abs(units - account) > tolerance:
                mismatch.append(
                    {
                        "kind": "position",
                        "symbol": symbol,
                        "ticker": ticker,
                        "ledger_units": fmt(units),
                        "account_units": fmt(account),
                        "difference": fmt(units - account),
                        "message": (
                            f"{symbol}: ledger {fmt(units)} units, account "
                            f"{fmt(account)} units"
                        ),
                    }
                )
        for ticker, row in sorted(positions.items()):
            if ticker in bot:
                continue
            match = TICKER.fullmatch(ticker)
            symbol = match.group(1) if match else ticker
            outside.append(
                {
                    "ticker": ticker,
                    "symbol": symbol,
                    "units": fmt(row["quantity"]),
                    "excluded": symbol in excluded,
                }
            )
        result = {
            "checked_at": self.clock(),
            "ledger_matches_account": not mismatch,
            "mismatch": mismatch,
            "outside_allocation": outside,
        }
        self._checks[user_id] = result
        if mismatch:
            # An earlier match no longer vouches for the ledger.
            self._verified.pop(user_id, None)
        return result

    # ── instruments and the reference feed ──

    @staticmethod
    def parse_instruments(rows):
        """US stocks quoted in USD from the instrument metadata, one per plain symbol.

        A US stock is a ``STOCK`` priced in USD whose ISIN is a US one. The plain symbol
        is the short name; a symbol that more than one instrument claims is dropped
        unless exactly one of them is the plain ``_US_EQ`` listing.
        """
        found = defaultdict(list)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            match = TICKER.fullmatch(str(row.get("ticker") or ""))
            isin = str(row.get("isin") or "")
            if (
                not match
                or row.get("type") != "STOCK"
                or row.get("currencyCode") != PRICE_CURRENCY
                or not isin.startswith("US")
            ):
                continue
            symbol = match.group(1)
            short = row.get("shortName")
            if isinstance(short, str) and short and short.upper() != symbol:
                continue
            found[symbol].append(
                {
                    "symbol": symbol,
                    "ticker": match.group(0),
                    "exchange": match.group(2),
                    "name": str(row.get("name") or "")[:80],
                    "min_quantity": _decimal(row.get("minTradeQuantity")),
                    "max_open_quantity": _decimal(row.get("maxOpenQuantity")),
                    "schedule_id": row.get("workingScheduleId"),
                    "extended_hours": row.get("extendedHours") is True,
                }
            )
        chosen = []
        for symbol, items in sorted(found.items()):
            if len(items) == 1:
                chosen.append(items[0])
                continue
            us = [i for i in items if i["exchange"] == "US"]
            if len(us) == 1:
                chosen.append(us[0])
        return chosen

    async def _universe(self, user_id):
        """The tradable US stocks of this account, from the instrument metadata (cached)."""
        stamp, cached = self._instruments.get(user_id, (0.0, None))
        if cached is not None and self.mono() - stamp < INSTRUMENT_TTL_SEC:
            return cached
        data = await self.private(user_id, "GET", "/equity/metadata/instruments")
        items = self.parse_instruments(data)
        if not items:
            raise ValueError("Trading 212 listed no US stocks for this account")
        self._instruments[user_id] = (self.mono(), items)
        return items

    async def _instrument(self, user_id, symbol):
        for item in await self._universe(user_id):
            if item["symbol"] == symbol:
                return item
        raise ValueError(
            f"{symbol} is not a US stock this Trading 212 account can trade here"
        )

    async def _reference_pairs(self):
        return await self.market_feed.pairs()

    async def _tickers(self, pairs):
        """``{pair: raw Kraken ticker}`` for the named pairs, one request per 40."""
        names = list(dict.fromkeys(pairs))
        found = {}
        step = xstocks_market.TICKER_CHUNK
        for start in range(0, len(names), step):
            chunk = names[start : start + step]
            result = await self.market_feed._get(
                "Ticker", {"pair": ",".join(chunk)}, asset_class=True
            )
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

    async def stocks(self, user_id, keyword=""):
        """The most traded US stocks Trading 212 lists that the reference feed covers.

        Not connected, the list is the reference feed's own and says so: it cannot be
        filtered by what Trading 212 lists. Connected, an unreadable instrument list is
        an error, never an unfiltered list.
        """
        if not isinstance(keyword, str) or len(keyword) > 40:
            raise ValueError("Enter a stock symbol")
        key = keyword.strip().upper()
        pairs = await self._reference_pairs()
        listed = {}
        unfiltered = not self.connected(user_id)
        if not unfiltered:
            listed = {i["symbol"]: i for i in await self._universe(user_id)}
        wanted = {
            s: p
            for s, p in pairs.items()
            if (unfiltered or s in listed) and (not key or key in s)
        }
        tickers = await self._tickers(wanted.values())
        rows = []
        for symbol, pair in wanted.items():
            ticker = tickers.get(pair)
            row = self.market_feed._row(symbol, pair, ticker) if ticker else None
            if not row:
                continue
            item = listed.get(symbol)
            row["ticker"] = item["ticker"] if item else None
            row["min_trade_quantity"] = (
                fmt(item["min_quantity"]) if item and item["min_quantity"] else None
            )
            rows.append(row)
        rows.sort(key=lambda r: r["volume_24h_usd"], reverse=True)
        if key:
            shown = rows[:10]
        else:
            shown = rows[:TOP_STOCKS]
            held = set(_read_ledger(user_id)["positions"])
            shown_symbols = {r["symbol"] for r in shown}
            shown += [r for r in rows if r["symbol"] in held - shown_symbols]
        self._record_prices(user_id, shown)
        answer = {
            "quote": QUOTE,
            "price_currency": PRICE_CURRENCY,
            "reference_note": REFERENCE_NOTE,
            "market": self._market(),
            "stocks": shown,
        }
        if unfiltered:
            answer["unfiltered"] = True
            answer["universe_note"] = (
                "Not connected: Trading 212's instrument list cannot be read, so these "
                "are the reference feed's stocks, not confirmed as tradable"
            )
        return answer

    async def candles(self, user_id, symbol, timeframe="15m", count=96):
        answer = await self.market_feed.candles(symbol, timeframe, count)
        return {**answer, "price_currency": PRICE_CURRENCY, "reference": True}

    # ── valuation ──

    def _ledger_ticker(self, ledger, symbol, universe=None):
        ticker = (ledger.get("tickers") or {}).get(symbol)
        if ticker:
            return ticker
        for item in universe or []:
            if item["symbol"] == symbol:
                return item["ticker"]
        return None

    async def equity(self, user_id, ledger, positions=None):
        """Cash plus the bot's positions at Trading 212's own current prices (EUR)."""
        total = Decimal(ledger["cash"])
        if not ledger["positions"]:
            return total
        positions = (
            positions if positions is not None else await self._positions(user_id)
        )
        universe = None
        for symbol, position in ledger["positions"].items():
            ticker = self._ledger_ticker(ledger, symbol, universe)
            if ticker is None and universe is None:
                universe = await self._universe(user_id)
                ticker = self._ledger_ticker(ledger, symbol, universe)
            price = self._price_eur(positions.get(ticker)) if ticker else None
            if not price:
                raise ValueError(
                    f"No Trading 212 price for {symbol}: it is in the bot ledger but "
                    "not in the account; sync the allocation again"
                )
            total += price * Decimal(position["units"])
        return total

    async def valuation(self, user_id):
        ledger = _read_ledger(user_id)
        if ledger["uncertain_order"]:
            raise ValueError(
                "Reconcile the uncertain order before valuing the bot allocation"
            )
        summary = await self._summary(user_id)
        self._require_eur(summary)
        positions = await self._positions(user_id)
        equity = await self.equity(user_id, ledger, positions)
        if equity <= FLOOR and ledger["enabled"]:
            self.set_enabled(user_id, False)
        check = self.account_check(user_id, ledger, summary, positions)
        rows = []
        for symbol, position in ledger["positions"].items():
            ticker = (ledger.get("tickers") or {}).get(symbol)
            live = positions.get(ticker) if ticker else None
            price = self._price_eur(live)
            units = Decimal(position["units"])
            rows.append(
                {
                    "symbol": symbol,
                    "ticker": ticker,
                    "units": position["units"],
                    "cost_eur": position["cost"],
                    "price_eur": fmt(price) if price else None,
                    "value_eur": fmt(price * units) if price else None,
                    "price_usd": (
                        fmt(live["price_usd"]) if live and live["price_usd"] else None
                    ),
                }
            )
        return {
            "equity": fmt(equity),
            "quote": QUOTE,
            "price_source": "Trading 212 positions endpoint (its own current price)",
            "positions": rows,
            "new_position_limit": fmt(
                equity / 6 if guards_active(equity) else equity * MAX_ASSET_FRACTION
            ),
            "minimum_cash_reserve": fmt(
                equity / 2 if guards_active(equity) else Decimal(0)
            ),
            "ledger_matches_account": check["ledger_matches_account"],
            "mismatch": check["mismatch"],
            "outside_allocation": check["outside_allocation"],
            **self.status(user_id),
        }

    # ── hours ──

    async def _schedule_state(self, user_id, schedule_id):
        """``open``, ``closed`` or ``unknown`` from the exchange's own working schedule.

        A cross-check of the US session clock (it knows holidays). Anything that cannot
        be read or interpreted is ``unknown``, never a pass.
        """
        if schedule_id is None:
            return "unknown"
        stamp, cached = self._exchanges.get(user_id, (0.0, None))
        if cached is None or self.mono() - stamp >= EXCHANGE_TTL_SEC:
            try:
                cached = await self.private(
                    user_id, "GET", "/equity/metadata/exchanges"
                )
            except ValueError:
                return "unknown"
            self._exchanges[user_id] = (self.mono(), cached)
        now = self.clock()
        for exchange in cached if isinstance(cached, list) else []:
            for schedule in (exchange or {}).get("workingSchedules") or []:
                if not isinstance(schedule, dict) or schedule.get("id") != schedule_id:
                    continue
                events = []
                for event in schedule.get("timeEvents") or []:
                    when = _parse_time((event or {}).get("date"))
                    if when is not None and when <= now:
                        events.append((when, str(event.get("type"))))
                if not events:
                    return "unknown"
                when, kind = max(events)
                if now - when > 4 * 86400:
                    return "unknown"
                return "open" if kind in ("OPEN", "BREAK_END") else "closed"
        return "unknown"

    # ── orders ──

    async def _plan(
        self, user_id, side, symbol, value, slippage_pct, order_type, positions=None
    ):
        """Size an order from the reference price and the instrument's rules."""
        side = side.lower() if isinstance(side, str) else ""
        if side not in SIDES:
            raise ValueError("Use side buy or sell")
        order_type = order_type.lower() if isinstance(order_type, str) else ""
        if order_type not in ORDER_TYPES:
            raise ValueError("Use order_type limit or market")
        symbol = symbol_of(symbol)
        if symbol in (_read_ledger(user_id).get("excluded_tickers") or []):
            raise ValueError(
                f"{symbol} is on the owner's exclusion list (a holding outside the "
                "bot allocation): the bot never trades it; no order placed"
            )
        instrument = await self._instrument(user_id, symbol)
        pair = (await self._reference_pairs()).get(symbol)
        if not pair:
            raise ValueError(f"{symbol} has no reference price feed (Kraken xStocks)")
        slip = amount(str(slippage_pct if slippage_pct else DEFAULT_SLIPPAGE_PCT))
        if slip > MAX_SLIPPAGE_PCT:
            raise ValueError(f"Slippage above {MAX_SLIPPAGE_PCT}% is not allowed")
        ticker = (await self._tickers([pair])).get(pair) or {}
        bid, ask, _ = self._touch(ticker)
        if not bid or not ask or bid <= 0 or ask <= 0 or bid > ask:
            raise ValueError(f"No live reference price for {symbol}")
        spread = (ask - bid) / ask * 100
        if spread > MAX_SPREAD_PCT:
            raise ValueError(
                f"{symbol} reference spread is {spread:.2f}%, above {MAX_SPREAD_PCT}%"
            )
        rate = await self.eur_usd()
        if not rate or not Decimal("0.5") < rate < Decimal("2.5"):
            raise ValueError("No usable EUR/USD rate to size the order")
        live = (positions or {}).get(instrument["ticker"])
        reference_source = "Kraken xStocks public data"
        sell_price = bid
        if side == "sell" and live and live["price_usd"]:
            # Trading 212's own current price of a stock the account holds beats the
            # reference; the price for a sale limit is the lower of the two.
            sell_price = min(bid, live["price_usd"])
            reference_source = (
                "Trading 212 position price and Kraken xStocks public data"
            )
        factor = slip / 100
        fee = PUBLISHED_FX_FEE_PCT / 100
        if side == "buy":
            limit = _round_up(ask * (1 + factor), PRICE_STEP)
            reference = ask
            unit_eur = limit / rate * (1 + fee) * (1 + FX_BUFFER)
            quantity = _round_down(amount(value) / unit_eur, QTY_STEP)
        else:
            limit = max(_round_down(sell_price * (1 - factor), PRICE_STEP), PRICE_STEP)
            reference = sell_price
            wanted = amount(value)
            available = live["available"] if live else None
            if available is not None and wanted > available:
                raise ValueError("Insufficient available Trading 212 shares")
            if available is not None and wanted == available:
                quantity = available  # a full close sells exactly what is held
            else:
                quantity = _round_down(wanted, QTY_STEP)
        if quantity <= 0 or limit <= 0:
            raise ValueError("Amount is below the minimum order size for this stock")
        if side == "buy" and ask < MIN_PRICE_USD:
            raise ValueError(f"{symbol} trades below {MIN_PRICE_USD} USD: not traded")
        minimum = instrument["min_quantity"]
        if minimum and quantity < minimum and not (side == "sell" and live):
            raise ValueError(
                f"Quantity {fmt(quantity)} is below Trading 212's minimum "
                f"{fmt(minimum)} for {instrument['ticker']}"
            )
        notional_eur = quantity * limit / rate
        if side == "buy" and notional_eur < MIN_ORDER_EUR:
            raise ValueError(
                f"Order value about {fmt(notional_eur.quantize(Decimal('0.01')))} EUR is "
                f"below the {MIN_ORDER_EUR} EUR minimum"
            )
        held = live["quantity"] if live else Decimal(0)
        maximum = instrument["max_open_quantity"]
        if side == "buy" and maximum and quantity + held > maximum:
            raise ValueError(
                f"Quantity is above Trading 212's maximum for {instrument['ticker']}"
            )
        return {
            "side": side,
            "order_type": order_type,
            "symbol": symbol,
            "ticker": instrument["ticker"],
            "instrument": instrument,
            "quantity": quantity,
            "limit_price": limit,
            "reference_price": reference,
            "reference_source": reference_source,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread,
            "slippage_pct": slip,
            "fx": rate,
            "notional_eur": notional_eur,
        }

    @staticmethod
    def _estimate(ledger, plan):
        """The labelled cost of a round trip at this size, and what the guard says."""
        fee = fx_fee_rate(ledger)
        notional = plan["notional_eur"]
        allowance_eur = 2 * ROUNDING_ALLOWANCE_EUR_PER_LEG
        components = {
            "fx_fee": 2 * fee["fx_fee_pct"],
            "spread": max(plan["spread_pct"], SPREAD_FLOOR_PCT),
            "slippage": 2 * SLIPPAGE_ALLOWANCE_PCT_PER_LEG,
            "regulatory": REGULATORY_ALLOWANCE_PCT,
            "rounding_allowance": allowance_eur / notional * 100,
        }
        others = sum(v for k, v in components.items() if k != "rounding_allowance")
        round_trip = others + components["rounding_allowance"]
        sources = {
            "fx_fee": fee["source"],
            "spread": "estimate (Kraken reference spread, floored)",
            "slippage": "assumption",
            "regulatory": "estimate",
            "rounding_allowance": "assumption (carried over from Revolut X)",
        }
        blocked = plan["side"] == "buy" and round_trip > MAX_BUY_ROUND_TRIP_PCT
        return {
            "components": components,
            "sources": sources,
            "round_trip": round_trip,
            "others": others,
            "notional": notional,
            "allowance_eur": allowance_eur,
            "allowance_pct": components["rounding_allowance"],
            "blocked": blocked,
            "fee": fee,
        }

    def _preview_of(self, ledger, plan, estimate, schedule):
        market = self._market()
        components = estimate["components"]
        return {
            "ticker": plan["ticker"],
            "symbol": plan["symbol"],
            "side": plan["side"],
            "order_type": plan["order_type"],
            "quantity": fmt(plan["quantity"]),
            "limit_price": fmt(plan["limit_price"]),
            "price_currency": PRICE_CURRENCY,
            "notional_eur": fmt(plan["notional_eur"].quantize(Decimal("0.01"))),
            "reference_price": fmt(plan["reference_price"]),
            "reference_source": plan["reference_source"],
            "fx_eur_usd": fmt(plan["fx"]),
            "bid": fmt(plan["bid"]),
            "ask": fmt(plan["ask"]),
            "spread_pct": str(round(plan["spread_pct"], 3)),
            "fx_fee_pct_each_way": str(estimate["fee"]["fx_fee_pct"]),
            "fee_source": estimate["fee"]["source"],
            "fee_note": estimate["fee"]["note"],
            "estimated_round_trip_pct": str(round(estimate["round_trip"], 3)),
            "round_trip_components_pct": {
                k: str(round(v, 3)) for k, v in components.items()
            },
            "round_trip_sources": estimate["sources"],
            "cost_label": (
                "ESTIMATE: the FX fee is Trading 212's published figure (zero "
                "commission); the spread, slippage, regulatory and rounding parts are "
                "estimates or assumptions; the real cost is known only from a fill"
            ),
            "rounding_allowance_eur": fmt(estimate["allowance_eur"]),
            "cost_guard_max_round_trip_pct": str(MAX_BUY_ROUND_TRIP_PCT),
            "cost_guard_blocks_buy": estimate["blocked"],
            "min_order_eur": str(MIN_ORDER_EUR),
            "schedule_check": schedule,
            "market_open": market["open"],
            "minutes_to_close": market["minutes_to_close"],
        }

    async def preview(
        self, user_id, side, symbol, value, slippage_pct=None, order_type="limit"
    ):
        """Evidence of size and cost from the reference feed; it places nothing."""
        ledger = _read_ledger(user_id)
        try:
            positions = await self._positions(user_id)
        except ValueError:
            positions = None
        plan = await self._plan(
            user_id, side, symbol, value, slippage_pct, order_type, positions
        )
        schedule = await self._schedule_state(
            user_id, plan["instrument"]["schedule_id"]
        )
        return self._preview_of(ledger, plan, self._estimate(ledger, plan), schedule)

    @staticmethod
    def _check_cost(estimate):
        """Refuse a buy that fees and rounding would eat. Nothing has been sent."""
        if not estimate["blocked"]:
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
            f"{estimate['round_trip']:.2f}% (cent-rounding allowance "
            f"{estimate['allowance_pct']:.2f}% of a {estimate['notional']:.2f} EUR "
            f"order), above the {MAX_BUY_ROUND_TRIP_PCT}% limit; {advice}. "
            "Selling a position is never blocked by this."
        )

    async def order(
        self, user_id, side, symbol, value, slippage_pct=None, order_type="limit"
    ):
        """Place one order and settle it: wait, cancel any remainder, read the fill back."""
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            if not ledger["enabled"] or ledger["uncertain_order"]:
                raise ValueError("Trading is paused")
            market = self._market()
            if not market["open"]:
                raise ValueError(
                    "Trading 212 orders are refused outside the regular US session "
                    f"(Mon-Fri 09:30-16:00 New York); it is {market['new_york_time']} "
                    "in New York"
                )
            summary = await self._summary(user_id)
            self._require_eur(summary)
            positions = await self._positions(user_id)
            plan = await self._plan(
                user_id, side, symbol, value, slippage_pct, order_type, positions
            )
            symbol, ticker = plan["symbol"], plan["ticker"]
            schedule = await self._schedule_state(
                user_id, plan["instrument"]["schedule_id"]
            )
            if schedule == "closed":
                raise ValueError(
                    "Trading 212's own working schedule says this exchange is closed "
                    "(a holiday or a break); no order placed"
                )
            estimate = self._estimate(ledger, plan)
            if plan["side"] == "buy":
                equity = await self.equity(user_id, ledger, positions)
                if equity <= FLOOR:
                    self.set_enabled(user_id, False)
                    raise ValueError(
                        f"Bot equity reached the {FLOOR} stop threshold; trading paused"
                    )
                spend = plan["notional_eur"] * (1 + PUBLISHED_FX_FEE_PCT / 100)
                if spend * (1 + FEE_HEADROOM) > Decimal(ledger["cash"]):
                    raise ValueError(
                        "Not enough bot cash for this purchase and its fee"
                    )
                if spend * (1 + FEE_HEADROOM) > summary["available"]:
                    raise ValueError("Insufficient available Trading 212 cash")
                check_purchase(ledger, symbol, spend, equity)
                self._check_cost(estimate)
            else:
                position = ledger["positions"].get(symbol)
                if not position or plan["quantity"] > Decimal(position["units"]):
                    raise ValueError("Cannot sell assets outside the bot allocation")
                live = positions.get(ticker)
                if not live or live["available"] < plan["quantity"]:
                    raise ValueError("Insufficient available Trading 212 shares")
            recent = [o for o in ledger["orders"] if time.time() - o["time"] < 60]
            if len(recent) >= ORDERS_PER_MINUTE:
                raise ValueError("Maximum ten orders per minute")
            if not _read_ledger(user_id)["enabled"]:
                raise ValueError("Trading paused during checks")
            # Anything that can fail without sending must fail BEFORE the uncertain
            # entry is written, or it would read as an order that may exist.
            self.credentials(user_id)
            # The account is the ground truth (cash and positions update at once; the
            # history may not list an API order promptly): snapshot it now so that
            # the outcome can be read from what changed.
            try:
                before = await self._snapshot(user_id, ticker)
            except ValueError as error:
                raise ValueError(
                    "Refused, no order placed: the Trading 212 account could not be "
                    f"read to snapshot it before the order ({error})"
                ) from None
            held_before = before["quantity"]
            signed = plan["quantity"] if plan["side"] == "buy" else -plan["quantity"]
            if plan["order_type"] == "limit":
                path = "/equity/orders/limit"
                body = {
                    "ticker": ticker,
                    "quantity": _number(signed),
                    "limitPrice": _number(plan["limit_price"]),
                    "timeValidity": "DAY",
                }
            else:
                path = "/equity/orders/market"
                body = {
                    "ticker": ticker,
                    "quantity": _number(signed),
                    "extendedHours": False,
                }
            # Written BEFORE the order is sent: a crash or timeout leaves the order
            # marked uncertain and trading paused, never silently forgotten. There is
            # no client order id, so reconcile matches on ticker, side, size and time.
            ledger["uncertain_order"] = {
                "side": plan["side"],
                "symbol": symbol,
                "ticker": ticker,
                "quantity": fmt(plan["quantity"]),
                "limit_price": fmt(plan["limit_price"]),
                "order_type": plan["order_type"],
                "expected_eur": fmt(plan["notional_eur"]),
                "evidence_before": self._pack(before),
                "time": time.time(),
            }
            _write(_root(user_id) / "ledger.json", ledger)
            try:
                placed = await self.private(user_id, "POST", path, None, body)
            except Trading212Error as error:
                if not error.definite:
                    raise ValueError(_UNCERTAIN) from None
                # Trading 212 answered and refused: no order exists, so there is
                # nothing to reconcile and trading stays as it was.
                ledger["uncertain_order"] = None
                _write(_root(user_id) / "ledger.json", ledger)
                raise ValueError(f"Trading 212 rejected the order: {error}") from None
            except Exception:
                raise ValueError(_UNCERTAIN) from None
            # From here an order may exist: nothing below clears the uncertain entry
            # except a read-back that shows the final state.
            order_id = placed.get("id") if isinstance(placed, dict) else None
            if isinstance(order_id, bool) or not isinstance(order_id, int):
                raise ValueError(_UNCERTAIN) from None
            ledger["uncertain_order"]["order_id"] = order_id
            _write(_root(user_id) / "ledger.json", ledger)
            try:
                try:
                    detail, cancelled = await self._settle(
                        user_id, ticker, order_id, held_before
                    )
                    after = None
                    try:
                        after = (await self._summary(user_id, fresh=True))["cash_total"]
                    except ValueError:
                        pass
                    valued = self._valued(
                        plan["side"],
                        detail,
                        plan["fx"],
                        summary["cash_total"],
                        after,
                        fx_fee_rate(ledger),
                    )
                except _NotInHistory as gone:
                    # The history did not list the order (it may lag or omit API
                    # orders): the account's cash and position say what happened.
                    cancelled = gone.cancelled
                    pending = ledger["uncertain_order"]
                    verdict = await self._poll_verdict(
                        user_id, ledger, pending, known=True, settle=True
                    )
                    detail, valued = await self._book_evidence(
                        ledger, pending, verdict, plan["fx"]
                    )
                self._guard_market_fill(plan, detail, valued)
            except _Inconclusive as error:
                raise ValueError(f"{_UNCERTAIN} Account evidence: {error}") from None
            except Exception:
                raise ValueError(_UNCERTAIN) from None
            self._apply_fill(ledger, plan["side"], symbol, ticker, detail, valued)
            ledger["enabled"] = (
                self.connected(user_id) and _read_ledger(user_id)["enabled"]
            )
            _write(_root(user_id) / "ledger.json", ledger)
            return {
                "order": self._summarise(detail, valued, plan, cancelled),
                "status": self.status(user_id),
            }

    # ── account evidence ──

    async def _snapshot(self, user_id, ticker):
        """Free cash (EUR) and the quantity of one ticker, read fresh from the account."""
        summary = await self._summary(user_id, fresh=True)
        self._require_eur(summary)
        positions = await self._positions(user_id, fresh=True)
        row = positions.get(ticker)
        return {
            "cash": summary["available"],
            "reserved": summary["reserved"],
            "quantity": row["quantity"] if row else Decimal(0),
            "time": time.time(),
        }

    @staticmethod
    def _pack(snapshot):
        return {
            "cash": fmt(snapshot["cash"]),
            "reserved": fmt(snapshot["reserved"]),
            "quantity": fmt(snapshot["quantity"]),
            "time": snapshot["time"],
        }

    @staticmethod
    def _baseline(pending, ledger):
        """What the account looked like before the order: the stored snapshot, else the ledger."""
        packed = pending.get("evidence_before")
        if isinstance(packed, dict):
            cash = _decimal(packed.get("cash"))
            quantity = _decimal(packed.get("quantity"))
            if cash is not None and quantity is not None:
                return {
                    "cash": cash,
                    "reserved": _decimal(packed.get("reserved")),
                    "quantity": quantity,
                    "source": "snapshot taken before the order",
                }
        held = (ledger.get("positions") or {}).get(pending.get("symbol"))
        return {
            "cash": Decimal(ledger["cash"]),
            "reserved": None,
            "quantity": Decimal(held["units"]) if held else Decimal(0),
            "source": "the bot ledger (no snapshot was stored with this order)",
        }

    async def _expected_eur(self, pending):
        """The EUR value of the whole order as planned, or None."""
        value = _decimal(pending.get("expected_eur"))
        if value and value > 0:
            return value
        limit = _decimal(pending.get("limit_price"))
        rate = await self.eur_usd()
        if limit and rate:
            return Decimal(pending["quantity"]) * limit / rate
        return None

    @staticmethod
    def _judge(pending, before, after, quantity, expected_eur, known):
        """What the account alone says happened to one order. Never guesses.

        ``filled``: the ticker's quantity moved by the order's quantity in the right
        direction AND free cash moved the right way by about the expected EUR.
        ``partial``: the same, for less than the quantity (only with a known order
        id: a smaller move cannot otherwise be told from a manual trade).
        ``unchanged``: neither cash nor the position moved. Anything else (only cash
        moved, only the position moved, the wrong direction, a different quantity, a
        different amount of cash, reserved cash changed) is ``ambiguous``.
        """
        sign = 1 if pending["side"] == "buy" else -1
        dq = (after["quantity"] - before["quantity"]) * sign
        dc = (before["cash"] - after["cash"]) * sign
        ticker = pending["ticker"]
        text = (
            f"free cash {fmt(before['cash'])} -> {fmt(after['cash'])} EUR, "
            f"{ticker} quantity {fmt(before['quantity'])} -> {fmt(after['quantity'])} "
            f"(baseline: {before['source']})"
        )
        verdict = {
            "verdict": "ambiguous",
            "quantity": dq,
            "cash": dc,
            "text": text,
            "before": before,
            "after": after,
        }
        reserved = before.get("reserved")
        if reserved is not None and after["reserved"] != reserved:
            verdict["text"] = (
                "cash reserved for orders changed (a resting order?): " + text
            )
            return verdict
        qty_moved = abs(dq) > QTY_EPSILON
        cash_moved = abs(dc) > CASH_TOLERANCE_EUR
        if not qty_moved and not cash_moved:
            verdict["verdict"] = "unchanged"
            return verdict
        if not (qty_moved and cash_moved):
            what = "only the cash moved" if cash_moved else "only the position moved"
            verdict["text"] = f"{what}: {text}"
            return verdict
        if dq <= 0 or dc <= 0:
            verdict["text"] = f"moved the wrong way: {text}"
            return verdict
        tolerance = max(quantity * FILLS_QUANTITY_TOLERANCE, QTY_EPSILON)
        if dq > quantity + tolerance:
            verdict["text"] = (
                "the position moved by more than this order (a concurrent manual "
                f"trade of the same ticker?): {text}"
            )
            return verdict
        if expected_eur is not None:
            part = expected_eur * min(dq, quantity) / quantity
            if abs(dc - part) > max(part * CASH_AGREEMENT, CASH_TOLERANCE_EUR):
                verdict["text"] = (
                    f"the cash change does not match the order (about "
                    f"{fmt(part.quantize(Decimal('0.01')))} EUR expected): {text}"
                )
                return verdict
        if abs(dq - quantity) <= tolerance:
            verdict["verdict"] = "filled"
        elif known:
            verdict["verdict"] = "partial"
        else:
            verdict["text"] = (
                f"a part of the order may have filled, but without its order id that "
                f"cannot be told from a manual trade: {text}"
            )
        return verdict

    async def _poll_verdict(
        self, user_id, ledger, pending, *, known, settle, quantity=None
    ):
        """Read the account (a few times when ``settle``) and judge the order by it.

        ``settle``: the order was only just placed, so cash and position may take a
        moment to show it: poll. Otherwise (a reconcile, long after) one answer that
        is neither a fill nor a partial one is final. A partial fill must read the
        same twice. An unreadable account is an error, never "unchanged".
        """
        before = self._baseline(pending, ledger)
        order_quantity = Decimal(pending["quantity"])
        quantity = order_quantity if quantity is None else quantity
        expected = await self._expected_eur(pending)
        if expected is not None:
            expected = expected * quantity / order_quantity
        verdict, last_partial, stable, failure = None, None, False, None
        for attempt in range(EVIDENCE_POLLS):
            try:
                after = await self._snapshot(user_id, pending["ticker"])
            except ValueError as error:
                failure, after = error, None
            if after is not None:
                failure = None
                verdict = self._judge(pending, before, after, quantity, expected, known)
                kind = verdict["verdict"]
                if kind == "filled":
                    break
                if kind == "partial":
                    if last_partial is not None and (
                        abs(last_partial - verdict["quantity"]) <= QTY_EPSILON
                    ):
                        stable = True
                        break
                    last_partial = verdict["quantity"]
                else:
                    last_partial = None
                    if not settle:
                        break
            if attempt + 1 < EVIDENCE_POLLS:
                await self.sleep(EVIDENCE_WAIT_SEC)
        if verdict is None:
            raise ValueError(
                "The Trading 212 account could not be read to verify the order "
                f"({failure}); trading stays paused, reconcile again shortly"
            )
        if verdict["verdict"] == "partial" and not stable:
            verdict["verdict"] = "ambiguous"
            verdict["text"] = "the partial move was still changing: " + verdict["text"]
        return verdict

    async def _book_evidence(self, ledger, pending, verdict, fx):
        """``(detail, valued)`` of a fill that only the account shows, every figure labelled."""
        if verdict["verdict"] not in ("filled", "partial"):
            raise _Inconclusive(verdict["text"])
        side = pending["side"]
        quantity, total = verdict["quantity"], verdict["cash"]
        held = (ledger.get("positions") or {}).get(pending["symbol"])
        if side == "sell" and held:
            units = Decimal(held["units"])
            if units < quantity <= units + max(units * Decimal("0.001"), QTY_EPSILON):
                quantity = units
        rate = fx_fee_rate(ledger)
        fraction = rate["fx_fee_pct"] / 100
        if side == "buy":
            fee = total * fraction / (1 + fraction)
            value = total - fee
        else:
            value = total / (1 - fraction)
            fee = value * fraction
        average = value * fx / quantity if fx and quantity > 0 else None
        detail = {
            "order_id": pending.get("order_id"),
            "ticker": pending["ticker"],
            "status": (
                "filled" if verdict["verdict"] == "filled" else "partially_filled"
            ),
            "inferred": (
                "the history did not list this order; booked from the account: "
                + verdict["text"]
            ),
        }
        valued = {
            "quantity": quantity,
            "value": value,
            "fee": fee,
            "value_source": "account_cash_change",
            "fee_source": "estimate",
            "fee_note": (
                f"FX fee estimated at the {rate['source']} {rate['fx_fee_pct']}% "
                "rate; the account evidence carries no tax figures"
            ),
            "fx_fee": None,
            "average_price": average,
            "average_price_source": (
                "estimate: EUR value / quantity converted at the Kraken reference "
                "EUR/USD"
            ),
            "fill_source": "account_evidence",
        }
        return detail, valued

    # ── reading orders back ──

    @staticmethod
    def _normalize_pending(info):
        return {
            "order_id": info.get("id"),
            "ticker": info.get("ticker"),
            "side": str(info.get("side") or "").lower(),
            "status": str(info.get("status") or "").lower(),
            "type": str(info.get("type") or "").lower(),
            "quantity": _decimal(info.get("quantity")),
            "filled_quantity": _decimal(info.get("filledQuantity"), Decimal(0)),
            "limit_price": info.get("limitPrice"),
            "initiated_from": info.get("initiatedFrom"),
            "created": _parse_time(info.get("createdAt")),
        }

    async def _pending(self, user_id, order_id):
        """The order while it is still pending, or None when Trading 212 no longer lists it."""
        try:
            data = await self.private(user_id, "GET", f"/equity/orders/{order_id}")
        except Trading212Error as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(data, dict) or not data.get("id"):
            # Not "no longer pending": an answer that cannot be read is no answer.
            raise ValueError("Trading 212 returned an unexpected order answer")
        return self._normalize_pending(data)

    async def _cancel(self, user_id, order_id):
        try:
            await self.private(user_id, "DELETE", f"/equity/orders/{order_id}")
        except Trading212Error as error:
            if error.status == 404:
                return  # already gone
            raise

    async def _await_gone(self, user_id, order_id, polls):
        """Poll until the order is no longer pending; True when it is gone."""
        for attempt in range(polls):
            try:
                info = await self._pending(user_id, order_id)
                if info is None or info["status"] in FINAL_STATUSES:
                    return True
            except ValueError:
                # A refused or failed read (rate limit, transport) is not an answer
                # about the order; keep polling and fall through to "still pending".
                pass
            if attempt + 1 < polls:
                await self.sleep(POLL_WAIT_SEC)
        return False

    @staticmethod
    def group_history(items, strict=True):
        """``{order id: detail}`` from historical order items (one per fill, or one per order).

        A strict read refuses fill records that disagree with their order; a lenient
        one (the read-only history panel) leaves that order out instead.
        """
        groups = {}
        for item in items if isinstance(items, list) else []:
            order = item.get("order") if isinstance(item, dict) else None
            if not isinstance(order, dict) or not order.get("id"):
                continue
            detail = groups.setdefault(
                order["id"],
                {
                    "order_id": order["id"],
                    "ticker": order.get("ticker")
                    or (order.get("instrument") or {}).get("ticker"),
                    "side": str(order.get("side") or "").lower(),
                    "status": str(order.get("status") or "").lower(),
                    "type": str(order.get("type") or "").lower(),
                    "quantity": abs(_decimal(order.get("quantity"), Decimal(0))),
                    "order_filled_quantity": _decimal(order.get("filledQuantity")),
                    "limit_price": order.get("limitPrice"),
                    "initiated_from": order.get("initiatedFrom"),
                    "created": _parse_time(order.get("createdAt")),
                    "fills": [],
                },
            )
            fill = item.get("fill")
            if isinstance(fill, dict) and fill.get("type") in (None, "TRADE"):
                detail["fills"].append(fill)
        finished = {}
        for key, detail in groups.items():
            try:
                finished[key] = Trading212Service._finish(detail)
            except ValueError:
                if strict:
                    raise
        return finished

    @staticmethod
    def _finish(detail):
        base, value, net, fee, fx_fee = Decimal(0), Decimal(0), Decimal(0), None, None
        updated = detail["created"]
        for fill in detail["fills"]:
            quantity = abs(_decimal(fill.get("quantity"), Decimal(0)))
            price = _decimal(fill.get("price"))
            if quantity <= 0 or not price:
                continue
            base += quantity
            value += quantity * price
            moment = _parse_time(fill.get("filledAt"))
            if moment and (updated is None or moment > updated):
                updated = moment
            impact = fill.get("walletImpact")
            if isinstance(impact, dict):
                if impact.get("currency") in (None, QUOTE):
                    figure = _decimal(impact.get("netValue"))
                    if figure is not None:
                        net += abs(figure)
                for tax in impact.get("taxes") or []:
                    charge = _decimal((tax or {}).get("quantity"))
                    if charge is None or (tax or {}).get("currency") not in (
                        None,
                        QUOTE,
                    ):
                        continue
                    fee = (fee or Decimal(0)) + abs(charge)
                    if (tax or {}).get("name") == "CURRENCY_CONVERSION_FEE":
                        fx_fee = (fx_fee or Decimal(0)) + abs(charge)
        reported = detail.pop("order_filled_quantity")
        if base > 0 and reported is not None and reported > 0:
            if abs(base - reported) > reported * FILLS_QUANTITY_TOLERANCE:
                raise ValueError("Trading 212 fill records disagree with the order")
        detail["filled_quantity"] = base if base > 0 else (reported or Decimal(0))
        detail["average_price"] = value / base if base > 0 else None
        detail["fills_value_usd"] = value
        detail["net_value_eur"] = net if net > 0 else None
        detail["fee_eur"] = fee
        detail["fx_fee_eur"] = fx_fee
        detail["updated"] = updated
        detail.pop("fills", None)
        return detail

    async def _history_pages(self, user_id, ticker=None, pages=MAX_PAGES):
        """Historical order items, newest first as far as Trading 212 orders them."""
        items, path, query = [], "/equity/history/orders", {"limit": 50}
        if ticker:
            query["ticker"] = ticker
        for _ in range(pages):
            data = await self.private(user_id, "GET", path, query)
            rows = data.get("items") if isinstance(data, dict) else None
            items += [r for r in rows or [] if isinstance(r, dict)]
            more = data.get("nextPagePath") if isinstance(data, dict) else None
            if not more or not rows:
                break
            if not str(more).startswith(API_PREFIX + "/equity/history/orders"):
                break
            path, query = more, None
        return items

    async def _history_detail(self, user_id, ticker, order_id):
        """One order's final record from the history, or None when it is not listed."""
        for attempt in range(HISTORY_POLLS):
            try:
                found = self.group_history(
                    await self._history_pages(user_id, ticker, pages=2)
                ).get(order_id)
            except Trading212Error as error:
                if not error.definite:
                    raise
                found = None
            if found is not None:
                return found
            if attempt + 1 < HISTORY_POLLS:
                await self.sleep(POLL_WAIT_SEC)
        return None

    async def _settle(self, user_id, ticker, order_id, held_before):
        """Wait for the order to leave the pending list, cancelling it if it will not.

        Returns ``(detail, cancelled by Condor)``. Anything short of a certain final
        state raises, and the caller reports an uncertain outcome.
        """
        cancelled = False
        if not await self._await_gone(user_id, order_id, PENDING_POLLS):
            # Never leave a resting order: cancel it, then wait for it to be gone.
            await self._cancel(user_id, order_id)
            cancelled = True
            if not await self._await_gone(user_id, order_id, CANCEL_POLLS):
                raise ValueError("Order is still pending after the cancel request")
        detail = await self._history_detail(user_id, ticker, order_id)
        if detail is None:
            # A cancelled order with no fill may not be listed in the history. It is
            # taken as unfilled only when Condor cancelled it and the account's
            # position did not move.
            now = (await self._positions(user_id, fresh=True)).get(ticker) or {}
            if cancelled and now.get("quantity", Decimal(0)) == held_before:
                return (
                    {
                        "order_id": order_id,
                        "ticker": ticker,
                        "status": "cancelled",
                        "filled_quantity": Decimal(0),
                        "average_price": None,
                        "fee_eur": None,
                        "fx_fee_eur": None,
                        "net_value_eur": None,
                        "updated": None,
                        "inferred": (
                            "not listed in the history; cancelled with the account "
                            "position unchanged"
                        ),
                    },
                    True,
                )
            raise _NotInHistory(cancelled)
        if detail["status"] not in FINAL_STATUSES and not (
            detail["filled_quantity"] > 0 and detail["status"] == "partially_filled"
        ):
            raise ValueError("Order has not reached a final state")
        return detail, cancelled

    def _valued(self, side, detail, fx, cash_before, cash_after, fee_rate):
        """What a fill was worth in EUR, and where each figure comes from.

        The account's own cash change is the best evidence (it includes every fee and
        the real FX rate) when it agrees with the fill; then the reported net value of
        the fills; last an ESTIMATE from the fill price and the reference EUR/USD rate.
        """
        quantity = detail["filled_quantity"]
        if quantity <= 0:
            return {
                "quantity": Decimal(0),
                "value": Decimal(0),
                "fee": Decimal(0),
                "value_source": "none",
                "fee_source": "none",
                "average_price": None,
            }
        average = detail.get("average_price")
        expected = quantity * average / fx if average and fx else None
        reported_fee = detail.get("fee_eur")
        total, source = None, None
        if cash_before is not None and cash_after is not None:
            delta = (
                cash_before - cash_after if side == "buy" else cash_after - cash_before
            )
            if delta > 0 and (
                expected is None or abs(delta / expected - 1) <= CASH_AGREEMENT
            ):
                total, source = delta, "account cash change"
        if total is not None:
            fee = reported_fee if reported_fee and reported_fee < total else Decimal(0)
            fee_source = "reported" if fee else "none (included in the value)"
            value = total - fee if side == "buy" else total + fee
        elif detail.get("net_value_eur"):
            value, source = detail["net_value_eur"], "reported netValue of the fills"
            fee = reported_fee or Decimal(0)
            fee_source = "reported" if reported_fee else "none"
        elif expected is not None:
            value, source = expected, "ESTIMATE from the fill price and EUR/USD"
            fee = value * fee_rate["fx_fee_pct"] / 100
            fee_source = "estimate"
        else:
            raise ValueError("Trading 212 reported no usable fill value")
        return {
            "quantity": quantity,
            "value": value,
            "fee": fee,
            "value_source": source,
            "fee_source": fee_source,
            "fx_fee": detail.get("fx_fee_eur"),
            "average_price": average,
        }

    @staticmethod
    def _guard_market_fill(plan, detail, valued):
        """A market order fills at whatever the market gives; say so when it strays."""
        average = valued.get("average_price")
        if plan["order_type"] != "market" or not average:
            return
        reference = plan["reference_price"]
        deviation = abs(average - reference) / reference * 100
        if deviation > MARKET_DEVIATION_PCT:
            detail["price_note"] = (
                f"market order filled {deviation:.2f}% from the reference price "
                f"{fmt(reference)} USD"
            )

    def _apply_fill(self, ledger, side, symbol, ticker, detail, valued):
        quantity, value, fee = valued["quantity"], valued["value"], valued["fee"]
        if quantity > 0:
            if side == "buy":
                spent = value + fee
                previous = ledger["positions"].get(symbol, {"units": "0", "cost": "0"})
                ledger["positions"][symbol] = {
                    "units": fmt(Decimal(previous["units"]) + quantity),
                    "cost": fmt(Decimal(previous["cost"]) + spent),
                }
                ledger["tickers"][symbol] = ticker
                ledger["cash"] = fmt(Decimal(ledger["cash"]) - spent)
            else:
                position = ledger["positions"].get(symbol)
                if not position or quantity > Decimal(position["units"]):
                    raise ValueError("Filled sale exceeds the bot position")
                fraction = quantity / Decimal(position["units"])
                cost = Decimal(position["cost"]) * fraction
                if fraction == 1:
                    del ledger["positions"][symbol]
                    ledger["tickers"].pop(symbol, None)
                else:
                    position["units"] = fmt(Decimal(position["units"]) - quantity)
                    position["cost"] = fmt(Decimal(position["cost"]) - cost)
                ledger["cash"] = fmt(Decimal(ledger["cash"]) + value - fee)
            observed = valued.get("fx_fee")
            if observed and value >= OBSERVED_FEE_MIN_VALUE_EUR:
                # The first real FX fee on a fill big enough to read tells the truth
                # about the rate; a cent-rounded fee on a tiny fill does not.
                ledger["observed_fx_fee_pct"] = fmt(
                    (observed / value * 100).quantize(Decimal("0.0001"))
                )
        ledger["uncertain_order"] = None
        ledger["orders"] = (
            ledger["orders"]
            + [
                {
                    "time": time.time(),
                    "side": side,
                    "symbol": symbol,
                    "ticker": ticker,
                    "order_id": detail.get("order_id"),
                    "client_oid": None,
                    "status": detail["status"],
                    "quantity": fmt(quantity),
                    "value": fmt(value),
                    "fee": fmt(fee),
                    "fee_currency": QUOTE,
                    "fee_source": valued["fee_source"],
                    "value_source": valued["value_source"],
                    **(
                        {"fill_source": valued["fill_source"]}
                        if "fill_source" in valued
                        else {}
                    ),
                }
            ]
        )[-200:]

    @staticmethod
    def _summarise(detail, valued, plan, cancelled):
        average = valued.get("average_price")
        reference = plan["reference_price"]
        out = {
            "order_id": detail.get("order_id"),
            "ticker": plan["ticker"],
            "side": plan["side"],
            "order_type": plan["order_type"],
            "status": detail["status"],
            "quantity": fmt(plan["quantity"]),
            "filled_quantity": fmt(valued["quantity"]),
            "limit_price": fmt(plan["limit_price"]),
            "price_currency": PRICE_CURRENCY,
            "average_fill_price": fmt(average) if average else None,
            "filled_value_eur": fmt(valued["value"]),
            "fee_eur": fmt(valued["fee"]),
            "fee_source": valued["fee_source"],
            "value_source": valued["value_source"],
            "cancelled_by_condor": cancelled,
            "reference_price": fmt(reference),
            "reference_source": plan["reference_source"],
        }
        if average and reference:
            out["fill_vs_reference_pct"] = str(
                round((average - reference) / reference * 100, 3)
            )
        for key in ("fill_source", "average_price_source", "fee_note"):
            if valued.get(key):
                out[key] = valued[key]
        for key in ("price_note", "inferred"):
            if detail.get(key):
                out[key] = detail[key]
        if valued["quantity"] <= 0:
            out["note"] = "Nothing was filled; no money moved."
        return out

    async def history(self, user_id):
        """The most recent orders, shaped like the other venues' histories (EUR figures)."""
        groups = self.group_history(
            await self._history_pages(user_id, pages=1), strict=False
        )
        rows = []
        for detail in groups.values():
            average = detail.get("average_price")
            moved = detail["filled_quantity"] > 0
            value = detail.get("net_value_eur")
            row = {
                "order_id": detail["order_id"],
                "pair": detail["ticker"],
                "side": detail["side"],
                "status": detail["status"],
                "type": detail["type"],
                "quantity": fmt(detail["quantity"]),
                "filled_quantity": fmt(detail["filled_quantity"]),
                "limit_price": detail.get("limit_price"),
                "average_fill_price": fmt(average) if average else None,
                "filled_amount": fmt(value) if value else None,
                "total_fee": (
                    fmt(detail["fee_eur"])
                    if detail.get("fee_eur") is not None
                    else None
                ),
                "fee_currency": QUOTE if detail.get("fee_eur") is not None else None,
                "updated": (
                    int(detail["updated"] * 1000) if detail.get("updated") else None
                ),
            }
            if moved or detail["status"] in FINAL_STATUSES:
                rows.append({k: v for k, v in row.items() if v is not None})
        rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
        return rows[:50]

    # ── reconcile ──

    async def _find_order(self, user_id, pending):
        """Look for the uncertain order among the pending and the historical orders.

        There is no client order id, so a candidate must match the ticker, the side and
        the quantity, have been created through the API shortly before the order was
        sent, and be the only such order. Returns ``(kind, detail, ambiguous)``.
        """
        ticker, side = pending["ticker"], pending["side"]
        quantity = Decimal(pending["quantity"])
        since = float(pending["time"]) - 30

        def matches(created, qty, order_side, origin):
            return (
                created is not None
                and created >= since
                and order_side == side
                and qty is not None
                and abs(qty) == quantity
                and origin in (None, "API")
            )

        if pending.get("order_id"):
            info = await self._pending(user_id, pending["order_id"])
            if info is not None:
                return "pending", info, False
        else:
            rows = await self.private(user_id, "GET", "/equity/orders")
            hits = [
                self._normalize_pending(r)
                for r in (rows if isinstance(rows, list) else [])
                if isinstance(r, dict)
            ]
            hits = [
                h
                for h in hits
                if h["ticker"] == ticker
                and matches(h["created"], h["quantity"], h["side"], h["initiated_from"])
            ]
            if len(hits) == 1:
                return "pending", hits[0], False
            if len(hits) > 1:
                return None, None, True
        groups = self.group_history(await self._history_pages(user_id, ticker))
        if pending.get("order_id") and pending["order_id"] in groups:
            return "history", groups[pending["order_id"]], False
        # Also by ticker, side, quantity and the time window, whether or not an id is
        # known: the history may list the order under a record that does not carry it.
        hits = [
            d
            for d in groups.values()
            if matches(d["created"], d["quantity"], d["side"], d["initiated_from"])
        ]
        if len(hits) == 1:
            return "history", hits[0], False
        if len(hits) > 1:
            return None, None, True
        return None, None, False

    @staticmethod
    def _object_detail(info):
        """A final-state order object read by id, shaped like a history record."""
        return {
            "order_id": info["order_id"],
            "ticker": info["ticker"],
            "side": info["side"],
            "status": info["status"],
            "quantity": abs(info["quantity"] or Decimal(0)),
            "filled_quantity": info["filled_quantity"] or Decimal(0),
        }

    async def reconcile(self, user_id):
        """Settle an order whose outcome was unknown. Leaves trading paused.

        Evidence, in this order: (i) the order object by id; (ii) the pending and
        historical orders (by id, else ticker, side, quantity and the time window);
        (iii) the ACCOUNT: free cash and the ticker's quantity against the snapshot
        taken before the order was sent. A fill that only the account shows is booked
        from it (labelled). The order is cleared as "never created" only when all of
        them show no trace of it for ``NOT_SEEN_AFTER_SEC``; mixed or ambiguous
        evidence keeps it uncertain, and nothing is ever cleared on the history's
        silence alone.
        """
        async with self.locks[user_id]:
            ledger = _read_ledger(user_id)
            pending = ledger["uncertain_order"]
            if not pending:
                return self.status(user_id)
            known = bool(pending.get("order_id"))
            age = time.time() - pending["time"]
            kind, detail, ambiguous = await self._find_order(user_id, pending)
            if ambiguous:
                raise ValueError(
                    "More than one Trading 212 order matches this one (same ticker, "
                    "side and size): keep trading paused, check the Trading 212 order "
                    "history and clear the uncertain order by hand"
                )
            if kind == "pending" and detail["status"] in FINAL_STATUSES:
                kind, detail = "object", self._object_detail(detail)
            if kind == "pending":
                # Never leave a resting order behind: ask for it to be cancelled.
                await self._cancel(user_id, detail["order_id"])
                raise ValueError(
                    "The order is still pending at Trading 212; a cancel was requested. "
                    "Reconcile again shortly"
                )
            if kind == "history":
                await self._apply_history(user_id, ledger, pending, detail)
            elif kind == "object":
                await self._apply_object(user_id, ledger, pending, detail)
            else:
                if age < MIN_RECONCILE_WAIT_SEC:
                    raise ValueError("Wait a minute before reconciling")
                verdict = await self._poll_verdict(
                    user_id, ledger, pending, known=known, settle=False
                )
                if verdict["verdict"] in ("filled", "partial"):
                    fx = await self.eur_usd()
                    book, valued = await self._book_evidence(
                        ledger, pending, verdict, fx
                    )
                    self._apply_fill(
                        ledger,
                        pending["side"],
                        pending["symbol"],
                        pending["ticker"],
                        book,
                        valued,
                    )
                elif verdict["verdict"] == "unchanged":
                    if age < NOT_SEEN_AFTER_SEC:
                        raise ValueError(
                            "No trace of the order yet in the pending list, the "
                            "history or the account; it stays uncertain until "
                            f"{NOT_SEEN_AFTER_SEC} s have passed since it was sent "
                            f"({int(NOT_SEEN_AFTER_SEC - age)} s left): "
                            + verdict["text"]
                        )
                    # Not pending, not in the history and the account has not moved
                    # in minutes: Trading 212 never created it.
                    ledger["uncertain_order"] = None
                else:
                    raise ValueError(
                        "The evidence is mixed or ambiguous, so the order stays "
                        "uncertain and trading stays paused (nothing is guessed): "
                        + verdict["text"]
                        + ". Check the Trading 212 order history and account."
                    )
            ledger["enabled"] = False
            _write(_root(user_id) / "ledger.json", ledger)
            return self.status(user_id)

    async def _apply_history(self, user_id, ledger, pending, detail):
        """Book an order the history lists; an unfilled one must agree with the account."""
        if detail["status"] not in FINAL_STATUSES and not (
            detail["filled_quantity"] > 0 and detail["status"] == "partially_filled"
        ):
            raise ValueError("The order is still open; reconcile again shortly")
        cash_before = cash_after = None
        if detail["filled_quantity"] <= 0:
            # The history says nothing filled: the account must say the same.
            verdict = await self._poll_verdict(
                user_id, ledger, pending, known=True, settle=False
            )
            if verdict["verdict"] != "unchanged":
                raise ValueError(
                    "The history shows no fill but the account changed, so the order "
                    "stays uncertain: " + verdict["text"]
                )
        else:
            before = self._baseline(pending, ledger)
            if before["source"].startswith("snapshot"):
                try:
                    after = await self._snapshot(user_id, pending["ticker"])
                    cash_before, cash_after = before["cash"], after["cash"]
                except ValueError:
                    pass
        fx = await self.eur_usd()
        valued = self._valued(
            pending["side"], detail, fx, cash_before, cash_after, fx_fee_rate(ledger)
        )
        self._apply_fill(
            ledger,
            pending["side"],
            pending["symbol"],
            pending["ticker"],
            detail,
            valued,
        )

    async def _apply_object(self, user_id, ledger, pending, detail):
        """The order object by id is in a final state: the account supplies the figures."""
        quantity = detail["filled_quantity"]
        verdict = await self._poll_verdict(
            user_id,
            ledger,
            pending,
            known=True,
            settle=False,
            quantity=quantity if quantity > 0 else None,
        )
        if quantity <= 0:
            if verdict["verdict"] != "unchanged":
                raise ValueError(
                    "The order is final with no fill but the account changed, so it "
                    "stays uncertain: " + verdict["text"]
                )
            ledger["uncertain_order"] = None
            return
        if verdict["verdict"] not in ("filled", "partial"):
            raise ValueError(
                "The order reports a fill that the account does not confirm, so it "
                "stays uncertain: " + verdict["text"]
            )
        book, valued = await self._book_evidence(
            ledger, pending, verdict, await self.eur_usd()
        )
        book["status"] = detail["status"]
        book["inferred"] = (
            "the order object by id was final; booked from the account: "
            + verdict["text"]
        )
        self._apply_fill(
            ledger, pending["side"], pending["symbol"], pending["ticker"], book, valued
        )

    # ── scorecard ──

    async def predict(
        self, user_id, symbol, verdict, target_pct, stop_pct, horizon_hours, note=""
    ):
        symbol = symbol_of(symbol)
        if scorecard.latest_price(user_id, symbol, venue=VENUE) is None:
            await self.stocks(user_id, symbol)
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
        """The live calls scored at an estimated round trip (the FX fee both ways, spread, slippage)."""
        rate = fx_fee_rate(_read_ledger(user_id))["fx_fee_pct"]
        cost = float(
            2 * rate
            + SPREAD_FLOOR_PCT * 2
            + 2 * SLIPPAGE_ALLOWANCE_PCT_PER_LEG
            + REGULATORY_ALLOWANCE_PCT
        )
        return scorecard.report(user_id, cost_pct=cost, venue=VENUE)


service = Trading212Service()
