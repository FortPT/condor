# Native Claude and Crypto.com App implementation plan

Goal: run Claude with the owner's subscription and support Crypto.com App trading from Condor's dashboard, chat and existing autonomous loops, starting with EUR 30.

Architecture: keep the original Condor/Hummingbot deployment. Add a small App API adapter using the official crypto-com/crypto-agent-trading request contracts, shared by HTTP routes and MCP tools. Keep credentials per user on the VM, never return them. Package pinned Claude CLI and ACP runtime with a persistent home. Reuse Condor's loop engine, not a second scheduler.

Scope: App spot purchases, sales, crypto exchange, balances, quotations, transaction history. No bank transfers, cash withdrawals, leverage, public network exposure or automatic live activation during setup. Asset universe and maximum loss are awaiting the owner's answer. Daily trading is the goal; no promised returns or forced daily order.

Approach: native adapter chosen over installing an unmodified trading skill because the dashboard and loops need the same enforcement and errors. No change to Hummingbot Exchange connector contracts or existing portfolio totals/currency conversions.

## Task 1: Claude runtime
- [ ] Pin Claude Code and claude-agent-acp with a package lock; include Node/npm and bins in Docker runtime.
- [ ] Set HOME to the already persistent Condor volume; no account material in images.
- [ ] Verify version/readiness and offline container smoke checks before login.

## Task 2: App integration
- [ ] Tests first: official HMAC body/path, exact decimal amounts, malformed responses, no redirects, no POST retry, expired/single-use/user-scoped quotes, credential rotation and error redaction.
- [ ] Implement per-user credential store, fixed-host signed HTTP client and quote/confirm flow.
- [ ] Expose authenticated routes and MCP tools through existing profile registration; no credential tool available to the model.
- [ ] Add native Crypto.com settings/trading panel, account/history refresh and explicit order confirmation. Existing loops may use execution tools once separately configured and enabled by the owner.
- [ ] Test UI, backend and existing profile/import contracts; full suite on Linux/container.

## Task 3: Deployment and handoff
- [ ] Review diff, publish branch/PR, build immutable image; update only Condor in Portainer stack 20 with rollback.
- [ ] Verify four healthy services, HTTPS, existing API association and no Caddy/network regressions.
- [ ] Hand off Claude login and Agent Key entry securely to owner; verify live auth without trading.
- [ ] Configure the existing trading loop only after asset/risk limits are known; owner activates real trading.

Review focus: duplicate order on timeout; key/quote cross-user leakage; stale quotations after restart/rotation; currency assumptions; automation spending beyond the EUR 30 allocation.

Execution: inline in the existing dedicated clean feat/native-account-preparation checkout. Owner explicitly resumed setup; continue without workflow approval prompts. Changes outside the account setup remain excluded.
