---
name: Paper Session
description: Scan tokenized US stocks during the regular session, log one scored paper call per tick
default_config:
  frequency_sec: 600
  tick_timeout_sec: 420
  execution_mode: loop
  total_amount_quote: 0
  restart_on_boot: true
  max_ticks: 0
default_trading_context: Paper only on Kraken xStocks public data; no account, no orders, no money.
---

# Paper Session

1. Call `xstocks_paper(action="status")`. If the regular US session is closed, write
   one line to the journal and end the tick. This is the normal outcome outside
   Monday to Friday, 09:30 to 16:00 New York time.
2. Read the journal and `xstocks_paper(action="scorecard")` so you do not repeat a
   call that is still open and you can see how the earlier calls scored.
3. Scan with `xstocks_paper(action="coins", keyword="")`. Shortlist 3 to 5 stocks:
   spread at most 0.1%, 24h dollar volume at least 50,000, and a 24h range of 1.5% or
   more, mixing single stocks with at most one broad ETF as a reference. Rotate
   through different stocks over the session instead of rechecking the same ones.
4. For each shortlisted stock read `xstocks_paper(action="candles", symbol="<TICKER>", timeframe="15m", count="96")`
   and, if useful, the 1-hour candles. Form a short view: trend, momentum, where the
   nearest support and resistance sit, and what would invalidate the idea.
5. Pick the best candidate that has no open call. Log exactly one call with
   `xstocks_paper(action="predict", symbol="<TICKER>", verdict="trade"|"pass", target_pct="...", stop_pct="...", horizon_hours="...", note="...")`.
   Target at least 1.5%, stop no farther than two thirds of the target, horizon in
   hours and no longer than the time left in the session (`minutes_to_close`).
   Use `trade` when you would have entered and `pass` for the best near-miss. If
   every shortlisted stock already has an open call, skip logging.
6. Journal the tick: the shortlist, the call logged and the numbers behind it, and
   what you declined and why. Keep ordinary ticks quiet. Send one notification a day
   at most, near the close, summarizing how many calls were logged and how the
   scorecard stands; send nothing for an empty tick.

Authoring or discovering this playbook starts nothing. Only an explicit owner start
uses the existing Condor engine, then it may resume after a restart. It places no
order and holds no money; the scorecard is the only output.
