/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CryptoComExchange } from "./CryptoComExchange";
import { limitsSummary } from "./cryptoExchangeLimits";

const request = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ apiFetch: request }));

const limits = {
  floor: "2", reserve_fraction: "0.5", position_fraction: "1/6", max_positions: 3,
  guards_apply_from: "1000", max_asset_fraction_below_threshold: "0.4", default_slippage_pct: "0.3",
};
let status: Record<string, unknown>;
let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
const button = (label: string) =>
  Array.from(host.querySelectorAll("button")).find((b) => b.textContent === label) as HTMLButtonElement;
async function type(field: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const calls = (method?: string) => request.mock.calls.filter(([, init]) => (method ? init?.method === method : !init));

async function mount(settings = false) {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => { root.render(<QueryClientProvider client={qc}><MemoryRouter><CryptoComExchange settings={settings} /></MemoryRouter></QueryClientProvider>); });
  await wait();
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  status = {
    connected: true, environment: "production", enabled: false, limits,
    ledger: { quote: "USD", cash: "30", positions: {}, uncertain_order: null, enabled: false, orders: [], floor: "2", synced: true },
  };
  request.mockReset();
  request.mockImplementation((path: string) => Promise.resolve(path.endsWith("/status") ? status : {}));
});
afterEach(async () => {
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
  vi.restoreAllMocks();
});

it("does not enable execution or submit orders on page load", async () => {
  await mount();
  expect(request.mock.calls.map(([path]) => path)).toEqual(["/api/v1/crypto-exchange/status"]);
});

it("describes the limits the server reports in the ledger quote currency", async () => {
  await mount();
  expect(host.textContent).toContain("no single coin may exceed 40% of bot equity");
  expect(host.textContent).toContain("From 1000 USD: 50% of bot equity stays in cash, at most 3 positions");
  expect(host.textContent).toContain("immediate-or-cancel");
  expect(host.textContent).not.toContain("€");
  expect(host.textContent).not.toContain("up to three positions and one sixth");
});

it("limitsSummary names the quote currency and falls back to the strict regime", () => {
  expect(limitsSummary({ ...limits }, "EUR")).toContain("Below 1000 EUR total bot equity");
  expect(limitsSummary({ floor: "2", reserve_fraction: "0.5", position_fraction: "1/6", max_positions: 3 }, "USDT"))
    .toContain("New exposure stops at 2 USDT total bot equity");
  expect(limitsSummary(undefined)).toBe("");
});

it("has no order form: trading lives in the Trade workspace", async () => {
  await mount();
  expect(host.querySelector("form")).toBeNull();
  expect(host.textContent).not.toContain("Preview order");
});

it("shows account balances as the provider reports them without rounding quantities", async () => {
  await mount();
  request.mockResolvedValue({ accounts: [
    { currency: "USD", balance: "30.12", available: "30.12" },
    { currency: "BTC", balance: "0.000000123456789", available: "0.0000001" },
  ] });
  await act(async () => button("Check connection & balances").click());
  await wait();
  expect(host.querySelectorAll("table")).toHaveLength(1);
  expect(host.textContent).toContain("30.12");
  expect(host.textContent).toContain("0.000000123456789");
});

it("takes the key, secret and environment in settings and never echoes them back", async () => {
  await mount(true);
  const [key, secret] = Array.from(host.querySelectorAll('input[type="password"]')) as HTMLInputElement[];
  expect(host.querySelectorAll('input[type="password"]')).toHaveLength(2);
  await type(key, "the-key");
  await type(secret, "the-secret");
  const selects = host.querySelectorAll("select");
  const environment = selects[selects.length - 1] as HTMLSelectElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(environment, "uat");
    environment.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await wait();
  const [, init] = calls("PUT")[0];
  expect(JSON.parse(init.body)).toEqual({ api_key: "the-key", api_secret: "the-secret", environment: "uat" });
  expect(key.value).toBe("");
  expect(secret.value).toBe("");
  expect(host.textContent).not.toContain("the-secret");
});

it("syncs the allocation from the Exchange account and shows cash, positions and skipped coins", async () => {
  await mount(true);
  request.mockImplementation((path: string, init?: { method?: string }) => Promise.resolve(path.endsWith("/sync")
    ? { ...status, ledger: { ...(status.ledger as object), cash: "42.5", positions: { BTC: { units: "0.01", cost: "700" } }, synced: true }, skipped: ["XYZ"] }
    : init ? {} : status));
  await act(async () => button("Sync allocation from Exchange account").click());
  await wait();
  const sync = calls("POST").find(([path]) => path.endsWith("/sync"))!;
  expect(JSON.parse(sync[1].body)).toEqual({ quote: "USD" });
  const text = host.querySelector('[role="status"]')!.textContent!;
  expect(text).toContain("cash 42.5 USD");
  expect(text).toContain("BTC 0.01");
  expect(text).toContain("skipped (no USD pair or price): XYZ");
});

it("will not sync while trading is enabled, nor enable before the first sync", async () => {
  status = { ...status, enabled: true, ledger: { ...(status.ledger as object), enabled: true } };
  await mount(true);
  expect(button("Sync allocation from Exchange account").disabled).toBe(true);
  expect(host.textContent).toContain("Pause trading to sync");
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
  status = { ...status, enabled: false, ledger: { ...(status.ledger as object), enabled: false, synced: false } };
  await mount(true);
  expect(button("Enable trading within allocation").disabled).toBe(true);
  expect(host.textContent).toContain("has not been synced");
});

it("toggles trading and shows the server's refusal text", async () => {
  await mount(true);
  request.mockImplementation((path: string, init?: { method?: string }) => path.endsWith("/enabled")
    ? Promise.reject(new Error("Trading cannot be enabled before the first sync"))
    : Promise.resolve(init ? {} : status));
  await act(async () => button("Enable trading within allocation").click());
  await wait();
  expect(JSON.parse(calls("PUT").at(-1)![1].body)).toEqual({ enabled: true });
  expect(host.querySelector('[role="alert"]')!.textContent).toContain("before the first sync");
});

it("offers reconcile only for an uncertain order and keeps trading paused", async () => {
  await mount(true);
  expect(button("Reconcile from Exchange order history")).toBeUndefined();
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
  status = { ...status, ledger: { ...(status.ledger as object), uncertain_order: { client_oid: "c1", side: "buy", symbol: "BTC" } } };
  await mount(true);
  expect(host.querySelector('[role="alert"]')!.textContent).toContain("needs reconciliation");
  expect(button("Enable trading within allocation").disabled).toBe(true);
  expect(button("Sync allocation from Exchange account").disabled).toBe(true);
  await act(async () => button("Reconcile from Exchange order history").click());
  await wait();
  expect(calls("POST").some(([path]) => path.endsWith("/reconcile"))).toBe(true);
});

it("disconnects through DELETE /credentials", async () => {
  await mount(true);
  await act(async () => button("Disconnect").click());
  await wait();
  expect(request.mock.calls.some(([path, init]) => path.endsWith("/credentials") && init?.method === "DELETE")).toBe(true);
});
