/**
 * The Portfolio page's headline: what is real, and an honest change.
 *
 * The page KPI used to be the first point of the history against the last, which mixes
 * accounts that start at different times (and deposits): a 55 EUR desk read +491%. It is
 * now the server's net-of-flows figure, or a dash with the reason. The simulated (paper)
 * account is listed, badged, and in none of the counts.
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

const bal = (token: string, usd: number) => ({
  token,
  total: usd,
  available: usd,
  usd_value: usd,
});

const PORTFOLIO: PortfolioResponse = {
  server: "FortPT VM-08",
  total_usd: 61.87,
  connectors: [
    { connector: "kraken", total_usd: 10.19, balances: [bal("EUR", 10.19)] },
    { connector: "Crypto.com Exchange", total_usd: 46.93, balances: [bal("USD", 30), bal("HYPE", 16.93)] },
    { connector: "Revolut X", total_usd: 4.75, balances: [bal("XRP", 4.75)] },
    {
      connector: "Kraken xStocks (paper)",
      total_usd: 100,
      paper: true,
      balances: [bal("USD", 100)],
    },
  ],
};

const history = (change: PortfolioHistoryResponse["change"]): PortfolioHistoryResponse => ({
  server: "FortPT VM-08",
  interval: "1h",
  // First 9.36, last 55.38: the old figure read +46.02 (+491.94%).
  points: [
    { timestamp: 1, total_usd: 9.36 },
    { timestamp: 2, total_usd: 55.38 },
  ],
  change,
});

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

describe("Portfolio page headline", () => {
  it("shows a dash with the reason, not the first-to-last gain, when sources start at different times", async () => {
    getPortfolioHistory.mockResolvedValue(
      history({ available: false, reason: "different_start", note: "History starts at different times." }),
    );
    await render();
    expect(container.textContent).not.toContain("46.02");
    const dash = container.querySelector('[data-testid="portfolio-change-unknown"]');
    expect(dash).not.toBeNull();
    expect(dash!.getAttribute("title")).toContain("different times");
  });

  it("shows the server's net-of-flows change for a clean window", async () => {
    getPortfolioHistory.mockResolvedValue(
      history({ available: true, change_usd: 0.62, change_pct: 1.01, note: null }),
    );
    await render();
    expect(container.querySelector('[data-testid="portfolio-change-unknown"]')).toBeNull();
    expect(container.textContent).toContain("+$0.62");
  });

  it("counts only the real accounts and badges the simulated one, with no share", async () => {
    getPortfolioHistory.mockResolvedValue(history({ available: false, reason: "different_start" }));
    await render();
    // 4 real balances over 3 real connectors; the paper USD 100 is in neither.
    expect(container.textContent).toContain("4 assets · 3 connectors");
    expect(container.textContent).toContain("$61.87");
    const badge = container.querySelector("[data-paper-connector]");
    expect(badge).not.toBeNull();
    expect(badge!.textContent).toContain("Simulated - not in total");
  });
});
