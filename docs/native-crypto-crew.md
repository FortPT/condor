# Native Crypto.com App crew

This adapts the research-then-synthesis role separation of
[Philippe Ostiguy's AITradingCrew](https://github.com/philippe-ostiguy/AITradingCrew)
to Condor's existing AgentStore and blocking `delegate(action="ask")` workflow.
The role instructions are authored here; no upstream code or prompts are copied.
Upstream produces stock research reports, not a live execution or backtest engine.
Its CrewAI runtime and dependencies are not installed.

| Native profile | Responsibility | Tool boundary |
| --- | --- | --- |
| `crypto_com_market_analyst` | Public market evidence and actionable/no-trade report | Public prices/candles and journal reads only |
| `crypto_com_risk_reviewer` | Approve/reject/incomplete for one bounded trade intent | Journal reads only; all market/allocation/quote context comes from coordinator |
| `crypto_com_app_trader` | Sequential consultation, native account guards, single execution and journal | Native App tool, blocking delegation, public market reads, journal/memory and notification |

All three use the existing Claude Sonnet model. Only the coordinator owns a
loop: `crypto_com_app_trader.daily_spot`, every 900 seconds. Helpers ship no loops,
cannot delegate or execute, and should only answer coordinator consultations.
Tool allowlists are enforced by Condor's existing MCP seat mutes, including ACP
seats. ACP permission checks also deny non-allowlisted host tools such as shell,
file writes and nested native tasks; ToolSearch remains available for discovery.
Allowed calls retain the coordinator's original risk callback. Shared skills do
not add omitted tools.

The coordinator first reads current native status, valuation, allocation and
journal, then asks the analyst. No-trade ends quietly. An actionable proposal
gets an indicative native quotation, then the risk reviewer receives the market
report, native status/allocation/limits, indicative quote ID, kind, currency IDs,
both decimal amounts, costs, timestamps and countdown. Approval must bind the
requested and maximum source amounts, minimum received amount, permitted
costs/spread with a supported basis, UTC deadline and allocation snapshot.
Missing bounds or unknown cost basis means incomplete. Failed/unavailable
consultation, reject, incomplete or expired approval means no order; failures
are journaled and notified.

Only after review does the coordinator recheck unchanged status/allocation and
request a fresh final native quote for the exact approved intent. It checks
every amount/cost bound and approval deadline, then submits only that fresh
single-use ID within its countdown through existing native guards. The old
indicative quote is evidence only and cannot be executed. A changed intent or
allocation, out-of-bounds final quote or stale approval needs new review; the
tick stops instead of chasing quotes. This puts the slow model consultation
before the final quote's short lifetime without bypassing expiry or risk review.

There is one native bot ledger and no extra capital for either helper. Its
current cash/positions and the owner's run context take precedence over generic
scheduler defaults. Keep the existing half-equity cash reserve, maximum three
positions, one-sixth-equity new-position limit and EUR 2 total-equity floor.
The floor is not cumulative loss. No outside holdings, leverage, borrowing,
transfers or withdrawals. Accepted/pending is not filled; uncertain submission
requires history/reconciliation and must not be retried.

Shipping these profiles starts nothing and changes no account enablement or
live ledger. The owner still enables trading and starts the coordinator loop;
its existing restart-on-boot behavior is preserved after that explicit start.
The helpers are separate identities, not independent trading bots or schedulers.
Installation-local profile/playbook overrides shadow these stock files and must
be reviewed by the operator when adopting the crew.

This is a prompt workflow with enforced tool boundaries, not a new server-side
risk-approval protocol. The native service enforces money/account/quote guards;
it does not cryptographically verify an LLM review. No new runtime, package,
provider credentials or scheduler is needed. There is no TimeGPT, news/sentiment
provider or fundamental-analysis integration, historical backtest, guaranteed
edge or profit claim. Public Exchange candles are research proxies and never
App execution prices; unavailable market data results in no trade.
