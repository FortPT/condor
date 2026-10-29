/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Amount, HoldingsTotalsRow, PerformanceSummary, ResultCell } from "./PerformanceSummary";
import { resultsOf, totalsOf, type Performance } from "./performance";
import { makePerformance } from "./performance.fixture";

let root: Root;
let host: HTMLDivElement;

const render = (node: React.ReactNode) => act(async () => root.render(node));
const summary = (perf: Performance | undefined, error: Error | null = null, isLoading = false) =>
  render(<PerformanceSummary query={{ data: perf, error, isLoading }} />);
const cardOf = (key: string) => host.querySelector(`[data-card="${key}"]`)!;

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

describe("PerformanceSummary", () => {
  it("shows the six figures with their currency, green for a gain and red for a loss", async () => {
    await summary(makePerformance("EUR"));
    expect(cardOf("today").textContent).toMatch(/Today\+1[.,]50 USD/);
    expect(cardOf("since-start").textContent).toMatch(/-3[.,]25 USD/);
    expect(cardOf("realized").textContent).toMatch(/\+4[.,]50 EUR/);
    expect(cardOf("unrealized").textContent).toMatch(/\+3[.,]00 EUR/);
    expect(cardOf("fees").textContent).toMatch(/0[.,]07 EUR/);
    expect(cardOf("trades").textContent).toContain("5 · 1W / 1L");
    const tone = (key: string) => cardOf(key).querySelector("[data-tone]")!;
    expect(tone("today").getAttribute("data-tone")).toBe("up");
    expect(tone("today").className).toContain("--color-green");
    expect(tone("since-start").getAttribute("data-tone")).toBe("down");
    expect(tone("since-start").className).toContain("--color-red");
    expect(tone("fees").className).toContain("--color-text-muted");
  });

  it("explains each figure on hover", async () => {
    await summary(makePerformance("EUR"));
    const hints = Array.from(host.querySelectorAll("[data-card]")).map((c) => c.getAttribute("title")!);
    expect(hints).toHaveLength(6);
    expect(hints[0]).toContain("start of the UTC day");
    expect(hints[0]).toContain("deposits and withdrawals");
    expect(hints[2]).toContain("proceeds minus the average cost");
    expect(hints[3]).toContain("best bid");
    expect(hints[4]).toContain("Already included in Realized");
  });

  it("shows an unavailable figure as a dash with the reason, never as zero", async () => {
    const perf = makePerformance("USD", {
      today: { value: null, currency: "USD", since: null, since_label: null, partial: false, estimated: false, flows_usd: 0, reason: "No equity history recorded yet", note: null },
    });
    await summary(perf);
    expect(cardOf("today").querySelector("[data-tone]")!.textContent).toBe("—");
    expect(host.querySelector('[data-note="today"]')!.textContent).toBe("No equity history recorded yet");
  });

  it("says when a history starts after the window would", async () => {
    const base = makePerformance("USD").today!;
    await summary(makePerformance("USD", { today: { ...base, partial: true, since_label: "2026-10-28 05:00 UTC" } }));
    expect(host.querySelector('[data-note="today"]')!.textContent).toContain("history starts at 2026-10-28 05:00 UTC");
  });

  it("shows the loading state, a failed read and an unreadable ledger", async () => {
    await summary(undefined, null, true);
    expect(host.textContent).toContain("Reading performance");
    await summary(undefined, new Error("502 bad gateway"));
    expect(host.querySelector('[data-testid="performance-error"]')!.textContent).toContain("502 bad gateway");
    expect(host.querySelector("[data-card]")).toBeNull();
    await summary({ ...makePerformance("USD"), available: false, reason: "The Revolut X ledger could not be read" });
    expect(host.querySelector('[data-testid="performance-error"]')!.textContent).toContain("ledger could not be read");
    expect(host.querySelector("[data-card]")).toBeNull();
  });

  it("keeps the last figures and says the refresh failed", async () => {
    await summary(makePerformance("USD"), new Error("timeout"));
    expect(host.querySelectorAll("[data-card]")).toHaveLength(6);
    expect(host.querySelector('[data-testid="performance-stale"]')!.textContent).toContain("timeout");
  });

  it("appends the notes about this report to the standing explanation", async () => {
    await summary(makePerformance("USD", { notes: ["a", "b", "The order list starts after some positions were opened."] }));
    expect(host.textContent).toContain("The order list starts after some positions were opened.");
  });
});

describe("Amount, ResultCell and HoldingsTotalsRow", () => {
  it("colours an amount by its sign", async () => {
    await render(<><Amount value={2} currency="EUR" /><Amount value={-2} currency="EUR" /><Amount value={null} currency="EUR" /></>);
    const spans = Array.from(host.querySelectorAll("span"));
    expect(spans.map((s) => s.textContent)).toEqual([expect.stringMatching(/^\+2[.,]00 EUR$/), expect.stringMatching(/^-2[.,]00 EUR$/), "—"]);
    expect(spans[0].className).toContain("--color-green");
    expect(spans[1].className).toContain("--color-red");
  });

  it("shows a sale's amount and percent, a dash for a buy, and n/a with the reason", async () => {
    const results = resultsOf(makePerformance("EUR", {
      orders: [
        { time: 1, symbol: "A", order_id: "x", client_oid: null, pnl: 2, pnl_pct: 5, closed_position: false, reason: null },
        { time: 2, symbol: "A", order_id: "y", client_oid: null, pnl: null, pnl_pct: null, closed_position: false, reason: "no recorded cost for this sale" },
      ],
    }));
    await render(<table><tbody><tr>
      <ResultCell side="sell" orderId="x-1" results={results} currency="EUR" />
      <ResultCell side="sell" orderId="y-2" results={results} currency="EUR" />
      <ResultCell side="buy" orderId="x-1" results={results} currency="EUR" />
    </tr></tbody></table>);
    const cells = Array.from(host.querySelectorAll("td"));
    expect(cells[0].textContent).toMatch(/\+2[.,]00 EUR\+5[.,]00%/);
    expect(cells[1].textContent).toBe("n/a");
    expect(cells[1].getAttribute("title")).toBe("no recorded cost for this sale");
    expect(cells[2].textContent).toBe("—");
  });

  it("totals cash + holdings = equity and the total unrealized", async () => {
    const totals = totalsOf(40, [{ cost: 12, value: 15 }]);
    await render(<table><tbody><HoldingsTotalsRow totals={totals} quote="EUR" trailing={1} /></tbody></table>);
    const row = host.querySelector('tr[data-row="totals"]')!;
    expect(row.querySelectorAll("td")).toHaveLength(7);
    expect(row.textContent).toMatch(/Total \(cash \+ holdings\)/);
    expect(row.textContent).toMatch(/55[.,]00 EUR equity/);
    expect(row.textContent).toMatch(/\+3[.,]00 EUR/);
    expect(row.textContent).toMatch(/\+25[.,]00%/);
  });
});
