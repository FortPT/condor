import { describe, expect, it } from "vitest";

import type { PortfolioHistoryResponse } from "@/lib/api";
import { historyChange, UNKNOWN_CHANGE_HINT } from "./portfolio-change";

const base = {
  server: "vm08",
  interval: "5m",
  // First 9.36, last 55.38: first-vs-last would read +491.7%.
  points: [
    { timestamp: 1, total_usd: 9.36 },
    { timestamp: 2, total_usd: 55.38 },
  ],
};

describe("the change beside a portfolio total", () => {
  it("is the server's figure for a clean window, net of flows, not first-vs-last", () => {
    const out = historyChange({
      ...base,
      change: { available: true, change_usd: 1.2, change_pct: 2.1, note: null },
    });
    expect(out).toEqual({ known: true, abs: 1.2, pct: 2.1, note: null });
  });

  it("keeps the amount when no percentage is meaningful", () => {
    const out = historyChange({ ...base, change: { available: true, change_usd: 3, change_pct: null } });
    expect(out).toMatchObject({ known: true, abs: 3, pct: null });
  });

  it("is unknown when sources start at different times, whatever the points say", () => {
    const out = historyChange({
      ...base,
      change: { available: false, reason: "different_start", note: "History starts at different times: Crypto.com has no value at the start." },
    });
    expect(out.known).toBe(false);
    if (!out.known) {
      expect(out.hint).toContain("different times");
      expect(out.hint).toContain(UNKNOWN_CHANGE_HINT);
    }
  });

  it("is unknown when the server sent no change at all (never derived from the points)", () => {
    expect(historyChange(base as PortfolioHistoryResponse).known).toBe(false);
    expect(historyChange(undefined).known).toBe(false);
    expect(historyChange({ ...base, change: null }).known).toBe(false);
  });

  it("is unknown when an available figure has no amount", () => {
    expect(historyChange({ ...base, change: { available: true } }).known).toBe(false);
  });
});
