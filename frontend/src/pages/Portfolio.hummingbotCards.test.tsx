/**
 * The Bots and Executors cards show Hummingbot's bots and executors. An install that does not use them can
 * hide them (the `hide_hummingbot_pages` preference); the Portfolio and Agents cards stay.
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
  getPortfolio.mockReset().mockResolvedValue({
    server: "FortPT VM-08",
    total_usd: 100,
    connectors: [{ connector: "Crypto.com", total_usd: 100, balances: [{ token: "USD", total: 100, available: 100, usd_value: 100 }] }],
  });
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
});

const cardLabels = () =>
  Array.from(container.querySelectorAll("div, span"))
    .map((el) => el.textContent?.trim())
    .filter((text): text is string => text === "Portfolio" || text === "Bots" || text === "Executors" || text === "Agents");

describe("Portfolio summary cards", () => {
  it("shows the Bots and Executors cards by default", async () => {
    await render();
    const labels = cardLabels();
    expect(labels).toContain("Bots");
    expect(labels).toContain("Executors");
  });

  it("hides the Bots and Executors cards, and only those, when the Hummingbot pages are hidden", async () => {
    preference.hidden = true;
    await render();
    const labels = cardLabels();
    expect(labels).not.toContain("Bots");
    expect(labels).not.toContain("Executors");
    expect(labels).toContain("Portfolio");
    expect(labels).toContain("Agents");
  });
});
