import { describe, expect, it } from "vitest";
import {
  changeColor,
  compareNullable,
  fmtCompact,
  fmtExpiry,
  fmtPct,
  fmtPctPoints,
  fmtPrice,
  NO_DATA,
  scoreColor,
  secondsSince,
} from "@/lib/marketFormat";

describe("marketFormat", () => {
  it("renders missing values as a dash, never as zero", () => {
    expect(fmtPrice(null)).toBe(NO_DATA);
    expect(fmtPct(null)).toBe(NO_DATA);
    expect(fmtCompact(undefined)).toBe(NO_DATA);
    expect(fmtPctPoints(Number.NaN)).toBe(NO_DATA);
    // A real zero still renders as a number: "no quote" and "zero" differ.
    expect(fmtPrice(0)).toBe("0.00");
    expect(fmtCompact(0)).toBe("0");
  });

  it("signs percentages only where the sign carries meaning", () => {
    expect(fmtPct(0.0412)).toBe("+4.12%");
    expect(fmtPct(-0.0412)).toBe("-4.12%");
    expect(fmtPct(0.0412, 1, false)).toBe("4.1%");
    expect(fmtPctPoints(1.5)).toBe("+1.50%");
  });

  it("abbreviates large magnitudes", () => {
    expect(fmtCompact(1.23e10)).toBe("12.30B");
    expect(fmtCompact(4.5e6)).toBe("4.50M");
    expect(fmtCompact(-9800)).toBe("-9.8K");
  });

  it("colours by sign and score band", () => {
    expect(changeColor(1)).toBe("text-success");
    expect(changeColor(-1)).toBe("text-danger");
    expect(changeColor(0)).toBe("text-muted-foreground");
    expect(changeColor(null)).toBe("text-muted-foreground");
    expect(scoreColor(80)).toBe("text-success");
    expect(scoreColor(50)).toBe("text-warning");
    expect(scoreColor(10)).toBe("text-muted-foreground");
  });

  it("sinks missing values to the bottom of a sort in both directions", () => {
    const rows = [3, null, 1, undefined];
    const desc = [...rows].sort((a, b) => compareNullable(a, b, "desc"));
    const asc = [...rows].sort((a, b) => compareNullable(a, b, "asc"));

    expect(desc.slice(0, 2)).toEqual([3, 1]);
    expect(asc.slice(0, 2)).toEqual([1, 3]);
    expect(desc.slice(2).every((value) => value == null)).toBe(true);
    expect(asc.slice(2).every((value) => value == null)).toBe(true);
  });

  it("measures freshness from an ISO stamp", () => {
    const now = Date.parse("2026-01-02T00:01:00Z");
    expect(secondsSince("2026-01-02T00:00:30Z", now)).toBe(30);
    expect(secondsSince(null, now)).toBeNull();
    expect(secondsSince("not-a-date", now)).toBeNull();
    // Clock skew must not surface as a negative age.
    expect(secondsSince("2026-01-02T00:02:00Z", now)).toBe(0);
  });

  it("renders an epoch expiration as a UTC date", () => {
    expect(fmtExpiry(1800604800)).toBe("2027-01-22");
    expect(fmtExpiry(null)).toBe(NO_DATA);
  });
});
