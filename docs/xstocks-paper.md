# xStocks paper account (Kraken public data)

A paper-only twin of the live Kraken xStocks crew. It needs no Kraken account, no API
key and no money: Condor reads Kraken's public market data, the paper crew places
SIMULATED orders against a simulated USD account, and the account's equity is measured
the same way the live crews' is. Nothing here can trade. The simulated account exists so
the paper crew's loop card and the Trade page show a real (simulated) PnL, and so the
paper crew can be compared with the live crew (`kraken_xstocks_trader`, see
`docs/kraken-xstocks.md`).

## What is simulated

Per user, in `CONDOR_DATA_DIR/xstocks_paper/<user>/ledger.json` (the scorecard database
sits beside it):

* **Cash and positions.** The account opens with 100 USD (`START_CASH` in
  `condor/xstocks_paper_ledger.py`) the first time it is valued or ordered on, or when
  the owner opens it. Positions are kept as units and cost, exactly as the live ledger
  does, keyed by the plain ticker (`AAPL`; Kraken's own names are `AAPLx` and `AAPLxUSD`).
* **Orders and fills.** Orders are immediate-or-cancel limits priced through the touch,
  like the live module's, but filled deterministically from a snapshot of the LIVE public
  Kraken order book (`Depth`):
  1. The order is sized exactly as the live module sizes it (it calls the live module's
     own `_plan`): a buy's `amount` is the USD to spend, a sell's is the number of stock
     units (fractions allowed); the limit is `max_slippage_pct` (default 0.3, at most 2)
     through the ask or bid, rounded to the pair's tick; the quantity is rounded down to
     the pair's lot step from `AssetPairs`.
  2. The book is walked from the touch outward. A level is taken while its price, after the
     slippage allowance, is inside the limit price. A thin book therefore gives a
     **partial fill**: the order ends `canceled` with the quantity that was filled, as an
     IOC order does on Kraken. A book with nothing inside the limit fills nothing (still
     recorded, nothing paid).
  3. Each fill pays the **taker fee of 0.08%** of its value, in USD (`TAKER_FEE_PCT`), and
     an adverse **slippage allowance** on the fill price, 0.02% per side by default
     (`CONDOR_XSTOCKS_PAPER_SLIPPAGE_PCT`, 0 to 1). Both are recorded on the order.
* **Guards, the same as the live crew's** (called from `condor/kraken_xstocks.py`, not
  copied): orders only while the regular US session is open (`us_market_open()`: 09:30-16:00
  New York, Monday to Friday; holidays are not modelled), only online `<TICKER>xUSD` pairs,
  a spread of at most 1%, the pair's minimum volume and cost, an extra **0.5 USD minimum
  order**, a 1% fee headroom on cash for a buy, at most ten orders a minute, one order per
  call, and capital guards: below 1000 of equity a single stock may take at most 40% of
  equity; from 1000 the half-equity cash reserve, three positions and one-sixth-equity new
  position apply; at or below an equity of 2 USD a buy is refused and the account is paused
  until the owner resets it. A sale cannot exceed the units held.
* **Idempotency.** `order` takes an optional `client_id` (1-64 of letters, digits, `.`, `_`,
  `-`). The same id again returns the recorded order (`duplicate: true`) and changes
  nothing. There is no "uncertain" order state, because nothing is ever sent.
* **Equity** is cash plus every position at the best bid (the price a sale would meet
  first), in USD, read from Kraken's public ticker.

The loop card of the paper crew reads this equity from the value history (the recorder
samples it every five minutes and every valuation records one too) and shows the same
"change in account equity" figure as the other native crews, with the hover starting
"Paper test only: simulated account, no real money."

## Deposits and resets

The account has **no deposits or withdrawals, by design**: nothing crosses its boundary, so
its PnL needs no flows (`condor/native_flows.py` refuses the paper venue). The only thing
that moves its equity without trading is the owner's **reset**.

`POST /api/v1/xstocks-paper/reset` (admin only; the Trade page shows an "Open paper
account" or "Reset paper account" button to admins, with a confirmation step and visible
pending and error text) archives the ledger to `xstocks_paper/<user>/archive/` and starts
again from 100 USD. The reset is written to `resets.json` with the equity before and
after, and the PnL maths subtracts that jump (`adjustments()`), so a reset is a new
baseline and never reads as a gain or a loss. If the market cannot be read at reset time
the "before" equity falls back to cash plus position cost and the entry is marked
`estimated`. Resetting is **not** an MCP action: the `xstocks_paper` tool has no `reset`.

## Paper money is never mixed with real money

The venue is flagged `paper=True` in `condor/native_venues.py`. `account_venues()` (real
accounts only) leaves it out; `ledger_venues()` includes it.

* The Portfolio page lists "Kraken xStocks (paper)" as a connector with a "Simulated - not
  in total" badge and `paper: true`; the route sums `total_usd` over real connectors only,
  and the page leaves it out of the token count, top holdings and allocation shares.
* Its value-history source is recorded under its own label, and
  `portfolio_history.series()` omits it unless asked (`include_paper=True`); `merge()` drops
  it as well, so the Portfolio Evolution chart is real money only.
* `native_portfolio` (and `GET /portfolio/native`) lists it with `paper: true` and a note
  and keeps it out of `total_usd`.

## Tool and routes

`xstocks_paper(action=...)`: `status`, `valuation`, `history`, `coins`, `candles`,
`preview`, `order`, `predict`, `scorecard`. `preview` sizes an order, walks the live book
and returns the simulated fill, the fee, the estimated round trip (spread plus fee and
slippage allowance on both legs), the book depth check and a `blocked_reason` when the
order would be refused; it writes nothing. `order` is state-changing but simulated: it is
recorded by the action log (`condor/runtime/danger.py`) like any order but needs no human
confirmation, which `kraken_xstocks` orders do in attended sessions.

REST (`/api/v1/xstocks-paper`): `GET status | coins | candles | accounts | valuation |
history | scorecard`, `POST preview | order | predict | reset`. `status` merges the public
service's fields with the account (`connected` means the account has been opened).

## Scorecard (paper calls)

`predict` still logs one paper call (`trade` for a call the agent would enter, `pass` for a
declined near-miss) and the scorecard re-scores it against later prices after a round-trip
cost, in `CONDOR_DATA_DIR/xstocks_paper/<user>/scorecard.db`. That cost is still the
conservative `ROUND_TRIP_COST_PCT` of 0.85%, not the fill model's roughly 0.26%: the
scorecard measures whether calls beat a pessimistic bar; the ledger measures what the
orders actually earned. About 30 resolved calls are needed before the scorecard means
anything.

## Agent

`xstocks_paper_trader` runs the `paper_session` loop every 5 minutes (the live crew's
cadence) while the US session is open; outside it, it journals one line and stops. It is the
analyst, risk reviewer and executor in one, with no reviewer: it applies the live crew's
rules (target at least 1.5 times the preview's round trip and at least 1.0%, stop within two
thirds of the target, horizon before the close, entries of 15-25% of equity, exits on
target, invalidation or a stale thesis, a 5% daily brake, one new entry a tick), journals
its reasoning and bounds before ordering, takes a fresh preview right before the order and
orders once with a `client_id`. It starts only by an explicit owner action.

## Comparing with the live crew

Same market data, same sizing, same session and capital guards, same decision thresholds and
the same equity-change maths, so the paper loop card and the live one are comparable. What
differs, and what the paper account cannot know:

* **Fills.** The paper fill is taken from one book snapshot with no latency, no queue and no
  other participants reacting; a real IOC order can be filled or missed differently. The
  slippage allowance is a flat guess at that, not a measurement.
* **Fees.** 0.08% is the assumed Kraken xStocks taker fee, unverified here. The live crew
  reads the account's real fee from Kraken (and the owner may be on a zero-fee plan); change
  `TAKER_FEE_PCT` if the real number differs.
* **Account size.** 100 USD is not the live allocation; percentage results compare, dollar
  results do not, and lot and minimum-order rounding bites differently at different sizes.
* **Review.** The live crew has an independent analyst and risk reviewer; paper has one
  agent reviewing itself, which can make it bolder or less cautious than the live crew.
* **Holidays and hours.** Exchange holidays are not modelled, and the tokens also trade
  (thinly) outside the session, which the paper account never uses.
* **No real-account effects.** Eligibility, the real fee tier, rejected orders, funding and
  custody of a real Kraken account are separate questions this test does not answer.
