# Revolut X (live trading, EUR)

Live trading of crypto on the Revolut X exchange through its REST API, in euros, within an
allocation the owner syncs from the Revolut X account. It mirrors the Kraken xStocks service
(`condor/kraken_xstocks.py`) and the Crypto.com Exchange service
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
  verified; that Revolut X accepts the Condor request built from it has not been.
* The documented shapes (not exercised against an account): the order object (`id`,
  `client_order_id`, `symbol`, `side`, `type`, `quantity`, `filled_quantity`,
  `leaves_quantity`, `amount`, `filled_amount`, `price`, `average_fill_price`, `total_fee`,
  `fee_currency`, `status`, `reject_reason`, `time_in_force`, `created_date`,
  `updated_date`), the placement answer (`data.venue_order_id`, `client_order_id`, `state`),
  balances (`currency`, `available`, `reserved`, `staked`, `total`), fills (`p`, `q`, `im`
  for maker, `oid`, `s`, `tdt`) and errors (`error_id`, `message`, `timestamp`).

**NOT verified against a real account** until the first tiny real order (nothing here has
ever talked to a private Revolut X endpoint; every test uses fakes):

* The exact place-order body. Sent: `{"client_order_id": <uuid>, "symbol": "BTC-EUR", "side":
  "buy"|"sell", "order_configuration": {"limit": {"base_size": ..., "price": ...,
  "time_in_force": "ioc", "execution_instructions": ["allow_taker"]}}}`. The spec lists
  `time_in_force` (`gtc`, `ioc`) and `execution_instructions` on the limit configuration and
  `side` in lowercase, but the placement schema was only read through a summarising fetch, and
  the spec's own Node example posts to a different path with `BTC/USD` and `qty`. If
  Revolut X refuses the body the answer is HTTP 400 and nothing is placed.
* The symbol spelling. Paths and order bodies use the hyphen (`BTC-EUR`); responses and
  the configuration use the slash (`BTC/EUR`). The service writes the hyphen and accepts
  both when reading. If the order endpoint wants the slash, the first order is refused.
* Which status an immediate-or-cancel order ends in. The statuses are `pending_new`, `new`,
  `partially_filled`, `filled`, `cancelled`, `rejected`, `replaced`. The service treats
  `filled`, `cancelled`, `rejected` and `replaced` as final; a `partially_filled` order that
  is no longer in `GET /orders/active` is also treated as final (an IOC order cannot rest).
  Anything else after ten polls stays an uncertain order.
* The fee fields. The order object has `total_fee` and `fee_currency`, and the fills have no
  fee. The service reads the order's fee; it has never seen it filled in. A missing fee, a fee
  in a currency other than EUR or the coin, or a **reported zero on a filled order** is not
  trusted: the published taker rate is charged to the ledger as an **estimate** and the order
  is marked `fee_source: "estimate"`. A reported positive fee is used and marked `reported`,
  and the rate it implies is remembered (`observed_taker_fee_pct` in the ledger, kept across
  syncs) and used by `preview` from then on.
* Whether the owner's Revolut Premium plan changes the taker fee. There is no fee endpoint, so
  the published EEA rates (maker 0%, taker 0.09%) are used only as a labelled estimate until
  the first real fill shows the truth. A true zero fee would be read as an estimate too (the
  ledger errs on the side of less cash); check the first fill by hand.
* API key permissions and IP rules. The spec shows an HTTP 401 "API key can only be used for
  authentication from whitelisted IP", so the key may be IP-restricted: register this
  server's public IP if Revolut X offers it, and give the key trading but no withdrawal
  rights if it distinguishes them.
* How the signature treats a query string that needs percent-encoding. The service signs the
  query exactly as it sends it (`urllib.parse.urlencode`). Order lookups avoid awkward
  characters, but the `cursor` of a paginated list is passed through as received.
* The shape of the answers beyond the documented examples: `GET /orders/{id}` and the
  active and historical lists (`data` plus `metadata.next_cursor`), the `symbols` filter
  format (not used: lists are read unfiltered), and whether an unknown order id answers 404
  (a 404 is read as "unknown").
* Rate limits as published (reads 100 a second and 1,000 a minute, place order 10 a second and
  1,000 a day, cancel 100 a second, 300 active orders). Condor limits itself to 600 private
  reads a minute and 900 orders a day.
* There is no sandbox. The first order is the test.

**The first order must be tiny**: the minimum order is 0.1 EUR (`min_order_size_quote`).
Place it by hand (Settings sync and enable, then one `preview` and one `order` of about 1 EUR),
look at the order in the Revolut X app, and compare it, its `total_fee` and its fills with the
ledger before enabling any loop. If the placement body, the read-back or the fee fields
differ from what is described here, fix them before going further.

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
  and `estimated_round_trip_pct` (spread plus the taker fee each way).
* `order` places an immediate-or-cancel limit priced `max_slippage_pct` (default 0.3, at most
  2) through the touch of the live book. Price rounds to the pair's `quote_step` (up for a buy,
  down for a sell) and quantity down to `base_step`; orders below `min_order_size` or
  `min_order_size_quote` or above the maximums are refused locally. A buy's amount is the EUR
  to spend; a sell's amount is coin units. A UUID `client_order_id` is generated per order.
* The fill is read back with `GET /orders/{id}` (`filled_quantity`, `average_fill_price`,
  `filled_amount`, `total_fee`, `fee_currency`) and, as evidence only, `GET
  /orders/fills/{id}` (trade count, maker or taker); a partial fill moves the ledger by what
  executed and a rejected or unfilled order by nothing. The order object decides, never the
  fills.
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
style is the Exchange crew's tuned intraday style with the cost taken from the preview: a
target of at least 1.5 times the preview's `estimated_round_trip_pct` and at least 0.8%, gross
reward-to-risk of at least 1.5:1, a 1 to 24 hour window, entries of 15-25% of equity, a cost
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
card's PnL is the change in that USD account equity (deposits and withdrawals are not
tracked), and the chat tool `native_portfolio` includes the account. The whole-account balance
does not increase the bot's allocation.

## Known limits

Only the synced allocation and its confirmed proceeds are tracked; outside trades and
transfers are not imported. `history` shows the final orders of the last seven days. Public
data is limited to one request a second, so a preview takes a few seconds on a cold cache.
Delistings, staking and corporate actions are not modelled.
