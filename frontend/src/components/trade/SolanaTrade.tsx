import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { useServer } from "@/hooks/useServer";
import { apiFetch, type CandleData } from "@/lib/api";
import {
  SOLANA_AGENT, SOLANA_CONNECTOR, SOLANA_LOOP, TOKEN_MINTS, allowedTokens, chartTokens, limitRows, n, parseDecisions, signedUsd, walletRows,
  type Overview, type Portfolio, type Quote, type Strategy, type Swap,
} from "./solana";
import { CandleChart } from "./TradeParts";
import { money, num } from "./tradeFormat";

const card = "rounded-lg border border-[var(--color-border)] p-4";
const muted = "text-[var(--color-text-muted)]";
const INTERVALS = ["15m", "1h", "4h", "1D"] as const;
const button = "rounded border border-[var(--color-border)] px-3 py-1 text-sm aria-pressed:bg-[var(--color-surface-hover)]";

const when = (value?: string | number | null) => {
  if (value === null || value === undefined || value === "") return "—";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
};

const statusText = (status?: string | null) => {
  const upper = String(status ?? "").toUpperCase();
  return upper === "CONFIRMED" ? "Confirmed" : upper === "SUBMITTED" ? "Pending" : upper ? "Failed" : "—";
};

const tone = (value?: number | null) =>
  value === undefined || value === null || Math.abs(value) < 0.005 ? "" : value > 0 ? "text-[var(--color-green)]" : "text-[var(--color-red)]";

const quotePrice = (price: number) => price.toLocaleString(undefined, { maximumFractionDigits: price >= 100 ? 2 : price >= 1 ? 3 : price >= 0.01 ? 4 : 8 });

function Metric({ label, value, note, className = "" }: { label: string; value: string; note?: string; className?: string }) {
  return <div className={card} data-metric={label}>
    <div className={`text-xs ${muted}`}>{label}</div>
    <div className={`text-xl font-semibold ${className}`}>{value}</div>
    {note && <div className={`text-xs ${muted}`}>{note}</div>}
  </div>;
}

/**
 * A read-only look at the Solana wallet the Gateway signs with and the loop that trades it: the wallet's
 * value, deposits and result net of them, a SOL price chart, Jupiter prices for the tokens the loop may use,
 * what the wallet holds, the agent's latest decisions in its own words, the limits the code enforces before
 * any swap signs, the latest swaps and the deposits found on chain. No buttons: swaps are made by the loop,
 * and the loop is started and stopped from the Agents page.
 *
 * Cadence: the loop every 15 s, swaps and decisions every 30 s, the wallet, prices and result every 60 s.
 * The chart comes from a public price source that rate-limits, so it is read when opened and when the
 * token or interval changes, not polled.
 */
export function SolanaTrade() {
  const { server } = useServer();
  const [interval, setInterval] = useState<(typeof INTERVALS)[number]>("1h");
  const [chosen, setChosen] = useState("SOL");
  const enc = encodeURIComponent(server ?? "");

  const strategy = useQuery({
    queryKey: ["solana-trade", "strategy"],
    queryFn: () => apiFetch<Strategy>(`/api/v1/agents/${SOLANA_AGENT}/strategies/${SOLANA_LOOP}`),
    refetchInterval: 15000,
    retry: false,
  });
  const portfolio = useQuery({
    queryKey: ["solana-trade", "portfolio", server],
    enabled: !!server,
    queryFn: () => apiFetch<Portfolio>(`/api/v1/servers/${enc}/portfolio`),
    refetchInterval: 60000,
    retry: false,
  });
  const overview = useQuery({
    queryKey: ["solana-trade", "overview", server],
    enabled: !!server,
    queryFn: () => apiFetch<Overview>(`/api/v1/servers/${enc}/solana/overview`),
    refetchInterval: 60000,
    retry: false,
  });
  const swaps = useQuery({
    queryKey: ["solana-trade", "swaps", server],
    enabled: !!server,
    queryFn: () => apiFetch<{ swaps?: Swap[] }>(`/api/v1/servers/${enc}/dex/swaps?limit=25`),
    refetchInterval: 30000,
    retry: false,
  });
  const loop = strategy.data;
  const tokens = allowedTokens(loop?.config);
  const quoteList = ["SOL", ...tokens.filter((t) => t !== "SOL")].join(",");
  const chartList = chartTokens(tokens);
  const chartToken = chartList.includes(chosen) ? chosen : "SOL";
  const quotes = useQuery({
    queryKey: ["solana-trade", "quotes", server, quoteList],
    enabled: !!server && tokens.length > 0,
    queryFn: () => apiFetch<{ quotes?: Quote[] }>(`/api/v1/servers/${enc}/dex/quotes?tokens=${encodeURIComponent(quoteList)}`),
    refetchInterval: 60000,
    retry: false,
  });
  const session = loop?.instances?.[0]?.session_num;
  const journal = useQuery({
    queryKey: ["solana-trade", "journal", session],
    enabled: session !== undefined,
    queryFn: () => apiFetch<{ content?: string }>(`/api/v1/agents/${SOLANA_AGENT}/strategies/${SOLANA_LOOP}/sessions/${session}/journal`),
    refetchInterval: 30000,
    retry: false,
  });
  const candles = useQuery({
    queryKey: ["solana-trade", "candles", server, chartToken, interval],
    enabled: !!server,
    queryFn: () => apiFetch<CandleData[]>(
      `/api/v1/servers/${enc}/market/candles?connector=${SOLANA_CONNECTOR}&trading_pair=${TOKEN_MINTS[chartToken]}-USDC&interval=${interval}&limit=200`),
    staleTime: 60000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const wallet = walletRows(portfolio.data);
  const running = loop?.status === "running";
  const instance = loop?.instances?.[0];
  const frequency = n(loop?.config?.frequency_sec);
  const rows = swaps.data?.swaps ?? [];
  const decisions = parseDecisions(journal.data?.content);
  const result = overview.data;
  const deposits = result?.deposits ?? [];
  const percent = result?.deposits_usd && result.deposits_usd > 0 && result.since_start_usd !== undefined
    ? `${result.since_start_usd >= 0 ? "+" : "−"}${Math.abs((result.since_start_usd / result.deposits_usd) * 100).toFixed(2)}% of deposits` : undefined;
  const priceOf = (token: string) => quotes.data?.quotes?.find((q) => q.token === token)?.price ?? null;
  const error = strategy.error || portfolio.error || swaps.error || overview.error || quotes.error;
  const chartRows = Array.isArray(candles.data) ? candles.data : [];

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

    <section className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-label="Summary">
      <Metric label="Wallet value" value={result?.value_usd === undefined ? money(wallet.total, "USD") : money(result.value_usd, "USD")} note={result?.address ? `${result.address.slice(0, 4)}…${result.address.slice(-4)}` : undefined} />
      <Metric label="Deposited" value={result?.deposits_usd === undefined ? "—" : money(result.deposits_usd, "USD")} note="net, found on chain" />
      <Metric label="Result since start" value={signedUsd(result?.since_start_usd)} note={percent ?? "net of deposits"} className={tone(result?.since_start_usd)} />
      <Metric label="Today" value={signedUsd(result?.today_usd)} note="since the first read of the UTC day" className={tone(result?.today_usd)} />
    </section>

    <section className={card} aria-label="Price chart">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{chartToken} / USDC</h2>
        <span className={`text-xs ${muted}`}>Public DEX price feed</span>
        <div className="flex gap-2" role="group" aria-label="Chart token">
          {chartList.map((t) => <button key={t} className={button} aria-pressed={chartToken === t} onClick={() => setChosen(t)}>{t}</button>)}
        </div>
        <div className="ml-auto flex gap-2" role="group" aria-label="Chart interval">
          {INTERVALS.map((i) => <button key={i} className={button} aria-pressed={interval === i} onClick={() => setInterval(i)}>{i}</button>)}
        </div>
      </div>
      <div className="h-72">
        {candles.isLoading ? <p role="status" className={`p-4 text-sm ${muted}`}>Loading chart…</p> :
          chartRows.length === 0 ? <p className={`p-4 text-sm ${muted}`} data-state="no-chart">The chart is unavailable right now: the public price feed rate-limits. It retries when you change the token or interval.</p> :
            <CandleChart rows={chartRows} />}
      </div>
    </section>

    <section className={card} aria-label="Markets">
      <h2 className="mb-2 text-sm font-semibold">Prices the loop can trade (Jupiter, in USDC)</h2>
      {tokens.length === 0 ? <p className={`text-sm ${muted}`}>No allowed tokens are set.</p> : quotes.isLoading ? <p className={`text-sm ${muted}`}>Reading prices…</p> :
        <table className="w-full text-sm">
          <thead><tr className={`text-left ${muted}`}><th>Token</th><th>Price</th><th>Price impact</th></tr></thead>
          <tbody>{(quotes.data?.quotes ?? []).filter((q) => q.token !== "USDC").map((q) => <tr key={q.token} data-quote={q.token}>
            <td>{q.token}</td>
            <td>{q.price === null ? <span className={muted}>{q.error ?? "—"}</span> : `$${quotePrice(q.price)}`}</td>
            <td>{q.price_impact_pct === null ? "—" : `${q.price_impact_pct.toFixed(2)}%`}</td>
          </tr>)}</tbody>
        </table>}
    </section>

    <section className={card} aria-label="Wallet">
      <h2 className="mb-2 text-sm font-semibold">Wallet</h2>
      {portfolio.isLoading ? <p className={`text-sm ${muted}`}>Reading the wallet…</p> :
        wallet.rows.length === 0 ? <p className={`text-sm ${muted}`}>The Solana wallet holds nothing, or the Gateway has no wallet yet.</p> :
          <table className="w-full text-sm">
            <thead><tr className={`text-left ${muted}`}><th>Token</th><th>Amount</th><th>Price</th><th>Value (USD)</th><th>Share</th></tr></thead>
            <tbody>{wallet.rows.map((r) => {
              const quoted = priceOf(r.token);
              return <tr key={r.token} data-token={r.token}>
                <td>{r.token}</td><td>{r.amount.toLocaleString(undefined, { maximumFractionDigits: 6 })}</td>
                <td>${quotePrice(quoted ?? r.usd / r.amount)}</td>
                <td>{money(r.usd, "USD")}</td><td>{r.share.toFixed(1)}%</td>
              </tr>;
            })}</tbody>
          </table>}
    </section>

    <section className={card} aria-label="Agent decisions">
      <h2 className="mb-2 text-sm font-semibold">What the agent decided{session !== undefined ? ` (session ${session})` : ""}</h2>
      {session === undefined ? <p className={`text-sm ${muted}`}>The loop is not running, so there is no current session.</p> :
        decisions.length === 0 ? <p className={`text-sm ${muted}`} data-state="no-decisions">No decisions yet: the first tick has not finished.</p> :
          <ul className="space-y-2 text-sm">{decisions.map((d) => <li key={d.tick} data-decision={d.tick}>
            <span className={muted}>Tick {d.tick} · {d.time} UTC</span><div>{d.text}</div>
          </li>)}</ul>}
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

    <section className={card} aria-label="Deposits">
      <h2 className="mb-2 text-sm font-semibold">Deposits and withdrawals</h2>
      <p className={`mb-2 text-xs ${muted}`}>Found on the Solana network: a transfer into the wallet that the wallet did not sign, valued once at the SOL price when first seen. Records only; nothing moves from here.</p>
      {overview.isLoading ? <p className={`text-sm ${muted}`}>Reading the chain…</p> :
        deposits.length === 0 ? <p className={`text-sm ${muted}`} data-state="no-deposits">No deposits found yet.</p> :
          <table className="w-full text-sm">
            <thead><tr className={`text-left ${muted}`}><th>Time</th><th>Value (USD)</th><th>Transaction</th></tr></thead>
            <tbody>{deposits.map((d) => <tr key={d.signature} data-deposit={d.signature}>
              <td>{when(d.ts)}</td><td className={tone(d.usd)}>{signedUsd(d.usd)}</td>
              <td><a className="underline" href={`https://solscan.io/tx/${encodeURIComponent(d.signature)}`} target="_blank" rel="noreferrer">{d.signature.slice(0, 8)}…</a></td>
            </tr>)}</tbody>
          </table>}
    </section>
  </div>;
}
