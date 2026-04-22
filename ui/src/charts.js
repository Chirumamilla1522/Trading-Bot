/**
 * Price + portfolio charts (TradingView lightweight-charts v4).
 *
 * Ticker chart features:
 *   • Candlestick / line / area modes
 *   • Volume histogram overlay (bottom 22% of the pane)
 *   • Correct X-axis tick labels via tickMarkFormatter (ET timezone)
 *   • Proper price formatter: 2 dp for normal prices, auto for fractional
 *   • Crosshair OHLCV readout
 *   • Follow-live on periodic refresh (only if the user is at the right edge)
 */
import { createChart, ColorType, CrosshairMode, TickMarkType } from "lightweight-charts";
import { $ as el } from "./ui_bindings.js";

const BACKEND = "http://localhost:8000";

/** WebKit/Tauri-safe timeouts.
 * Important: even when AbortSignal.timeout exists, some WebKit builds abort fetches unexpectedly.
 * So we always use our own AbortController timer.
 */
function fetchWithTimeout(url, init = {}, ms = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => {
    try { controller.abort(); } catch { /* ignore */ }
  }, ms);
  return fetch(url, { ...init, signal: init.signal ?? controller.signal })
    .finally(() => clearTimeout(timer));
}

const US_STOCK_TZ = "America/New_York"; // ET/EST/EDT depending on date (IANA timezone)

// ── Shared chart theme (Atlas-driven) ─────────────────────────────────────────
// Pull from CSS variables so charts match Atlas light/dark + palette.
function _cssVar(name, fallback) {
  const v = getComputedStyle(document.body).getPropertyValue(name).trim();
  return v || fallback;
}

function atlasChartColors() {
  const isDark = document.body.classList.contains("theme-dark");
  const bg = _cssVar("--atlas-bg", isDark ? "#121110" : "#faf8f5");
  const ink = _cssVar("--atlas-ink", isDark ? "#fafaf9" : "#1c1917");
  const line = _cssVar("--atlas-line", isDark ? "rgba(250,250,249,0.1)" : "rgba(28,25,23,0.09)");
  const accent = _cssVar("--atlas-accent", isDark ? "#fb923c" : "#c2410c");
  return {
    bg,
    text: isDark ? "rgba(250,250,249,0.55)" : "rgba(28,25,23,0.55)",
    grid: line,
    border: line,
    up: _cssVar("--atlas-up", "#15803d"),
    down: _cssVar("--atlas-down", "#b91c1c"),
    crosshair: isDark ? "rgba(250,250,249,0.14)" : "rgba(28,25,23,0.14)",
    crosshairLabel: isDark ? "rgba(38,34,32,0.92)" : "rgba(255,255,255,0.92)",
    accent,
  };
}

// Cache semantic series colors (used by bars + volume).
const _C0 = atlasChartColors();
const _UP = _C0.up || "#059669";
const _DOWN = _C0.down || "#dc2626";

const crosshairLine = {
  color: atlasChartColors().crosshair,
  width: 1,
  style: 3,   // dashed
  visible: true,
  labelVisible: true,
  labelBackgroundColor: atlasChartColors().crosshairLabel,
};

function chartLayout() {
  const c = atlasChartColors();
  return {
    layout: {
      background: { type: ColorType.Solid, color: c.bg },
      textColor: c.text,
      fontSize: 11,
      fontFamily: '"JetBrains Mono", ui-monospace, monospace',
      attributionLogo: false,
    },
    grid: {
      vertLines: { color: c.grid, style: 0 },
      horzLines: { color: c.grid, style: 0 },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { ...crosshairLine, color: c.crosshair, labelBackgroundColor: c.crosshairLabel },
      horzLine: { ...crosshairLine, color: c.crosshair, labelBackgroundColor: c.crosshairLabel },
    },
    rightPriceScale: {
      borderColor: c.border,
      scaleMargins: { top: 0.06, bottom: 0.22 },
      entireTextOnly: false,
      autoScale: true,
      mode: 0,
      ticksVisible: false,
    },
    timeScale: {
      borderColor: c.border,
      lockVisibleTimeRangeOnResize: true,
      rightOffset: 10,
      barSpacing: 7,
      fixLeftEdge: false,
      fixRightEdge: false,
      visible: true,
      timeVisible: true,
      secondsVisible: false,
      ticksVisible: true,
      /** Default is 8 — our ET time strings need a bit more room or ticks get suppressed. */
      tickMarkMaxCharacterLength: 14,
      minimumHeight: 28,
    },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
    handleScale:  { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
    kineticScroll: { touch: true, mouse: true },
  };
}

// ── Module state ──────────────────────────────────────────────────────────────
let tickerChart  = null;
let tickerSeries = null;
let volSeries    = null;
let tickerSeriesMode = "candles";

let portfolioChart  = null;
let portfolioSeries = null;

// ── Request coalescing (prevents WebKit abort storms) ─────────────────────────
let _barsFetchController = null;
let _barsFetchSeq = 0;

// ── Helpers ───────────────────────────────────────────────────────────────────
function _chartDims(el, fallbackH) {
  const w = Math.max(el?.clientWidth || el?.offsetWidth || 0, 300);
  // offsetHeight is reliable even before layout engine has processed clientHeight
  const h = Math.max(el?.offsetHeight || el?.clientHeight || 0, fallbackH);
  return { width: w, height: h };
}

export function resizeTerminalCharts() {
  const tc = el("ticker-chart");
  const pc = el("portfolio-chart");
  if (tc && tickerChart) {
    const { width, height } = _chartDims(tc, 360);
    tickerChart.resize(width, height);
  }
  if (pc && portfolioChart) {
    const { width, height } = _chartDims(pc, 180);
    portfolioChart.resize(width, height);
  }
}

function fitTickerTimeScale() {
  if (!tickerChart || !tickerSeries) return;
  tickerChart.timeScale().fitContent();
  requestAnimationFrame(() => {
    if (tickerChart && tickerSeries) tickerChart.timeScale().fitContent();
  });
}

// ── Daily-mode: use YYYY-MM-DD string, not unix, so library handles weekends ─
const DAILY_CHART_TIMEFRAMES = new Set(["5D", "1M", "3M", "6M", "1Y", "2Y", "5Y", "MAX", "1Day"]);

function barChartTime(timeframe, unixSec) {
  const t = Math.floor(Number(unixSec));
  if (!Number.isFinite(t) || t <= 0) return null;
  if (DAILY_CHART_TIMEFRAMES.has(timeframe)) {
    // Use ET calendar day so daily candles align to US market sessions.
    const d = new Date(t * 1000);
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: US_STOCK_TZ,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(d);
    const p = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
    return `${p.year}-${p.month}-${p.day}`;
  }
  return t;   // UTCTimestamp (integer seconds)
}

function _etDayKey(unixSec) {
  const t = Math.floor(Number(unixSec));
  if (!Number.isFinite(t) || t <= 0) return null;
  const d = new Date(t * 1000);
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: US_STOCK_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(d);
  const p = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${p.year}-${p.month}-${p.day}`;
}

/**
 * Dedupe by unix second; for daily chart modes also collapse rows that share the same ET calendar day
 * (Yahoo occasionally returns two rows for one session → lightweight-charts requires strictly increasing time).
 */
function normalizeBars(bars, timeframe) {
  if (!bars?.length) return [];
  const tf = timeframe || "";
  const dailyMode = tf && DAILY_CHART_TIMEFRAMES.has(tf);

  if (dailyMode) {
    const byDay = new Map();
    for (const b of bars) {
      const t = Math.floor(Number(b.time));
      if (!Number.isFinite(t) || t <= 0) continue;
      const dk = _etDayKey(t);
      if (!dk) continue;
      const prev = byDay.get(dk);
      if (!prev || t >= Math.floor(Number(prev.time))) {
        byDay.set(dk, b);
      }
    }
    return [...byDay.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([, v]) => v);
  }

  const byTime = new Map();
  for (const b of bars) {
    const t = Math.floor(Number(b.time));
    if (!Number.isFinite(t) || t <= 0) continue;
    byTime.set(t, b);
  }
  return [...byTime.entries()].sort((a, b) => a[0] - b[0]).map(([, v]) => v);
}

/** Merge rows that share the same logical chart `time` (strictly increasing for lightweight-charts). */
function _dedupeChartRows(rows, mode) {
  const m = new Map();
  for (const r of rows) {
    const key = typeof r.time === "string" ? `s:${r.time}` : `n:${Number(r.time)}`;
    const prev = m.get(key);
    if (!prev) {
      m.set(key, { ...r });
      continue;
    }
    if (mode === "candles") {
      m.set(key, {
        time: r.time,
        open: prev.open,
        high: Math.max(Number(prev.high), Number(r.high)),
        low: Math.min(Number(prev.low), Number(r.low)),
        close: r.close,
      });
    } else {
      m.set(key, { time: r.time, value: r.value });
    }
  }
  const out = [...m.values()];
  out.sort((a, b) => {
    if (typeof a.time === "string" && typeof b.time === "string") {
      return a.time.localeCompare(b.time);
    }
    return Number(a.time) - Number(b.time);
  });
  return out;
}

function chartTimeToUnixSec(t) {
  if (t === undefined || t === null) return NaN;
  if (typeof t === "number" && Number.isFinite(t)) return t;
  if (typeof t === "string" && /^\d{4}-\d{2}-\d{2}$/.test(t)) {
    // Interpret daily string as ET midnight (not UTC) to keep visible range logic aligned.
    const [y, m, d] = t.split("-").map(Number);
    const ms = Date.parse(`${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}T00:00:00`);
    // Fallback: Date.parse above is local; instead compute a stable UTC timestamp for that ET day.
    // Use noon UTC as safe anchor then shift by offset is overkill; we only need monotonic mapping.
    return Math.floor(Date.UTC(y, m - 1, d) / 1000);
  }
  if (typeof t === "object" && t.year != null) {
    return Math.floor(Date.UTC(t.year, t.month - 1, t.day) / 1000);
  }
  return NaN;
}

function shouldFollowRealtimeAfterRefresh(visibleRange, previousLastBarUnix) {
  if (!visibleRange || previousLastBarUnix == null) return true;
  const toSec = chartTimeToUnixSec(visibleRange.to);
  const lastSec = Math.floor(Number(previousLastBarUnix));
  if (!Number.isFinite(toSec) || !Number.isFinite(lastSec)) return true;
  return toSec >= lastSec - 180;
}

function barLimitForTimeframe(tf) {
  const m = {
    "1Min": 500,
    "5Min": 500,
    "1D": 500,
    "15Min": 350,
    "1Hour": 180,
    "1Day": 8000,
    "5D": 40,
    "1M": 260,
    "3M": 400,
    "6M": 500,
    "1Y": 800,
    "2Y": 520,
    "5Y": 1300,
    "MAX": 8000,
  };
  return m[tf] ?? 180;
}

const _barSizeLabels = {
  "5D": "1d", "1M": "1d", "3M": "1d", "6M": "1d", "1Y": "1d", "2Y": "1d", "5Y": "1d", "MAX": "1d",
  "1D": "5m", "1Day": "1d", "1Hour": "1h", "15Min": "15m", "5Min": "5m", "1Min": "1m",
};
function chartBarSizeLabel(tf) { return _barSizeLabels[tf] || tf || "—"; }

export function updateChartBarSizeLabel(tf) {
  const bar = el("ticker-chart-bar-size");
  if (bar) bar.textContent = chartBarSizeLabel(tf);
}

// ── X-axis tick mark formatter ────────────────────────────────────────────────
// TickMarkType: 0=Year  1=Month  2=DayOfMonth  3=Time  4=TimeWithSeconds
function makeTickMarkFormatter(tf) {
  return function tickMarkFormatter(time, type) {
    const isDaily = DAILY_CHART_TIMEFRAMES.has(tf);
    if (isDaily) {
      // `time` is usually YYYY-MM-DD, BusinessDay, or unix from the library — normalize to ms.
      let ms;
      if (typeof time === "string") {
        ms = Date.parse(time.includes("T") ? time : `${time}T00:00:00Z`);
      } else if (time && typeof time === "object" && "year" in time) {
        ms = Date.UTC(time.year, time.month - 1, time.day);
      } else {
        const n = Number(time);
        ms = Number.isFinite(n) ? n * 1000 : NaN;
      }
      if (!Number.isFinite(ms)) return null;
      const d = new Date(ms);
      if (!Number.isFinite(d.getTime())) return null;
      if (type === TickMarkType.Year) {
        return new Intl.DateTimeFormat("en-US", { timeZone: US_STOCK_TZ, year: "numeric" }).format(d);
      }
      if (type === TickMarkType.Month) {
        return d.toLocaleDateString("en-US", { timeZone: US_STOCK_TZ, month: "short", year: "2-digit" });
      }
      return d.toLocaleDateString("en-US", { timeZone: US_STOCK_TZ, month: "short", day: "numeric" });
    }
    // Intraday: UTCTimestamp as number (or string); fall back to default formatter if unsure.
    const sec = typeof time === "number" ? time : typeof time === "string" ? Number(time) : NaN;
    if (!Number.isFinite(sec)) return null;
    const d = new Date(sec * 1000);
    if (!Number.isFinite(d.getTime())) return null;
    if (type === TickMarkType.DayOfMonth) {
      return d.toLocaleDateString("en-US", { timeZone: US_STOCK_TZ, month: "short", day: "numeric" });
    }
    // Hour:minute in ET — use hourCycle:h23 so macOS 24h preference doesn't
    // flip to UTC-looking output (Tauri/WebKit locale override bug).
    return new Intl.DateTimeFormat("en-US", {
      timeZone: US_STOCK_TZ,
      hour: "2-digit", minute: "2-digit",
      hourCycle: "h23",
    }).format(d); // e.g. "13:30"
  };
}

// ── Crosshair time label (tooltip) ───────────────────────────────────────────
// Uses formatToParts so the output is immune to the macOS 24h system setting
// that overrides hour12/hourCycle in Tauri's WKWebView.
function fmtCrosshairTime(tf, time) {
  if (time == null) return "";
  const isDaily = DAILY_CHART_TIMEFRAMES.has(tf);
  if (isDaily) {
    let ms;
    if (typeof time === "string") ms = Date.parse(time + "T00:00:00Z");
    else if (typeof time === "object" && time.year) ms = Date.UTC(time.year, time.month - 1, time.day);
    else ms = Number(time) * 1000;
    if (!Number.isFinite(ms)) return "";
    const d = new Date(ms);
    if (!Number.isFinite(d.getTime())) return "";
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: US_STOCK_TZ, month: "short", day: "numeric", year: "numeric",
    }).formatToParts(d);
    const p = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
    return `${p.month} ${p.day}, ${p.year}`;
  }
  const ms = Number(time) * 1000;
  if (!Number.isFinite(ms)) return "";
  const d = new Date(ms);
  if (!Number.isFinite(d.getTime())) return "";
  const opts = {
    timeZone: US_STOCK_TZ,
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
    hourCycle: "h23",
  };
  if (tf === "1Min") opts.second = "2-digit";
  try {
    const parts = new Intl.DateTimeFormat("en-US", opts).formatToParts(d);
    const p = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
    const hms = tf === "1Min" ? `${p.hour}:${p.minute}:${p.second}` : `${p.hour}:${p.minute}`;
    return `${p.month} ${Number(p.day)}  ${hms} ET`;
  } catch {
    // If WebView Intl chokes on an edge-case date, fail soft.
    return "";
  }
}

// ── Price formatter ───────────────────────────────────────────────────────────
function fmtPx(n) {
  const x = Number(n);
  if (!Number.isFinite(x)) return "—";
  if (x >= 1000) return x.toFixed(2);
  if (x >= 10)   return x.toFixed(2);
  if (x >= 1)    return x.toFixed(3);
  return x.toFixed(4);
}
function fmtPct(n) {
  const x = Number(n);
  if (!Number.isFinite(x)) return "—";
  return `${x >= 0 ? "+" : ""}${x.toFixed(2)}%`;
}
function fmtVolShort(v) {
  const x = Number(v);
  if (!Number.isFinite(x) || x <= 0) return null;
  if (x >= 1e9) return `${(x / 1e9).toFixed(2)}B`;
  if (x >= 1e6) return `${(x / 1e6).toFixed(2)}M`;
  if (x >= 1e3) return `${(x / 1e3).toFixed(1)}K`;
  return String(Math.round(x));
}

// ── Series price format (auto-selects precision) ─────────────────────────────
function priceFormat(price) {
  const p = Number(price);
  if (p >= 10)  return { type: "price", precision: 2, minMove: 0.01 };
  if (p >= 1)   return { type: "price", precision: 3, minMove: 0.001 };
  return           { type: "price", precision: 4, minMove: 0.0001 };
}

// ── Chart init ────────────────────────────────────────────────────────────────
export function initTerminalCharts() {
  const tc = el("ticker-chart");
  const pc = el("portfolio-chart");
  // Always render the main ticker plot even if the portfolio panel is hidden/removed.
  if (!tc) return;

  tc.style.height = "400px";
  if (pc) pc.style.height = "180px";

  const d1 = _chartDims(tc, 400);
  const d2 = pc ? _chartDims(pc, 180) : null;

  // Guard: if we ever initialize with a 0px container in WebView, the chart can render invisible.
  // Wait a frame and try again (Tauri layout sometimes settles after initial JS).
  if (d1.width <= 320 || d1.height <= 200) {
    requestAnimationFrame(() => {
      try {
        const d = _chartDims(tc, 400);
        if (tickerChart && d.width > 320 && d.height > 200) tickerChart.resize(d.width, d.height);
      } catch { /* ignore */ }
    });
  }

  const tf = el("ticker-chart-tf")?.value || "5D";

  const layout = chartLayout();
  tickerChart = createChart(tc, {
    ...layout,
    width: d1.width,
    height: d1.height,
    timeScale: {
      ...layout.timeScale,
      tickMarkFormatter: makeTickMarkFormatter(tf),
    },
    localization: {
      locale: "en-US",
      priceFormatter: (p) => fmtPx(p),
      // timeFormatter controls the crosshair axis label (separate from tickMarkFormatter).
      // Without this, LW Charts uses its own internal formatter which shows UTC time.
      timeFormatter: (time) => fmtCrosshairTime(window.__lastTickerTf || "5D", time),
    },
  });

  if (pc && d2) {
    portfolioChart = createChart(pc, {
      ...layout,
      width: d2.width,
      height: d2.height,
    });
  }

  const ro = new ResizeObserver(() => resizeTerminalCharts());
  ro.observe(tc);
  if (pc) ro.observe(pc);

  const styleSel = el("ticker-chart-style");
  const tfSel    = el("ticker-chart-tf");
  const portSel  = el("portfolio-chart-metric");

  styleSel?.addEventListener("change", () => {
    tickerSeriesMode = styleSel.value || "candles";
    if (window.__lastTickerBars) applyTickerBars(window.__lastTickerBars);
  });
  tfSel?.addEventListener("change", () => {
    if (window.__refreshTickerChart) window.__refreshTickerChart();
    if (window.__scheduleChartPoll)  window.__scheduleChartPoll();
  });
  portSel?.addEventListener("change", () => {
    if (window.__lastPortfolioPoints)
      applyPortfolioPoints(window.__lastPortfolioPoints, portSel.value || "equity");
  });

  updateChartBarSizeLabel(tf);

  tickerChart.subscribeCrosshairMove((param) => {
    const hint = el("ticker-chart-crosshair");
    if (!hint) return;
    if (!param.point || param.time === undefined || !tickerSeries) {
      // Crosshair left — restore the last bar summary
      const bars = window.__lastTickerBars;
      if (bars?.length) {
        const last = bars[bars.length - 1];
        const tf   = window.__lastTickerTf || "5D";
        const row  = tickerSeriesMode === "candles"
          ? { time: last.time, open: last.open, high: last.high, low: last.low, close: last.close }
          : { time: last.time, value: last.close };
        _showOhlcvHint(row, last, tf);
      }
      return;
    }
    const data = param.seriesData.get(tickerSeries);
    if (!data) return;
    const raw = window.__tickerBarLookup?.get(param.time) ?? window.__tickerBarLookup?.get(String(param.time));
    _showOhlcvHint(data, raw, window.__lastTickerTf || "5D");
  });
}

// ── OHLCV hint helper ─────────────────────────────────────────────────────────
function _showOhlcvHint(bar, rawBar, tf) {
  const hint = el("ticker-chart-crosshair");
  if (!hint || !bar) return;
  const tlab = fmtCrosshairTime(tf || window.__lastTickerTf || "5D", bar.time);
  let line = tlab;
  if (bar.open !== undefined) {
    const { open: o, high: hi, low: lo, close: c } = bar;
    const chg = o > 0 ? (((c - o) / o) * 100).toFixed(2) : "0.00";
    const sign = c >= o ? "▲" : "▼";
    line = `${tlab}  O ${fmtPx(o)}  H ${fmtPx(hi)}  L ${fmtPx(lo)}  C ${fmtPx(c)}  ${sign}${chg}%`;
    const vol = rawBar?.volume != null ? fmtVolShort(rawBar.volume) : null;
    if (vol) line += `  Vol ${vol}`;
  } else if (bar.value !== undefined) {
    line = `${tlab}  ${fmtPx(bar.value)}`;
  }
  hint.textContent = line;
  hint.style.opacity = "1";
}

// ── Apply bars to the chart ───────────────────────────────────────────────────
function clearTickerSeries() {
  if (tickerChart) {
    if (tickerSeries) { try { tickerChart.removeSeries(tickerSeries); } catch { /**/ } tickerSeries = null; }
    if (volSeries)    { try { tickerChart.removeSeries(volSeries);    } catch { /**/ } volSeries    = null; }
  }
}

function applyTickerBars(bars, opts = {}) {
  const { fit = true, followRealtime = false, previousLastBarUnix } = opts;
  if (!tickerChart) return;

  if (!bars?.length) { clearTickerSeries(); return; }
  const tf   = window.__lastTickerTf || "5D";
  const clean = normalizeBars(bars, tf);
  if (!clean.length) { clearTickerSeries(); return; }

  const toT  = (unix) => barChartTime(tf, unix);
  const mode = tickerSeriesMode || "candles";
  const intraday = !DAILY_CHART_TIMEFRAMES.has(tf);

  // Re-apply tick formatter and crosshair time formatter whenever tf changes
  tickerChart.applyOptions({
    timeScale: {
      timeVisible: true,
      secondsVisible: intraday && tf === "1Min",
      rightOffset: intraday ? 10 : 6,
      tickMarkFormatter: makeTickMarkFormatter(tf),
      tickMarkMaxCharacterLength: 14,
      minimumHeight: 28,
      ticksVisible: true,
      visible: true,
    },
    localization: {
      locale: "en-US",
      priceFormatter: (p) => fmtPx(p),
      timeFormatter: (time) => fmtCrosshairTime(tf, time),
    },
  });

  let savedRange = null;
  if (!fit && tickerSeries) {
    try { savedRange = tickerChart.timeScale().getVisibleRange(); } catch { /**/ }
  }

  // Build candle/line rows
  const rows = [];
  for (const b of clean) {
    const time = toT(b.time);
    if (time == null) continue;
    if (mode === "candles") {
      const o = +b.open, hi = +b.high, lo = +b.low, c = +b.close;
      if (![o, hi, lo, c].every(Number.isFinite)) continue;
      rows.push({ time, open: o, high: hi, low: lo, close: c });
    } else {
      const c = +b.close;
      if (!Number.isFinite(c)) continue;
      rows.push({ time, value: c });
    }
  }
  if (!rows.length) { clearTickerSeries(); return; }

  const priceRows = _dedupeChartRows(rows, mode);

  // Build volume rows — match candle colors; dedupe/sum volume per chart time
  const volMap = new Map();
  for (const b of clean) {
    const time = toT(b.time);
    const vol  = Number(b.volume);
    if (time == null || !Number.isFinite(vol) || vol <= 0) continue;
    const up = Number(b.close) >= Number(b.open);
    const key = typeof time === "string" ? `s:${time}` : `n:${Number(time)}`;
    const prev = volMap.get(key);
    const color = up ? `${_UP}55` : `${_DOWN}55`;
    if (!prev) {
      volMap.set(key, { time, value: vol, color });
    } else {
      volMap.set(key, { time, value: prev.value + vol, color });
    }
  }
  const volRows = [...volMap.values()].sort((a, b) => {
    if (typeof a.time === "string" && typeof b.time === "string") {
      return a.time.localeCompare(b.time);
    }
    return Number(a.time) - Number(b.time);
  });

  // Build lookup for crosshair tooltip
  const lookup = new Map();
  for (const b of clean) {
    const time = toT(b.time);
    if (time == null) continue;
    lookup.set(time, b);
    lookup.set(String(time), b);
  }
  window.__tickerBarLookup = lookup;

  clearTickerSeries();

  // Sample price for precision selection
  const samplePrice = clean[clean.length - 1]?.close ?? 100;
  const pf = priceFormat(samplePrice);

  if (mode === "candles") {
    tickerSeries = tickerChart.addCandlestickSeries({
      upColor:          _UP,
      downColor:        _DOWN,
      borderUpColor:    _UP,
      borderDownColor:  _DOWN,
      wickUpColor:      _UP,
      wickDownColor:    _DOWN,
      borderVisible:    true,
      wickVisible:      true,
      priceFormat:      pf,
      priceLineVisible: true,
      priceLineWidth:   1,
      priceLineColor:   "rgba(255,255,255,0.18)",
      priceLineStyle:   3,
      lastValueVisible: true,
    });
  } else if (mode === "line") {
    tickerSeries = tickerChart.addLineSeries({
      color:            _UP,
      lineWidth:        2,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius:  3,
      priceFormat:      pf,
      priceLineVisible: true,
      priceLineColor:   "rgba(255,255,255,0.18)",
      lastValueVisible: true,
    });
  } else {
    tickerSeries = tickerChart.addAreaSeries({
      lineColor:        _UP,
      topColor:         `${_UP}33`,
      bottomColor:      `${_UP}00`,
      lineWidth:        2,
      priceFormat:      pf,
      priceLineVisible: true,
      priceLineColor:   "rgba(255,255,255,0.18)",
      lastValueVisible: true,
    });
  }
  tickerSeries.setData(priceRows);
  const lastPr = priceRows[priceRows.length - 1];
  const lastRaw =
    clean.find(b => {
      const to = toT(b.time);
      return to === lastPr.time || String(to) === String(lastPr.time);
    }) || clean[clean.length - 1];
  _showOhlcvHint(lastPr, lastRaw, tf);

  // Volume histogram on a separate price scale so it doesn't overlap candles
  if (volRows.length) {
    volSeries = tickerChart.addHistogramSeries({
      priceFormat:    { type: "volume" },
      priceScaleId:   "vol",
      color:          "rgba(0,180,90,0.4)",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    tickerChart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
      drawTicks:    false,
      borderVisible: false,
      visible:      false,   // hide the vol axis — keeps it clean
    });
    volSeries.setData(volRows);
  }

  requestAnimationFrame(() => {
    resizeTerminalCharts();
    if (fit) {
      fitTickerTimeScale();
    } else if (tickerChart) {
      if (savedRange) {
        try { tickerChart.timeScale().setVisibleRange(savedRange); } catch { /**/ }
      }
      if (followRealtime && shouldFollowRealtimeAfterRefresh(savedRange, previousLastBarUnix)) {
        try { tickerChart.timeScale().scrollToRealTime(); } catch { /**/ }
      }
    }
  });
}

// ── Underlying stats strip ────────────────────────────────────────────────────
function renderUnderlyingDetail(u) {
  const set = (id, text, cls) => {
    const node = el(id);
    if (!node) return;
    node.textContent = text;
    node.className = cls ? `sd-val ${cls}` : "sd-val";
  };
  if (!u || !u.has_data) {
    ["sd-last","sd-chg1","sd-chgper","sd-hl","sd-vol"].forEach(id => set(id, "—"));
    return;
  }
  set("sd-last", fmtPx(u.last));
  const c1 = u.last_bar_change_pct;
  set("sd-chg1", fmtPct(c1), c1 > 0 ? "pos" : c1 < 0 ? "neg" : "");
  const cp = u.period_change_pct;
  set("sd-chgper", fmtPct(cp), cp > 0 ? "pos" : cp < 0 ? "neg" : "");
  set("sd-hl", `${fmtPx(u.period_high)} / ${fmtPx(u.period_low)}`);
  const v = u.volume_total;
  set("sd-vol", v > 0 ? (fmtVolShort(v) ?? "—") : "—");
}

// ── Public: load bars from backend ───────────────────────────────────────────
export async function loadTickerBars(ticker, timeframe, opts = {}) {
  const { preserveRange = false, followRealtime = false, bust = false, limit: limitOverride } = opts;
  const srcEl = el("ticker-chart-source");
  const tf    = timeframe || "5D";
  window.__lastTickerTf = tf;
  updateChartBarSizeLabel(tf);
  // Keep button strip in sync
  document.querySelectorAll(".chart-tf-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.tf === tf);
  });
  const lim = limitOverride ?? barLimitForTimeframe(tf);

  try {
    // Cancel any in-flight request: we only care about the latest user intent.
    _barsFetchSeq += 1;
    const seq = _barsFetchSeq;
    try { _barsFetchController?.abort(); } catch { /* ignore */ }
    _barsFetchController = new AbortController();

    const bustParam = bust ? "&bust=true" : "";
    const r = await fetchWithTimeout(
      `${BACKEND}/bars/${encodeURIComponent(ticker)}?timeframe=${encodeURIComponent(tf)}&limit=${lim}${bustParam}`,
      { signal: _barsFetchController.signal },
      45000,
    );
    // If a newer request started while this was in-flight, drop this response.
    if (seq !== _barsFetchSeq) return;
    if (!r.ok) {
      let detail = "";
      try { detail = await r.text(); } catch { /* ignore */ }
      throw new Error(`bars ${r.status}${detail ? ` · ${detail.slice(0, 180)}` : ""}`);
    }
    const data = await r.json();

    const prevBars = window.__lastTickerBars;
    const previousLastBarUnix = preserveRange && prevBars?.length
      ? prevBars[prevBars.length - 1].time : undefined;
    window.__lastTickerBars = data.bars || [];

    if (srcEl) {
      const labels = {
        alpaca: "Alpaca",
        alphavantage: "AlphaVantage",
        yfinance: "Yahoo",
        sqlite_daily: "Local DB",
        synthetic: "Synthetic",
        no_data: "No data",
      };
      srcEl.textContent = labels[data.source] || data.source || "—";
    }

    renderUnderlyingDetail(data.underlying);
    applyTickerBars(window.__lastTickerBars, {
      fit: !preserveRange,
      followRealtime: preserveRange && followRealtime,
      previousLastBarUnix,
    });
  } catch (e) {
    // Abort is expected during rapid clicking / range switching; don't treat as a failure.
    if (e?.name === "AbortError" || String(e?.message || e).toLowerCase().includes("aborted")) {
      return;
    }
    if (srcEl) srcEl.textContent = "err";
    renderUnderlyingDetail(null);
    console.warn("loadTickerBars", e);
    try {
      window.__showToast?.(`Chart data error: ${String(e?.message || e)}`, "err", 7000);
    } catch { /* ignore */ }
  }
}

// ── Portfolio chart ───────────────────────────────────────────────────────────
function clearPortfolioSeries() {
  if (!portfolioChart || !portfolioSeries) return;
  try { portfolioChart.removeSeries(portfolioSeries); } catch { /**/ }
  portfolioSeries = null;
}

export function applyPortfolioPoints(points, metric) {
  if (!portfolioChart || !points?.length) return;
  const sorted = [...points]
    .filter(p => p && Number.isFinite(Number(p.time)))
    .sort((a, b) => Number(a.time) - Number(b.time));
  if (!sorted.length) return;
  clearPortfolioSeries();

  const colors = { equity: "#00d4ff", daily_pnl: "#00e676", delta: "#f0c040", vega: "#b388ff", drawdown_pct: "#ff3355" };
  const m = metric || "equity";

  portfolioSeries = portfolioChart.addLineSeries({ color: colors[m] || "#00d4ff", lineWidth: 2 });
  portfolioSeries.setData(sorted.map(p => {
    let v = typeof p[m] === "number" ? p[m] : parseFloat(p[m]) || 0;
    if (m === "drawdown_pct") v *= 100;
    return { time: Math.floor(Number(p.time)), value: v };
  }));
  requestAnimationFrame(() => {
    resizeTerminalCharts();
    portfolioChart.timeScale().fitContent();
  });
}

export async function loadPortfolioSeries() {
  const countEl = el("portfolio-chart-count");
  try {
    const r = await fetchWithTimeout(`${BACKEND}/portfolio_series?points=300`, {}, 8000);
    if (!r.ok) throw new Error(String(r.status));
    const data = await r.json();
    window.__lastPortfolioPoints = data.points || [];
    if (countEl) countEl.textContent = `${window.__lastPortfolioPoints.length} pts`;
    const metric = el("portfolio-chart-metric")?.value || "equity";
    applyPortfolioPoints(window.__lastPortfolioPoints, metric);
  } catch (e) {
    if (countEl) countEl.textContent = "—";
    console.warn("loadPortfolioSeries", e);
  }
}
