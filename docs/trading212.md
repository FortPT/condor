# Trading 212 (US stocks, Invest account in EUR)

Condor trades US large-cap stocks on the owner's **Trading 212 Invest account** through
Trading 212's public API, live only, in EUR, during the regular US session. It follows
the pattern of the Revolut X module: a per-user ledger of the bot's
allocation, a native service (`condor/trading212.py`), web routes (`/api/v1/trading212`),
an MCP tool (`trading212`), a three-agent crew (`trading212_trader`, `trading212_analyst`,
`trading212_risk_reviewer`; loop `intraday`), a Settings tab and a read-only Trade tab.

**One real order has run** (2026-10-29, see "VERIFIED against the real account"). It
filled, and it showed that the order history can omit an API order for a while: the
module now reads an order's outcome from the account itself (cash and positions), never
from the history's silence. Everything not listed under that heading is still read from
Trading 212's public documentation, not observed; see "NOT VERIFIED".

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
reference. The EUR/USD rate for sizing is Kraken's public EURUSD (`xstocks_market`'s
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
   order (404, or an order object in a final state); if it is still pending after the
   bounded wait, **cancel it** and wait for it to disappear. A resting order is never
   left behind. Then read the historical orders for the ticker and take the fills
   (price, quantity, `walletImpact`).
   **If the history does not list the order** (it can lag or omit API orders), the
   outcome is read from the **account** (see "Account evidence" below) after a short
   settle wait (three reads two seconds apart). A conclusive fill is booked and
   labelled; anything else keeps the order uncertain and trading paused.
5. **Valuation of the fill in EUR:** the account's own cash change around the order
   (best evidence: includes every fee and the real FX rate) when it agrees with the fill
   within 10%; else the fills' reported `netValue`; else an **ESTIMATE** from the fill
   price and EUR/USD. The answer says which (`value_source`, `fee_source`).
6. **Zero or partial fills** are normal: only what filled moves the ledger. A cancelled
   order that the history does not list is accepted as unfilled **only** when Condor
   cancelled it and the account's position did not move (labelled `inferred`).
7. **Reconcile** (`reconcile`), evidence in this order: (i) the order object by id; (ii)
   the pending and historical orders, by id, else **ticker, side, quantity, created
   through the API within 30 s before the send**; (iii) the **account** (below). A pending
   match is cancelled (and trading stays paused); a historical match is applied (an
   unfilled one only if the account agrees that nothing moved); two candidates are never
   guessed between. Orders placed by hand in the app (`initiatedFrom` other than `API`)
   never match. The first reconcile waits at least **60 s** after the send. An order is
   cleared as "never created" only when the pending list, the history AND the account
   (cash and that ticker's quantity) all show no trace of it for **180 s** since the send
   (`NOT_SEEN_AFTER_SEC`); between 60 s and 180 s it says how long is left. Mixed or
   ambiguous evidence stays uncertain, with the evidence listed. Reconcile always leaves
   trading paused; the owner enables it again in Settings.

### Account evidence

`GET /equity/account/summary` (free cash, `availableToTrade`) and `GET /equity/positions`
update immediately and are the ground truth. **Before any order is sent** the module reads
both (fresh) and stores in the `uncertain_order` entry (written before the send, as
before) `evidence_before` (free cash, reserved cash, the ticker's quantity) and
`expected_eur` (the order's EUR value). If the account cannot be read, no order is sent.
The outcome is then judged per **ticker** (the owner's other holdings never count):

| account change vs the snapshot | verdict |
|---|---|
| cash and the ticker's quantity both moved the right way, the quantity equals the order's (0.5% tolerance), the cash is within 10% (or 0.05 EUR) of the order's EUR value | **filled** (booked) |
| the same, for less than the quantity, order id known, reading identical twice | **partial** (booked) |
| neither moved (cash within 0.05 EUR, quantity within 0.00001) | **unchanged**: cleared only by reconcile after 180 s |
| only cash moved, only the position moved, wrong direction, the position moved by MORE than the order (a concurrent manual trade of the same ticker), cash that does not fit the order, reserved cash changed (a resting order), a partial move without an order id | **ambiguous**: stays uncertain, never guessed |

A fill booked from the account carries `fill_source: "account_evidence"` and
`value_source: "account_cash_change"`: quantity = the position's change, EUR value = the
cash change (minus the fee), fee = an **estimate** at the published (or observed) FX rate
(`fee_source: "estimate"`; no tax figures exist without a history record), average price
= EUR value / quantity converted at Kraken's reference EUR/USD (`average_price_source`
says so). In the order call a conclusive fill is returned without pausing trading; a
reconcile leaves it paused.

## The bot ledger against the real account

`valuation` (and the cached `status.account_check`) carry `ledger_matches_account` and a
`mismatch` list: the ledger's cash against the account's free cash (more than **0.05 EUR**
apart) and, for every ticker the ledger holds or the bot has traded, its units against the
account's quantity (pie shares excluded). The owner's other holdings are not errors: they
are listed under `outside_allocation` (flagged `excluded` when on the exclusion list).
**Trading cannot be enabled** while they disagree: `PUT /trading212/enabled {"enabled":
true}` reads the account first and refuses with the differences and the instruction to
pause trading and **Sync allocation** (the same step as before). A successful sync (it
seeds the ledger from the same read) or a matching check allows enabling for two minutes; a
mismatch cancels it.

## The owner's own holdings (exclusion list)

Sync **adopts** every mapped position the account holds, as the other venues do, except
the tickers on the **exclusion list** (`excluded_tickers`, plain symbols such as `MRNA`,
stored in the ledger and kept across syncs). Edit it in Settings > Trading 212 > "Holdings
kept out of the bot" or `PUT /trading212/excluded {"tickers": ["MRNA"]}` (a Trading 212
ticker such as `MRNA_US_EQ` is accepted and stored as `MRNA`). Changing it pauses trading;
sync, then enable. An excluded ticker is refused by `preview` and `order` with a definite
message before anything is sent (a refusal does not pause trading), and it counts as a
mismatch if it is still in the ledger. The crew prompts say these tickers and any holding
outside the ledger are off limits.

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

## VERIFIED against the real account (the first live order, 2026-10-29 13:31 UTC)

Account EUR, free cash 30.00, US session open. `POST /trading212/order` buy AAPL, amount
6: preview ticker `AAPL_US_EQ`, quantity **0.0188**, limit **352.29**, notional 5.96 EUR.

* A **limit order is accepted** by the live API, with a **fractional quantity of four
  decimals** and a `DAY` validity, and it **fills instantly** (the pending list,
  `GET /equity/orders`, answered a bare empty array `[]` right after).
* After the fill the account showed free cash **24.05** (5.95 EUR spent, the limit price
  read as USD: 0.0188 x 352.29 / 1.1125 = 5.95 EUR) and a position `AAPL_US_EQ` quantity
  0.0188 worth about 5.92 EUR. `availableToTrade` and the positions answer reflected the
  fill at once: they are the ground truth.
* **The history can omit an API order.** `GET /equity/history/orders?limit=5` returned
  only the owner's earlier order, not the API order 59604555109, even more than a minute
  after it filled. This is what made the old read-back give up ("needs reconciliation")
  and the old reconcile then clear the order as never created while the account had
  changed (ledger != reality). Nothing is cleared on the history's silence any more.
* **The history's shape**, from the owner's own market order (id 59601807310, `MARKET`,
  status `FILLED`, `initiatedFrom` `SYSTEM`, created 11:35:59Z, filled 13:30:00Z at
  `fill.price` 216.5 in USD): `items[]` with `order{...}` and `fill{... walletImpact{
  currency EUR, netValue 9.11, fxRate 1.11253878, taxes[{name
  CURRENCY_CONVERSION_FEE, quantity -0.01, currency EUR}]}}`. The tax quantity is
  **negative** (the module takes its absolute value); `fxRate` is USD per EUR; the 0.01
  EUR conversion fee is the published 0.15% rounded to the cent. 0.0467626 x 216.5 /
  1.11253878 = 9.10, so `netValue` 9.11 looks like it INCLUDES the 0.01 fee (one buy, not
  yet confirmed): the module still takes the account's cash change first and `netValue`
  second, and a follow-up should confirm whether `netValue` includes the fee before
  `netValue + fee` is trusted for a purchase.
* The owner's MRNA position (0.0467626) was in the account and not in the ledger: the
  bot compares per ticker, so a holding it did not open never confuses an order.
* The module's protections worked: write-before-send, the pause on doubt, no resubmit.
* **`GET /equity/history/transactions` answered HTTP 400 `/api-errors/invalid-request`**
  to the first reader (`limit=50` and `time=2026-09-29T14:15:22Z`, no milliseconds), so
  the venue showed `unavailable: transactions: ...`. Which part Trading 212 refused
  was not in the error body. Fixed to the documented form below; the corrected call has
  **not** been run live yet (no key in the fixing session).

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
* Transactions (`GET /api/v0/equity/history/transactions`, the OpenAPI spec at
  `docs.trading212.com/_spec/api.json`, read 2026-10-29): query parameters are exactly
  `cursor` (string), `time` (string, `format: date-time`, "Retrieve transactions starting
  from the specified time") and `limit` (integer, default 20, "Max items: 50"); the
  spec has no other filter and no example. `items[]` with `amount` (in the transaction's
  currency), `currency`, `dateTime` (date-time), `reference` (an ID), `type` (WITHDRAW,
  DEPOSIT, FEE, TRANSFER, INTEREST_ON_FREE_CASH, LENDING_INTEREST); `nextPagePath`
  (string). Errors 400 ("Bad filtering arguments"), 401, 403 (scope
  `history:transactions`), 408, 429; 20 requests per minute. NOT in the spec but seen in
  Trading 212's community thread on the API update (January/February 2026, user reports,
  not an official statement): every example writes `time` with milliseconds and a `Z`
  (`2026-01-12T11:12:31.337Z`); `nextPagePath` is sometimes only a query string
  (`limit=5&cursor=..&time=..`), sometimes null for some accounts, and following it can
  fail on a later page (404 "Transaction with id .. not found"); some accounts miss
  deposits.
* Account summary (`GET /equity/account/summary`) cash fields, from the spec:
  `availableToTrade` ("funds available for investing"), `inPies` ("the sum of the cash
  inside of all pies that is not yet invested"), `reservedForOrders` ("the amount of
  cash reserved for pending orders; this cash is not available for placing new trades");
  `totalValue` is described only as "Investments value in your account's primary
  currency" (ambiguous: whole account or investments only).
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

1. ~~Whether live accounts accept limit orders.~~ **Settled by the live order: they do**
   (and `market` stays for the one case where they would not).
2. ~~Fractional quantities.~~ **Settled: four decimals accepted** (0.0188). A full close
   sending the exact quantity the account reports, and sells, are still untried live.
3. The **tick** of `limitPrice` and the fill price's currency as read from a fill record:
   the limit was accepted as USD (see the arithmetic above) but the API order's own fill
   record was never read, because the history omitted it. Any minimum price increment.
4. Whether the instrument list carries **`minTradeQuantity`** (the instruments page does
   not list it; Condor reads it when present and otherwise relies on the 1 EUR floor), and
   that `shortName` equals the ticker's symbol and `isin` starts with `US` for US stocks.
5. That `GET /equity/orders/{id}` answers **404 once an order is no longer pending**
   (presumed; the order call did not record what it answered; a final-state object is
   handled too). `GET /equity/orders` answering a bare array is verified.
6. What the **history lists for a cancelled or unfilled order** (Condor handles both),
   whether items come newest first, one per fill or one per order, **when and whether the
   history lists an order placed through the API at all** (the live one was missing after
   more than a minute), the sign of `netValue` of a sale, and whether `netValue` includes
   the fee on a purchase (one fill suggests it does). The primary EUR evidence is the
   account's cash change, with `netValue` second.
7. Whether `cash.availableToTrade` is all free cash, whether a buy reserves cash
   (`reservedForOrders`), and what `totalValue` contains. Condor does not use
   `totalValue`.
8. ~~Whether the account cash has settled the moment the order leaves the pending
   list.~~ **Settled for a buy: free cash and the position updated at once.** Whether
   `quantityAvailableForTrading` follows as fast (the ledger compares the position's
   `quantity`, not that field) and the same for a sale are still unobserved.
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
14. `GET /equity/history/transactions`: that the corrected request (`limit=50`, `time` as
    `YYYY-MM-DDTHH:MM:SS.mmmZ`) is accepted (the first, millisecond-less form was refused
    with HTTP 400; that milliseconds are what it wanted is an inference from the
    published examples, and if a 400 persists the reader retries the first page with only
    `limit` and cuts the rows to the lookback itself), whether the rows come newest or
    oldest first, how `nextPagePath` behaves for this account (a full page without one is
    reported as an incomplete read), the **sign of WITHDRAW amounts** (Condor signs by
    type), and whether card or bank deposits appear as `DEPOSIT`.
15. That the **key permissions** offered in the app match the scope names above, and
    that a key without `history:*` or `metadata` degrades as intended (deposit tracking
    "unavailable"; no instrument list is an error for trading).
16. That `extendedHours: false` is accepted on a market order (it is sent explicitly).
17. That Condor's pacing is enough when the owner also uses the API from elsewhere (the
    limits are per account, not per key).
18. Practical behaviour of a `DAY` limit order priced through the reference when the
    reference (a tokenized copy) lags the real market: the common failure is a zero fill
    that Condor cancels, which is safe.
19. The account-evidence path against a **sale**, a **partial** fill and a resting order
    (`reservedForOrders`), and whether `reservedForOrders` is part of `availableToTrade`:
    all are unit-tested with fakes of the live sequence, none has been seen live.
20. The **FX fee of an evidence-booked fill** is an estimate (0.15%); the real figure is
    in the history record the API omitted. Once it appears there, the next sync or a
    sufficiently large fill (50 EUR or more) teaches the observed rate.

## First real order (owner-approved, tiny)

1. Settings > Trading 212: save the key and secret, "Sync allocation" (confirm the
   account currency shows EUR and the cash and positions match the app), "Enable".
2. Run `preview` for a buy of about 6 EUR of a liquid stock (the cost guard refuses a buy
   under about 3.5 EUR): check the quantity, the limit price (USD) and the labelled cost.
3. Place that one order. Compare, against the app: the limit being accepted (item 1), the
   quantity and its decimals (2), the fill price against the reference, the EUR spent
   against `value_source` (`account cash change`, or `account_cash_change` with
   `fill_source: account_evidence` when the history omits the order), the FX fee in the
   app against the published 0.15% (`fee_source`), and what the history returned (6).
   (Done once on 2026-10-29; see the verified section.)
4. Sell it back (the full close path, which sends the exact quantity) and compare the
   round trip with the estimate. **Still to do.**
5. Only then start the crew's loop.

## Deposits and withdrawals

`condor/native_flow_fetchers.py` reads `GET /equity/history/transactions` (`limit=50`, `time`
as a millisecond ISO timestamp, cursor pagination by `nextPagePath`, whole path or only
its query string; `DEPOSIT` and `WITHDRAW` rows, signed by type; `TRANSFER`, `FEE`,
`INTEREST_ON_FREE_CASH` and `LENDING_INTEREST` are never external flows) so the loop
card PnL is a trading result. A 400 on the first page is retried once with only `limit`.
It degrades to "unavailable: <reason>" like the other venues (a later page failing keeps
the rows already read and says so), and the Trade tab's deposits panel is included
automatically. See `docs/native-flows.md`.

## Whole-account value, and the trading result

The value the dashboard shows and the history samples every five minutes
(`portfolio()`, in USD) is the whole account: free cash (`availableToTrade`) + cash
reserved for pending orders (`reservedForOrders`) + cash in pies (`inPies`) + every
position at Trading 212's own current value. The bot allocation (the ledger and
`valuation`: free cash + the bot's positions, never the excluded tickers) is separate and
is what the bot may trade.

* **A pending order must not make money vanish.** On 2026-10-29 the owner's market order
  for MRNA (9.11 EUR) was queued before the open. The first history sample read 30.00 EUR
  = 33.42 USD; after the 13:30 fill the account read about 39 EUR, so "trading result
  since the first sample" showed +10.2 USD with nothing earned. The code already added
  `reservedForOrders` and `inPies`, so the live summary evidently did not itemise that
  reservation in any cash field. Now, when the account's own `totalValue` exceeds the
  itemised sum (cash fields + positions, every position priced) by more than
  max(0.10 EUR, 0.2%), the difference is counted as cash and named in the sample's note
  (`composition_eur.not_itemised`). If `totalValue` is only the investments' value (the
  spec's wording) it is always below the sum and nothing is added: the rule can only
  add money Trading 212 itself reports. **UNVERIFIED live**: whether `totalValue` is the
  whole account, and where the live API put the reservation.
* **The owner's excluded holdings are in the whole-account total** (the portfolio page and
  the history chart keep them) **and out of the trading result.** The view carries
  `excluded_value_usd` (the excluded tickers' market value) and the recorder stores it
  beside each sample (`sample_excluded` table). The trading result is
  `(now - excluded now) - (start - excluded at start) - confirmed flows`, so the owner's
  shares never count as the bot's profit or loss. A sample recorded before that value was
  stored has no figure: the comparison with it stays the plain equity change and says so
  in its note (it is never assumed zero).
* **Known limit:** cash that the owner turns into an excluded share by hand (or the
  reverse) moves value between the bot's side and the owner's side and shows as a loss
  (or gain) of that amount in the trading result until it is entered as a manual
  withdrawal (or deposit) in the Trade tab. The false "unconfirmed cash movement" it used
  to raise is gone (see `docs/native-flows.md`); the result, not the flow list, carries it.

## The stock scheduler (on the VM, outside this repository)

`/srv/fortpt/ai-trade-crew/ops/stock_session.sh` runs every five minutes on weekdays from cron
(`*/5 12-21 * * 1-5`, UTC; the window is wide enough for both daylight-saving regimes). It starts
`trading212_trader.intraday` at the US open and stops it at the close, and only if the venue is
enabled, synced and has no uncertain order (`enabled` already means "enabled and no uncertain
order"). The session clock is read from `/trading212/market`, which needs no account; a failure
to read it changes nothing. A loop that is running at the close is stopped.

```bash
#!/bin/bash
# Start the stock crew (Trading 212) when the regular US session opens and
# stop them at the close. Each one starts only if its own venue is enabled, synced and has no
# uncertain order; orders still need that venue's trading switched on.
exec docker exec -i -w /var/lib/condor fortpt-ai-trade-crew-condor-1 /var/lib/condor/.venv/bin/python - <<'PY'
import json, time, urllib.request, urllib.error
from condor.web.auth import create_jwt
from utils.config import WEB_PORT

LOOP = "intraday"
CREWS = (
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
status = call("GET", "/trading212/market")
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

## Left out

* No demo or paper mode, no stop or stop-limit orders, no value (amount) orders, no
  extended hours, no pies, no dividends reading, no ISA or multi-currency handling.
* No owner override of the cost guard route (Revolut X has one; the owner asked for
  conservative refuse-on-doubt here).
* No VM change: the scheduler, the loops and the funded account are untouched. Nothing
  was deployed, merged or contacted at Trading 212.
