# Trading 212 (US stocks, Invest account in EUR)

Condor trades US large-cap stocks on the owner's **Trading 212 Invest account** through
Trading 212's public API, live only, in EUR, during the regular US session. It follows
the pattern of the Kraken xStocks and Revolut X modules: a per-user ledger of the bot's
allocation, a native service (`condor/trading212.py`), web routes (`/api/v1/trading212`),
an MCP tool (`trading212`), a three-agent crew (`trading212_trader`, `trading212_analyst`,
`trading212_risk_reviewer`; loop `intraday`), a Settings tab and a read-only Trade tab.

**Nothing in it has run against a real account.** Every statement about an authenticated
endpoint is read from Trading 212's public documentation, not observed. The first real
order must be tiny and approved by the owner; see "First real order" below.

## What it does and does not do

* Live only. There is no demo mode and no paper mode: the base URL is
  `https://live.trading212.com/api/v0`.
* Orders: one `limit` (default) or `market` order per call, quantity-based, never
  extended hours, never short selling, never margin. The module can only reach the
  endpoints in an allow-list (`_ROUTES`): account summary, positions, pending orders (and
  one by id), place limit, place market, cancel one order, instruments, exchanges,
  historical orders, transactions. Pies, stop orders, reports and anything else are
  refused before a request is built. **The Trading 212 API has no withdrawal endpoint**
  (and no withdrawal permission), so there is nothing to disable.
* Only a **EUR** account is accepted. The currency is read from the account summary at
  sync and before every order; any other currency refuses the order and says why
  (orders execute in the account's own currency, and every amount here is read as EUR).
* Only **US stocks that Trading 212 lists and the Kraken xStocks public feed covers**
  (so a reference price exists). The mapping `AAPL` -> `AAPL_US_EQ` is read from the
  instrument metadata at runtime: `type` STOCK, `currencyCode` USD, a US ISIN and a
  short name equal to the ticker's symbol; a symbol claimed by two listings maps only
  when exactly one is the plain `_US_EQ` listing.
* Guards (same as the other venues, in EUR): 40% single-stock cap below 1000 of bot
  equity (from 1000: 50% cash reserve, three positions, one sixth per position), equity
  floor 2 (trading pauses), at most ten orders per minute, one order per call, a buy of
  at least 1 EUR and at least the instrument's minimum quantity when it reports one,
  regular US session only (`us_market_open()`), cross-checked against Trading 212's
  own exchange working schedule when it can be read (a schedule that says *closed*
  refuses the order; one that cannot be read is "unknown" and does not block).

## How prices are obtained

Trading 212 has **no market-data endpoint** (verified from the endpoint list). Prices
for the `stocks` table, the candles, sizing and limit prices are **reference prices in
USD from Kraken's public xStocks data** (`condor/xstocks_market.py`), a tokenized copy of
the same stocks. They are good for price levels and candles and are labelled as such in
the tool answers, the Trade tab and the crew prompts. For a stock the account already
holds, Trading 212's own current price (positions endpoint) is used for a sale's limit
(the lower of the two). The real fill price, quantity and fees come only from Trading
212: the historical-orders endpoint and the account cash change, never from the
reference. The EUR/USD rate for sizing is Kraken's public EURUSD (`kraken_xstocks`'s
`eur_usd`), not Trading 212's own conversion rate (a 0.5% sizing buffer covers the
difference).

Valuation (`valuation`, `equity`) uses Trading 212's own positions endpoint
(`walletImpact.currentValue` per share, already in EUR), not the reference.

## Cost model (round trip, percent of the order value)

| part | value | source |
|---|---|---|
| commission | 0 | published (Trading 212 help centre) |
| FX fee | 0.15% each leg, 0.30% round trip | **published** (help centre "What is the FX fee?": applied when the instrument's currency differs from the account's, on buy and sell); replaced by the **observed** rate after the first fill of at least 50 EUR |
| spread | the Kraken reference spread, never below 0.02% | **estimate** |
| slippage | 0.03% per leg | **assumption** |
| regulatory | 0.003% on a sale | **estimate** (SEC fee and FINRA fee on US sales; the help centre lists them, amounts are tiny) |
| rounding | 0.01 EUR per leg over the order value | **assumption** carried over from the Revolut X lesson (EUR amounts rounded to the cent); makes tiny orders look expensive |

The `preview` answer carries `estimated_round_trip_pct`, `round_trip_components_pct`,
`round_trip_sources` (published / observed / estimate / assumption) and a `cost_label`.
**Cost guard:** a *buy* whose estimated round trip is above **1.0%** is refused with no
order sent (`cost_guard_blocks_buy` says so in the preview); with a 0.05% spread that is
an order below about 3.5 EUR. A sale is never refused for cost. There is deliberately no
owner override route (the Revolut X `allow_costly_buy` has no counterpart here).

## Order semantics

Verified from the documentation: `POST /equity/orders/limit` takes `ticker`, `quantity`
(negative sells), `limitPrice`, `timeValidity` (`DAY` or `GOOD_TILL_CANCEL`);
`POST /equity/orders/market` takes `ticker`, `quantity`, `extendedHours`. There is **no
immediate-or-cancel**. Both endpoints are documented as **not idempotent** (a repeat can
create a duplicate) and the API has **no client order id**.

So the module works like this:

1. `limit` (default): a `DAY` limit priced `max_slippage_pct` (default 0.3, maximum 2)
   through the reference ask (buy) or bid (sale), rounded to the cent. A fast market
   cannot fill it worse than that. `market`: for the one case where Trading 212 refuses
   limit orders; it is sized the same way, never extended hours, and a fill more than 2%
   from the reference is flagged in the answer.
2. **Write-before-send:** the order is written to the ledger as `uncertain_order`
   (ticker, side, quantity, limit price, time) before the request leaves. A definite
   refusal clears it; anything else keeps trading paused.
3. **Definite vs uncertain:** HTTP 4xx (400, 401, 403, 404, **429**) means nothing was
   placed, except **408**, which is treated as uncertain. A local rate-limit refusal sent
   nothing. 5xx, 408, a timeout, an unreadable body or a missing order id are
   **uncertain**: nothing is retried, trading pauses, and the answer says "Do not
   resubmit". 429 honours `Retry-After`, else `x-ratelimit-reset`, and blocks that
   endpoint class locally for that long.
4. **Settle:** poll `GET /equity/orders/{id}` until Trading 212 no longer lists the
   order (404); if it is still pending after the bounded wait, **cancel it** and wait for
   it to disappear. A resting order is never left behind. Then read the historical
   orders for the ticker and take the fills (price, quantity, `walletImpact`).
5. **Valuation of the fill in EUR:** the account's own cash change around the order
   (best evidence: includes every fee and the real FX rate) when it agrees with the fill
   within 10%; else the fills' reported `netValue`; else an **ESTIMATE** from the fill
   price and EUR/USD. The answer says which (`value_source`, `fee_source`).
6. **Zero or partial fills** are normal: only what filled moves the ledger. A cancelled
   order that the history does not list is accepted as unfilled **only** when Condor
   cancelled it and the account's position did not move (labelled `inferred`).
7. **Reconcile** (`reconcile`): by order id when known, else by listing pending orders
   and the historical orders and matching **ticker, side, quantity, created through the
   API within 30 s before the send**. A pending match is cancelled (and trading stays
   paused); a historical match is applied; two candidates are never guessed between; an
   order that is nowhere after a minute is cleared as never created. Orders placed by hand
   in the app (`initiatedFrom` other than `API`) never match. Reconcile always leaves
   trading paused; the owner enables it again in Settings.

Pacing: Condor spaces calls a little wider than the published per-account limits
(summary 1 per 5 s, pending orders 1 per 5 s, order by id and positions 1 per s,
instruments 1 per 50 s, exchanges 1 per 30 s, limit order 1 per 2 s, market order and
cancel 50 per minute, history and transactions 20 per minute); a wait longer than 12 s is
a refusal. Reads are cached for a few seconds, the instrument list for 30 minutes.

## Setup

1. In the Trading 212 app: Settings > API, create a key for the **live** Invest account
   with the minimum permissions: account data, portfolio, orders read, orders execute,
   plus metadata (instrument list), history orders and history transactions (deposit
   tracking). Do **not** grant anything else. The API has no withdrawal permission.
   Optionally restrict the key to the Condor server's IP address.
2. Paste the API key and the API secret into Condor, Settings > Trading 212. They are
   stored in `$CONDOR_DATA_DIR/trading212/<user>/credentials.json` (0600), accepted
   write-only and never returned or logged. An empty secret means a legacy single key
   (sent as the bare `Authorization` value).
3. "Sync allocation" reads the account (currency must be EUR) and seeds the ledger:
   cash is `cash.availableToTrade`, positions come from the positions endpoint
   (units available to trade, cost from `walletImpact.totalCost`, else the current value
   and the answer says so). Then "Enable trading". The crew's loop is started separately
   (see the scheduler).

## VERIFIED (read from Trading 212's public documentation on 2026-10-29)

Source: docs.trading212.com (`/api`, the Authentication, Rate Limiting and Pagination
sections, the per-endpoint pages) and the help centre FX fee articles.

* Base URLs: demo `https://demo.trading212.com/api/v0`, live
  `https://live.trading212.com/api/v0`. Only Invest and Stocks ISA accounts. The API is
  in **beta**.
* Authentication: HTTP Basic, API key as username and API secret as password
  (`Authorization: Basic base64(key:secret)`); the endpoint pages also list a "legacy API
  key in the Authorization header". Optional IP restriction in the app.
* Rate limits are per account, whatever the key or IP; headers `x-ratelimit-limit`,
  `-period`, `-remaining`, `-reset` (Unix timestamp) and `-used`; limits per endpoint:
  account summary 1/5 s, pending orders 1/5 s, order by id 1/1 s, positions 1/1 s,
  instruments 1/50 s, exchanges 1/30 s, limit order 1/2 s, market order 50/min, cancel
  50/min, history orders/dividends/transactions 20/min (one page lists 6/min for history
  orders: Condor stays under the stricter figure in practice by pacing 3.2 s).
  At most 50 pending orders per ticker.
* Endpoint paths: `GET /equity/account/summary`, `GET /equity/positions`,
  `GET /equity/orders`, `GET /equity/orders/{id}`, `POST /equity/orders/limit`,
  `POST /equity/orders/market`, `DELETE /equity/orders/{id}`,
  `GET /equity/metadata/instruments`, `GET /equity/metadata/exchanges`,
  `GET /equity/history/orders`, `GET /equity/history/transactions`.
* Account summary fields: `id`, `currency`, `totalValue`, `cash.availableToTrade`,
  `cash.inPies`, `cash.reservedForOrders`, `investments.*`.
* Position fields: `averagePricePaid`, `currentPrice` (instrument currency), `quantity`,
  `quantityAvailableForTrading`, `quantityInPies`, `instrument{ticker,currency,isin,name}`,
  `walletImpact{currency,currentValue,totalCost,unrealizedProfitLoss,fxImpact}`.
* Order objects: `id` (integer), `status` enum (LOCAL, UNCONFIRMED, CONFIRMED, NEW,
  CANCELLING, CANCELLED, PARTIALLY_FILLED, FILLED, REJECTED, REPLACING, REPLACED), `side`,
  `type`, `timeInForce`, `quantity`, `filledQuantity`, `limitPrice`, `initiatedFrom`
  (API, IOS, ...), `createdAt`. Orders by value are not supported through the API.
* Historical orders: `items[]` each with `order{...}` and `fill{id, price, quantity,
  filledAt, type, walletImpact{currency, fxRate, netValue, realisedProfitLoss,
  taxes[{name, quantity, currency, chargedAt}]}}`; `taxes[].name` includes
  `CURRENCY_CONVERSION_FEE`; cursor pagination with `limit` (max 50) and `nextPagePath`.
* Transactions: `items[]` with `amount`, `currency`, `dateTime`, `reference`, `type`
  (WITHDRAW, DEPOSIT, FEE, TRANSFER, INTEREST_ON_FREE_CASH, LENDING_INTEREST).
* Scopes seen in the error tables: `account`, `portfolio`, `metadata`, `orders:read`,
  `orders:execute`, `history:orders`, `history:transactions`.
* Selling = negative `quantity`. Orders execute only in the account's primary currency.
  `timeValidity`/`timeInForce` is `DAY` (expires at midnight in the exchange's time zone)
  or `GOOD_TILL_CANCEL`: **no IOC**. Market and limit orders are documented as not
  idempotent. Market orders placed while the market is closed queue until the open.
* Error codes documented per endpoint: 400, 401, 403 (missing scope), 408, 429 (and 404
  on cancel).
* Fees: commission free; FX fee 0.15% on each leg when the instrument's currency differs
  from the account's (no FX fee on dividends); US sales carry SEC and FINRA charges.
* There is no market-data, quote or order-book endpoint.

## NOT VERIFIED (each needs the first tiny real order, or a real response, to settle)

1. **Whether live accounts accept limit orders at all.** A third-party summary said the
   live environment supports market orders only; the official pages I read say nothing
   either way. If the live API rejects limit orders, the definite 4xx comes back, the
   crew journals it and uses `order_type="market"` (same size) as instructed.
2. Whether **fractional quantities** are accepted through the API and with how many
   decimals (Condor rounds down to 4; a full close sends the exact quantity the account
   reports). The documentation example quantity is `0.1`.
3. The **currency and tick** of `limitPrice` (assumed USD, the instrument's currency;
   rounded to the cent), and any minimum price increment.
4. Whether the instrument list carries **`minTradeQuantity`** (the instruments page does
   not list it; Condor reads it when present and otherwise relies on the 1 EUR floor), and
   that `shortName` equals the ticker's symbol and `isin` starts with `US` for US stocks.
5. That `GET /equity/orders/{id}` answers **404 once an order is no longer pending**, and
   whether `GET /equity/orders` answers a bare array.
6. What the **history lists for a cancelled or unfilled order** (Condor handles both),
   whether items come newest first, one per fill or one per order, the **sign** of
   `netValue` and `taxes[].quantity`, and whether `netValue` already includes the fees.
   The primary EUR evidence is therefore the account's cash change, with `netValue` second.
7. Whether `cash.availableToTrade` is all free cash, whether a buy reserves cash
   (`reservedForOrders`), and what `totalValue` contains. Condor does not use
   `totalValue`.
8. Whether the account cash has **settled** the moment the order leaves the pending list
   (the cash-change figure is read right after).
9. The **error body shape** (Condor reads `message`, `code` or `type`), whether `429`
   carries `Retry-After`, and what `x-ratelimit-reset` holds exactly.
10. What a **408** means for an order (Condor treats it as uncertain).
11. The **exchange working schedule** semantics (`OPEN`, `CLOSE`, `BREAK_*` events and
    their date format): the holiday cross-check reads the latest past event as the
    current state and ignores anything older than four days.
12. Trading 212's own **EUR/USD conversion rate** against Kraken's (sizing carries a 0.5%
    buffer), and whether `walletImpact.totalCost` includes the FX fee.
13. The **spread, slippage, regulatory and rounding** parts of the round-trip estimate
    (all estimates or assumptions), and the FX fee itself on an EU/EUR account (the help
    centre page does not name a region; the 0.15% figure is the published one).
14. `GET /equity/history/transactions`: the **sign of WITHDRAW amounts** (Condor signs by
    type), what the `time` filter means, and whether card or bank deposits appear as
    `DEPOSIT`.
15. That the **key permissions** offered in the app match the scope names above, and
    that a key without `history:*` or `metadata` degrades as intended (deposit tracking
    "unavailable"; no instrument list is an error for trading).
16. That `extendedHours: false` is accepted on a market order (it is sent explicitly).
17. That Condor's pacing is enough when the owner also uses the API from elsewhere (the
    limits are per account, not per key).
18. Practical behaviour of a `DAY` limit order priced through the reference when the
    reference (a tokenized copy) lags the real market: the common failure is a zero fill
    that Condor cancels, which is safe.

## First real order (owner-approved, tiny)

1. Settings > Trading 212: save the key and secret, "Sync allocation" (confirm the
   account currency shows EUR and the cash and positions match the app), "Enable".
2. Run `preview` for a buy of about 6 EUR of a liquid stock (the cost guard refuses a buy
   under about 3.5 EUR): check the quantity, the limit price (USD) and the labelled cost.
3. Place that one order. Compare, against the app: the limit being accepted (item 1), the
   quantity and its decimals (2), the fill price against the reference, the EUR spent
   against `value_source: account cash change`, the FX fee in the app against the
   published 0.15% (`fee_source`), and what the history returned (6).
4. Sell it back (the full close path, which sends the exact quantity) and compare the
   round trip with the estimate.
5. Only then start the crew's loop.

## Deposits and withdrawals

`condor/native_flow_fetchers.py` reads `GET /equity/history/transactions` (cursor
pagination, `DEPOSIT` and `WITHDRAW` rows, signed by type; fees, interest and transfers
are not external flows) so the loop card PnL is a trading result. It degrades to
"unavailable: <reason>" like the other venues, and the Trade tab's deposits panel is
included automatically. See `docs/native-flows.md`.

## The stock scheduler (VM, not touched by this change)

`/srv/fortpt/ai-trade-crew/ops/stock_session.sh` currently starts only
`kraken_xstocks_trader.intraday` at the US open and stops it at the close. This is the
exact generalisation to apply on the VM: it starts and stops **both** loops, each one
only if **its own venue** is enabled, synced and not uncertain (`enabled` already means
"enabled and no uncertain order"). The session clock is read from the same route as
before; a failure to read it changes nothing. A loop that is running at the close is
stopped whichever venue it belongs to.

```bash
#!/bin/bash
# Start the stock crews (Kraken xStocks, Trading 212) when the regular US session opens and
# stop them at the close. Each one starts only if its own venue is enabled, synced and has no
# uncertain order; orders still need that venue's trading switched on.
exec docker exec -i -w /var/lib/condor fortpt-ai-trade-crew-condor-1 /var/lib/condor/.venv/bin/python - <<'PY'
import json, time, urllib.request, urllib.error
from condor.web.auth import create_jwt
from utils.config import WEB_PORT

LOOP = "intraday"
#: (crew slug, the venue's status route)
CREWS = (
    ("kraken_xstocks_trader", "/kraken-xstocks/status"),
    ("trading212_trader", "/trading212/status"),
)
token = create_jwt(1, role="user")

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{WEB_PORT}/api/v1{path}", data=data, method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    except urllib.error.HTTPError as e:
        return {"HTTP": e.code, "detail": e.read().decode()[:200]}
    except Exception as e:
        return {"error": type(e).__name__}

stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + "Z"
status = call("GET", "/xstocks-paper/status")
market = (status or {}).get("market") or {}
if "open" not in market:
    print(stamp, "market status unavailable; no change:", json.dumps(status)[:120]); raise SystemExit(0)
for slug, venue_route in CREWS:
    loop = call("GET", f"/agents/{slug}/strategies/{LOOP}")
    state = loop.get("status") if isinstance(loop, dict) else None
    if market["open"]:
        venue = call("GET", venue_route)
        ledger = venue.get("ledger", {}) if isinstance(venue, dict) else {}
        if state == "running":
            print(stamp, slug, "session open; loop already running")
        elif not venue.get("enabled") or not ledger.get("synced") or ledger.get("uncertain_order"):
            print(stamp, slug, "session open but its venue is off, unsynced or uncertain; not starting")
        else:
            r = call("POST", f"/agents/{slug}/strategies/{LOOP}/start",
                     {"config": {"frequency_sec": 300, "tick_timeout_sec": 600}})
            print(stamp, slug, "session open; started the loop:", json.dumps(r)[:160])
    else:
        if state == "running":
            r = call("POST", f"/agents/{slug}/strategies/{LOOP}/stop")
            print(stamp, slug, "session closed; stopped the loop:", json.dumps(r)[:120])
        else:
            print(stamp, slug, "session closed; loop idle")
PY
```

Notes: `/xstocks-paper/status` is the legacy name of the US session clock that stays
registered for exactly this script; `/trading212/market` answers the same clock and
needs no Kraken module. The two crews trade different accounts with separate ledgers,
so neither start condition depends on the other. Install it the same way
`install_stock_schedule.sh` installed the old one (write the file, `chmod 700`, run it once
while the market is closed and expect "session closed; loop idle" twice).

## Left out

* No demo or paper mode, no stop or stop-limit orders, no value (amount) orders, no
  extended hours, no pies, no dividends reading, no ISA or multi-currency handling.
* No owner override of the cost guard route (Revolut X has one; the owner asked for
  conservative refuse-on-doubt here).
* No VM change: the scheduler, the loops and the funded account are untouched. Nothing
  was deployed, merged or contacted at Trading 212.
