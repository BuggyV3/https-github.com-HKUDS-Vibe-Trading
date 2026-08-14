import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Loader2 } from "lucide-react";
import type { OptionChainResponse, OptionContract } from "@/lib/api";
import { IvSmileChart } from "@/components/charts/IvSmileChart";
import {
  compareNullable,
  fmtCompact,
  fmtExpiry,
  fmtGreek,
  fmtInt,
  fmtPct,
  fmtPrice,
  fmtScore,
  scoreColor,
  NO_DATA,
} from "@/lib/marketFormat";
import { cn } from "@/lib/utils";

type SideFilter = "both" | "call" | "put";
type ChainSort = "liquidity_score" | "open_interest" | "volume" | "spread_pct" | "strike";

interface Props {
  chain: OptionChainResponse | null;
  loading: boolean;
  error: string | null;
  /** Symbol the explorer is pointed at, even while its chain is still loading. */
  ticker: string | null;
  onExpirationChange: (expiration: number) => void;
}

/** Moneyness bands offered by the filter, as a fraction away from spot. */
const MONEYNESS_BANDS = [0.05, 0.1, 0.25, 1] as const;

/**
 * Rank contracts for the "most tradeable" list.
 *
 * Ordering is by the backend's liquidity score, which blends quoted spread,
 * open interest and volume. It says a contract is easy to get in and out of —
 * nothing about whether it is worth owning.
 */
function bestContracts(rows: OptionContract[], limit: number): OptionContract[] {
  return [...rows]
    .filter((row) => row.liquidity_score != null)
    .sort((a, b) => compareNullable(a.liquidity_score, b.liquidity_score, "desc"))
    .slice(0, limit);
}

/**
 * Option-chain explorer: filters, a ranked shortlist, the skew chart and the
 * full ladder with greeks.
 *
 * All filtering and sorting is local to the already-fetched expiration, so the
 * only interaction that costs a provider request is changing the expiry.
 */
export function OptionsPanel({ chain, loading, error, ticker, onExpirationChange }: Props) {
  const { t } = useTranslation();
  const [side, setSide] = useState<SideFilter>("both");
  const [band, setBand] = useState<number>(0.1);
  const [minOi, setMinOi] = useState(0);
  const [maxSpread, setMaxSpread] = useState(0.25);
  const [sortKey, setSortKey] = useState<ChainSort>("liquidity_score");

  const contracts = useMemo(() => {
    if (!chain) return [];
    const pool =
      side === "call" ? chain.calls : side === "put" ? chain.puts : [...chain.calls, ...chain.puts];
    return pool.filter((row) => {
      // A band filter is meaningless without a spot price, so it is skipped
      // rather than silently emptying the table when the quote is missing.
      if (band < 1 && row.moneyness != null && Math.abs(row.moneyness) > band) return false;
      if (minOi > 0 && (row.open_interest == null || row.open_interest < minOi)) return false;
      // Unquoted contracts have no spread to test; they are dropped only once
      // the user tightens the filter below its wide-open default.
      if (maxSpread < 1 && (row.spread_pct == null || row.spread_pct > maxSpread)) return false;
      return true;
    });
  }, [chain, side, band, minOi, maxSpread]);

  const sorted = useMemo(() => {
    const direction = sortKey === "spread_pct" || sortKey === "strike" ? "asc" : "desc";
    return [...contracts].sort((a, b) => compareNullable(a[sortKey], b[sortKey], direction));
  }, [contracts, sortKey]);

  const shortlist = useMemo(() => bestContracts(contracts, 5), [contracts]);

  if (!ticker) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
        {t("markets.optionsIdle")}
      </div>
    );
  }

  if (loading && !chain) {
    return (
      <div className="flex items-center justify-center gap-2 rounded-lg border p-8 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t("markets.optionsLoading", { ticker })}
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/5 p-4 text-sm text-danger">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        <span>{error}</span>
      </div>
    );
  }

  if (!chain) return null;

  const { summary } = chain;

  return (
    <div className="flex flex-col gap-4">
      {/* Chain-level statistics */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label={t("markets.statSpot")} value={fmtPrice(chain.spot)} mono />
        <Stat
          label={t("markets.statAtmIv")}
          value={summary.atm_iv != null ? fmtPct(summary.atm_iv, 1, false) : NO_DATA}
          mono
        />
        <Stat
          label={t("markets.statPcOi")}
          value={summary.put_call_oi_ratio != null ? summary.put_call_oi_ratio.toFixed(2) : NO_DATA}
          mono
        />
        <Stat
          label={t("markets.statPcVolume")}
          value={
            summary.put_call_volume_ratio != null
              ? summary.put_call_volume_ratio.toFixed(2)
              : NO_DATA
          }
          mono
        />
        <Stat
          label={t("markets.statMedianSpread")}
          value={fmtPct(summary.median_spread_pct, 1, false)}
          mono
        />
        <Stat
          label={t("markets.statDte")}
          value={chain.days_to_expiry != null ? chain.days_to_expiry.toFixed(1) : NO_DATA}
          mono
        />
      </div>

      {/* Expiration ladder */}
      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-medium">{t("markets.expiration")}</span>
        <div className="flex flex-wrap gap-1.5">
          {chain.expirations.slice(0, 14).map((epoch) => (
            <button
              key={epoch}
              type="button"
              onClick={() => onExpirationChange(epoch)}
              aria-pressed={chain.expiration === epoch}
              disabled={loading}
              className={cn(
                "rounded border px-2.5 py-1 font-mono text-xs transition-colors disabled:opacity-50",
                chain.expiration === epoch
                  ? "bg-primary text-primary-foreground"
                  : "border-muted-foreground/30 hover:border-primary",
              )}
            >
              {fmtExpiry(epoch)}
            </button>
          ))}
        </div>
      </div>

      {/* Contract filters */}
      <div className="flex flex-wrap items-end gap-4 rounded-lg border p-3">
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">{t("markets.side")}</span>
          <div className="flex gap-1.5">
            {(["both", "call", "put"] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setSide(option)}
                aria-pressed={side === option}
                className={cn(
                  "rounded border px-3 py-1.5 text-sm transition-colors",
                  side === option
                    ? "bg-primary text-primary-foreground"
                    : "border-muted-foreground/30 hover:border-primary",
                )}
              >
                {t(`markets.side_${option}`)}
              </button>
            ))}
          </div>
        </div>

        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">
            {t("markets.moneynessBand")}
          </span>
          <div className="flex gap-1.5">
            {MONEYNESS_BANDS.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setBand(option)}
                aria-pressed={band === option}
                className={cn(
                  "rounded border px-3 py-1.5 text-sm transition-colors",
                  band === option
                    ? "bg-primary text-primary-foreground"
                    : "border-muted-foreground/30 hover:border-primary",
                )}
              >
                {option === 1 ? t("markets.bandAll") : `±${Math.round(option * 100)}%`}
              </button>
            ))}
          </div>
        </div>

        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-xs font-medium text-muted-foreground">
            {t("markets.minOpenInterest", { value: fmtInt(minOi) })}
          </span>
          <input
            type="range"
            min={0}
            max={5000}
            step={100}
            value={minOi}
            onChange={(event) => setMinOi(Number(event.target.value))}
            aria-label={t("markets.minOpenInterestLabel")}
            className="w-36"
          />
        </label>

        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-xs font-medium text-muted-foreground">
            {t("markets.maxSpread", { value: (maxSpread * 100).toFixed(0) })}
          </span>
          <input
            type="range"
            min={0.01}
            max={1}
            step={0.01}
            value={maxSpread}
            onChange={(event) => setMaxSpread(Number(event.target.value))}
            aria-label={t("markets.maxSpreadLabel")}
            className="w-36"
          />
        </label>

        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-xs font-medium text-muted-foreground">{t("markets.sortBy")}</span>
          <select
            value={sortKey}
            onChange={(event) => setSortKey(event.target.value as ChainSort)}
            className="rounded-md border bg-background px-2 py-1.5 text-sm"
          >
            <option value="liquidity_score">{t("markets.sortLiquidity")}</option>
            <option value="open_interest">{t("markets.sortOpenInterest")}</option>
            <option value="volume">{t("markets.sortVolume")}</option>
            <option value="spread_pct">{t("markets.sortSpread")}</option>
            <option value="strike">{t("markets.sortStrike")}</option>
          </select>
        </label>
      </div>

      {/* Shortlist */}
      {shortlist.length > 0 && (
        <div className="flex flex-col gap-2">
          <div className="flex items-baseline gap-2">
            <h3 className="text-sm font-semibold">{t("markets.shortlistTitle")}</h3>
            <span className="text-xs text-muted-foreground">{t("markets.shortlistHint")}</span>
          </div>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
            {shortlist.map((row) => (
              <div key={row.contract_symbol ?? `${row.side}-${row.strike}`} className="rounded-lg border p-3">
                <div className="flex items-center justify-between">
                  <span
                    className={cn(
                      "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase",
                      row.side === "call"
                        ? "bg-success/15 text-success"
                        : "bg-danger/15 text-danger",
                    )}
                  >
                    {t(`markets.side_${row.side}`)}
                  </span>
                  <span className={cn("font-mono text-sm font-semibold", scoreColor(row.liquidity_score))}>
                    {fmtScore(row.liquidity_score)}
                  </span>
                </div>
                <div className="mt-1 font-mono text-lg">{fmtPrice(row.strike)}</div>
                <dl className="mt-1 space-y-0.5 text-xs text-muted-foreground">
                  <MiniRow label={t("markets.colMid")} value={fmtPrice(row.mid)} />
                  <MiniRow label={t("markets.colSpread")} value={fmtPct(row.spread_pct, 1, false)} />
                  <MiniRow label={t("markets.colOi")} value={fmtCompact(row.open_interest)} />
                  <MiniRow label={t("markets.colBreakEven")} value={fmtPrice(row.break_even)} />
                </dl>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Skew + open interest */}
      <div className="rounded-lg border p-3">
        <h3 className="mb-1 text-sm font-semibold">{t("markets.chartTitle")}</h3>
        <IvSmileChart calls={chain.calls} puts={chain.puts} spot={chain.spot} />
      </div>

      {/* Full ladder */}
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full min-w-[980px] text-sm">
          <caption className="sr-only">{t("markets.chainCaption", { ticker: chain.ticker })}</caption>
          <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
            <tr>
              <th scope="col" className="px-3 py-2 text-start font-medium">{t("markets.colSide")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colStrike")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colMoneyness")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colBid")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colAsk")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colMid")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colSpread")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colIv")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colDelta")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colTheta")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colVolume")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colOi")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colBreakEven")}</th>
              <th scope="col" className="px-3 py-2 text-end font-medium">{t("markets.colLiquidity")}</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr
                key={`${row.side}-${row.contract_symbol ?? row.strike}`}
                className="border-t border-border/60 hover:bg-muted/30"
              >
                <td className="px-3 py-1.5">
                  <span
                    className={cn(
                      "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase",
                      row.side === "call" ? "bg-success/15 text-success" : "bg-danger/15 text-danger",
                    )}
                  >
                    {t(`markets.side_${row.side}`)}
                  </span>
                </td>
                <td className="px-3 py-1.5 text-end font-mono font-medium">{fmtPrice(row.strike)}</td>
                <td className="px-3 py-1.5 text-end font-mono">{fmtPct(row.moneyness, 1)}</td>
                <td className="px-3 py-1.5 text-end font-mono">{fmtPrice(row.bid)}</td>
                <td className="px-3 py-1.5 text-end font-mono">{fmtPrice(row.ask)}</td>
                <td className="px-3 py-1.5 text-end font-mono">{fmtPrice(row.mid)}</td>
                <td className="px-3 py-1.5 text-end font-mono">{fmtPct(row.spread_pct, 1, false)}</td>
                <td className="px-3 py-1.5 text-end font-mono">
                  {fmtPct(row.implied_volatility, 1, false)}
                  {row.iv_source === "implied" && (
                    <span
                      className="ms-1 text-[10px] text-warning"
                      title={t("markets.ivImpliedHint")}
                    >
                      *
                    </span>
                  )}
                </td>
                <td className="px-3 py-1.5 text-end font-mono">{fmtGreek(row.greeks?.delta)}</td>
                <td className="px-3 py-1.5 text-end font-mono">{fmtGreek(row.greeks?.theta)}</td>
                <td className="px-3 py-1.5 text-end font-mono">{fmtCompact(row.volume)}</td>
                <td className="px-3 py-1.5 text-end font-mono">{fmtCompact(row.open_interest)}</td>
                <td className="px-3 py-1.5 text-end font-mono">{fmtPrice(row.break_even)}</td>
                <td className={cn("px-3 py-1.5 text-end font-mono", scoreColor(row.liquidity_score))}>
                  {fmtScore(row.liquidity_score)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {sorted.length === 0 && (
          <p className="p-6 text-center text-sm text-muted-foreground">
            {t("markets.emptyChainFilters")}
          </p>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        {t("markets.chainFootnote", {
          shown: sorted.length,
          total: chain.summary.contracts,
          rate: (chain.risk_free_rate * 100).toFixed(2),
        })}
      </p>
    </div>
  );
}

function Stat({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="rounded-lg border p-2.5">
      <div className="text-[11px] uppercase text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 text-lg", mono && "font-mono")}>{value}</div>
    </div>
  );
}

function MiniRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2">
      <dt>{label}</dt>
      <dd className="font-mono text-foreground">{value}</dd>
    </div>
  );
}
