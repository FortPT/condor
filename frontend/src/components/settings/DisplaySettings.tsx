import { useHideHummingbot } from "@/hooks/useUiPreferences";

/**
 * Which pages the navigation offers. An install that trades through Condor's own venues (Crypto.com,
 * Revolut X, Trading 212, the Solana wallet) has no Hummingbot bots to look at: the Bots and DEX pages, the
 * Hummingbot terminal on Trade and the Bots and Executors cards on Portfolio only show zeros.
 */
export function DisplaySettings() {
  const { hidden, loading, setHidden, saving, failed } = useHideHummingbot();
  return (
    <div className="space-y-4">
      <section>
        <h3 className="mb-1 text-sm font-semibold text-[var(--color-text)]">Display</h3>
        <p className="text-xs leading-relaxed text-[var(--color-text-muted)]">
          Choose what the navigation shows. This is saved with your account, so every device agrees.
        </p>
      </section>
      <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={hidden}
          disabled={loading || saving}
          onChange={(event) => setHidden(event.target.checked)}
        />
        <span>
          <span className="block text-sm font-medium text-[var(--color-text)]">Hide the Hummingbot pages</span>
          <span className="mt-1 block text-xs leading-relaxed text-[var(--color-text-muted)]">
            Hides Bots, DEX, the Hummingbot terminal on Trade, and the Bots and Executors cards on Portfolio.
            Hummingbot keeps running behind the Solana wallet, market data and the value history; only these
            pages go. The Hummingbot API and Gateway settings stay in Settings.
          </span>
        </span>
      </label>
      {failed && <p role="alert" className="text-xs text-[var(--color-red)]">Could not save the setting.</p>}
    </div>
  );
}
