/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CryptoComApp } from "./CryptoComApp";

const request = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ apiFetch: request }));

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;

beforeEach(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  request.mockReset();
  request.mockImplementation(() => Promise.resolve({
    connected: true, enabled: true, ledger: { cash_eur: "30", positions: {}, uncertain_order: null },
    limits: { floor_eur: "2", reserve_fraction: "0.5", position_fraction: "1/6", max_positions: 3,
      guards_apply_from_eur: "1000", max_asset_fraction_below_threshold: "0.4" },
  }));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => { root.render(<QueryClientProvider client={qc}><MemoryRouter><CryptoComApp /></MemoryRouter></QueryClientProvider>); });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
});
afterEach(async () => {
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
  vi.restoreAllMocks();
});

it("does not enable execution or submit orders on page load", () => {
  expect(request.mock.calls.map(([path]) => path)).toEqual(["/api/v1/crypto-com/status"]);
});

it("describes the limits the server reports instead of fixed text", () => {
  expect(host.textContent).toContain("no single coin may exceed 40% of bot equity");
  expect(host.textContent).toContain("From €1000: 50% of bot equity stays in cash, at most 3 positions");
  expect(host.textContent).not.toContain("up to three positions and one sixth");
});

it("has no order form: trading lives in the Trade workspace", () => {
  expect(host.querySelector("form")).toBeNull();
  expect(host.textContent).not.toContain("Quote an order");
});

it("shows cash and crypto in their source currencies without rounding quantities", async () => {
  request.mockResolvedValue({ cash: [{ currency: "EUR", amount: { amount: "30.12", currency: "EUR" } }],
    crypto: [{ currency: "BTC", balance: { amount: "0.000000123456789", currency: "BTC" }, available: { amount: "0.0000001", currency: "BTC" } }] });
  const check = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Check connection & balances")!;
  await act(async () => check.click());
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
  expect(host.querySelectorAll("table")).toHaveLength(2);
  expect(host.textContent).toContain("30.12 EUR");
  expect(host.textContent).toContain("0.000000123456789 BTC");
});
