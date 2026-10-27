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
const quotation = { id: "q1", countdown: 15,
  from_amount: { amount: "5", currency: "EUR" },
  to_amount: { amount: "0.001", currency: "BTC" } };

beforeEach(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  request.mockReset();
  request.mockImplementation((path: string) => Promise.resolve(path.endsWith("/quote") ? quotation : {
    connected: true, enabled: true, ledger: { cash_eur: "30", positions: {}, uncertain_order: null },
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

async function quote() {
  const field = host.querySelector('input[inputmode="decimal"]') as HTMLInputElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, "5");
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
}

it("does not enable execution or submit orders on page load", () => {
  expect(request.mock.calls.map(([path]) => path)).toEqual(["/api/v1/crypto-com/status"]);
});

it("requests an exact decimal source amount and expires before order submission", async () => {
  const start = Date.now();
  await quote();
  const call = request.mock.calls.find(([path]) => path.endsWith("/quote"));
  expect(JSON.parse(call![1].body)).toEqual({ kind: "purchase", source: "EUR", target: "BTC", amount: "5" });
  vi.spyOn(Date, "now").mockReturnValue(start + 60000);
  const confirm = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Confirm & submit order")!;
  await act(async () => confirm.click());
  expect(request.mock.calls.some(([path]) => path.endsWith("/execute"))).toBe(false);
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
