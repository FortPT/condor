import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { ExchangeResult } from "@/components/settings/CryptoComExchange";
import { apiFetch, type CandleData } from "@/lib/api";
import { CandleChart, Change, ScorecardPanel } from "./TradeParts";
import {
  accountValue, accountsOf, equityOf, historyOf, holdingsOf, rowsOf, useNativeStatus,
  type NativeVenue, type VenueRow,
} from "./nativeVenue";
import { money, num, price } from "./tradeFormat";

type Candles = { pair: string; timeframe: string; candles: { t: number | string; o: string; h: string; l: string; c: string; v: string }[] };

const INTERVALS = ["15m", "1h", "4h", "1D"] as const;
const CANDLES = 200;
const input = "w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40 aria-pressed:bg-[var(--color-surface-hover)]";
const card = "rounded-lg border border-[var(--color-border)] p-4";
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const volume = (value: string | number | null) => {
  const parsed = num(value);
  return parsed === null ? "—" : compact.format(parsed);
};

/**
 * A read-only look at a native venue (Kraken xStocks, Revolut X): the bot allocation and
 * what it holds, the whole account's balances, the most traded markets, a chart, recent
 * orders and the paper scorecard. There is no order ticket: the agent loop trades, and
 * connecting, syncing and enabling live in Settings.
 *
 * The cadence is the Crypto.com panel's: status every 10 s and the top markets every
 * 30 s (public data, cached on the server). Balances, history and valuation are read once
 * and on request, never polled, because Revolut X's public endpoints allow one request a
 * second and its valuation reads a book per position.
 */
export function NativeVenueTrade({ venue }: { venue: NativeVenue }) {
  const { base, key, title } = venue;
  const status = useNativeStatus(venue);
  const data = status.data;
  const ledger = data?.ledger;
  const quote = ledger?.quote ?? data?.quote ?? venue.quote;
  const connected = !!data?.connected;
  const synced = !!ledger?.synced;
  const held = Object.keys(ledger?.positions ?? {});
  const uncertain = !!ledger?.uncertain_order;
  const settings = `/settings?tab=${key}`;

  const top = useQuery({
    queryKey: [key, "top"],
    queryFn: () => apiFetch<unknown>(`${base}/coins?keyword=`),
    refetchInterval: 30000,
    retry: false,
  });
  const [search, setSearch] = useState("");
  const term = search.trim().toUpperCase();
  const found = useQuery({
    queryKey: [key, "search", term],
    enabled: term.length > 0,
    queryFn: () => apiFetch<unknown>(`${base}/coins?keyword=${encodeURIComponent(term)}`),
    retry: false,
  });
  const topRows = useMemo(() => rowsOf(venue, top.data), [venue, top.data]);
  const foundRows = useMemo(() => rowsOf(venue, found.data), [venue, found.data]);
  const known = useMemo(() => {
    const map = new Map<string, VenueRow>();
    for (const row of [...topRows, ...foundRows]) if (!map.has(row.symbol)) map.set(row.symbol, row);
    return map;
  }, [topRows, foundRows]);
  // The server appends held coins to the top list, so a holding always has its live price.
  const listed = term ? foundRows : topRows;

  const [picked, setPicked] = useState<string | null>(null);
  const symbol = picked ?? held[0] ?? topRows[0]?.symbol ?? null;
  const [interval, setIntervalValue] = useState<(typeof INTERVALS)[number]>("1h");
  const current = symbol ? known.get(symbol) : undefined;
  const candles = useQuery({
    queryKey: [key, "candles", symbol, interval],
    enabled: !!symbol,
    retry: false,
    staleTime: 20000,
    queryFn: async () => {
      const res = await apiFetch<Candles>(`${base}/candles?symbol=${encodeURIComponent(symbol!)}&timeframe=${interval}&count=${CANDLES}`);
      const rows: CandleData[] = res.candles
        .map((row) => ({ timestamp: Math.floor(Number(row.t) / 1000), open: Number(row.o), high: Number(row.h), low: Number(row.l), close: Number(row.c), volume: Number(row.v) }))
        .filter((row) => [row.timestamp, row.open, row.high, row.low, row.close].every(Number.isFinite));
      if (!rows.length) throw new Error(`No chart data for ${symbol}`);
      return { pair: res.pair, rows };
    },
  });

  // Private reads: only once connected, never polled.
  const accounts = useQuery({
    queryKey: [key, "accounts"],
    enabled: connected,
    queryFn: () => apiFetch<unknown>(`${base}/accounts`),
    staleTime: 60000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const history = useQuery({
    queryKey: [key, "history"],
    enabled: connected,
    queryFn: () => apiFetch<unknown>(`${base}/history`),
    staleTime: 60000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  // Valuation reads a live book per position on Revolut X, so it runs on request only.
  const valuation = useMutation({ mutationFn: () => apiFetch<unknown>(`${base}/valuation`), retry: false });

  const holdings = holdingsOf(ledger, known);
  const cash = num(ledger?.cash);
  const equity = equityOf(ledger, holdings);
  const accountRows = accountsOf(accounts.data);
  const historyRows = historyOf(venue, history.data);
  const botOrders = [...(ledger?.orders ?? [])].reverse().slice(0, 5);
  const error = status.error || top.error || found.error || accounts.error || history.error || valuation.error;

  return <div className="space-y-4" data-venue={key}>
    <div className="flex flex-wrap items-center gap-3">
      <h1 className="text-xl font-semibold">{title}</h1>
      <span className="text-sm">{connected ? "Connected" : data ? "not connected" : "Checking connection…"} · Trading: {data?.enabled ? "enabled" : "paused"}</span>
      {ledger && <span className="text-sm">Cash {money(cash, quote)} · {held.length} holdings</span>}
      {ledger && <span className="text-sm">Equity {equity === null ? "—" : money(equity, quote)}</span>}
      {data?.market && <span className="text-sm">US market {data.market.open ? "open" : "closed"}{data.market.note ? ` · ${data.market.note}` : ""}</span>}
      <div className="ml-auto flex flex-wrap gap-2">
        <button className={button} disabled={!connected || accounts.isFetching} onClick={() => void accounts.refetch()}>{accounts.isFetching ? "Reading balances…" : "Refresh balances"}</button>
        <button className={button} disabled={!connected || history.isFetching} onClick={() => void history.refetch()}>{history.isFetching ? "Reading history…" : "Refresh history"}</button>
        <button className={button} disabled={!connected || valuation.isPending} onClick={() => valuation.mutate()}>{valuation.isPending ? "Valuing…" : "Value allocation"}</button>
      </div>
    </div>
    {error && <p role="alert" className="text-sm text-[var(--color-red)]">{error.message}</p>}
    {data && !connected && <p className="text-sm" data-state="not-connected">Not connected - open <a className="underline" href={settings}>Settings</a> to add the {title} API key. Markets below are public data.</p>}
    {connected && !synced && <p className="text-sm">The bot allocation has not been synced from the {title} account. Use the sync in <a className="underline" href={settings}>Settings</a> to start.</p>}
    {uncertain && <p role="alert" className="text-sm text-[var(--color-red)]">An order needs reconciliation and trading stays paused. Reconcile it in <a className="underline" href={settings}>Settings</a> or ask the trading agent.</p>}
    <p className="text-xs text-[var(--color-text-muted)]">Read-only view: the agent loop trades inside the bot allocation. Connecting, syncing and enabling trading are in Settings.</p>

    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <section className={`${card} flex max-h-[34rem] flex-col`} aria-label="Markets">
        <input className={input} placeholder={`Search ${venue.noun === "coins" ? "coin" : "stock"}`} value={search} onChange={(event) => setSearch(event.target.value)} aria-label={`Search ${venue.noun === "coins" ? "coin" : "stock"}`} />
        <div className="mt-2 min-h-0 flex-1 overflow-auto">
          {top.isLoading && <p role="status" className="p-2 text-sm">Loading markets…</p>}
          {term && found.isLoading && <p role="status" className="p-2 text-sm">Searching…</p>}
          {top.isError && <p className="p-2 text-sm text-[var(--color-text-muted)]">Markets unavailable: {top.error.message}</p>}
          {listed.length > 0 && <table className="w-full text-left text-sm">
            <thead><tr><th>{venue.noun === "coins" ? "Coin" : "Stock"}</th><th>Price</th><th>Bid</th><th>Ask</th><th>Spread</th><th>{venue.changeLabel}</th><th>{venue.volumeLabel}</th></tr></thead>
            <tbody>{listed.map((row) => <tr key={row.symbol}>
              <td><button type="button" aria-pressed={row.symbol === symbol} className="rounded px-1 font-medium hover:bg-[var(--color-surface-hover)] aria-pressed:bg-[var(--color-surface-hover)]" onClick={() => setPicked(row.symbol)}>{row.symbol}</button>
                {held.includes(row.symbol) && <span className="ml-1 text-xs text-[var(--color-text-muted)]">held</span>}</td>
              <td>{price(row.price)}</td><td>{price(row.bid)}</td><td>{price(row.ask)}</td>
              <td>{row.spread_pct === undefined ? "—" : `${row.spread_pct}%`}</td>
              <td><Change value={row.change_pct} /></td><td>{volume(row.volume)}</td>
            </tr>)}</tbody>
          </table>}
          {!top.isLoading && !top.isError && !term && !listed.length && <p className="p-2 text-sm">No {venue.noun} are listed on {title} right now.</p>}
          {term && !found.isLoading && !listed.length && <p className="p-2 text-sm">No {title} {venue.noun === "coins" ? "coin" : "stock"} matches “{term}”.</p>}
        </div>
      </section>

      <section className={card} aria-label="Chart">
        <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h2 className="text-lg font-semibold">{symbol ?? "—"}{symbol && <span className="ml-2 text-sm font-normal text-[var(--color-text-muted)]">{symbol}/{quote}</span>}</h2>
          <span className="text-lg">{price(current?.price)}</span>
          <span className="text-sm"><Change value={current?.change_pct} label={`${venue.changeLabel} `} /></span>
          {current?.spread_pct !== undefined && <span className="text-sm text-[var(--color-text-muted)]">spread {current.spread_pct}%</span>}
          <div className="ml-auto flex gap-1" role="group" aria-label="Interval">
            {INTERVALS.map((value) => <button key={value} className={button} aria-pressed={interval === value} onClick={() => setIntervalValue(value)}>{value}</button>)}
          </div>
        </div>
        <div className="h-[22rem]">
          {!symbol && <p className="p-4 text-sm text-[var(--color-text-muted)]">Pick a {venue.noun === "coins" ? "coin" : "stock"} to see its chart.</p>}
          {symbol && candles.isLoading && <p role="status" className="p-4 text-sm">Loading chart…</p>}
          {candles.isError && <p className="p-4 text-sm text-[var(--color-text-muted)]">{candles.error.message}.</p>}
          {candles.data && <CandleChart rows={candles.data.rows} />}
        </div>
        <p className="mt-2 text-xs text-[var(--color-text-muted)]">Chart: {candles.data ? `${title} ${candles.data.pair}` : `${title} candles`} ({interval}).</p>
      </section>
    </div>

    <section className={card} aria-label="Holdings">
      <h2 className="mb-2 font-semibold">Bot holdings</h2>
      <table className="w-full text-left text-sm">
        <thead><tr><th>Asset</th><th>Units</th><th>Cost</th><th>Value at bid</th><th>vs cost</th></tr></thead>
        <tbody>
          <tr><td>{quote} cash</td><td>{ledger?.cash ?? "—"}</td><td>—</td><td>{money(cash, quote)}</td><td>—</td></tr>
          {holdings.map((row) => <tr key={row.symbol}>
            <td>{row.symbol}</td><td>{row.units}</td><td>{money(row.cost, quote)}</td><td>{money(row.value, quote)}</td>
            <td>{row.pnl === null ? "—" : <Change value={String(row.pnl)} />}</td>
          </tr>)}
        </tbody>
      </table>
      {ledger && !holdings.length && <p className="mt-2 text-sm">The bot holds no positions.</p>}
      <p className="mt-2 text-xs text-[var(--color-text-muted)]">Value uses the live best bid, the price a sale would meet first. Equity is the bot's cash plus those values, in {quote}. Only the bot allocation is traded.</p>
      {valuation.data !== undefined && <div className="mt-2"><ExchangeResult value={valuation.data} /></div>}
    </section>

    <section className={card} aria-label="Account balances">
      <h2 className="mb-2 font-semibold">Account balances</h2>
      {!connected && <p className="text-sm">Not connected - open <a className="underline" href={settings}>Settings</a> to see the {title} account.</p>}
      {connected && accounts.isLoading && <p role="status" className="text-sm">Reading balances…</p>}
      {connected && accounts.isError && <p className="text-sm text-[var(--color-text-muted)]">Balances unavailable: {accounts.error.message}</p>}
      {connected && accounts.data !== undefined && (accountRows.length ? <table className="w-full text-left text-sm">
        <thead><tr><th>Asset</th><th>Balance</th><th>Available</th><th>Value at bid</th></tr></thead>
        <tbody>{accountRows.map((row) => <tr key={row.currency}>
          <td>{row.currency}</td><td>{row.balance}</td><td>{row.available}</td>
          <td>{money(accountValue(venue, row.currency, row.balance, known), quote)}</td>
        </tr>)}</tbody>
      </table> : <p className="text-sm">The {title} account holds no funded balances.</p>)}
      <p className="mt-2 text-xs text-[var(--color-text-muted)]">The whole {title} account, not only the bot allocation. An asset outside the listed markets shows no value.</p>
    </section>

    <section className={card} aria-label="Recent orders">
      <h2 className="mb-2 font-semibold">Recent orders</h2>
      {!connected && <p className="text-sm">Not connected - open <a className="underline" href={settings}>Settings</a> to see order history.</p>}
      {connected && history.isLoading && <p role="status" className="text-sm">Reading history…</p>}
      {connected && history.isError && <p className="text-sm text-[var(--color-text-muted)]">History unavailable: {history.error.message}</p>}
      {connected && history.data !== undefined && (historyRows.length ? <table className="w-full text-left text-sm">
        <thead><tr><th>Time</th><th>Pair</th><th>Side</th><th>Status</th><th>Filled</th><th>Avg price</th><th>Value</th><th>Fee</th></tr></thead>
        <tbody>{historyRows.map((row) => <tr key={row.id}>
          <td>{row.time === null ? "—" : new Date(row.time).toLocaleString()}</td><td>{row.pair}</td><td>{row.side}</td><td>{row.status}</td>
          <td>{row.quantity}</td><td>{row.average_price}</td><td>{row.value}</td><td>{row.fee}</td>
        </tr>)}</tbody>
      </table> : <p className="text-sm">No final orders in the history window.</p>)}
      {botOrders.length > 0 && <>
        <h3 className="mb-1 mt-3 text-sm font-medium">Bot ledger</h3>
        <table className="w-full text-left text-sm">
          <thead><tr><th>Time</th><th>Side</th><th>Asset</th><th>Status</th><th>Filled</th><th>Value</th><th>Fee</th></tr></thead>
          <tbody>{botOrders.map((order) => <tr key={`${order.order_id ?? order.client_oid}-${order.time}`}>
            <td>{new Date(order.time * 1000).toLocaleString()}</td><td>{order.side}</td><td>{order.symbol}</td><td>{order.status}</td>
            <td>{order.quantity}</td><td>{order.value} {quote}</td><td>{order.fee} {order.fee_currency ?? ""}</td>
          </tr>)}</tbody>
        </table>
      </>}
    </section>

    <ScorecardPanel base={base} queryKey={`${key}-scorecard`} venue={title} noun={venue.noun} />
  </div>;
}
