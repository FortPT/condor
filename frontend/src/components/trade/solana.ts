/** Pure helpers and shapes for the read-only Solana Trade tab. */
import { money, num } from "./tradeFormat";

/** The agent and loop that trade the Gateway's Solana wallet. */
export const SOLANA_AGENT = "solana_dex_trader";
export const SOLANA_LOOP = "swing";
export const SOLANA_CONNECTOR = "solana-mainnet-beta";

export type Balance = { token: string; total: number; usd_value?: number | null };
export type Portfolio = { connectors?: { connector: string; balances?: Balance[] }[] };
export type Strategy = {
  status?: string;
  unavailable?: string;
  config?: Record<string, unknown>;
  instances?: { session_num?: number; tick_count?: number }[];
};
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

