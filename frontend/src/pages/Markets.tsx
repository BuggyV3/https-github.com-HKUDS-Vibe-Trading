import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Info, LineChart, Loader2, RefreshCw } from "lucide-react";
import {
  api,
  type OptionChainResponse,
  type ScreenerMarket,
  type ScreenerResponse,
  type ScreenerRow,
  type ScreenerSort,
} from "@/lib/api";
import { ScreenerPanel } from "@/components/markets/ScreenerPanel";
import { OptionsPanel } from "@/components/markets/OptionsPanel";
import { secondsSince } from "@/lib/marketFormat";
import { cn } from "@/lib/utils";

const MARKETS: ScreenerMarket[] = ["us", "a", "hk"];
const SORTS: ScreenerSort[] = ["amount", "change_pct", "volume", "turnover"];
const TOP_N_CHOICES = [20, 40, 60, 100] as const;

/** Auto-refresh choices in seconds; 0 means off. */
const REFRESH_CHOICES = [0, 15, 30, 60] as const;

/** Past this many seconds without a successful fetch the panel is marked stale. */
const STALE_AFTER_SECONDS = 90;

/** Option chains are US-listed only — the Yahoo chain endpoint has no others. */
const OPTIONS_MARKET: ScreenerMarket = "us";

/**
 * Live market-discovery dashboard.
 *
 * Two coupled views: a full-market screener that ranks what is trading, and an
 * option-chain explorer for whatever the screener (or the search box) points at.
 * Both read the same throttled providers the agent's own tools use, and both
 * report the timestamp of the underlying fetch rather than of the render, so a
 * panel that has stopped updating says so instead of looking live.
 */
export function Markets() {
  const { t } = useTranslation();

  const [market, setMarket] = useState<ScreenerMarket>("us");
  const [sortBy, setSortBy] = useState<ScreenerSort>("amount");
  const [topN, setTopN] = useState<number>(40);
  const [refreshSeconds, setRefreshSeconds] = useState<number>(0);

  const [screener, setScreener] = useState<ScreenerResponse | null>(null);
  const [screenerLoading, setScreenerLoading] = useState(true);
  const [screenerError, setScreenerError] = useState<string | null>(null);

  const [ticker, setTicker] = useState<string | null>(null);
  const [tickerDraft, setTickerDraft] = useState("");
  const [expiration, setExpiration] = useState<number | null>(null);
  const [chain, setChain] = useState<OptionChainResponse | null>(null);
  const [chainLoading, setChainLoading] = useState(false);
  const [chainError, setChainError] = useState<string | null>(null);

  const [now, setNow] = useState(() => Date.now());
  const screenerAbort = useRef<AbortController | null>(null);
  const chainAbort = useRef<AbortController | null>(null);

  const loadScreener = useCallback(async () => {
    screenerAbort.current?.abort();
    const controller = new AbortController();
    screenerAbort.current = controller;
    setScreenerLoading(true);
    try {
      const result = await api.getMarketScreener(market, sortBy, topN, controller.signal);
      if (controller.signal.aborted) return;
      setScreener(result);
      setScreenerError(null);
    } catch (error) {
      if (controller.signal.aborted) return;
      // The previous rows stay on screen behind the error banner: a failed
      // refresh should not blank a dashboard the user is reading, and the
      // "as of" stamp already shows how old they are.
      setScreenerError(error instanceof Error ? error.message : t("markets.screenerError"));
    } finally {
      if (!controller.signal.aborted) setScreenerLoading(false);
    }
  }, [market, sortBy, topN, t]);

  const loadChain = useCallback(
    async (symbol: string, expiry: number | null) => {
      chainAbort.current?.abort();
      const controller = new AbortController();
      chainAbort.current = controller;
      setChainLoading(true);
      try {
        const result = await api.getOptionChain(symbol, expiry, undefined, controller.signal);
        if (controller.signal.aborted) return;
        setChain(result);
        setExpiration(result.expiration);
        setChainError(null);
      } catch (error) {
        if (controller.signal.aborted) return;
        setChain(null);
        setChainError(error instanceof Error ? error.message : t("markets.chainError"));
      } finally {
        if (!controller.signal.aborted) setChainLoading(false);
      }
    },
    [t],
  );

  useEffect(() => {
    void loadScreener();
    return () => screenerAbort.current?.abort();
  }, [loadScreener]);

  useEffect(() => () => chainAbort.current?.abort(), []);

  // Auto-refresh. Both panels refresh together so their "as of" stamps stay
  // comparable; the chain is skipped while none is open.
  useEffect(() => {
    if (refreshSeconds === 0) return;
    const timer = window.setInterval(() => {
      void loadScreener();
      if (ticker) void loadChain(ticker, expiration);
    }, refreshSeconds * 1000);
    return () => window.clearInterval(timer);
  }, [refreshSeconds, loadScreener, loadChain, ticker, expiration]);

  // Drives the "updated Ns ago" counter without re-fetching anything.
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const selectSymbol = (symbol: string) => {
    const normalized = symbol.trim().toUpperCase();
    if (!normalized) return;
    setTicker(normalized);
    setTickerDraft(normalized);
    setExpiration(null);
    void loadChain(normalized, null);
  };

  const onSelectRow = (row: ScreenerRow) => selectSymbol(row.code);

  const onExpirationChange = (epoch: number) => {
    setExpiration(epoch);
    if (ticker) void loadChain(ticker, epoch);
  };

  const age = useMemo(() => secondsSince(screener?.as_of, now), [screener, now]);
  const stale = age != null && age > STALE_AFTER_SECONDS;
  const optionsAvailable = market === OPTIONS_MARKET;

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-6 p-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <LineChart className="h-6 w-6 text-primary" />
          <div>
            <h1 className="text-2xl font-bold">{t("markets.title")}</h1>
            <p className="text-sm text-muted-foreground">{t("markets.subtitle")}</p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <span
            className={cn("text-xs", stale ? "text-warning" : "text-muted-foreground")}
            aria-live="polite"
          >
            {age == null
              ? t("markets.neverUpdated")
              : stale
                ? t("markets.staleAge", { seconds: age })
                : t("markets.updatedAge", { seconds: age })}
          </span>
          <button
            type="button"
            onClick={() => {
              void loadScreener();
              if (ticker) void loadChain(ticker, expiration);
            }}
            disabled={screenerLoading}
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:border-primary disabled:opacity-50"
          >
            {screenerLoading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <RefreshCw className="h-4 w-4" />
            )}
            {t("markets.refresh")}
          </button>
        </div>
      </div>

      {/* Screener controls */}
      <div className="flex flex-wrap items-end gap-4 rounded-lg border p-4">
        <ControlGroup label={t("markets.market")}>
          {MARKETS.map((option) => (
            <Choice
              key={option}
              active={market === option}
              onClick={() => setMarket(option)}
              label={t(`markets.market_${option}`)}
            />
          ))}
        </ControlGroup>

        <ControlGroup label={t("markets.rankBy")}>
          {SORTS.map((option) => (
            <Choice
              key={option}
              active={sortBy === option}
              onClick={() => setSortBy(option)}
              label={t(`markets.sort_${option}`)}
            />
          ))}
        </ControlGroup>

        <ControlGroup label={t("markets.depth")}>
          {TOP_N_CHOICES.map((option) => (
            <Choice
              key={option}
              active={topN === option}
              onClick={() => setTopN(option)}
              label={String(option)}
            />
          ))}
        </ControlGroup>

        <ControlGroup label={t("markets.autoRefresh")}>
          {REFRESH_CHOICES.map((option) => (
            <Choice
              key={option}
              active={refreshSeconds === option}
              onClick={() => setRefreshSeconds(option)}
              label={option === 0 ? t("markets.refreshOff") : `${option}s`}
            />
          ))}
        </ControlGroup>
      </div>

      {screenerError && (
        <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/5 p-3 text-sm text-danger">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{screenerError}</span>
        </div>
      )}

      <ScreenerPanel
        data={screener}
        loading={screenerLoading}
        selected={ticker}
        onSelect={onSelectRow}
        selectable={optionsAvailable}
      />

      {/* Options explorer */}
      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">{t("markets.optionsTitle")}</h2>
            <p className="text-sm text-muted-foreground">
              {optionsAvailable ? t("markets.optionsHint") : t("markets.optionsUsOnly")}
            </p>
          </div>
          <form
            className="flex items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              selectSymbol(tickerDraft);
            }}
          >
            <input
              type="text"
              value={tickerDraft}
              onChange={(event) => setTickerDraft(event.target.value)}
              placeholder={t("markets.tickerPlaceholder")}
              aria-label={t("markets.tickerLabel")}
              className="w-36 rounded-md border bg-background px-3 py-1.5 font-mono text-sm uppercase"
            />
            <button
              type="submit"
              disabled={!tickerDraft.trim() || chainLoading}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {t("markets.loadChain")}
            </button>
          </form>
        </div>

        <OptionsPanel
          chain={chain}
          loading={chainLoading}
          error={chainError}
          ticker={ticker}
          onExpirationChange={onExpirationChange}
        />
      </section>

      {/* How the scores are built */}
      <details className="rounded-lg border p-4 text-sm">
        <summary className="cursor-pointer font-medium">
          <Info className="me-1.5 inline h-4 w-4" />
          {t("markets.methodologyTitle")}
        </summary>
        <div className="mt-3 space-y-2 text-muted-foreground">
          <p>{t("markets.methodologyFlow")}</p>
          <p>{t("markets.methodologyLiquidity")}</p>
          <p>{t("markets.methodologyGreeks")}</p>
          <p>{t("markets.methodologyProviders")}</p>
          <p>{t("markets.methodologyMissing")}</p>
          <p className="font-medium text-foreground">{t("markets.methodologyDisclaimer")}</p>
          <p>
            {t("markets.sources", {
              screener: screener?.source ?? "eastmoney",
              options: chain?.source ?? "yahoo",
            })}
          </p>
        </div>
      </details>
    </div>
  );
}

function ControlGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <div className="flex flex-wrap gap-1.5" role="group" aria-label={label}>
        {children}
      </div>
    </div>
  );
}

function Choice({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "rounded border px-3 py-1.5 text-sm transition-colors",
        active
          ? "bg-primary text-primary-foreground"
          : "border-muted-foreground/30 hover:border-primary",
      )}
    >
      {label}
    </button>
  );
}
