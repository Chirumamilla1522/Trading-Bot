/**
 * Agentic Trading Terminal – Frontend Controller
 *
 * Layout:
 *   #col-scanner  → S&P 500 scanner sidebar (all tickers, live IV, P/C, OI)
 *   #col-left     → Options chain for the active ticker
 *   #col-centre   → Metrics strip + positions + news tape
 *   #col-right    → Agent reasoning log + order blotter
 *
 * Polling:
 *   /scanner         every 10 s  → scanner (IV, P/C, OI, structure)
 *   /scanner/quotes  every 1 s   → merge live last / day % into scanner rows
 *   /options/{tkr}  on demand   → options chain (fetched when ticker changes)
 *   /state          every 2 s   → metrics, positions, news, agent_runtime
 *   /reasoning_log  every 4 s   → XAI log
 *   /bars/{tkr}     ~15s intraday / ~90s daily → OHLC refresh (REST = delayed vs true tick stream)
 *   /portfolio_series  ~15 s    → NAV / greeks time series
 *   /quote/{t}         every 1 s → price pill + quote strip + sd-last (incl. pre/post)
 */

import {
  initTerminalCharts,
  loadTickerBars,
  loadPortfolioSeries,
  resizeTerminalCharts,
} from "./charts.js";
import "./trading-viz-mount.tsx";

// ── Tauri v2 API ──────────────────────────────────────────────────────────────
const _tauri  = window.__TAURI__;
const invoke  = _tauri?.core?.invoke  ?? (async () => ({}));
const listen  = _tauri?.event?.listen ?? (() => {});
const BACKEND    = "http://localhost:8000";
const WS_BACKEND = "ws://localhost:8000/ws";

// ── Active ticker state ───────────────────────────────────────────────────────
let activeTicker    = "SPY";
/** Used by the embedded trading-viz WebSocket to align with the terminal ticker on connect. */
window.__terminalActiveTicker = () => activeTicker;
let _chartPollTimer = null;
let scannerData     = [];   // latest scanner rows from /scanner
let scannerFilter   = "";
let scannerSort     = "iv";

// ── Toast notification system ────────────────────────────────────────────────
const _toastContainer = () => document.getElementById("toast-container");

function showToast(msg, type = "info", duration = 4000) {
  const icons = { ok: "✓", err: "✕", warn: "⚠", info: "ℹ" };
  const tc = _toastContainer();
  if (!tc) return;
  const t = document.createElement("div");
  t.className = `toast toast-${type}`;
  t.innerHTML = `<span class="toast-icon">${icons[type] ?? "ℹ"}</span><span class="toast-msg">${msg}</span>`;
  tc.appendChild(t);
  setTimeout(() => {
    t.classList.add("toast-out");
    t.addEventListener("animationend", () => t.remove());
  }, duration);
}

// ── Flash animation helper ───────────────────────────────────────────────────
const _prevMetrics = {};

function _flashUpdate(id, rawVal, displayText) {
  const el = document.getElementById(id);
  if (!el) return;
  const prev = _prevMetrics[id];
  if (prev !== undefined && prev !== rawVal) {
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth; // force reflow
    el.classList.add(rawVal > prev ? "flash-up" : "flash-down");
  }
  _prevMetrics[id] = rawVal;
  el.textContent = displayText;
}

// ── Resizable columns ────────────────────────────────────────────────────────
const COL_STORAGE_KEY = "att_col_widths";
const COL_MIN = [100, 220, 200, 200]; // min px for scanner, left, centre, right

function _defaultColWidths() {
  return [168, 390, null, 320]; // null = flex (1fr)
}

function _loadColWidths() {
  try {
    const saved = JSON.parse(localStorage.getItem(COL_STORAGE_KEY));
    if (Array.isArray(saved) && saved.length === 4) return saved;
  } catch {}
  return _defaultColWidths();
}

function _saveColWidths(w) {
  try { localStorage.setItem(COL_STORAGE_KEY, JSON.stringify(w)); } catch {}
}

function _applyColWidths(w) {
  const ws = document.getElementById("workspace");
  if (!ws) return;
  // w[2] is the centre column — uses 1fr when null
  const c2 = w[2] != null ? `${w[2]}px` : "1fr";
  ws.style.gridTemplateColumns = `${w[0]}px 5px ${w[1]}px 5px ${c2} 5px ${w[3]}px`;
}

function _wireResizeHandles() {
  const workspace = document.getElementById("workspace");
  if (!workspace) return;

  // Inject handles as grid children after each section
  const sections = ["col-scanner", "col-left", "col-centre"];
  sections.forEach((id, i) => {
    const section = document.getElementById(id);
    if (!section) return;
    const handle = document.createElement("div");
    handle.className = "resize-handle";
    handle.dataset.handleIdx = String(i);
    handle.title = "Drag to resize";
    // Insert after the section
    section.insertAdjacentElement("afterend", handle);
  });

  _applyColWidths(_loadColWidths());

  // Double-click handle → reset columns to default
  workspace.addEventListener("dblclick", e => {
    if (!e.target.closest(".resize-handle")) return;
    _applyColWidths(_defaultColWidths());
    _saveColWidths(_defaultColWidths());
    setTimeout(resizeTerminalCharts, 60);
  });

  let dragging = null;

  workspace.addEventListener("mousedown", e => {
    const handle = e.target.closest(".resize-handle");
    if (!handle) return;
    e.preventDefault();
    const idx = parseInt(handle.dataset.handleIdx, 10);
    const colIds = ["col-scanner","col-left","col-centre","col-right"];
    const measured = colIds.map(id => document.getElementById(id)?.getBoundingClientRect().width ?? 0);
    dragging = { idx, startX: e.clientX, startW: measured, handle };
    handle.classList.add("dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  });

  document.addEventListener("mousemove", e => {
    if (!dragging) return;
    const { idx, startX, startW } = dragging;
    const dx = e.clientX - startX;
    const a = Math.max(COL_MIN[idx],     startW[idx]     + dx);
    const b = Math.max(COL_MIN[idx + 1], startW[idx + 1] - dx);
    const w = [...startW];
    w[idx] = a; w[idx + 1] = b;
    // Centre (index 2) stays 1fr unless handle 2 is being dragged
    _applyColWidths([w[0], w[1], idx === 2 ? w[2] : null, w[3]]);
  });

  document.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging.handle.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    const colIds = ["col-scanner","col-left","col-centre","col-right"];
    const final = colIds.map(id => Math.round(document.getElementById(id)?.getBoundingClientRect().width ?? 0));
    _saveColWidths([final[0], final[1], null, final[3]]);
    dragging = null;
    setTimeout(resizeTerminalCharts, 60);
  });
}

// ── Collapsible panels ────────────────────────────────────────────────────────
function _wireCollapsible(header, bodyId) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  header.addEventListener("click", e => {
    // Don't collapse if user clicked a button inside the header
    if (e.target.tagName === "BUTTON" || e.target.tagName === "SELECT") return;
    header.classList.toggle("collapsed");
    body.classList.toggle("collapsed");
  });
}

function _wireAllCollapsibles() {
  document.querySelectorAll(".panel-header.collapsible").forEach(h => {
    const target = h.dataset.target;
    if (target) _wireCollapsible(h, target);
  });
}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────
function _wireKeyboardShortcuts() {
  document.addEventListener("keydown", e => {
    // "/" focuses the gobar (unless already in an input)
    if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
      e.preventDefault();
      document.getElementById("gobar")?.focus();
    }
    // Escape blurs any input
    if (e.key === "Escape") {
      document.activeElement?.blur();
    }
  });
  // Enter on gobar triggers ticker switch
  document.getElementById("gobar")?.addEventListener("keydown", e => {
    if (e.key === "Enter") {
      const val = e.target.value.trim().toUpperCase();
      if (val) {
        switchTicker(val);
        e.target.value = "";
        e.target.blur();
      }
    }
  });
}

// ── Clock ─────────────────────────────────────────────────────────────────────
function startClock() {
  const el = document.getElementById("clock");
  const tick = () => {
    el.textContent = new Date().toLocaleTimeString("en-US", {
      hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  };
  tick();
  setInterval(tick, 1000);
}

// ── S&P 500 Scanner ───────────────────────────────────────────────────────────

async function pollScanner() {
  try {
    const url = `${BACKEND}/scanner?sort=${scannerSort}`;
    const r   = await fetch(url, { signal: AbortSignal.timeout(25000) });
    if (!r.ok) return;
    scannerData = await r.json();
    renderScanner();
  } catch { /* backend not ready */ }
}

/** Sort key aligned with server `sort_scan_rows` (for re-order after quote-only refresh). */
function _scannerRowSortKey(row, sort) {
  switch (sort) {
    case "iv":
      return Number(row.avg_iv_30d) || 0;
    case "pc":
      return Number(row.pc_ratio) || 0;
    case "oi":
      return Number(row.total_oi) || 0;
    case "ticker":
      return row.ticker || "";
    case "price": {
      const L = row.last != null ? Number(row.last) : NaN;
      if (Number.isFinite(L)) return L;
      return Number(row.underlying_price) || 0;
    }
    case "chg": {
      const c = row.change_pct;
      if (c != null && Number.isFinite(Number(c))) return Number(c);
      return -1e9;
    }
    default:
      return Number(row.avg_iv_30d) || 0;
  }
}

function resortScannerDataClient() {
  const s = scannerSort;
  scannerData.sort((a, b) => {
    if (s === "ticker") {
      return (a.ticker || "").localeCompare(b.ticker || "");
    }
    const va = _scannerRowSortKey(a, s);
    const vb = _scannerRowSortKey(b, s);
    if (va < vb) return 1;
    if (va > vb) return -1;
    return 0;
  });
}

/** ~1 Hz live price / change; skips when tab hidden or scanner not loaded yet. */
async function pollScannerQuotes() {
  if (document.visibilityState !== "visible") return;
  if (!scannerData.length) return;
  try {
    const r = await fetch(`${BACKEND}/scanner/quotes`, {
      signal: AbortSignal.timeout(12000),
    });
    if (!r.ok) return;
    const data = await r.json();
    const quotes = data.quotes || {};
    for (const row of scannerData) {
      const q = quotes[row.ticker?.toUpperCase?.()];
      if (!q) continue;
      row.last = q.last;
      row.change_pct = q.change_pct;
      row.quote_source = q.quote_source;
      row.quote_session = q.session;
    }
    if (scannerSort === "price" || scannerSort === "chg") {
      resortScannerDataClient();
    }
    renderScanner();
  } catch { /* ignore */ }
}

function renderScanner() {
  const filter = scannerFilter.toUpperCase();
  const rows   = scannerData.filter(d =>
    !filter || d.ticker.includes(filter)
  );

  const total = scannerData.length;
  document.getElementById("scanner-count").textContent =
    `${total} / ${rows.length}`;

  const tbody = document.getElementById("scanner-body");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-row scan-loading">
      ${total === 0 ? "Scanning S&amp;P 500… (~2 min first load)" : "No matches"}
    </td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(d => {
    const isActive = d.ticker === activeTicker;
    const iv30     = d.avg_iv_30d ?? 0;
    const ivPct    = (iv30 * 100).toFixed(1);
    const ivCls    = iv30 > 0.5 ? "iv-hot" : iv30 > 0.25 ? "iv-warm" : "iv-cool";
    const pc       = d.pc_ratio ?? 0;
    const pcCls    = pc > 1.2 ? "pc-bearish" : pc < 0.8 ? "pc-bullish" : "";
    const lastN    = d.last != null ? Number(d.last) : NaN;
    const hasLive  = Number.isFinite(lastN);
    const estN     = !hasLive && d.underlying_price > 0 ? Number(d.underlying_price) : NaN;
    const pxNum    = hasLive ? lastN : estN;
    const px       = Number.isFinite(pxNum)
      ? fmtNum(pxNum, pxNum >= 100 ? 2 : 4)
      : "—";
    const sess = d.quote_session || "";
    const sessHint =
      sess === "pre" ? " · PRE-MKT" : sess === "post" ? " · AFTER-HRS" : sess === "regular" ? " · RTH" : "";
    const pxTitle  = hasLive
      ? `Last trade (live quote)${sessHint}`
      : (Number.isFinite(estN) ? "ATM strike est — quote pending" : "—");
    const pxCls    = hasLive ? "" : "scan-loading";
    const chgRaw   = d.change_pct;
    let chgStr     = "—";
    let chgCls     = "";
    if (chgRaw != null && Number.isFinite(Number(chgRaw))) {
      const c = Number(chgRaw);
      chgStr = `${c >= 0 ? "+" : ""}${c.toFixed(2)}%`;
      chgCls = c > 0 ? "pos" : c < 0 ? "neg" : "";
    } else if (hasLive) {
      chgStr = "—";
    }

    return `<tr class="scanner-row${isActive ? " active" : ""}" data-ticker="${esc(d.ticker)}">
      <td title="${esc(d.ticker)}">${esc(d.ticker)}</td>
      <td${pxCls ? ` class="${pxCls}"` : ""} title="${pxTitle}">${px}</td>
      <td class="${chgCls}">${chgStr}</td>
      <td class="${ivCls}">${ivPct > 0 ? ivPct + "%" : "—"}</td>
      <td class="${pcCls}">${pc > 0 ? pc.toFixed(2) : "—"}</td>
      <td class="scan-loading">${d.num_contracts ?? 0}</td>
    </tr>`;
  }).join("");
}

// ── Scanner interactions ──────────────────────────────────────────────────────

document.getElementById("scanner-body").addEventListener("click", e => {
  const row = e.target.closest(".scanner-row");
  if (!row) return;
  const ticker = row.dataset.ticker;
  if (ticker) switchTicker(ticker);
});

document.getElementById("scanner-filter").addEventListener("input", e => {
  scannerFilter = e.target.value.trim();
  renderScanner();
});

document.getElementById("scanner-sort").addEventListener("change", e => {
  scannerSort = e.target.value;
  pollScanner();  // re-fetch with new sort order
});

// ── Ticker switching ──────────────────────────────────────────────────────────

async function switchTicker(ticker) {
  if (ticker === activeTicker) return;
  activeTicker = ticker;

  // Update header & price pill ticker immediately
  document.getElementById("chain-ticker").textContent = ticker;
  const cLab = document.getElementById("chart-ticker-label");
  if (cLab) cLab.textContent = ticker;
  document.getElementById("chain-count").textContent = "loading…";
  const ppTicker = document.getElementById("pp-ticker");
  if (ppTicker) ppTicker.textContent = ticker;
  const ppPrice = document.getElementById("pp-price");
  if (ppPrice) { ppPrice.textContent = "—"; ppPrice.dataset.prev = ""; }
  const ppChange = document.getElementById("pp-change");
  if (ppChange) { ppChange.textContent = "—"; ppChange.className = ""; }

  // Highlight scanner row
  renderScanner();

  // Notify backend (sets firm_state.ticker + pre-warms drilldown cache)
  fetch(`${BACKEND}/set_ticker`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker }),
  }).catch(() => {});

  await fetchOptionsChain(ticker);
  _syncStockTicker(ticker);

  window.dispatchEvent(
    new CustomEvent("terminal:symbol", { detail: { symbol: ticker } }),
  );

  const tf = document.getElementById("ticker-chart-tf")?.value || "5D";
  loadTickerBars(ticker, tf);
  pollQuote();
  loadStockInfo(ticker);
  scheduleChartPoll();
}

async function fetchOptionsChain(ticker) {
  try {
    document.getElementById("options-body").innerHTML =
      `<tr><td colspan="11" class="empty-row scan-loading">Fetching ${esc(ticker)}…</td></tr>`;
    const r = await fetch(`${BACKEND}/options/${encodeURIComponent(ticker)}`,
      { signal: AbortSignal.timeout(30000) });
    if (!r.ok) throw new Error(r.statusText);
    const data = await r.json();
    renderOptionsTable(data.contracts ?? []);
  } catch (e) {
    document.getElementById("options-body").innerHTML =
      `<tr><td colspan="11" class="empty-row scan-error">Failed: ${esc(String(e))}</td></tr>`;
  }
}

// ── Options chain table ───────────────────────────────────────────────────────

let _chainRaw      = [];   // full unfiltered dataset
let _chainFilter   = "all"; // "all" | "calls" | "puts"
let _chainSort     = "iv_desc";
let _chainStrike   = null;  // number | null

function _applyChainView() {
  let rows = [..._chainRaw];

  // C/P filter
  if (_chainFilter === "calls") rows = rows.filter(g => g.right === "CALL");
  if (_chainFilter === "puts")  rows = rows.filter(g => g.right === "PUT");

  // Strike proximity filter
  if (_chainStrike !== null && !isNaN(_chainStrike)) {
    const range = Math.max(_chainStrike * 0.05, 10); // ±5% or ±10 pts
    rows = rows.filter(g => Math.abs((g.strike ?? 0) - _chainStrike) <= range);
  }

  // Sort
  const sorters = {
    iv_desc:     (a, b) => (b.iv ?? 0) - (a.iv ?? 0),
    iv_asc:      (a, b) => (a.iv ?? 0) - (b.iv ?? 0),
    strike_asc:  (a, b) => (a.strike ?? 0) - (b.strike ?? 0),
    strike_desc: (a, b) => (b.strike ?? 0) - (a.strike ?? 0),
    exp_asc:     (a, b) => (a.expiry ?? "").localeCompare(b.expiry ?? ""),
    delta_desc:  (a, b) => Math.abs(b.delta ?? 0) - Math.abs(a.delta ?? 0),
    bid_desc:    (a, b) => (b.bid ?? 0) - (a.bid ?? 0),
  };
  rows.sort(sorters[_chainSort] ?? sorters.iv_desc);

  // Cap for perf
  rows = rows.slice(0, 300);

  document.getElementById("chain-count").textContent =
    rows.length ? `${rows.length} / ${_chainRaw.length} contracts` : "no data";

  if (!rows.length) {
    document.getElementById("options-body").innerHTML =
      `<tr><td colspan="11" class="empty-row">No contracts match filters</td></tr>`;
    return;
  }

  const tbody = document.getElementById("options-body");
  tbody.innerHTML = rows.map(g => {
    const isCall = g.right === "CALL";
    const cls    = isCall ? "call" : "put";
    const ivPct  = ((g.iv ?? 0) * 100).toFixed(1);
    const ivCls  = parseFloat(ivPct) > 80 ? "iv-hot" :
                   parseFloat(ivPct) > 40 ? "iv-warm" : "";
    return `<tr class="options-row" data-contract='${JSON.stringify({
      symbol: g.symbol, right: g.right, strike: g.strike,
      expiry: g.expiry, bid: g.bid, ask: g.ask, iv: g.iv, delta: g.delta,
    }).replace(/'/g, "&#39;")}' title="Click to fill order ticket">
      <td class="${cls}">${esc((g.symbol ?? "").slice(0, 18))}</td>
      <td>${esc(fmtExpiry(g.expiry ?? ""))}</td>
      <td>${fmtNum(g.strike, 0)}</td>
      <td class="${cls}">${isCall ? "C" : "P"}</td>
      <td>${fmtNum(g.bid, 2)}</td>
      <td>${fmtNum(g.ask, 2)}</td>
      <td class="${ivCls}">${ivPct}%</td>
      <td>${fmtNum(g.delta, 3)}</td>
      <td>${fmtNum(g.gamma, 4)}</td>
      <td class="neg">${fmtNum(g.theta, 2)}</td>
      <td>${fmtNum(g.vega, 2)}</td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll(".options-row").forEach(row => {
    row.addEventListener("click", () => {
      try {
        const contract = JSON.parse(row.dataset.contract);
        prefillOptionTicket(contract);
      } catch { /* malformed data */ }
    });
  });
}

function renderOptionsTable(greeks) {
  _chainRaw = greeks ?? [];
  document.getElementById("chain-count").textContent =
    _chainRaw.length ? `${_chainRaw.length} contracts` : "no data";

  if (!_chainRaw.length) {
    document.getElementById("options-body").innerHTML =
      `<tr><td colspan="11" class="empty-row">No contracts available</td></tr>`;
    return;
  }
  _applyChainView();
}

// ── Chain toolbar wiring ──────────────────────────────────────────────────────
// ── Range × Interval → backend timeframe mapping ──────────────────────────────
// Both range (window) and interval (bar size) are independently selectable for
// any combination. Backend lookbacks were expanded to support multi-day intraday requests.
//
// Backend TF codes:
//   "1D"    = today's 5-minute bars (filtered to last ET session)
//   "1Min"  = 1-minute bars  (limit controls depth)
//   "15Min" = 15-minute bars
//   "1Hour" = 1-hour bars
//   "5D"/"1M"/"3M"/"6M"/"1Y" = daily bars over that window
//
// Bars per trading day: 1m≈390, 5m≈78, 15m≈26, 1H≈7, 1D=1
// Trading days per range: 1D=1, 5D=5, 1M=22, 3M=66, 6M=126, 1Y=252
const _INTERVAL_TO_TF = {
  "1m":  "1Min",
  "5m":  "1D",    // special: today's session 5m bars via filter
  "15m": "15Min",
  "1H":  "1Hour",
  "1D":  null,    // resolved per range below
};
const _DAILY_RANGE_TF = { "5D": "5D", "1M": "1M", "3M": "3M", "6M": "6M", "1Y": "1Y" };

// Trading days per range (used to compute limit for intraday bars over multi-day windows)
const _RANGE_TRADE_DAYS = { "1D": 1, "5D": 5, "1M": 22, "3M": 66, "6M": 126, "1Y": 252 };
// Bars per full trading day per interval
const _BARS_PER_DAY = { "1m": 390, "5m": 78, "15m": 26, "1H": 7 };

// Sensible default interval to auto-select when range changes
const _DEFAULT_INTERVAL = { "1D": "5m", "5D": "1H", "1M": "1H", "3M": "1D", "6M": "1D", "1Y": "1D" };

let _chartRange    = "5D";
let _chartInterval = "1H";  // default for 5D

function _resolveBackendTf() {
  // "1D" bar interval: use the historical daily range TF
  if (_chartInterval === "1D") {
    return { tf: _DAILY_RANGE_TF[_chartRange] || "5D", limit: null };
  }
  // Special 5m case for single-day range (today's session filtered bars)
  if (_chartRange === "1D" && _chartInterval === "5m") {
    return { tf: "1D", limit: 80 };
  }
  // Map interval → backend TF string
  const backendTf = _INTERVAL_TO_TF[_chartInterval];
  if (!backendTf) return { tf: _DAILY_RANGE_TF[_chartRange] || "5D", limit: null };

  const tradingDays = _RANGE_TRADE_DAYS[_chartRange] || 5;
  const barsPerDay  = _BARS_PER_DAY[_chartInterval] || 7;
  // 20% buffer for partial days / pre-post; hard floor of 20 to stay above API ge=10
  const limit = Math.min(2000, Math.max(20, Math.ceil(tradingDays * barsPerDay * 1.2)));
  return { tf: backendTf, limit };
}

function _applyChartTf(bust = false) {
  const { tf, limit } = _resolveBackendTf();
  const tfSel = document.getElementById("ticker-chart-tf");
  if (tfSel) tfSel.value = tf;
  loadTickerBars(activeTicker, tf, { bust, limit: limit ?? undefined });
  scheduleChartPoll();
  updateChartBarSizeLabel?.(tf);
  _highlightActiveIntervalBtn();
}

function _highlightActiveIntervalBtn() {
  // Reflect current interval selection visually; grey 1m/5m for long ranges
  const tradingDays = _RANGE_TRADE_DAYS[_chartRange] || 1;
  document.querySelectorAll(".chart-interval-btn").forEach(b => {
    const iv = b.dataset.interval;
    // Dim intervals that would produce impossibly many bars (>2000)
    const bpd = _BARS_PER_DAY[iv] || 1;
    const tooGranular = tradingDays * bpd > 2000;
    b.classList.toggle("chart-interval-dim", tooGranular);
    b.title = tooGranular
      ? `${iv} bars — too granular for ${_chartRange} range (use 1H or 1D)`
      : `${iv} bars`;
  });
}

function _wireChartStripButtons() {
  const styleSel = document.getElementById("ticker-chart-style");

  // Range buttons — auto-select sensible default interval when range changes
  document.querySelectorAll(".chart-range-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".chart-range-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _chartRange = btn.dataset.range;
      // Auto-switch to a sensible interval for the new range
      const defInterval = _DEFAULT_INTERVAL[_chartRange] || "1D";
      _chartInterval = defInterval;
      // Reflect in UI
      document.querySelectorAll(".chart-interval-btn").forEach(b => {
        b.classList.toggle("active", b.dataset.interval === defInterval);
      });
      _applyChartTf(true);
    });
  });

  // Interval buttons — now active for ALL ranges
  document.querySelectorAll(".chart-interval-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".chart-interval-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _chartInterval = btn.dataset.interval;
      _applyChartTf(true);
    });
  });

  // Style buttons
  document.querySelectorAll(".chart-style-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".chart-style-btn").forEach(b => b.classList.remove("chart-style-active"));
      btn.classList.add("chart-style-active");
      if (styleSel) {
        styleSel.value = btn.dataset.style;
        styleSel.dispatchEvent(new Event("change"));
      }
    });
  });

  // Initial state sync
  _highlightActiveIntervalBtn();

  // Update sd-chg1 badge class (pos/neg) when text changes
  const chgEl = document.getElementById("sd-chg1");
  if (chgEl) {
    const obs = new MutationObserver(() => {
      const v = parseFloat(chgEl.textContent);
      chgEl.classList.toggle("pos", v > 0);
      chgEl.classList.toggle("neg", v < 0);
    });
    obs.observe(chgEl, { childList: true, subtree: true, characterData: true });
  }
}

function _wireChainToolbar() {
  // C/P filter buttons
  ["cp-all", "cp-calls", "cp-puts"].forEach(id => {
    document.getElementById(id)?.addEventListener("click", () => {
      document.querySelectorAll(".chain-cp-btn").forEach(b => b.classList.remove("chain-cp-active"));
      document.getElementById(id).classList.add("chain-cp-active");
      _chainFilter = id === "cp-all" ? "all" : id === "cp-calls" ? "calls" : "puts";
      _applyChainView();
    });
  });

  // Sort dropdown
  document.getElementById("chain-sort")?.addEventListener("change", e => {
    _chainSort = e.target.value;
    // Update header highlight
    document.querySelectorAll(".th-sort-active").forEach(th => th.classList.remove("th-sort-active"));
    const colMap = { iv_desc: "iv", iv_asc: "iv", strike_asc: "strike", strike_desc: "strike",
                     exp_asc: "exp", delta_desc: "delta", bid_desc: null };
    const col = colMap[_chainSort];
    if (col) document.querySelector(`th[data-col="${col}"]`)?.classList.add("th-sort-active");
    _applyChainView();
  });

  // Strike filter input
  document.getElementById("chain-strike-filter")?.addEventListener("input", e => {
    const v = parseFloat(e.target.value);
    _chainStrike = isNaN(v) ? null : v;
    _applyChainView();
  });

  // Sortable header clicks
  document.querySelectorAll(".th-sortable").forEach(th => {
    th.style.cursor = "pointer";
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      const sortMap = { iv: "iv_desc", strike: "strike_asc", exp: "exp_asc", delta: "delta_desc", symbol: "iv_desc" };
      // Toggle asc/desc for same column
      if (_chainSort === sortMap[col]) {
        const toggles = { iv_desc: "iv_asc", iv_asc: "iv_desc",
                          strike_asc: "strike_desc", strike_desc: "strike_asc" };
        _chainSort = toggles[_chainSort] ?? sortMap[col];
      } else {
        _chainSort = sortMap[col] ?? "iv_desc";
      }
      document.getElementById("chain-sort").value = _chainSort;
      document.querySelectorAll(".th-sort-active").forEach(t => t.classList.remove("th-sort-active"));
      th.classList.add("th-sort-active");
      _applyChainView();
    });
  });
}

// Format YYMMDD → MM/DD/YY
function fmtExpiry(s) {
  if (!s || s.length !== 6) return s;
  return `${s.slice(2, 4)}/${s.slice(4, 6)}/${s.slice(0, 2)}`;
}

// ── Positions tables (stocks vs options) ─────────────────────────────────────

function renderStockPositions(stocks) {
  const tbody = document.getElementById("stock-positions-body");
  if (!tbody) return;
  if (!stocks?.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-row">No stock positions</td></tr>`;
    return;
  }
  tbody.innerHTML = stocks.map(s => {
    const pnl = s.unrealized_pl ?? 0;
    const pnlCls = pnl >= 0 ? "pos" : "neg";
    return `<tr>
      <td>${esc(s.ticker ?? "")}</td>
      <td>${fmtNum(s.quantity, 4)}</td>
      <td>${fmtNum(s.avg_cost, 2)}</td>
      <td>${fmtNum(s.market_value, 2)}</td>
      <td class="${pnlCls}">${pnl >= 0 ? "+" : ""}${fmtNum(pnl, 2)}</td>
    </tr>`;
  }).join("");
}

function renderPositions(positions) {
  const tbody = document.getElementById("positions-body");
  if (!positions?.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-row">No option positions</td></tr>`;
    return;
  }
  tbody.innerHTML = positions.map(p => {
    const pnl    = p.current_pnl ?? 0;
    const pnlCls = pnl >= 0 ? "pos" : "neg";
    return `<tr>
      <td>${esc(p.symbol ?? "")}</td>
      <td class="${p.right === "CALL" ? "call" : "put"}">${p.right === "CALL" ? "C" : "P"}</td>
      <td>${fmtNum(p.strike, 0)}</td>
      <td>${esc(p.expiry ?? "")}</td>
      <td>${p.quantity ?? 0}</td>
      <td>${fmtNum(p.avg_cost, 2)}</td>
      <td class="${pnlCls}">${pnl >= 0 ? "+" : ""}${fmtNum(pnl, 2)}</td>
    </tr>`;
  }).join("");
}

// ── Metrics strip ─────────────────────────────────────────────────────────────

function setUsdCell(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  const n = Number(val);
  if (!Number.isFinite(n)) { el.textContent = "—"; return; }
  const formatted = `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  _flashUpdate(id, n, formatted);
}

function updateMetrics(state) {
  const r = state.risk ?? {};
  setMetric("daily-pnl",  r.daily_pnl       ?? 0, "$", true,  2);
  setMetric("port-delta", r.portfolio_delta  ?? 0, "",  true,  3);
  setMetric("port-vega",  r.portfolio_vega   ?? 0, "$", true,  2);
  setMetric("drawdown",   (r.drawdown_pct    ?? 0) * 100, "", false, 2, "%");
  setMetric("sentiment",  state.aggregate_sentiment ?? 0, "", true, 2);

  setUsdCell("acct-cash", state.cash_balance ?? 0);
  setUsdCell("acct-bp", state.buying_power ?? 0);
  const eqNav = Number(state.account_equity);
  const eq =
    Number.isFinite(eqNav) && eqNav > 0
      ? eqNav
      : Number(r.current_nav ?? r.opening_nav ?? 0);
  setUsdCell("acct-equity", eq);

  const stateTicker = state.ticker ?? "SPY";
  if (stateTicker !== activeTicker) {
    // Backend-initiated ticker change (e.g. agent decision)
    activeTicker = stateTicker;
    document.getElementById("chain-ticker").textContent = stateTicker;
    const cLab = document.getElementById("chart-ticker-label");
    if (cLab) cLab.textContent = stateTicker;
    window.dispatchEvent(
      new CustomEvent("terminal:symbol", { detail: { symbol: stateTicker } }),
    );
  }

  updateRegimeBadge(state.market_regime);
  if (state.circuit_breaker_tripped) setCircuitBreakerUI(true);

  renderStockPositions(state.stock_positions);
  renderPositions(state.open_positions);

  _updateNewsFeedStatus(state);
  // Render news from state (backup in case /news poll fails)
  if (state.news_feed?.length) renderNews(state.news_feed);

  if (state.agent_runtime) renderAgentStatus(state.agent_runtime);
}

function setMetric(id, val, prefix, signed, decimals = 2, suffix = "") {
  const el = document.getElementById(id);
  if (!el) return;
  const formatted = `${prefix}${Number(val).toFixed(decimals)}${suffix}`;
  const prev = _prevMetrics[id];
  if (prev !== undefined && prev !== val) {
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth;
    el.classList.add(val > prev ? "flash-up" : "flash-down");
    // Update trend arrow
    const trendEl = document.getElementById(`trend-${id}`);
    if (trendEl) {
      trendEl.textContent = val > prev ? "▲" : "▼";
      trendEl.className = `metric-trend ${val > prev ? "up" : "down"}`;
    }
    // Update card border color
    const card = document.getElementById(`card-${id}`);
    if (card && signed) {
      card.classList.toggle("positive-card", val >= 0);
      card.classList.toggle("negative-card", val < 0);
    }
  }
  _prevMetrics[id] = val;
  el.textContent = formatted;
  el.className   = "metric-value" +
    (signed ? (val >= 0 ? " positive" : " negative") : "");
}

function updateRegimeBadge(regime) {
  const el = document.getElementById("regime-badge");
  // Prefix so it is not read as one phrase with "AGENTS: …" (e.g. ERROR + UNKNOWN).
  el.textContent = `REGIME: ${regime ?? "UNKNOWN"}`;
  el.className = `badge badge-${
    (regime ?? "").includes("UP")   ? "ok"   :
    (regime ?? "").includes("DOWN") ? "warn" :
    regime === "HIGH_VOL"           ? "warn" : "unknown"
  }`;
}

function setCircuitBreakerUI(tripped) {
  const el = document.getElementById("cb-badge");
  el.textContent = tripped ? "⚠ CB TRIPPED" : "CIRCUIT OK";
  el.className   = `badge ${tripped ? "badge-danger" : "badge-ok"}`;
}

// ── News tape ─────────────────────────────────────────────────────────────────

const _seenNews = new Set();

// ── Category meta ─────────────────────────────────────────────────────────────
const _CAT_META = {
  earnings:   { label: "EARNINGS",   cls: "cat-earnings"   },
  deal:       { label: "M&A",        cls: "cat-deal"        },
  macro:      { label: "MACRO",      cls: "cat-macro"       },
  regulatory: { label: "REG",        cls: "cat-regulatory"  },
  guidance:   { label: "GUIDANCE",   cls: "cat-guidance"    },
  dividend:   { label: "DIV",        cls: "cat-dividend"    },
  bankruptcy: { label: "BKRPT",      cls: "cat-bankruptcy"  },
  management: { label: "MGMT CHG",   cls: "cat-management"  },
  activist:   { label: "ACTIVIST",   cls: "cat-activist"    },
  split:      { label: "SPLIT",      cls: "cat-split"       },
  analyst:    { label: "ANALYST",    cls: "cat-analyst"     },
  partnership:{ label: "DEAL",       cls: "cat-partnership" },
  product:    { label: "PRODUCT",    cls: "cat-product"     },
  buyback:    { label: "BUYBACK",    cls: "cat-buyback"     },
  general:    { label: "",           cls: ""                },
};

const _TIER_LABEL = {
  index:     { label: "INDEX",     cls: "tier-index"     },
  portfolio: { label: "PORTFOLIO", cls: "tier-portfolio" },
  active:    { label: "ACTIVE",    cls: "tier-active"    },
  top:       { label: "S&P500",    cls: "tier-top"       },
};

function _newsSrcBadge(source) {
  const s = (source || "").toUpperCase();
  // Backend uses "Benzinga" or "Benzinga / Author" — never exact "BENZINGA"
  if (s.startsWith("BENZINGA"))  return `<span class="news-src news-src-bz">BZ</span>`;
  if (s.startsWith("SYNTHETIC")) return `<span class="news-src news-src-syn">SYN</span>`;
  return `<span class="news-src news-src-yf">YF</span>`;
}

function _newsRelTime(pubStr) {
  try {
    const ageSec = (Date.now() - new Date(pubStr).getTime()) / 1000;
    if (ageSec < 60)        return `${Math.round(ageSec)}s ago`;
    if (ageSec < 3600)      return `${Math.round(ageSec / 60)}m ago`;
    if (ageSec < 86400)     return `${Math.round(ageSec / 3600)}h ago`;
    return new Date(pubStr).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  } catch { return "--"; }
}

// ── News detail modal ─────────────────────────────────────────────────────────
function _openNewsModal(item) {
  const modal   = document.getElementById("news-modal");
  const metaEl  = document.getElementById("news-modal-meta");
  const headEl  = document.getElementById("news-modal-headline");
  const badgeEl = document.getElementById("news-modal-badges");
  const sumEl   = document.getElementById("news-modal-summary");
  const footEl  = document.getElementById("news-modal-footer");
  if (!modal) return;

  const s      = Number(item.sentiment ?? 0);
  const conf   = Number(item.confidence ?? 0);
  const sCls   = s > 0.1 ? "sentiment-pos" : s < -0.1 ? "sentiment-neg" : "sentiment-neu";
  const isHigh = (item.priority || "NORMAL") === "HIGH";
  const cat    = _CAT_META[item.category]   || _CAT_META.general;
  const tier   = _TIER_LABEL[item.ticker_tier] || _TIER_LABEL.top;

  const timeStr = item.published_at
    ? new Date(item.published_at).toLocaleString("en-US", {
        month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
        hour12: false, timeZoneName: "short",
      })
    : "";

  metaEl.innerHTML = `
    <span class="news-modal-source">${esc(item.source || "")}</span>
    <span class="news-modal-time">${esc(timeStr)}</span>`;

  headEl.textContent = item.headline || "";
  headEl.className   = `news-modal-headline ${s > 0.1 ? "modal-bull" : s < -0.1 ? "modal-bear" : ""}`;

  const tickerTags = (item.tickers || [])
    .filter(t => t && t.length <= 6).slice(0, 6)
    .map(t => `<span class="news-ticker">${esc(t)}</span>`).join("");

  const catBadge  = cat.label  ? `<span class="news-cat ${cat.cls}">${cat.label}</span>`   : "";
  const tierBadge = tier.label ? `<span class="news-tier ${tier.cls}">${tier.label}</span>` : "";
  const highBadge = isHigh     ? `<span class="news-cat cat-high">⚡ HIGH PRIORITY</span>`  : "";

  badgeEl.innerHTML = `
    ${highBadge}${catBadge}${tierBadge}
    <span class="news-modal-sentiment ${sCls}">
      Sentiment ${s >= 0 ? "+" : ""}${s.toFixed(2)} &nbsp; Confidence ${Math.round(conf * 100)}%
    </span>
    ${tickerTags}`;

  const summary = (item.summary || "").trim();
  if (summary) {
    sumEl.textContent = summary;
    sumEl.style.display = "";
  } else {
    sumEl.textContent = "No summary available for this article.";
    sumEl.style.display = "";
    sumEl.classList.add("news-modal-no-summary");
  }

  const url = (item.url || "").trim();
  footEl.innerHTML = url
    ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" class="news-modal-link">
         Read full article ↗
       </a>`
    : `<span class="news-modal-no-link">No source URL available</span>`;

  modal.removeAttribute("hidden");
  document.body.classList.add("news-modal-open");
  document.getElementById("news-modal-close")?.focus();
}

function _closeNewsModal() {
  const modal = document.getElementById("news-modal");
  if (!modal) return;
  modal.setAttribute("hidden", "");
  document.body.classList.remove("news-modal-open");
}

// Wire up modal close controls once DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("news-modal-close")
    ?.addEventListener("click", _closeNewsModal);
  document.getElementById("news-modal")
    ?.addEventListener("click", e => { if (e.target === e.currentTarget) _closeNewsModal(); });
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") _closeNewsModal();
});

function _updateNewsFeedStatus(state) {
  const badge = document.getElementById("news-feed-status");
  const tape  = document.getElementById("news-tape");
  if (!tape) return;
  const enabled = state.news_feed_enabled !== false;

  if (badge) {
    badge.textContent = enabled ? "ON" : "OFF";
    badge.className   = enabled ? "news-feed-badge on" : "news-feed-badge off";
    badge.title       = enabled
      ? "News ingestion enabled (Benzinga + yfinance)"
      : "News ingestion disabled — set ENABLE_NEWS_FEED=true and restart API";
  }

  if (tape.querySelector(".news-item")) return;
  if (state.news_feed?.length) return;

  const prev = tape.querySelector(".news-empty");
  const msg  = enabled
    ? "Fetching headlines…"
    : "News feed off — set ENABLE_NEWS_FEED=true in .env and restart the API server.";
  if (prev) {
    prev.textContent = msg;
    prev.classList.toggle("muted", !enabled);
  } else {
    const div = document.createElement("div");
    div.className = "news-empty" + (enabled ? "" : " muted");
    div.textContent = msg;
    tape.appendChild(div);
  }
}

function renderNews(items) {
  if (!items?.length) return;
  const tape = document.getElementById("news-tape");
  if (!tape) return;
  tape.querySelector(".news-empty")?.remove();

  // Sort incoming: HIGH first, then by time desc
  const sorted = [...items].sort((a, b) => {
    const pOrd = { HIGH: 0, NORMAL: 1, LOW: 2 };
    const pd = (pOrd[a.priority] ?? 1) - (pOrd[b.priority] ?? 1);
    if (pd !== 0) return pd;
    return new Date(b.published_at) - new Date(a.published_at);
  });

  sorted.forEach(item => {
    const id = (item.published_at ?? "") + (item.headline ?? "");
    if (_seenNews.has(id)) return;
    _seenNews.add(id);

    const s      = Number(item.sentiment ?? 0);
    const conf   = Number(item.confidence ?? 0);
    const sCls   = s > 0.1 ? "sentiment-pos" : s < -0.1 ? "sentiment-neg" : "sentiment-neu";
    const isHigh = (item.priority || "NORMAL") === "HIGH";

    const cat   = _CAT_META[item.category] || _CAT_META.general;
    const tier  = _TIER_LABEL[item.ticker_tier] || _TIER_LABEL.top;

    const catBadge = cat.label
      ? `<span class="news-cat ${cat.cls}">${cat.label}</span>`
      : "";
    const tierBadge = tier.label
      ? `<span class="news-tier ${tier.cls}">${tier.label}</span>`
      : "";

    const tickerTags = (item.tickers || [])
      .filter(t => t && t.length <= 6)
      .slice(0, 3)
      .map(t => `<span class="news-ticker">${esc(t)}</span>`)
      .join("");

    const div = document.createElement("div");
    div.className = [
      "news-item",
      s > 0.1 ? "news-item-bull" : s < -0.1 ? "news-item-bear" : "news-item-neu",
      isHigh ? "news-item-high" : "",
    ].join(" ").trim();
    div.setAttribute("role", "button");
    div.setAttribute("tabindex", "0");
    div.title = "Click to expand";

    div.innerHTML = `
      <div class="news-row1">
        <span class="news-time">${_newsRelTime(item.published_at)}</span>
        ${_newsSrcBadge(item.source)}
        ${catBadge}
        ${tierBadge}
        <span class="news-sentiment ${sCls}">${s >= 0 ? "+" : ""}${s.toFixed(2)}</span>
        <span class="news-conf">${Math.round(conf * 100)}%</span>
        ${tickerTags}
      </div>
      <div class="news-row2">
        <span class="news-text">${esc(item.headline)}</span>
      </div>`;

    // Store full item data for the modal
    div._newsItem = item;
    div.addEventListener("click", () => _openNewsModal(item));
    div.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") _openNewsModal(item); });

    // HIGH items always prepended at top; NORMAL/LOW go below the last HIGH
    if (isHigh) {
      tape.prepend(div);
    } else {
      // Find first non-HIGH item and insert before it, or append
      const firstNormal = [...tape.children].find(c => !c.classList.contains("news-item-high"));
      firstNormal ? tape.insertBefore(div, firstNormal) : tape.prepend(div);
    }
  });

  while (tape.children.length > 60) tape.removeChild(tape.lastChild);
}

// ── Reasoning log ─────────────────────────────────────────────────────────────

const _seenReasoning = new Set();

function appendReasoningEntry(entry) {
  if (!entry) return;
  const key = `${entry.timestamp}|${entry.agent}|${entry.action}`;
  if (_seenReasoning.has(key)) return;
  _seenReasoning.add(key);

  const panel  = document.getElementById("reasoning-panel");
  const action = entry.action ?? "INFO";
  const cls    = ["PROCEED","HOLD","ABORT","ERROR"].includes(action)
    ? `action-${action}` : "action-default";
  const div    = document.createElement("div");
  div.className = "reasoning-entry";
  div.innerHTML = `
    <div class="re-header">
      <span class="re-agent">${esc(entry.agent ?? "")}</span>
      <span class="re-action ${cls}">${esc(action)}</span>
      <span class="re-time">${(entry.timestamp ?? "").slice(11, 19)}</span>
    </div>
    <div class="re-reasoning">${esc(entry.reasoning ?? "")}</div>`;
  panel.prepend(div);
  while (panel.children.length > 120) panel.removeChild(panel.lastChild);

  setAgentActive(true);
  setTimeout(() => setAgentActive(false), 2000);
}

function setAgentActive(active) {
  const dot = document.getElementById("agent-indicator");
  if (dot) dot.className = `agent-dot ${active ? "dot-active" : "dot-idle"}`;
}

// ── Stock Info (fundamentals, peers, ecosystem) ───────────────────────────────

function _fmtLarge(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v === 0) return "—";
  if (v >= 1e12) return `$${(v / 1e12).toFixed(2)}T`;
  if (v >= 1e9)  return `$${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6)  return `$${(v / 1e6).toFixed(2)}M`;
  return `$${v.toLocaleString()}`;
}

function _fmtPct(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function _fmtDecimal(n, d = 2) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(d) : "—";
}

function _siSet(id, text, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text ?? "—";
  if (cls) el.className = `si-val ${cls}`;
}

function _makeChips(tickers, container, onClick) {
  if (!container) return;
  if (!tickers?.length) {
    container.innerHTML = `<span class="si-empty">None identified</span>`;
    return;
  }
  container.innerHTML = tickers.map(t =>
    `<span class="si-chip" data-ticker="${esc(t)}">${esc(t)}</span>`
  ).join("");
  container.querySelectorAll(".si-chip").forEach(el => {
    el.addEventListener("click", () => onClick(el.dataset.ticker));
  });
}

function renderStockInfo(info) {
  if (!info) return;

  _siSet("si-name", info.name || info.ticker);
  const secEl = document.getElementById("si-sector");
  if (secEl) {
    const sector = info.sector || "";
    const ind    = info.industry || "";
    secEl.textContent = [sector, ind].filter(Boolean).join(" · ") || "—";
  }

  // Fundamentals grid
  _siSet("si-mktcap",  _fmtLarge(info.market_cap));
  _siSet("si-pe",   _fmtDecimal(info.pe_ratio));
  _siSet("si-fpe",  _fmtDecimal(info.forward_pe));
  _siSet("si-peg",  _fmtDecimal(info.peg_ratio));
  _siSet("si-eps",  _fmtDecimal(info.eps_trailing));
  _siSet("si-rev",  _fmtLarge(info.revenue));
  _siSet("si-gm",   info.gross_margin   != null ? _fmtPct(info.gross_margin)    : "—");
  _siSet("si-nm",   info.profit_margin  != null ? _fmtPct(info.profit_margin)   : "—");
  _siSet("si-beta", _fmtDecimal(info.beta));
  _siSet("si-div",  info.dividend_yield != null ? _fmtPct(info.dividend_yield)  : "—");
  _siSet("si-52h",  info.week52_high    != null ? `$${Number(info.week52_high).toFixed(2)}`  : "—");
  _siSet("si-52l",  info.week52_low     != null ? `$${Number(info.week52_low).toFixed(2)}`   : "—");
  _siSet("si-roe",  info.return_on_equity != null ? _fmtPct(info.return_on_equity) : "—");

  const rec = (info.recommendation || "").toLowerCase();
  const recEl = document.getElementById("si-rec");
  if (recEl) {
    recEl.textContent = rec || "—";
    recEl.className   = `si-val ${rec.includes("buy") ? "pos" : rec.includes("sell") ? "neg" : ""}`;
  }

  const descEl = document.getElementById("si-desc");
  if (descEl) descEl.textContent = info.description || "No description available.";

  // Peers tab
  _makeChips(info.competitors,     document.getElementById("si-competitors"), switchTicker);
  _makeChips(info.similar_tickers, document.getElementById("si-similar"),     switchTicker);

  // Ecosystem tab
  _makeChips(info.depends_on,   document.getElementById("si-depends-on"),   t => switchTicker(t));
  _makeChips(info.depended_by,  document.getElementById("si-depended-by"),  t => switchTicker(t));
}

async function loadStockInfo(ticker) {
  try {
    const r = await fetch(`${BACKEND}/stock_info/${encodeURIComponent(ticker)}`, {
      signal: AbortSignal.timeout(20000),
    });
    if (!r.ok) return;
    const data = await r.json();
    renderStockInfo(data);
  } catch (e) {
    console.warn("loadStockInfo", e);
  }
}

// Tab switching inside the Stock Info panel
document.querySelectorAll(".si-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll(".si-tab").forEach(b => b.classList.remove("si-tab-active"));
    document.querySelectorAll(".si-section").forEach(s => s.classList.remove("si-section-active"));
    btn.classList.add("si-tab-active");
    const section = document.getElementById(`si-${tab}`);
    if (section) section.classList.add("si-section-active");
  });
});

// ── Live stock quote (NBBO / last vs prev close) ───────────────────────────────

function quoteSessionStripLabel(session) {
  if (!session) return { text: "", title: "" };
  if (session === "pre") return { text: "PRE", title: "Pre-market (extended hours)" };
  if (session === "post") return { text: "POST", title: "After hours (extended session)" };
  if (session === "regular") return { text: "", title: "Regular trading hours" };
  return { text: "", title: session };
}

async function pollQuote() {
  const t = activeTicker;
  if (!t) return;
  const elBid   = document.getElementById("sq-bid");
  const elAsk   = document.getElementById("sq-ask");
  const elLast  = document.getElementById("sq-last");
  const elPct   = document.getElementById("sq-daypct");
  const elSrc   = document.getElementById("sq-src");
  const elSess  = document.getElementById("sq-session");
  // Price pill elements
  const ppTicker = document.getElementById("pp-ticker");
  const ppPrice  = document.getElementById("pp-price");
  const ppChange = document.getElementById("pp-change");
  try {
    const r = await fetch(`${BACKEND}/quote/${encodeURIComponent(t)}`, {
      signal: AbortSignal.timeout(6000),
    });
    if (!r.ok) return;
    const q = await r.json();

    // Spread strip
    if (elBid) elBid.textContent = q.bid  != null ? fmtNum(q.bid,  2) : "—";
    if (elAsk) elAsk.textContent = q.ask  != null ? fmtNum(q.ask,  2) : "—";
    if (elLast) elLast.textContent = q.last != null ? fmtNum(q.last, 2) : "—";

    const p = q.change_pct;
    const pNum = p != null && Number.isFinite(Number(p)) ? Number(p) : null;

    if (elPct) {
      if (pNum == null) {
        elPct.textContent = "—"; elPct.className = "sq-val";
      } else {
        elPct.textContent = `${pNum >= 0 ? "+" : ""}${pNum.toFixed(2)}%`;
        elPct.className = `sq-val ${pNum >= 0 ? "pos" : "neg"}`;
      }
    }
    if (elSrc) {
      const s = q.source ?? "";
      elSrc.textContent =
        s === "alpaca" ? "Alpaca"
        : s === "alphavantage" ? "Alpha Vantage"
        : s === "yfinance" ? "Yahoo"
        : s === "underlying_proxy" ? "Est."
        : "";
    }
    if (elSess) {
      const { text, title } = quoteSessionStripLabel(q.session);
      elSess.textContent = text;
      elSess.title = title;
    }

    const sdLast = document.getElementById("sd-last");
    if (sdLast && q.last != null && Number.isFinite(Number(q.last))) {
      const v = Number(q.last);
      sdLast.textContent = fmtNum(v, v >= 100 ? 2 : 4);
    }

    // ── Update topbar price pill ──
    if (ppTicker) ppTicker.textContent = t;
    if (ppPrice && q.last != null) {
      const prev = parseFloat(ppPrice.dataset.prev ?? "0");
      const cur  = Number(q.last);
      ppPrice.textContent = `$${fmtNum(cur, 2)}`;
      ppPrice.dataset.prev = cur;
      // Flash color on change
      if (prev && prev !== cur) {
        ppPrice.style.color = cur > prev ? "var(--green)" : "var(--red)";
        clearTimeout(ppPrice._colorTimer);
        ppPrice._colorTimer = setTimeout(() => {
          ppPrice.style.color = "";
        }, 1200);
      }
    }
    if (ppChange) {
      if (pNum == null) {
        ppChange.textContent = "—";
        ppChange.className = "";
      } else {
        // Try to compute dollar change from prev close
        const last  = Number(q.last ?? 0);
        const chgPct = pNum / 100;
        const prevClose = last / (1 + chgPct);
        const chgDollar = last - prevClose;
        ppChange.textContent = `${chgDollar >= 0 ? "+" : ""}${chgDollar.toFixed(2)} (${pNum >= 0 ? "+" : ""}${pNum.toFixed(2)}%)`;
        ppChange.className = pNum >= 0 ? "pos" : "neg";
      }
    }
  } catch { /* silent */ }
}

// ── WebSocket real-time connection ────────────────────────────────────────────

let _ws             = null;
let _wsConnected    = false;
let _wsRetryTimer   = null;
let _wsRetryMs      = 2000;

function _connectWS() {
  if (_ws && (_ws.readyState === WebSocket.CONNECTING || _ws.readyState === WebSocket.OPEN)) return;
  try {
    _ws = new WebSocket(WS_BACKEND);

    _ws.onopen = () => {
      _wsConnected = true;
      _wsRetryMs   = 2000;
      clearTimeout(_wsRetryTimer);
      const pill = document.getElementById("ws-indicator");
      if (pill) { pill.textContent = "● LIVE"; pill.className = "ws-indicator ws-live"; }
      _ws.send(JSON.stringify({ type: "ping" }));
    };

    _ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state") {
          // Map compact ws payload onto same shape updateMetrics expects
          updateMetrics({
            ticker:              msg.ticker,
            underlying_price:    msg.underlying_price,
            market_regime:       msg.market_regime,
            aggregate_sentiment: msg.aggregate_sentiment,
            circuit_breaker_tripped: msg.circuit_breaker,
            kill_switch_active:  msg.kill_switch,
            trader_decision:     msg.trader_decision,
            risk:                msg.risk,
            cash_balance:        msg.cash_balance,
            buying_power:        msg.buying_power,
            account_equity:      msg.account_equity,
            stock_positions:     msg.stock_positions,
            open_positions:      msg.open_positions,
            agent_runtime:       msg.agent_runtime,
            news_feed_enabled:   msg.news_feed_enabled,
          });
        }
      } catch { /* ignore malformed */ }
    };

    _ws.onerror = () => { /* handled by onclose */ };

    _ws.onclose = () => {
      _wsConnected = false;
      const pill = document.getElementById("ws-indicator");
      if (pill) { pill.textContent = "● POLL"; pill.className = "ws-indicator ws-poll"; }
      // Exponential back-off (cap 30 s)
      _wsRetryMs = Math.min(30000, _wsRetryMs * 1.5);
      _wsRetryTimer = setTimeout(_connectWS, _wsRetryMs);
    };
  } catch (e) {
    _wsRetryTimer = setTimeout(_connectWS, _wsRetryMs);
  }
}

// ── Backend polling (fallback when WS is disconnected) ───────────────────────

let _pollStateCount = 0;
async function pollState() {
  // Always run the very first poll regardless of WS status (WS may not be open yet)
  if (_wsConnected && _pollStateCount > 0) return;
  _pollStateCount++;
  try {
    const r = await fetch(`${BACKEND}/state`, { signal: AbortSignal.timeout(4000) });
    if (!r.ok) return;
    const s = await r.json();
    updateMetrics(s);
  } catch { /* silent */ }
}

async function pollReasoningLog() {
  try {
    const r = await fetch(`${BACKEND}/reasoning_log?tail=200`, {
      signal: AbortSignal.timeout(12000),
    });
    if (!r.ok) return;
    const entries = await r.json();
    entries.slice(-100).forEach(appendReasoningEntry);
  } catch { /* silent */ }
}

/** Fallback if `/state` is an older backend without `agent_runtime`. */
async function pollAgentStatus() {
  try {
    const r = await fetch(`${BACKEND}/agent_status`, { signal: AbortSignal.timeout(3000) });
    if (!r.ok) return;
    const st = await r.json();
    renderAgentStatus(st);
  } catch { /* silent */ }
}

function renderAgentStatus(st) {
  const el = document.getElementById("agent-status");
  if (!el) return;

  const inProgress = !!st.in_progress;
  const lastOkAge  = typeof st.age_since_success_s === "number" ? st.age_since_success_s : null;
  const lastErrAge = typeof st.age_since_error_s === "number" ? st.age_since_error_s : null;
  const decision   = st.last_trader_decision || "--";
  const cyclesTotal = typeof st.cycles_total === "number" ? st.cycles_total : 0;

  let cls = "agent-status agent-status-unknown";
  let label = `AGENTS: ${decision}`;

  if (inProgress) {
    cls = "agent-status agent-status-running";
    label = "AGENTS: RUN";
  } else if (cyclesTotal === 0) {
    cls = "agent-status agent-status-unknown";
    label = "AGENTS: IDLE";
  } else if (lastErrAge !== null && lastErrAge < 300) {
    cls = "agent-status agent-status-error";
    const hint = shortAgentErrorHint(st.last_error);
    label = hint ? `AGENTS: ERR · ${hint}` : "AGENTS: ERR";
  } else if (lastOkAge !== null && lastOkAge <= 300) {
    cls = "agent-status agent-status-ok";
    label = `AGENTS: ${decision}`;
  } else if (lastOkAge !== null && lastOkAge > 300) {
    cls = "agent-status agent-status-stale";
    label = "AGENTS: STALE";
  }

  el.className = cls;
  el.textContent = label;

  const parts = [];
  if (typeof st.cycles_total === "number") parts.push(`cycles=${st.cycles_total} ok=${st.cycles_ok} err=${st.cycles_error}`);
  if (typeof st.last_cycle_duration_s === "number" && st.last_cycle_duration_s) parts.push(`last=${st.last_cycle_duration_s.toFixed(2)}s`);
  if (lastOkAge !== null) parts.push(`ok_age=${fmtAge(lastOkAge)}`);
  if (lastErrAge !== null) parts.push(`err_age=${fmtAge(lastErrAge)}`);
  if (st.last_error) parts.push(`last_error=${String(st.last_error).slice(0, 140)}`);
  el.title = parts.join(" · ");
}

function fmtAge(s) {
  if (s < 10) return `${s.toFixed(1)}s`;
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

/** First token of last_error (e.g. exception name) for the status pill; full text stays in title. */
function shortAgentErrorHint(lastError) {
  if (lastError == null || lastError === "") return "";
  const s = String(lastError).replace(/\s+/g, " ").trim();
  const head = s.split(":", 1)[0];
  if (head.length <= 36) return head;
  return `${head.slice(0, 33)}…`;
}

// ── Tauri event listener ──────────────────────────────────────────────────────

async function initTauriEvents() {
  if (!_tauri) return;
  try {
    await listen("agent_event", ({ payload }) => appendReasoningEntry(payload));
    const initial = await invoke("get_reasoning_log");
    if (Array.isArray(initial)) initial.slice(-100).forEach(appendReasoningEntry);
  } catch (e) {
    console.warn("Tauri events unavailable:", e);
  }
}

// ── Go Bar ────────────────────────────────────────────────────────────────────

document.getElementById("gobar").addEventListener("keydown", e => {
  if (e.key !== "Enter") return;
  const cmd = e.target.value.trim().toUpperCase();
  e.target.value = "";

  if (/^[A-Z]{1,5}$/.test(cmd)) {
    switchTicker(cmd);
  } else if (cmd === "KILL") {
    handleKillSwitch();
  } else if (cmd === "CLEAR") {
    document.getElementById("reasoning-panel").innerHTML = "";
    _seenReasoning.clear();
  } else if (cmd.startsWith("SORT ")) {
    const s = cmd.slice(5).toLowerCase();
    if (["iv","pc","oi","ticker","price","chg"].includes(s)) {
      scannerSort = s;
      document.getElementById("scanner-sort").value = s;
      pollScanner();
    }
  }
});

// ── Kill switch ───────────────────────────────────────────────────────────────

document.getElementById("kill-btn").addEventListener("click", () => {
  if (!confirm("⚡ Activate Kill Switch? All trading will halt.")) return;
  handleKillSwitch();
});

async function handleKillSwitch() {
  setCircuitBreakerUI(true);
  try {
    if (_tauri) await invoke("trigger_kill_switch");
    else        await fetch(`${BACKEND}/kill_switch`, { method: "POST" });
  } catch {}
  appendReasoningEntry({
    timestamp: new Date().toISOString(),
    agent: "UI", action: "ABORT",
    reasoning: "Kill switch triggered by operator.",
  });
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s ?? "")
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function fmtNum(v, decimals) {
  const n = parseFloat(v);
  return isNaN(n) ? "—" : n.toFixed(decimals);
}

// ── Order Ticket ──────────────────────────────────────────────────────────────

// Tab switching (STOCK / OPTION)
document.querySelectorAll(".ot-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.otab;
    document.querySelectorAll(".ot-tab").forEach(b => b.classList.remove("ot-tab-active"));
    document.querySelectorAll("#order-ticket .ot-section").forEach(s => s.classList.remove("ot-section-active"));
    btn.classList.add("ot-tab-active");
    const sec = document.getElementById(`ot-${tab}`);
    if (sec) sec.classList.add("ot-section-active");
  });
});

// ── Stock order ticket ──────────────────────────────────────────────────────

let _otStockSide = "buy";

function _otStockSideUpdate() {
  const isBuy = _otStockSide === "buy";
  document.getElementById("ot-s-side-buy").classList.toggle("ot-side-active", isBuy);
  document.getElementById("ot-s-side-sell").classList.toggle("ot-side-active", !isBuy);
  document.getElementById("ot-s-side-buy").classList.toggle("ot-btn-buy", isBuy);
  document.getElementById("ot-s-side-sell").classList.toggle("ot-btn-sell", !isBuy);
  const btn = document.getElementById("ot-s-submit");
  btn.textContent = isBuy ? "PLACE BUY ORDER" : "PLACE SELL ORDER";
  btn.className   = `ot-submit ${isBuy ? "ot-submit-buy" : "ot-submit-sell"}`;
}

document.getElementById("ot-s-side-buy").addEventListener("click",  () => { _otStockSide = "buy";  _otStockSideUpdate(); });
document.getElementById("ot-s-side-sell").addEventListener("click", () => { _otStockSide = "sell"; _otStockSideUpdate(); });

document.getElementById("ot-s-type").addEventListener("change", e => {
  document.getElementById("ot-s-lmt-row").style.display =
    e.target.value === "limit" ? "" : "none";
});

document.getElementById("ot-s-submit").addEventListener("click", async () => {
  const ticker  = document.getElementById("ot-s-ticker").value.trim().toUpperCase();
  const qty     = parseFloat(document.getElementById("ot-s-qty").value);
  const oType   = document.getElementById("ot-s-type").value;
  const lmt     = oType === "limit" ? parseFloat(document.getElementById("ot-s-lmt").value) : null;
  const tif     = document.getElementById("ot-s-tif").value;
  const statusEl = document.getElementById("ot-s-status");

  if (!ticker || !qty || qty <= 0) {
    statusEl.textContent = "⚠ Enter a valid ticker and quantity.";
    statusEl.className   = "ot-status ot-status-err";
    return;
  }
  if (oType === "limit" && (!lmt || lmt <= 0)) {
    statusEl.textContent = "⚠ Enter a valid limit price.";
    statusEl.className   = "ot-status ot-status-err";
    return;
  }

  const btn = document.getElementById("ot-s-submit");
  btn.disabled    = true;
  statusEl.textContent = "Submitting…";
  statusEl.className   = "ot-status";

  try {
    const body = { ticker, side: _otStockSide, qty, order_type: oType, tif };
    if (lmt) body.limit_price = lmt;
    const r = await fetch(`${BACKEND}/order/stock`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
      signal:  AbortSignal.timeout(15000),
    });
    const data = await r.json();
    if (!r.ok) {
      const msg = data?.detail?.detail || data?.detail || JSON.stringify(data);
      statusEl.textContent = `✗ ${msg}`;
      statusEl.className   = "ot-status ot-status-err";
      showToast(`Order rejected: ${msg}`, "err");
    } else {
      const st = (data.status || "submitted").toUpperCase();
      statusEl.textContent = `✓ ${_otStockSide.toUpperCase()} ${qty} ${ticker} — ${st}`;
      statusEl.className   = "ot-status ot-status-ok";
      showToast(`${_otStockSide.toUpperCase()} ${qty}× ${ticker} → ${st}`, "ok");
      pollOrders();
      refreshPositions();   // refresh stock positions table
    }
  } catch (e) {
    statusEl.textContent = `✗ ${e.message}`;
    statusEl.className   = "ot-status ot-status-err";
    showToast(`Network error: ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
});

// ── Option order ticket ────────────────────────────────────────────────────

let _otOptSide = "buy";

function _otOptSideUpdate() {
  const isBuy = _otOptSide === "buy";
  document.getElementById("ot-o-side-buy").classList.toggle("ot-side-active", isBuy);
  document.getElementById("ot-o-side-sell").classList.toggle("ot-side-active", !isBuy);
  document.getElementById("ot-o-side-buy").classList.toggle("ot-btn-buy", isBuy);
  document.getElementById("ot-o-side-sell").classList.toggle("ot-btn-sell", !isBuy);
  const btn = document.getElementById("ot-o-submit");
  btn.textContent = isBuy ? "PLACE BUY ORDER" : "PLACE SELL ORDER";
  btn.className   = `ot-submit ${isBuy ? "ot-submit-buy" : "ot-submit-sell"}`;
}

document.getElementById("ot-o-side-buy").addEventListener("click",  () => { _otOptSide = "buy";  _otOptSideUpdate(); });
document.getElementById("ot-o-side-sell").addEventListener("click", () => { _otOptSide = "sell"; _otOptSideUpdate(); });

document.getElementById("ot-o-type").addEventListener("change", e => {
  document.getElementById("ot-o-lmt-row").style.display =
    e.target.value === "limit" ? "" : "none";
});

/** Called when the user clicks an options chain row. Pre-fills the option ticket. */
export function prefillOptionTicket(contract) {
  // Switch to option tab
  document.querySelectorAll(".ot-tab").forEach(b => b.classList.remove("ot-tab-active"));
  document.querySelectorAll("#order-ticket .ot-section").forEach(s => s.classList.remove("ot-section-active"));
  const optTab = document.querySelector('.ot-tab[data-otab="option"]');
  if (optTab) optTab.classList.add("ot-tab-active");
  const optSec = document.getElementById("ot-option");
  if (optSec) optSec.classList.add("ot-section-active");

  document.getElementById("ot-o-symbol").value = contract.symbol || "";
  const mid = (contract.bid != null && contract.ask != null)
    ? ((contract.bid + contract.ask) / 2).toFixed(2)
    : "";
  if (mid) document.getElementById("ot-o-lmt").value = mid;

  const preEl = document.getElementById("ot-opt-preview");
  if (preEl) {
    const cp  = contract.right === "CALL" ? "C" : "P";
    const exp = fmtExpiry(contract.expiry || "");
    const iv  = contract.iv != null ? `IV ${(contract.iv * 100).toFixed(1)}%` : "";
    const dlt = contract.delta != null ? `Δ ${contract.delta.toFixed(3)}` : "";
    preEl.innerHTML = `
      <span class="${cp === "C" ? "call" : "put"}">${cp}</span>
      <span class="ot-opt-strike">$${Number(contract.strike || 0).toFixed(0)}</span>
      <span class="ot-opt-exp">${exp}</span>
      <span class="ot-opt-meta">${[iv, dlt].filter(Boolean).join(" · ")}</span>
      <span class="ot-opt-mid">mid $${mid || "—"}</span>`;
  }

  document.getElementById("ot-s-status") && (document.getElementById("ot-o-status").textContent = "");
  // Scroll order ticket into view
  document.getElementById("order-ticket")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

document.getElementById("ot-o-submit").addEventListener("click", async () => {
  const symbol  = document.getElementById("ot-o-symbol").value.trim();
  const qty     = parseInt(document.getElementById("ot-o-qty").value, 10);
  const oType   = document.getElementById("ot-o-type").value;
  const lmt     = oType === "limit" ? parseFloat(document.getElementById("ot-o-lmt").value) : null;
  const tif     = document.getElementById("ot-o-tif").value;
  const statusEl = document.getElementById("ot-o-status");

  if (!symbol) {
    statusEl.textContent = "⚠ Select a contract from the options chain.";
    statusEl.className   = "ot-status ot-status-err";
    return;
  }
  if (!qty || qty <= 0) {
    statusEl.textContent = "⚠ Enter a valid contract quantity.";
    statusEl.className   = "ot-status ot-status-err";
    return;
  }
  if (oType === "limit" && (!lmt || lmt <= 0)) {
    statusEl.textContent = "⚠ Enter a valid limit price.";
    statusEl.className   = "ot-status ot-status-err";
    return;
  }

  const btn = document.getElementById("ot-o-submit");
  btn.disabled    = true;
  statusEl.textContent = "Submitting…";
  statusEl.className   = "ot-status";

  try {
    const body = { symbol, side: _otOptSide, qty, order_type: oType, tif };
    if (lmt) body.limit_price = lmt;
    const r = await fetch(`${BACKEND}/order/option`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
      signal:  AbortSignal.timeout(15000),
    });
    const data = await r.json();
    if (!r.ok) {
      const msg = data?.detail?.detail || data?.detail || JSON.stringify(data);
      statusEl.textContent = `✗ ${msg}`;
      statusEl.className   = "ot-status ot-status-err";
      showToast(`Option order rejected: ${msg}`, "err");
    } else {
      const st = (data.status || "submitted").toUpperCase();
      statusEl.textContent = `✓ ${_otOptSide.toUpperCase()} ${qty}× ${symbol} — ${st}`;
      statusEl.className   = "ot-status ot-status-ok";
      showToast(`${_otOptSide.toUpperCase()} ${qty}× ${symbol} → ${st}`, "ok");
      pollOrders();
      refreshPositions();   // refresh option positions table
    }
  } catch (e) {
    statusEl.textContent = `✗ ${e.message}`;
    statusEl.className   = "ot-status ot-status-err";
    showToast(`Network error: ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
});

// Pre-fill ticket when activeTicker changes (for stock tab)
const _origActiveSetter = Object.getOwnPropertyDescriptor(window, "activeTicker");
function _syncStockTicker(t) {
  const el = document.getElementById("ot-s-ticker");
  if (el && el.value !== t) el.value = t;
}

// ── Order Blotter ──────────────────────────────────────────────────────────

function _blotterStatusCls(status) {
  const s = (status || "").toLowerCase();
  if (["filled", "partially_filled"].includes(s)) return "pos";
  if (["canceled", "expired", "rejected", "replaced"].includes(s)) return "neg";
  if (["accepted", "new", "pending_new", "held"].includes(s)) return "pending-fill";
  return "muted-text";
}

// ── LLM backend status ────────────────────────────────────────────────────────
async function pollLlmStatus() {
  try {
    const r = await fetch(`${BACKEND}/llm/status`, { signal: AbortSignal.timeout(4000) });
    if (!r.ok) return;
    const d = await r.json();
    _renderLlmBadge(d);
  } catch { /* silent */ }
}

function _renderLlmBadge(d) {
  const badge = document.getElementById("llm-backend-badge");
  if (!badge) return;
  const cloudOn = Boolean(d.openrouter_enabled);
  const isLocal = d.primary === "local" && d.local_healthy;
  const isCooldown = d.primary === "local" && !d.local_healthy && d.cooldown_remaining_s > 0;
  const lastUsed = d.last_backend_used || "unknown";

  if (d.primary === "local") {
    if (isLocal) {
      badge.textContent = cloudOn ? "LLM: LOCAL" : "LLM: LOCAL ONLY";
      badge.className = "badge llm-badge-local";
      badge.title = cloudOn
        ? `llama.cpp is online at ${d.local_base_url}. Last used: ${lastUsed}`
        : `Local-only mode (OpenRouter off). ${d.local_base_url} — last used: ${lastUsed}`;
    } else if (!cloudOn) {
      const mins = Math.ceil((d.cooldown_remaining_s || 0) / 60);
      badge.textContent = mins > 0 ? `LLM: DOWN (${mins}m)` : "LLM: DOWN";
      badge.className = "badge llm-badge-fallback";
      badge.title = `llama.cpp unreachable; no cloud fallback (OPENROUTER_ENABLED=false). ${d.local_base_url}`;
    } else if (isCooldown) {
      const mins = Math.ceil(d.cooldown_remaining_s / 60);
      badge.textContent = `LLM: CLOUD (local down ${mins}m)`;
      badge.className = "badge llm-badge-fallback";
      badge.title = `llama.cpp unreachable. Using OpenRouter. Retry in ${d.cooldown_remaining_s}s.`;
    } else {
      badge.textContent = "LLM: CLOUD";
      badge.className = "badge llm-badge-cloud";
      badge.title = `Primary=local but local is offline. Using OpenRouter.`;
    }
  } else {
    badge.textContent = "LLM: CLOUD";
    badge.className = "badge llm-badge-cloud";
    badge.title = `OpenRouter is primary (LLAMA_LOCAL_PRIMARY=false). Last used: ${lastUsed}`;
  }
}

// ── Market clock ──────────────────────────────────────────────────────────────
let _marketClock = { is_open: null, next_open: null, next_close: null };

async function pollMarketClock() {
  try {
    const r = await fetch(`${BACKEND}/market/clock`, { signal: AbortSignal.timeout(6000) });
    if (!r.ok) return;
    const data = await r.json();
    _marketClock = data;
    _renderMarketClock(data);
  } catch { /* silent */ }
}

function _renderMarketClock(data) {
  const badge = document.getElementById("market-clock-badge");
  if (!badge) return;
  if (data.is_open === null || data.is_open === undefined) {
    badge.textContent = "MKT: --";
    badge.className = "badge market-closed";
    return;
  }
  if (data.is_open) {
    // Market is open — show close time
    const closeStr = data.next_close
      ? new Date(data.next_close).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York" })
      : "";
    badge.textContent = closeStr ? `MKT OPEN · closes ${closeStr}` : "MKT: OPEN";
    badge.className = "badge market-open";
    badge.title = `NYSE open. Closes at ${data.next_close}`;
  } else {
    // Market is closed — show next open
    const nextOpen = data.next_open ? new Date(data.next_open) : null;
    const today    = new Date();
    const isToday  = nextOpen && nextOpen.toDateString() === today.toDateString();
    const dayLabel = nextOpen
      ? (isToday ? "today" : nextOpen.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" }))
      : "";
    const timeLabel = nextOpen
      ? nextOpen.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York" })
      : "";
    badge.textContent = `MKT CLOSED · opens ${dayLabel} ${timeLabel}`.trim();
    badge.className = "badge market-closed";
    badge.title = `NYSE closed. Next open: ${data.next_open}`;
  }
}

function _marketStatusLabel() {
  if (_marketClock.is_open) return "awaiting fill";
  if (_marketClock.next_open) {
    const nextOpen = new Date(_marketClock.next_open);
    const today    = new Date();
    const isToday  = nextOpen.toDateString() === today.toDateString();
    const day  = isToday ? "today" : nextOpen.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
    const time = nextOpen.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York" });
    return `market opens ${day} ${time} ET`;
  }
  return "market closed";
}

function _updatePendingNotice(orders) {
  // If we have accepted/pending orders but no positions, show a notice in the position tables
  const pending = (orders || []).filter(o =>
    ["accepted", "new", "pending_new", "held"].includes((o.status || "").toLowerCase())
  );
  const stockPending = pending.filter(o => (o.asset_class || "").includes("equity"));
  const optPending   = pending.filter(o => (o.asset_class || "").includes("option"));

  const stockBody = document.getElementById("stock-positions-body");
  const optBody   = document.getElementById("positions-body");
  const mktLabel  = _marketStatusLabel();

  if (stockPending.length && stockBody?.querySelector(".empty-row")) {
    stockBody.innerHTML = stockPending.map(o =>
      `<tr><td colspan="5" class="pending-fill" style="text-align:center;font-style:italic">
        ⏳ ${o.qty} ${esc(o.symbol)} ${(o.side||'').toUpperCase()} — ${mktLabel}
      </td></tr>`
    ).join("");
  }
  if (optPending.length && optBody?.querySelector(".empty-row")) {
    optBody.innerHTML = optPending.map(o =>
      `<tr><td colspan="7" class="pending-fill" style="text-align:center;font-style:italic">
        ⏳ ${o.qty} ${esc(o.symbol)} ${(o.side||'').toUpperCase()} — ${mktLabel}
      </td></tr>`
    ).join("");
  }
}

function renderOrderBlotter(orders) {
  const tbody = document.getElementById("orders-body");
  if (!tbody) return;
  _updatePendingNotice(orders);
  if (!orders?.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-row">No orders</td></tr>`;
    return;
  }
  tbody.innerHTML = orders.slice(0, 50).map(o => {
    const t    = o.submitted_at || o.created_at || "";
    const time = t ? new Date(t).toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit" }) : "—";
    const sym  = (o.symbol || "").slice(0, 20);
    const side = (o.side || "").toUpperCase();
    const sideCls = side === "BUY" ? "call" : "put";
    const qty  = o.filled_qty && Number(o.filled_qty) > 0
      ? `${o.filled_qty}/${o.qty}`
      : o.qty || "—";
    const otype = (o.type || "").toUpperCase().slice(0, 3);
    const price = o.filled_avg_price
      ? `$${Number(o.filled_avg_price).toFixed(2)}`
      : o.limit_price
        ? `$${Number(o.limit_price).toFixed(2)}`
        : "MKT";
    const status = (o.status || "").toUpperCase();
    const stCls  = _blotterStatusCls(o.status);
    const oid = esc(o.id || "");
    const showCancel = ["new", "partially_filled", "accepted", "pending_new"].includes(
      (o.status || "").toLowerCase()
    );
    return `<tr>
      <td>${time}</td>
      <td>${esc(sym)}</td>
      <td class="${sideCls}">${side}</td>
      <td>${qty}</td>
      <td>${otype}</td>
      <td>${price}</td>
      <td class="${stCls}">${status}</td>
      <td>${showCancel ? `<button class="blotter-cancel-btn" data-oid="${oid}">✕</button>` : ""}</td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll(".blotter-cancel-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const r = await fetch(`${BACKEND}/order/${encodeURIComponent(btn.dataset.oid)}`, {
          method: "DELETE", signal: AbortSignal.timeout(10000),
        });
        if (r.ok) pollOrders();
        else btn.textContent = "err";
      } catch { btn.disabled = false; }
    });
  });
}

async function pollOrders() {
  try {
    const r = await fetch(`${BACKEND}/orders?limit=30`, { signal: AbortSignal.timeout(8000) });
    if (!r.ok) return;
    renderOrderBlotter(await r.json());
  } catch { /* silent */ }
}

/**
 * Force-syncs positions from broker and re-renders both position tables.
 * Called immediately after any successful order placement.
 * The server waits 2s after the order before pulling from Alpaca,
 * so we poll twice: once at 3s (fast feedback) and once at 8s (fill confirmation).
 */
async function refreshPositions() {
  const _sync = async () => {
    try {
      const r = await fetch(`${BACKEND}/positions/refresh`, {
        method: "POST",
        signal: AbortSignal.timeout(12000),
      });
      if (!r.ok) return;
      const data = await r.json();
      if (data.stock_positions !== undefined) {
        renderStockPositions(data.stock_positions);
      }
      if (data.open_positions !== undefined) {
        renderPositions(data.open_positions);
      }
      // Also update account balances if returned
      if (data.account_equity != null) {
        setUsdCell("acct-equity", data.account_equity);
      }
      if (data.cash_balance != null) {
        setUsdCell("acct-cash", data.cash_balance);
      }
      if (data.buying_power != null) {
        setUsdCell("acct-bp", data.buying_power);
      }
    } catch { /* silent */ }
  };
  // First refresh at 3s (order registered quickly on paper account)
  setTimeout(_sync, 3000);
  // Second refresh at 9s (confirm fill status)
  setTimeout(_sync, 9000);
}

// Blotter refresh with spinner animation
document.getElementById("refresh-orders-btn")?.addEventListener("click", async () => {
  const btn = document.getElementById("refresh-orders-btn");
  btn?.classList.add("spinning");
  await pollOrders();
  setTimeout(() => btn?.classList.remove("spinning"), 600);
});

// Positions sync button
document.getElementById("refresh-positions-btn")?.addEventListener("click", async () => {
  const btn = document.getElementById("refresh-positions-btn");
  if (btn) { btn.textContent = "⟳ …"; btn.disabled = true; }
  try {
    const r = await fetch(`${BACKEND}/positions/refresh`, {
      method: "POST",
      signal: AbortSignal.timeout(12000),
    });
    if (r.ok) {
      const data = await r.json();
      if (data.stock_positions !== undefined) renderStockPositions(data.stock_positions);
      if (data.open_positions  !== undefined) renderPositions(data.open_positions);
      if (data.cash_balance    != null) setUsdCell("acct-cash",   data.cash_balance);
      if (data.buying_power    != null) setUsdCell("acct-bp",     data.buying_power);
      if (data.account_equity  != null) setUsdCell("acct-equity", data.account_equity);
      showToast("Positions synced from broker", "ok");
    } else {
      showToast("Sync failed — check server logs", "err");
    }
  } catch (e) {
    showToast("Sync error: " + e.message, "err");
  } finally {
    if (btn) { btn.textContent = "⟳ SYNC"; btn.disabled = false; }
  }
});

// ── Chart polling (OHLC refresh; quote strip uses pollQuote) ───────────────────

/** Presets where bars update slowly — poll less often to save API quota. */
const CHART_DAILY_HISTORY_TF = new Set(["5D", "1M", "3M", "6M", "1Y", "1Day"]);

function _chartPollIntervalMs(tf) {
  return CHART_DAILY_HISTORY_TF.has(tf) ? 90000 : 15000;
}

function scheduleChartPoll() {
  if (_chartPollTimer) {
    clearInterval(_chartPollTimer);
    _chartPollTimer = null;
  }
  const tfSel = document.getElementById("ticker-chart-tf");
  const tick = () => {
    if (document.visibilityState !== "visible") return;
    const tf = tfSel?.value || "5D";
    const intraday = !CHART_DAILY_HISTORY_TF.has(tf);
    loadTickerBars(activeTicker, tf, {
      preserveRange: true,
      followRealtime: intraday,
    });
  };
  const ms = _chartPollIntervalMs(tfSel?.value || "5D");
  _chartPollTimer = setInterval(tick, ms);
}

window.__scheduleChartPoll = scheduleChartPoll;

// ── Bootstrap ─────────────────────────────────────────────────────────────────

startClock();
initTauriEvents();

window.__refreshTickerChart = () => {
  const { tf, limit } = _resolveBackendTf();
  loadTickerBars(activeTicker, tf, { limit: limit ?? undefined });
  scheduleChartPoll();
};
// Use a getter so window.__activeTicker always reflects the current module-level variable
Object.defineProperty(window, "__activeTicker", { get: () => activeTicker, configurable: true });

function bootstrapCharts() {
  _wireResizeHandles();      // must be first — modifies grid before charts init
  initTerminalCharts();
  resizeTerminalCharts();
  // bust=true clears any stale 1-bar cache from a previous server run
  const _initTfRes = _resolveBackendTf();
  loadTickerBars(activeTicker, _initTfRes.tf, { bust: true, limit: _initTfRes.limit ?? undefined });
  loadPortfolioSeries();
  pollQuote();
  loadStockInfo(activeTicker);
  _wireChartStripButtons();
  _wireChainToolbar();
  _wireAllCollapsibles();
  _wireKeyboardShortcuts();
  scheduleChartPoll();
}

requestAnimationFrame(() => {
  bootstrapCharts();
  // Second pass after layout settles — re-read real pixel dimensions
  setTimeout(() => {
    resizeTerminalCharts();
    loadTickerBars(activeTicker, document.getElementById("ticker-chart-tf")?.value || "5D", { bust: true });
    loadPortfolioSeries();
  }, 400);
});

// Initial data load
_connectWS();          // start WebSocket (falls back to polling on error)
pollState();           // immediate HTTP load regardless
pollMarketClock();     // real-time NYSE open/close status
pollLlmStatus();       // LLM backend badge (local vs cloud)
pollScanner();
pollReasoningLog();
fetchOptionsChain(activeTicker);
pollAgentStatus();
pollOrders();
_syncStockTicker(activeTicker);

// Auto-sync positions from broker shortly after page load
// (gives the server time to complete its initial Alpaca sync)
setTimeout(async () => {
  try {
    const r = await fetch(`${BACKEND}/positions/refresh`, {
      method: "POST",
      signal: AbortSignal.timeout(12000),
    });
    if (r.ok) {
      const data = await r.json();
      if (data.stock_positions !== undefined) renderStockPositions(data.stock_positions);
      if (data.open_positions  !== undefined) renderPositions(data.open_positions);
      if (data.cash_balance    != null) setUsdCell("acct-cash",   data.cash_balance);
      if (data.buying_power    != null) setUsdCell("acct-bp",     data.buying_power);
      if (data.account_equity  != null) setUsdCell("acct-equity", data.account_equity);
    }
  } catch { /* silent — server may not support this endpoint yet */ }
}, 4000);

// Polling intervals (pollState skips when WS is live)
setInterval(pollState,           2000);   // metrics fallback
setInterval(pollMarketClock,    60000);   // NYSE clock (every minute is enough)
setInterval(pollLlmStatus,      30000);   // LLM backend badge (updates after cooldown expires)
setInterval(pollScanner,        10000);   // full scanner (options metrics + quotes)
setInterval(pollScannerQuotes,   1000);   // live last / day % only
setInterval(pollReasoningLog,    4000);   // XAI log
setInterval(loadPortfolioSeries, 15000);  // portfolio / greeks chart
setInterval(pollQuote,            1000);  // quote strip + price pill + stock strip LAST (pre/post aware)
setInterval(pollOrders,          15000);  // order blotter
