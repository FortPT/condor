import { Navigate, useSearchParams } from "react-router-dom";

import { ConnectKeysOverlay } from "@/components/ConnectKeysOverlay";
import { ExchangeTrade } from "@/components/trade/ExchangeTrade";
import { NativeFlowsPanel } from "@/components/trade/NativeFlowsPanel";
import { NativeVenueTrade } from "@/components/trade/NativeVenueTrade";
import { SolanaTrade } from "@/components/trade/SolanaTrade";
import { flowVenueKey } from "@/components/trade/nativeFlows";
import { NATIVE_VENUES, isNativeVenueKey, type NativeVenueKey } from "@/components/trade/nativeVenue";
import { useCredentials, useCryptoExchangeStatus } from "@/hooks/useCredentials";
import { useServer } from "@/hooks/useServer";
import { CreateExecutor } from "@/pages/CreateExecutor";

export function Trade() {
  const [params, setParams] = useSearchParams();
  const { server } = useServer();
  const { hasKeys, credentials, isLoading: keysLoading } = useCredentials();
  const status = useCryptoExchangeStatus();
  // `workspace=exchange` has always meant the Hummingbot exchange workspace; `app` is the retired App link.
  const workspace = params.get("workspace") === "app" ? "crypto-exchange" : params.get("workspace");
  const explicitExchange = workspace === "hummingbot" || workspace === "exchange" ||
    params.has("type") || params.has("connector") || params.has("pair");
  // Revolut X and Trading 212 are only ever opened explicitly: they never become the default.
  const nativeWorkspace = isNativeVenueKey(workspace) ? workspace : null;
  // The Solana wallet is read-only here too: the Gateway signs for the loop, never from this page.
  const solanaWorkspace = workspace === "solana";
  const cryptoWorkspace = !nativeWorkspace && !solanaWorkspace && (workspace === "crypto-exchange" ||
    (!explicitExchange && !!status.data?.connected && credentials.length === 0));
  const loading = !workspace && !explicitExchange && (keysLoading || status.isLoading);

  // CreateExecutor consumes connector/pair; retain the account choice afterward.
  if (!workspace && !params.has("type") && (params.has("connector") || params.has("pair"))) {
    const nextParams = new URLSearchParams(params);
    nextParams.set("workspace", "hummingbot");
    return <Navigate to={{ pathname: "/trade", search: `?${nextParams}` }} replace />;
  }

  function selectWorkspace(next: "crypto-exchange" | "hummingbot" | "solana" | NativeVenueKey) {
    const nextParams = new URLSearchParams(params);
    nextParams.set("workspace", next);
    if (next !== "hummingbot") {
      nextParams.delete("type");
      nextParams.delete("connector");
      nextParams.delete("pair");
    }
    setParams(nextParams);
  }

  return <div className="-m-6 flex h-[calc(100%+3rem)] flex-col">
    <div className="flex shrink-0 gap-2 border-b border-[var(--color-border)] p-3" role="group" aria-label="Trading account">
      <button className="rounded border border-[var(--color-border)] px-3 py-2 text-sm aria-pressed:bg-[var(--color-surface-hover)]" aria-pressed={!loading && cryptoWorkspace} onClick={() => selectWorkspace("crypto-exchange")}>Crypto.com</button>
      {Object.values(NATIVE_VENUES).map((venue) =>
        <button key={venue.key} className="rounded border border-[var(--color-border)] px-3 py-2 text-sm aria-pressed:bg-[var(--color-surface-hover)]" aria-pressed={!loading && nativeWorkspace === venue.key} onClick={() => selectWorkspace(venue.key)}>{venue.title}</button>)}
      <button className="rounded border border-[var(--color-border)] px-3 py-2 text-sm aria-pressed:bg-[var(--color-surface-hover)]" aria-pressed={!loading && solanaWorkspace} onClick={() => selectWorkspace("solana")}>Solana</button>
      <button className="rounded border border-[var(--color-border)] px-3 py-2 text-sm aria-pressed:bg-[var(--color-surface-hover)]" aria-pressed={!loading && !cryptoWorkspace && !nativeWorkspace && !solanaWorkspace} onClick={() => selectWorkspace("hummingbot")}>Hummingbot</button>
    </div>
    {loading ? <p role="status" className="p-6 text-sm">Loading trading accounts…</p> :
      solanaWorkspace ? <div className="min-h-0 flex-1 space-y-4 overflow-auto p-6"><SolanaTrade /></div> :
      nativeWorkspace ? <div className="min-h-0 flex-1 space-y-4 overflow-auto p-6">
        <NativeVenueTrade venue={NATIVE_VENUES[nativeWorkspace]} />
        <NativeFlowsPanel venue={flowVenueKey(nativeWorkspace)} title={NATIVE_VENUES[nativeWorkspace].title} defaultCurrency={NATIVE_VENUES[nativeWorkspace].quote} />
      </div> :
      cryptoWorkspace ? <div className="min-h-0 flex-1 space-y-4 overflow-auto p-6">
        <ExchangeTrade />
        <NativeFlowsPanel venue="crypto_com_exchange" title="Crypto.com" defaultCurrency="USD" />
      </div> :
        <div className="relative min-h-0 flex-1 p-6">
          <CreateExecutor />
          {server && !keysLoading && !hasKeys && <ConnectKeysOverlay />}
        </div>}
  </div>;
}
