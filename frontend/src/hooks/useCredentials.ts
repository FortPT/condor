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
    /** Revolut X: the fills' real value, and whether the fee was read from the order or estimated. */
    fills_value?: string | null; fee_source?: string | null;
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
  ledger: ExchangeLedger & { account_currency?: string | null; excluded_tickers?: string[] };
  /** The owner's own holdings the bot never adopts or trades (plain symbols). */
  excluded_tickers?: string[];
  /** The last bot-ledger-against-account check (memory only on the server; null until one ran). */
  account_check?: {
    ledger_matches_account: boolean;
    mismatch: { kind: string; message: string }[];
    outside_allocation: { ticker: string; symbol: string; units: string; excluded: boolean }[];
  } | null;
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

export function useCryptoExchangeStatus() {
  return useQuery({
    queryKey: ["crypto-exchange-status"],
    queryFn: () => apiFetch<CryptoExchangeStatus>("/api/v1/crypto-exchange/status"),
    refetchInterval: 10000,
    retry: false,
  });
}
