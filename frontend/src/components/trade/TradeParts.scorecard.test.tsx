/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ScorecardPanel } from "./TradeParts";
import { pct } from "./tradeFormat";

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.fetch }));
vi.mock("lightweight-charts", () => ({}));

const summary = { total: 0, resolved: 0, open: 0, hit_target: 0, stopped: 0, expired: 0, avg_gross_pct: null, avg_net_pct: null, beat_costs: 0 };
const card = (cost: number) => ({
  assumptions: { round_trip_cost_pct: cost, basis: "x" }, prices: { snapshots: 10, coins: 3, days: 1 },
  trade_calls: { ...summary, total: 1, resolved: 1, hit_target: 1, avg_net_pct: 1.59, beat_costs: 1 },
  pass_calls: { ...summary }, by_cost_multiple: [], baseline_gross_pct: null, edge_vs_baseline_pct: null,
  enough_data: false, recent: [],
});

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

async function panelText(cost: number, venue: string, noun = "coins") {
  mocks.fetch.mockResolvedValue(card(cost));
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => root.render(
    <QueryClientProvider client={qc}><ScorecardPanel base="/api/v1/x" queryKey={`k-${venue}`} venue={venue} noun={noun} /></QueryClientProvider>,
  ));
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
  return host.querySelector('section[aria-label="Scorecard"]')!.textContent!;
}

it("is a call scorecard that says it scores calls, not placed trades, with no paper or test wording", async () => {
  const text = await panelText(1.25, "Revolut X");
  expect(text).toContain("Call scorecard");
  expect(text).toContain("scored later against real Revolut X prices after a 1.25% round-trip cost. It scores the calls, not the trades that were actually placed; no money moves.");
  expect(text).toContain("Calls to trade");
  expect(text).not.toMatch(/(^|[^a-z])(paper|test)([^a-z]|$)|Trader scorecard/i);
});

it("never prints float noise in the round-trip cost", async () => {
  const text = await panelText(0.1 + 0.11, "Kraken xStocks", "stocks");
  expect(0.1 + 0.11).not.toBe(0.21);
  expect(text).toContain("after a 0.21% round-trip cost");
  expect(text).not.toContain("0.21000000000000002");
});

it("formats percentages with at most two decimals", () => {
  expect(pct(0.21000000000000002)).toBe("0.21");
  expect(pct(1)).toBe("1");
  expect(pct(1.2)).toBe("1.2");
  expect(pct(0.349)).toBe("0.35");
  expect(pct(null)).toBe("—");
  expect(pct(Number.NaN)).toBe("—");
});
