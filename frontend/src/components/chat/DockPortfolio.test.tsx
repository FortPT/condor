/**
 * The desk's Portfolio summary beside the chat.
 *
 * The change beside the total used to be the first point of the history against the
 * last, which mixed sources that start at different times (+491% on a 55 EUR desk).
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
  },
}));

// Display currency is the quote itself: figures print as dollars, unconverted.
vi.mock("@/hooks/useRates", () => ({
  useRates: () => ({
    convert: (value: number) => ({ value }),
    currencySymbol: "$",
  }),
}));

const { DockPortfolio } = await import("./DockPortfolio");

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

const bal = (token: string, usd: number) => ({
  token,
  total: usd,
  available: usd,
  usd_value: usd,
});

/** The live desk: three real accounts (61.87 USD). */
const PORTFOLIO: PortfolioResponse = {
  server: "FortPT VM-08",
  total_usd: 61.87,
  connectors: [
    { connector: "solana-mainnet-beta", total_usd: 0, balances: [] },
    { connector: "kraken", total_usd: 10.19, balances: [bal("EUR", 10.19)] },
    {
      connector: "Crypto.com",
      total_usd: 46.93,
      balances: [bal("USD", 30), bal("HYPE", 9.4), bal("SOL", 7.53)],
    },
    { connector: "Revolut X", total_usd: 4.75, balances: [bal("XRP", 4.75)] },
  ],
};

const POINTS = [
  { timestamp: 1, total_usd: 9.36 },
  { timestamp: 2, total_usd: 55.38 },
];

const HISTORY_UNKNOWN: PortfolioHistoryResponse = {
  server: "FortPT VM-08",
  interval: "5m",
  points: POINTS,
  change: { available: false, reason: "different_start", note: "History starts at different times." },
};

let container: HTMLDivElement;
let root: Root;
let qc: QueryClient;

async function render() {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <DockPortfolio server="FortPT VM-08" />
        </QueryClientProvider>
      </MemoryRouter>,
    );
  });
  for (let i = 0; i < 4; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

/** The printed share of each breakdown row (its last cell). */
const shares = () =>
  [...container.querySelectorAll('[data-testid="portfolio-breakdown"] li')].map((li) =>
    Number(/(\d+)%/.exec(li.lastElementChild?.textContent ?? "")?.[1]),
  );
const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  getPortfolio.mockReset().mockResolvedValue(PORTFOLIO);
  getPortfolioHistory.mockReset().mockResolvedValue(HISTORY_UNKNOWN);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  qc.clear();
});

describe("DockPortfolio breakdown", () => {
  it("prints the server's real total and shares that add up", async () => {
    await render();
    expect(container.textContent).toContain("$61.87");
    expect(sum(shares())).toBeGreaterThanOrEqual(99);
    expect(sum(shares())).toBeLessThanOrEqual(101);
  });
});

describe("DockPortfolio by asset", () => {
  it("lists a token held on two connectors once, summed", async () => {
    getPortfolio.mockResolvedValue({
      server: "FortPT VM-08",
      total_usd: 152,
      connectors: [
        { connector: "Trading 212", total_usd: 90, balances: [bal("USD", 40), bal("TSLA", 50)] },
        { connector: "Crypto.com", total_usd: 62, balances: [bal("USD", 26), bal("HYPE", 36)] },
      ],
    });
    await render();
    const rows = [...container.querySelectorAll('[data-testid="portfolio-breakdown"] li')].map(
      (li) => li.textContent ?? "",
    );
    expect(rows.filter((row) => row.startsWith("USD"))).toHaveLength(1);
    expect(rows).toHaveLength(3);
    // 66 of 152 is 43%, the sum, not 26% or 17%.
    expect(rows.find((row) => row.startsWith("USD"))).toContain("43%");
  });
});

describe("DockPortfolio change figure", () => {
  it("shows a dash with the reason, never +491%, when history starts at different times", async () => {
    await render();
    expect(container.textContent).not.toContain("491");
    expect(container.textContent).not.toContain("%+");
    const unknown = container.querySelector('[data-testid="portfolio-change-unknown"]');
    expect(unknown).not.toBeNull();
    expect(unknown!.textContent).toContain("—");
    expect(unknown!.getAttribute("title")).toContain("different times");
  });

  it("shows the server's net-of-flows change for a clean window", async () => {
    getPortfolioHistory.mockResolvedValue({
      ...HISTORY_UNKNOWN,
      change: { available: true, change_usd: 0.62, change_pct: 1.01, note: null },
    });
    await render();
    expect(container.textContent).toContain("+$0.62 (+1.01%)");
    expect(container.querySelector('[data-testid="portfolio-change-unknown"]')).toBeNull();
  });
});
