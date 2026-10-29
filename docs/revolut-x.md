# Revolut X (live trading, EUR)

Live trading of crypto on the Revolut X exchange through its REST API, in euros, within an
allocation the owner syncs from the Revolut X account. It mirrors the Kraken xStocks service
(`condor/kraken_xstocks.py`) and the Crypto.com service
(`condor/crypto_com_exchange.py`); it changes neither. The ledger quote currency is EUR: the
owner's money is euros, so nothing is converted and only `<COIN>-EUR` pairs are traded
(USD, USDC and GBP pairs exist on Revolut X and are refused).

## Read this first: what is verified and what is not

**Verified** (official OpenAPI spec, <https://developer.revolut.com/docs/api/revolut-x-crypto-exchange.yml>,
and the PUBLIC endpoints, read live on 28 October 2026; no key involved):

* Public pair configuration `GET /api/1.0/public/configuration/pairs`: a dict keyed
  `BASE/QUOTE` with `base_step` (quantity step), `quote_step` (price step), `min_order_size`,
  `max_order_size`, `min_order_size_quote` (0.1 for the EUR pairs), `max_order_size_quote`,
  `status`. 61 EUR pairs were listed.
* Public tickers `GET /api/1.0/public/tickers` (a `data` list with `symbol`, `bid`, `ask`,
  `mid`, `last_price`, `price_change_24h`, `volume_24h`, `quote_volume_24h`, `region`); only
  `region=EEA` rows are used. `price_change_24h` is an absolute move in the quote currency,
  not a percentage (the service converts it).
* Public order book v2 `GET /api/2.0/public/order-book/{pair}?limit=n`: `data.asks` and
  `data.bids` of `{price, quantity, count}`.
* Public candles `GET /api/1.0/public/candles/{pair}?interval=<minutes>&since=<ms>&until=<ms>`:
  intervals 1, 5, 15, 30, 60, 240 and 1440 minutes work (3 is refused with HTTP 400), at most
  about 1000 candles per call, `data` rows of `{start, open, high, low, close, volume}`.
* There is **no EUR/USD pair** on Revolut X (`EUR-USD` answers "No such pair"). The dashboard
  therefore values euros in USD through the USDC/EUR mid price (a stand-in for the euro
  rate), falling back to Kraken's public EURUSD.
* The signing formula, against the Ed25519 test vectors of RFC 8032 and an independent
  assembly of the message from the spec (`tests/test_revolut_x.py`). The formula is
  verified, and Revolut X accepted the signed requests of the real order of 28 October (below).
* The documented shapes: the order object (`id`, `client_order_id`, `symbol`, `side`, `type`,
  `quantity`, `filled_quantity`, `leaves_quantity`, `amount`, `filled_amount`, `price`,
  `average_fill_price`, `total_fee`, `fee_currency`, `status`, `reject_reason`,
  `time_in_force`, `created_date`, `updated_date`), the placement answer
  (`data.venue_order_id`, `client_order_id`, `state`), balances, fills (`p`, `q`, `im` for
  maker, `oid`, `s`, `tdt`) and errors (`error_id`, `message`, `timestamp`).

**Verified by a real 1 EUR BTC/EUR buy and sell on 2026-10-28** (the first private calls this
service ever made; everything else here is still fakes):

* The order body is accepted: `{"client_order_id": <uuid>, "symbol": "BTC-EUR", "side":
  "buy"|"sell", "order_configuration": {"limit": {"base_size": ..., "price": ...,
  "time_in_force": "ioc", "execution_instructions": ["allow_taker"]}}}`. The IOC limit through
  the touch filled as a taker.
* `GET /orders/{id}` returns `{"data": order}` and the order carries `total_fee` and
  `fee_currency`. `GET /orders/fills/{id}` works. Balances return a plain list of
  `{currency, available, reserved, total}`.
* Quantities are rounded to the pair's `base_step`.
* The buy's fee was reported in the base currency (about 2e-8 BTC); the sell's fee was reported
  in EUR and rounded to a cent (0.01 EUR on a 0.99 EUR sale).
* **EUR amounts are rounded to the cent, and that dominates small trades.** The buy took exactly
  1.00 EUR from the balance although the fills were worth 0.996945; the sell's fills were worth
  0.9955751 but the balance rose by only 0.98 (0.99 reported, minus the 0.01 fee). The round trip
  lost 0.02 EUR (about 2%) against the 0.19% a taker fee and spread would suggest.
* The order object's `average_fill_price` is a rounding artefact: for the sale it read 72368.42
  although the real price (fills value over quantity) was about 72776. It equals the cent-rounded
  `filled_amount` (0.99) divided by the quantity. The price a trade really got comes from the
  fills.
* The 0.09% taker fee is not zero on this account: the buy fee was non-zero, so Premium did NOT
  make the fee zero. The exact rate is still unread, because the cent-rounded sell fee is too
  coarse (0.01 on 0.99 EUR would read as 1%); the service learns the rate only from a fill of
  at least 50 EUR.

What Condor does with this:

* `preview` adds a cent-rounding allowance of up to 0.01 EUR per leg (0.02 EUR per round trip,
  `ROUNDING_ALLOWANCE_EUR_PER_LEG` in `condor/revolut_x.py`) to `estimated_round_trip_pct`, which is
  now two taker fees plus the spread plus the impact plus that allowance as a percentage of the
  order. It reports `rounding_allowance_eur`, `rounding_allowance_pct` and every part in
  `round_trip_components_pct`, so the crew can see why a small trade looks expensive (1.5 EUR:
  about 1.3 points of allowance; 10 EUR: 0.2; 40 EUR: 0.05).
* A BUY whose preview round-trip cost is above 1.0% (`MAX_BUY_ROUND_TRIP_PCT`) is refused before
  anything is sent ("Refused, no order placed", with the order size that would be cheaper). The
  preview flags it in advance (`cost_guard_blocks_buy`). The owner's API route can pass
  `allow_costly_buy` to override; the MCP tool the crew uses has no such argument. A sale is never
  blocked by this: an exit must always be possible.
* The user-facing `average_fill_price` of an order is the fills-derived price (value over
  quantity) whenever the fills add up to the filled quantity; the order's own figure is kept as
  `reported_average_fill_price` and a `price_note` appears when they differ materially. The
  result also carries `fills_value`, `rounding_cost_eur` (what the cent amounts cost against the
  fills' value) and `realised_cost_eur` (that plus the fee). The ledger itself still moves by the
  EUR amounts Revolut X reports, because those are what the balance really did.

**VERIFIED live on 2026-10-29: a BUY's taker fee is charged in the BASE currency, a SELL's in
EUR.** A BTC buy paid 0.00000002 BTC and an XRP buy 0.01242 XRP, and the real balance was the
filled quantity minus that fee. The order object may NOT carry `total_fee` immediately: the ETH/EUR
buy of 14:22 UTC was read back without one, the estimate was booked in EUR against cash and the
position kept the full filled quantity (0.00573246 ETH), while the account held 0.0057273 ETH (the
0.09% fee, 5.16e-6 ETH, was taken from the coin). A full-position sale then asked for more ETH than
was available and Revolut X refuses that, which could have blocked a stop-loss exit. Condor now:

* books an estimated BUY fee in the base currency (quantity times the taker rate, rounded UP to the
  pair's `base_step`, `fee_source: "estimate"`, `fee_currency` the coin, also shown as
  `estimated_fee` and `estimated_fee_currency`): the position units fall by it, and cash moves only
  by the EUR value of the fill. An estimated SELL fee stays in EUR (off the proceeds). A reported
  fee, in either currency, is used as reported and does not trigger the balance read below.
* after any filled order whose fee was only an estimate, reads `GET /1.0/balances` once and never lets
  the ledger claim more than the account holds: if the ledger units for that coin exceed the
  available balance by more than one `base_step`, the units are set to the available balance
  (`units_source: "account_balance"` on the position, `balance_correction` on the order and in the
  order log), and the ledger cash is capped to the available EUR (`cash_source:
  "account_balance"`). Only a downward correction is made: more in the account than in the ledger
  is outside the allocation and is left alone. A buy whose coin shows no balance at all is flagged
  (`balance_check`) and not erased, since that reads like a stale answer; a failed read leaves the
  fill as booked (`balance_check: "unavailable"`).
* sizes a sale of the WHOLE position from `min(ledger units, available balance)`, so a rounding
  mismatch cannot make a full exit refuse. When the balance is below the pair's minimum order size or
  minimum value the sale is refused before anything is sent, with a message saying so. A partial sale
  is never resized silently. A resized exit reads the balances again afterwards, so the dust the ledger
  no longer owns is dropped.

**Still NOT verified against a real account** (every test uses fakes for these):

* That the balances read right after a fill already shows the filled amounts and the fee (the
  correction above relies on it; the live ETH numbers say the account itself was exact).

* Partial fills: which status an immediate-or-cancel order that fills in part ends in. The
  statuses are `pending_new`, `new`, `partially_filled`, `filled`, `cancelled`, `rejected`,
  `replaced`. The service treats `filled`, `cancelled`, `rejected` and `replaced` as final; a
  `partially_filled` order that is no longer in `GET /orders/active` is also treated as final.
  Anything else after ten polls stays an uncertain order.
* Refused orders: the answer to a body or balance Revolut X rejects (HTTP 4xx is assumed to be
  definite, with the shape `error_id`, `message`), and whether an unknown order id answers 404.
* HTTP 429 behaviour and the `Retry-After` header (assumed milliseconds), and the published rate
  limits (reads 100 a second and 1,000 a minute, place order 10 a second and 1,000 a day, cancel
  100 a second, 300 active orders). Condor limits itself to 600 private reads a minute and 900
  orders a day.
* Whether the Premium plan changes fees in some other way: the buy fee was non-zero, so it does not
  make them zero, but the true taker (and maker) rate is unread.
* A fee missing or zero on a filled order is not trusted and the published rate is charged to the
  ledger as an **estimate** (`fee_source: "estimate"`, in the currency the venue really charges,
  see the next section); a reported positive fee is `reported`.
* API key permissions and IP rules. The spec shows an HTTP 401 "API key can only be used for
  authentication from whitelisted IP", so the key may be IP-restricted.
* How the signature treats a query string that needs percent-encoding (the `cursor` of a
  paginated list is passed through as received) and the shape of the paginated lists
  (`metadata.next_cursor`) and the `symbols` filter (not used).
* There is no sandbox. Any new behaviour is tested by a tiny real order.

**Size matters more than the fee.** Because of the cent rounding, an order below about 10 EUR
pays a large share of its value to rounding, and with under about 25 EUR of bot equity the venue
is not worth day-trading. The crew is told to prefer entries of at least 10 EUR and to pass on
smaller ones. Any new order flow is still checked by hand: compare the order in the Revolut X app,
its `total_fee` and its fills with the ledger.


## Connecting (the key never leaves the server)

Revolut X signs requests with Ed25519. Settings → Revolut X does the following, and the
private key never enters the browser, the model or a log:

1. **Generate signing key.** The server creates the key pair with the `cryptography` library
   (already a dependency through `python-jose[cryptography]`). The private key is written to
   `CONDOR_DATA_DIR/revolut_x/<user>/signing_key.pem` with mode 0600 and is never returned;
   the answer carries only the PUBLIC key (PEM), shown with a copy button.
2. In the Revolut X web app, open API keys, add a key and paste that public key. Revolut X
   returns a 64-character API key.
3. **Save API key.** Paste it into the password field; it is stored in a 0600
   `credentials.json` beside the key and never sent back.

Status says connected only when both a key pair and an API key exist. Saving the API key, or
generating a new key pair, pauses trading; a new key pair also removes the saved API key
(Revolut X ties the API key to the old public key) and is refused while an order is
uncertain. **Disconnect** removes both files and pauses trading.

The Settings page shows a pending label on every button while it runs, a visible error line
for every failed call (with a fallback text when the server gives no reason) and, for a
disabled button, the reason it is disabled.

## Allocation and ledger

The bot trades only an allocation the owner syncs from the Revolut X account (Settings →
Sync, `POST /revolut-x/sync`): the available EUR and every coin that has an active EUR pair
(available units; reserved or staked units are left out). Cost basis for coins already in the
account restarts at their current bid. Syncing is refused while trading is on or an order is
uncertain, and trading cannot be enabled before the first sync.

The ledger (`ledger.json`, beside the credentials) is in EUR and keyed by the plain coin
(`BTC`). The same rules as the other bots apply: below 1000 of total bot equity no single coin
may exceed 40% of equity; new exposure stops at 2 total equity (a floor, not cumulative
losses); from 1000 the 50% cash reserve, three positions and one-sixth position limit apply
too. Equity is cash plus each position at the live best bid. A buy must also leave 1% of its
value in cash for the fee. At most ten orders a minute and one order per call.

## Orders

* Only active `<COIN>-EUR` pairs can be traded; any other quote currency is refused before
  anything is sent. Stablecoins and wrapped or tokenized assets are never bought and are left
  out of `coins`. Crypto trades around the clock: there is no market-hours rule.
* A spread above 1% is refused. `preview` sizes and prices an order from the live public book
  without placing it (it needs no key), walks the book for the estimated average price and
  impact, and reports `taker_fee_pct`, `fee_source` (`estimate` or `observed`), a `fee_note`
  and `estimated_round_trip_pct` (the spread, the impact, the taker fee each way and the
  cent-rounding allowance of 0.02 EUR per round trip as a share of the order, with the parts in
  `round_trip_components_pct`, `rounding_allowance_eur` and `rounding_allowance_pct`).
* A buy whose estimated round-trip cost is above 1.0% is refused before anything is sent unless
  the owner's API route passes `allow_costly_buy` (see the verified section above); a sale is
  never refused for its cost.
* `order` places an immediate-or-cancel limit priced `max_slippage_pct` (default 0.3, at most
  2) through the touch of the live book. Price rounds to the pair's `quote_step` (up for a buy,
  down for a sell) and quantity down to `base_step`; orders below `min_order_size` or
  `min_order_size_quote` or above the maximums are refused locally. A buy's amount is the EUR
  to spend; a sell's amount is coin units. A UUID `client_order_id` is generated per order.
* The fill is read back with `GET /orders/{id}` (`filled_quantity`, `filled_amount`,
  `total_fee`, `fee_currency`) and `GET /orders/fills/{id}` (trade count, maker or taker, value
  and the real average price); a partial fill moves the ledger by what executed and a rejected
  or unfilled order by nothing. The order object decides the quantity and the EUR amounts; the
  fills give the user-facing `average_fill_price` (the order's own is cent-rounded and is kept as
  `reported_average_fill_price`) when they add up to the filled quantity.
* POSTs are never retried. A new order is written to the ledger as uncertain before it is sent.

### Definite versus uncertain

Every HTTP 4xx answer to the placement (400, 401, 403, 409 and 429, whose `Retry-After` is in
milliseconds) is **definite**: the request was refused at the door, the uncertain entry is
cleared and trading stays as it was. A local rate-limit refusal is definite too (nothing was
sent). HTTP 5xx, a timeout, a transport error or an unreadable answer is **uncertain**: it may
have created the order, so trading pauses until `reconcile`. An error from the read-back after
the placement succeeded is never treated as a rejection: the order exists, so the entry stays
uncertain (with its `venue_order_id`).

`reconcile` reads the order by id when known, otherwise looks for the `client_order_id` in
`GET /orders/active` and then in `GET /orders/historical` (a window starting five minutes
before the attempt, up to four pages by `metadata.next_cursor`). A found final order is
applied to the ledger; a still-open one is left alone; an order Revolut X never listed is
cleared only after a minute. Resuming trading after reconciliation remains an owner action.

## Signing

Every private call carries `X-Revx-API-Key`, `X-Revx-Timestamp` (Unix milliseconds) and
`X-Revx-Signature`, the base64 Ed25519 signature over the concatenation, with no separators,
of the timestamp, the uppercase method, the path from `/api`, the query string (no `?`) and
the minified JSON body that is actually sent. An HTTP 409 "Request timestamp is in the
future" means this server's clock is ahead.

## Agents and scorecard

The `revolut_x` MCP tool exposes `status`, `valuation`, `accounts`, `history`, `coins`,
`candles`, `book`, `preview`, `order`, `reconcile`, `predict` and `scorecard`; credentials, the
signing key, sync and enable/disable are not MCP actions. `order` is classified
dangerous/mutating (attended sessions confirm it, a rehearsal refuses it); the other actions
are read-only except `reconcile`, which settles the ledger.

The crew is `revolut_x_trader` (loop `intraday`, every 2 minutes), `revolut_x_analyst` and
`revolut_x_risk_reviewer`, with the same bounded-intent protocol as the other crews (analyst,
then preview, then reviewer, then a fresh preview, then one order). The analyst and reviewer
have only the journal; the trader reads the candles and passes the analyst a summary. The
style is the Crypto.com crew's tuned intraday style with the cost taken from the preview: a
target of at least 1.5 times the preview's `estimated_round_trip_pct` and at least 0.8%, gross
reward-to-risk of at least 1.5:1, a 1 to 24 hour window, entries of 15-25% of equity but of at
least 10 EUR when equity allows (a smaller entry is too expensive after cent rounding, so the crew
passes; under about 25 EUR of equity the venue is not worth day-trading), a cost
bound at least 0.15 percentage points above the preview, every held coin reviewed for an exit
each tick, and no new entries on a UTC day when the bot's equity is 5% below that day's
opening equity. Exits are never blocked on paperwork: an incomplete review of an exit is
re-asked with the missing bounds filled in. No fee is hard-coded in a prompt. The loop starts
only by an explicit owner action.

The live scorecard (`predict` / `scorecard`) keeps its own database
(`CONDOR_DATA_DIR/revolut_x/<user>/scorecard.db`, venue `revolut_x`). It scores at
`2 x taker fee + the median spread` of the top listed coins, the taker fee being the observed
one when a real fill has shown it and the published estimate otherwise; if no market answer is
available it uses 0.35%.

## Portfolio dashboard

Revolut X is one entry in the native-venue registry (`condor/native_venues.py`: venue
`revolut_x`, service `condor.revolut_x`, crew agent `revolut_x_trader`), so it shares the
path the other native accounts use (`condor/native_accounts.py`). The Portfolio page adds the
authenticated user's Revolut X balances (connector "Revolut X"), after reading the shared
Hummingbot cache and never written into it. Hummingbot has no Revolut X connector, so there
is no double-count rule. Euros and coins are valued at the last traded price and converted to
USD through the USDC/EUR rate (Kraken's EUR/USD when that pair is missing); assets without a
price are listed with zero value and named in a note. With no EUR/USD rate at all the account
is not valued (a note says so) rather than recorded as zero. The bot ledger stays in EUR;
every account value, chart sample and equity change is in USD. A value sample is recorded for
the Portfolio chart (`condor/portfolio_history.py`, source "Revolut X"), the Revolut X loop
card's PnL is the change in that USD account equity, net of the deposits and withdrawals
recorded in `condor/native_flows.py` (see `docs/native-flows.md`; with none recorded and no
history read it is the plain equity change and says so), and the chat tool `native_portfolio` includes the account. The whole-account balance
does not increase the bot's allocation.

## Known limits

Only the synced allocation and its confirmed proceeds are tracked; outside trades and
transfers are not imported. `history` shows the final orders of the last seven days. Public
data is limited to one request a second, so a preview takes a few seconds on a cold cache.
Delistings, staking and corporate actions are not modelled.
