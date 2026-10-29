import { useQuery } from "@tanstack/react-query";

import { useServer } from "@/hooks/useServer";
import { api, apiFetch } from "@/lib/api";
import { credentialsQuery, gatewayWalletsQuery } from "@/lib/queryClient";
import type { CryptoExchangeLimits } from "@/components/settings/cryptoExchangeLimits";

export function useCredentials() {
  const { server } = useServer();

  const { data, isLoading: credsLoading } = useQuery({
    ...credentialsQuery(server),
    queryFn: () => api.getCredentials(server!),
    enabled: !!server,
  });

  // A Gateway wallet (Solana/Ethereum) unlocks the portfolio just like a CEX key does.
  // Errors (e.g. Gateway not running) simply count as "no wallets".
  const { data: walletsData, isLoading: walletsLoading } = useQuery({
    ...gatewayWalletsQuery(server),
    queryFn: () => api.getGatewayWallets(server!),
    enabled: !!server,
    retry: false,
  });

  const credentials = data?.credentials ?? [];
  const hasWallets = (walletsData?.wallets ?? []).some(
    (g) => (g.walletAddresses ?? []).length > 0,
  );
  const hasKeys = credentials.length > 0 || hasWallets;
  const isLoading = credsLoading || walletsLoading;

  return { hasKeys, isLoading, credentials };
}

export type ExchangeLedger = {
  quote: string;
  cash: string;
  positions: Record<string, { units: string; cost: string }>;
  uncertain_order: unknown;
  enabled?: boolean;
  orders?: {
    time: number; side: string; symbol: string; order_id?: string | null; client_oid?: string | null;
    status: string; quantity: string; value: string; fee: string; fee_currency?: string | null;
  }[];
  floor?: string;
  synced: boolean;
};

export type CryptoExchangeStatus = {
  connected: boolean;
  environment: "production" | "uat";
  enabled: boolean;
  ledger: ExchangeLedger;
  limits?: CryptoExchangeLimits;
};

export type KrakenXStocksStatus = {
  connected: boolean;
  venue: string;
  quote: string;
  enabled: boolean;
  ledger: ExchangeLedger;
  market: { open: boolean; new_york_time: string; minutes_to_close: number; note?: string };
  limits?: {
    floor?: string;
    reserve_fraction?: string;
    position_fraction?: string;
    max_positions?: number;
    guards_apply_from?: string;
    max_asset_fraction_below_threshold?: string;
    default_slippage_pct?: string;
    pairs?: string;
    session?: string;
  };
};

export type RevolutXStatus = {
  connected: boolean;
  has_signing_key: boolean;
  has_api_key: boolean;
  /** The PUBLIC half of the signing key, for the owner to register at Revolut X. */
  public_key: string | null;
  venue: string;
  quote: string;
  enabled: boolean;
  ledger: ExchangeLedger & { observed_taker_fee_pct?: string | null };
  limits?: {
    floor?: string;
    reserve_fraction?: string;
    position_fraction?: string;
    max_positions?: number;
    guards_apply_from?: string;
    max_asset_fraction_below_threshold?: string;
    default_slippage_pct?: string;
    pairs?: string;
    session?: string;
    taker_fee_pct_estimate?: string;
  };
};

export type Trading212Status = {
  /** API credentials are saved (the key itself is never returned). */
  connected: boolean;
  has_secret: boolean;
  venue: string;
  quote: string;
  /** The account currency Trading 212 reported, or null before the first successful read. */
  account_currency: string | null;
  enabled: boolean;
  ledger: ExchangeLedger & { account_currency?: string | null };
  market: { open: boolean; new_york_time: string; minutes_to_close: number; note?: string };
  limits?: {
    floor?: string;
    reserve_fraction?: string;
    position_fraction?: string;
    max_positions?: number;
    guards_apply_from?: string;
    max_asset_fraction_below_threshold?: string;
    default_slippage_pct?: string;
    pairs?: string;
    session?: string;
    fx_fee_pct_each_way?: string;
    max_buy_round_trip_pct?: string;
    min_order_eur?: string;
  };
};

export function useTrading212Status() {
  return useQuery({
    queryKey: ["trading212-status"],
    queryFn: () => apiFetch<Trading212Status>("/api/v1/trading212/status"),
    refetchInterval: 10000,
    retry: false,
  });
}

export function useRevolutXStatus() {
  return useQuery({
    queryKey: ["revolut-x-status"],
    queryFn: () => apiFetch<RevolutXStatus>("/api/v1/revolut-x/status"),
    refetchInterval: 10000,
    retry: false,
  });
}

export function useKrakenXStocksStatus() {
  return useQuery({
    queryKey: ["kraken-xstocks-status"],
    queryFn: () => apiFetch<KrakenXStocksStatus>("/api/v1/kraken-xstocks/status"),
    refetchInterval: 10000,
    retry: false,
  });
}

export function useCryptoExchangeStatus() {
  return useQuery({
    queryKey: ["crypto-exchange-status"],
    queryFn: () => apiFetch<CryptoExchangeStatus>("/api/v1/crypto-exchange/status"),
    refetchInterval: 10000,
    retry: false,
  });
}
