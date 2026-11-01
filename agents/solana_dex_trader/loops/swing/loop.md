---
name: Swing
description: Quote-first swing trading of whitelisted Solana tokens against USDC inside the swap limits
default_config:
  frequency_sec: 900
  tick_timeout_sec: 600
  execution_mode: loop
  total_amount_quote: 30
  quote_asset: USDC
  restart_on_boot: true
  max_ticks: 0
  risk_limits:
    # USDC, the quote of every pair this loop trades.
    max_position_size_quote: 15
    max_swap_quote: 12
    max_swap_slippage_pct: 1.0
    swap_default_wallet_only: true
    min_swap_sol_reserve: 0.02
    allowed_swap_tokens: SOL,USDC,JUP,PYTH,RAY,BONK
default_trading_context: Solana DEX only, Gateway default wallet, jupiter/router on solana-mainnet-beta, USDC as cash; the limits in risk_limits are enforced in code
---

# Swing

Authoring this playbook starts nothing and changes no financial activation. Only an explicit
owner start runs it, at 15-minute intervals.

1. Read the journal and `[CURRENT CONFIG]`. Read the wallet with `get_portfolio_overview` and
   note SOL (keep it above the reserve), USDC, and every held token with its value. Missing
   balances or values block new entries. At your first tick of the UTC day, journal the wallet
   value as the day's opening value; at 5% below it, no new entries until 00:00 UTC and notify
   the owner (exits stay allowed).
2. Shortlist at most 5 tokens from the whitelist by recent move, volume and range, always
   including every token you hold. Read `get_market_data` or `explore_geckoterminal` for
   them: price, 24h change, range, volume, recent candles. Skip anything with too little
   volume for the order size.
3. For each held token, decide hold or exit: target reached, stop broken or thesis stale.
4. For the best new candidate, write a short thesis with a target, a stop and a window of 1 to
   24 hours. Skip it when the target is below 1.0% or below 1.5 times the round-trip cost.
5. For any trade you intend (an exit first, then at most one entry): call `quote_swap` for
   the exact connector `jupiter/router`, network `solana-mainnet-beta`, pair, side and base
   amount. Record price, impact and fees. Apply the cost bar from your instructions. A quote
   outside the bar means no swap: journal the near-miss with numbers.
6. Place the swap once with `execute_swap` (`connector`, `network`, `trading_pair`, `side`,
   `amount`, `slippage_pct`; no `wallet_address`). If the code refuses it, do nothing else:
   journal and notify. Never retry.
7. Read the result with `get_swap_status` for the transaction hash. Pending or unknown is not
   a fill: stop swapping until it is resolved (`resolve_swap` when stuck). Journal the hash,
   the filled amounts and the outcome.
8. Notify swap errors, code refusals, stuck swaps, the daily brake and a low SOL reserve; keep
   empty ticks quiet.
