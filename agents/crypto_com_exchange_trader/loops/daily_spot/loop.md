---
name: Daily Spot
description: Sequential market analysis, bounded-intent risk review and fresh preview before a native Crypto.com order
default_config:
  frequency_sec: 120
  tick_timeout_sec: 600
  execution_mode: loop
  total_amount_quote: 30
  restart_on_boot: true
  max_ticks: 0
default_trading_context: Crypto.com only; read capital from the native ledger and run context, never scheduler budget; stop new exposure at 2 total bot equity. Analyst then bounded-intent review, then a fresh preview, then one order.
---

# Daily Spot

Before step 1, check the cheap idle tick rule below. When it applies, do the short
tick it describes and stop; otherwise run steps 1 to 8.

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
3. Call scorecard (it scores the calls, not the trades placed): after the analyst's report each full tick, log ONE call for
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
   new entry per tick; zero trades is valid. A maker entry or target exit adds
   mode="maker" and purpose to the preview and the order, as the Maker mode
   paragraph below says; a stop exit is always taker.
8. Read history. Pending/uncertain is not a fill: reconcile through the native
   service and stop until resolved; never retry the order. Journal market
   evidence, review verdict/bounds/deadline, previews, amounts, costs, order IDs
   and outcome or no-trade reason. Notify failures, stale/invalid approvals,
   stop thresholds, missing funds and account restrictions, not every empty tick.

Trading style (owner instruction 2026-10-28): the owner wants active intraday
trading, not buy-and-hold. The measured round-trip cost is about 1.2% (a 0.5% taker
fee each way plus the spread). Take a NEW ENTRY when the analyst's target is at
least 2.4% (about twice that cost; owner instruction 2026-10-29, fewer and better
trades because fees ate the whole gross result of the first day), the stop is no
farther than two thirds of the target (gross reward-to-risk at least 1.5:1) and the
window is 1 to 24 hours; do not wait for a perfect setup, but never enter below that
bar. Review every
held coin on every tick as well: sell it when its target is reached, when its
invalidation level breaks, or when its thesis is stale (past its window with less
than half of the target made); an exit is a trade too. A rotation (selling a held
coin to buy a better one) follows the cash rotation rules below. Size entries
at roughly 15 to 25 percent of bot equity (never below 5 in the quote currency) within the 40%
cap. Expect a few trades on a day with moves of 2.4% or more; a quiet day with no
candidate over the bar is acceptable, but never trade below the bar just to trade,
and journal the best near-miss with its numbers. Daily brake: if bot equity is 5%
below its value at the start of the UTC day, make no new entries until 00:00 UTC and
notify the owner. Pace limits (owner instruction 2026-10-29): at most 4 NEW entries
per UTC day (count today's buys in `history`; the buy of a rotation counts), and a
coin bought less than 2 hours ago is sold only for its stop, a broken invalidation
level or an account-safety reason, never for a target, a stale thesis or a rotation.
The 2.4% bar replaces every 1.6% bar for new entries and rotations in this playbook.
These limits never block a stop or invalidation exit; journal when one holds you back.

Cost bound with headroom (found on 28 Oct, when a reviewer-approved buy was dropped
because the fresh preview cost 1.061% against an approved bound of 1.034%): the spread
moves between the preview and the order. Ask the reviewer for a cost bound at least
0.15 percentage points above the preview's round-trip cost (1.25% when the preview
shows 1.06%), and place the order whenever the fresh preview is inside the approved
bound. Only a preview above the bound, or a changed pair, side or amount, blocks it;
a small drift inside the headroom is not a reason to stop.

Maker mode (owner instruction 2026-10-29, optional): the Exchange charges a maker 0.25% and a taker 0.50%, so a resting post-only order saves fees, but it may never fill, it may fill after the thesis is stale, and nothing has run live yet. The owner switches maker mode on; a maker preview shows maker_mode_enabled, and when it is false use taker mode as before.
- Use mode="maker" only for an ENTRY (purpose="entry", a buy) or a TARGET exit (purpose="target_exit", a sell), and only when the thesis window is at least 1 hour and the maker preview's maker_taker_round_trip_pct (a maker entry plus a taker exit) times the usual cost multiple (1.3) is at most the target. The 1.6% target bar is unchanged until the owner reviews maker_stats: maker mode lowers the cost, it does not lower the bar.
- Always use taker mode for a stop or invalidation exit (maker is refused for purpose="stop_exit", and a stop must complete), for a rotation leg and for any exit that must complete.
- Preview with `crypto_com_exchange(action="preview", side="<buy|sell>", symbol="<COIN>", amount="<exact decimal>", mode="maker", purpose="<entry|target_exit>")`. The bounded intent names mode maker and its purpose, the preview's resting_price, maker_fee_pct with its source, maker_taker_round_trip_pct and the wait. The same size guards, spread bound and cost bound with headroom apply to the effective price, which is the resting price at the touch, never through it.
- Place it once with the same call as `action="order"` plus mode="maker", purpose and optionally maker_wait_sec (default 60, at most 75, so the call stays inside its 90 second budget). The call waits for the fill, cancels what is left and returns outcome filled, partial or unfilled. An unfilled order changes nothing: no trade happened.
- Never retry an unfilled or partial maker order as a taker order in the same tick, never resubmit it, and never chase the price. A post-only refusal (the price would cross the book) is a refusal: journal it and stop. A later tick may start again with a fresh review.
- Journal the maker outcome (filled, partial or unfilled) with the resting price, the filled fraction, the fee class and the seconds waited, so the fill rate can be measured. In a full tick that used maker mode, read `crypto_com_exchange(action="maker_stats")` once and put its fill rate in the journal line. Never change the mode or the cost bar from those numbers yourself: the owner decides.

Cash rotation (owner instruction 2026-10-29): the bot is often fully invested, with
cash below the minimum entry, so no new trade is possible and every tick ends in no
trade. When cash is below the minimum entry and the analyst returns an actionable
candidate whose net target clears the usual bar (target at least 1.6%, stop within two
thirds of the target, window 1 to 24 hours), you may propose ONE rotation: sell the
weakest eligible holding, then buy the candidate.
- Eligible holding: worth at least 1 in the quote currency (ignore dust below 1), and
  never a holding that is above its entry-implied target path and still within its
  thesis window. Prefer the holding with the smallest remaining expected move, or
  whose thesis is invalidated or stale.
- It must pay for itself: the candidate's expected gross move must exceed the full cost
  of the rotation (the sell leg's cost plus the buy leg's cost, each from a fresh
  preview, so roughly one round trip, about 1.2%) times the usual cost multiple (1.3),
  PLUS the holding's own forgone expected move (the rest of its distance to its target,
  in percent). Write the arithmetic in the journal.
- Limits: one rotation at a time, at most one rotation per 3 hours (read the journal
  for the time of the last one), and none while the 5% daily brake is hit.
- Both legs are ordinary trades: each has its own bounded intent (side, exact pair,
  amount, maximum amount, maximum slippage percent, costs/spread, UTC validity
  deadline), is approved by the reviewer, and is placed only after its own fresh
  preview is inside the approved bounds. Preview the sell leg before the review; the
  buy leg's order follows the sell's fill and is sized to the cash actually received
  (never above the approved maximum, never below the minimum entry).
- If the sell fills and the buy is then refused, out of bounds, expired or uncertain,
  say so plainly in the journal and notify the owner; do not chase: no second attempt,
  no substitute coin, no new review in the same tick. The cash simply stays cash.
- A stop or invalidation exit is never held up by any of this (no 3-hour limit, no cost
  multiple, no brake); it follows the normal exit path.

Cheap idle ticks (owner instruction 2026-10-29): when nothing can be done, do not pay
for a full tick. A tick is a CHEAP IDLE TICK when ALL of these hold:
1. status is connected, enabled and not uncertain (otherwise run the normal tick);
2. cash is below the minimum entry (5 in the quote currency);
3. no holding worth at least 1 has its bid within 25% of the way to its invalidation
   level (that is, bid minus invalidation is at most 25% of entry minus invalidation),
   none has reached its target, and none is past its window with less than half of the
   target made; a holding whose invalidation level is not in the journal is not
   cheap-safe, so run the normal tick;
4. the last full tick (analyst consulted, scorecard logged) was less than 60 minutes
   ago, so no rotation can be considered yet and the hourly scorecard call is not due.
Condition 4 caps the silence: at least once per hour the tick is a FULL tick (every
step, including the analyst, the scorecard call and, when the rotation conditions
hold, a rotation proposal), even when cash is below the minimum entry.
In a cheap idle tick do exactly this and stop:
- read valuation (cash, equity, positions, enabled and uncertain flags) and the held
  coins' current bids (the coin list); nothing else: no analyst, no reviewer, no
  previews, no scorecard calls, no candle or book reads, and no journal scan beyond the
  last full tick's holdings line (coin, entry, target, invalidation, window end);
- compare every held coin's bid with its invalidation level and target, and equity
  with the 5% daily brake (equity at the start of the UTC day, from the journal);
- write ONE journal line: "cheap tick: cash X below minimum; holdings clear (closest to
  invalidation: COIN at P% of the way); equity E; brake not hit; next full tick at
  HH:MM UTC";
- if any check fails (a bid at or under its invalidation level, a target reached, a
  thesis gone stale, the brake hit, a status change), stop being cheap and continue as
  a full tick from the first step so any exit goes through the normal path. Notify as
  usual; a cheap tick itself stays quiet.
The scorecard's one log per tick is waived for cheap idle ticks only; the full tick of
each hour still logs at least one call, so the call scorecard continues. Do not change the
loop's frequency: it is configured outside this prompt.

Authoring or discovering this playbook starts nothing and changes no financial
activation. Only an explicit owner start uses the existing Condor engine at
5-minute intervals; restart_on_boot preserves its existing resume behavior.
Generic scheduler defaults never reset or replace the native ledger/run context.
Bounded-intent approval is an advisory agent protocol, not server authorization;
existing native guards remain authoritative.
