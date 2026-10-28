# Native Crypto.com Exchange crew

This adapts the research-then-synthesis role separation of
[Philippe Ostiguy's AITradingCrew](https://github.com/philippe-ostiguy/AITradingCrew)
to Condor's existing AgentStore and blocking `delegate(action="ask")` workflow.
The role instructions are authored here; no upstream code or prompts are copied.
Upstream produces stock research reports, not a live execution or backtest engine.
Its CrewAI runtime and dependencies are not installed.

| Native profile | Responsibility | Tool boundary |
| --- | --- | --- |
| `crypto_com_market_analyst` | Public market evidence and actionable/no-trade report | Public prices/candles and journal reads only |
| `crypto_com_risk_reviewer` | Approve/reject/incomplete for one bounded trade intent | Journal reads only; all market/allocation/preview context comes from coordinator |
| `crypto_com_exchange_trader` | Sequential consultation, native account guards, single order and journal | Native Exchange tool, blocking delegation, public market reads, journal/memory and notification |

All three use the existing Claude Sonnet model. Only the coordinator owns a
loop: `crypto_com_exchange_trader.daily_spot`, every 900 seconds. Helpers ship no
loops, cannot delegate or order, and should only answer coordinator consultations.
Tool allowlists are enforced by Condor's existing MCP seat mutes, including ACP
seats. ACP permission checks also deny non-allowlisted host tools such as shell,
file writes and nested native tasks; ToolSearch remains available for discovery.
Allowed calls retain the coordinator's original risk callback. Shared skills do
not add omitted tools.

The coordinator first reads current native status, valuation, allocation and
journal, scans the Exchange's top pairs, then asks the analyst. No-trade ends
quietly. An actionable proposal gets a native preview of the live order book, then
the risk reviewer receives the market report, native status/allocation/limits,
the preview (pair, side, quantity, limit price, bid, ask, spread and estimated
impact), costs and timestamps. Approval must bind the side, exact pair, requested and
maximum amounts, maximum slippage percent, permitted costs/spread with a supported
basis, UTC deadline and allocation snapshot. Missing bounds or unknown cost basis
means incomplete. Failed/unavailable consultation, reject, incomplete or expired
approval means no order; failures are journaled and notified.

Only after review does the coordinator recheck unchanged status/allocation and take
a fresh preview for the exact approved intent. It checks every amount/cost bound and
the approval deadline, then places one immediate-or-cancel limit order through the
existing native guards. There is no quotation on the Exchange, so there is no
countdown to race; the slippage bound in the order is what limits the price. A
changed intent or allocation, out-of-bounds fresh preview or stale approval needs new
review; the tick stops instead of chasing the market.

There is one native bot ledger and no extra capital for either helper. Its current
cash/positions and the owner's run context take precedence over generic scheduler
defaults. Below 1000 of bot equity no single asset may exceed 40% of equity; from
1000 the half-equity cash reserve, maximum three positions and one-sixth-equity
new-position limit apply. The 2 total-equity floor is not cumulative loss. No outside
holdings, leverage, margin, borrowing, transfers or withdrawals. A filled or
cancelled order is read from the Exchange; an uncertain submission requires
reconciliation and must not be retried.

Shipping these profiles starts nothing and changes no account enablement or live
ledger. The owner still syncs the allocation, enables trading and starts the
coordinator loop; its existing restart-on-boot behavior is preserved after that
explicit start. The helpers are separate identities, not independent trading bots or
schedulers. Agent workspaces remain visible without Hummingbot credentials, so
Exchange-only operators can inspect all three roles and the coordinator's loop
directly. Installation-local profile/playbook overrides shadow these stock files and
must be reviewed by the operator when adopting the crew.

This is a prompt workflow with enforced tool boundaries, not a new server-side
risk-approval protocol. The native service enforces money/account/order guards; it
does not cryptographically verify an LLM review. No new runtime, package, provider
credentials or scheduler is needed. There is no TimeGPT, news/sentiment provider or
fundamental-analysis integration, historical backtest, guaranteed edge or profit
claim. Candles from other venues are research proxies for the Exchange price;
unavailable market data results in no trade.
