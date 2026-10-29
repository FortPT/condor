/**
 * The desk's Portfolio summary beside the chat.
 *
 * Two live defects pinned here. The simulated (paper) account is not money: it was
 * summed into "By asset"/"By venue" (USD read 70% of a 146 EUR desk whose real total
 * was 55 EUR). And the change beside the total was the first point of the history
 * against the last, which mixed sources that start at different times (+491%).
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

/** The live desk: three real accounts (61.87 USD) and a 100 USD paper account. */
const PORTFOLIO: PortfolioResponse = {
  server: "FortPT VM-08",
  total_usd: 61.87,
  connectors: [
    { connector: "solana-mainnet-beta", total_usd: 0, balances: [] },
    { connector: "kraken", total_usd: 10.19, balances: [bal("EUR", 10.19)] },
    {
      connector: "Crypto.com Exchange",
      total_usd: 46.93,
      balances: [bal("USD", 30), bal("HYPE", 9.4), bal("SOL", 7.53)],
    },
    { connector: "Revolut X", total_usd: 4.75, balances: [bal("XRP", 4.75)] },
    {
      connector: "Kraken xStocks (paper)",
      total_usd: 100,
      paper: true,
      balances: [bal("USD", 100)],
    },
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

async function pick(label: RegExp) {
  const button = [...container.querySelectorAll("button")].find((b) => label.test(b.textContent ?? ""));
  await act(async () => {
    button!.click();
  });
}

const rows = () =>
  [...container.querySelectorAll('[data-testid="portfolio-breakdown"] li')].map((li) => li.textContent ?? "");

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

describe("DockPortfolio and the simulated account", () => {
  it("leaves the paper account out of the asset split and shares", async () => {
    await render();
    const text = rows().join("|");
    // USD is 30 of the real 61.87 (its own share of the real assets), never the 130 it
    // would be with the paper 100 added, and never 70%.
    expect(text).toContain("USD$30.00");
    expect(text).not.toContain("$130.00");
    expect(text).not.toContain("70%");
    // The listed shares are of the real assets only and add up to them.
    expect(sum(shares())).toBeGreaterThanOrEqual(99);
    expect(sum(shares())).toBeLessThanOrEqual(101);
    expect(container.querySelector('[data-testid="portfolio-paper"]')).toBeNull();
  });

  it("shows it in the venue split only as its own badged row, with no share", async () => {
    await render();
    await pick(/By venue/i);
    const paper = container.querySelector('[data-testid="portfolio-paper"]');
    expect(paper).not.toBeNull();
    expect(paper!.textContent).toMatch(/xstocks \(paper\)/i);
    expect(paper!.textContent).toContain("Simulated - not in total");
    expect(paper!.textContent).not.toContain("%");
    // And it is not one of the breakdown rows the bar and the shares are made of.
    expect(rows().join("|")).not.toContain("paper");
    expect(sum(shares())).toBeGreaterThanOrEqual(99);
    expect(sum(shares())).toBeLessThanOrEqual(101);
  });

  it("prints the server's real total", async () => {
    await render();
    expect(container.textContent).toContain("$61.87");
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
