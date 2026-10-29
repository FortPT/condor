/** A performance report as the server answers it, for the Trade panels' tests. */
import type { PerfWindow, Performance } from "./performance";

const win = (value: number | null, extra: Partial<PerfWindow> = {}): PerfWindow => ({
  value, currency: "USD", since: 1_793_145_600, since_label: "2026-10-28 00:00 UTC", partial: false, estimated: false,
  flows_usd: 0, reason: value === null ? "No equity history recorded yet" : null, note: null, ...extra,
});

export function makePerformance(currency: string, overrides: Partial<Performance> = {}): Performance {
  return {
    venue: "test", label: "Test", as_of: 1_793_200_000, currency, connected: true, synced: true, available: true,
    today: win(1.5), since_start: win(-3.25, { since_label: "2026-10-20 09:00 UTC" }),
    account_equity_usd: 110,
    equity: { now: 55, cash: 40, positions_cost: 12, unrealized: 3, currency, reason: null },
    realized: { value: 4.5, complete: true, reason: null, sells: 2, unknown_sells: 0, partial_history: false, partial_assets: [], history_starts_at: 1_793_100_000, currency },
    unrealized: { value: 3, currency, reason: null },
    fees: { value: 0.07, estimated: 0, is_estimate: false, unconverted: 0, complete: true, currency },
    trades: { filled_orders: 5, buys: 3, sells: 2, round_trips_closed: 1, wins: 1, losses: 1, flat: 0, win_rate: 0.5, best: null, worst: null },
    orders: [],
    notes: ["standing note one", "standing note two"],
    ...overrides,
  };
}
