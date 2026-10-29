/**
 * The Portfolio Evolution chart: one baseline, breaks at gaps, deposit markers and the
 * Value | Trading result toggle.
 *
 * The merged history stepped 9 -> 52 -> 55 as accounts were added and money deposited,
 * which read as performance. The chart now draws the server's `chart_points` (from when
 * every account was recording), cuts the line where an account had no reading, marks the
 * confirmed deposits and offers the result net of them.
 *
 * @vitest-environment jsdom
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PortfolioHistoryResponse, PortfolioResponse } from "@/lib/api";

const getPortfolio = vi.fn();
const getPortfolioHistory = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    getPortfolio: (...a: unknown[]) => getPortfolio(...a),
    getPortfolioHistory: (...a: unknown[]) => getPortfolioHistory(...a),
    getBots: async () => ({ bots: [], controllers: [] }),
    getExecutors: async () => [],
    getExecutorsSummary: async () => ({ pnl: 0, volume: 0, count: 0, converted: true }),
    getAgents: async () => [],
    getConsolidatedPositions: async () => ({ executor_positions: [], bot_positions: [] }),
  },
}));

vi.mock("@/hooks/useServer", () => ({ useServer: () => ({ server: "FortPT VM-08" }) }));
vi.mock("@/hooks/useWebSocket", () => ({ useCondorWebSocket: () => ({}) }));
vi.mock("@/hooks/useLpPositions", () => ({
  useLpPositions: () => ({ positions: [], label: "", isLoading: false }),
}));
vi.mock("@/hooks/useRates", () => ({
  useRates: () => ({
    convert: (value: number) => ({ value }),
    formatValueDetailed: (value: number) => String(value),
    formatPnlValue: (value: number) => String(value),
    resolvedSymbol: "$",
  }),
}));

const { Portfolio } = await import("./Portfolio");

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

const PORTFOLIO: PortfolioResponse = {
  server: "FortPT VM-08",
  total_usd: 61.87,
  connectors: [
    {
      connector: "Revolut X",
      total_usd: 4.75,
      balances: [{ token: "XRP", total: 4.75, available: 4.75, usd_value: 4.75 }],
    },
  ],
};

const H = 3600;
const T0 = 1_800_000_000;

const BASE: PortfolioHistoryResponse = {
  server: "FortPT VM-08",
  interval: "1h",
  // The old, staircase series: 9 -> 52 -> 55.
  points: [
    { timestamp: T0, total_usd: 9 },
    { timestamp: T0 + H, total_usd: 52 },
    { timestamp: T0 + 2 * H, total_usd: 55 },
  ],
  chart_points: [
    { timestamp: T0 + 2 * H, total_usd: 55 },
    { timestamp: T0 + 3 * H, total_usd: 59.27 },
    { timestamp: T0 + 4 * H, total_usd: 59.3 },
  ],
  baseline_ts: T0 + 2 * H,
  series_start_ts: T0 + 2 * H,
  series_start_reason: "all_accounts_recording_from",
  sources: [
    { source: "Hummingbot", first_ts: T0, status: "recording" },
    { source: "Crypto.com Exchange", first_ts: T0 + H, status: "recording" },
    { source: "Revolut X", first_ts: T0 + 2 * H, status: "recording" },
  ],
  gaps: [],
  flows: [
    {
      ts: T0 + 2.5 * H,
      venue: "Revolut X",
      currency: "EUR",
      amount: "4.27",
      usd_value_at_time: 4.27,
      source: "api",
      status: "confirmed",
    },
  ],
  pending_flows: 0,
  flows_note: null,
  result_points: [
    { timestamp: T0 + 2 * H, result_usd: 0 },
    { timestamp: T0 + 3 * H, result_usd: 0 },
    { timestamp: T0 + 4 * H, result_usd: 0.03 },
  ],
  result_reason: null,
  result_note: null,
  result_not_netted: [],
  change: { available: false, reason: "different_start", note: "x" },
};

let container: HTMLDivElement;
let root: Root;
let qc: QueryClient;

async function render() {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <Portfolio />
        </QueryClientProvider>
      </MemoryRouter>,
    );
  });
  for (let i = 0; i < 6; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

const q = (id: string) => container.querySelector(`[data-testid="${id}"]`);
const qa = (id: string) => [...container.querySelectorAll(`[data-testid="${id}"]`)];

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  getPortfolio.mockReset().mockResolvedValue(PORTFOLIO);
  getPortfolioHistory.mockReset();
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  qc.clear();
});

describe("Portfolio Evolution chart", () => {
  it("says from when it shows and that earlier points only had some accounts", async () => {
    getPortfolioHistory.mockResolvedValue(BASE);
    await render();
    const note = q("chart-baseline-note");
    expect(note).not.toBeNull();
    expect(note!.textContent).toMatch(/^Showing from .+, when all 3 accounts were recording\. Earlier points only had some accounts\.$/);
  });

  it("has no note when the window starts after the baseline", async () => {
    getPortfolioHistory.mockResolvedValue({ ...BASE, series_start_reason: null });
    await render();
    expect(q("chart-baseline-note")).toBeNull();
  });

  it("draws the chart series, not the staircase: the axis starts near 55, never at 9", async () => {
    getPortfolioHistory.mockResolvedValue(BASE);
    await render();
    const labels = [...container.querySelectorAll("svg text")].map((t) => t.textContent ?? "");
    expect(labels.some((t) => /^\$5\d\.\d\d$/.test(t))).toBe(true);
    expect(labels.some((t) => t.includes("$9.00"))).toBe(false);
  });

  it("marks a confirmed deposit on the line with its label", async () => {
    getPortfolioHistory.mockResolvedValue(BASE);
    await render();
    const markers = qa("chart-flow-marker");
    expect(markers).toHaveLength(1);
    expect(markers[0].getAttribute("data-label")).toBe("Deposit +4.27 EUR, Revolut X (confirmed)");
    expect(markers[0].querySelector("title")?.textContent).toBe("Deposit +4.27 EUR, Revolut X (confirmed)");
  });

  it("marks a withdrawal and leaves a flow outside the drawn range off", async () => {
    getPortfolioHistory.mockResolvedValue({
      ...BASE,
      flows: [
        { ...BASE.flows![0], amount: "-3", usd_value_at_time: -3 },
        { ...BASE.flows![0], ts: T0 + 9 * H },
      ],
    });
    await render();
    const markers = qa("chart-flow-marker");
    expect(markers).toHaveLength(1);
    expect(markers[0].getAttribute("data-label")).toContain("Withdrawal -3 EUR");
  });

  it("cuts the line at a gap and says so under the chart", async () => {
    getPortfolioHistory.mockResolvedValue({
      ...BASE,
      chart_points: [
        { timestamp: T0 + 2 * H, total_usd: 55 },
        { timestamp: T0 + 3 * H, total_usd: 55.5 },
        { timestamp: T0 + 3.5 * H, total_usd: null },
        { timestamp: T0 + 4 * H, total_usd: 56 },
        { timestamp: T0 + 5 * H, total_usd: 56.5 },
      ],
      gaps: [{ source: "Hummingbot", from_ts: T0 + 3.2 * H, to_ts: T0 + 3.8 * H }],
      flows: [],
    });
    await render();
    const d = q("chart-line")!.getAttribute("d")!;
    expect(d.match(/M /g)).toHaveLength(2);
    expect(qa("chart-gap")).toHaveLength(1);
    expect(q("chart-footnotes")!.textContent).toContain("Hummingbot: no readings from");
    expect(q("chart-footnotes")!.textContent).toContain("nothing is filled in");
  });

  it("reports a reading left out as an outlier and cuts the line there", async () => {
    getPortfolioHistory.mockResolvedValue({
      ...BASE,
      chart_points: [
        { timestamp: T0 + 2 * H, total_usd: 55 },
        { timestamp: T0 + 3 * H, total_usd: 55 },
        { timestamp: T0 + 3.5 * H, total_usd: null },
        { timestamp: T0 + 4 * H, total_usd: 55 },
        { timestamp: T0 + 5 * H, total_usd: 55 },
      ],
      anomalies: [{ source: "Hummingbot", ts: T0 + 3.5 * H, value: 72.19, neighbour_value: 55 }],
      flows: [],
    });
    await render();
    expect(qa("chart-anomaly")).toHaveLength(1);
    expect(qa("chart-anomaly")[0].textContent).toContain("one reading ($72.19)");
    expect(q("chart-line")!.getAttribute("d")!.match(/M /g)).toHaveLength(2);
  });

  it("shows no gap band or gap footnote when nothing is missing", async () => {
    getPortfolioHistory.mockResolvedValue({ ...BASE, gaps: [], anomalies: [] });
    await render();
    expect(qa("chart-gap")).toHaveLength(0);
    expect(qa("chart-anomaly")).toHaveLength(0);
    expect(q("chart-footnotes")!.textContent).not.toContain("no readings");
  });

  it("shows an empty state instead of a line for a single point", async () => {
    getPortfolioHistory.mockResolvedValue({
      ...BASE,
      chart_points: [{ timestamp: T0 + 2 * H, total_usd: 55 }],
      result_points: null,
      flows: [],
    });
    await render();
    expect(q("chart-empty")).not.toBeNull();
    expect(q("chart-empty")!.textContent).toMatch(/at least two readings/);
    expect(q("chart-line")).toBeNull();
  });

  it("shows an empty state when there is no common baseline yet", async () => {
    getPortfolioHistory.mockResolvedValue({
      ...BASE,
      chart_points: [],
      baseline_ts: null,
      series_start_reason: null,
      sources: [{ source: "Revolut X", first_ts: null, status: "pending" }],
      result_points: null,
      flows: [],
    });
    await render();
    expect(q("chart-empty")!.textContent).toContain("Revolut X has no recorded value yet");
  });

  it("keeps an older server's response working: it draws the plain points", async () => {
    getPortfolioHistory.mockResolvedValue({
      server: "FortPT VM-08",
      interval: "1h",
      points: BASE.points,
    });
    await render();
    expect(q("chart-line")).not.toBeNull();
    expect(q("chart-baseline-note")).toBeNull();
    expect(q("chart-mode-result")!.hasAttribute("disabled")).toBe(true);
  });
});

describe("Value | Trading result toggle", () => {
  it("defaults to Value, with Trading result available", async () => {
    getPortfolioHistory.mockResolvedValue(BASE);
    await render();
    expect(q("chart-mode-value")!.getAttribute("aria-pressed")).toBe("true");
    expect(q("chart-mode-result")!.getAttribute("aria-pressed")).toBe("false");
    expect(q("chart-mode-result")!.hasAttribute("disabled")).toBe(false);
    expect(q("chart-zero-line")).toBeNull();
  });

  it("switches to the net-of-deposits series, which starts at zero", async () => {
    getPortfolioHistory.mockResolvedValue(BASE);
    await render();
    await act(async () => {
      (q("chart-mode-result") as HTMLButtonElement).click();
    });
    expect(q("chart-mode-result")!.getAttribute("aria-pressed")).toBe("true");
    expect(q("chart-zero-line")).not.toBeNull();
    const labels = [...container.querySelectorAll("svg text")].map((t) => t.textContent ?? "");
    expect(labels.some((t) => t.startsWith("+$"))).toBe(true);
    // The deposit marker stays on the result line.
    expect(qa("chart-flow-marker")).toHaveLength(1);
    await act(async () => {
      (q("chart-mode-value") as HTMLButtonElement).click();
    });
    expect(q("chart-zero-line")).toBeNull();
  });

  it("is disabled with the reason when the result cannot be netted honestly", async () => {
    getPortfolioHistory.mockResolvedValue({
      ...BASE,
      result_points: null,
      result_reason: "flows_untracked",
      result_note: "Deposits and withdrawals of Revolut X are not tracked, so a deposit would read as a gain.",
    });
    await render();
    const button = q("chart-mode-result") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("title")).toContain("Revolut X are not tracked");
    expect(q("chart-mode-value")!.getAttribute("aria-pressed")).toBe("true");
  });

  it("shows the Hummingbot caveat under the chart in result mode", async () => {
    getPortfolioHistory.mockResolvedValue({
      ...BASE,
      result_not_netted: ["Hummingbot"],
      result_note: "Deposits and withdrawals of Hummingbot-connected accounts are not recorded.",
    });
    await render();
    expect(q("chart-footnotes")!.textContent).not.toContain("Hummingbot-connected");
    await act(async () => {
      (q("chart-mode-result") as HTMLButtonElement).click();
    });
    expect(q("chart-footnotes")!.textContent).toContain("Hummingbot-connected accounts are not recorded");
  });

  it("reports unconfirmed movements and the accounts without deposit data", async () => {
    getPortfolioHistory.mockResolvedValue({
      ...BASE,
      pending_flows: 2,
      flows_note: "Deposits and withdrawals are only tracked for Condor's own accounts.",
    });
    await render();
    const text = q("chart-footnotes")!.textContent!;
    expect(text).toContain("2 unconfirmed deposits or withdrawals");
    expect(text).toContain("only tracked for Condor's own accounts");
  });
});
