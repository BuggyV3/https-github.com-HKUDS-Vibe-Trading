/**
 * Formatting helpers shared by the market-discovery dashboard.
 *
 * The market APIs report `null` wherever the provider had no value, and the
 * dashboard must never render that as a zero. Every helper here turns `null`
 * (and any non-finite number that slipped through) into a visible em dash, so
 * "no data" and "zero" stay distinguishable on screen.
 */

/** Placeholder rendered wherever a provider value is missing. */
export const NO_DATA = "—";

function isNum(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Format a price with a fixed number of decimals. */
export function fmtPrice(value: number | null | undefined, digits = 2): string {
  return isNum(value) ? value.toFixed(digits) : NO_DATA;
}

/**
 * Format a fraction as a signed percentage.
 *
 * @param value Fraction (0.0412 renders as "+4.12%").
 * @param digits Decimal places.
 * @param signed Prefix positive values with "+". Off for magnitudes like a
 *   bid/ask spread, where a sign carries no meaning.
 */
export function fmtPct(
  value: number | null | undefined,
  digits = 2,
  signed = true,
): string {
  if (!isNum(value)) return NO_DATA;
  const sign = signed && value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(digits)}%`;
}

/**
 * Format a value already expressed in percentage points (the screener reports
 * change and turnover that way, not as fractions).
 */
export function fmtPctPoints(
  value: number | null | undefined,
  digits = 2,
  signed = true,
): string {
  if (!isNum(value)) return NO_DATA;
  const sign = signed && value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

/** Abbreviate a large count or currency amount (1.2B / 34.5M / 12.3K). */
export function fmtCompact(value: number | null | undefined): string {
  if (!isNum(value)) return NO_DATA;
  const abs = Math.abs(value);
  if (abs >= 1e12) return `${(value / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
  return value.toFixed(0);
}

/** Format a whole number with locale grouping. */
export function fmtInt(value: number | null | undefined): string {
  return isNum(value) ? Math.round(value).toLocaleString() : NO_DATA;
}

/** Format a 0-100 score to one decimal. */
export function fmtScore(value: number | null | undefined): string {
  return isNum(value) ? value.toFixed(1) : NO_DATA;
}

/** Format a greek to four decimals (delta/gamma need the resolution). */
export function fmtGreek(value: number | null | undefined): string {
  return isNum(value) ? value.toFixed(4) : NO_DATA;
}

/** Tailwind text colour for a signed change: green up, red down, muted flat. */
export function changeColor(value: number | null | undefined): string {
  if (!isNum(value) || value === 0) return "text-muted-foreground";
  return value > 0 ? "text-success" : "text-danger";
}

/**
 * Bucket a 0-100 score into a colour band.
 *
 * The thresholds (70 / 40) are display bands, not thresholds with any market
 * meaning — they exist so a long table can be scanned at a glance.
 */
export function scoreColor(value: number | null | undefined): string {
  if (!isNum(value)) return "text-muted-foreground";
  if (value >= 70) return "text-success";
  if (value >= 40) return "text-warning";
  return "text-muted-foreground";
}

/** Seconds elapsed since an ISO timestamp; `null` when it cannot be parsed. */
export function secondsSince(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null;
  const parsed = Date.parse(iso);
  if (!Number.isFinite(parsed)) return null;
  return Math.max(0, Math.round((now - parsed) / 1000));
}

/** Render an epoch-second expiration as an ISO date (YYYY-MM-DD, UTC). */
export function fmtExpiry(epochSeconds: number | null | undefined): string {
  if (!isNum(epochSeconds)) return NO_DATA;
  return new Date(epochSeconds * 1000).toISOString().slice(0, 10);
}

/**
 * Compare two possibly-null numbers for a table sort.
 *
 * Missing values always sink to the bottom regardless of direction: a blank
 * cell is not "the smallest value", and floating it to the top of a descending
 * sort would bury the rows the user is looking for.
 */
export function compareNullable(
  a: number | null | undefined,
  b: number | null | undefined,
  direction: "asc" | "desc",
): number {
  const aOk = isNum(a);
  const bOk = isNum(b);
  if (!aOk && !bOk) return 0;
  if (!aOk) return 1;
  if (!bOk) return -1;
  return direction === "asc" ? a - b : b - a;
}
