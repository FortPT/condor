---
name: Daily Spot
description: Evaluate App spot opportunities and manage the bot allocation throughout the day
default_config:
  frequency_sec: 900
  tick_timeout_sec: 180
  execution_mode: loop
  total_amount_quote: 30
  restart_on_boot: true
  max_ticks: 0
default_trading_context: Crypto.com Main App only; initial EUR 30 allocation; stop new exposure at EUR 2 equity.
---

# Daily Spot

1. Call `crypto_com_app(action="status")`. If disconnected, disabled or an
   order is uncertain, place no orders. Read history/reconcile an uncertain
   submission; report anything requiring the owner. Never re-enable yourself.
2. Call valuation and accounts. If equity is at or below EUR 2, place no new
   orders and report the stop. Missing valuations block entries, not a valid
   sale of an independently liquid asset.
3. Review open bot positions, recent history and your previous journal. Decide
   whether an exit is justified by the current evidence, costs or changed
   thesis. Do not sell unrelated account holdings.
4. Search for candidate opportunities. You may consider any App-listed coin;
   no currency allowlist is imposed. Use current market data and a coherent
   entry/exit thesis. Skip data-poor or illiquid candidates and skip weak reads.
5. For each justified action, obtain a provider quotation. Check the exact
   currencies, source/destination amounts, expiry, costs and the current
   proportional allocation before executing. An autonomous tick does not need
   a per-trade confirmation. Execute at most one new entry per tick; it is
   acceptable to make zero trades in a day. Never chase an expired quote or
   repeatedly churn the same exposure.
6. Read history after submission. Pending/uncertain is not a fill: reconcile
   through the service and stop submitting until resolved. Journal the outcome
   or no-trade reason. Notify the owner for an execution error, stop threshold,
   missing funds, provider restriction or account expiry, not every empty tick.

The loop is shipped but never starts itself on installation. The owner connects
the account, verifies the integration and explicitly starts this loop. Once
started, restart_on_boot permits the existing Condor engine to resume it after
a restart; the service's enabled/uncertain state still governs every order.
