import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { AppResult } from "@/components/settings/CryptoComApp";
import { limitsSummary } from "@/components/settings/cryptoComLimits";
import { useCryptoComStatus } from "@/hooks/useCredentials";
import { useServer } from "@/hooks/useServer";
import { api, apiFetch, type CandleData } from "@/lib/api";
import { getThemeColors } from "@/lib/theme-colors";

type Money = { amount?: string; currency?: string };
export type AppCoin = {
  rank?: number;
  symbol: string;
  name?: string;
  is_tradable?: boolean;
  price_native?: Money;
  percent_change_24h_native?: string;
  percent_change_1w_native?: string;
  percent_change_30d_native?: string;
  volume_24h_native?: Money;
};
type CoinList = { coins: AppCoin[] };
type Quote = {
  id: string;
  countdown: number;
  from_amount: { amount: string; currency: string };
  to_amount: { amount: string; currency: string };
  fee?: Money;
};
type Side = "buy" | "sell";

const base = "/api/v1/crypto-com";
const INTERVALS = ["15m", "1h", "4h", "1d"] as const;
const INTERVAL_SECONDS = { "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 } as const;
const CANDLES = 200;
// The App has no candle feed; public exchange candles in USDT are the chart.
const CHART_VENUES = ["binance", "okx"] as const;
const input = "w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40 aria-pressed:bg-[var(--color-surface-hover)]";
const card = "rounded-lg border border-[var(--color-border)] p-4";
const DECIMAL = /^\d{1,18}(\.\d{1,18})?$/;

const num = (value?: string | number) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};
const eur = (value: number | null, digits = 2) =>
  value === null ? "—" : `€${value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
const price = (value?: string) => {
  const parsed = num(value);
  if (parsed === null) return "—";
  const digits = parsed >= 100 ? 2 : parsed >= 1 ? 3 : parsed >= 0.01 ? 4 : 8;
  return `€${parsed.toLocaleString(undefined, { maximumFractionDigits: digits })}`;
};
function Change({ value, label }: { value?: string; label?: string }) {
  const parsed = num(value);
  if (parsed === null) return <span className="text-[var(--color-text-muted)]">—</span>;
  return <span className={parsed >= 0 ? "text-[var(--color-green)]" : "text-[var(--color-red)]"}>
    {label}{parsed >= 0 ? "+" : ""}{parsed.toFixed(2)}%
  </span>;
}
/** A decimal string without trailing zeros, floored (never rounded up past what is held). */
function units(value: number, digits = 8) {
  const factor = 10 ** digits;
  return (Math.floor(value * factor) / factor).toFixed(digits).replace(/\.?0+$/, "") || "0";
}

function CandleChart({ rows }: { rows: CandleData[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let disposed = false;
    let cleanup = () => {};
    void import("lightweight-charts").then((mod) => {
      if (disposed || !ref.current) return;
      const colors = getThemeColors();
      const chart = mod.createChart(ref.current, {
        autoSize: true,
        layout: { background: { type: mod.ColorType.Solid, color: colors.bg }, textColor: colors.text },
        grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
        timeScale: { timeVisible: true, secondsVisible: false },
        rightPriceScale: { borderVisible: false },
      });
      const ordered = [...rows].sort((a, b) => a.timestamp - b.timestamp)
        .filter((row, index, all) => index === 0 || row.timestamp !== all[index - 1].timestamp);
      // Cheap coins (SHIB, CRO) need more decimals on the price axis than BTC.
      const last = ordered.at(-1)?.close ?? 1;
      const precision = last >= 100 ? 2 : last >= 1 ? 3 : last >= 0.01 ? 5 : 8;
      const series = chart.addSeries(mod.CandlestickSeries, {
        upColor: colors.up, downColor: colors.down, borderVisible: false,
        wickUpColor: colors.up, wickDownColor: colors.down,
        priceFormat: { type: "price", precision, minMove: 10 ** -precision },
      });
      series.setData(ordered.map((row) => ({
        time: row.timestamp as import("lightweight-charts").UTCTimestamp,
        open: row.open, high: row.high, low: row.low, close: row.close,
      })));
      chart.timeScale().fitContent();
      cleanup = () => chart.remove();
    });
    return () => { disposed = true; cleanup(); };
  }, [rows]);
  return <div ref={ref} data-testid="app-chart" className="h-full w-full" />;
}

export function AppTrade() {
  const { server } = useServer();
  const qc = useQueryClient();
  const status = useCryptoComStatus();
  const data = status.data;
  const positions = data?.ledger.positions ?? {};
  const held = Object.keys(positions);

  const top = useQuery({
    queryKey: ["crypto-com-top"],
    queryFn: () => apiFetch<CoinList>(`${base}/coins?keyword=`),
    refetchInterval: 30000,
    retry: false,
  });
  const [search, setSearch] = useState("");
  const term = search.trim().toUpperCase();
  const found = useQuery({
    queryKey: ["crypto-com-search", term],
    enabled: term.length > 0,
    queryFn: () => apiFetch<CoinList>(`${base}/coins?keyword=${encodeURIComponent(term)}`),
    retry: false,
  });
  const missing = held.filter((symbol) => !top.data?.coins.some((coin) => coin.symbol === symbol));
  const extra = useQueries({
    queries: missing.map((symbol) => ({
      queryKey: ["crypto-com-coin", symbol],
      queryFn: () => apiFetch<CoinList>(`${base}/coins?keyword=${encodeURIComponent(symbol)}`),
      staleTime: 30000,
      retry: false,
    })),
  });

  const known = useMemo(() => {
    const map = new Map<string, AppCoin>();
    for (const coin of top.data?.coins ?? []) map.set(coin.symbol, coin);
    for (const result of extra) {
      const match = result.data?.coins.find((coin) => held.includes(coin.symbol));
      if (match) map.set(match.symbol, match);
    }
    for (const coin of found.data?.coins ?? []) if (!map.has(coin.symbol)) map.set(coin.symbol, coin);
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [top.data, found.data, extra.map((result) => result.dataUpdatedAt).join(","), held.join(",")]);

  const listed = useMemo(() => {
    const rows = term
      ? (found.data?.coins ?? [])
      : [...held.map((symbol): AppCoin => known.get(symbol) ?? { symbol }),
        ...(top.data?.coins ?? []).filter((coin) => !held.includes(coin.symbol))];
    return rows;
  }, [term, found.data, top.data, known, held]);

  const [symbol, setSymbol] = useState("BTC");
  const [side, setSide] = useState<Side>("buy");
  const [interval, setIntervalValue] = useState<(typeof INTERVALS)[number]>("1h");
  const coin = known.get(symbol);
  const listedPrice = num(coin?.price_native?.amount);

  const candles = useQuery({
    queryKey: ["app-candles", server, symbol, interval],
    enabled: !!server,
    retry: false,
    staleTime: 20000,
    queryFn: async () => {
      for (const connector of CHART_VENUES) {
        try {
          // Without a window the backend returns only the last few hours.
          const end = Math.floor(Date.now() / 1000);
          const rows = await api.getCandles(server!, connector, `${symbol}-USDT`, interval, CANDLES,
            end - CANDLES * INTERVAL_SECONDS[interval], end);
          if (rows.length) return { connector, rows };
        } catch { /* try the next venue */ }
      }
      throw new Error(`No chart data for ${symbol}`);
    },
  });

  // ── order ticket ──
  const [amount, setAmount] = useState("");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [expiresAt, setExpiresAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [result, setResult] = useState<unknown>(null);
  useEffect(() => {
    if (!quote) return;
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [quote]);
  const refresh = () => qc.invalidateQueries({ queryKey: ["crypto-com-status"] });
  const heldUnits = positions[symbol]?.units;
  const cash = num(data?.ledger.cash_eur);

  function reset() { setQuote(null); }
  const requestQuote = useMutation({
    mutationFn: () => apiFetch<Quote>(`${base}/quote`, {
      method: "POST",
      body: JSON.stringify(side === "buy"
        ? { kind: "purchase", source: "EUR", target: symbol, amount }
        : { kind: "sale", source: symbol, target: "EUR", amount }),
    }),
    onSuccess: (next) => { setQuote(next); setExpiresAt(Date.now() + next.countdown * 1000); setNow(Date.now()); },
  });
  const execute = useMutation({
    mutationFn: (quotationId: string) => apiFetch(`${base}/execute`, { method: "POST", body: JSON.stringify({ quotation_id: quotationId }) }),
    onSuccess: (next) => { setResult(next); refresh(); qc.invalidateQueries({ queryKey: ["crypto-com-coin"] }); },
    onSettled: () => setQuote(null),
    retry: false,
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => apiFetch(`${base}/enabled`, { method: "PUT", body: JSON.stringify({ enabled }) }),
    onSuccess: () => { setQuote(null); refresh(); },
  });
  const inspect = useMutation({ mutationFn: (path: string) => apiFetch(base + path), onSuccess: setResult });
  const reconcile = useMutation({
    mutationFn: () => apiFetch(`${base}/reconcile`, { method: "POST" }),
    onSuccess: () => refresh(),
    retry: false,
  });
  const error = status.error || top.error || requestQuote.error || execute.error || toggle.error || inspect.error || reconcile.error;

  const remaining = quote ? Math.max(0, Math.ceil((expiresAt - now) / 1000)) : 0;
  const expired = !!quote && remaining <= 0;
  const effective = quote
    ? side === "buy"
      ? num(quote.from_amount.amount)! / num(quote.to_amount.amount)!
      : num(quote.to_amount.amount)! / num(quote.from_amount.amount)!
    : null;
  const versusListed = effective !== null && listedPrice ? (effective / listedPrice - 1) * 100 : null;
  // Positive means the quote is worse for you than the listed price.
  const costPct = versusListed === null ? null : side === "buy" ? versusListed : -versusListed;
  const amountValid = DECIMAL.test(amount) && Number(amount) > 0;
  const canQuote = !!data?.connected && amountValid && coin?.is_tradable !== false && !requestQuote.isPending && !execute.isPending;

  function pick(next: string, nextSide?: Side) {
    setSymbol(next);
    if (nextSide) setSide(nextSide);
    setAmount(""); reset();
  }
  function fill(fraction: number) {
    reset();
    if (side === "buy") setAmount(cash === null ? "" : units(cash * fraction, 2));
    else if (heldUnits) setAmount(fraction === 1 ? heldUnits : units(Number(heldUnits) * fraction));
  }

  const valueRows = held.map((held_symbol) => {
    const position = positions[held_symbol];
    const row = known.get(held_symbol);
    const value = num(position.units) !== null && num(row?.price_native?.amount) !== null
      ? num(position.units)! * num(row?.price_native?.amount)! : null;
    const cost = num(position.cost_eur);
    return { symbol: held_symbol, units: position.units, cost, value, pnl: value !== null && cost ? (value / cost - 1) * 100 : null };
  });

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center gap-3">
      <h1 className="text-xl font-semibold">Crypto.com App</h1>
      <span className="text-sm">{data?.connected ? "Agent Key saved" : "not connected"} · Trading: {data?.enabled ? "enabled" : "paused"}</span>
      {data?.ledger && <span className="text-sm">Cash {eur(cash)} · {held.length} holdings</span>}
      <div className="ml-auto flex flex-wrap gap-2">
        <button className={button} disabled={!data?.connected || toggle.isPending || !!data?.ledger.uncertain_order} onClick={() => toggle.mutate(!data?.enabled)}>{data?.enabled ? "Pause trading" : "Enable trading within allocation"}</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/accounts")}>Balances</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/history")}>History</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/valuation")}>Value allocation</button>
      </div>
    </div>
    {error && <p role="alert" className="text-sm text-[var(--color-red)]">{error.message}</p>}
    {!!data?.ledger.uncertain_order && <div className={card}>
      <p role="alert" className="text-sm text-[var(--color-red)]">An order needs reconciliation. Trading stays paused. Check Crypto.com history before any new submission.</p>
      <button className={`${button} mt-2`} disabled={reconcile.isPending} onClick={() => reconcile.mutate()}>Reconcile from provider history</button>
    </div>}

    <div className="grid gap-4 lg:grid-cols-[17rem_minmax(0,1fr)_20rem]">
      <section className={`${card} flex max-h-[34rem] flex-col`} aria-label="Markets">
        <input className={input} placeholder="Search coin" value={search} onChange={(event) => setSearch(event.target.value)} aria-label="Search coin" />
        <div className="mt-2 min-h-0 flex-1 overflow-auto">
          {top.isLoading && <p role="status" className="p-2 text-sm">Loading markets…</p>}
          {term && found.isLoading && <p role="status" className="p-2 text-sm">Searching…</p>}
          {listed.map((row) => <button key={row.symbol} type="button" aria-pressed={row.symbol === symbol}
            onClick={() => pick(row.symbol, held.includes(row.symbol) ? "sell" : "buy")}
            className="flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-[var(--color-surface-hover)] aria-pressed:bg-[var(--color-surface-hover)]">
            <span>
              <span className="font-medium">{row.symbol}</span>
              {held.includes(row.symbol) && <span className="ml-1 text-xs text-[var(--color-text-muted)]">held</span>}
              {row.is_tradable === false && <span className="ml-1 text-xs text-[var(--color-text-muted)]">not tradable</span>}
            </span>
            <span className="text-right"><span className="block">{price(row.price_native?.amount)}</span><span className="block text-xs"><Change value={row.percent_change_24h_native} /></span></span>
          </button>)}
          {term && !found.isLoading && !listed.length && <p className="p-2 text-sm">No App coin matches “{term}”.</p>}
        </div>
      </section>

      <section className={card} aria-label="Chart">
        <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h2 className="text-lg font-semibold">{symbol}{coin?.name ? <span className="ml-2 text-sm font-normal text-[var(--color-text-muted)]">{coin.name}</span> : null}</h2>
          <span className="text-lg">{price(coin?.price_native?.amount)}</span>
          <span className="text-sm"><Change value={coin?.percent_change_24h_native} label="24h " /></span>
          <span className="text-sm"><Change value={coin?.percent_change_1w_native} label="1w " /></span>
          <span className="text-sm"><Change value={coin?.percent_change_30d_native} label="30d " /></span>
          <div className="ml-auto flex gap-1" role="group" aria-label="Interval">
            {INTERVALS.map((value) => <button key={value} className={button} aria-pressed={interval === value} onClick={() => setIntervalValue(value)}>{value}</button>)}
          </div>
        </div>
        <div className="h-[24rem]">
          {candles.isLoading && <p role="status" className="p-4 text-sm">Loading chart…</p>}
          {candles.isError && <p className="p-4 text-sm text-[var(--color-text-muted)]">{candles.error.message}. Trading this coin still works without a chart.</p>}
          {candles.data && <CandleChart rows={candles.data.rows} />}
        </div>
        <p className="mt-2 text-xs text-[var(--color-text-muted)]">
          Chart: {candles.data ? `${candles.data.connector} ${symbol}/USDT` : "public exchange candles in USDT"}. The App prices and quotes are in EUR, so levels differ from the chart. Spreads apply when selling, so check the quote.
        </p>
      </section>

      <section className={`${card} space-y-3`} aria-label="Order ticket">
        <div className="flex gap-2" role="group" aria-label="Side">
          <button className={`${button} flex-1`} aria-pressed={side === "buy"} onClick={() => { setSide("buy"); setAmount(""); reset(); }}>Buy</button>
          <button className={`${button} flex-1`} aria-pressed={side === "sell"} onClick={() => { setSide("sell"); setAmount(""); reset(); }}>Sell</button>
        </div>
        <p className="text-sm">{side === "buy" ? `Buy ${symbol} with EUR` : `Sell ${symbol} to EUR`}</p>
        {coin?.is_tradable === false && <p className="text-sm text-[var(--color-red)]">{symbol} is not tradable in the App.</p>}
        <form className="space-y-3" onChange={reset} onSubmit={(event) => { event.preventDefault(); reset(); requestQuote.mutate(); }}>
          <label className="block text-sm">{side === "buy" ? "Amount to spend (EUR)" : `Amount to sell (${symbol})`}
            <input className={input} value={amount} inputMode="decimal" onChange={(event) => setAmount(event.target.value)} required />
          </label>
          <div className="flex gap-2">
            {[0.25, 0.5, 1].map((fraction) => <button key={fraction} type="button" className={`${button} flex-1`}
              disabled={side === "sell" && !heldUnits} onClick={() => fill(fraction)}>{fraction === 1 ? "Max" : `${fraction * 100}%`}</button>)}
          </div>
          <p className="text-xs text-[var(--color-text-muted)]">
            {side === "buy" ? `Cash available ${eur(cash)}.` : heldUnits ? `Held ${heldUnits} ${symbol}.` : `No ${symbol} held in the bot allocation.`}
          </p>
          <button className={`${button} w-full`} disabled={!canQuote}>Get quotation</button>
        </form>
        {quote && <div className="space-y-2 border-t border-[var(--color-border)] pt-3 text-sm">
          <p>{side === "buy" ? "Spend" : "Sell"} {quote.from_amount.amount} {quote.from_amount.currency} → receive {quote.to_amount.amount} {quote.to_amount.currency}</p>
          {effective !== null && <p>Effective price {price(String(effective))}{costPct !== null && <> · <span className={costPct >= 2 ? "text-[var(--color-red)]" : ""}>{costPct >= 0 ? "costs " : "saves "}{Math.abs(costPct).toFixed(2)}% vs listed</span></>}</p>}
          {quote.fee?.amount !== undefined && <p>Fee {quote.fee.amount} {quote.fee.currency}</p>}
          <p className="text-xs text-[var(--color-text-muted)]">{expired ? "Quotation expired. Request a new one." : `Quotation expires in ${remaining} seconds. Expired quotes are rejected by the server.`}</p>
          <button className={`${button} w-full`} disabled={!data?.enabled || execute.isPending || expired} onClick={() => {
            if (Date.now() >= expiresAt) { setQuote(null); return; }
            execute.mutate(quote.id);
          }}>Confirm & submit order</button>
          {!data?.enabled && <p className="text-xs text-[var(--color-red)]">Trading is paused. Enable it above to submit.</p>}
        </div>}
      </section>
    </div>

    <section className={card} aria-label="Holdings">
      <h2 className="mb-2 font-semibold">Bot holdings</h2>
      <table className="w-full text-left text-sm">
        <thead><tr><th>Asset</th><th>Units</th><th>Cost</th><th>Listed value</th><th>vs cost</th><th></th></tr></thead>
        <tbody>
          <tr><td>EUR cash</td><td>{data?.ledger.cash_eur ?? "—"}</td><td>—</td><td>{eur(cash)}</td><td>—</td><td></td></tr>
          {valueRows.map((row) => <tr key={row.symbol}>
            <td>{row.symbol}</td><td>{row.units}</td><td>{eur(row.cost)}</td><td>{eur(row.value)}</td>
            <td>{row.pnl === null ? "—" : <Change value={String(row.pnl)} />}</td>
            <td><button className={button} onClick={() => pick(row.symbol, "sell")}>Sell…</button></td>
          </tr>)}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-[var(--color-text-muted)]">Listed value uses App listing prices; selling realises less after the sale spread. Only the bot allocation is traded; the agent loop uses this same ledger.</p>
    </section>

    <section className={card}>
      <h2 className="mb-1 font-semibold">Bot allocation rules</h2>
      <p className="text-sm">{limitsSummary(data?.limits)}</p>
      <p className="mt-2 text-xs text-[var(--color-text-muted)]">Enabling execution does not start a loop or place an order. A stop is not a guaranteed exit price.</p>
    </section>
    {result !== null && <AppResult value={result} />}
  </div>;
}
