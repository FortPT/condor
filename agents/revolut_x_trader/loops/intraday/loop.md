---
name: Intraday
description: Sequential market analysis, bounded-intent risk review and fresh preview before a native Revolut X order
default_config:
  frequency_sec: 120
  tick_timeout_sec: 600
  execution_mode: loop
  total_amount_quote: 0
  restart_on_boot: true
  max_ticks: 0
default_trading_context: Revolut X only (EUR pairs, 24/7); read capital from the native ledger and run context, never scheduler budget; stop new exposure at 2 total bot equity. Analyst then bounded-intent review, then a fresh preview, then one order.
---

# Intraday

1. Call `revolut_x(action="status")`. Disconnected, disabled or uncertain means no order. Read
   history/reconcile uncertain submissions and notify the owner; never enable trading yourself.
   Read valuation, accounts, bot positions, history and journal. Native ledger and run context
   own capital, not generic scheduler defaults. Missing valuation blocks entries. At or below 2
   total bot equity, no new exposure. No outside holdings, leverage, margin, borrowing,
   transfers or withdrawals. At your first tick of the UTC day, journal the bot equity as the
   day's opening equity; if equity is 5% below that, make no new entries until 00:00 UTC and
   notify the owner (exits stay allowed).
2. Scan the market with `revolut_x(action="coins", keyword="")`: the most traded EUR pairs with
   bid, ask, spread, 24h euro volume, 24h change and range. Skip stablecoins, wrapped coins and
   tokenized assets (the list already leaves them out) and anything with a spread above 0.5% or
   too little 24h volume to trade the order size. Shortlist the 6 most promising new candidates
   by recent move, momentum, volume and range, rotate across ticks, and always include every
   coin the bot holds. For each shortlisted and held coin read
   `revolut_x(action="candles", symbol="<COIN>", timeframe="15m", count="96")` and, if useful,
   the 1-hour candles. Then call
   `delegate(action="ask", agent="revolut_x_analyst", task="Assess evidence for intraday entries or exits; return actionable or no_trade", context="<native allocation, bot positions, shortlist with Revolut X bid/ask/spread/24h euro volume/change/range, compact candle summaries, journal and timestamps>")`.
   Wait for its answer. Never start helper loops or detached workers. An unavailable analyst,
   error, timeout or missing answer means no order; journal and notify. A no_trade report ends
   the tick quietly after journaling. Selling a held coin to fund a better one is allowed when
   the new expected gain beats the held coin's own outlook plus both legs' costs. Review every
   held coin for an exit every tick: target reached, invalidation broken or thesis stale.
3. Call scorecard (it scores the calls, not the trades placed): after the analyst's report each tick, log ONE call for the best
   candidate with no open call using `revolut_x(action="predict", symbol="<COIN>", verdict="trade"|"pass", target_pct="...", stop_pct="...", horizon_hours="...", note="...")`.
   Use the analyst's target, invalidation and window. Read `action="scorecard"` first and never
   log numbers you do not expect. Logging places no order.
4. Only for an actionable proposal, take one preview with
   `revolut_x(action="preview", side="<buy|sell>", symbol="<COIN>", amount="<exact decimal>")`
   and record pair, quantity, limit price, bid, ask, spread, estimated impact, taker fee,
   fee_source, `estimated_round_trip_pct` and timestamp. It is evidence only. The round-trip cost
   comes from the preview, never from memory: the target must be at least 1.5 times that cost and
   at least 0.6%, with the stop no farther than two thirds of the target and a window of 1 to 24
   hours; otherwise no order, journal the near-miss. Apply the trend gate: while BTC and ETH are
   both down more than 2% over the last 24 hours, a new entry needs a confirmed reclaim (the
   candidate's latest 15-minute close above its last swing high and BTC's latest 1-hour candle
   closing up); a bounce that still makes lower highs is no entry, whatever the coin's own 24-hour
   gain, and exits are never blocked by it. Propose a bounded intent naming side, exact
   pair, requested and maximum amount, maximum slippage percent, a cost bound at least 0.15
   percentage points above the preview's round-trip cost, and a UTC validity deadline. Entries
   are roughly 15 to 25 percent of bot equity, sized so the preview's `rounding_allowance_pct`
   (Revolut X rounds EUR amounts and fees to the cent) stays small: prefer entries of at least
   10 EUR when equity allows, going above 25 percent of equity inside the 40% cap to get there,
   and pass on an entry that cannot reach 10 EUR (too expensive). A buy whose preview has
   `cost_guard_blocks_buy` true would be refused by the native service: do not propose it.
5. Call `delegate(action="ask", agent="revolut_x_risk_reviewer", task="Approve, reject or mark incomplete this bounded trade intent before the order", context="<market report; preview; the bounds; native status, cash, positions, equity, limits and valuation timestamp>")`.
   Analysis precedes review: consult sequentially and on demand. An unavailable reviewer, error,
   timeout, missing answer, reject or incomplete means NO ORDER: journal and notify. Require an
   explicit approve binding side, exact pair, requested amount, maximum amount, maximum slippage
   percent, permitted costs/spread, supported cost basis, UTC validity deadline and the
   allocation snapshot. Missing bounds or unknown cost basis means incomplete. An exit of a held
   coin (stop, invalidation or stale thesis) is never left waiting on paperwork: when the answer
   is incomplete, ask again with the missing bounds filled in from the preview and the headroom,
   and if it is incomplete twice escalate by re-asking with every bound written out explicitly,
   journal it and notify the owner; only an approve releases the order.
6. Recheck current native status, allocation and thesis after review. Changed
   intent/allocation, disconnected/disabled/uncertain state or expired approval means no order
   and new review. Otherwise take one fresh preview for the exact approved request. Verify
   side/pair/amount match, amount <= approved maximum, the round-trip cost inside the approved
   cost bound and the estimated impact within the approved costs, the book covers the order and
   the approval deadline is unexpired. A preview inside the bound places the order; only one above
   it, or a changed pair, side or amount, blocks it. Missing fields or out-of-bounds means no
   order; journal and notify, require new review next tick, never chase the market. Do not insert
   a slow consultation after this fresh preview.
7. Place the order once while within all bounds:
   `revolut_x(action="order", side="<buy|sell>", symbol="<COIN>", amount="<exact decimal>", max_slippage_pct="<approved>")`.
   A buy's amount is the EUR to spend; a sell's amount is coin units. Native guards still
   enforce enabled state, EUR pairs only, ledger allocation, the 40% asset cap below 1000 equity
   (or half-equity cash reserve, maximum three positions and one-sixth-equity new-position limit
   from 1000) and the 2 total-equity floor, not cumulative losses. Never bypass a refusal or
   enlarge an order. At most one new entry per tick; zero trades is valid.
8. Read history. Pending/uncertain is not a fill: reconcile through the native service and stop
   until resolved; never retry the order. Journal market evidence, review
   verdict/bounds/deadline, previews, amounts, costs, order IDs and outcome or no-trade reason.
   Notify failures, stale/invalid approvals, stop thresholds, missing funds and account
   restrictions, not every empty tick.

Equity check (plainly): with under about 25 EUR of bot equity this venue is not worth
day-trading, because cent rounding of EUR amounts and fees dominates the cost. Make no new
entries then, journal it, notify the owner once and keep reviewing held coins for exits.

Trading style (owner instruction 2026-10-28): active intraday trading. Never hard-code the fee:
the cost is the preview's `estimated_round_trip_pct`, whose fee is an estimate until a real fill
reports the truth. Take any candidate whose target is at least 1.5 times that measured cost and
at least 0.6%, with gross reward-to-risk of at least 1.5:1
and a window of 1 to 24 hours. Do not wait for a perfect setup, but never trade below the bar just
to trade, and never buy a bounce that still makes lower highs while BTC and ETH are both down more
than 2% over 24 hours (trend gate, owner instruction 2026-10-30).

Authoring or discovering this playbook starts nothing and changes no financial activation. Only
an explicit owner start uses the existing Condor engine at 2-minute intervals; restart_on_boot
preserves its existing resume behavior. Generic scheduler defaults never reset or replace the
native ledger/run context. Bounded-intent approval is an advisory agent protocol, not server
authorization; existing native guards remain authoritative.
