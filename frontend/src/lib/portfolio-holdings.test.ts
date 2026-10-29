import { describe, expect, it } from "vitest";

import { topHoldings } from "./portfolio-holdings";

const bal = (token: string, usd_value: number) => ({ token, usd_value });

describe("Top Holdings", () => {
  it("lists a token held on two connectors once, summed", () => {
    const rows = topHoldings([
      { connector: "Kraken xStocks", balances: [bal("USD", 40), bal("TSLA", 1500)] },
      { connector: "Crypto.com", balances: [bal("USD", 26), bal("HYPE", 16.93)] },
    ]);
    expect(rows.map((row) => row.token)).toEqual(["TSLA", "USD", "HYPE"]);
    const usd = rows.find((row) => row.token === "USD")!;
    expect(usd.usd_value).toBe(66);
    expect(usd.connector).toBe("Kraken xStocks, Crypto.com");
    expect(rows.filter((row) => row.token === "USD")).toHaveLength(1);
  });

  it("keeps the largest eight, largest first, and ignores a value that is not a number", () => {
    const many = Array.from({ length: 12 }, (_, i) => bal(`T${i}`, i + 1));
    const rows = topHoldings([{ connector: "A", balances: [...many, bal("BAD", Number.NaN)] }]);
    expect(rows).toHaveLength(8);
    expect(rows[0]).toMatchObject({ token: "T11", usd_value: 12 });
    expect(rows.some((row) => row.token === "BAD")).toBe(false);
  });

  it("is empty without connectors", () => {
    expect(topHoldings([])).toEqual([]);
  });
});
