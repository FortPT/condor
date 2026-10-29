import { describe, expect, it } from "vitest";

import type { ConnectorBalance } from "@/lib/api";
import { paperConnectors, realConnectors, realShare, realTokenCount, realTokenTotals } from "./portfolio-paper";

const real: ConnectorBalance = { connector: "Kraken xStocks", balances: [{ token: "USD", total: 75, available: 75, usd_value: 75 }], total_usd: 75 };
const paper: ConnectorBalance = { connector: "Kraken xStocks (paper)", balances: [{ token: "USD", total: 99, available: 99, usd_value: 99 }], total_usd: 99, paper: true };

describe("simulated connectors on the Portfolio page", () => {
  it("keeps the paper account out of every real figure", () => {
    expect(realConnectors([real, paper])).toEqual([real]);
    expect(realConnectors([paper])).toEqual([]);
    expect(realConnectors([])).toEqual([]);
  });

  it("gives the paper account no share of the real total and real accounts their normal one", () => {
    // the server total is 75: the 99 of paper money is not in it
    expect(realShare(paper, 75)).toBe(0);
    expect(realShare(real, 75)).toBe(100);
    expect(realShare(real, 0)).toBe(0);
  });
});

describe("shared helpers every portfolio consumer uses", () => {
  it("lists the paper accounts apart", () => {
    expect(paperConnectors([real, paper])).toEqual([paper]);
    expect(paperConnectors([real])).toEqual([]);
  });

  it("sums per token over the real connectors only", () => {
    const other: ConnectorBalance = { connector: "kraken", balances: [{ token: "USD", total: 5, available: 5, usd_value: 5 }, { token: "EUR", total: 4, available: 4, usd_value: 4.5 }], total_usd: 9.5 };
    const totals = realTokenTotals([real, paper, other]);
    expect(totals.get("USD")).toBe(80);
    expect(totals.get("EUR")).toBe(4.5);
    expect([...totals.keys()].sort()).toEqual(["EUR", "USD"]);
    expect(realTokenTotals([paper]).size).toBe(0);
  });

  it("counts the real balances only", () => {
    expect(realTokenCount([real, paper])).toBe(1);
    expect(realTokenCount([paper])).toBe(0);
  });
});
