/**
 * Trading 212 reports the owner's own holdings with their result against cost; the Portfolio shows that line
 * in green or red under the account, and says so when the cost is not known.
 *
 * @vitest-environment jsdom
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getPortfolio = vi.fn();
const getPortfolioHistory = vi.fn();
const preference = vi.hoisted(() => ({ hidden: false }));
const holdingsFixture = vi.hoisted(() => ({ value: [] as unknown[] }));

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
vi.mock("@/hooks/useUiPreferences", () => ({
  useHideHummingbot: () => ({ hidden: preference.hidden, loading: false, setHidden: () => {}, saving: false, failed: false }),
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
  getPortfolio.mockReset().mockImplementation(async () => ({
    server: "FortPT VM-08",
    total_usd: 100,
    connectors: [{
      connector: "Trading 212", total_usd: 100, note: "unified",
      balances: [{ token: "VWRPl", total: 0.5, available: 0.5, usd_value: 60 }, { token: "EUR", total: 40, available: 40, usd_value: 40 }],
      holdings: holdingsFixture.value,
    }],
  }));
  getPortfolioHistory.mockReset().mockResolvedValue({
    server: "FortPT VM-08",
    interval: "1h",
    points: [{ timestamp: 1, total_usd: 90 }, { timestamp: 2, total_usd: 100 }],
  });
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  qc.clear();
  preference.hidden = false;
  holdingsFixture.value = [];
});

const holdingLine = () => container.querySelector("[data-holding]")?.textContent ?? "";

describe("Trading 212 holdings", () => {
  it("shows a gain in green with its percentage", async () => {
    holdingsFixture.value = [{ token: "VWRPl", quantity: 0.5, value_usd: 60, cost_usd: 58, gain_usd: 2, gain_pct: 3.4 }];
    await render();
    expect(holdingLine()).toContain("VWRPl");
    expect(holdingLine()).toContain("+");
    expect(holdingLine()).toContain("(+3.4%)");
    expect((container.querySelector("[data-holding] .font-medium") as HTMLElement).style.color).toBe("var(--color-green)");
  });

  it("shows a loss in red", async () => {
    holdingsFixture.value = [{ token: "VWRPl", quantity: 0.5, value_usd: 60, cost_usd: 62, gain_usd: -2, gain_pct: -3.2 }];
    await render();
    expect(holdingLine()).toContain("−");
    expect(holdingLine()).toContain("(−3.2%)");
    expect((container.querySelector("[data-holding] .font-medium") as HTMLElement).style.color).toBe("var(--color-red)");
  });

  it("says the result is not available when the cost is not known, and shows nothing without holdings", async () => {
    holdingsFixture.value = [{ token: "VWRPl", quantity: 0.5, value_usd: 60, cost_usd: null, gain_usd: null, gain_pct: null }];
    await render();
    expect(holdingLine()).toContain("not available");
    holdingsFixture.value = [];
    qc.clear();
    await render();
    expect(container.querySelector("[data-holding]")).toBeNull();
  });
});
