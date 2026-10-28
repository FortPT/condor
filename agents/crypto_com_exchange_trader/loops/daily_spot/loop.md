---
name: Daily Spot
description: Sequential market analysis, bounded-intent risk review and fresh preview before a native Exchange order
default_config:
  frequency_sec: 900
  tick_timeout_sec: 180
  execution_mode: loop
  total_amount_quote: 30
  restart_on_boot: true
  max_ticks: 0
default_trading_context: Crypto.com Exchange only; read capital from the native ledger and run context, never scheduler budget; stop new exposure at 2 total bot equity. Analyst then bounded-intent review, then a fresh preview, then one order.
---

# Daily Spot

1. Call `crypto_com_exchange(action="status")`. Disconnected, disabled or
   uncertain means no order. Read history/reconcile uncertain submissions and
   notify the owner; never enable trading yourself. Read valuation, accounts,
   bot positions, history and journal. Native ledger and run context own capital,
   not generic scheduler defaults. Missing valuation blocks entries. At or below
   2 total bot equity, no new exposure. No outside holdings, leverage, margin,
   borrowing, transfers or withdrawals.
2. Scan the market with `crypto_com_exchange(action="coins", keyword="")`: the top
   pairs by 24h volume in the ledger's quote currency, with bid, ask, spread and
   lot size. Skip stablecoins, wrapped coins, tokenized funds or metals and pairs
   with a spread above 0.5% or too little volume to trade the order size. Shortlist
   the 6 most promising new candidates by recent move, momentum and volume, rotate
   across ticks, and always include every coin the bot holds. Then call
   `delegate(action="ask", agent="crypto_com_market_analyst", task="Assess evidence for spot entries or exits; return actionable or no_trade", context="<native allocation, bot positions, shortlist with Exchange bid/ask/spread/volume, journal and timestamps>")`.
   Wait for its answer. Never start helper loops or detached workers. An
   unavailable analyst, error, timeout or missing answer means no order; journal
   and notify. A no_trade report ends the tick quietly after journaling.
   Selling a held coin to fund a better one is allowed when the new expected gain
   beats the held coin's own outlook plus both legs' costs; the cost per leg is the
   measured spread plus the fee, a fraction of a percent, so small moves can pay.
3. Scorecard (a paper test): after the analyst's report each tick, log ONE call for
   the best candidate with no open call using `crypto_com_exchange(action="predict", symbol="<COIN>", verdict="trade"|"pass", target_pct="...", stop_pct="...", horizon_hours="...", note="...")`.
   Use the analyst's target, invalidation and window. Read `action="scorecard"`
   first and never log numbers you do not expect. Logging places no order.
4. Only for an actionable proposal, take one preview with
   `crypto_com_exchange(action="preview", side="<buy|sell>", symbol="<COIN>", amount="<exact decimal>")`
   and record pair, quantity, limit price, bid, ask, spread, estimated impact and
   timestamp. It is evidence only. Propose a bounded intent naming side, exact
   pair, requested and maximum amount, maximum slippage percent, permitted
   costs/spread with basis and a UTC validity deadline.
5. Call `delegate(action="ask", agent="crypto_com_risk_reviewer", task="Approve, reject or mark incomplete this bounded trade intent before the order", context="<market report; preview; the bounds; native status, cash, positions, equity, limits and valuation timestamp>")`.
   Analysis precedes review: consult sequentially and on demand. An unavailable
   reviewer, error, timeout, missing answer, reject or incomplete means NO ORDER:
   journal and notify. Require an explicit approve binding side, exact pair,
   requested amount, maximum amount, maximum slippage percent, permitted
   costs/spread, supported cost basis, UTC validity deadline and the allocation
   snapshot. Missing bounds or unknown cost basis means incomplete.
6. Recheck current native status, allocation and thesis after review. Changed
   intent/allocation, disconnected/disabled/uncertain state or expired approval
   means no order and new review. Otherwise take one fresh preview for the exact
   approved request. Verify side/pair/amount match, amount <= approved maximum,
   spread and estimated impact within the approved costs, the book covers the
   order and the approval deadline is unexpired. Missing fields or out-of-bounds
   means no order; journal and notify, require new review next tick, never chase
   the market. Do not insert a slow consultation after this fresh preview.
7. Place the order once while within all bounds:
   `crypto_com_exchange(action="order", side="<buy|sell>", symbol="<COIN>", amount="<exact decimal>", max_slippage_pct="<approved>")`.
   Native guards still enforce enabled state, ledger allocation, the 40% asset cap
   below 1000 equity (or half-equity cash reserve, maximum three positions and
   one-sixth-equity new-position limit from 1000) and the 2 total-equity floor,
   not cumulative losses. Never bypass a refusal or enlarge an order. At most one
   new entry per tick; zero trades is valid.
8. Read history. Pending/uncertain is not a fill: reconcile through the native
   service and stop until resolved; never retry the order. Journal market
   evidence, review verdict/bounds/deadline, previews, amounts, costs, order IDs
   and outcome or no-trade reason. Notify failures, stale/invalid approvals,
   stop thresholds, missing funds and account restrictions, not every empty tick.

Authoring or discovering this playbook starts nothing and changes no financial
activation. Only an explicit owner start uses the existing Condor engine at
15-minute intervals; restart_on_boot preserves its existing resume behavior.
Generic scheduler defaults never reset or replace the native ledger/run context.
Bounded-intent approval is an advisory agent protocol, not server authorization;
existing native guards remain authoritative.
