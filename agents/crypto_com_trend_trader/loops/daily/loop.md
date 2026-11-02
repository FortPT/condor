---
name: Daily breakout
description: Every few hours compare the daily breakout signal with the bot's holdings and trade only when they differ
default_config:
  frequency_sec: 14400
  tick_timeout_sec: 600
  execution_mode: loop
  total_amount_quote: 0
  restart_on_boot: true
  max_ticks: 0
default_trading_context: Crypto.com only (BTC and ETH spot); read capital from the native ledger and run context, never scheduler budget. The signal is computed in code; no analyst and no reviewer.
---

# Daily breakout

Runs every four hours. The signal only changes with a new closed UTC day, so a tick that finds
nothing to do is normal; the repeat is there so a missed tick costs hours, not a day.

1. Read `crypto_com_exchange(action="status")` and `valuation`. Disconnected, disabled or
   uncertain means no order. At your first tick of the UTC day journal the bot equity as the
   day's opening equity.
2. Read `crypto_com_exchange(action="trend")` and the journal. For every coin whose `action`
   is `sell` or `buy`, follow your instructions: exits first, entries second, a preview before
   each order, at most one order per coin per tick.
3. A `hold` or `none` coin needs nothing. `no_data` is never a reason to act.
4. Journal the tick. Notify only orders, refusals, uncertain outcomes and repeated missing
   data.

Authoring this playbook starts nothing and changes no financial activation. Only an explicit
owner start runs it.
