/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { failureText } from "./failureText";
import { KrakenXStocks } from "./KrakenXStocks";

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
const alerts = () => Array.from(host.querySelectorAll('[role="alert"]')).map((node) => node.textContent);

async function mount() {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => { root.render(<QueryClientProvider client={qc}><KrakenXStocks /></QueryClientProvider>); });
  await wait();
}
async function remount(next: Record<string, unknown>) {
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
  status = next;
  await mount();
}
const ledgerOf = (extra: object) => ({ ...(status.ledger as object), ...extra });

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  status = {
    connected: true, venue: "Kraken xStocks", quote: "USD", enabled: false, limits,
    market: { open: true, new_york_time: "Wed 11:00", minutes_to_close: 300 },
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

it("does not enable execution, sync or order on page load", async () => {
  await mount();
  expect(request.mock.calls.map(([path]) => path)).toEqual(["/api/v1/kraken-xstocks/status"]);
});

it("describes the server's limits and the USD-only, session-only rule", async () => {
  await mount();
  expect(host.textContent).toContain("Only Kraken xStocks pairs quoted in USD");
  expect(host.textContent).toContain("regular US session");
  expect(host.textContent).toContain("no single stock may exceed 40% of bot equity");
  expect(host.textContent).toContain("From 1000 USD: 50% of bot equity stays in cash, at most 3 positions");
  expect(host.textContent).toContain("US market: open (300 min to the close)");
  expect(host.textContent).toContain("immediate-or-cancel");
  expect(host.querySelectorAll("form")).toHaveLength(1);
  expect(host.textContent).not.toContain("Preview order");
});

it("takes the key and secret, never echoes them and sends nothing else", async () => {
  await mount();
  const fields = Array.from(host.querySelectorAll('input[type="password"]')) as HTMLInputElement[];
  expect(fields).toHaveLength(2);
  await type(fields[0], "the-key");
  await type(fields[1], "the-secret");
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await wait();
  const [path, init] = calls("PUT")[0];
  expect(path).toBe("/api/v1/kraken-xstocks/credentials");
  expect(JSON.parse(init.body)).toEqual({ api_key: "the-key", api_secret: "the-secret" });
  expect(fields[0].value).toBe("");
  expect(fields[1].value).toBe("");
  expect(host.textContent).not.toContain("the-secret");
  expect(host.textContent).toContain("API key saved. Trading is paused");
});

it("syncs the allocation and shows cash, positions and skipped assets", async () => {
  await mount();
  request.mockImplementation((path: string, init?: { method?: string }) => Promise.resolve(path.endsWith("/sync")
    ? { ...status, ledger: ledgerOf({ cash: "42.5", positions: { AAPL: { units: "0.1", cost: "34.5" } }, synced: true }), skipped: ["XXBT"] }
    : init ? {} : status));
  await act(async () => button("Sync allocation from Kraken account").click());
  await wait();
  const sync = calls("POST").find(([path]) => path.endsWith("/sync"))!;
  expect(sync[0]).toBe("/api/v1/kraken-xstocks/sync");
  const text = host.querySelector('[role="status"]')!.textContent!;
  expect(text).toContain("cash 42.5 USD");
  expect(text).toContain("AAPL 0.1");
  expect(text).toContain("skipped (not an online xStock or no price): XXBT");
});

it("shows a visible pending state while a sync is running", async () => {
  await mount();
  let finish: (value: unknown) => void = () => {};
  request.mockImplementation((path: string, init?: { method?: string }) => path.endsWith("/sync")
    ? new Promise((resolve) => { finish = resolve; })
    : Promise.resolve(init ? {} : status));
  await act(async () => button("Sync allocation from Kraken account").click());
  expect(button("Syncing…").disabled).toBe(true);
  expect(button("Syncing…").getAttribute("aria-busy")).toBe("true");
  await act(async () => finish({ ...status, skipped: [] }));
  await wait();
  expect(button("Sync allocation from Kraken account")).toBeDefined();
});

it("never lets a failed sync or toggle pass silently, even with an empty reason", async () => {
  await mount();
  request.mockImplementation((path: string, init?: { method?: string }) => {
    if (path.endsWith("/sync")) return Promise.reject(new Error("Kraken unavailable"));
    if (path.endsWith("/enabled")) return Promise.reject(new Error(""));
    return Promise.resolve(init ? {} : status);
  });
  await act(async () => button("Sync allocation from Kraken account").click());
  await wait();
  expect(alerts()).toContain("Sync allocation failed: Kraken unavailable");
  await act(async () => button("Enable trading within allocation").click());
  await wait();
  expect(JSON.parse(calls("PUT").at(-1)![1].body)).toEqual({ enabled: true });
  expect(alerts().some((text) => text!.startsWith("Enable/pause trading failed: the server gave no reason"))).toBe(true);
  expect(failureText(undefined)).toContain("no reason");
  expect(failureText("plain text")).toBe("plain text");
});

it("shows a failed status load instead of an empty page", async () => {
  request.mockImplementation(() => Promise.reject(new Error("Request failed: 500")));
  await mount();
  expect(alerts()).toContain("Loading status failed: Request failed: 500");
  expect(button("Sync allocation from Kraken account").disabled).toBe(true);
  expect(host.textContent).toContain("Sync is unavailable: waiting for the status to load");
});

it("says why a button is disabled instead of doing nothing", async () => {
  const healthy = status;
  await mount();
  await remount({ ...healthy, connected: false });
  expect(button("Sync allocation from Kraken account").disabled).toBe(true);
  expect(host.textContent).toContain("Sync is unavailable: save an API key first");
  expect(host.textContent).toContain("Enabling is unavailable: save an API key first");
  expect(button("Disconnect").disabled).toBe(true);
  await remount({ ...healthy, enabled: true, ledger: { ...(healthy.ledger as object), enabled: true } });
  expect(button("Sync allocation from Kraken account").disabled).toBe(true);
  expect(host.textContent).toContain("Sync is unavailable: pause trading first");
  expect(button("Pause trading").disabled).toBe(false);
  await remount({ ...healthy, ledger: { ...(healthy.ledger as object), synced: false } });
  expect(button("Enable trading within allocation").disabled).toBe(true);
  expect(host.textContent).toContain("Enabling is unavailable: sync the allocation first");
  expect(host.textContent).toContain("has not been synced");
});

it("toggles trading and confirms it, or shows the server's refusal text", async () => {
  await mount();
  await act(async () => button("Enable trading within allocation").click());
  await wait();
  expect(JSON.parse(calls("PUT").at(-1)![1].body)).toEqual({ enabled: true });
  expect(host.textContent).toContain("Trading enabled within the allocation.");
  request.mockImplementation((path: string, init?: { method?: string }) => path.endsWith("/enabled")
    ? Promise.reject(new Error("Sync the bot allocation from the Kraken account first"))
    : Promise.resolve(init ? {} : status));
  await act(async () => button("Enable trading within allocation").click());
  await wait();
  expect(alerts().join(" ")).toContain("first");
});

it("offers reconcile only for an uncertain order and keeps trading paused", async () => {
  await mount();
  expect(button("Reconcile from Kraken order history")).toBeUndefined();
  await remount({ ...status, ledger: ledgerOf({ uncertain_order: { client_oid: "c1", side: "buy", symbol: "AAPL" } }) });
  expect(alerts().join(" ")).toContain("needs reconciliation");
  expect(button("Enable trading within allocation").disabled).toBe(true);
  expect(button("Sync allocation from Kraken account").disabled).toBe(true);
  await act(async () => button("Reconcile from Kraken order history").click());
  await wait();
  expect(calls("POST").some(([path]) => path.endsWith("/reconcile"))).toBe(true);
  expect(host.textContent).toContain("Reconciled from Kraken's order history");
});

it("disconnects through DELETE /credentials", async () => {
  await mount();
  await act(async () => button("Disconnect").click());
  await wait();
  expect(request.mock.calls.some(([path, init]) => path.endsWith("/credentials") && init?.method === "DELETE")).toBe(true);
  expect(host.textContent).toContain("Disconnected.");
});
