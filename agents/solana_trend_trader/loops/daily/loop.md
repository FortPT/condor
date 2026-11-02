---
name: Daily trend
description: Every hour compare the daily SOL signal with the wallet and swap one step towards the rule when they differ
default_config:
  frequency_sec: 3600
  tick_timeout_sec: 600
  execution_mode: loop
  total_amount_quote: 30
  quote_asset: USDC
  restart_on_boot: true
  max_ticks: 0
  risk_limits:
    # USDC, the quote of the only pair this loop trades.
    max_position_size_quote: 15
    max_swap_quote: 12
    max_swap_slippage_pct: 1.0
    swap_default_wallet_only: true
    min_swap_sol_reserve: 0.02
    allowed_swap_tokens: SOL,USDC
default_trading_context: Solana DEX only, Gateway default wallet, jupiter/router on solana-mainnet-beta, SOL against USDC; the signal is computed in code and the limits in risk_limits are enforced in code
---

# Daily trend

Runs every hour. The signal only changes with a new closed UTC day; the repeat is there because
the swap limits cap each swap, so moving a whole wallet takes several ticks.

1. Read `solana_trend()` and the journal. If an earlier swap is pending or unknown, resolve it
   first (`get_swap_status`, then `resolve_swap` when stuck) and place nothing else.
2. Follow your instructions for the `plan.action`: `sell_sol` or `buy_sol` means one quoted swap
   within the limits; `hold` and `none` mean nothing.
3. Journal the tick. Notify swaps, refusals, stuck swaps and repeated missing data.

Authoring this playbook starts nothing and changes no financial activation. Only an explicit
owner start runs it, at one-hour intervals.
