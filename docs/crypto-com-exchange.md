# Crypto.com and Claude

The container includes pinned Claude Code 2.1.311 and claude-agent-acp 0.97.0.
`HOME=/var/lib/condor` is the existing persistent, private state volume. Authenticate
as container UID 10001 with `claude auth login`; no API key or copied account token
is needed. Do not embed credentials in Compose, an image or Git.

Settings → Crypto.com accepts a Crypto.com API key and secret, created under
User Center → API on the Crypto.com site. Create the key with trading
permission only (no withdrawal) and restrict it to this server's IP address. Keys are
stored in a per-user 0600 file under `CONDOR_DATA_DIR/crypto_com_exchange`, never sent
back to the UI or the model. Saving or replacing them pauses trading. The environment
can be `production` (`api.crypto.com`) or `uat` (the Crypto.com sandbox, with its own
keys), so the whole path can be tried without real money.

This is the Exchange, not the Crypto.com App. Orders go to the public order book, so a
buy followed by a sell loses the spread plus the fee (hundredths of a percent on liquid
coins, measured 0.02-0.19% on the first six coins), where the App's quotations cost
about 5% per round trip. Funds move between the App and the Exchange by a transfer the
owner makes; Condor never moves money.

## Allocation and ledger

The bot trades only an allocation the owner syncs from the Exchange account
(Settings → Sync, `POST /crypto-exchange/sync`): the quote-currency cash (USD by
default; USDT, USDC or EUR when the account holds those) and every coin with a pair in
that currency. Cost basis for coins already in the account restarts at their current
bid. Syncing is refused while trading is on or an order is uncertain, and trading
cannot be enabled before the first sync.

The ledger is in the quote currency. Below `GUARD_THRESHOLD` (1000) of total bot equity
the bot may use all of its money; only two rules stay: no single asset above 40% of
equity (`MAX_ASSET_FRACTION`) and a stop of new exposure at 2 total equity (`FLOOR`;
an equity floor, not cumulative losses). From 1000 the 50% cash reserve, three positions
and one-sixth position limit apply as well. Equity is cash plus each position at the
live best bid.

## Orders

`preview` sizes and prices an order from the live book without placing it. `order`
places an immediate-or-cancel limit priced `max_slippage_pct` (default 0.3, at most 2)
through the touch, so a fast market cannot fill it at a worse price than accepted.
A buy's amount is the quote currency to spend; a sell's amount is coin units. Both
round down to the pair's lot size (SHIB trades in lots of 10,000), so a remainder
smaller than one lot stays in the ledger. A pair whose spread is above 1% is refused.

The fill is read from the Exchange order detail (cumulative quantity, value and fee),
not assumed. A fee paid in the coin or the quote currency adjusts the ledger; a fee in
another currency (for example CRO) is recorded but not applied. POSTs are never
retried. A new order is written to the ledger as uncertain before it is sent, with a
`client_oid` the Exchange rejects if reused; if the outcome is unknown, trading pauses
until `reconcile` reads the order by that id. An order the Exchange never saw is
cleared only after a minute and a check of recent history. Resuming after
reconciliation remains an operator action.

Requests are signed with HMAC-SHA256 over the method, id, API key, the flattened
parameters and the nonce, as the Exchange documents. The Exchange allows 15 requests
per 100 ms for order methods; Condor limits itself to 90 per minute.

## Maker mode

Optional and off by default. The account's measured fees are maker 0.25% and taker
0.50% (`private/get-fee-rate`), so a resting order saves up to 0.25 percentage points a
side, plus the spread it does not cross, but it may not fill. It is a separate mode on
`preview` and `order`; the taker path above is unchanged.

**Switch.** The owner turns it on in the Exchange panel (`PUT /crypto-exchange/maker-mode`,
stored in `maker_settings.json` beside the ledger, default off). It is not an MCP action
or tool argument, so the crew cannot enable it. A maker `order` while it is off is refused
before anything is placed; `preview` still works and reports `maker_mode_enabled`.

**Arguments.** `mode` = `taker` (default) or `maker`; `purpose` = `entry` (a buy),
`target_exit` (a sell) or `stop_exit`; `maker_wait_sec` = 5 to 75 whole seconds (default
60). In maker mode `purpose` is required, and `stop_exit` is refused for both `preview`
and `order` with a message to use taker mode: a stop or invalidation exit must complete.
`maker_wait_sec` with taker mode is refused.

**What an order does.**

1. Reads the live book and rests a POST_ONLY limit order at the best bid (buy, rounded
   down to the tick) or the best ask (sell, rounded up), never through the touch. A
   locked or crossed book, a spread above 1% or a lot below the minimum is refused.
2. The same guards as a taker order apply to the resting notional (single-asset cap,
   reserve, positions, floor, balance, ten orders a minute). The ledger is written
   uncertain with the `client_oid` before the send, as for a taker order.
3. Polls `get-order-detail` every 2.5 s until the order is FILLED, cancelled, rejected
   or expired, or the wait ends. The wait is also capped so placement, wait, cancel and
   the final read finish inside the 90 s MCP budget (72 s after the call started).
   Poll errors are tolerated; three in a row stop the wait.
4. At the deadline it sends `private/cancel-order` (retried once, a cancel is
   idempotent) and reads the order again until it is final.
5. Returns `outcome` (`filled`, `partial` or `unfilled`) and a `maker` block: resting
   price, ordered, filled and `unfilled_quantity`, average price, fee, fee currency,
   `effective_fee_pct` and `fee_class` (maker, taker, other or unknown, judged against
   the account's rates), wait and time to fill. `status` stays the service status object
   it is for taker orders; the maker result is `outcome`.

An `unfilled` order is a normal result: no trade happened and the ledger (cash,
positions, order list) is unchanged. A partial fill updates the ledger like a partial
taker fill; the rest is cancelled and is not retried.

**Refusals and uncertainty.** A post-only order that the Exchange refuses at placement
(documented reason code 43005, `POST_ONLY_REJ`) or reports REJECTED with nothing filled
is a definite refusal: nothing was placed, the ledger is unchanged, and it is never
retried as a taker order; the caller decides. 429 and other 4xx answers to the create are
definite refusals; 5xx answers, a timeout or an unreadable reply are uncertain. If the
placement is uncertain, if the cancel cannot be confirmed (two failures), or if the final
state cannot be read, the write-before-send record stays: trading is paused and `reconcile`
reads the order by `client_oid`; for a maker order that is still open it cancels it first,
reads the final state, applies any fill and logs the attempt. A cancel that answers
`NO_ACTIVE_ORDER` (316) means the order had already ended, and the final read decides.
If the whole call is cut off while the order rests (a client or tool timeout), the same
record keeps trading paused and `reconcile` cancels the order.

**Preview.** `resting_price`, `quantity`, `notional`, `maker_fee_pct` and its source
(`exchange_get_fee_rate`, `default_estimate_not_read` when the rate could not be read, or
`owner_override`), `taker_fee_pct`, and three round trips in percent, all fees only and
paid only if the order fills: `maker_taker_round_trip_pct` (also
`estimated_round_trip_pct`: a maker entry and a taker exit pay no spread at the same
touch), `maker_maker_round_trip_pct` and `taker_taker_round_trip_pct` (spread plus two
taker fees, the old estimate). The saving is before adverse selection: a resting order
fills most when the price moves against it. The book gives the queue at the touch
(`touch_queue_quantity` and `touch_queue_value`); there is no trade-flow estimate, so the
preview says the fill probability is unknown.

**Per-pair maker fee override.** `ExchangeService.set_maker_fee_override(user_id, pair,
pct)` stores a maker rate for one pair (percent, 0 to 1) in `maker_settings.json`, for a
promotion such as a zero-fee token once its terms are read. It changes only the preview
estimate and the `fee_class` label. It has no route and no MCP action, and nothing assumes
it exists. The ledger always books the fee the Exchange reports.

**Measurement.** Every attempt is appended to `maker_log.json` beside the ledger (time,
pair, side, purpose, resting price, wait, outcome, filled fraction, fee, fee in quote,
taker rate then, time to fill; the last 1000 are kept). `GET /crypto-exchange/maker-stats`
and the MCP action `maker_stats` (read-only) return the attempts, outcomes, fill rate
(complete fills over placed and resolved orders), any-fill rate, average filled fraction,
average seconds to fill, average fee percent, `fee_saved_vs_taker`, the notional left
unfilled, a split by purpose and the last ten attempts. An attempt logged as uncertain is
replaced by its reconciled row. Fee saved is not the whole story: judge after some days
whether it beats the moves missed by unfilled orders. The Trade page shows a compact block.

**Crew.** The trader prompt uses maker mode only for entries and target exits with a window
of at least an hour whose maker round trip, times the usual 1.3 multiple, is at most the
target; the 1.6% target bar is unchanged. Stops, rotation legs and anything that must
complete stay taker. An unfilled or partial maker order is never retried as a taker order
in the same tick. The reviewer's bounded intent carries an optional `mode`, and the analyst
says whether an entry is patient or urgent. These are prompts, not server authorization.

### Verified and not verified

Read on 29 Oct 2026 from the public Exchange API v1 documentation
(`exchange-developer.crypto.com`, formerly `exchange-docs.crypto.com`) and ccxt's
`cryptocom.py`. Nothing below was tested against the live Exchange or the UAT sandbox, no
private endpoint was called, and the tests use fakes.

Verified in the documents:

* `private/create-order` takes `instrument_name`, `side`, `type` (LIMIT), `price`,
  `quantity`, `client_oid` (up to 36 characters), `time_in_force` (`GOOD_TILL_CANCEL`,
  `IMMEDIATE_OR_CANCEL`, `FILL_OR_KILL`) and `exec_inst`, an array that may hold
  `POST_ONLY` or `SMART_POST_ONLY` (mutually exclusive). ccxt sends post-only as
  `exec_inst: ["POST_ONLY"]` with `GOOD_TILL_CANCEL`, which is what Condor sends.
* `create-order` and `cancel-order` are asynchronous: the reply only confirms the request.
  `cancel-order` takes `order_id` or `client_oid`.
* Reason code 43005 `POST_ONLY_REJ` ("Rejected POST_ONLY create-order request"), code 316
  `NO_ACTIVE_ORDER`, 42901 `TOO_MANY_REQUESTS` on HTTP 429, 204 `DUPLICATE_CLORDID`.
* `get-order-detail` returns `status` (PENDING, NEW, ACTIVE, FILLED, CANCELED, REJECTED,
  EXPIRED), `quantity`, `limit_price`, `avg_price`, `cumulative_quantity`,
  `cumulative_value`, `cumulative_fee`, `fee_instrument_name`, `exec_inst`, `reason`,
  `maker_fee_rate` and `taker_fee_rate`.
* Rate limits: 15 requests per 100 ms each for create, cancel and cancel-all; 30 per
  100 ms for get-order-detail. A maker wait uses about 30 requests of the
  module's own 90 a minute budget.
* `private/get-fee-rate` reports the effective spot maker and taker rate in basis points
  (the owner's account: 25 and 50, read earlier).

Not verified:

* Whether a post-only order that would cross is refused synchronously (code 43005) or
  accepted and then ended as REJECTED or CANCELED. The documented text for 43005 only
  names a time-in-force mismatch, not a crossing. Condor handles both: 43005 on the create
  and a REJECTED order with nothing filled are definite refusals; a zero-fill CANCELED or
  EXPIRED order before the deadline is reported as `unfilled` with `cancelled_by:
  exchange`.
* The HTTP status of 43005 (the table lists it under 500, which Condor treats as a definite
  refusal for this one code only), and the exact reply to a cancel of an order that has
  just filled (assumed code 316).
* That a partly filled resting order reads `ACTIVE` with a non-zero `cumulative_quantity`,
  and `CANCELED` with its partial fill after the cancel.
* How long the cancel takes to show; Condor reads up to 10 times, 1 s apart.
* The fee on a maker fill: its currency (base, quote or CRO), whether `cumulative_fee` is
  reported for it, and whether it matches `effective_spot_maker_rate_bps`. A fee in a
  currency other than the coin or the quote is recorded but not applied to the ledger, as
  for taker orders.
* Whether `get-order-detail` answers at once after the create (a "not found" is tolerated
  while polling, but not after the cancel).
* Minimum order size or notional for post-only orders, and the book's third column (order
  count) and exact size semantics at a level.
* Self-trade prevention (code 43012) with a resting order.
* The "Zero-Fee Token" promotion: not enabled, its terms unread; nothing assumes it.
* Fill probability: no trade-flow or queue-position data is read.

**First live use.** Nothing has been run live. The first live maker order must be a tiny
manual one the owner approves: the owner switches maker mode on, a single minimum-size
order is placed through the owner's own API call, and its `maker` block, the Exchange order
history and `maker_stats` are checked against the points above before the crew is allowed
to use it.

## Market data

`coins` lists the top pairs by 24-hour volume in the quote currency with bid, ask,
spread and lot size (held coins are always included); `candles` returns OHLCV
(1m, 5m, 15m, 30m, 1h, 2h, 4h, 12h, 1D, 7D, 14D, 1M); `book` returns the top levels.
These are public endpoints. Only 7 pairs are quoted in EUR (ADA, BTC, DOGE, ETH, PAXG,
SOL, XRP); most are USD or USDT.

## Call scorecard

Before trusting the trader with money, its calls are scored against real prices. The
scorecard scores the calls the trader logs, not the trades that were actually placed: a
call can be scored as hitting its target even though the order was refused or never sent.
Two things are stored in `scorecard.db` beside the ledger:

* **Prices**: the listed price of every coin the coin list returns, at most once per
  4 minutes per coin, kept 90 days.
* **Calls**: logged by the trader with `crypto_com_exchange(action="predict",
  symbol="SOL", verdict="trade"|"pass", target_pct, stop_pct, horizon_hours, note)`.
  One open call per coin. `pass` records the best near-miss it declined. Logging
  never places an order.

Outcomes are not stored; `crypto_com_exchange(action="scorecard")` recomputes them
from the prices: *target* if the price reaches it before the stop, *stopped* if the
stop comes first, otherwise the last price inside the window; all net of
`ROUND_TRIP_COST_PCT` (1.2%: the tier-1 taker fee of 0.5% each way plus about 0.2% of spread). The report also shows the average of all listed coins over the same
windows as a baseline, and what each cost multiple (1x, 1.5x, 2x, 3x) would have
returned. About 30 resolved calls are needed before the numbers mean anything.

## Agents

The shared `crypto_com_exchange` MCP tool is mounted through existing tool profiles.
`order` is classified as dangerous/mutating, so attended confirmations and dry-run
rejection still work. Credentials, sync and enable/disable are not MCP actions. The
shipped Crypto.com Trader / Daily Spot loop evaluates every 2 minutes
(`frequency_sec: 120` in its `loop.md`, see "Loop cadence" in `native-crypto-crew.md`), can
choose any listed pair, and uses the same enforced order path. It starts only by an
explicit operator action, then may resume after a restart.

Known limits: only the synced allocation and its confirmed proceeds are tracked;
outside trades and transfers are not imported into the ledger. Reconciliation reads
the latest 50 orders. Fee schedules, minimum order sizes and the order-detail field
names must be checked against the actual account (use `uat` first) before enabling
autonomous execution; fixtures are not proof of a working live association or a
profitable strategy.

## Rotation and idle ticks

Observed on 29 Oct: with cash 0.30 USD of about 48 equity (below the 5 minimum entry) and
five coins held, every tick ended in "no trade" while still paying for the analyst, the
reviewer and the scorecard. Two prompt-only changes (the trader `AGENT.md` and the
`daily_spot` loop; no engine, ledger or guard change, and the configured frequency is
untouched):

* **Cash rotation.** With cash below the minimum entry and an actionable analyst candidate
  that clears the usual bar (target at least 1.6%, stop within two thirds of it, 1 to 24 h),
  the trader may sell the weakest eligible holding and buy the candidate. Eligible: worth
  at least 1 (dust ignored), and never one that is above its entry-implied target path and
  still inside its thesis window; prefer the smallest remaining expected move or an
  invalidated or stale thesis. It must pay for itself: the candidate's expected gross move
  has to exceed (sell cost + buy cost, each from a fresh preview, about one round trip at
  1.2%) x 1.3 PLUS the holding's own forgone expected move. One rotation at a time, at most
  one per 3 hours, never while the 5% daily brake is hit. Both legs are ordinary trades:
  bounded intent, reviewer approval, fresh preview inside the bounds. If the sell fills and
  the buy is refused, out of bounds, expired or uncertain, the trader journals and notifies
  and does not chase; the cash stays cash. Stop and invalidation exits are never blocked by
  these rules.
* **Cheap idle ticks.** When status is connected, enabled and not uncertain, cash is below
  the minimum entry, no holding is within 25% of the way to its invalidation level (bid -
  invalidation <= 25% of entry - invalidation), none has hit its target or is stale, and a
  full tick ran less than 60 minutes ago, the tick only reads valuation and the held coins'
  bids, compares them with the invalidation levels, targets and the 5% daily brake, and
  writes one journal line. No analyst, reviewer, previews or scorecard calls. Any failed
  check turns it into a full tick from step 1. At least once an hour the tick is a full one
  (analyst, scorecard call, rotation proposal), so the call scorecard keeps running; with
  120 s ticks that is about 1 full tick in 30.

Nothing here has been run against a live account; the prompts are a contract the model
follows, not server authorization. Native guards stay authoritative, and the tests
(`tests/test_crypto_com_crew.py`) pin the wording, not the model's behaviour.

## Portfolio dashboard

The main Portfolio page includes the authenticated user's Exchange balances
automatically, even while trading is paused. They are added after reading the shared
Hummingbot cache and never written into it. Balances are valued in USD at the last
traded price (EUR through BTC_USD / BTC_EUR); a coin with no USD price is listed with
zero value and named in a note. The whole-account balance does not increase the bot's
allocation.

The packaged runtime disables automatic `uv` synchronization for MCP children as well
as the parent process. Otherwise the default development group is resolved inside an
offline production image and both stdio connections close before they can expose any
tools. The container smoke check initializes and lists tools on both actual MCP
subprocesses with networking disabled, without calling an account or placing an order.
