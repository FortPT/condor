import { useQuery } from "@tanstack/react-query";

import { useServer } from "@/hooks/useServer";
import { apiFetch } from "@/lib/api";
import { money, num } from "./tradeFormat";

/** The agent and loop that trade the Gateway's Solana wallet. */
export const SOLANA_AGENT = "solana_dex_trader";
export const SOLANA_LOOP = "swing";
export const SOLANA_CONNECTOR = "solana-mainnet-beta";

type Balance = { token: string; total: number; usd_value?: number | null };
type Portfolio = { connectors?: { connector: string; balances?: Balance[] }[] };
type Strategy = {
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

const card = "rounded-lg border border-[var(--color-border)] p-4";
const muted = "text-[var(--color-text-muted)]";

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
const n = (value: unknown) => num(value as number | string | null | undefined);

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

const when = (value?: string | number | null) => {
  if (value === null || value === undefined || value === "") return "—";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
};

const statusText = (status?: string | null) => {
  const upper = String(status ?? "").toUpperCase();
  return upper === "CONFIRMED" ? "Confirmed" : upper === "SUBMITTED" ? "Pending" : upper ? "Failed" : "—";
};

/**
 * A read-only look at the Solana wallet the Gateway signs with and the loop that trades it:
 * what the wallet holds, whether the loop is running, the limits the code enforces before any
 * swap signs, and the latest swaps. No buttons: swaps are made by the loop, and the loop is
 * started and stopped from the Agents page.
 *
 * Cadence: the loop every 15 s, swaps every 30 s and the wallet every 60 s (the portfolio read
 * touches every connector, so it is not polled faster).
 */
export function SolanaTrade() {
  const { server } = useServer();
  const strategy = useQuery({
    queryKey: ["solana-trade", "strategy"],
    queryFn: () => apiFetch<Strategy>(`/api/v1/agents/${SOLANA_AGENT}/strategies/${SOLANA_LOOP}`),
    refetchInterval: 15000,
    retry: false,
  });
  const portfolio = useQuery({
    queryKey: ["solana-trade", "portfolio", server],
    enabled: !!server,
    queryFn: () => apiFetch<Portfolio>(`/api/v1/servers/${encodeURIComponent(server ?? "")}/portfolio`),
    refetchInterval: 60000,
    retry: false,
  });
  const swaps = useQuery({
    queryKey: ["solana-trade", "swaps", server],
    enabled: !!server,
    queryFn: () => apiFetch<{ swaps?: Swap[] }>(`/api/v1/servers/${encodeURIComponent(server ?? "")}/dex/swaps?limit=25`),
    refetchInterval: 30000,
    retry: false,
  });

  const wallet = walletRows(portfolio.data);
  const loop = strategy.data;
  const running = loop?.status === "running";
  const instance = loop?.instances?.[0];
  const frequency = n(loop?.config?.frequency_sec);
  const rows = swaps.data?.swaps ?? [];
  const error = strategy.error || portfolio.error || swaps.error;

  return <div className="space-y-4" data-solana>
    <div className="flex flex-wrap items-center gap-3">
      <h1 className="text-xl font-semibold">Solana</h1>
      <span className="text-sm">{strategy.isLoading ? "Checking the loop…" : loop ? `Loop: ${running ? "running" : loop.status ?? "stopped"}` : "Loop not found"}</span>
      {running && instance && <span className="text-sm">Session {instance.session_num ?? "—"} · {instance.tick_count ?? 0} ticks{frequency ? ` · every ${Math.round(frequency / 60)} min` : ""}</span>}
      {loop?.unavailable === "no_server" && <span className="text-sm text-[var(--color-red)]">The loop has no server pinned, so it cannot swap.</span>}
      <span className={`ml-auto text-sm ${muted}`}>Read only: the loop trades, and it is started and stopped on the Agents page.</span>
    </div>
    {error && <p role="alert" className="text-sm text-[var(--color-red)]">{error.message}</p>}
    {!server && <p className="text-sm">Select a server to see the Solana wallet.</p>}

    <section className={card} aria-label="Wallet">
      <h2 className="mb-2 text-sm font-semibold">Wallet</h2>
      {portfolio.isLoading ? <p className={`text-sm ${muted}`}>Reading the wallet…</p> :
        wallet.rows.length === 0 ? <p className={`text-sm ${muted}`}>The Solana wallet holds nothing, or the Gateway has no wallet yet.</p> : <>
          <p className="mb-2 text-sm">Value {money(wallet.total, "USD")}</p>
          <table className="w-full text-sm">
            <thead><tr className={`text-left ${muted}`}><th>Token</th><th>Amount</th><th>Value (USD)</th><th>Share</th></tr></thead>
            <tbody>{wallet.rows.map((r) => <tr key={r.token} data-token={r.token}>
              <td>{r.token}</td><td>{r.amount.toLocaleString(undefined, { maximumFractionDigits: 6 })}</td><td>{money(r.usd, "USD")}</td><td>{r.share.toFixed(1)}%</td>
            </tr>)}</tbody>
          </table>
        </>}
    </section>

    <section className={card} aria-label="Limits">
      <h2 className="mb-2 text-sm font-semibold">Limits enforced in code before any swap signs</h2>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
        {limitRows(loop?.config).map(([label, value]) => <div key={label} className="flex justify-between gap-3">
          <dt className={muted}>{label}</dt><dd>{value}</dd>
        </div>)}
      </dl>
    </section>

    <section className={card} aria-label="Swaps">
      <h2 className="mb-2 text-sm font-semibold">Latest swaps</h2>
      {swaps.isLoading ? <p className={`text-sm ${muted}`}>Reading swaps…</p> :
        rows.length === 0 ? <p className={`text-sm ${muted}`} data-state="no-swaps">No swaps yet.</p> :
          <table className="w-full text-sm">
            <thead><tr className={`text-left ${muted}`}><th>Time</th><th>Pair</th><th>Side</th><th>In</th><th>Out</th><th>Price</th><th>Status</th><th>Transaction</th></tr></thead>
            <tbody>{rows.map((s, i) => <tr key={s.transaction_hash ?? i}>
              <td>{when(s.timestamp)}</td><td>{s.trading_pair ?? "—"}</td><td>{String(s.side ?? "—").toUpperCase()}</td>
              <td>{num(s.input_amount) ?? "—"}</td><td>{num(s.output_amount) ?? "—"}</td><td>{num(s.price) ?? "—"}</td>
              <td>{statusText(s.status)}</td>
              <td>{s.transaction_hash ? <a className="underline" href={`https://solscan.io/tx/${encodeURIComponent(s.transaction_hash)}`} target="_blank" rel="noreferrer">{s.transaction_hash.slice(0, 8)}…</a> : "—"}</td>
            </tr>)}</tbody>
          </table>}
    </section>
  </div>;
}
