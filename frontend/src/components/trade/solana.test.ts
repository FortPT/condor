import { describe, expect, it } from "vitest";

import { swapResults, type Swap } from "./solana";

const swap = (hash: string, at: string, side: string, input: number, output: number, status = "CONFIRMED"): Swap => ({
  status, side, trading_pair: "SOL-USDC", base_token: "SOL", quote_token: "USDC",
  input_amount: input, output_amount: output, transaction_hash: hash, timestamp: at,
});

describe("swapResults", () => {
  it("gives a buy nothing and a sale its USDC proceeds minus the average cost of the units sold", () => {
    const results = swapResults([
      swap("sell1", "2026-11-02T00:00:00Z", "sell", 0.1, 10),
      swap("buy1", "2026-11-01T00:00:00Z", "buy", 8, 0.1),
    ]);
    expect(results.get("buy1")).toEqual({ kind: "none" });
    const sale = results.get("sell1");
    expect(sale).toMatchObject({ kind: "known" });
    if (sale?.kind === "known") {
      expect(sale.usd).toBeCloseTo(2);
      expect(sale.pct).toBeCloseTo(25);
    }
  });

  it("uses one running average cost across several buys", () => {
    const results = swapResults([
      swap("b1", "2026-11-01T00:00:00Z", "buy", 10, 0.1),
      swap("b2", "2026-11-01T01:00:00Z", "buy", 12, 0.1),
      swap("s1", "2026-11-01T02:00:00Z", "sell", 0.1, 12),
    ]);
    const sale = results.get("s1");
    expect(sale?.kind === "known" && sale.usd).toBeCloseTo(1);
  });

  it("says unknown for a sale of more than the swaps shown ever bought", () => {
    const results = swapResults([
      swap("b1", "2026-11-01T00:00:00Z", "buy", 8, 0.1),
      swap("s1", "2026-11-01T01:00:00Z", "sell", 0.3, 30),
    ]);
    expect(results.get("s1")).toEqual({ kind: "unknown" });
  });

  it("ignores swaps that did not confirm", () => {
    const results = swapResults([swap("f1", "2026-11-01T00:00:00Z", "sell", 0.1, 10, "FAILED")]);
    expect(results.size).toBe(0);
  });
});
