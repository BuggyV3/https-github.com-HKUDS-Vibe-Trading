import { useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import type { OptionContract } from "@/lib/api";
import { getChartTheme } from "@/lib/chart-theme";
import { echarts } from "@/lib/echarts";
import { useThemeDark } from "@/lib/theme-store";

interface Props {
  calls: OptionContract[];
  puts: OptionContract[];
  /** Underlying price, drawn as a vertical marker. Omitted when unquoted. */
  spot: number | null;
  height?: number;
}

/** One strike's worth of plotted data; nulls leave gaps rather than a fake zero. */
type Point = [number, number | null];

function ivSeries(contracts: OptionContract[]): Point[] {
  return contracts
    .filter((row) => row.strike != null)
    .map((row) => [row.strike as number, row.implied_volatility != null ? row.implied_volatility * 100 : null]);
}

function oiSeries(contracts: OptionContract[]): Point[] {
  return contracts
    .filter((row) => row.strike != null)
    .map((row) => [row.strike as number, row.open_interest ?? null]);
}

/**
 * Implied-volatility skew across strikes, with open interest behind it.
 *
 * Two things a trader reads before picking a strike sit in one picture here:
 * where the market is pricing volatility (the smile, on the left axis) and
 * where the open positions actually are (the bars, on the right axis). Strikes
 * whose IV is unavailable break the line instead of dropping it to zero, so a
 * gap in the data reads as a gap.
 */
export function IvSmileChart({ calls, puts, spot, height = 280 }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const dark = useThemeDark();
  const { t } = useTranslation();

  const series = useMemo(
    () => ({
      callIv: ivSeries(calls),
      putIv: ivSeries(puts),
      callOi: oiSeries(calls),
      putOi: oiSeries(puts),
    }),
    [calls, puts],
  );

  const hasData = series.callIv.length > 0 || series.putIv.length > 0;

  useEffect(() => {
    if (!ref.current || !hasData) return;
    const theme = getChartTheme();
    const chart = echarts.init(ref.current);

    chart.setOption({
      backgroundColor: "transparent",
      grid: { left: 8, right: 8, top: 36, bottom: 8, containLabel: true },
      legend: {
        top: 4,
        textStyle: { color: theme.textColor, fontSize: 11 },
        data: [
          t("markets.chartCallIv"),
          t("markets.chartPutIv"),
          t("markets.chartCallOi"),
          t("markets.chartPutOi"),
        ],
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: theme.tooltipBg,
        borderColor: theme.tooltipBorder,
        textStyle: { color: theme.tooltipText, fontSize: 11 },
      },
      xAxis: {
        type: "value",
        name: t("markets.chartStrike"),
        nameTextStyle: { color: theme.textColor, fontSize: 10 },
        scale: true,
        axisLine: { lineStyle: { color: theme.axisColor } },
        axisLabel: { color: theme.textColor, fontSize: 10 },
        splitLine: { show: false },
      },
      yAxis: [
        {
          type: "value",
          name: t("markets.chartIvAxis"),
          nameTextStyle: { color: theme.textColor, fontSize: 10 },
          axisLabel: { color: theme.textColor, fontSize: 10, formatter: "{value}%" },
          splitLine: { lineStyle: { color: theme.gridColor } },
        },
        {
          type: "value",
          name: t("markets.chartOiAxis"),
          nameTextStyle: { color: theme.textColor, fontSize: 10 },
          axisLabel: { color: theme.textColor, fontSize: 10 },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: t("markets.chartCallOi"),
          type: "bar",
          yAxisIndex: 1,
          data: series.callOi,
          itemStyle: { color: `${theme.upColor}33` },
          barMaxWidth: 8,
        },
        {
          name: t("markets.chartPutOi"),
          type: "bar",
          yAxisIndex: 1,
          data: series.putOi,
          itemStyle: { color: `${theme.downColor}33` },
          barMaxWidth: 8,
        },
        {
          name: t("markets.chartCallIv"),
          type: "line",
          yAxisIndex: 0,
          data: series.callIv,
          connectNulls: false,
          showSymbol: false,
          smooth: true,
          lineStyle: { color: theme.upColor, width: 2 },
          itemStyle: { color: theme.upColor },
          // The spot marker rides on the IV series so it spans the plot area.
          ...(spot != null
            ? {
                markLine: {
                  silent: true,
                  symbol: "none",
                  data: [
                    {
                      xAxis: spot,
                      label: {
                        formatter: t("markets.chartSpot", { value: spot.toFixed(2) }),
                        color: theme.warningColor,
                        fontSize: 10,
                        position: "insideEndTop",
                      },
                    },
                  ],
                  lineStyle: { color: theme.warningColor, width: 1, type: "dashed" },
                },
              }
            : {}),
        },
        {
          name: t("markets.chartPutIv"),
          type: "line",
          yAxisIndex: 0,
          data: series.putIv,
          connectNulls: false,
          showSymbol: false,
          smooth: true,
          lineStyle: { color: theme.downColor, width: 2 },
          itemStyle: { color: theme.downColor },
        },
      ],
    });

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [series, spot, dark, hasData, t]);

  if (!hasData) return null;
  return <div ref={ref} style={{ height }} />;
}
