/**
 * Top Holdings lists a token once, summed across connectors; the Assets table keeps the rows
 * per connector.
 *
 * @vitest-environment jsdom
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PortfolioResponse } from "@/lib/api";

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
  total_usd: 1592.93,
  connectors: [
    { connector: "Trading 212", total_usd: 1540, balances: [bal("USD", 40), bal("TSLA", 1500)] },
    { connector: "Crypto.com", total_usd: 52.93, balances: [bal("USD", 36), bal("HYPE", 16.93)] },
  ],
};

let container: HTMLDivElement;
let root: Root;
let qc: QueryClient;

beforeEach(async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  getPortfolio.mockReset().mockResolvedValue(PORTFOLIO);
  getPortfolioHistory.mockReset().mockResolvedValue({
    server: "FortPT VM-08",
    interval: "1h",
    points: [{ timestamp: 1, total_usd: 1500 }, { timestamp: 2, total_usd: 1592.93 }],
  });
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
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  qc.clear();
});

const topHoldingsBlock = () =>
  Array.from(container.querySelectorAll("h3"))
    .find((heading) => heading.textContent === "Top Holdings")!
    .parentElement!;

describe("Portfolio Top Holdings", () => {
  it("lists USD once although Trading 212 and Crypto.com each hold it", () => {
    const labels = Array.from(topHoldingsBlock().querySelectorAll("span.font-medium")).map((el) => el.textContent);
    expect(labels.filter((label) => label === "USD")).toHaveLength(1);
    expect(labels).toEqual(["TSLA", "USD", "HYPE"]);
  });

  it("gives that token's share as the sum over both connectors (76 of 1592.93), not either part", () => {
    const text = topHoldingsBlock().textContent!;
    expect(text).toContain("USD4.8%");
    expect(text).not.toContain("2.5%");
    expect(text).not.toContain("2.3%");
  });
});
