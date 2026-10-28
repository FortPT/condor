import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { limitsSummary } from "@/components/settings/cryptoExchangeLimits";
import { useCryptoExchangeStatus, type CryptoExchangeStatus } from "@/hooks/useCredentials";
import { apiFetch } from "@/lib/api";

const base = "/api/v1/crypto-exchange";
const QUOTES = ["USD", "USDT", "USDC", "EUR"] as const;
const input = "w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40";

type SyncResult = CryptoExchangeStatus & { skipped: string[] };

export function CryptoComExchange({ settings = false }: { settings?: boolean }) {
  const qc = useQueryClient();
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [environment, setEnvironment] = useState<"production" | "uat">("production");
  const [syncQuote, setSyncQuote] = useState("");
  const [result, setResult] = useState<unknown>(null);
  const [synced, setSynced] = useState<SyncResult | null>(null);
  const status = useCryptoExchangeStatus();
  const refresh = () => qc.invalidateQueries({ queryKey: ["crypto-exchange-status"] });
  const save = useMutation({
    mutationFn: () => apiFetch(base + "/credentials", {
      method: "PUT", body: JSON.stringify({ api_key: apiKey, api_secret: apiSecret, environment }),
    }),
    onSuccess: () => { setApiKey(""); setApiSecret(""); setSynced(null); refresh(); },
  });
  const disconnect = useMutation({
    mutationFn: () => apiFetch(base + "/credentials", { method: "DELETE" }),
    onSuccess: () => { setResult(null); setSynced(null); refresh(); },
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => apiFetch(base + "/enabled", { method: "PUT", body: JSON.stringify({ enabled }) }),
    onSettled: () => refresh(),
  });
  const sync = useMutation({
    mutationFn: (quote: string) => apiFetch<SyncResult>(base + "/sync", { method: "POST", body: JSON.stringify({ quote }) }),
    onSuccess: (next) => { setSynced(next); refresh(); },
    retry: false,
  });
  const inspect = useMutation({
    mutationFn: (path: string) => apiFetch(base + path),
    onSuccess: setResult,
  });
  const reconcile = useMutation({
    mutationFn: () => apiFetch(base + "/reconcile", { method: "POST" }),
    onSettled: () => refresh(),
    retry: false,
  });
  const error = status.error || save.error || disconnect.error || toggle.error || sync.error || inspect.error || reconcile.error;
  const data = status.data;
  const ledger = data?.ledger;
  const quote = ledger?.quote ?? "USD";
  const uncertain = !!ledger?.uncertain_order;
  const held = Object.keys(ledger?.positions ?? {});

  return <div className="mx-auto max-w-3xl space-y-5">
    <div>
      <h1 className="text-xl font-semibold">Crypto.com Exchange</h1>
      <p className="mt-1 text-sm text-[var(--color-text-muted)]">Spot trading on the Crypto.com Exchange order book through your Exchange API key. Claude and agent loops use this same account and its enforced allocation.</p>
    </div>
    {error && <p role="alert" className="text-sm text-[var(--color-red)]">{error.message}</p>}
    <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4">
      <h2 className="font-semibold">Bot allocation</h2>
      <p className="text-sm">{limitsSummary(data?.limits, quote)}</p>
      <p className="text-sm">Account: {data?.connected ? `API key saved (${data.environment})` : "not connected"} · Trading: {data?.enabled ? "enabled" : "paused"}</p>
      {ledger && (ledger.synced
        ? <p className="text-sm">Allocated cash: {ledger.cash} {quote} · Open positions: {held.length}{held.length > 0 && ` (${held.join(", ")})`}</p>
        : <p className="text-sm">The allocation has not been synced from the Exchange account yet. Sync it before enabling trading.</p>)}
      {uncertain && <p role="alert" className="text-sm text-[var(--color-red)]">An order needs reconciliation. Trading stays paused. Check the Exchange order history before any new submission.</p>}
      {uncertain && <button className={button} disabled={reconcile.isPending} onClick={() => reconcile.mutate()}>Reconcile from Exchange order history</button>}
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-sm">Quote currency
          <select className={input + " w-auto"} value={syncQuote || quote} onChange={(event) => setSyncQuote(event.target.value)}>
            {QUOTES.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        <button className={button} disabled={!data?.connected || !!data?.enabled || uncertain || sync.isPending} onClick={() => sync.mutate(syncQuote || quote)}>Sync allocation from Exchange account</button>
      </div>
      {!!data?.enabled && <p className="text-xs text-[var(--color-text-muted)]">Pause trading to sync the allocation again.</p>}
      {synced && <p role="status" className="text-sm">Allocation synced: cash {synced.ledger.cash} {synced.ledger.quote}
        {" · "}positions: {Object.entries(synced.ledger.positions).map(([symbol, position]) => `${symbol} ${position.units}`).join(", ") || "none"}
        {synced.skipped.length > 0 && <> · skipped (no {synced.ledger.quote} pair or price): {synced.skipped.join(", ")}</>}</p>}
      <div className="flex flex-wrap gap-2">
        <button className={button} disabled={!data?.connected || toggle.isPending || (!data?.enabled && (uncertain || !ledger?.synced))} onClick={() => toggle.mutate(!data?.enabled)}>{data?.enabled ? "Pause trading" : "Enable trading within allocation"}</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/accounts")}>Check connection & balances</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/history")}>Order history</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/valuation")}>Value bot allocation</button>
      </div>
      <p className="text-xs text-[var(--color-text-muted)]">Enabling execution does not start a loop or place an order. Account holdings outside the bot allocation are excluded. Orders are immediate-or-cancel limits, so a fast market can leave them partly filled or unfilled.</p>
    </section>
    {settings ? <form className="space-y-3 rounded-lg border border-[var(--color-border)] p-4" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <h2 className="font-semibold">Connect an Exchange API key</h2>
      <p className="text-sm">On the Crypto.com Exchange site open User Center → API and create a key with trading permission only (no withdrawal), restricted to this server's IP address. Use the uat environment with its own sandbox keys to try the whole path without real money. The key and secret are stored on this Condor host and never returned to the browser or model. Saving or replacing them pauses trading.</p>
      <label className="block text-sm">API key<input type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} className={input} required /></label>
      <label className="block text-sm">API secret<input type="password" autoComplete="off" value={apiSecret} onChange={(event) => setApiSecret(event.target.value)} className={input} required /></label>
      <label className="block text-sm">Environment
        <select className={input} value={environment} onChange={(event) => setEnvironment(event.target.value as "production" | "uat")}>
          <option value="production">production</option>
          <option value="uat">uat (sandbox)</option>
        </select>
      </label>
      <div className="flex flex-wrap items-center gap-3">
        <button className={button} disabled={save.isPending}>Save API key</button>
        <button type="button" className={button} disabled={!data?.connected || disconnect.isPending} onClick={() => disconnect.mutate()}>Disconnect</button>
        <Link className="text-sm underline" to="/trade?workspace=crypto-exchange">Open trading</Link>
      </div>
    </form> : !data?.connected && <Link className="text-sm underline" to="/settings?tab=crypto-exchange">Connect Crypto.com Exchange</Link>}
    {result !== null && <ExchangeResult value={result} />}
  </div>;
}

type Account = { currency: string; balance: string; available: string };
type OrderSummary = {
  order_id?: string; instrument_name?: string; side?: string; status?: string; quantity?: string; limit_price?: string;
  avg_price?: string; cumulative_quantity?: string; cumulative_value?: string; cumulative_fee?: string; fee_instrument_name?: string;
};

function OrderTable({ orders }: { orders: OrderSummary[] }) {
  return <div className="max-h-72 overflow-auto"><table className="w-full text-left"><thead><tr>
    <th>Pair</th><th>Side</th><th>Status</th><th>Filled</th><th>Average price</th><th>Value</th><th>Fee</th>
  </tr></thead><tbody>
    {orders.map((order, index) => <tr key={`${order.order_id}-${index}`}>
      <td>{order.instrument_name}</td><td>{order.side}</td><td>{order.status}</td>
      <td>{order.cumulative_quantity ?? "—"} / {order.quantity ?? "—"}</td><td>{order.avg_price ?? "—"}</td><td>{order.cumulative_value ?? "—"}</td>
      <td>{order.cumulative_fee ?? "—"} {order.fee_instrument_name ?? ""}</td>
    </tr>)}
  </tbody></table></div>;
}

/** Whatever an inspection or order call returned, shown without rounding the provider's strings. */
export function ExchangeResult({ value }: { value: unknown }) {
  const data = value as {
    equity?: string; quote?: string; new_position_limit?: string; minimum_cash_reserve?: string;
    accounts?: Account[]; order?: OrderSummary;
  };
  if (data.equity !== undefined) return <section className="rounded-lg border border-[var(--color-border)] p-4 text-sm">
    <h2 className="mb-2 font-semibold">Bot allocation at current bid prices</h2>
    <p>Total: {data.equity} {data.quote}</p>
    <p>Cash reserve: {data.minimum_cash_reserve} {data.quote}</p>
    <p>Maximum new position: {data.new_position_limit} {data.quote}</p>
  </section>;
  if (data.accounts) return <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4 text-sm">
    <h2 className="font-semibold">Exchange account balances</h2>
    <p className="text-xs text-[var(--color-text-muted)]">Whole account holdings; only the bot allocation may be traded.</p>
    {data.accounts.length ? <div className="max-h-64 overflow-auto"><table className="w-full text-left"><thead><tr><th>Currency</th><th>Balance</th><th>Available</th></tr></thead><tbody>
      {data.accounts.map((row) => <tr key={row.currency}><td>{row.currency}</td><td>{row.balance}</td><td>{row.available}</td></tr>)}
    </tbody></table></div> : <p>No holdings</p>}
  </section>;
  if (data.order) return <section className="space-y-2 rounded-lg border border-[var(--color-border)] p-4 text-sm">
    <h2 className="font-semibold">Order result</h2>
    <OrderTable orders={[data.order]} />
  </section>;
  const orders: OrderSummary[] = Array.isArray(value) ? value : [];
  return <section className="overflow-auto rounded-lg border border-[var(--color-border)] p-4 text-sm">
    <h2 className="mb-2 font-semibold">Recent Exchange orders</h2>
    {orders.length ? <OrderTable orders={orders} /> : <p>No recent orders</p>}
  </section>;
}
