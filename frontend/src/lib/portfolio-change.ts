import type { PortfolioHistoryResponse } from "@/lib/api";

/**
 * The change shown beside a portfolio total.
 *
 * Never the first point against the last of the history series: that series is the
 * Hummingbot history plus the accounts Condor records itself, which start at different
 * times and hold deposits, so an account added three hours ago reads as growth from
 * nothing (+491% on a 55 EUR desk). The server works the figure out from sources that all
 * have a value at the window start, net of the recorded deposits and withdrawals, or says
 * why it cannot (`change.available: false`); this only carries that answer to the screen.
 */
export type HistoryChange =
  | { known: true; abs: number; pct: number | null; note: string | null }
  | { known: false; hint: string };

export const UNKNOWN_CHANGE_HINT =
  "No honest change for this window: the history starts at different times for different accounts, or deposits are not recorded, so a difference would read as a gain.";

export function historyChange(history: PortfolioHistoryResponse | undefined | null): HistoryChange {
  const change = history?.change;
  if (!change) return { known: false, hint: UNKNOWN_CHANGE_HINT };
  if (!change.available || change.change_usd == null || !Number.isFinite(change.change_usd)) {
    return { known: false, hint: change.note ? `${change.note} ${UNKNOWN_CHANGE_HINT}` : UNKNOWN_CHANGE_HINT };
  }
  const pct = change.change_pct;
  return {
    known: true,
    abs: change.change_usd,
    pct: pct != null && Number.isFinite(pct) ? pct : null,
    note: change.note ?? null,
  };
}
