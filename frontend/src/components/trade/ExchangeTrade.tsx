import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { ExchangeResult } from "@/components/settings/CryptoComExchange";
import { limitsSummary } from "@/components/settings/cryptoExchangeLimits";
import { useCryptoExchangeStatus } from "@/hooks/useCredentials";
import { apiFetch, type CandleData } from "@/lib/api";
import { getThemeColors } from "@/lib/theme-colors";

export type ExchangeCoin = {
  symbol: string;
  pair?: string;
  price?: string;
  bid?: string;
  ask?: string;
  spread_pct?: string;
  change_24h_pct?: string | null;
  volume_24h?: string;
  min_quantity_step?: string | null;
};
type CoinList = { quote: string; coins: ExchangeCoin[] };
type Candles = { pair: string; timeframe: string; candles: { t: number | string; o: string; h: string; l: string; c: string; v: string }[] };
type Preview = {
  pair: string;
  side: Side;
  quantity: string;
  limit_price: string;
  bid: string;
  ask: string;
  spread_pct: string;
  estimated_average_price: string | null;
  estimated_impact_pct: string | null;
  book_covers_order: boolean;
};
type OrderBody = { side: Side; symbol: string; amount: string; max_slippage_pct?: string };
type Side = "buy" | "sell";
type Summary = {
  total: number; resolved: number; open: number; hit_target: number; stopped: number; expired: number;
  avg_gross_pct: number | null; avg_net_pct: number | null; beat_costs: number;
};
export type Scorecard = {
  assumptions: { round_trip_cost_pct: number; basis: string };
  prices: { snapshots: number; coins: number; days: number };
  trade_calls: Summary;
  pass_calls: Summary;
  by_cost_multiple: { multiple: number; min_target_pct: number; resolved: number; avg_net_pct: number | null; win_rate: number | null }[];
  baseline_gross_pct: number | null;
  edge_vs_baseline_pct: number | null;
  enough_data: boolean;
  recent: { ts: number; symbol: string; verdict: string; target_pct: number; stop_pct: number; horizon_h: number; outcome: string; net_pct: number | null }[];
};

const base = "/api/v1/crypto-exchange";
const INTERVALS = ["15m", "1h", "4h", "1D"] as const;
const CANDLES = 200;
const input = "w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40 aria-pressed:bg-[var(--color-surface-hover)]";
const card = "rounded-lg border border-[var(--color-border)] p-4";
const DECIMAL = /^\d{1,18}(\.\d{1,18})?$/;

const num = (value?: string | number | null) => {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};
const digitsFor = (value: number) => (value >= 100 ? 2 : value >= 1 ? 3 : value >= 0.01 ? 4 : 8);
/** A price or amount in the ledger quote currency, with the currency named rather than assumed. */
const money = (value: number | null, quote: string, digits = 2) =>
  value === null ? "—" : `${value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${quote}`.trim();
const price = (value?: string | null) => {
  const parsed = num(value);
  return parsed === null ? "—" : parsed.toLocaleString(undefined, { maximumFractionDigits: digitsFor(parsed) });
};
function Change({ value, label }: { value?: string | null; label?: string }) {
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
  return <div ref={ref} data-testid="exchange-chart" className="h-full w-full" />;
}

const signed = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;

/** Paper test of the trader's logged calls, scored against real prices after costs. */
function ScorecardPanel() {
  const score = useQuery({
    queryKey: ["crypto-exchange-scorecard"],
    queryFn: () => apiFetch<Scorecard>(`${base}/scorecard`),
    refetchInterval: 60000,
    retry: false,
  });
  const data = score.data;
  if (!data?.trade_calls) {
    return <section className={card} aria-label="Scorecard">
      <h2 className="mb-1 font-semibold">Trader scorecard</h2>
      <p className="text-sm text-[var(--color-text-muted)]">{score.isError ? "Scorecard unavailable." : "Loading scorecard…"}</p>
    </section>;
  }
  const resolved = data.trade_calls.resolved + data.pass_calls.resolved;
  const rows: [string, Summary][] = [["Trade calls", data.trade_calls], ["Pass calls (what it skipped)", data.pass_calls]];
  return <section className={card} aria-label="Scorecard">
    <h2 className="mb-1 font-semibold">Trader scorecard</h2>
    <p className="text-sm">
      Paper test: each call the trader logs (coin, target, stop, time window) is scored later against real Exchange prices after a {data.assumptions.round_trip_cost_pct}% round-trip cost. No money moves.
    </p>
    <p className="mt-1 text-xs text-[var(--color-text-muted)]">
      Tracking {data.prices.coins} coins · {data.prices.snapshots} price snapshots over {data.prices.days} days · {resolved} of 30 resolved calls needed before trusting the result{data.enough_data ? " (reached)" : ""}.
    </p>
    <table className="mt-3 w-full text-left text-sm">
      <thead><tr><th>Calls</th><th>Logged</th><th>Resolved</th><th>Open</th><th>Hit target</th><th>Stopped</th><th>Avg net</th><th>Beat costs</th></tr></thead>
      <tbody>{rows.map(([label, row]) => <tr key={label}>
        <td>{label}</td><td>{row.total}</td><td>{row.resolved}</td><td>{row.open}</td><td>{row.hit_target}</td><td>{row.stopped}</td>
        <td>{signed(row.avg_net_pct)}</td><td>{row.beat_costs}/{row.resolved}</td>
      </tr>)}</tbody>
    </table>
    <p className="mt-2 text-sm">
      Baseline (average of all listed coins over the same windows): {signed(data.baseline_gross_pct)} · Edge vs baseline: {signed(data.edge_vs_baseline_pct)}
    </p>
    <h3 className="mb-1 mt-3 text-sm font-medium">What cost multiple would have worked?</h3>
    <table className="w-full text-left text-sm">
      <thead><tr><th>Target at least</th><th>Resolved</th><th>Avg net</th><th>Win rate</th></tr></thead>
      <tbody>{data.by_cost_multiple.map((row) => <tr key={row.multiple}>
        <td>{row.multiple}× cost ({row.min_target_pct.toFixed(1)}%)</td><td>{row.resolved}</td><td>{signed(row.avg_net_pct)}</td>
        <td>{row.win_rate === null ? "—" : `${Math.round(row.win_rate * 100)}%`}</td>
      </tr>)}</tbody>
    </table>
    {data.recent.length > 0 && <>
      <h3 className="mb-1 mt-3 text-sm font-medium">Latest calls</h3>
      <ul className="text-sm">{data.recent.slice(0, 5).map((row, index) => <li key={`${row.ts}-${row.symbol}-${index}`}>
        {row.symbol} · {row.verdict} · target +{row.target_pct}% / stop −{row.stop_pct}% in {row.horizon_h}h · {row.outcome}{row.net_pct === null ? "" : ` (${signed(row.net_pct)} net)`}
      </li>)}</ul>
    </>}
  </section>;
}

function PreviewDetails({ preview, quote, symbol, amount }: { preview: Preview; quote: string; symbol: string; amount: string }) {
  const buy = preview.side === "buy";
  return <div className="space-y-1">
    <p className="font-medium">{buy ? "Buy" : "Sell"} up to {preview.quantity} {symbol} on {preview.pair}</p>
    <p>{buy ? `Spending at most ${amount} ${quote}` : `Selling ${amount} ${symbol}`} · limit price {preview.limit_price} {quote}</p>
    <p>Best bid {preview.bid} · best ask {preview.ask} · spread {preview.spread_pct}%</p>
    <p>Estimated average price {preview.estimated_average_price ? `${preview.estimated_average_price} ${quote}` : "—"}
      {preview.estimated_impact_pct !== null && <> · price impact {preview.estimated_impact_pct}%</>}</p>
    {!preview.book_covers_order && <p className="text-[var(--color-red)]">The order book is too thin to fill the whole order. Expect a partial fill.</p>}
    <p className="text-xs text-[var(--color-text-muted)]">Immediate-or-cancel limit order: it fills what it can at or better than the limit price right away and cancels the rest. It is never resubmitted automatically.</p>
  </div>;
}

export function ExchangeTrade() {
  const qc = useQueryClient();
  const status = useCryptoExchangeStatus();
  const data = status.data;
  const ledger = data?.ledger;
  const quote = ledger?.quote ?? "USD";
  const positions = ledger?.positions ?? {};
  const held = Object.keys(positions);
  const uncertain = !!ledger?.uncertain_order;

  const top = useQuery({
    queryKey: ["crypto-exchange-top"],
    queryFn: () => apiFetch<CoinList>(`${base}/coins?keyword=`),
    refetchInterval: 30000,
    retry: false,
  });
  const [search, setSearch] = useState("");
  const term = search.trim().toUpperCase();
  const found = useQuery({
    queryKey: ["crypto-exchange-search", term],
    enabled: term.length > 0,
    queryFn: () => apiFetch<CoinList>(`${base}/coins?keyword=${encodeURIComponent(term)}`),
    retry: false,
  });

  const known = useMemo(() => {
    const map = new Map<string, ExchangeCoin>();
    for (const coin of top.data?.coins ?? []) map.set(coin.symbol, coin);
    for (const coin of found.data?.coins ?? []) if (!map.has(coin.symbol)) map.set(coin.symbol, coin);
    return map;
  }, [top.data, found.data]);

  // The server appends held coins to the top list, so a holding always has its live price.
  const listed = useMemo(() => term
    ? (found.data?.coins ?? [])
    : [...held.map((symbol): ExchangeCoin => known.get(symbol) ?? { symbol }),
      ...(top.data?.coins ?? []).filter((coin) => !held.includes(coin.symbol))],
  // eslint-disable-next-line react-hooks/exhaustive-deps
  [term, found.data, top.data, known, held.join(",")]);

  const [symbol, setSymbol] = useState("BTC");
  const [side, setSide] = useState<Side>("buy");
  const [interval, setIntervalValue] = useState<(typeof INTERVALS)[number]>("1h");
  const coin = known.get(symbol);

  const candles = useQuery({
    queryKey: ["crypto-exchange-candles", symbol, interval],
    retry: false,
    staleTime: 20000,
    queryFn: async () => {
      const res = await apiFetch<Candles>(`${base}/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${interval}&count=${CANDLES}`);
      const rows: CandleData[] = res.candles
        .map((row) => ({ timestamp: Math.floor(Number(row.t) / 1000), open: Number(row.o), high: Number(row.h), low: Number(row.l), close: Number(row.c), volume: Number(row.v) }))
        .filter((row) => [row.timestamp, row.open, row.high, row.low, row.close].every(Number.isFinite));
      if (!rows.length) throw new Error(`No chart data for ${symbol}`);
      return { pair: res.pair, rows };
    },
  });

  // ── order ticket: preview first, then place ──
  const [amount, setAmount] = useState("");
  const [slippage, setSlippage] = useState("");
  const [preview, setPreview] = useState<{ body: OrderBody; data: Preview } | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<unknown>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["crypto-exchange-status"] });
  const heldUnits = positions[symbol]?.units;
  const cash = num(ledger?.cash);

  function reset() { setPreview(null); setConfirming(false); }
  const requestPreview = useMutation({
    mutationFn: (body: OrderBody) => apiFetch<Preview>(`${base}/preview`, { method: "POST", body: JSON.stringify(body) }),
    onSuccess: (next, body) => setPreview({ body, data: next }),
    retry: false,
  });
  const place = useMutation({
    mutationFn: (body: OrderBody) => apiFetch(`${base}/order`, { method: "POST", body: JSON.stringify(body) }),
    onSuccess: (next) => { setResult(next); setAmount(""); },
    // An order whose outcome is unknown fails with an error but changes the ledger, so always re-read the status.
    onSettled: () => { reset(); refresh(); qc.invalidateQueries({ queryKey: ["crypto-exchange-top"] }); },
    retry: false,
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => apiFetch(`${base}/enabled`, { method: "PUT", body: JSON.stringify({ enabled }) }),
    onSuccess: () => reset(),
    onSettled: () => refresh(),
  });
  const inspect = useMutation({ mutationFn: (path: string) => apiFetch(base + path), onSuccess: setResult });
  const reconcile = useMutation({
    mutationFn: () => apiFetch(`${base}/reconcile`, { method: "POST" }),
    onSettled: () => refresh(),
    retry: false,
  });
  const error = status.error || top.error || requestPreview.error || place.error || toggle.error || inspect.error || reconcile.error;

  const amountValid = DECIMAL.test(amount) && Number(amount) > 0;
  const slippageValid = slippage === "" || (DECIMAL.test(slippage) && Number(slippage) > 0);
  const synced = !!ledger?.synced;
  const canPreview = !!data?.connected && synced && amountValid && slippageValid && !requestPreview.isPending && !place.isPending;

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

  const valueRows = held.map((heldSymbol) => {
    const position = positions[heldSymbol];
    const bid = num(known.get(heldSymbol)?.bid);
    const value = num(position.units) !== null && bid !== null ? num(position.units)! * bid : null;
    const cost = num(position.cost);
    return { symbol: heldSymbol, units: position.units, cost, value, pnl: value !== null && cost ? (value / cost - 1) * 100 : null };
  });
  const botOrders = [...(ledger?.orders ?? [])].reverse().slice(0, 5);

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center gap-3">
      <h1 className="text-xl font-semibold">Crypto.com Exchange</h1>
      <span className="text-sm">{data?.connected ? `API key saved (${data.environment})` : "not connected"} · Trading: {data?.enabled ? "enabled" : "paused"}</span>
      {ledger && <span className="text-sm">Cash {money(cash, quote)} · {held.length} holdings</span>}
      <div className="ml-auto flex flex-wrap gap-2">
        <button className={button} disabled={!data?.connected || toggle.isPending || (!data?.enabled && (uncertain || !synced))} onClick={() => toggle.mutate(!data?.enabled)}>{data?.enabled ? "Pause trading" : "Enable trading within allocation"}</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/accounts")}>Balances</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/history")}>History</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/valuation")}>Value allocation</button>
      </div>
    </div>
    {error && <p role="alert" className="text-sm text-[var(--color-red)]">{error.message}</p>}
    {data && !data.connected && <p className="text-sm">Connect an Exchange API key in <a className="underline" href="/settings?tab=crypto-exchange">Settings</a> to trade.</p>}
    {data?.connected && !synced && <p className="text-sm">The bot allocation has not been synced from the Exchange account. Use “Sync allocation from Exchange account” in <a className="underline" href="/settings?tab=crypto-exchange">Settings</a> before trading.</p>}
    {uncertain && <div className={card}>
      <p role="alert" className="text-sm text-[var(--color-red)]">An order needs reconciliation. Trading stays paused. Check the Exchange order history before any new submission.</p>
      <button className={`${button} mt-2`} disabled={reconcile.isPending} onClick={() => reconcile.mutate()}>Reconcile from Exchange order history</button>
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
            </span>
            <span className="text-right"><span className="block">{price(row.price)}</span><span className="block text-xs"><Change value={row.change_24h_pct} /></span></span>
          </button>)}
          {term && !found.isLoading && !listed.length && <p className="p-2 text-sm">No Exchange coin matches “{term}” in {quote}.</p>}
        </div>
      </section>

      <section className={card} aria-label="Chart">
        <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h2 className="text-lg font-semibold">{symbol}<span className="ml-2 text-sm font-normal text-[var(--color-text-muted)]">{symbol}/{quote}</span></h2>
          <span className="text-lg">{price(coin?.price)}</span>
          <span className="text-sm"><Change value={coin?.change_24h_pct} label="24h " /></span>
          {coin?.spread_pct !== undefined && <span className="text-sm text-[var(--color-text-muted)]">spread {coin.spread_pct}%</span>}
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
          Chart: {candles.data ? `Exchange ${candles.data.pair}` : "Exchange candles"} ({interval}). Orders go to the public order book, so the preview shows the spread and price impact before you place anything.
        </p>
      </section>

      <section className={`${card} space-y-3`} aria-label="Order ticket">
        <div className="flex gap-2" role="group" aria-label="Side">
          <button className={`${button} flex-1`} aria-pressed={side === "buy"} onClick={() => { setSide("buy"); setAmount(""); reset(); }}>Buy</button>
          <button className={`${button} flex-1`} aria-pressed={side === "sell"} onClick={() => { setSide("sell"); setAmount(""); reset(); }}>Sell</button>
        </div>
        <p className="text-sm">{side === "buy" ? `Buy ${symbol} with ${quote}` : `Sell ${symbol} for ${quote}`}</p>
        <form className="space-y-3" onChange={reset} onSubmit={(event) => { event.preventDefault(); reset(); requestPreview.mutate({ side, symbol, amount, ...(slippage ? { max_slippage_pct: slippage } : {}) }); }}>
          <label className="block text-sm">{side === "buy" ? `Amount to spend (${quote})` : `Amount to sell (${symbol})`}
            <input className={input} value={amount} inputMode="decimal" onChange={(event) => setAmount(event.target.value)} required />
          </label>
          <div className="flex gap-2">
            {[0.25, 0.5, 1].map((fraction) => <button key={fraction} type="button" className={`${button} flex-1`}
              disabled={side === "sell" && !heldUnits} onClick={() => fill(fraction)}>{fraction === 1 ? "Max" : `${fraction * 100}%`}</button>)}
          </div>
          <p className="text-xs text-[var(--color-text-muted)]">
            {side === "buy" ? `Cash available ${money(cash, quote)}.` : heldUnits ? `Held ${heldUnits} ${symbol}.` : `No ${symbol} held in the bot allocation.`}
          </p>
          <label className="block text-sm">Max slippage (%)
            <input className={input} value={slippage} inputMode="decimal" placeholder={data?.limits?.default_slippage_pct ? `default ${data.limits.default_slippage_pct}` : "default"} onChange={(event) => setSlippage(event.target.value)} />
          </label>
          <button className={`${button} w-full`} disabled={!canPreview}>Preview order</button>
        </form>
        {preview && <div className="space-y-2 border-t border-[var(--color-border)] pt-3 text-sm">
          <PreviewDetails preview={preview.data} quote={quote} symbol={preview.body.symbol} amount={preview.body.amount} />
          <button className={`${button} w-full`} disabled={!data?.enabled || place.isPending} onClick={() => setConfirming(true)}>Place order…</button>
          {!data?.enabled && <p className="text-xs text-[var(--color-red)]">Trading is paused. Enable it above to place an order.</p>}
        </div>}
      </section>
    </div>

    {confirming && preview && <div role="dialog" aria-modal="true" aria-label="Confirm order" className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-md space-y-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] p-5 text-sm">
        <h2 className="text-base font-semibold">Confirm order</h2>
        <PreviewDetails preview={preview.data} quote={quote} symbol={preview.body.symbol} amount={preview.body.amount} />
        <p className="text-xs text-[var(--color-text-muted)]">The numbers above are an estimate from the live order book; the Exchange fills the order at the time it is placed.</p>
        <div className="flex gap-2">
          <button className={`${button} flex-1`} disabled={place.isPending} onClick={() => setConfirming(false)}>Cancel</button>
          <button className={`${button} flex-1`} disabled={!data?.enabled || place.isPending} onClick={() => place.mutate(preview.body)}>Confirm and place order</button>
        </div>
      </div>
    </div>}

    <section className={card} aria-label="Holdings">
      <h2 className="mb-2 font-semibold">Bot holdings</h2>
      <table className="w-full text-left text-sm">
        <thead><tr><th>Asset</th><th>Units</th><th>Cost</th><th>Value at bid</th><th>vs cost</th><th></th></tr></thead>
        <tbody>
          <tr><td>{quote} cash</td><td>{ledger?.cash ?? "—"}</td><td>—</td><td>{money(cash, quote)}</td><td>—</td><td></td></tr>
          {valueRows.map((row) => <tr key={row.symbol}>
            <td>{row.symbol}</td><td>{row.units}</td><td>{money(row.cost, quote)}</td><td>{money(row.value, quote)}</td>
            <td>{row.pnl === null ? "—" : <Change value={String(row.pnl)} />}</td>
            <td><button className={button} onClick={() => pick(row.symbol, "sell")}>Sell…</button></td>
          </tr>)}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-[var(--color-text-muted)]">Value uses the live best bid, the price a sale would meet first. Cost for coins carried over at a sync restarts at their bid then. Only the bot allocation is traded; the agent loop uses this same ledger.</p>
    </section>

    {botOrders.length > 0 && <section className={card} aria-label="Bot orders">
      <h2 className="mb-2 font-semibold">Recent bot orders</h2>
      <table className="w-full text-left text-sm">
        <thead><tr><th>Time</th><th>Side</th><th>Asset</th><th>Status</th><th>Filled</th><th>Value</th><th>Fee</th></tr></thead>
        <tbody>{botOrders.map((order) => <tr key={`${order.order_id ?? order.client_oid}-${order.time}`}>
          <td>{new Date(order.time * 1000).toLocaleString()}</td><td>{order.side}</td><td>{order.symbol}</td><td>{order.status}</td>
          <td>{order.quantity}</td><td>{order.value} {quote}</td><td>{order.fee} {order.fee_currency ?? ""}</td>
        </tr>)}</tbody>
      </table>
    </section>}

    <section className={card}>
      <h2 className="mb-1 font-semibold">Bot allocation rules</h2>
      <p className="text-sm">{limitsSummary(data?.limits, quote)}</p>
      <p className="mt-2 text-xs text-[var(--color-text-muted)]">Enabling execution does not start a loop or place an order.</p>
    </section>
    <ScorecardPanel />
    {result !== null && <ExchangeResult value={result} />}
  </div>;
}
