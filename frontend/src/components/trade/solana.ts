/** Pure helpers and shapes for the read-only Solana Trade tab. */
import { money, num } from "./tradeFormat";

/** The agent and loop that trade the Gateway's Solana wallet. */
export const SOLANA_AGENT = "solana_dex_trader";
export const SOLANA_LOOP = "swing";
export const SOLANA_CONNECTOR = "solana-mainnet-beta";
/**
 * Token mints for the chart: the DEX candle feed finds a pool by the base token's address, not its
 * ticker. Each was checked against the live feed (the last candle's close matched Jupiter's quote).
 */
export const TOKEN_MINTS: Record<string, string> = {
  SOL: "So11111111111111111111111111111111111111112",
  JUP: "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
  PYTH: "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
  RAY: "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
  BONK: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
};

/** The tokens the chart can show: SOL first, then the loop's allowed tokens that have a known mint. */
export function chartTokens(allowed: string[]): string[] {
  return ["SOL", ...allowed.filter((token) => token !== "SOL" && token in TOKEN_MINTS)];
}

export type Balance = { token: string; total: number; usd_value?: number | null };
export type Portfolio = { connectors?: { connector: string; balances?: Balance[] }[] };
export type Strategy = {
  status?: string;
  unavailable?: string;
  config?: Record<string, unknown>;
  instances?: { session_num?: number; tick_count?: number }[];
};
/** `GET /servers/{name}/solana/overview`: the wallet's value, its deposits found on chain, and the result net of them. */
export type Overview = {
  address?: string | null;
  value_usd?: number;
  deposits_usd?: number;
  since_start_usd?: number;
  today_usd?: number;
  deposits?: { signature: string; ts: number; usd: number }[];
};
/** `GET /servers/{name}/dex/quotes`: Jupiter's price for one unit of a token in USDC. */
export type Quote = { token: string; price: number | null; price_impact_pct: number | null; error: string | null };
export type Swap = {
  status?: string | null;
  side?: string | null;
  trading_pair?: string | null;
  input_amount?: number | string | null;
  output_amount?: number | string | null;
  price?: number | string | null;
  transaction_hash?: string | null;
  timestamp?: string | number | null;
};

/** The wallet's rows: Solana-connector balances worth something, largest first, with their share. */
export function walletRows(portfolio?: Portfolio) {
  const balances = portfolio?.connectors?.find((c) => c.connector === SOLANA_CONNECTOR)?.balances ?? [];
  const rows = balances
    .map((b) => ({ token: b.token, amount: num(b.total) ?? 0, usd: num(b.usd_value) ?? 0 }))
    .filter((r) => r.amount > 0)
    .sort((a, b) => b.usd - a.usd);
  const total = rows.reduce((sum, r) => sum + r.usd, 0);
  return { rows: rows.map((r) => ({ ...r, share: total > 0 ? (r.usd / total) * 100 : 0 })), total };
}

/** A config value as a number, or null when it is missing or not numeric. */
export const n = (value: unknown) => num(value as number | string | null | undefined);

/** The limits the loop runs under, as label/value pairs, read from its saved config. */
export function limitRows(config?: Record<string, unknown>): [string, string][] {
  const limits = (config?.risk_limits ?? {}) as Record<string, unknown>;
  const tokens = limits.allowed_swap_tokens;
  const list = Array.isArray(tokens) ? tokens.join(", ") : typeof tokens === "string" ? tokens.split(",").map((t) => t.trim()).filter(Boolean).join(", ") : "";
  const usdc = (value: unknown) => (n(value) === null ? "—" : money(n(value), "USDC"));
  const slippage = n(limits.max_swap_slippage_pct);
  const reserve = n(limits.min_swap_sol_reserve);
  return [
    ["Allowed tokens", list || "—"],
    ["Largest swap", usdc(limits.max_swap_quote)],
    ["Largest total per tick", usdc(limits.max_position_size_quote)],
    ["Slippage limit", slippage === null ? "—" : `${slippage}%`],
    ["SOL always kept for fees", reserve === null ? "—" : `${reserve} SOL`],
    ["Signs with", limits.swap_default_wallet_only ? "the default wallet only" : "any wallet"],
  ];
}


/** The tokens the loop may swap into, from its saved limits: upper case, once each, USDC left out (it is cash). */
export function allowedTokens(config?: Record<string, unknown>): string[] {
  const limits = (config?.risk_limits ?? {}) as Record<string, unknown>;
  const raw = limits.allowed_swap_tokens;
  const list = Array.isArray(raw) ? raw.map(String) : typeof raw === "string" ? raw.split(",") : [];
  return [...new Set(list.map((t) => t.trim().toUpperCase()).filter((t) => t && t !== "USDC"))];
}

/** The agent's decisions from a session journal's `## Decisions` section, newest first. */
export function parseDecisions(content?: string, limit = 5): { tick: string; time: string; text: string }[] {
  const rows: { tick: string; time: string; text: string }[] = [];
  let inSection = false;
  for (const raw of (content ?? "").split("\n")) {
    const line = raw.trim();
    if (line.startsWith("## ")) {
      inSection = line === "## Decisions";
      continue;
    }
    const hit = inSection ? /^- \*\*#(\d+)\*\* \((\d{2}:\d{2})\) (.+)$/.exec(line) : null;
    if (hit) rows.push({ tick: hit[1], time: hit[2], text: hit[3] });
  }
  return rows.reverse().slice(0, limit);
}

/** A USD amount with its sign, or a dash when there is none. */
export function signedUsd(value?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const cents = Math.abs(value).toFixed(2);
  return value >= 0.005 ? `+$${cents}` : value <= -0.005 ? `−$${cents}` : `$${cents}`;
}
