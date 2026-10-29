/**
 * The Loops panel's own cards (FEAT-125): what each one says, how they group,
 * and where a click on one goes.
 *
 * The panel is a pure reader of the props `useLiveLoops` already filtered and
 * sorted — its own hook has that test, plus `groupLoopsByAgent`/`loopStats`'s.
 * What is pinned here is the empty state, the grouping (a heading only with
 * 2+ agents), a card's populated facts, the roster-join-miss degrade, a
 * failed last action, an overdue tick, and that a click hands back exactly
 * the card's own slugs.
 *
 * Needs a DOM, so this file overrides vitest's default `node` environment.
 *
 * @vitest-environment jsdom
 */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveFleetOwner } from "@/hooks/useLiveLoops";
import type { AgentSummary } from "@/lib/api";
import type { LiveLoop } from "@/lib/agent-attribution";

import { LoopsPanel } from "./LoopsPanel";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

const NOW_SEC = Math.floor(Date.now() / 1000);

function live(over: Partial<LiveLoop> = {}): LiveLoop {
  return {
    agentId: "a1",
    sessionNum: 4,
    status: "running",
    tickCount: 12,
    lastTickAt: NOW_SEC - 10,
    lastReadingAt: 0,
    lastSuccessfulActionAt: 0,
    lastTickDuration: 0,
    frequencySec: 30,
    lastAction: "",
    lastDid: null,
    lastError: "",
    ...over,
  };
}

function owner(over: Partial<LiveFleetOwner> = {}): LiveFleetOwner {
  return {
    runKey: "brigado.brl_mm",
    agentSlug: "brigado",
    agentName: "Brigado",
    strategySlug: "brl_mm",
    strategyName: "BRL MM",
    namespace: "brigado-brl_mm",
    declaredBots: [],
    agentIds: [],
    live: live(),
    ...over,
  };
}

function agentSummary(over: Partial<AgentSummary> = {}): AgentSummary {
  return {
    slug: "brigado",
    name: "Brigado",
    description: "",
    when_to_consult: "",
    agent_key: "",
    strategy_count: 1,
    strategies: [
      {
        slug: "brl_mm",
        name: "BRL MM",
        description: "",
        status: "running",
        agent_id: "brigado.brl_mm",
        session_count: 5,
        experiment_count: 2,
        tick_count: 12,
        latest_session_pnl: 42.5,
        total_pnl: 100,
        total_volume: 1000,
        open_positions: 0,
        instances: [],
      },
    ],
    status: "running",
    session_count: 5,
    experiment_count: 2,
    tick_count: 12,
    latest_session_pnl: 42.5,
    total_pnl: 100,
    total_volume: 1000,
    open_positions: 0,
    instances: [],
    ...over,
  };
}

let container: HTMLDivElement;
let root: Root;

async function render(props: Partial<Parameters<typeof LoopsPanel>[0]> = {}) {
  await act(async () => {
    root.render(
      <LoopsPanel
        loops={[]}
        isLoading={false}
        agents={[]}
        onOpenLoop={() => {}}
        onClose={() => {}}
        {...props}
      />,
    );
  });
}

const panel = () =>
  container.querySelector<HTMLElement>('[data-testid="loops-panel"]')!;
const cards = () =>
  [...container.querySelectorAll<HTMLButtonElement>("[data-loop-row]")];
const groupHeadings = () =>
  [...container.querySelectorAll<HTMLElement>("[data-loop-group]")];

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("LoopsPanel", () => {
  it("says nothing is looping with an empty list", async () => {
    await render({ loops: [] });
    expect(panel().textContent).toContain("Nothing looping right now");
  });

  it("says it is loading rather than claiming nothing is looping", async () => {
    await render({ loops: [], isLoading: true });
    expect(panel().textContent).not.toContain("Nothing looping right now");
    expect(panel().textContent).toContain("Loading");
  });

  it("renders one card per loop, under no heading, for a single agent", async () => {
    await render({
      loops: [
        owner({ strategySlug: "brl_mm", strategyName: "BRL MM" }),
        owner({
          strategySlug: "grid",
          strategyName: "Grid",
          live: live({ status: "paused", sessionNum: 2, tickCount: 5 }),
        }),
      ],
    });

    expect(cards()).toHaveLength(2);
    expect(groupHeadings()).toHaveLength(0);
    expect(cards()[0].textContent).toContain("Brigado");
    expect(cards()[0].textContent).toContain("BRL MM");
  });

  it("groups by agent, with a heading per agent, when 2+ agents are live", async () => {
    await render({
      loops: [
        owner({ agentSlug: "brigado", agentName: "Brigado" }),
        owner({
          agentSlug: "vega",
          agentName: "Vega",
          strategySlug: "momentum",
          strategyName: "Momentum",
          live: live({ status: "paused" }),
        }),
      ],
    });

    expect(groupHeadings()).toHaveLength(2);
    expect(groupHeadings()[0].textContent).toContain("Brigado");
    expect(groupHeadings()[1].textContent).toContain("Vega");
    expect(cards()).toHaveLength(2);
  });

  it("shows the tick facts, and the PnL badge, when the roster join hits", async () => {
    await render({
      loops: [owner()],
      agents: [agentSummary()],
    });

    const card = cards()[0];
    expect(card.textContent).toContain("+$42.50");
    expect(card.textContent).toContain("12");
    expect(card.textContent).toContain("5");
    expect(card.textContent).toContain("2 dry");
  });

  // QA on PR 244: with the API down the card read `$0.00`, an outage printed
  // as a loop that traded nothing.
  it("draws a dash, not $0.00, when the server could not price the loop", async () => {
    const base = agentSummary();
    await render({
      loops: [owner()],
      agents: [
        {
          ...base,
          strategies: [
            { ...base.strategies[0], latest_session_pnl: 0, unavailable: "unreachable" },
          ],
        },
      ],
    });

    const pnl = cards()[0].querySelector<HTMLElement>("[data-loop-pnl]")!;
    expect(pnl.textContent).toBe("—");
    expect(pnl.title).toBe("Server unreachable");
    expect(cards()[0].textContent).not.toContain("$0.00");
  });

  // A native crew (Crypto.com, Kraken xStocks) trades outside
  // Hummingbot: its figure is the account's equity change, computed from the
  // venue's own history, so the card must print it like any other PnL.
  describe("a native crew", () => {
    function nativeAgent(over: Record<string, unknown>) {
      const base = agentSummary({
        slug: "crypto_com_exchange_trader",
        name: "Crypto.com Trader",
      });
      return {
        ...base,
        strategies: [
          {
            ...base.strategies[0],
            slug: "daily_spot",
            pnl_basis: "account_equity",
            ...over,
          },
        ],
      };
    }
    const nativeLoop = () =>
      owner({
        agentSlug: "crypto_com_exchange_trader",
        agentName: "Crypto.com Trader",
        strategySlug: "daily_spot",
        strategyName: "Daily spot",
      });

    it("shows its equity change as a real number, with what it measures on hover", async () => {
      await render({
        loops: [nativeLoop()],
        agents: [nativeAgent({ latest_session_pnl: 1.234 })],
      });

      const pnl = cards()[0].querySelector<HTMLElement>("[data-loop-pnl]")!;
      expect(pnl.textContent).toBe("+$1.23");
      expect(pnl.title).toBe(
        "Change in account equity since this session started (native crew)",
      );
    });

    it("shows a loss as a negative figure and says so on hover when it is an estimate", async () => {
      await render({
        loops: [nativeLoop()],
        agents: [
          nativeAgent({
            latest_session_pnl: -0.5,
            pnl_estimated: true,
            pnl_note: "Session start estimated from the equity sample 2.0 h after it.",
          }),
        ],
      });

      const pnl = cards()[0].querySelector<HTMLElement>("[data-loop-pnl]")!;
      expect(pnl.textContent).toBe("-$0.50");
      expect(pnl.title).toContain("native crew");
      expect(pnl.title).toContain("estimated from the equity sample 2.0 h after it");
    });

    it("calls the figure a trading result once the venue's deposits and withdrawals are tracked", async () => {
      await render({
        loops: [nativeLoop()],
        agents: [nativeAgent({ latest_session_pnl: 0.47, pnl_flows_tracked: true })],
      });

      const pnl = cards()[0].querySelector<HTMLElement>("[data-loop-pnl]")!;
      expect(pnl.textContent).toBe("+$0.47");
      expect(pnl.title).toBe(
        "Trading result since this session started: change in account equity net of recorded deposits and withdrawals (native crew)",
      );
      expect(cards()[0].querySelector("[data-loop-unconfirmed]")).toBeNull();
    });

    it("shows an unconfirmed cash movement as a visible line, not only on hover", async () => {
      await render({
        loops: [nativeLoop()],
        agents: [
          nativeAgent({
            latest_session_pnl: 4.74,
            pnl_unconfirmed_count: 1,
            pnl_unconfirmed_usd: 4.74,
          }),
        ],
      });

      const line = cards()[0].querySelector<HTMLElement>("[data-loop-unconfirmed]")!;
      expect(line.textContent).toBe("Unconfirmed cash movement of +$4.74");
      const pnl = cards()[0].querySelector<HTMLElement>("[data-loop-pnl]")!;
      expect(pnl.title).toContain("is not subtracted until you confirm it");
    });

    it("shows no unconfirmed line on an executor loop", async () => {
      await render({ loops: [owner()], agents: [agentSummary()] });
      expect(cards()[0].querySelector("[data-loop-unconfirmed]")).toBeNull();
    });

    it("says why there is no figure instead of $0.00 when no equity is recorded yet", async () => {
      await render({
        loops: [nativeLoop()],
        agents: [nativeAgent({ latest_session_pnl: 0, unavailable: "no_history" })],
      });

      const pnl = cards()[0].querySelector<HTMLElement>("[data-loop-pnl]")!;
      expect(pnl.textContent).toBe("—");
      expect(pnl.title).toBe("No equity history yet");
      expect(cards()[0].textContent).not.toContain("$0.00");
    });

    it("an executor loop's figure carries no equity-change hover", async () => {
      await render({ loops: [owner()], agents: [agentSummary()] });

      const pnl = cards()[0].querySelector<HTMLElement>("[data-loop-pnl]")!;
      expect(pnl.textContent).toBe("+$42.50");
      expect(pnl.title).toBe("");
    });
  });

  it("degrades — no PnL, a session label instead of a run count — when the join misses", async () => {
    await render({
      loops: [owner({ live: live({ sessionNum: 7 }) })],
      agents: [],
    });

    const card = cards()[0];
    expect(card.querySelector("[data-loop-pnl]")).toBeNull();
    expect(card.textContent).toContain("Session 7");
  });

  it("does not throw when a loop's strategy is missing from an agent that does exist", async () => {
    await render({
      loops: [owner({ agentSlug: "brigado", strategySlug: "unknown_strategy" })],
      agents: [agentSummary()],
    });

    expect(cards()).toHaveLength(1);
    expect(cards()[0].querySelector("[data-loop-pnl]")).toBeNull();
  });

  it("shows a failed last action amber, with a failed suffix", async () => {
    await render({
      loops: [
        owner({
          live: live({
            lastDid: {
              tick: 9,
              at: NOW_SEC,
              tool: "create_lp_executor",
              verb: "create_lp_executor",
              summary: "Create grid executor on SOL-USDC",
              ok: false,
              error: "insufficient balance",
            },
          }),
        }),
      ],
    });

    const deed = cards()[0].querySelector("span[title='Create grid executor on SOL-USDC']")!;
    expect(deed.textContent).toContain("failed");
    expect(deed.className).toContain("text-amber-500");
  });

  it("shows an amber, overdue next-tick tile past its cadence", async () => {
    await render({
      loops: [owner({ live: live({ lastTickAt: NOW_SEC - 90, frequencySec: 30 }) })],
    });

    const overdue = cards()[0].querySelector("span.text-amber-400")!;
    expect(overdue).not.toBeNull();
    expect(overdue.textContent).toMatch(/overdue/);
  });

  it("calls onOpenLoop with the card's own slugs, exactly once", async () => {
    const onOpenLoop = vi.fn();
    await render({
      loops: [
        owner({ agentSlug: "brigado", strategySlug: "brl_mm" }),
        owner({ agentSlug: "vega", strategySlug: "momentum" }),
      ],
      onOpenLoop,
    });

    await act(async () => {
      cards()[1].dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onOpenLoop).toHaveBeenCalledTimes(1);
    expect(onOpenLoop).toHaveBeenCalledWith("vega", "momentum");
  });
});
