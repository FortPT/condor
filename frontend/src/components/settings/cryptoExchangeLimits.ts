/** The limits the server reports for the bot allocation (`GET /crypto-exchange/status`). */
export type CryptoExchangeLimits = {
  floor?: string;
  reserve_fraction?: string;
  position_fraction?: string;
  max_positions?: number;
  guards_apply_from?: string;
  max_asset_fraction_below_threshold?: string;
  default_slippage_pct?: string;
};

const percent = (value?: string) => `${Math.round(Number(value) * 100)}%`;

/**
 * One sentence per regime, built from what the server enforces so the page can
 * never describe limits the service no longer applies. Amounts are in the
 * ledger quote currency (`status.ledger.quote`).
 */
export function limitsSummary(limits?: CryptoExchangeLimits, quote = ""): string {
  if (!limits) return "";
  const money = (value: string) => `${value} ${quote}`.trim();
  const floor = money(limits.floor ?? "2");
  const strict = `${percent(limits.reserve_fraction)} of bot equity stays in cash, at most ${limits.max_positions} positions and ${limits.position_fraction} of equity per position`;
  const slippage = limits.default_slippage_pct
    ? ` Orders are immediate-or-cancel limits, priced at most ${limits.default_slippage_pct}% through the touch by default.`
    : "";
  if (limits.guards_apply_from && limits.max_asset_fraction_below_threshold) {
    const from = money(limits.guards_apply_from);
    return `Below ${from} total bot equity there is no cash reserve and no position-count limit, but no single coin may exceed ${percent(limits.max_asset_fraction_below_threshold)} of bot equity. From ${from}: ${strict}. New exposure stops at ${floor} total bot equity. Profits increase what the bot can use.${slippage}`;
  }
  return `${strict}. New exposure stops at ${floor} total bot equity. Profits increase position sizes.${slippage}`;
}
