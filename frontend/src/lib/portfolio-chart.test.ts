import { describe, expect, it } from "vitest";

import type { PortfolioHistoryResponse } from "@/lib/api";
import {
  anomalyLabel,
  baselineNote,
  chartState,
  drawablePoints,
  emptyMessage,
  flowLabel,
  gapLabel,
  markerAnchor,
  pendingNote,
  recordingCount,
  resultToggle,
  segments,
  seriesFor,
  valuePoints,
} from "@/lib/portfolio-chart";

const time = (ts: number) => `t${ts}`;

const history = (over: Partial<PortfolioHistoryResponse> = {}): PortfolioHistoryResponse => ({
  server: "vm",
  interval: "1h",
  points: [
    { timestamp: 1, total_usd: 9 },
    { timestamp: 2, total_usd: 55 },
  ],
  chart_points: [
    { timestamp: 10, total_usd: 55 },
    { timestamp: 20, total_usd: 56 },
  ],
  baseline_ts: 10,
  series_start_ts: 10,
  series_start_reason: "all_accounts_recording_from",
  sources: [
    { source: "Hummingbot", first_ts: 1, status: "recording" },
    { source: "Crypto.com Exchange", first_ts: 5, status: "recording" },
    { source: "Revolut X", first_ts: 10, status: "recording" },
    { source: "Kraken xStocks", first_ts: null, status: "pending" },
  ],
  result_points: [
    { timestamp: 10, result_usd: 0 },
    { timestamp: 20, result_usd: 1 },
  ],
  ...over,
});

describe("series", () => {
  it("draws the server's chart series, not the staircase points", () => {
    expect(valuePoints(history()).map((p) => p.value)).toEqual([55, 56]);
  });

  it("falls back to the plain points on an older server", () => {
    expect(valuePoints(history({ chart_points: undefined })).map((p) => p.value)).toEqual([9, 55]);
  });

  it("keeps a missing total as a null, never a zero", () => {
    const h = history({
      chart_points: [
        { timestamp: 1, total_usd: 5 },
        { timestamp: 2, total_usd: null },
        { timestamp: 3, total_usd: 6 },
      ],
    });
    expect(valuePoints(h).map((p) => p.value)).toEqual([5, null, 6]);
  });

  it("the result mode draws the result series, and falls back to value when there is none", () => {
    expect(seriesFor(history(), "result").map((p) => p.value)).toEqual([0, 1]);
    expect(seriesFor(history({ result_points: null }), "result").map((p) => p.value)).toEqual([55, 56]);
    expect(seriesFor(history(), "value").map((p) => p.value)).toEqual([55, 56]);
  });

  it("splits at nulls so a gap is a break in the line", () => {
    const runs = segments([
      { value: 1 },
      { value: 2 },
      { value: null },
      { value: 3 },
      { value: null },
      { value: null },
      { value: 4 },
      { value: 5 },
    ]);
    expect(runs.map((r) => r.map((p) => p.value))).toEqual([[1, 2], [3], [4, 5]]);
  });

  it("counts the points that can be drawn", () => {
    expect(drawablePoints([{ timestamp: 1, value: null }, { timestamp: 2, value: 3 }])).toBe(1);
  });
});

describe("state", () => {
  it("is a line with two or more readings", () => {
    expect(chartState(history())).toBe("line");
  });

  it("is the empty state for a single reading or no common period, not a line", () => {
    expect(chartState(history({ chart_points: [{ timestamp: 1, total_usd: 5 }] }))).toBe("empty");
    expect(chartState(history({ chart_points: [] }))).toBe("empty");
    expect(
      chartState(
        history({
          chart_points: [
            { timestamp: 1, total_usd: null },
            { timestamp: 2, total_usd: 5 },
          ],
        }),
      ),
    ).toBe("empty");
  });

  it("is none with no history at all, and a line for an older server's points", () => {
    expect(chartState(history({ chart_points: [], points: [] }))).toBe("none");
    expect(chartState(undefined)).toBe("none");
    expect(chartState(history({ chart_points: undefined }))).toBe("line");
  });

  it("the empty message names accounts that are not recording yet", () => {
    const text = emptyMessage(history());
    expect(text).toMatch(/at least two readings/);
    expect(text).toContain("Kraken xStocks has no recorded value yet");
  });
});

describe("notes", () => {
  it("says from when, and how many accounts were recording", () => {
    expect(baselineNote(history(), time)).toBe(
      "Showing from t10, when all 3 accounts were recording. Earlier points only had some accounts.",
    );
    expect(recordingCount(history())).toBe(3);
  });

  it("is silent when the window starts at or after the baseline", () => {
    expect(baselineNote(history({ series_start_reason: null }), time)).toBeNull();
    expect(baselineNote(undefined, time)).toBeNull();
  });

  it("names pending accounts, singular and plural", () => {
    expect(pendingNote(history())).toBe("Kraken xStocks has no recorded value yet and is not in this chart.");
    expect(
      pendingNote(
        history({
          sources: [
            { source: "A", first_ts: null, status: "pending" },
            { source: "B", first_ts: null, status: "pending" },
          ],
        }),
      ),
    ).toBe("A, B have no recorded value yet and are not in this chart.");
    expect(pendingNote(history({ sources: [] }))).toBeNull();
  });

  it("labels a gap with the account and the stretch", () => {
    expect(gapLabel({ source: "Hummingbot", from_ts: 100, to_ts: 200 }, time)).toBe(
      "Hummingbot: no readings from t100 to t200",
    );
  });
});

describe("anomalies", () => {
  it("says which reading was left out and what was around it", () => {
    expect(
      anomalyLabel({ source: "Hummingbot", ts: 7, value: 20.62, neighbour_value: 10.19 }, time, (v) => `$${v}`),
    ).toBe("Hummingbot: one reading ($20.62) at t7 was left out as an outlier; the readings around it were about $10.19.");
  });
});

describe("flows", () => {
  const flow = { ts: 1, venue: "Revolut X", currency: "EUR", source: "api", status: "confirmed" };

  it("labels a deposit and a withdrawal", () => {
    expect(flowLabel({ ...flow, amount: "4.27" })).toBe("Deposit +4.27 EUR, Revolut X (confirmed)");
    expect(flowLabel({ ...flow, amount: "-3" })).toBe("Withdrawal -3 EUR, Revolut X (confirmed)");
  });

  it("anchors a marker on the line between its two points", () => {
    const pts = [
      { timestamp: 0, value: 10 },
      { timestamp: 10, value: 20 },
    ];
    expect(markerAnchor(pts, 5)).toEqual({ ts: 5, value: 15 });
    expect(markerAnchor(pts, 10)).toEqual({ ts: 10, value: 20 });
  });

  it("leaves a marker off outside the range or inside a gap", () => {
    const pts = [
      { timestamp: 0, value: 10 },
      { timestamp: 10, value: null },
      { timestamp: 20, value: 30 },
    ];
    expect(markerAnchor(pts, 15)).toBeNull();
    expect(markerAnchor(pts, 99)).toBeNull();
    expect(markerAnchor(pts, -1)).toBeNull();
  });
});

describe("result toggle", () => {
  it("is enabled when there is a result series", () => {
    expect(resultToggle(history(), false).enabled).toBe(true);
  });

  it("carries the not-netted caveat in its hint", () => {
    const hint = resultToggle(history({ result_note: "Hummingbot deposits are not netted." }), false).hint;
    expect(hint).toContain("Hummingbot deposits are not netted.");
  });

  it("is disabled with the reason when the result cannot be told honestly", () => {
    const t = resultToggle(history({ result_points: null, result_note: "Deposits of Revolut X are not tracked." }), false);
    expect(t.enabled).toBe(false);
    expect(t.hint).toContain("Deposits of Revolut X are not tracked.");
  });

  it("is disabled in the stacked view", () => {
    const t = resultToggle(history(), true);
    expect(t.enabled).toBe(false);
    expect(t.hint).toMatch(/stacked/);
  });
});
