import { describe, expect, it } from "vitest";

import type { ConnectorBalance } from "@/lib/api";
import { realConnectors, realShare } from "./portfolio-paper";

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
