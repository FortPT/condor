import { describe, expect, it } from "vitest";

/**
 * The owner calls this venue "Crypto.com", never "Exchange": a label, tab, message or note the
 * user can read must not say "Crypto.com Exchange". The route (`/crypto-exchange`), the
 * settings tab key and other identifiers keep their names; only text is checked.
 */
const sources = import.meta.glob(["../**/*.ts", "../**/*.tsx", "!../**/*.test.*"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const FORBIDDEN = [
  "Crypto.com Exchange",
  "Exchange API key",
  "Exchange account",
  "Exchange order history",
  "Recent Exchange orders",
  "Exchange candles",
  "Trader scorecard",
  "Paper test",
];

describe("venue names the user reads", () => {
  it("scans the application source", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(100);
  });

  it.each(FORBIDDEN)("never shows %s", (phrase) => {
    const hits = Object.entries(sources)
      .filter(([, text]) => text.includes(phrase))
      .map(([file]) => file);
    expect(hits).toEqual([]);
  });
});
