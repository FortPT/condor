import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import {
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Wallet,
  Server,
  BarChart3,
  Layers,
  KeyRound,
  ArrowUpRight,
  ArrowDownRight,
} from "lucide-react";
import { useState, useEffect, useMemo, useCallback } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { NoServerCard } from "@/components/NoServerCard";
import { PositionsTab } from "@/components/portfolio/PositionsTab";
import { useLpPositions } from "@/hooks/useLpPositions";
import { useRates } from "@/hooks/useRates";
import { useServer } from "@/hooks/useServer";
import { useCondorWebSocket } from "@/hooks/useWebSocket";
import {
  api,
  type AgentSummary,
  type BalanceItem,
  type ConnectorBalance,
  type ExecutorPeriodSummary,
  type PortfolioFlowMarker,
  type PortfolioHistoryResponse,
} from "@/lib/api";
import { formatCurrency, formatCurrencyPnl, formatCurrencyVolume, isExecutorActive } from "@/lib/formatters";
import { historyChange } from "@/lib/portfolio-change";
import {
  baselineNote,
  chartState,
  emptyMessage,
  flowLabel,
  gapLabel,
  markerAnchor,
  pendingNote,
  resultToggle,
  segments,
  seriesFor,
  type ChartMode,
} from "@/lib/portfolio-chart";
import { agentsQuery, EXECUTORS_REFETCH_MS, executorsQuery } from "@/lib/queryClient";
import { ACCOUNT_EQUITY } from "@/lib/strategy-unavailable";
import { getThemeColors } from "@/lib/theme-colors";

// ── Formatters ──

function formatAmount(val: number) {
  if (val === 0) return "0";
  if (Math.abs(val) >= 1_000_000) return (val / 1_000_000).toFixed(2) + "M";
  if (Math.abs(val) >= 10_000) return (val / 1_000).toFixed(1) + "K";
  if (Math.abs(val) < 0.001) return val.toExponential(2);
  return val.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

function formatTokenPrice(val: number, symbol = "$") {
  if (val === 0) return "-";
  if (val >= 1000) return symbol + val.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (val >= 1) return symbol + val.toFixed(2);
  if (val >= 0.01) return symbol + val.toFixed(4);
  return symbol + val.toExponential(2);
}

// ── Chart Colors ──

function getChartColors() {
  const tc = getThemeColors();
  return [
    "#d4a845", // gold
    tc.green,   // green (theme-aware)
    "#6366f1", // indigo
    tc.red,     // red (theme-aware)
    "#06b6d4", // cyan
    "#8b5cf6", // violet
    "#ec4899", // pink
    "#14b8a6", // teal
    "#f97316", // orange
    "#a78bfa", // light violet
  ];
}

// ── KPI helpers ──

const TIME_PERIODS = ["1D", "1W", "1M"] as const;

// ── Unified Dashboard Strip ──

function KpiCell({
  label,
  mainValue,
  pnl,
  details,
  currencySymbol,
  pnlTitle,
  pnlUnknownTitle,
}: {
  label: string;
  mainValue: string | number;
  pnl?: number;
  details: string;
  currencySymbol?: string;
  pnlTitle?: string;
  /** When set and there is no `pnl`: show a dash with this hover instead of nothing. */
  pnlUnknownTitle?: string;
}) {
  return (
    <div className="flex-1 px-5 py-3 min-w-0">
      <span className="text-[11px] uppercase tracking-wider font-medium text-[var(--color-text-muted)]">{label}</span>
      <div className="flex items-baseline gap-2.5 mt-1">
        <span className="text-2xl font-bold tabular-nums tracking-tight leading-none">{mainValue}</span>
        {pnl !== undefined && pnl !== null && (
          <span className="inline-flex items-center gap-0.5 text-sm tabular-nums font-semibold"
            title={pnlTitle}
            style={{ color: pnl >= 0 ? "var(--color-green)" : "var(--color-red)" }}>
            {pnl >= 0 ? <ArrowUpRight className="h-3.5 w-3.5" /> : <ArrowDownRight className="h-3.5 w-3.5" />}
            {formatCurrencyPnl(pnl, currencySymbol || "$")}
          </span>
        )}
        {(pnl === undefined || pnl === null) && pnlUnknownTitle && (
          <span
            className="text-sm tabular-nums text-[var(--color-text-muted)]"
            data-testid="portfolio-change-unknown"
            title={pnlUnknownTitle}
          >
            —
          </span>
        )}
      </div>
      <div className="text-xs text-[var(--color-text-muted)] mt-1 truncate">{details}</div>
    </div>
  );
}

function DashboardStrip({
  totalUsd,
  totalTokens,
  connectorCount,
  portfolioPnl,
  portfolioPnlTitle,
  portfolioPnlUnknownTitle,
  botCount,
  controllerCount,
  botPnl,
  botVolume,
  activeExecutorCount,
  execStats,
  agents,
  period,
  onPeriodChange,
  onNavigate,
  convertFromUsd,
  currencySymbol,
}: {
  totalUsd: number;
  totalTokens: number;
  connectorCount: number;
  portfolioPnl: number | null;
  portfolioPnlTitle?: string;
  portfolioPnlUnknownTitle?: string;
  botCount: number;
  controllerCount: number;
  botPnl: number;
  botVolume: number;
  activeExecutorCount: number;
  execStats: ExecutorPeriodSummary | undefined;
  agents: AgentSummary[];
  period: string;
  onPeriodChange: (p: string) => void;
  onNavigate: (path: string) => void;
  convertFromUsd: (val: number) => number;
  currencySymbol: string;
}) {
  const activeAgents = agents.filter((a) => a.status === "running" || a.status === "active");
  const agentPnl = agents.reduce((s, a) => s + (a.latest_session_pnl ?? 0), 0);
  const agentSessions = agents.reduce((s, a) => s + (a.session_count ?? 0), 0);

  const btnClass = "flex items-center justify-center gap-1.5 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-1.5 text-[11px] font-medium text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-text)] w-full";

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
      <div className="flex items-stretch divide-x divide-[var(--color-border)]">
        <KpiCell
          label="Portfolio"
          mainValue={formatCurrency(convertFromUsd(totalUsd), currencySymbol)}
          pnl={portfolioPnl != null ? convertFromUsd(portfolioPnl) : undefined}
          pnlTitle={portfolioPnlTitle}
          pnlUnknownTitle={portfolioPnlUnknownTitle}
          details={`${totalTokens} assets · ${connectorCount} connector${connectorCount !== 1 ? "s" : ""}`}
          currencySymbol={currencySymbol}
        />

        <KpiCell
          label="Bots"
          mainValue={botCount}
          pnl={botPnl}
          details={`${controllerCount} controller${controllerCount !== 1 ? "s" : ""} · vol ${formatCurrencyVolume(botVolume, currencySymbol)}`}
          currencySymbol={currencySymbol}
        />

        <KpiCell
          label="Executors"
          mainValue={`${activeExecutorCount} active`}
          pnl={execStats ? convertFromUsd(execStats.pnl) : undefined}
          details={
            execStats
              ? `${execStats.count} in ${period} · vol ${formatCurrencyVolume(convertFromUsd(execStats.volume), currencySymbol)}${execStats.converted ? "" : " ⚠"}`
              : `Loading ${period}…`
          }
          currencySymbol={currencySymbol}
        />

        <KpiCell
          label="Agents"
          mainValue={activeAgents.length}
          pnl={convertFromUsd(agentPnl)}
          pnlTitle={
            agents.some((a) => a.pnl_basis === ACCOUNT_EQUITY)
              ? agents.some((a) => a.pnl_basis === ACCOUNT_EQUITY && !a.pnl_flows_tracked)
                ? "Includes the change in account equity of native crews (Crypto.com Exchange, Kraken xStocks, Revolut X); deposits and withdrawals are only subtracted where they are recorded"
                : "Includes the trading result of native crews (Crypto.com Exchange, Kraken xStocks, Revolut X): the change in account equity net of recorded deposits and withdrawals"
              : undefined
          }
          details={`${agents.length} total · ${agentSessions} session${agentSessions !== 1 ? "s" : ""}`}
          currencySymbol={currencySymbol}
        />

        {/* ── Period + Quick Links ── */}
        <div className="flex flex-col justify-center gap-1 px-2.5 py-2 shrink-0 w-[100px]">
          <div className="flex gap-0.5 rounded-md border border-[var(--color-border)] p-0.5 bg-[var(--color-bg)] w-full justify-center">
            {TIME_PERIODS.map((p) => (
              <button
                key={p}
                onClick={() => onPeriodChange(p)}
                className={`px-1.5 py-1 text-[10px] rounded font-medium transition-colors flex-1 ${
                  period === p
                    ? "bg-[var(--color-accent)] text-white shadow-sm"
                    : "text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                }`}
              >
                {p}
              </button>
            ))}
          </div>
          <button onClick={() => onNavigate("/settings?tab=keys")} className={btnClass}>
            <KeyRound className="h-3 w-3" />
            Keys
          </button>
          <button onClick={() => onNavigate("/settings?tab=servers")} className={btnClass}>
            <Server className="h-3 w-3" />
            Servers
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Horizontal Bar Chart ──

function TokenBarChart({
  tokens,
  title,
  totalPortfolioValue,
}: {
  tokens: { token: string; usd_value: number; connector: string }[];
  title: string;
  totalPortfolioValue: number;
}) {
  if (tokens.length === 0) return null;
  const maxVal = tokens[0]?.usd_value ?? 0;
  const chartColors = getChartColors();

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4 h-full flex flex-col">
      <h3 className="text-sm font-medium text-[var(--color-text-muted)] mb-3">{title}</h3>
      <div className="flex flex-col gap-2.5 flex-1 justify-center">
        {tokens.map((t, i) => {
          const barPct = maxVal > 0 ? (t.usd_value / maxVal) * 100 : 0;
          const allocPct = totalPortfolioValue > 0 ? (t.usd_value / totalPortfolioValue) * 100 : 0;
          const color = chartColors[i % chartColors.length];
          return (
            <div key={`${t.connector}-${t.token}`} className="flex items-center gap-2">
              <div className="flex items-center gap-1.5 w-16 justify-end shrink-0">
                <div
                  className="h-2.5 w-2.5 rounded-sm shrink-0"
                  style={{ backgroundColor: color }}
                />
                <span className="text-xs font-medium truncate">{t.token}</span>
              </div>
              <div className="flex-1 h-5 rounded bg-[var(--color-bg)] overflow-hidden relative">
                <div
                  className="h-full rounded transition-all duration-500"
                  style={{
                    width: `${Math.max(barPct, 2)}%`,
                    backgroundColor: color,
                    opacity: 0.8,
                  }}
                />
              </div>
              <span className="text-xs tabular-nums text-[var(--color-text-muted)] shrink-0 text-right">
                {allocPct.toFixed(1)}%
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Allocation Bar ──

function AllocationBar({ pct }: { pct: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-[var(--color-border)]">
        <div
          className="h-full rounded-full bg-[var(--color-accent)]"
          style={{ width: `${Math.min(pct, 100)}%` }}
        />
      </div>
      <span className="w-12 text-right text-xs tabular-nums text-[var(--color-text-muted)]">
        {pct < 0.1 ? "<0.1" : pct.toFixed(1)}%
      </span>
    </div>
  );
}

// ── Table Rows ──

function TokenRow({
  b,
  totalPortfolio,
  convertFromUsd,
  currencySymbol,
}: {
  b: BalanceItem;
  totalPortfolio: number;
  convertFromUsd: (val: number) => number;
  currencySymbol: string;
}) {
  const price = b.total > 0 ? b.usd_value / b.total : 0;
  const pct = totalPortfolio > 0 ? (b.usd_value / totalPortfolio) * 100 : 0;

  return (
    <tr className="border-b border-[var(--color-border)]/30 last:border-0 hover:bg-[var(--color-surface-hover)]/50">
      <td className="py-2 pl-11 pr-4">
        <span className="text-sm font-medium">{b.token}</span>
      </td>
      <td className="px-4 py-2 text-right text-sm tabular-nums">{formatAmount(b.total)}</td>
      <td className="px-4 py-2 text-right text-sm tabular-nums text-[var(--color-text-muted)]">
        {formatTokenPrice(convertFromUsd(price), currencySymbol)}
      </td>
      <td className="px-4 py-2 text-right text-sm tabular-nums font-medium">
        {formatCurrency(convertFromUsd(b.usd_value), currencySymbol)}
      </td>
      <td className="px-4 py-2">
        <AllocationBar pct={pct} />
      </td>
    </tr>
  );
}

function ConnectorRow({
  connector,
  totalPortfolio,
  convertFromUsd,
  currencySymbol,
}: {
  connector: ConnectorBalance;
  totalPortfolio: number;
  convertFromUsd: (val: number) => number;
  currencySymbol: string;
}) {
  const [expanded, setExpanded] = useState(true);
  const Chevron = expanded ? ChevronDown : ChevronRight;
  const pct = totalPortfolio > 0 ? (connector.total_usd / totalPortfolio) * 100 : 0;

  return (
    <>
      <tr
        className="cursor-pointer border-b border-[var(--color-border)] bg-[var(--color-surface)] hover:bg-[var(--color-surface-hover)]"
        onClick={() => setExpanded(!expanded)}
      >
        <td className="px-4 py-3">
          <div className="flex items-center gap-2">
            <Chevron className="h-4 w-4 text-[var(--color-text-muted)]" />
            <span className="font-semibold">{connector.connector}</span>
            <span className="text-xs text-[var(--color-text-muted)]">
              {connector.balances.length} token{connector.balances.length !== 1 ? "s" : ""}
            </span>
            {connector.note && (
              <span
                title={connector.note}
                className="rounded bg-[var(--color-surface-hover)] px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]"
              >
                {connector.connector === "Crypto.com Exchange" ? "Exchange account" : "unified"}
              </span>
            )}
          </div>
        </td>
        <td className="px-4 py-3" />
        <td className="px-4 py-3" />
        <td className="px-4 py-3 text-right font-semibold tabular-nums">
          {formatCurrency(convertFromUsd(connector.total_usd), currencySymbol)}
        </td>
        <td className="px-4 py-3">
          <span className="text-sm tabular-nums text-[var(--color-text-muted)]">
            {pct.toFixed(1)}%
          </span>
        </td>
      </tr>
      {expanded && connector.note && (
        <tr className="border-b border-[var(--color-border)] bg-[var(--color-surface)]">
          <td colSpan={5} className="px-4 py-2.5 pl-10 text-xs italic text-[var(--color-text-muted)]">
            {connector.note}
          </td>
        </tr>
      )}
      {expanded &&
        connector.balances.map((b) => (
          <TokenRow key={b.token} b={b} totalPortfolio={totalPortfolio} convertFromUsd={convertFromUsd} currencySymbol={currencySymbol} />
        ))}
    </>
  );
}

// ── Portfolio Evolution Chart ──

function formatAxisTime(ts: number, range: string) {
  const d = new Date(ts * 1000);
  if (range === "1D") return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
  if (range === "1W") return d.toLocaleDateString("en-US", { weekday: "short" });
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function formatTooltipDate(ts: number, range: string) {
  const d = new Date(ts * 1000);
  if (range === "1D")
    return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function formatNoteTime(ts: number) {
  return new Date(ts * 1000).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

type DrawnPoint = { timestamp: number; value: number; tokens: Record<string, number> | undefined };

function PortfolioEvolution({ server, range, convertFromUsd, currencySymbol }: { server: string; range: string; convertFromUsd: (val: number) => number; currencySymbol: string }) {
  const [stacked, setStacked] = useState(false);
  const [mode, setMode] = useState<ChartMode>("value");
  const [hover, setHover] = useState<{ x: number; point: DrawnPoint } | null>(null);
  const [hoverFlow, setHoverFlow] = useState<{ x: number; y: number; label: string } | null>(null);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!server) return;
    TIME_PERIODS.forEach((r) => {
      queryClient.prefetchQuery({
        queryKey: ["portfolio-history", server, r],
        queryFn: () => api.getPortfolioHistory(server, r),
        staleTime: 60000,
      });
    });
  }, [server, queryClient]);

  const { data, isLoading } = useQuery({
    queryKey: ["portfolio-history", server, range],
    queryFn: () => api.getPortfolioHistory(server, range),
    enabled: !!server,
    refetchInterval: 60000,
  });

  const { data: breakdownData } = useQuery({
    queryKey: ["portfolio-history-breakdown", server, range],
    queryFn: () => api.getPortfolioHistory(server, range, true),
    enabled: !!server && stacked,
    refetchInterval: 60000,
  });

  const activeData: PortfolioHistoryResponse | undefined = stacked && breakdownData ? breakdownData : data;
  const topTokens = activeData?.top_tokens ?? [];

  // The chart draws the server's series from the point every account was recording; the
  // Result view is the same readings net of confirmed deposits and withdrawals, and is
  // only offered when those can be taken out honestly.
  const toggle = resultToggle(activeData, stacked);
  const effectiveMode: ChartMode = toggle.enabled && mode === "result" ? "result" : "value";
  const state = chartState(activeData);
  const baselineText = baselineNote(activeData, formatNoteTime);
  const pendingText = pendingNote(activeData);
  const flows = !stacked ? (activeData?.flows ?? []) : [];
  const gaps = activeData?.gaps ?? [];

  // Convert all values from USDT to display currency (a null stays a break)
  const points = useMemo(() => {
    const raw = seriesFor(activeData, effectiveMode);
    return raw.map((p) => ({
      timestamp: p.timestamp,
      value: p.value == null ? null : convertFromUsd(p.value),
      tokens: p.tokens
        ? Object.fromEntries(Object.entries(p.tokens).map(([k, v]) => [k, convertFromUsd(v)]))
        : undefined,
    }));
  }, [activeData, effectiveMode, convertFromUsd]);

  const W = 800;
  const H = 250;
  const PAD = { top: 20, right: 16, bottom: 30, left: 64 };
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;

  // Memoize chart geometry so hover re-renders only rebuild the crosshair/tooltip,
  // not the scales and SVG path strings (theme-dependent colors stay outside).
  const { minTs, tsRange, toX, toY, stackedAreaPaths, linePath, areaPath, dots, yTicks, xTicks, drawn } = useMemo(() => {
    const drawn: DrawnPoint[] = points.filter((p): p is DrawnPoint => p.value != null);
    const runs = segments(points);

    // Compute scales
    const minTs = points.length > 0 ? points[0].timestamp : 0;
    const maxTs = points.length > 0 ? points[points.length - 1].timestamp : 1;
    const values = drawn.map((p) => p.value);
    let minVal: number;
    let maxVal: number;
    if (effectiveMode === "result") {
      // A result starts at 0 and may go below it: keep the zero line in view.
      const lo = Math.min(0, ...values);
      const hi = Math.max(0, ...values);
      const pad = (hi - lo) * 0.1 || 1;
      minVal = lo - pad;
      maxVal = hi + pad;
    } else {
      minVal = stacked ? 0 : (values.length > 0 ? Math.min(...values) * 0.98 : 0);
      maxVal = values.length > 0 ? Math.max(...values) * 1.02 : 1;
    }
    const valRange = maxVal - minVal || 1;
    const tsRange = maxTs - minTs || 1;

    const toX = (ts: number) => PAD.left + ((ts - minTs) / tsRange) * plotW;
    const toY = (val: number) => PAD.top + plotH - ((val - minVal) / valRange) * plotH;

    // Build stacked area paths (one sub-path per unbroken run, so a gap stays a gap)
    const stackedAreaPaths: { token: string; path: string }[] = [];
    if (stacked && topTokens.length > 0) {
      const tokenOrder = topTokens;
      for (let ti = 0; ti < tokenOrder.length; ti++) {
        const token = tokenOrder[ti];
        const parts: string[] = [];
        for (const run of runs) {
          if (run.length < 2) continue;
          // Upper line (cumulative up to and including this token)
          const upperPoints = run.map((p) => {
            let cumUpper = 0;
            for (let j = 0; j <= ti; j++) {
              cumUpper += p.tokens?.[tokenOrder[j]] ?? 0;
            }
            return { x: toX(p.timestamp).toFixed(2), y: toY(cumUpper).toFixed(2) };
          });

          // Lower line (cumulative up to but NOT including this token)
          const lowerPoints = run.map((p) => {
            let cumLower = 0;
            for (let j = 0; j < ti; j++) {
              cumLower += p.tokens?.[tokenOrder[j]] ?? 0;
            }
            return { x: toX(p.timestamp).toFixed(2), y: toY(cumLower).toFixed(2) };
          });

          const upperPath = upperPoints.map((pt, i) => `${i === 0 ? "M" : "L"} ${pt.x} ${pt.y}`).join(" ");
          const lowerPath = [...lowerPoints].reverse().map((pt) => `L ${pt.x} ${pt.y}`).join(" ");
          parts.push(`${upperPath} ${lowerPath} Z`);
        }
        stackedAreaPaths.push({ token, path: parts.join(" ") });
      }
    }

    // Build line path (normal mode): one sub-path per unbroken run
    const runPath = (run: DrawnPoint[]) =>
      run.map((p, i) => `${i === 0 ? "M" : "L"} ${toX(p.timestamp).toFixed(2)} ${toY(p.value).toFixed(2)}`).join(" ");
    const lines = runs.filter((run) => run.length > 1);
    const linePath = !stacked ? lines.map(runPath).join(" ") : "";

    const areaPath =
      !stacked && effectiveMode === "value"
        ? lines
            .map(
              (run) =>
                runPath(run) +
                ` L ${toX(run[run.length - 1].timestamp).toFixed(2)} ${(PAD.top + plotH).toFixed(2)}` +
                ` L ${toX(run[0].timestamp).toFixed(2)} ${(PAD.top + plotH).toFixed(2)} Z`,
            )
            .join(" ")
        : "";

    // A reading with a gap on both sides has no line to belong to: draw it as a dot.
    const dots = !stacked ? runs.filter((run) => run.length === 1).map((run) => run[0]) : [];

    // Y-axis gridlines (5 lines)
    const yTicks = Array.from({ length: 5 }, (_, i) => minVal + (valRange * i) / 4);

    // X-axis labels (5 labels)
    const xTicks = Array.from({ length: 5 }, (_, i) => minTs + (tsRange * i) / 4);

    return { minTs, tsRange, toX, toY, stackedAreaPaths, linePath, areaPath, dots, yTicks, xTicks, drawn };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, stacked, topTokens, effectiveMode]);

  const chartColors = getChartColors();
  const stackedAreas = stackedAreaPaths.map((area, ti) => ({ ...area, color: chartColors[ti % chartColors.length] }));
  const formatValue = (val: number) =>
    effectiveMode === "result" ? formatCurrencyPnl(val, currencySymbol) : formatCurrency(val, currencySymbol);

  // Deposit/withdrawal markers sit on the line where the flow happened; one inside a gap
  // (no value to sit on) is left off rather than placed on a guess.
  const markers = flows
    .map((flow) => ({ flow, anchor: markerAnchor(points, flow.ts) }))
    .filter((m): m is { flow: PortfolioFlowMarker; anchor: { ts: number; value: number } } => m.anchor !== null);

  // Handle mouse move
  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    if (drawn.length === 0) return;
    const svg = e.currentTarget;
    const rect = svg.getBoundingClientRect();
    const mouseX = ((e.clientX - rect.left) / rect.width) * W;
    const ts = minTs + ((mouseX - PAD.left) / plotW) * tsRange;

    let closest = drawn[0];
    let closestDist = Math.abs(drawn[0].timestamp - ts);
    for (const p of drawn) {
      const dist = Math.abs(p.timestamp - ts);
      if (dist < closestDist) {
        closest = p;
        closestDist = dist;
      }
    }
    setHover({ x: toX(closest.timestamp), point: closest });
  };

  // Tooltip dimensions for stacked mode
  const hoverTokens = hover?.point.tokens ?? {};
  const hoverTokenEntries = stacked && topTokens.length > 0
    ? topTokens.filter((t) => (hoverTokens[t] ?? 0) > 0).map((t) => ({ token: t, value: hoverTokens[t] ?? 0, color: chartColors[topTokens.indexOf(t) % chartColors.length] }))
    : [];
  const tooltipH = stacked && hoverTokenEntries.length > 0 ? 28 + hoverTokenEntries.length * 16 : 40;
  const tooltipW = stacked && hoverTokenEntries.length > 0 ? 150 : 108;

  const modeButton = (id: ChartMode, label: string, enabled: boolean, hint: string) => (
    <button
      type="button"
      data-testid={`chart-mode-${id}`}
      aria-pressed={effectiveMode === id}
      disabled={!enabled}
      title={hint}
      onClick={() => setMode(id)}
      className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
        effectiveMode === id
          ? "bg-[var(--color-accent)] text-white"
          : enabled
            ? "text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
            : "text-[var(--color-text-muted)] opacity-50 cursor-not-allowed"
      }`}
    >
      {label}
    </button>
  );

  const pendingFlows = activeData?.pending_flows ?? 0;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4 h-full">
      <div className="flex items-center justify-between mb-3 gap-2">
        <h3 className="text-sm font-medium text-[var(--color-text-muted)]">Portfolio Evolution</h3>
        <div className="flex items-center gap-2">
          <div className="flex gap-0.5 rounded border border-[var(--color-border)] p-0.5" role="group" aria-label="Chart shows">
            {modeButton("value", "Value", true, "The portfolio's value, including deposits and withdrawals.")}
            {modeButton("result", "Trading result", toggle.enabled, toggle.hint)}
          </div>
          <div className="flex gap-0.5 rounded border border-[var(--color-border)] p-0.5">
            <button
              onClick={() => setStacked(false)}
              className={`p-1 rounded transition-colors ${!stacked ? "bg-[var(--color-accent)] text-white" : "text-[var(--color-text-muted)] hover:text-[var(--color-text)]"}`}
              title="Line chart"
            >
              <BarChart3 className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setStacked(true)}
              className={`p-1 rounded transition-colors ${stacked ? "bg-[var(--color-accent)] text-white" : "text-[var(--color-text-muted)] hover:text-[var(--color-text)]"}`}
              title="Stacked area chart"
            >
              <Layers className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>

      {baselineText && state !== "none" && (
        <p className="mb-2 text-xs text-[var(--color-text-muted)]" data-testid="chart-baseline-note">
          {baselineText}
        </p>
      )}

      {isLoading ? (
        <div className="flex items-center justify-center h-[250px] text-[var(--color-text-muted)] text-sm">
          Loading...
        </div>
      ) : state === "none" ? (
        <div className="flex items-center justify-center h-[250px] text-[var(--color-text-muted)] text-sm">
          No history data available
        </div>
      ) : state === "empty" ? (
        <div
          className="flex items-center justify-center h-[250px] px-6 text-center text-[var(--color-text-muted)] text-sm"
          data-testid="chart-empty"
        >
          {emptyMessage(activeData)}
        </div>
      ) : (
        <>
          <svg
            viewBox={`0 0 ${W} ${H}`}
            width="100%"
            className="overflow-visible"
            onMouseMove={handleMouseMove}
            onMouseLeave={() => setHover(null)}
          >
            <defs>
              <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--color-accent)" stopOpacity="0.3" />
                <stop offset="100%" stopColor="var(--color-accent)" stopOpacity="0.02" />
              </linearGradient>
            </defs>

            {/* Y gridlines + labels */}
            {yTicks.map((val, i) => (
              <g key={i}>
                <line
                  x1={PAD.left}
                  x2={W - PAD.right}
                  y1={toY(val)}
                  y2={toY(val)}
                  stroke="var(--color-border)"
                  strokeDasharray="4 4"
                  strokeWidth="0.5"
                />
                <text
                  x={PAD.left - 8}
                  y={toY(val) + 4}
                  textAnchor="end"
                  fill="var(--color-text-muted)"
                  fontSize="10"
                >
                  {formatValue(val)}
                </text>
              </g>
            ))}

            {/* The result starts at zero: that line is the reference */}
            {effectiveMode === "result" && (
              <line
                x1={PAD.left}
                x2={W - PAD.right}
                y1={toY(0)}
                y2={toY(0)}
                stroke="var(--color-text-muted)"
                strokeWidth="1"
                data-testid="chart-zero-line"
              />
            )}

            {/* X labels */}
            {xTicks.map((ts, i) => (
              <text
                key={i}
                x={toX(ts)}
                y={H - 4}
                textAnchor="middle"
                fill="var(--color-text-muted)"
                fontSize="10"
              >
                {formatAxisTime(ts, range)}
              </text>
            ))}

            {/* Stretches where an account had no reading: the line is cut, not filled */}
            {gaps.map((gap, i) => {
              const from = Math.max(gap.from_ts, minTs);
              const to = Math.min(gap.to_ts, minTs + tsRange);
              if (to <= from) return null;
              return (
                <rect
                  key={`gap-${i}`}
                  data-testid="chart-gap"
                  x={toX(from)}
                  y={PAD.top}
                  width={Math.max(toX(to) - toX(from), 1)}
                  height={plotH}
                  fill="var(--color-text-muted)"
                  opacity="0.1"
                >
                  <title>{gapLabel(gap, formatNoteTime)}</title>
                </rect>
              );
            })}

            {stacked ? (
              <>
                {/* Stacked area fills (render bottom-to-top, so reverse for correct layering) */}
                {[...stackedAreas].reverse().map((area) => (
                  <path key={area.token} d={area.path} fill={area.color} opacity="0.7" />
                ))}
              </>
            ) : (
              <>
                {/* Area fill */}
                {areaPath && <path d={areaPath} fill="url(#areaGrad)" />}
                {/* Line */}
                <path d={linePath} fill="none" stroke="var(--color-accent)" strokeWidth="2" data-testid="chart-line" />
                {dots.map((p) => (
                  <circle key={p.timestamp} cx={toX(p.timestamp)} cy={toY(p.value)} r="2.5" fill="var(--color-accent)" />
                ))}
              </>
            )}

            {/* Hover elements */}
            {hover && (
              <>
                <line
                  x1={hover.x}
                  x2={hover.x}
                  y1={PAD.top}
                  y2={PAD.top + plotH}
                  stroke="var(--color-text-muted)"
                  strokeDasharray="4 4"
                  strokeWidth="1"
                />
                {!stacked && (
                  <circle
                    cx={hover.x}
                    cy={toY(hover.point.value)}
                    r="4"
                    fill="var(--color-accent)"
                    stroke="var(--color-surface)"
                    strokeWidth="2"
                  />
                )}
                {/* Tooltip */}
                <g transform={`translate(${hover.x < W / 2 ? hover.x + 12 : hover.x - tooltipW - 12}, ${PAD.top + 8})`}>
                  <rect
                    width={tooltipW}
                    height={tooltipH}
                    rx="4"
                    fill="var(--color-surface)"
                    stroke="var(--color-border)"
                  />
                  <text x="8" y="16" fill="var(--color-text-muted)" fontSize="10">
                    {formatTooltipDate(hover.point.timestamp, range)}
                  </text>
                  {stacked && hoverTokenEntries.length > 0 ? (
                    <>
                      {hoverTokenEntries.map((entry, i) => (
                        <g key={entry.token} transform={`translate(0, ${24 + i * 16})`}>
                          <rect x="8" y="0" width="6" height="6" rx="1" fill={entry.color} />
                          <text x="18" y="7" fill="var(--color-text)" fontSize="9">
                            {entry.token}
                          </text>
                          <text x={tooltipW - 8} y="7" fill="var(--color-text-muted)" fontSize="9" textAnchor="end">
                            {formatCurrency(entry.value, currencySymbol)}
                          </text>
                        </g>
                      ))}
                    </>
                  ) : (
                    <text x="8" y="32" fill="var(--color-text)" fontSize="12" fontWeight="bold">
                      {formatValue(hover.point.value)}
                    </text>
                  )}
                </g>
              </>
            )}

            {/* Invisible overlay for mouse events */}
            <rect
              x={PAD.left}
              y={PAD.top}
              width={plotW}
              height={plotH}
              fill="transparent"
            />

            {/* Deposit / withdrawal markers (above the overlay so they can be hovered) */}
            {markers.map(({ flow, anchor }, i) => {
              const x = toX(anchor.ts);
              const y = toY(anchor.value);
              const deposit = !String(flow.amount).trim().startsWith("-");
              const colour = deposit ? "var(--color-green)" : "var(--color-red)";
              const label = flowLabel(flow);
              const shape = deposit
                ? `${x},${y - 6} ${x - 4.5},${y + 3} ${x + 4.5},${y + 3}`
                : `${x},${y + 6} ${x - 4.5},${y - 3} ${x + 4.5},${y - 3}`;
              return (
                <g
                  key={`flow-${i}`}
                  data-testid="chart-flow-marker"
                  data-label={label}
                  onMouseEnter={() => setHoverFlow({ x, y, label })}
                  onMouseLeave={() => setHoverFlow(null)}
                >
                  <title>{label}</title>
                  <polygon points={shape} fill={colour} stroke="var(--color-surface)" strokeWidth="1" />
                </g>
              );
            })}
            {hoverFlow && (
              <g transform={`translate(${Math.min(Math.max(hoverFlow.x - 90, PAD.left), W - PAD.right - 180)}, ${Math.max(hoverFlow.y - 34, 2)})`} pointerEvents="none">
                <rect width="180" height="22" rx="4" fill="var(--color-surface)" stroke="var(--color-border)" />
                <text x="6" y="15" fill="var(--color-text)" fontSize="10">
                  {hoverFlow.label}
                </text>
              </g>
            )}
          </svg>

          {/* Legend for stacked mode */}
          {stacked && topTokens.length > 0 && (
            <div className="flex flex-wrap gap-3 mt-2 px-1">
              {topTokens.map((token, i) => (
                <div key={token} className="flex items-center gap-1.5">
                  <div
                    className="h-2.5 w-2.5 rounded-sm"
                    style={{ backgroundColor: chartColors[i % chartColors.length] }}
                  />
                  <span className="text-xs text-[var(--color-text-muted)]">{token}</span>
                </div>
              ))}
            </div>
          )}

          {/* What the chart leaves out, and why */}
          <ul className="mt-2 space-y-0.5 text-[11px] text-[var(--color-text-muted)]" data-testid="chart-footnotes">
            {gaps.map((gap, i) => (
              <li key={`gap-${i}`}>{gapLabel(gap, formatNoteTime)} (the line is cut, nothing is filled in).</li>
            ))}
            {pendingFlows > 0 && (
              <li>
                {pendingFlows === 1
                  ? "1 unconfirmed deposit or withdrawal in this window is not marked until confirmed."
                  : `${pendingFlows} unconfirmed deposits or withdrawals in this window are not marked until confirmed.`}
              </li>
            )}
            {activeData?.flows_note && <li>{activeData.flows_note}</li>}
            {effectiveMode === "result" && activeData?.result_note && <li>{activeData.result_note}</li>}
            {pendingText && <li>{pendingText}</li>}
          </ul>
        </>
      )}
    </div>
  );
}

// ── Main Portfolio Page ──

/** Assets is what you hold; Positions is what you are in. */
type PortfolioTab = "assets" | "positions";

export function Portfolio() {
  const { server } = useServer();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [period, setPeriod] = useState<string>("1W");

  // A tab is a real address here (Bots.tsx idiom): the default clears the param
  // rather than writing it, so `/portfolio` stays the canonical URL for Assets.
  const [searchParams, setSearchParams] = useSearchParams();
  const tab: PortfolioTab = searchParams.get("tab") === "positions" ? "positions" : "assets";
  const setTab = (next: PortfolioTab) => {
    if (next === "assets") setSearchParams({}, { replace: true });
    else setSearchParams({ tab: next }, { replace: true });
  };

  const { data, isLoading, error, isPlaceholderData } = useQuery({
    queryKey: ["portfolio", server],
    queryFn: () => api.getPortfolio(server!),
    enabled: !!server,
    refetchInterval: 15000,
    placeholderData: keepPreviousData,
  });

  // One-time background refresh on mount / server change
  useEffect(() => {
    if (!server) return;
    const timer = setTimeout(() => {
      api
        .getPortfolio(server, true)
        .then((fresh) => {
          queryClient.setQueryData(["portfolio", server], fresh);
        })
        .catch(() => {}); // silent fail, cached data still shown
    }, 500);
    return () => clearTimeout(timer);
  }, [server, queryClient]);

  const { data: bots } = useQuery({
    queryKey: ["bots", server],
    queryFn: () => api.getBots(server!),
    enabled: !!server,
    refetchInterval: 30000,
    placeholderData: keepPreviousData,
  });

  // Subscribe to the executors WS channel so the KPI strip updates live;
  // the query below shares the canonical unfiltered executors cache key
  // (prefetched by usePrefetchData, pushed by useWebSocket) with a relaxed
  // poll as fallback.
  const executorChannels = useMemo(
    () => (server ? [`executors:${server}`] : []),
    [server],
  );
  useCondorWebSocket(executorChannels, server ?? null);

  const { data: allExecutors } = useQuery({
    queryKey: executorsQuery(server).queryKey,
    queryFn: () => api.getExecutors(server!),
    enabled: !!server,
    refetchInterval: EXECUTORS_REFETCH_MS,
    placeholderData: keepPreviousData,
  });

  // Period totals are aggregated server-side over the whole executor history:
  // the cache above is bounded to one poll page, so summing it here reported a
  // fraction of the window on any busy server (CORR-129).
  const { data: execStats } = useQuery({
    queryKey: ["executors-summary", server, period],
    queryFn: () => api.getExecutorsSummary(server!, period),
    enabled: !!server,
    refetchInterval: 60000,
    // No keepPreviousData: the tile labels its own window ("12 in 1M"), so
    // carrying the previous period's totals across a switch would caption the
    // old numbers with the new period.
  });

  const { data: agents } = useQuery(agentsQuery());

  const { data: periodHistory } = useQuery({
    queryKey: ["portfolio-history", server, period],
    queryFn: () => api.getPortfolioHistory(server!, period),
    enabled: !!server,
    refetchInterval: 60000,
  });

  // Holds — the same server-scoped cache entry the trade workspace and the pool
  // pages already invalidate after a mutation, so this observer inherits every
  // existing invalidation and adds no new cache surface. Polled slowly while
  // Assets is showing, since only the tab badge depends on it there.
  const { data: positionsData, isLoading: isLoadingHolds } = useQuery({
    queryKey: ["consolidated-positions", server],
    queryFn: () => api.getConsolidatedPositions(server!),
    enabled: !!server,
    refetchInterval: tab === "positions" ? 15_000 : 60_000,
    placeholderData: keepPreviousData,
  });
  const holds = useMemo(
    () => [
      ...(positionsData?.executor_positions ?? []),
      ...(positionsData?.bot_positions ?? []),
    ],
    [positionsData],
  );

  // Liquidity — shared with the /dex strip, and deliberately not derived from
  // the unfiltered executors cache below (see the hook).
  const { positions: lpPositions, label: lpLabel, isLoading: isLoadingLp } =
    useLpPositions(server ?? null);

  // Currency conversion for bots + executors (must be before early returns)
  const controllers = bots?.controllers ?? [];
  const executorsList = allExecutors ?? [];
  const quoteCurrencies = useMemo(() => {
    const quotes = new Set<string>(["USDT"]);
    for (const c of controllers) {
      quotes.add(c.trading_pair?.split("-")[1] || "USDT");
    }
    for (const e of executorsList) {
      quotes.add(e.trading_pair?.split("-")[1] || "USDT");
    }
    // Without these a hold on a non-USDT pair renders unconverted under the
    // display currency's symbol — the number and its label disagreeing.
    for (const pos of holds) {
      quotes.add(pos.trading_pair?.split("-")[1] || "USDT");
    }
    return Array.from(quotes);
  }, [controllers, executorsList, holds]);
  const {
    convert,
    formatValueDetailed,
    formatPnlValue,
    resolvedSymbol: currencySymbol,
  } = useRates(quoteCurrencies);

  // Convert a USDT-denominated value to the display currency
  const convertFromUsd = useCallback(
    (val: number) => convert(val, "USDT").value,
    [convert]
  );

  if (!server) {
    return <NoServerCard message="Select a server from the sidebar to view your portfolio." />;
  }

  if (isLoading && !data) {
    return (
      <div className="space-y-6">
        {/* Skeleton dashboard strip */}
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-5 py-4 flex gap-8">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="flex-1">
              <div className="h-3 w-16 rounded bg-[var(--color-border)] animate-pulse mb-2" />
              <div className="h-7 w-24 rounded bg-[var(--color-border)] animate-pulse mb-1.5" />
              <div className="h-3 w-32 rounded bg-[var(--color-border)] animate-pulse" />
            </div>
          ))}
        </div>
        {/* Skeleton chart */}
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
          <div className="h-[250px] rounded bg-[var(--color-border)]/30 animate-pulse" />
        </div>
        {/* Skeleton table */}
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4 space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-4 rounded bg-[var(--color-border)] animate-pulse" style={{ width: `${85 - i * 10}%` }} />
          ))}
        </div>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="rounded-lg border border-red-500/30 bg-[var(--color-surface)] p-8 text-center max-w-sm">
          <AlertCircle className="h-10 w-10 mx-auto mb-3 text-[var(--color-red)]" />
          <h2 className="text-lg font-semibold mb-1">Failed to Load Portfolio</h2>
          <p className="text-sm text-[var(--color-text-muted)]">
            {error instanceof Error ? error.message : "An unexpected error occurred."}
          </p>
        </div>
      </div>
    );
  }

  const totalUsd = data?.total_usd ?? 0;
  const connectors = data?.connectors ?? [];
  const hasExchange = connectors.some((connector) => connector.connector === "Crypto.com Exchange");

  // The portfolio's change over the period (follows the period selector), as the
  // server works it out: net of recorded deposits and withdrawals and only from sources
  // that all have a value at the start. Never the first point against the last of the
  // chart series, which mixes accounts that start at different times (and so once read
  // +491%); when it cannot be stated honestly it is a dash with the reason.
  const portfolioChange = historyChange(periodHistory);
  const portfolioPnl = portfolioChange.known ? portfolioChange.abs : null;

  // Compute aggregate stats
  const totalTokens = connectors.reduce((s, c) => s + c.balances.length, 0);
  const botsList = bots?.bots ?? [];
  const controllerCount = controllers.length;
  const activeExecutorCount = executorsList.filter((e) => isExecutorActive(e.status)).length;

  // Convert bot PnL and volume per-controller
  let botPnl = 0;
  let botVolume = 0;
  for (const ctrl of controllers) {
    const quote = ctrl.trading_pair?.split("-")[1] || "USDT";
    botPnl += convert(ctrl.global_pnl_quote, quote).value;
    botVolume += convert(ctrl.volume_traded, quote).value;
  }

  // Flatten all tokens for top holdings
  const allTokens = connectors.flatMap((c) =>
    c.balances.map((b) => ({ token: b.token, usd_value: b.usd_value, connector: c.connector })),
  );
  allTokens.sort((a, b) => b.usd_value - a.usd_value);
  const topTokens = allTokens.slice(0, 8);

  return (
    <div className={`space-y-6 transition-opacity duration-300 ${isPlaceholderData ? "opacity-60" : "opacity-100"}`}>
      {/* Dashboard Strip */}
      <DashboardStrip
        totalUsd={totalUsd}
        totalTokens={totalTokens}
        connectorCount={connectors.length}
        portfolioPnl={portfolioPnl}
        portfolioPnlTitle={
          portfolioChange.known
            ? [
                `Change over ${period}, net of recorded deposits and withdrawals` +
                  (portfolioChange.pct !== null
                    ? ` (${portfolioChange.pct >= 0 ? "+" : ""}${portfolioChange.pct.toFixed(2)}%)`
                    : ""),
                portfolioChange.note,
              ]
                .filter(Boolean)
                .join(". ")
            : undefined
        }
        portfolioPnlUnknownTitle={
          portfolioChange.known || !periodHistory ? undefined : portfolioChange.hint
        }
        botCount={botsList.length}
        controllerCount={controllerCount}
        botPnl={botPnl}
        botVolume={botVolume}
        activeExecutorCount={activeExecutorCount}
        execStats={execStats}
        agents={agents ?? []}
        period={period}
        onPeriodChange={setPeriod}
        onNavigate={navigate}
        convertFromUsd={convertFromUsd}
        currencySymbol={currencySymbol}
      />

      {data?.notes?.map((note) => <p key={note} role="alert" className="text-sm text-[var(--color-red)]">{note}</p>)}
      {hasExchange && <p className="text-sm text-[var(--color-text-muted)]">Crypto.com Exchange is included in the current total and assets below. The chart adds its value from when Condor started recording it (every 5 minutes); earlier history is not available. <a className="underline" href="/trade?workspace=crypto-exchange">Open Crypto.com trading</a></p>}

      {/* Portfolio Evolution + Top Holdings side by side */}
      <div className="flex flex-col lg:flex-row gap-4">
        <div className={connectors.length > 0 && topTokens.length > 0 ? "lg:flex-[2] min-w-0" : "w-full"}>
          <PortfolioEvolution server={server!} range={period} convertFromUsd={convertFromUsd} currencySymbol={currencySymbol} />
        </div>
        {connectors.length > 0 && topTokens.length > 0 && (
          <div className="lg:flex-1 min-w-0">
            <TokenBarChart tokens={topTokens} title="Top Holdings" totalPortfolioValue={totalUsd} />
          </div>
        )}
      </div>

      {/* Assets | Positions — the header above describes the whole portfolio,
          so it stays above the split. */}
      <div className="flex items-center gap-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-1 w-fit">
        {([
          { key: "assets" as const, label: "Assets", icon: Wallet, count: null },
          {
            key: "positions" as const,
            label: "Positions",
            icon: Layers,
            // A flash of `(0)` on a portfolio that has seven positions is worse
            // than a beat of no badge, so it waits for both queries.
            count: isLoadingHolds || isLoadingLp ? null : holds.length + lpPositions.length,
          },
        ]).map(({ key, label, icon: Icon, count }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
              tab === key
                ? "bg-[var(--color-bg)] text-[var(--color-text)] shadow-sm"
                : "text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
            }`}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
            {count !== null && (
              <span className="text-xs text-[var(--color-text-muted)]">({count})</span>
            )}
          </button>
        ))}
      </div>

      {/* Both tabs stay mounted and the inactive one is hidden: this is a
          toggle, not a route change, and the balance table is expensive to
          rebuild. Deferring the mount buys nothing — the queries behind
          Positions run at page level anyway, for the tab badge. */}
      <div style={{ display: tab === "assets" ? undefined : "none" }}>
        {connectors.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-16 text-[var(--color-text-muted)]">
            <Wallet className="h-10 w-10" />
            <p>No balances found</p>
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border border-[var(--color-border)]">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--color-border)] bg-[var(--color-surface)]">
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-[var(--color-text-muted)]">
                    Token
                  </th>
                  <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-[var(--color-text-muted)]">
                    Amount
                  </th>
                  <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-[var(--color-text-muted)]">
                    Price
                  </th>
                  <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-[var(--color-text-muted)]">
                    Value
                  </th>
                  <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-[var(--color-text-muted)]">
                    Allocation
                  </th>
                </tr>
              </thead>
              <tbody>
                {connectors.map((c) => (
                  <ConnectorRow key={c.connector} connector={c} totalPortfolio={totalUsd} convertFromUsd={convertFromUsd} currencySymbol={currencySymbol} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div style={{ display: tab === "positions" ? undefined : "none" }}>
        <PositionsTab
          holds={holds}
          lpPositions={lpPositions}
          lpLabel={lpLabel}
          isLoading={isLoadingHolds || isLoadingLp}
          convert={convert}
          formatValue={formatValueDetailed}
          formatPnlValue={formatPnlValue}
        />
      </div>
    </div>
  );
}
