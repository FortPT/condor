---
name: Intraday
description: Sequential market analysis, bounded-intent risk review and fresh preview before a native Kraken xStocks order
default_config:
  frequency_sec: 300
  tick_timeout_sec: 600
  execution_mode: loop
  total_amount_quote: 0
  restart_on_boot: true
  max_ticks: 0
default_trading_context: Kraken xStocks only (USD pairs, regular US session); read capital from the native ledger and run context, never scheduler budget; stop new exposure at 2 total bot equity. Analyst then bounded-intent review, then a fresh preview, then one order.
---

# Intraday

1. Call `kraken_xstocks(action="status")`. If the regular US session is closed, journal one short
   line and end the tick: no scan, no analyst, no order. Disconnected, disabled or uncertain means
   no order. Read history/reconcile uncertain submissions and notify the owner; never enable
   trading yourself. Read valuation, accounts, bot positions, history and journal. Native ledger
   and run context own capital, not generic scheduler defaults. Missing valuation blocks entries.
   At or below 2 total bot equity, no new exposure. No outside holdings, leverage, margin,
   borrowing, transfers or withdrawals. At your first tick of the New York trading day, journal
   the bot equity as the day's open; if equity is 5% below that, make no new entries today and
   notify the owner (exits stay allowed).
2. Scan the market with `kraken_xstocks(action="coins", keyword="")`: the most traded xStocks with
   bid, ask, spread, 24h dollar volume, 24h range and the move since the open. Skip anything with a
   spread above 0.1% or under 50,000 in 24h dollar volume. Shortlist the 6 most promising new
   candidates by recent move, momentum, volume and range (single stocks with a 24h range of 1.5%
   or more; at most one broad ETF as a reference), rotate across ticks, and always include every
   stock the bot holds. For each shortlisted and held stock read
   `kraken_xstocks(action="candles", symbol="<TICKER>", timeframe="15m", count="96")` and, if useful,
   the 1-hour candles. Then call
   `delegate(action="ask", agent="kraken_xstocks_analyst", task="Assess evidence for intraday entries or exits; return actionable or no_trade", context="<native allocation, bot positions, shortlist with Kraken bid/ask/spread/24h dollar volume/range/move since open, minutes to close, compact candle summaries, journal and timestamps>")`.
   Wait for its answer. Never start helper loops or detached workers. An unavailable analyst,
   error, timeout or missing answer means no order; journal and notify. A no_trade report ends the
   tick quietly after journaling. Selling a held stock to fund a better one is allowed when the new
   expected gain beats the held stock's own outlook plus both legs' costs. Review every held stock
   for an exit every tick: target reached, invalidation broken or thesis stale.
3. Call scorecard (it scores the calls, not the trades placed): after the analyst's report each tick, log ONE call for the best
   candidate with no open call using `kraken_xstocks(action="predict", symbol="<TICKER>", verdict="trade"|"pass", target_pct="...", stop_pct="...", horizon_hours="...", note="...")`.
   Use the analyst's target, invalidation and window; the horizon must end before the close.
   Read `action="scorecard"` first and never log numbers you do not expect. Logging places no order.
4. Only for an actionable proposal, take one preview with
   `kraken_xstocks(action="preview", side="<buy|sell>", symbol="<TICKER>", amount="<exact decimal>")`
   and record pair, quantity, limit price, bid, ask, spread, estimated impact, taker fee, estimated
   round-trip cost and timestamp. It is evidence only. The round-trip cost comes from the preview,
   never from memory: an unknown fee means incomplete. The target must be at least 1.5 times that
   cost and at least 1.0%, with the stop no farther than two thirds of the target and the horizon
   ending before the close; otherwise no order, journal the near-miss. Propose a bounded intent naming
   side, exact pair, requested and maximum amount, maximum slippage percent, permitted costs/spread
   with basis and a UTC validity deadline. Entries are roughly 15 to 25 percent of bot equity.
5. Call `delegate(action="ask", agent="kraken_xstocks_risk_reviewer", task="Approve, reject or mark incomplete this bounded trade intent before the order", context="<market report; preview; the bounds; minutes to close; native status, cash, positions, equity, limits and valuation timestamp>")`.
   Analysis precedes review: consult sequentially and on demand. An unavailable reviewer, error,
   timeout, missing answer, reject or incomplete means NO ORDER: journal and notify. Require an
   explicit approve binding side, exact pair, requested amount, maximum amount, maximum slippage
   percent, permitted costs/spread, supported cost basis, UTC validity deadline and the allocation
   snapshot. Missing bounds or unknown cost basis means incomplete.
6. Recheck current native status, allocation, the session and the thesis after review. Changed
   intent/allocation, disconnected/disabled/uncertain state, a closed session or expired approval
   means no order and new review. Otherwise take one fresh preview for the exact approved request.
   Verify side/pair/amount match, amount <= approved maximum, spread and estimated impact within the
   approved costs, the book covers the order and the approval deadline is unexpired. Missing fields
   or out-of-bounds means no order; journal and notify, require new review next tick, never chase
   the market. Do not insert a slow consultation after this fresh preview.
7. Place the order once while within all bounds:
   `kraken_xstocks(action="order", side="<buy|sell>", symbol="<TICKER>", amount="<exact decimal>", max_slippage_pct="<approved>")`.
   A buy's amount is the USD to spend; a sell's amount is stock units. Native guards still enforce
   enabled state, USD xStocks pairs only, the regular session, ledger allocation, the 40% asset cap
   below 1000 equity (or half-equity cash reserve, maximum three positions and one-sixth-equity
   new-position limit from 1000) and the 2 total-equity floor, not cumulative losses. Never bypass a
   refusal or enlarge an order. At most one new entry per tick; zero trades is valid.
8. Read history. Pending/uncertain is not a fill: reconcile through the native service and stop
   until resolved; never retry the order. Journal market evidence, review verdict/bounds/deadline,
   previews, amounts, costs, order IDs and outcome or no-trade reason. Notify failures,
   stale/invalid approvals, stop thresholds, missing funds and account restrictions, not every
   empty tick.

Trading style (owner instruction 2026-10-28): aggressive intraday trading, about 100 round trips a
month. Never hard-code the fee: the owner may be on a zero-fee plan, so use the preview's number. Take
any candidate whose target is at least 1.5 times the measured round-trip cost and at least 1.0%, with
gross reward-to-risk of at least 1.5:1 and a horizon that ends before the close. Do not wait for a
perfect setup, but never trade below the bar just to trade.

Authoring or discovering this playbook starts nothing and changes no financial activation. Only an
explicit owner start uses the existing Condor engine at 5-minute intervals; restart_on_boot preserves
its existing resume behavior. Generic scheduler defaults never reset or replace the native ledger/run
context. Bounded-intent approval is an advisory agent protocol, not server authorization; existing
native guards remain authoritative.
