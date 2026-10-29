import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api";
import { num } from "./tradeFormat";

export type MakerStats = {
  attempts: number;
  outcomes: Record<string, number>;
  resolved: number;
  fill_rate: number | null;
  any_fill_rate: number | null;
  avg_seconds_to_fill: number | null;
  avg_fee_pct: number | null;
  fee_saved_vs_taker: number | null;
  unfilled_notional: number | null;
  quote: string | null;
};
type MakerStatsResponse = { maker_enabled: boolean; stats?: MakerStats };

const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40";
const card = "rounded-lg border border-[var(--color-border)] p-4";
const percent = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : `${(value * 100).toFixed(0)}%`;
const fixed = (value: number | null | undefined, digits: number, unit = "") =>
  value === null || value === undefined || num(value) === null ? "—" : `${value.toFixed(digits)}${unit}`;

/** Read-only maker-order results, plus the owner's switch (off by default; the crew cannot flip it). */
export function MakerStatsPanel({ base, quote }: { base: string; quote: string }) {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["crypto-exchange-maker-stats"],
    queryFn: () => apiFetch<MakerStatsResponse>(`${base}/maker-stats`),
    refetchInterval: 60000,
    retry: false,
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => apiFetch(`${base}/maker-mode`, { method: "PUT", body: JSON.stringify({ enabled }) }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["crypto-exchange-maker-stats"] }),
    retry: false,
  });
  const data = query.data;
  const stats = data?.stats;
  if (!stats) {
    return <section className={card} aria-label="Maker orders">
      <h2 className="mb-1 font-semibold">Maker orders</h2>
      <p className="text-sm text-[var(--color-text-muted)]">{query.isError ? "Maker statistics unavailable." : "Loading maker statistics…"}</p>
    </section>;
  }
  const enabled = data.maker_enabled === true;
  const outcomes = stats.outcomes ?? {};
  const unit = stats.quote ?? quote;
  return <section className={card} aria-label="Maker orders">
    <div className="flex flex-wrap items-center gap-3">
      <h2 className="font-semibold">Maker orders</h2>
      <span className="text-sm">{enabled ? "allowed" : "off"}</span>
      <button className={`${button} ml-auto`} disabled={toggle.isPending} onClick={() => toggle.mutate(!enabled)}>
        {enabled ? "Turn maker orders off" : "Allow maker orders"}
      </button>
    </div>
    {toggle.error && <p role="alert" className="mt-1 text-sm text-[var(--color-red)]">{toggle.error.message}</p>}
    <p className="mt-1 text-xs text-[var(--color-text-muted)]">
      Optional resting (post-only) orders at the best bid or ask, cancelled after a short wait. They pay the lower maker fee but may not fill. Nothing has been tried live yet: the first use should be one tiny order you approve.
    </p>
    {stats.attempts === 0
      ? <p className="mt-2 text-sm">No maker attempts yet.</p>
      : <table className="mt-2 w-full text-left text-sm">
        <thead><tr><th>Attempts</th><th>Filled</th><th>Partial</th><th>Unfilled</th><th>Refused</th><th>Fill rate</th><th>Any fill</th><th>Avg fee</th><th>Fee saved vs taker</th><th>Avg time to fill</th></tr></thead>
        <tbody><tr>
          <td>{stats.attempts}</td><td>{outcomes.filled ?? 0}</td><td>{outcomes.partial ?? 0}</td><td>{outcomes.unfilled ?? 0}</td>
          <td>{(outcomes.rejected ?? 0) + (outcomes.uncertain ?? 0)}</td>
          <td>{percent(stats.fill_rate)}</td><td>{percent(stats.any_fill_rate)}</td>
          <td>{fixed(stats.avg_fee_pct, 3, "%")}</td><td>{fixed(stats.fee_saved_vs_taker, 4, ` ${unit}`)}</td>
          <td>{fixed(stats.avg_seconds_to_fill, 1, " s")}</td>
        </tr></tbody>
      </table>}
    {stats.attempts > 0 && <p className="mt-2 text-xs text-[var(--color-text-muted)]">
      Fill rate counts complete fills among placed orders that finished; refused includes uncertain attempts. Fee saved ignores moves missed by unfilled orders ({fixed(stats.unfilled_notional, 2, ` ${unit}`)} left unfilled so far).
    </p>}
  </section>;
}
