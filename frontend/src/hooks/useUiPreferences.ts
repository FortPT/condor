import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api";

/**
 * The preference that hides the Hummingbot-only pages (Bots, DEX, the Hummingbot terminal on Trade, and the
 * Bots and Executors cards on Portfolio) for an install that trades only through Condor's own venues.
 * Hummingbot itself keeps running behind the Solana wallet, market data and the value history: only the
 * pages that drive Hummingbot bots go. Stored server-side in the user's preferences, so every device agrees.
 */
export const HIDE_HUMMINGBOT_PAGES = "hide_hummingbot_pages";

const PREFERENCES_KEY = ["ui-preferences"] as const;
type Preferences = Record<string, unknown>;

export function useHideHummingbot() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: PREFERENCES_KEY,
    queryFn: () => apiFetch<Preferences>("/api/v1/auth/preferences"),
    staleTime: 60_000,
    retry: false,
  });
  const save = useMutation({
    mutationFn: (hide: boolean) =>
      apiFetch<Preferences>("/api/v1/auth/preferences", {
        method: "PATCH",
        body: JSON.stringify({ updates: { [HIDE_HUMMINGBOT_PAGES]: hide } }),
      }),
    onSuccess: (data) => queryClient.setQueryData(PREFERENCES_KEY, data),
  });
  return {
    /** True only when the preference was read and is on: while it loads, or if it cannot be read, nothing is hidden. */
    hidden: query.data?.[HIDE_HUMMINGBOT_PAGES] === true,
    loading: query.isLoading,
    setHidden: save.mutate,
    saving: save.isPending,
    failed: save.isError,
  };
}
