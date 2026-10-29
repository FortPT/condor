---
name: Paper Session
description: Scan tokenized US stocks during the regular session, preview and place simulated orders on the paper account, and log one scored paper call per tick
default_config:
  frequency_sec: 300
  tick_timeout_sec: 420
  execution_mode: loop
  total_amount_quote: 0
  restart_on_boot: true
  max_ticks: 0
default_trading_context: Simulated Kraken xStocks paper account (100 USD, live public order book, taker fee and slippage charged); read capital from the paper ledger, never scheduler budget; stop new exposure at 2 equity. No real money.
---

# Paper Session

1. Call `xstocks_paper(action="status")`. If the regular US session is closed, write one line
   to the journal and end the tick: no scan, no order. This is the normal outcome outside
   Monday to Friday, 09:30 to 16:00 New York time. A paused account means no order: notify
   the owner once (only the owner can reset it) and end the tick.
2. Call `xstocks_paper(action="valuation")` for the simulated equity, then read
   `xstocks_paper(action="history")`, the journal and `xstocks_paper(action="scorecard")`.
   The paper ledger owns capital, not generic scheduler defaults. At or below 2 equity there
   is no new exposure. At your first tick of the New York trading day, journal the equity as
   the day's open; if equity is 5% below that, make no new entries today and notify the owner
   (exits stay allowed).
3. Scan with `xstocks_paper(action="coins", keyword="")`. Skip anything with a spread above
   0.1% or under 50,000 in 24h dollar volume. Shortlist the 6 most promising new candidates
   by recent move, momentum, volume and range (single stocks with a 24h range of 1.5% or
   more; at most one broad ETF as a reference), rotate across ticks, and always include every
   stock the account holds. For each shortlisted and held stock read
   `xstocks_paper(action="candles", symbol="<TICKER>", timeframe="15m", count="96")` and, if
   useful, the 1-hour candles. Form a short view: trend, momentum, nearest support and
   resistance, a target, an invalidation level and a window in hours that ends before the
   close.
4. Review every held stock for an exit every tick: target reached, invalidation broken or
   thesis stale. Selling a held stock to fund a better one is allowed when the new expected
   gain beats the held stock's outlook plus both legs' costs.
5. Scorecard: log ONE call for the best candidate with no open call using
   `xstocks_paper(action="predict", symbol="<TICKER>", verdict="trade"|"pass", target_pct="...", stop_pct="...", horizon_hours="...", note="...")`.
   The horizon must end before the close and the stop be no farther than two thirds of the
   target. Never log numbers you do not expect. If every shortlisted stock already has an open
   call, skip logging.
6. Only for an actionable proposal, take one preview with
   `xstocks_paper(action="preview", side="<buy|sell>", symbol="<TICKER>", amount="<exact decimal>")`
   and record pair, quantity, limit price, bid, ask, spread, estimated impact, fee, the
   estimated round-trip cost and `blocked_reason`.
   The round-trip cost comes from the preview, never from memory. The target must be at least 1.5 times that cost and at least 1.0%, with the stop no farther than two thirds of the target and the horizon ending before the close; otherwise no order, and journal the near-miss. Entries are roughly 15 to 25 percent of simulated equity (never below 5 USD).
7. There is no reviewer on paper. Journal the reasoning and the bounds first: side, exact
   pair, amount, maximum slippage percent, permitted spread and impact, the round trip, the
   target, stop and horizon. Then take one fresh preview for the exact amount and verify that
   `blocked_reason` is empty, `book_covers_order` is true and the spread and impact are still
   inside the bounds. Out of bounds means no order; never chase the market.
8. Place the order once while within all bounds:
   `xstocks_paper(action="order", side="<buy|sell>", symbol="<TICKER>", amount="<exact decimal>", max_slippage_pct="<bound>", client_id="<NY date>-<tick>-<TICKER>-<side>")`.
   A buy's amount is the USD to spend; a sell's amount is stock units. The ledger still
   enforces the session, the 40% asset cap below 1000 equity (or the half-equity cash
   reserve, three positions and one-sixth-equity new-position limit from 1000), the minimum
   order and the 2 equity floor. Never bypass a refusal or enlarge an order. At most one new
   entry per tick; zero trades is valid.
9. Read `xstocks_paper(action="history")` for the fill. A partial fill (status canceled with
   some quantity) is a result; journal it and do not retry. Journal the market evidence, each
   preview, the bounds, the order, its fee and fill price, the equity and the no-trade reason
   when there was none. Keep ordinary ticks quiet. Send one notification a day at most, near
   the close, with the simulated equity, orders and scorecard; notify at once only when the
   account is paused or the daily brake triggers.

Trading style (the live crew's owner instruction): aggressive intraday trading, about 100
round trips a month. Never hard-code the cost: use the preview's number. Take any candidate
whose target is at least 1.5 times the measured round-trip cost and at least 1.0%, with gross
reward-to-risk of at least 1.5:1 and a horizon that ends before the close. Do not wait for a
perfect setup, but never trade below the bar just to trade.

Authoring or discovering this playbook starts nothing. Only an explicit owner start uses the
existing Condor engine, then it may resume after a restart. Every order is simulated: it
places nothing on any exchange and holds no real money. The simulated equity is the output,
beside the scorecard.
