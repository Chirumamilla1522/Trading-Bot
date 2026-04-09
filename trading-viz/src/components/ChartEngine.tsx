import { ColorType, createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import { useMarketStore } from "../store/useMarketStore";
import type { OHLC } from "../types/market";

/** Canvas-based chart engine (TradingView lightweight-charts uses layered canvases internally). */
export function ChartEngine() {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lineRef = useRef<ISeriesApi<"Line"> | null>(null);

  const candles = useMarketStore((s) => s.candles);
  const forming = useMarketStore((s) => s.forming);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: "#0d0d12" },
        textColor: "#9b9bb5",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#1f1f2e" },
        horzLines: { color: "#1f1f2e" },
      },
      rightPriceScale: { borderColor: "#2a2a3a" },
      timeScale: { borderColor: "#2a2a3a", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
      handleScroll: { mouseWheel: true, pressedMouseMove: true },
      handleScale: { mouseWheel: true, pinch: true },
    });

    const candle = chart.addCandlestickSeries({
      upColor: "#00c853",
      downColor: "#ff1744",
      borderVisible: true,
      wickVisible: true,
    });

    const sma = chart.addLineSeries({
      color: "rgba(0, 212, 255, 0.75)",
      lineWidth: 1,
      priceLineVisible: false,
    });

    chartRef.current = chart;
    seriesRef.current = candle;
    lineRef.current = sma;

    const ro = new ResizeObserver(() => {
      const { width, height } = el.getBoundingClientRect();
      chart.applyOptions({ width, height });
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      lineRef.current = null;
    };
  }, []);

  useEffect(() => {
    const candle = seriesRef.current;
    const smaLine = lineRef.current;
    if (!candle || !smaLine) return;

    const rows: OHLC[] = [...candles.values()].sort((a, b) => a.time - b.time);

    if (forming) {
      rows.push({
        time: Math.floor(forming.bucketMs / 1000) as UTCTimestamp,
        open: forming.open,
        high: forming.high,
        low: forming.low,
        close: forming.close,
        volume: forming.volume,
      });
    }

    const data = rows.map((c) => ({
      time: c.time as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));

    candle.setData(data);

    const closes = rows.map((c) => c.close);
    const period = 14;
    const smaVals: (number | null)[] = closes.map((_, i) => {
      if (i < period - 1) return null;
      let s = 0;
      for (let j = 0; j < period; j++) s += closes[i - j];
      return s / period;
    });

    smaLine.setData(
      rows
        .map((c, i) => ({ time: c.time as UTCTimestamp, v: smaVals[i] }))
        .filter((x): x is { time: UTCTimestamp; v: number } => x.v != null)
        .map((x) => ({ time: x.time, value: x.v }))
    );

    chartRef.current?.timeScale().fitContent();
  }, [candles, forming]);

  return (
    <div
      ref={containerRef}
      className="chart-engine"
      style={{ width: "100%", height: "100%", minHeight: 320 }}
    />
  );
}
