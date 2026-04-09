import { ColorType, createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import { useMarketStore } from "../store/useMarketStore";
import type { OHLC } from "../types/market";

/** Single candlestick chart from live tick–aggregated OHLC (no overlay series). */
export function ChartEngine() {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  const candles = useMarketStore((s) => s.candles);
  const forming = useMarketStore((s) => s.forming);
  const hasRows =
    candles.size > 0 ||
    (forming !== null && Number.isFinite(forming.open));

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: "#0d0d12" },
        textColor: "#9b9bb5",
        fontSize: 11,
        attributionLogo: false,
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

    chartRef.current = chart;
    seriesRef.current = candle;

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
    };
  }, []);

  useEffect(() => {
    const candle = seriesRef.current;
    if (!candle) return;

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

    chartRef.current?.timeScale().fitContent();
  }, [candles, forming]);

  return (
    <div className="chart-engine-wrap" style={{ position: "relative", width: "100%", height: "100%", minHeight: 280 }}>
      <div
        ref={containerRef}
        className="chart-engine"
        style={{ width: "100%", height: "100%", minHeight: 280 }}
      />
      {!hasRows && (
        <div className="chart-empty-hint">
          No 1s candles yet — ticks build this chart. If the session is closed, quotes/trades may be sparse.
        </div>
      )}
    </div>
  );
}
