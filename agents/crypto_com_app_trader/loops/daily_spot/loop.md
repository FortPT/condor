---
name: Daily Spot
description: Sequential market analysis, bounded-intent risk review and fresh native App quote execution
default_config:
  frequency_sec: 900
  tick_timeout_sec: 180
  execution_mode: loop
  total_amount_quote: 30
  restart_on_boot: true
  max_ticks: 0
default_trading_context: Crypto.com Main App only; read capital from the native ledger and run context, never scheduler budget; stop new exposure at EUR 2 total bot equity. Analyst then bounded-intent risk review are required before a final live quote and execution.
---

# Daily Spot

1. Call `crypto_com_app(action="status")`. Disconnected, disabled or uncertain
   means no order. Read history/reconcile uncertain submissions and notify the
   owner; never enable trading yourself. Read valuation, accounts, bot positions,
   history and journal. Native ledger and run context own capital, not generic
   scheduler defaults. Missing valuation blocks entries. At or below EUR 2
   total bot equity, no new exposure. No outside holdings, leverage, borrowing,
   transfers or withdrawals.
2. Call `delegate(action="ask", agent="crypto_com_market_analyst", task="Assess evidence for spot entries or exits; return actionable or no_trade", context="<native allocation, bot positions, App candidates, journal and timestamps>")`.
   Wait for its answer. Never start helper loops or detached workers. An
   unavailable analyst, error, timeout or missing answer means no order; journal
   and notify. A no_trade report ends the tick quietly after journaling.
3. Only for an actionable proposal, obtain one indicative native quote with
   `crypto_com_app(action="quote", kind="<purchase|sale|exchange>", source="<provider currency ID>", target="<provider currency ID>", amount="<exact decimal source amount>")`.
   Record quote ID, both amounts, timestamp/countdown and supported costs/spread.
   This ID is evidence only: never execute it. Propose a bounded intent naming
   kind, exact currency IDs, requested and maximum source amount, minimum received
   amount, permitted costs/spread with basis and UTC validity deadline.
4. Call `delegate(action="ask", agent="crypto_com_risk_reviewer", task="Approve, reject or mark incomplete this bounded trade intent before final quote", context="<market report; indicative quote and cost evidence; proposed intent bounds/deadline; native status, cash, positions, equity, limits and valuation timestamp>")`.
   Analysis precedes review: consult sequentially and on demand. An unavailable
   reviewer, error, timeout, missing answer, reject or incomplete means NO ORDER:
   journal and notify. Require explicit approve binding kind, exact currencies,
   requested source amount, maximum source amount, minimum received amount,
   permitted costs/spread, supported cost basis, UTC validity deadline and the
   allocation snapshot. Missing bounds or unknown cost basis means incomplete.
5. Recheck current native status, allocation and thesis after review. Changed
   intent/allocation, disconnected/disabled/uncertain state or expired approval
   means no order and new review. Otherwise request one fresh final quote for
   the exact approved request. Verify its kind/currencies/source amount match,
   source <= approved maximum, destination >= approved minimum, costs/spread
   within approved bounds, approval deadline unexpired and final countdown
   positive. Missing fields or out-of-bounds means no order; journal and notify,
   require new review next tick, never chase quotes. The indicative quote ID
   cannot be executed. Do not insert a slow consultation after this final quote.
6. Submit only that fresh final ID once while within all bounds:
   `crypto_com_app(action="execute", quotation_id="<fresh final quote ID>")`.
   Native guards still enforce enabled state, ledger allocation, half-equity cash
   reserve, maximum three positions, one-sixth-equity new-position limit and
   EUR 2 total-equity floor, not cumulative losses. Never bypass a refusal or
   enlarge an order to meet a minimum. At most one new entry per tick; zero
   trades is valid. Purchases and sale proceeds use EUR.
7. Read history. Pending/uncertain is not a fill: reconcile through the native
   service and stop until resolved; never retry execute. Journal market evidence,
   review verdict/bounds/deadline, indicative/final quote IDs, amounts, costs,
   transaction IDs and outcome or no-trade reason. Notify failures, stale/invalid
   approvals, stop thresholds, missing funds and provider restrictions, not every
   empty tick.

Authoring or discovering this playbook starts nothing and changes no financial
activation. Only an explicit owner start uses the existing Condor engine at
15-minute intervals; restart_on_boot preserves its existing resume behavior.
Generic scheduler defaults never reset or replace the native ledger/run context.
Bounded-intent approval is an advisory agent protocol, not server authorization;
existing native guards remain authoritative.
