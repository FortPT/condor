/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NativeFlowsPanel } from "./NativeFlowsPanel";
import type { FlowsResponse, NativeFlow } from "./nativeFlows";

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.fetch }));

const flow = (over: Partial<NativeFlow> = {}): NativeFlow => ({
  id: "a".repeat(32), venue: "revolut_x", ts: 1_793_188_800, currency: "EUR", amount: "4.27",
  usd_value_at_time: 4.7, source: "manual", status: "confirmed", note: "Top-up", external_id: null, ...over,
});
const summary = (over: Record<string, unknown> = {}) => ({
  venue: "revolut_x", label: "Revolut X", recorded_net_flows_usd: 0, confirmed_count: 0, pending_count: 0,
  pending_net_usd: 0, unpriced_count: 0, tracked: false, fetch: null, note: "", ...over,
});

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
let data: FlowsResponse;
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
const calls = () => mocks.fetch.mock.calls as [string, { method?: string; body?: string } | undefined][];
const button = (label: string | RegExp) => Array.from(host.querySelectorAll("button")).find((b) =>
  typeof label === "string" ? b.textContent === label : label.test(b.textContent ?? ""))!;
const field = (label: string) => host.querySelector<HTMLInputElement | HTMLSelectElement>(`[aria-label="${label}"]`)!;

async function type(label: string, value: string) {
  const element = field(label);
  const proto = element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(element, value);
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}

function build(response: FlowsResponse, overrides: Record<string, (init?: { method?: string; body?: string }) => unknown> = {}) {
  data = response;
  mocks.fetch.mockReset().mockImplementation((path: string, init?: { method?: string; body?: string }) => {
    for (const [suffix, handler] of Object.entries(overrides)) if (path.endsWith(suffix)) return Promise.resolve(handler(init));
    if (path.startsWith("/api/v1/native-flows?")) return Promise.resolve(data);
    if (path.endsWith("/sync")) return Promise.resolve({ results: [{ venue: "revolut_x", ok: true, reason: "", new: 1, detected: 0 }] });
    if (path.endsWith("/confirm")) return Promise.resolve(flow({ status: "confirmed" }));
    if (path.endsWith("/dismiss")) return Promise.resolve(flow({ status: "dismissed" }));
    if (init?.method === "DELETE") return Promise.resolve({ deleted: "x" });
    if (init?.method === "POST") return Promise.resolve(flow());
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}
async function mount() {
  await act(async () => root.render(
    <QueryClientProvider client={qc}><NativeFlowsPanel venue="revolut_x" title="Revolut X" defaultCurrency="EUR" /></QueryClientProvider>));
  await wait();
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
});
afterEach(async () => {
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
});

describe("NativeFlowsPanel", () => {
  it("asks only for this venue's flows and says nothing is recorded yet", async () => {
    build({ flows: [], venues: [summary()] });
    await mount();
    expect(calls()[0][0]).toBe("/api/v1/native-flows?venue=revolut_x");
    const text = host.textContent!;
    expect(text).toContain("No deposits or withdrawals recorded.");
    expect(text).toContain("Nothing is recorded for Revolut X yet");
    expect(text).toContain("a deposit reads as a gain");
    expect(text).toContain("has not been read yet");
  });

  it("lists recorded flows with their USD value and calls the PnL a trading result", async () => {
    build({
      flows: [flow(), flow({ id: "b".repeat(32), amount: "-1.5", currency: "USDC", usd_value_at_time: -1.5, source: "api", note: "Revolut X send" })],
      venues: [summary({ tracked: true, confirmed_count: 2, recorded_net_flows_usd: 3.2, fetch: { ts: 1_793_000_000, ok: true, reason: "", count: 2 } })],
    });
    await mount();
    const text = host.textContent!;
    expect(text).toContain("is a trading result");
    expect(text).toContain("Recorded net flows +$3.20 across 2 confirmed entries");
    const rows = Array.from(host.querySelectorAll("tbody tr")).map((row) => row.textContent);
    expect(rows[0]).toContain("Deposit");
    expect(rows[0]).toContain("4.27 EUR");
    expect(rows[0]).toContain("+$4.70");
    expect(rows[1]).toContain("Withdrawal");
    expect(rows[1]).toContain("-$1.50");
    expect(rows[1]).toContain("Venue history");
    // A hand-entered row can be deleted; a venue row can only be dismissed.
    expect(rows[0]).toContain("Delete");
    expect(rows[1]).toContain("Dismiss");
  });

  it("shows a pending detection with Confirm and Dismiss and posts the choice", async () => {
    const pending = flow({ id: "c".repeat(32), source: "detected", status: "pending", note: "EUR balance rose with no matching bot order" });
    build({ flows: [pending], venues: [summary({ pending_count: 1, pending_net_usd: 4.7, note: "Unconfirmed cash movement of +$4.70 (1 item) is not subtracted until you confirm it." })] });
    await mount();
    const block = host.querySelector("[data-flows-pending]")!;
    expect(block.textContent).toContain("Unconfirmed cash movement of +$4.70");
    expect(block.textContent).toContain("Detected");
    await act(async () => button("Confirm").click());
    await wait();
    expect(calls().some(([path, init]) => path === `/api/v1/native-flows/${pending.id}/confirm` && init?.method === "POST")).toBe(true);
    await act(async () => button("Dismiss").click());
    await wait();
    expect(calls().some(([path, init]) => path === `/api/v1/native-flows/${pending.id}/dismiss` && init?.method === "POST")).toBe(true);
  });

  it("shows a visible pending label while a confirmation is in flight and the error if it fails", async () => {
    const pending = flow({ id: "d".repeat(32), source: "detected", status: "pending" });
    build({ flows: [pending], venues: [summary({ pending_count: 1, pending_net_usd: 4.7 })] });
    mocks.fetch.mockImplementation((path: string) => {
      if (path.startsWith("/api/v1/native-flows?")) return Promise.resolve(data);
      return new Promise((_, reject) => setTimeout(() => reject(new Error("Flow not found")), 40));
    });
    await mount();
    await act(async () => button("Confirm").click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    expect(button("Confirming…")).toBeTruthy();
    expect(button("Confirming…").disabled).toBe(true);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 80)); });
    expect(host.querySelector("[role='alert']")!.textContent).toBe("Flow not found");
  });

  it("adds a withdrawal as a negative amount with the date and note, then clears the form", async () => {
    build({ flows: [], venues: [summary()] });
    await mount();
    expect(button("Add entry").disabled).toBe(true);
    await type("Type", "withdrawal");
    await type("Amount", "3.5");
    await type("Currency", "usdc");
    await type("Date", "2026-10-28");
    await type("Note", "to cold wallet");
    await type("USD value", "3.5");
    expect(button("Add entry").disabled).toBe(false);
    await act(async () => button("Add entry").click());
    await wait();
    const post = calls().find(([path, init]) => path === "/api/v1/native-flows" && init?.method === "POST")!;
    expect(JSON.parse(post[1]!.body!)).toEqual({
      venue: "revolut_x", amount: "-3.5", currency: "USDC", ts: "2026-10-28", note: "to cold wallet", usd_value: "3.5",
    });
    expect(host.textContent).toContain("Saved.");
    expect(field("Amount").value).toBe("");
  });

  it("explains a malformed amount, refuses to submit it, and shows the server's refusal", async () => {
    build({ flows: [], venues: [summary()] });
    await mount();
    await type("Amount", "-5");
    expect(host.textContent).toContain("Enter the amount as a positive number");
    expect(button("Add entry").disabled).toBe(true);
    build({ flows: [], venues: [summary()] }, {
      "/native-flows": () => { throw new Error("No USD price for DOGE is available; enter its USD value"); },
    });
    await type("Amount", "5");
    await act(async () => button("Add entry").click());
    await wait();
    expect(host.querySelector("[role='alert']")!.textContent).toContain("enter its USD value");
  });

  it("re-reads the history and reports what happened, including an unavailable venue", async () => {
    build({ flows: [], venues: [summary()] }, {
      "/sync": () => ({ results: [{ venue: "revolut_x", ok: false, reason: "unavailable: transactions: HTTP 403", detected: 1 }] }),
    });
    await mount();
    await act(async () => button("Re-read venue history").click());
    await wait();
    const post = calls().find(([path]) => path.endsWith("/sync"))!;
    expect(post[1]!.method).toBe("POST");
    expect(JSON.parse(post[1]!.body!)).toEqual({ venue: "revolut_x" });
    expect(host.querySelector("[data-flows-sync]")!.textContent).toBe("History not read: unavailable: transactions: HTTP 403; 1 detected.");
  });

  it("deletes a hand-entered row", async () => {
    build({ flows: [flow()], venues: [summary({ tracked: true, confirmed_count: 1, recorded_net_flows_usd: 4.7 })] });
    await mount();
    await act(async () => button("Delete").click());
    await wait();
    expect(calls().some(([path, init]) => path === `/api/v1/native-flows/${"a".repeat(32)}` && init?.method === "DELETE")).toBe(true);
  });

  it("says when the list cannot be read instead of going blank", async () => {
    mocks.fetch.mockReset().mockRejectedValue(new Error("Request failed: 500"));
    await mount();
    expect(host.querySelector("[role='alert']")!.textContent).toBe("Request failed: 500");
    expect(host.textContent).not.toContain("No deposits or withdrawals recorded.");
  });
});
