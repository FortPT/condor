# Crypto.com Main App and Claude

The container includes pinned Claude Code 2.1.311 and claude-agent-acp 0.97.0.
`HOME=/var/lib/condor` is the existing persistent, private state volume. Authenticate
as container UID 10001 with `claude auth login`; no API key or copied account token
is needed. Do not embed credentials in Compose, an image or Git. A rebuild preserves
this volume and its login. The native Claude readiness picker must report installed
before login, then authentication can be tested without a trading request.

Settings → Crypto.com App accepts an App Agent Key and secret. This is separate
from the Hummingbot Crypto.com Exchange connector. Enable Execute trades in the
official App, subject to account/jurisdiction availability. Keys are stored in a
per-user 0600 file under CONDOR_DATA_DIR/crypto_com_app, never sent back to the UI
or model. Saving/replacing them pauses execution and invalidates quotations.

The Crypto.com tab provides account checks, quotes, execution, history and bot
valuation. The bot ledger starts at EUR 30, distinct from other holdings. It keeps
half the current liquidation value in cash, allows at most three positions and
sizes new positions at most one sixth of current equity. Existing positions are
not topped up. Stop new exposure at **EUR 2 total bot equity**, not EUR 2 of losses;
profits increase the allocation. No guaranteed exit price or guaranteed profit.

The shared crypto_com_app MCP tool is mounted through existing tool profiles.
Execution is classified as dangerous/mutating, so attended confirmations and
dry-run rejection still work. Credentials and enable/disable are not MCP actions.
The shipped Crypto.com App Trader / Daily Spot loop evaluates every 15 minutes,
can choose any App-supported coin, and uses the same enforced order path. It
starts only by an explicit operator action, then may resume after a restart.

POSTs are never retried. A transaction ID alone does not establish a fill. Pending,
unknown or malformed order responses persist uncertainty and prevent further orders,
including after restart. Reconciliation reads provider history, matches a transaction
or quotation ID and requires verified final status and source/destination amounts.
Failed terminal orders clear uncertainty without changing the allocation. Resuming
after reconciliation remains an operator action. Unknown provider schemas require
inspection and a contract update, never an assumed successful fill.

Known limits: only the initial bot allocation and its confirmed proceeds are
tracked; outside trades/transfers and portfolio deposits are not imported into this
ledger. Exit valuations can fail on provider order minimums/dust; then new exposure
fails closed. Whole-account totals/history are not merged into the Hummingbot PnL
chart. Reconciliation currently searches the latest 50 App transactions. App keys,
fee schedules, minimum orders and terminal fill shapes must be checked against the
actual account before enabling autonomous execution. Fixtures are not proof of a
working live financial association or profitable strategy.

The live App wallet endpoint returns the entire supported-asset catalogue (about
1.4 MiB / 1,939 wallets on the initial connection). Responses are bounded at 8 MiB;
only funded balances and necessary currency/amount fields leave the adapter.
Card transactions, addresses and merchant metadata are not passed to the model.
The App's `done` transaction status is supported. When a completed order omits
explicit from/to amounts, the service requires exact source debit and destination
credit against its persisted pre-order wallet snapshot before recording a fill.
Ambiguous changes remain paused; they are never inferred from a transaction ID.

Sources: official crypto-com/crypto-agent-trading App scripts (revision
92e80c5527312d0acdf769f16511ba3c6db1f2fe), Crypto.com Agent Key management, Condor
LLM integration documentation and Claude Code authentication documentation.
# Portfolio dashboard

The main Portfolio page includes the authenticated user's Crypto.com App cash and
crypto wallets automatically, even while execution is paused. These holdings are
added after reading the shared Hummingbot cache and never written into that cache.
The App's native wallet valuations are converted to USD using its Bitcoin native/USD
price snapshot, so no missing EUR conversion is silently treated as parity. Values
are indicative, not executable sale proceeds. A valuation failure is shown explicitly.
The whole-account balance does not increase the bot's allocation. Historical charts
still cover Hummingbot only and are labelled accordingly; no App PnL is invented.

The Trade page offers a Crypto.com App workspace alongside the existing Hummingbot
workspace. A connected App account with no Exchange credentials opens there by
default. Explicit executor links retain the Hummingbot workspace, and its key gate
stays inside that workspace so the account selector remains accessible. Both App
entry points use the same quote, confirmation and allocation checks.

The packaged runtime disables automatic `uv` synchronization for MCP children as
well as the parent process. Otherwise the default development group is resolved
inside an offline production image and both stdio connections close before they
can expose any tools. The container smoke check initializes and lists tools on
both actual MCP subprocesses with networking disabled, without calling an account
or executing an order.
