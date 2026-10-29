import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { apiFetch, type CandleData } from "@/lib/api";
import { getThemeColors } from "@/lib/theme-colors";
import { num, pct, signed, type Scorecard, type Summary } from "./tradeFormat";

const card = "rounded-lg border border-[var(--color-border)] p-4";

export function Change({ value, label }: { value?: string | null; label?: string }) {
  const parsed = num(value);
  if (parsed === null) return <span className="text-[var(--color-text-muted)]">—</span>;
  return <span className={parsed >= 0 ? "text-[var(--color-green)]" : "text-[var(--color-red)]"}>
    {label}{parsed >= 0 ? "+" : ""}{parsed.toFixed(2)}%
  </span>;
}

export function CandleChart({ rows }: { rows: CandleData[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let disposed = false;
    let cleanup = () => {};
    void import("lightweight-charts").then((mod) => {
      if (disposed || !ref.current) return;
      const colors = getThemeColors();
      const chart = mod.createChart(ref.current, {
        autoSize: true,
        layout: { background: { type: mod.ColorType.Solid, color: colors.bg }, textColor: colors.text },
        grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
        timeScale: { timeVisible: true, secondsVisible: false },
        rightPriceScale: { borderVisible: false },
      });
      const ordered = [...rows].sort((a, b) => a.timestamp - b.timestamp)
        .filter((row, index, all) => index === 0 || row.timestamp !== all[index - 1].timestamp);
      // Cheap coins (SHIB, CRO) need more decimals on the price axis than BTC.
      const last = ordered.at(-1)?.close ?? 1;
      const precision = last >= 100 ? 2 : last >= 1 ? 3 : last >= 0.01 ? 5 : 8;
      const series = chart.addSeries(mod.CandlestickSeries, {
        upColor: colors.up, downColor: colors.down, borderVisible: false,
        wickUpColor: colors.up, wickDownColor: colors.down,
        priceFormat: { type: "price", precision, minMove: 10 ** -precision },
      });
      series.setData(ordered.map((row) => ({
        time: row.timestamp as import("lightweight-charts").UTCTimestamp,
        open: row.open, high: row.high, low: row.low, close: row.close,
      })));
      chart.timeScale().fitContent();
      cleanup = () => chart.remove();
    });
    return () => { disposed = true; cleanup(); };
  }, [rows]);
  return <div ref={ref} data-testid="exchange-chart" className="h-full w-full" />;
}

/** The trader's logged calls, scored against real prices after costs (calls, not placed orders). */
export function ScorecardPanel({ base, queryKey, venue = "Crypto.com", noun = "coins" }: {
  /** The venue's API prefix, for example `/api/v1/crypto-exchange`. */
  base: string;
  queryKey: string;
  venue?: string;
  noun?: string;
}) {
  const score = useQuery({
    queryKey: [queryKey],
    queryFn: () => apiFetch<Scorecard>(`${base}/scorecard`),
    refetchInterval: 60000,
    retry: false,
  });
  const data = score.data;
  if (!data?.trade_calls) {
    return <section className={card} aria-label="Scorecard">
      <h2 className="mb-1 font-semibold">Call scorecard</h2>
      <p className="text-sm text-[var(--color-text-muted)]">{score.isError ? "Scorecard unavailable." : "Loading scorecard…"}</p>
    </section>;
  }
  const resolved = data.trade_calls.resolved + data.pass_calls.resolved;
  const rows: [string, Summary][] = [["Calls to trade", data.trade_calls], ["Calls to pass (what it skipped)", data.pass_calls]];
  return <section className={card} aria-label="Scorecard">
    <h2 className="mb-1 font-semibold">Call scorecard</h2>
    <p className="text-sm">
      Each call the trader logs ({noun === "coins" ? "coin" : "stock"}, target, stop, time window) is scored later against real {venue} prices after a {pct(data.assumptions.round_trip_cost_pct)}% round-trip cost. It scores the calls, not the trades that were actually placed; no money moves.
    </p>
    <p className="mt-1 text-xs text-[var(--color-text-muted)]">
      Tracking {data.prices.coins} {noun} · {data.prices.snapshots} price snapshots over {data.prices.days} days · {resolved} of 30 resolved calls needed before trusting the result{data.enough_data ? " (reached)" : ""}.
    </p>
    <table className="mt-3 w-full text-left text-sm">
      <thead><tr><th>Calls</th><th>Logged</th><th>Resolved</th><th>Open</th><th>Hit target</th><th>Stopped</th><th>Avg net</th><th>Beat costs</th></tr></thead>
      <tbody>{rows.map(([label, row]) => <tr key={label}>
        <td>{label}</td><td>{row.total}</td><td>{row.resolved}</td><td>{row.open}</td><td>{row.hit_target}</td><td>{row.stopped}</td>
        <td>{signed(row.avg_net_pct)}</td><td>{row.beat_costs}/{row.resolved}</td>
      </tr>)}</tbody>
    </table>
    <p className="mt-2 text-sm">
      Baseline (average of all listed {noun} over the same windows): {signed(data.baseline_gross_pct)} · Edge vs baseline: {signed(data.edge_vs_baseline_pct)}
    </p>
    <h3 className="mb-1 mt-3 text-sm font-medium">What cost multiple would have worked?</h3>
    <table className="w-full text-left text-sm">
      <thead><tr><th>Target at least</th><th>Resolved</th><th>Avg net</th><th>Win rate</th></tr></thead>
      <tbody>{data.by_cost_multiple.map((row) => <tr key={row.multiple}>
        <td>{row.multiple}× cost ({row.min_target_pct.toFixed(1)}%)</td><td>{row.resolved}</td><td>{signed(row.avg_net_pct)}</td>
        <td>{row.win_rate === null ? "—" : `${Math.round(row.win_rate * 100)}%`}</td>
      </tr>)}</tbody>
    </table>
    {data.recent.length > 0 && <>
      <h3 className="mb-1 mt-3 text-sm font-medium">Latest calls</h3>
      <ul className="text-sm">{data.recent.slice(0, 5).map((row, index) => <li key={`${row.ts}-${row.symbol}-${index}`}>
        {row.symbol} · {row.verdict} · target +{row.target_pct}% / stop −{row.stop_pct}% in {row.horizon_h}h · {row.outcome}{row.net_pct === null ? "" : ` (${signed(row.net_pct)} net)`}
      </li>)}</ul>
    </>}
  </section>;
}
