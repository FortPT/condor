/** The limits the server reports for the bot allocation (`GET /crypto-com/status`). */
export type CryptoComLimits = {
  floor_eur?: string;
  reserve_fraction?: string;
  position_fraction?: string;
  max_positions?: number;
  guards_apply_from_eur?: string;
  max_asset_fraction_below_threshold?: string;
};

const percent = (value?: string) => `${Math.round(Number(value) * 100)}%`;

/**
 * One sentence per regime, built from what the server enforces so the page can
 * never describe limits the service no longer applies.
 */
export function limitsSummary(limits?: CryptoComLimits): string {
  if (!limits) return "";
  const floor = limits.floor_eur ?? "2";
  const strict = `${percent(limits.reserve_fraction)} of bot equity stays in cash, at most ${limits.max_positions} positions and ${limits.position_fraction} of equity per position`;
  if (limits.guards_apply_from_eur && limits.max_asset_fraction_below_threshold) {
    return `Below €${limits.guards_apply_from_eur} total bot equity there is no cash reserve and no position-count limit, but no single coin may exceed ${percent(limits.max_asset_fraction_below_threshold)} of bot equity. From €${limits.guards_apply_from_eur}: ${strict}. New exposure stops at €${floor} total bot equity. Profits increase what the bot can use.`;
  }
  return `${strict}. New exposure stops at €${floor} total bot equity. Profits increase position sizes.`;
}
