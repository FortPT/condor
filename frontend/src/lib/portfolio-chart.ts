import type {
  PortfolioChartPoint,
  PortfolioFlowMarker,
  PortfolioHistoryGap,
  PortfolioHistoryResponse,
} from "@/lib/api";

/**
 * What the Portfolio Evolution chart draws, and the words that explain it.
 *
 * The merged `points` series steps up whenever an account starts being recorded and
 * whenever money is deposited, which reads as performance. The server's `chart_points`
 * start when every account is recording, hold `null` where an account had no reading
 * (a break in the line, never a filled value) and come with the deposits as markers and
 * a trading-result series net of them (`condor/portfolio_chart.py`). Nothing here
 * estimates, fills or converts a value: it only decides what to show and how to say it.
 */

export type ChartMode = "value" | "result";

/** A chart point whose total is `null` where a source had no reading. */
export type SeriesPoint = {
  timestamp: number;
  value: number | null;
  tokens?: Record<string, number>;
};

/** The series the chart draws, or `null` when the server did not send one (older server). */
export function chartSeries(history: PortfolioHistoryResponse | undefined | null): PortfolioChartPoint[] | null {
  const series = history?.chart_points;
  return Array.isArray(series) ? series : null;
}

/** The points to draw: the server's chart series, else the plain history points. */
export function valuePoints(history: PortfolioHistoryResponse | undefined | null): SeriesPoint[] {
  const series = chartSeries(history);
  const raw = series ?? history?.points ?? [];
  return raw.map((p) => ({
    timestamp: p.timestamp,
    value: p.total_usd == null || !Number.isFinite(p.total_usd) ? null : p.total_usd,
    tokens: p.tokens,
  }));
}

/** The trading-result series (cumulative, net of confirmed flows), or `null` with no result. */
export function resultPoints(history: PortfolioHistoryResponse | undefined | null): SeriesPoint[] | null {
  const series = history?.result_points;
  if (!Array.isArray(series)) return null;
  return series.map((p) => ({
    timestamp: p.timestamp,
    value: p.result_usd == null || !Number.isFinite(p.result_usd) ? null : p.result_usd,
  }));
}

/** The points actually drawn for a mode: the result falls back to the value series. */
export function seriesFor(history: PortfolioHistoryResponse | undefined | null, mode: ChartMode): SeriesPoint[] {
  return (mode === "result" ? resultPoints(history) : null) ?? valuePoints(history);
}

/** Runs of consecutive points with a value: the line is drawn per run, so a gap is a break. */
export function segments<T extends { value: number | null }>(points: T[]): (T & { value: number })[][] {
  const out: (T & { value: number })[][] = [];
  let run: (T & { value: number })[] = [];
  for (const p of points) {
    if (p.value == null) {
      if (run.length) out.push(run);
      run = [];
    } else {
      run.push(p as T & { value: number });
    }
  }
  if (run.length) out.push(run);
  return out;
}

/** How many points have a value: fewer than two cannot draw a line. */
export function drawablePoints(points: SeriesPoint[]): number {
  return points.reduce((n, p) => (p.value == null ? n : n + 1), 0);
}

/**
 * `"empty"` when the server sent a chart series with no common period yet (fewer than two
 * readings in which every account was recording); `"none"` when there is no history at
 * all; otherwise `"line"`. A single point is never drawn as a line.
 */
export type ChartState = "line" | "empty" | "none";

export function chartState(history: PortfolioHistoryResponse | undefined | null): ChartState {
  const series = chartSeries(history);
  if (series === null) {
    // An older server: the plain points, as before.
    const n = history?.points?.length ?? 0;
    return n === 0 ? "none" : "line";
  }
  if (series.length === 0 && (history?.points?.length ?? 0) === 0) return "none";
  return drawablePoints(valuePoints(history)) >= 2 ? "line" : "empty";
}

/** Sources that are recording: what "all N accounts" counts. */
export function recordingCount(history: PortfolioHistoryResponse | undefined | null): number {
  return (history?.sources ?? []).filter((s) => s.status === "recording").length;
}

/** `Showing from <time>, when all <N> accounts were recording. ...`, or `null`. */
export function baselineNote(
  history: PortfolioHistoryResponse | undefined | null,
  formatTime: (ts: number) => string,
): string | null {
  if (!history || history.series_start_reason !== "all_accounts_recording_from") return null;
  const from = history.series_start_ts ?? history.baseline_ts;
  if (from == null) return null;
  const n = recordingCount(history);
  const accounts = n === 1 ? "1 account was" : `all ${n} accounts were`;
  return `Showing from ${formatTime(from)}, when ${accounts} recording. Earlier points only had some accounts.`;
}

/** Accounts that are connected but have no reading yet, which the chart does not wait for. */
export function pendingNote(history: PortfolioHistoryResponse | undefined | null): string | null {
  const pending = (history?.sources ?? []).filter((s) => s.status === "pending").map((s) => s.source);
  if (!pending.length) return null;
  return `${pending.join(", ")} ${pending.length === 1 ? "has" : "have"} no recorded value yet and ${
    pending.length === 1 ? "is" : "are"
  } not in this chart.`;
}

/** What the empty state says when there is no common recording period. */
export function emptyMessage(history: PortfolioHistoryResponse | undefined | null): string {
  const pending = pendingNote(history);
  const base =
    "Not enough history yet: the chart needs at least two readings in which every account was recording, so it does not draw a line from a single point.";
  return pending ? `${base} ${pending}` : base;
}

/** `Deposit +4.27 EUR, Revolut X (confirmed)`. */
export function flowLabel(flow: PortfolioFlowMarker): string {
  const amount = String(flow.amount ?? "").trim();
  const negative = amount.startsWith("-");
  const digits = amount.replace(/^[+-]/, "");
  const sign = negative ? "-" : "+";
  return `${negative ? "Withdrawal" : "Deposit"} ${sign}${digits} ${flow.currency}, ${flow.venue} (${flow.status || "confirmed"})`;
}

/** A line under the chart for one gap. */
export function gapLabel(gap: PortfolioHistoryGap, formatTime: (ts: number) => string): string {
  return `${gap.source}: no readings from ${formatTime(gap.from_ts)} to ${formatTime(gap.to_ts)}`;
}

/**
 * Where on the line a flow marker sits: its time, and the line's value there, between the
 * two points around it. `null` when the flow is outside the drawn range or falls inside a
 * gap (no value to sit on); the marker is then left off rather than placed on a guess.
 */
export function markerAnchor(points: SeriesPoint[], ts: number): { ts: number; value: number } | null {
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    if (ts < a.timestamp || ts > b.timestamp) continue;
    if (a.value == null || b.value == null) return null;
    const span = b.timestamp - a.timestamp;
    const t = span > 0 ? (ts - a.timestamp) / span : 0;
    return { ts, value: a.value + (b.value - a.value) * t };
  }
  const only = points.length === 1 && points[0].timestamp === ts && points[0].value != null;
  return only ? { ts, value: points[0].value as number } : null;
}

/** Whether the Result toggle can be used, and what to tell the reader when it cannot. */
export function resultToggle(
  history: PortfolioHistoryResponse | undefined | null,
  stacked: boolean,
): { enabled: boolean; hint: string } {
  if (stacked) return { enabled: false, hint: "The trading result is shown on the line chart, not the stacked view." };
  if (resultPoints(history)) {
    const note = history?.result_note;
    return {
      enabled: true,
      hint: note
        ? `Change since the chart start, net of confirmed deposits and withdrawals. ${note}`
        : "Change since the chart start, net of confirmed deposits and withdrawals.",
    };
  }
  const why = history?.result_note;
  return {
    enabled: false,
    hint: why
      ? `Trading result not available: ${why}`
      : "Trading result not available: deposits and withdrawals cannot be taken out honestly for this window.",
  };
}
