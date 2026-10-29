/** Number and text helpers shared by the Trade page's venue panels. */

export type Summary = {
  total: number; resolved: number; open: number; hit_target: number; stopped: number; expired: number;
  avg_gross_pct: number | null; avg_net_pct: number | null; beat_costs: number;
};
export type Scorecard = {
  assumptions: { round_trip_cost_pct: number; basis: string };
  prices: { snapshots: number; coins: number; days: number };
  trade_calls: Summary;
  pass_calls: Summary;
  by_cost_multiple: { multiple: number; min_target_pct: number; resolved: number; avg_net_pct: number | null; win_rate: number | null }[];
  baseline_gross_pct: number | null;
  edge_vs_baseline_pct: number | null;
  enough_data: boolean;
  recent: { ts: number; symbol: string; verdict: string; target_pct: number; stop_pct: number; horizon_h: number; outcome: string; net_pct: number | null }[];
};

export const num = (value?: string | number | null) => {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};
export const digitsFor = (value: number) => (value >= 100 ? 2 : value >= 1 ? 3 : value >= 0.01 ? 4 : 8);
/** A price or amount in the ledger quote currency, with the currency named rather than assumed. */
export const money = (value: number | null, quote: string, digits = 2) =>
  value === null ? "—" : `${value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${quote}`.trim();
export const price = (value?: string | null) => {
  const parsed = num(value);
  return parsed === null ? "—" : parsed.toLocaleString(undefined, { maximumFractionDigits: digitsFor(parsed) });
};
/** A percentage with at most two decimals and no trailing zeros, so a float sum such as
 *  0.1 + 0.11 reads 0.21 and never 0.21000000000000002. */
export const pct = (value: number | null | undefined) =>
  value === null || value === undefined || !Number.isFinite(value) ? "—" : String(Number(value.toFixed(2)));
export const signed = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
