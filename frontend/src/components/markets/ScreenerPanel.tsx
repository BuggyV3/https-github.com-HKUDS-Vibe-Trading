import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ArrowDown, ArrowUp, Search } from "lucide-react";
import type { ScreenerResponse, ScreenerRow } from "@/lib/api";
import {
  changeColor,
  compareNullable,
  fmtCompact,
  fmtPctPoints,
  fmtPrice,
  fmtScore,
  scoreColor,
} from "@/lib/marketFormat";
import { cn } from "@/lib/utils";

type SortKey =
  | "flow_score"
  | "change_pct"
  | "amount"
  | "volume"
  | "turnover_rate"
  | "price";

type Direction = "up" | "down" | "all";

interface Props {
  data: ScreenerResponse | null;
  loading: boolean;
  /** Symbol currently loaded in the options explorer, highlighted in the table. */
  selected: string | null;
  onSelect: (row: ScreenerRow) => void;
  /** Whether rows can drive the options explorer (US chains only). */
  selectable: boolean;
}

// `as const` keeps `labelKey` narrowed to its literal type, which is what the
// project's typed `t()` needs to accept it as a translation key.
const COLUMNS = [
  { key: "price", labelKey: "markets.colPrice" },
  { key: "change_pct", labelKey: "markets.colChange" },
  { key: "amount", labelKey: "markets.colTurnover" },
  { key: "volume", labelKey: "markets.colVolume" },
  { key: "turnover_rate", labelKey: "markets.colTurnoverRate" },
  { key: "flow_score", labelKey: "markets.colFlowScore" },
] as const satisfies readonly { key: SortKey; labelKey: string }[];

/**
 * Sortable, filterable table of a market's ranked instruments.
 *
 * The provider does the heavy ranking server-side (so the slice really is the
 * top of the whole market); everything in this component re-orders and filters
 * that slice locally, which keeps every interaction instant and never issues a
 * new provider request just to flip a sort direction.
 */
export function ScreenerPanel({ data, loading, selected, onSelect, selectable }: Props) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [direction, setDirection] = useState<Direction>("all");
  const [minScore, setMinScore] = useState(0);
  const [sortKey, setSortKey] = useState<SortKey>("flow_score");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const rows = useMemo(() => {
    const source = data?.rows ?? [];
    const needle = query.trim().toLowerCase();
    const filtered = source.filter((row) => {
      if (needle && !`${row.code} ${row.name}`.toLowerCase().includes(needle)) return false;
      if (direction === "up" && !(row.change_pct != null && row.change_pct > 0)) return false;
      if (direction === "down" && !(row.change_pct != null && row.change_pct < 0)) return false;
      // A row with no score is never filtered out by the score slider at its
      // default of zero, but must not survive a raised threshold either.
      if (minScore > 0 && (row.flow_score == null || row.flow_score < minScore)) return false;
      return true;
    });
    return [...filtered].sort((a, b) => compareNullable(a[sortKey], b[sortKey], sortDir));
  }, [data, query, direction, minScore, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((prev) => (prev === "desc" ? "asc" : "desc"));
      return;
    }
    setSortKey(key);
    setSortDir("desc");
  };

  const total = data?.rows.length ?? 0;

  return (
    <div className="flex flex-col gap-3">
      {/* Local filters */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[180px]">
          <Search className="pointer-events-none absolute start-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("markets.searchPlaceholder")}
            aria-label={t("markets.searchPlaceholder")}
            className="w-full rounded-md border bg-background py-2 pe-3 ps-9 text-sm"
          />
        </div>

        <div className="flex gap-1.5" role="group" aria-label={t("markets.directionFilter")}>
          {(["all", "up", "down"] as const).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setDirection(option)}
              aria-pressed={direction === option}
              className={cn(
                "rounded border px-3 py-1.5 text-sm transition-colors",
                direction === option
                  ? "bg-primary text-primary-foreground"
                  : "border-muted-foreground/30 hover:border-primary",
              )}
            >
              {t(`markets.direction_${option}`)}
            </button>
          ))}
        </div>

        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">
            {t("markets.minFlowScore", { value: minScore })}
          </span>
          <input
            type="range"
            min={0}
            max={100}
            step={5}
            value={minScore}
            onChange={(event) => setMinScore(Number(event.target.value))}
            aria-label={t("markets.minFlowScoreLabel")}
            className="w-28"
          />
        </label>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full min-w-[720px] text-sm">
          <caption className="sr-only">{t("markets.tableCaption")}</caption>
          <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
            <tr>
              <th scope="col" className="px-3 py-2 text-start font-medium">
                {t("markets.colSymbol")}
              </th>
              {COLUMNS.map((column) => {
                const active = sortKey === column.key;
                return (
                  <th
                    key={column.key}
                    scope="col"
                    aria-sort={active ? (sortDir === "asc" ? "ascending" : "descending") : "none"}
                    className="px-3 py-2 text-end font-medium"
                  >
                    <button
                      type="button"
                      onClick={() => toggleSort(column.key)}
                      className={cn(
                        "inline-flex items-center gap-1 hover:text-foreground",
                        active && "text-foreground",
                      )}
                    >
                      {t(column.labelKey)}
                      {active &&
                        (sortDir === "asc" ? (
                          <ArrowUp className="h-3 w-3" />
                        ) : (
                          <ArrowDown className="h-3 w-3" />
                        ))}
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const isSelected = selected === row.code;
              return (
                <tr
                  key={row.code}
                  onClick={() => selectable && onSelect(row)}
                  className={cn(
                    "border-t border-border/60",
                    selectable && "cursor-pointer hover:bg-muted/40",
                    isSelected && "bg-primary/10",
                  )}
                >
                  <td className="px-3 py-2">
                    <div className="font-mono font-medium">{row.code}</div>
                    <div className="max-w-[220px] truncate text-xs text-muted-foreground">
                      {row.name}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-end font-mono">{fmtPrice(row.price)}</td>
                  <td className={cn("px-3 py-2 text-end font-mono", changeColor(row.change_pct))}>
                    {fmtPctPoints(row.change_pct)}
                  </td>
                  <td className="px-3 py-2 text-end font-mono">{fmtCompact(row.amount)}</td>
                  <td className="px-3 py-2 text-end font-mono">{fmtCompact(row.volume)}</td>
                  <td className="px-3 py-2 text-end font-mono">
                    {fmtPctPoints(row.turnover_rate, 2, false)}
                  </td>
                  <td className="px-3 py-2 text-end">
                    <div className="flex items-center justify-end gap-2">
                      <div
                        className="h-1.5 w-16 overflow-hidden rounded bg-muted"
                        aria-hidden="true"
                      >
                        <div
                          className="h-full bg-primary"
                          style={{ width: `${Math.max(0, Math.min(100, row.flow_score ?? 0))}%` }}
                        />
                      </div>
                      <span className={cn("w-10 font-mono", scoreColor(row.flow_score))}>
                        {fmtScore(row.flow_score)}
                      </span>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        {!loading && rows.length === 0 && (
          <p className="p-6 text-center text-sm text-muted-foreground">
            {total === 0 ? t("markets.emptyMarket") : t("markets.emptyFilters")}
          </p>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        {t("markets.rowCount", { shown: rows.length, total })}
      </p>
    </div>
  );
}
