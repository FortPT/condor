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

export function useCryptoExchangeStatus() {
  return useQuery({
    queryKey: ["crypto-exchange-status"],
    queryFn: () => apiFetch<CryptoExchangeStatus>("/api/v1/crypto-exchange/status"),
    refetchInterval: 10000,
    retry: false,
  });
}
