/**
 * Agentic Trading — Atlas UI controller
 *
 * Layout (DOM ids via ui_bindings.js → atlas-*):
 *   col-scanner  → S&P 500 scanner
 *   col-left     → Chart, chain, stock info
 *   col-centre   → Account, P&L, positions, portfolio chart, news
 *   col-right    → Order ticket, agent log, blotter
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
  updateChartBarSizeLabel,
} from "./charts.js";
import "./trading-viz-mount.tsx";
import { $ as el, WORKSPACE_COL_IDS } from "./ui_bindings.js";

// ── Tauri v2 API ──────────────────────────────────────────────────────────────
const _tauri  = window.__TAURI__;
const invoke  = _tauri?.core?.invoke  ?? (async () => ({}));
const listen  = _tauri?.event?.listen ?? (() => {});
const BACKEND    = "http://localhost:8000";
const WS_BACKEND = "ws://localhost:8000/ws";

// ── Fetch helpers (Tauri/WKWebView-safe timeouts) ─────────────────────────────
function fetchWithTimeout(url, init = {}, ms = 8000) {
  // AbortSignal.timeout is not supported in some WebViews; fall back safely.
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    return fetch(url, { ...init, signal: init.signal ?? AbortSignal.timeout(ms) });
  }
  const controller = new AbortController();
  const timer = setTimeout(() => {
    try { controller.abort(); } catch {}
  }, ms);
  return fetch(url, { ...init, signal: init.signal ?? controller.signal })
    .finally(() => clearTimeout(timer));
}

// ── Live toggles ──────────────────────────────────────────────────────────────
let _agentsLive = true;
function _setAgentsLive(on) {
  _agentsLive = Boolean(on);
  const btn = el("agent-live-btn");
  if (btn) {
    btn.setAttribute("aria-pressed", _agentsLive ? "true" : "false");
    btn.textContent = _agentsLive ? "LIVE" : "PAUSED";
    btn.title = _agentsLive ? "Live agent updates are ON" : "Live agent updates are PAUSED";
  }
}

// ── Light/Dark color mode (token overrides; persisted in localStorage) ────────
const COLOR_MODE_KEY = "att_color_mode";

function _applyColorMode(mode) {
  const m = (mode === "dark" || mode === "light") ? mode : "light";
  document.body.classList.toggle("theme-dark", m === "dark");
  try {
    localStorage.setItem(COLOR_MODE_KEY, m);
  } catch (_) { /* ignore */ }

  const btn = el("color-mode-btn");
  if (btn) {
    // Text is kept for fallback; primary UI is CSS-driven knob + icon
    btn.textContent = m === "dark" ? "DARK" : "LIGHT";
    btn.title = `Color mode: ${m}. Click to toggle (saved locally).`;
    btn.setAttribute("aria-pressed", m === "dark" ? "true" : "false");
    btn.setAttribute("aria-label", m === "dark" ? "Color mode: Dark" : "Color mode: Light");
    btn.dataset.mode = m;
  }
}

function _initColorMode() {
  let saved = "light";
  try {
    saved = localStorage.getItem(COLOR_MODE_KEY) || "light";
  } catch (_) { /* ignore */ }
  _applyColorMode(saved);
  el("color-mode-btn")?.addEventListener("click", () => {
    const next = document.body.classList.contains("theme-dark") ? "light" : "dark";
    _applyColorMode(next);
  });
}

// ── Experimental UI themes (body classes; persisted in localStorage) ───────────
const UI_THEME_KEY = "att_ui_theme";
/** Atlas is the only product shell; optional accent palettes via body[data-atlas-palette] */
const UI_THEMES = [
  { id: "atlas", label: "◆ ATLAS", palette: "default" },
  { id: "atlas-ember", label: "◇ EMBER", palette: "ember" },
  { id: "atlas-ice", label: "◇ ICE", palette: "ice" },
];

const _LEGACY_THEME_CLASSES = [
  "theme-controlroom", "theme-starshipos", "theme-hud", "theme-orchard",
  "theme-nuke", "theme-crt", "theme-neon", "theme-brutal",
];

function _applyUiTheme(themeId) {
  const t = UI_THEMES.find(x => x.id === themeId) || UI_THEMES[0];
  _LEGACY_THEME_CLASSES.forEach(c => document.body.classList.remove(c));
  document.body.classList.add("atlas");
  document.body.dataset.atlasPalette = t.palette || "default";
  try {
    localStorage.setItem(UI_THEME_KEY, t.id);
  } catch (_) { /* private mode */ }
  const btn = el("theme-cycle-btn");
  if (btn) {
    btn.textContent = t.label;
    btn.title = `Accent: ${t.id}. Click to cycle palettes (saved locally).`;
  }
}

function _initUiTheme() {
  let saved = "atlas";
  try {
    saved = localStorage.getItem(UI_THEME_KEY) || "atlas";
  } catch (_) { /* ignore */ }
  if (!UI_THEMES.some(x => x.id === saved)) saved = "atlas";
  _applyUiTheme(saved);
  el("theme-cycle-btn")?.addEventListener("click", () => {
    let cur = "atlas";
    try {
      cur = localStorage.getItem(UI_THEME_KEY) || "atlas";
    } catch (_) { /* ignore */ }
    const i = Math.max(0, UI_THEMES.findIndex(x => x.id === cur));
    _applyUiTheme(UI_THEMES[(i + 1) % UI_THEMES.length].id);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    _initColorMode();
    _initUiTheme();
    _wireControlRoomNav();
  });
} else {
  _initColorMode();
  _initUiTheme();
  _wireControlRoomNav();
}

// ── Active ticker state ───────────────────────────────────────────────────────
let activeTicker    = "SPY";
/** Used by the embedded trading-viz WebSocket to align with the terminal ticker on connect. */
window.__terminalActiveTicker = () => activeTicker;
let _chartPollTimer = null;
let scannerData     = [];   // latest scanner rows from /scanner
let scannerFilter   = "";
let scannerSort     = "iv";

/** From ``GET /quotes/benchmarks`` ``sections`` — drives Markets strip + scanner group headers. */
let _scannerBenchSections = null; // [{ id, label, tickers: string[] }]
let _benchmarkTickerSet = null; // Set<string> | null
/** Fingerprint of benchmark section structure; only then call ``renderScanner`` (avoids scroll jump every 2s). */
let _benchmarkMetaSig = "";

// ── Instant switching caches (client-side) ────────────────────────────────────
// These caches make ticker clicks feel instant by painting last-known data
// immediately, then refreshing in the background.
const _quoteCache     = new Map(); // ticker -> { q, at }
const _stockInfoCache = new Map(); // ticker -> { info, at }
const _optionsCache   = new Map(); // ticker -> { contracts, at }

function _now() { return Date.now(); }
function _getCache(map, t, maxAgeMs) {
  const v = map.get(t);
  if (!v) return null;
  if (maxAgeMs != null && (_now() - (v.at || 0)) > maxAgeMs) return null;
  return v;
}

// Stock info (fundamentals/peers/ecosystem) single-flight to prevent stale UI after fast clicking
let _stockInfoSeq = 0;
let _stockInfoController = null;

// Options chain single-flight + cancel on fast switching
let _optionsSeq = 0;
let _optionsController = null;

// Quote fetch cancel on fast switching (pollQuote also runs 1Hz)
let _quoteSeq = 0;
let _quoteController = null;

// ── Toast notification system ────────────────────────────────────────────────
const _toastContainer = () => el("toast-container");

function _ensureToastControls() {
  const tc = _toastContainer();
  if (!tc) return null;
  if (tc.dataset.controlsWired === "1") return tc;
  tc.dataset.controlsWired = "1";

  // Clear-all control (shown only when toasts exist).
  const clear = document.createElement("button");
  clear.type = "button";
  clear.id = "toast-clear";
  clear.className = "toast-clear";
  clear.textContent = "Clear";
  clear.hidden = true;
  clear.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    tc.querySelectorAll(".toast").forEach(n => n.remove());
    clear.hidden = true;
  });
  tc.prepend(clear);

  // Delegate close buttons.
  tc.addEventListener("click", (e) => {
    const btn = e.target.closest(".toast-close");
    if (!btn || !tc.contains(btn)) return;
    e.preventDefault();
    e.stopPropagation();
    const toast = btn.closest(".toast");
    toast?.remove();
    const any = tc.querySelector(".toast");
    clear.hidden = !any;
  });
  return tc;
}

function showToast(msg, type = "info", duration = 4000) {
  const icons = { ok: "✓", err: "✕", warn: "⚠", info: "ℹ" };
  const tc = _ensureToastControls();
  if (!tc) return;
  const t = document.createElement("div");
  t.className = `toast toast-${type}`;
  t.innerHTML = `
    <span class="toast-icon">${icons[type] ?? "ℹ"}</span>
    <span class="toast-msg">${msg}</span>
    <button type="button" class="toast-close" aria-label="Dismiss notification">✕</button>
  `;
  tc.appendChild(t);
  const clear = tc.querySelector("#toast-clear");
  if (clear) clear.hidden = false;
  setTimeout(() => {
    t.classList.add("toast-out");
    t.addEventListener("animationend", () => {
      t.remove();
      const tc = _toastContainer();
      const clear = tc?.querySelector?.("#toast-clear");
      if (clear) clear.hidden = !tc.querySelector(".toast");
    });
  }, duration);
}

// Expose for other modules (charts.js) to surface errors inside Tauri WebView
window.__showToast = showToast;

// Surface runtime errors (Atlas: helps diagnose missing bindings quickly)
window.addEventListener("error", (e) => {
  try { showToast(`JS error: ${e?.message || "unknown"}`, "err", 9000); } catch {}
});
window.addEventListener("unhandledrejection", (e) => {
  try { showToast(`Promise: ${e?.reason?.message || e?.reason || "rejected"}`, "err", 9000); } catch {}
});

// ── Flash animation helper ───────────────────────────────────────────────────
const _prevMetrics = {};

function _flashUpdate(id, rawVal, displayText) {
  const node = el(id);
  if (!node) return;
  const prev = _prevMetrics[id];
  if (prev !== undefined && prev !== rawVal) {
    node.classList.remove("flash-up", "flash-down");
    void node.offsetWidth; // force reflow
    node.classList.add(rawVal > prev ? "flash-up" : "flash-down");
  }
  _prevMetrics[id] = rawVal;
  node.textContent = displayText;
}

// ── Resizable columns ────────────────────────────────────────────────────────
const COL_STORAGE_KEY = "att_col_widths_v3";
const COL_MIN = [220, 520, 280]; // min px for left, centre, right (3-column layout)

function _defaultColWidths() {
  // left/right fixed-ish; centre flex
  return [260, null, 320]; // null = flex
}

function _loadColWidths() {
  try {
    const saved = JSON.parse(localStorage.getItem(COL_STORAGE_KEY));
    if (Array.isArray(saved) && saved.length === 3) return saved;
  } catch {}
  return _defaultColWidths();
}

function _saveColWidths(w) {
  try { localStorage.setItem(COL_STORAGE_KEY, JSON.stringify(w)); } catch {}
}

function _atlasGrid() {
  return document.querySelector(".atlas-grid");
}

function _applyColWidths(w) {
  const grid = _atlasGrid();
  if (!grid) return;
  const c0 = w[0] != null ? `${w[0]}px` : "260px";
  const c1 = w[1] != null ? `${w[1]}px` : "minmax(720px, 1.9fr)";
  const c2 = w[2] != null ? `${w[2]}px` : "320px";
  grid.style.gridTemplateColumns = `${c0} 8px ${c1} 8px ${c2}`;
}

function _wireResizeHandles() {
  const workspace = el("workspace");
  const grid = _atlasGrid();
  if (!workspace || !grid) return;

  // Inject handles as grid children after each section
  const sections = ["atlas-col-left", "atlas-col-centre"];
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
    const colIds = WORKSPACE_COL_IDS;
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
    // Centre stays flex unless user drags the centre-right handle
    _applyColWidths([w[0], idx === 1 ? w[1] : null, w[2]]);
  });

  document.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging.handle.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    const colIds = WORKSPACE_COL_IDS;
    const final = colIds.map(id => Math.round(document.getElementById(id)?.getBoundingClientRect().width ?? 0));
    _saveColWidths([final[0], null, final[2]]);
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

function _wireAgentLiveToggle() {
  const btn = el("agent-live-btn");
  if (!btn) return;
  _setAgentsLive(true);
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    _setAgentsLive(!_agentsLive);
  });
}

// ── Agent log modal (opened from AI AGENTS section) ───────────────────────────

function buildReasoningEntryElement(entry) {
  const action = entry.action ?? "INFO";
  const cls = ["PROCEED", "HOLD", "ABORT", "ERROR"].includes(action)
    ? `action-${action}`
    : "action-default";
  const agentName = (entry.agent ?? "").toUpperCase().replace(/\s+/g, "_");
  const div = document.createElement("div");
  div.className = "reasoning-entry";
  div.innerHTML = `
    <div class="re-header">
      <span class="re-agent" data-role="${esc(agentName)}">${esc(entry.agent ?? "")}</span>
      <span class="re-action ${cls}">${esc(action)}</span>
      <span class="re-time">${(entry.timestamp ?? "").slice(11, 19)}</span>
    </div>
    <div class="re-reasoning">${esc(entry.reasoning ?? "")}</div>`;
  return div;
}

/** Load today’s JSONL into the log modal (optional filter by backend agent id). Newest entries first. */
async function openAgentLogModal({ agentKey }) {
  const modal = el("agent-log-modal");
  const panel = el("reasoning-panel");
  const titleEl = el("agent-log-title");
  if (!modal || !panel) return;

  const filtered = agentKey != null && String(agentKey).trim() !== "";
  const key = filtered ? String(agentKey).trim() : "";
  if (titleEl) {
    titleEl.textContent = filtered
      ? `${_displayAgentName(key)} · log`
      : "Agent log";
  }

  panel.innerHTML = `<div class="reasoning-modal-loading">Loading…</div>`;
  modal.hidden = false;
  document.body.classList.add("news-modal-open");

  try {
    const q = filtered
      ? `tail=500&agent=${encodeURIComponent(key)}`
      : "tail=500";
    const r = await fetchWithTimeout(`${BACKEND}/reasoning_log?${q}`, {}, 18000);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const entries = await r.json();
    panel.innerHTML = "";
    const list = Array.isArray(entries) ? [...entries].reverse() : [];
    if (!list.length) {
      panel.innerHTML = `<div class="reasoning-modal-empty">No entries for today${
        filtered ? ` · ${esc(_displayAgentName(key))}` : ""
      }.</div>`;
      return;
    }
    list.forEach(e => panel.appendChild(buildReasoningEntryElement(e)));
  } catch {
    panel.innerHTML = `<div class="reasoning-modal-err">Could not load reasoning log (is the backend running?).</div>`;
  }
}

function _wireAgentLogModal() {
  const modal = el("agent-log-modal");
  const openBtn = el("agent-log-btn");
  const closeBtn = el("agent-log-close");
  if (!modal || !openBtn || !closeBtn) return;

  const close = () => {
    modal.hidden = true;
    document.body.classList.remove("news-modal-open");
  };

  openBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    void openAgentLogModal({ agentKey: null });
  });
  closeBtn.addEventListener("click", close);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) close();
  });

  const host = el("agent-roster");
  if (host && host.dataset.agentLogWire !== "1") {
    host.dataset.agentLogWire = "1";
    host.addEventListener("click", e => {
      const btn = e.target.closest(".agent-card-log-btn");
      if (!btn || !host.contains(btn)) return;
      e.preventDefault();
      e.stopPropagation();
      const key = btn.getAttribute("data-agent");
      if (key == null || key === "") return;
      void openAgentLogModal({ agentKey: key });
    });
  }
}

function _wireAgentFlowModal() {
  const modal = el("agent-flow-modal");
  const openBtn = el("agent-flow-btn");
  const closeBtn = el("agent-flow-close");
  const mlflowEl = el("agent-flow-mlflow");
  if (!modal || !openBtn || !closeBtn) return;

  const refreshMlflow = async () => {
    if (!mlflowEl) return;
    mlflowEl.classList.remove("mlflow-on");
    mlflowEl.textContent = "Checking MLflow…";
    try {
      const r = await fetchWithTimeout(`${BACKEND}/agents/mlflow_status`, {}, 5000);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      if (d.enabled) {
        mlflowEl.classList.add("mlflow-on");
        const hint = d.tracking_uri_hint || "";
        const exp = d.experiment ? esc(String(d.experiment)) : "atlas-agents";
        const safeHint = esc(hint);
        if (hint && /^https?:\/\//i.test(hint)) {
          mlflowEl.innerHTML =
            `MLflow <strong>on</strong> · experiment <code>${exp}</code> · ` +
            `<a href="${safeHint}" target="_blank" rel="noopener noreferrer">open UI</a>`;
        } else {
          mlflowEl.innerHTML =
            `MLflow <strong>on</strong> · experiment <code>${exp}</code>` +
            (hint ? ` · <code>${safeHint}</code>` : "");
        }
      } else {
        mlflowEl.textContent =
          "MLflow off — set MLFLOW_TRACKING_URI on the agent server (optional). Each T3 cycle logs a run when enabled.";
      }
    } catch {
      mlflowEl.textContent = "MLflow status unavailable (is the backend running?).";
    }
  };

  const open = () => {
    modal.hidden = false;
    document.body.classList.add("news-modal-open");
    void refreshMlflow();
  };
  const close = () => {
    modal.hidden = true;
    document.body.classList.remove("news-modal-open");
  };

  openBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    open();
  });
  closeBtn.addEventListener("click", close);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) close();
  });
}

// ── L2/L3 panel below chart (Robinhood-style: plot stays visible; panel is optional) ──
function _wireL2Overlay() {
  const panel = el("l2-market-panel");
  const openBtn = el("l2-toggle-btn");
  const closeBtn = el("l2-panel-close");
  if (!panel || !openBtn || !closeBtn) return;

  const sync = () => {
    setTimeout(() => {
      try { resizeTerminalCharts(); } catch { /* ignore */ }
    }, 50);
  };
  const show = () => { panel.hidden = false; openBtn.setAttribute("aria-pressed", "true"); sync(); };
  const hide = () => { panel.hidden = true; openBtn.setAttribute("aria-pressed", "false"); sync(); };

  openBtn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    panel.hidden ? show() : hide();
  });
  closeBtn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    hide();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden) hide();
  });
}

// ── Control Room nav (non-terminal shell interactions) ─────────────────────────
function _wireControlRoomNav() { /* Atlas shell */ }

// ── Keyboard shortcuts ────────────────────────────────────────────────────────
function _wireKeyboardShortcuts() {
  document.addEventListener("keydown", e => {
    // "/" focuses the gobar (unless already in an input)
    if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
      e.preventDefault();
      el("gobar")?.focus();
    }
    // Escape blurs any input
    if (e.key === "Escape") {
      document.activeElement?.blur();
    }
  });
  // Enter on gobar triggers ticker switch
  el("gobar")?.addEventListener("keydown", e => {
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
  const clockEl = el("clock");
  const tick = () => {
    if (!clockEl) return;
    // Always show market time (ET). DST will automatically render as EDT/EST.
    clockEl.textContent = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).format(new Date());
    clockEl.title = "Time zone: America/New_York (ET)";
  };
  tick();
  setInterval(tick, 1000);
}

// ── S&P 500 Scanner ───────────────────────────────────────────────────────────

async function pollScanner() {
  try {
    const url = `${BACKEND}/scanner?sort=${scannerSort}`;
    const r   = await fetchWithTimeout(url, {}, 25000);
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
    const r = await fetchWithTimeout(`${BACKEND}/scanner/quotes`, {}, 12000);
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

function _scannerRowHtml(d) {
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
}

function renderScanner() {
  const filter = scannerFilter.toUpperCase();
  const rows   = scannerData.filter(d =>
    !filter || d.ticker.includes(filter)
  );

  const total = scannerData.length;
  el("scanner-count").textContent =
    `${total} / ${rows.length}`;

  const tbody = el("scanner-body");
  const scrollParent = el("col-left-scroll");
  const prevScrollTop = scrollParent ? scrollParent.scrollTop : 0;

  const _restoreScannerScroll = () => {
    if (!scrollParent || !Number.isFinite(prevScrollTop)) return;
    requestAnimationFrame(() => {
      const max = Math.max(0, scrollParent.scrollHeight - scrollParent.clientHeight);
      scrollParent.scrollTop = Math.min(Math.max(0, prevScrollTop), max);
    });
  };

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-row scan-loading">
      ${total === 0 ? "Scanning S&amp;P 500… (~2 min first load)" : "No matches"}
    </td></tr>`;
    _restoreScannerScroll();
    return;
  }

  if (!_scannerBenchSections || !_benchmarkTickerSet) {
    tbody.innerHTML = rows.map((d) => _scannerRowHtml(d)).join("");
    _restoreScannerScroll();
    return;
  }

  const byTicker = new Map(rows.map((r) => [(r.ticker || "").toUpperCase(), r]));
  let html = "";
  for (const sec of _scannerBenchSections) {
    const secRows = [];
    for (const t of sec.tickers || []) {
      const r = byTicker.get(t);
      if (r) secRows.push(r);
    }
    if (!secRows.length) continue;
    html += `<tr class="scanner-section-row"><td colspan="6" class="scanner-section">${esc(sec.label)}</td></tr>`;
    for (const d of secRows) html += _scannerRowHtml(d);
  }
  const eqRows = rows.filter((r) => !_benchmarkTickerSet.has((r.ticker || "").toUpperCase()));
  if (eqRows.length) {
    html += `<tr class="scanner-section-row"><td colspan="6" class="scanner-section">Single names</td></tr>`;
    for (const d of eqRows) html += _scannerRowHtml(d);
  }
  tbody.innerHTML = html || rows.map((d) => _scannerRowHtml(d)).join("");
  _restoreScannerScroll();
}

// ── Scanner interactions ──────────────────────────────────────────────────────

el("scanner-body")?.addEventListener("click", e => {
  const row = e.target.closest(".scanner-row");
  if (!row) return;
  const ticker = row.dataset.ticker;
  if (ticker) switchTicker(ticker);
});

// Prefetch on hover so clicks feel instant (quote + stock_info + options).
let _prefetchTimer = null;
function _prefetchTickerSoon(ticker) {
  const t = String(ticker || "").toUpperCase();
  if (!t) return;
  clearTimeout(_prefetchTimer);
  _prefetchTimer = setTimeout(() => {
    try {
      // Quote (fast)
      if (!_getCache(_quoteCache, t, 4000)) {
        fetchWithTimeout(`${BACKEND}/quote/${encodeURIComponent(t)}`, {}, 3500)
          .then(r => r.ok ? r.json() : null)
          .then(q => { if (q) _quoteCache.set(t, { q, at: _now() }); })
          .catch(() => {});
      }
      // Fundamentals (cached server-side; this just warms client cache/UI)
      if (!_getCache(_stockInfoCache, t, 10 * 60 * 1000)) {
        fetchWithTimeout(`${BACKEND}/stock_info/${encodeURIComponent(t)}`, {}, 6000)
          .then(r => r.ok ? r.json() : null)
          .then(info => { if (info) _stockInfoCache.set(t, { info, at: _now() }); })
          .catch(() => {});
      }
      // Options (server-filtered)
      if (!_getCache(_optionsCache, t, 2 * 60 * 1000)) {
        fetchWithTimeout(`${BACKEND}/options/${encodeURIComponent(t)}`, {}, 7000)
          .then(r => r.ok ? r.json() : null)
          .then(d => {
            const contracts = d?.contracts ?? null;
            if (Array.isArray(contracts)) _optionsCache.set(t, { contracts, at: _now() });
          })
          .catch(() => {});
      }
    } catch { /* ignore */ }
  }, 120);
}

el("scanner-body")?.addEventListener("mouseover", e => {
  const row = e.target.closest(".scanner-row");
  const t = row?.dataset?.ticker;
  if (t) _prefetchTickerSoon(t);
});

el("scanner-filter")?.addEventListener("input", e => {
  scannerFilter = e.target.value.trim();
  renderScanner();
});

el("scanner-sort")?.addEventListener("change", e => {
  scannerSort = e.target.value;
  pollScanner();  // re-fetch with new sort order
});

// ── Ticker switching ──────────────────────────────────────────────────────────

async function switchTicker(ticker) {
  if (ticker === activeTicker) return;
  activeTicker = ticker;

  // Update header & price pill ticker immediately
  el("chain-ticker").textContent = ticker;
  const cLab = el("chart-ticker-label");
  if (cLab) cLab.textContent = ticker;
  el("chain-count").textContent = "loading…";
  const ppTicker = el("pp-ticker");
  if (ppTicker) ppTicker.textContent = ticker;
  const ppPrice = el("pp-price");
  // Fast paint: use last known scanner quote if present (avoids "—" flash).
  try {
    const row = scannerData.find(r => (r.ticker || "").toUpperCase() === ticker.toUpperCase());
    const lastN = row?.last != null ? Number(row.last) : NaN;
    if (ppPrice) {
      if (Number.isFinite(lastN)) {
        ppPrice.textContent = fmtNum(lastN, lastN >= 100 ? 2 : 4);
      } else {
        ppPrice.textContent = "—";
      }
      ppPrice.dataset.prev = "";
    }
  } catch {
    if (ppPrice) { ppPrice.textContent = "—"; ppPrice.dataset.prev = ""; }
  }
  const ppChange = el("pp-change");
  try {
    const row = scannerData.find(r => (r.ticker || "").toUpperCase() === ticker.toUpperCase());
    const c = row?.change_pct;
    if (ppChange) {
      if (c != null && Number.isFinite(Number(c))) {
        const pNum = Number(c);
        ppChange.textContent = `${pNum >= 0 ? "+" : ""}${pNum.toFixed(2)}%`;
        ppChange.className = pNum > 0 ? "pos" : pNum < 0 ? "neg" : "";
      } else {
        ppChange.textContent = "—";
        ppChange.className = "";
      }
    }
  } catch {
    if (ppChange) { ppChange.textContent = "—"; ppChange.className = ""; }
  }

  // Highlight scanner row
  renderScanner();

  // Notify backend (sets firm_state.ticker + pre-warms drilldown cache)
  fetch(`${BACKEND}/set_ticker`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker }),
  }).catch(() => {});

  // Start dependent loads without blocking fundamentals/plot updates.
  fetchOptionsChain(ticker);
  _syncStockTicker(ticker);

  window.dispatchEvent(
    new CustomEvent("terminal:symbol", { detail: { symbol: ticker } }),
  );

  const { tf, limit } = _resolveBackendTf();
  if (el("ticker-chart-tf")) el("ticker-chart-tf").value = tf;
  loadTickerBars(ticker, tf, { limit: limit ?? undefined });
  pollQuote(); // refresh quote immediately (also continues at 1Hz)
  _stockInfoSkeleton(ticker);
  loadStockInfo(ticker);
  scheduleChartPoll();
}

async function fetchOptionsChain(ticker) {
  try {
    const t = String(ticker || "").toUpperCase();
    // Instant paint from cache (then refresh in background)
    const cached = _getCache(_optionsCache, t, 15 * 60 * 1000);
    if (cached?.contracts?.length) {
      renderOptionsTable(cached.contracts);
    }

    _optionsSeq += 1;
    const seq = _optionsSeq;
    try { _optionsController?.abort(); } catch { /* ignore */ }
    _optionsController = new AbortController();

    el("options-body").innerHTML =
      `<tr><td colspan="11" class="empty-row scan-loading">Fetching ${esc(t)}…</td></tr>`;
    const r = await fetchWithTimeout(
      `${BACKEND}/options/${encodeURIComponent(t)}`,
      { signal: _optionsController.signal },
      30000,
    );
    if (seq !== _optionsSeq) return; // stale
    if (!r.ok) throw new Error(r.statusText);
    const data = await r.json();
    if (seq !== _optionsSeq) return; // stale
    const contracts = data.contracts ?? [];
    _optionsCache.set(t, { contracts, at: _now() });
    renderOptionsTable(contracts);
  } catch (e) {
    if (e?.name === "AbortError" || String(e?.message || e).toLowerCase().includes("aborted")) return;
    el("options-body").innerHTML =
      `<tr><td colspan="11" class="empty-row scan-error">Failed: ${esc(String(e))}</td></tr>`;
  }
}

// ── Options chain table ───────────────────────────────────────────────────────

let _chainRaw      = [];   // full unfiltered dataset
let _chainFilter   = "all"; // "all" | "calls" | "puts"
let _chainSort     = "iv_desc";
let _chainStrike   = null;  // number | null

// Options chain worker (keeps UI thread smooth)
let _optWorker = null;
let _optWorkerSeq = 0;
function _ensureOptWorker() {
  if (_optWorker) return _optWorker;
  try {
    _optWorker = new Worker(new URL("./options_worker.js", import.meta.url), { type: "module" });
    return _optWorker;
  } catch {
    _optWorker = null;
    return null;
  }
}

function _applyChainView() {
  const worker = _ensureOptWorker();
  if (worker) {
    _optWorkerSeq += 1;
    const seq = _optWorkerSeq;
    worker.onmessage = (ev) => {
      const msg = ev.data || {};
      if (seq !== _optWorkerSeq) return;
      if (msg.type !== "result") return;
      const rows = msg.rows || [];
      const total = msg.total || 0;
      const rawTotal = msg.rawTotal || 0;
      el("chain-count").textContent =
        total ? `${Math.min(rows.length, total)} / ${rawTotal} contracts` : "no data";
      if (!rows.length) {
        el("options-body").innerHTML =
          `<tr><td colspan="11" class="empty-row">No contracts match filters</td></tr>`;
        return;
      }
      const tbody = el("options-body");
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
    };
    worker.postMessage({
      type: "apply",
      raw: _chainRaw,
      filter: _chainFilter,
      sort: _chainSort,
      strike: _chainStrike,
      cap: 300,
    });
    return;
  }

  // Fallback (no worker support)
  let rows = [..._chainRaw];

  if (_chainFilter === "calls") rows = rows.filter(g => g.right === "CALL");
  if (_chainFilter === "puts")  rows = rows.filter(g => g.right === "PUT");

  if (_chainStrike !== null && !isNaN(_chainStrike)) {
    const range = Math.max(_chainStrike * 0.05, 10);
    rows = rows.filter(g => Math.abs((g.strike ?? 0) - _chainStrike) <= range);
  }

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
  rows = rows.slice(0, 300);

  el("chain-count").textContent =
    rows.length ? `${rows.length} / ${_chainRaw.length} contracts` : "no data";

  if (!rows.length) {
    el("options-body").innerHTML =
      `<tr><td colspan="11" class="empty-row">No contracts match filters</td></tr>`;
    return;
  }

  const tbody = el("options-body");
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
  el("chain-count").textContent =
    _chainRaw.length ? `${_chainRaw.length} contracts` : "no data";

  if (!_chainRaw.length) {
    el("options-body").innerHTML =
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
//   "5D"/"1M"/…/"MAX" = daily bars over that window (MAX ≈ multi-decade history, server-trimmed)
//
// Bars per trading day: 1m≈390, 5m≈78, 15m≈26, 1H≈7, 1D=1
// Trading days per range: 1D=1, 5D=5, 1M=22, 3M=66, 6M=126, 1Y=252, 2Y≈504, 5Y≈1260, MAX→server cap
const _INTERVAL_TO_TF = {
  "1m":  "1Min",
  "5m":  "1D",    // special: today's session 5m bars via filter
  "15m": "15Min",
  "1H":  "1Hour",
  "1D":  null,    // resolved per range below
};
const _DAILY_RANGE_TF = {
  "5D": "5D", "1M": "1M", "3M": "3M", "6M": "6M", "1Y": "1Y", "2Y": "2Y", "5Y": "5Y", "MAX": "MAX",
};

// Trading days per range (used to compute limit for intraday bars over multi-day windows)
const _RANGE_TRADE_DAYS = {
  "1D": 1, "5D": 5, "1M": 22, "3M": 66, "6M": 126, "1Y": 252, "2Y": 504, "5Y": 1260, "MAX": 8000,
};
// Bars per full trading day per interval
const _BARS_PER_DAY = { "1m": 390, "5m": 78, "15m": 26, "1H": 7 };

// Sensible default interval to auto-select when range changes
const _DEFAULT_INTERVAL = {
  "1D": "5m", "5D": "1H", "1M": "1H", "3M": "1D", "6M": "1D", "1Y": "1D", "2Y": "1D", "5Y": "1D", "MAX": "1D",
};

let _chartRange    = "5D";
let _chartInterval = "1H";  // default for 5D

function _resolveBackendTf() {
  // "1D" bar interval: use the historical daily range TF
  if (_chartInterval === "1D") {
    // RANGE "1D" + daily bars = ambiguous; show ~1Y of dailies instead of falling back to ~1 week.
    if (_chartRange === "1D") {
      return { tf: "1Y", limit: null };
    }
    return { tf: _DAILY_RANGE_TF[_chartRange] || "1Y", limit: null };
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
  const limit = Math.min(10000, Math.max(20, Math.ceil(tradingDays * barsPerDay * 1.2)));
  return { tf: backendTf, limit };
}

function _applyChartTf(bust = false) {
  const { tf, limit } = _resolveBackendTf();
  const tfSel = el("ticker-chart-tf");
  if (tfSel) tfSel.value = tf;
  loadTickerBars(activeTicker, tf, { bust, limit: limit ?? undefined });
  scheduleChartPoll();
  updateChartBarSizeLabel(tf);
  _highlightActiveIntervalBtn();
}

function _highlightActiveIntervalBtn() {
  // Reflect current interval selection visually; grey 1m/5m for long ranges
  const tradingDays = _RANGE_TRADE_DAYS[_chartRange] || 1;
  document.querySelectorAll(".chart-interval-btn").forEach(b => {
    const iv = b.dataset.interval;
    // Dim intervals that would produce impossibly many bars (>2000)
    const bpd = _BARS_PER_DAY[iv] || 1;
    const tooGranular = tradingDays * bpd > 10000;
    b.classList.toggle("chart-interval-dim", tooGranular);
    b.title = tooGranular
      ? `${iv} bars — too granular for ${_chartRange} range (use 1H or 1D)`
      : `${iv} bars`;
  });
}

function _closeChartSettingsPopover() {
  const pop = el("chart-settings-popover");
  const btn = el("chart-settings-btn");
  if (pop) pop.hidden = true;
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function _wireChartSettingsPopover() {
  const btn = el("chart-settings-btn");
  const pop = el("chart-settings-popover");
  if (!btn || !pop || btn.dataset.popoverWired === "1") return;
  btn.dataset.popoverWired = "1";
  btn.addEventListener("click", e => {
    e.stopPropagation();
    const willOpen = pop.hidden;
    pop.hidden = !willOpen;
    btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
  });
  pop.addEventListener("click", e => e.stopPropagation());
  document.addEventListener("click", _closeChartSettingsPopover);
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    if (!pop.hidden) {
      _closeChartSettingsPopover();
      e.preventDefault();
    }
  });
}

function _wireChartStripButtons() {
  const styleSel = el("ticker-chart-style");

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
  _wireChartSettingsPopover();

  // Update sd-chg1 badge class (pos/neg) when text changes
  const chgEl = el("sd-chg1");
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
    el(id)?.addEventListener("click", () => {
      document.querySelectorAll(".chain-cp-btn").forEach(b => b.classList.remove("chain-cp-active"));
      el(id).classList.add("chain-cp-active");
      _chainFilter = id === "cp-all" ? "all" : id === "cp-calls" ? "calls" : "puts";
      _applyChainView();
    });
  });

  // Sort dropdown
  el("chain-sort")?.addEventListener("change", e => {
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
  el("chain-strike-filter")?.addEventListener("input", e => {
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
      el("chain-sort").value = _chainSort;
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
  const tbody = el("stock-positions-body");
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
  const tbody = el("positions-body");
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
  const node = el(id);
  if (!node) return;
  const n = Number(val);
  if (!Number.isFinite(n)) { node.textContent = "—"; return; }
  const formatted = `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  _flashUpdate(id, n, formatted);
}

function updateMetrics(state) {
  // Store for UI features like "exposure-only" news filtering.
  try { window.__atlasLastState = state; } catch {}
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
  // IMPORTANT: the UI is the source of truth for the selected ticker.
  // The backend `firm_state.ticker` can change due to other tabs/websocket clients
  // (or background services). Auto-overwriting `activeTicker` here can desync the
  // chart header vs options chain table (header updates but options do not refetch).
  //
  // We only accept the backend ticker on the *first* successful /state poll, as an
  // initial default when the UI hasn't been driven by the user yet.
  if (!updateMetrics._tickerInitDone) {
    updateMetrics._tickerInitDone = true;
    try {
      const st = String(stateTicker || "").toUpperCase();
      const cur = String(activeTicker || "").toUpperCase();
      if (st && st !== cur) {
        // Use the normal switching path so all dependent panels stay in sync.
        switchTicker(st);
      }
    } catch { /* ignore */ }
  }

  updateRegimeBadge(state.market_regime);
  if (state.circuit_breaker_tripped) setCircuitBreakerUI(true);

  renderStockPositions(state.stock_positions);
  renderPositions(state.open_positions);

  _updateNewsFeedStatus(state);
  // Render news from state (backup in case /news poll fails)
  if (state.news_feed?.length) renderNews(state.news_feed);

  if (state.agent_runtime) {
    renderAgentStatus(state.agent_runtime);
    renderLlmModelsHint(state.agent_runtime);
  }

  // Option rights preference (CALL/PUT/BOTH) used by Strategist proposals.
  try {
    const rights = String(state.allowed_option_rights || "").toUpperCase();
    const sel = el("ot-rights");
    if (sel && (rights === "CALL" || rights === "PUT" || rights === "BOTH")) {
      if (sel.value !== rights) sel.value = rights;
    }
  } catch { /* ignore */ }

  // Risk limits inputs (max drawdown / position cap). Server stores fractions; UI shows %.
  try {
    const dd = r.max_drawdown_pct != null ? Number(r.max_drawdown_pct) * 100 : null;
    const pc = r.position_cap_pct != null ? Number(r.position_cap_pct) * 100 : null;
    const ddEl = el("risk-max-dd");
    const pcEl = el("risk-pos-cap");
    if (ddEl && dd != null && Number.isFinite(dd) && document.activeElement !== ddEl) {
      ddEl.value = dd.toFixed(1);
    }
    if (pcEl && pc != null && Number.isFinite(pc) && document.activeElement !== pcEl) {
      pcEl.value = pc.toFixed(1);
    }
  } catch { /* ignore */ }
}

// ── Risk limits (user inputs) ───────────────────────────────────────────────
el("risk-save")?.addEventListener("click", async () => {
  const btn = el("risk-save");
  const st  = el("risk-status");
  const ddEl = el("risk-max-dd");
  const pcEl = el("risk-pos-cap");
  if (!btn || !ddEl || !pcEl) return;
  const maxDD = parseFloat(ddEl.value);
  const posCap = parseFloat(pcEl.value);
  btn.disabled = true;
  if (st) { st.textContent = "Saving…"; st.style.opacity = "0.85"; }
  try {
    const r = await fetchWithTimeout(`${BACKEND}/risk/limits`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        max_drawdown_pct: Number.isFinite(maxDD) ? maxDD : null,
        position_cap_pct: Number.isFinite(posCap) ? posCap : null,
      }),
    }, 5000);
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data?.ok === false) {
      const msg = data?.detail?.error || data?.detail || data?.error || "Save failed";
      if (st) { st.textContent = `✗ ${String(msg)}`; st.style.opacity = "0.9"; }
      showToast(String(msg), "err", 7000);
    } else {
      if (st) { st.textContent = "✓ Saved"; st.style.opacity = "0.8"; }
      showToast("Risk limits saved.", "ok", 3000);
      try { pollState(); } catch {}
    }
  } catch (e) {
    if (st) st.textContent = `✗ ${e?.message || e}`;
    showToast(`Could not save risk limits: ${e?.message || e}`, "err", 8000);
  } finally {
    btn.disabled = false;
    setTimeout(() => { try { if (st) st.textContent = ""; } catch {} }, 3500);
  }
});

function setMetric(id, val, prefix, signed, decimals = 2, suffix = "") {
  const node = el(id);
  if (!node) return;
  const formatted = `${prefix}${Number(val).toFixed(decimals)}${suffix}`;
  const prev = _prevMetrics[id];
  if (prev !== undefined && prev !== val) {
    node.classList.remove("flash-up", "flash-down");
    void node.offsetWidth;
    node.classList.add(val > prev ? "flash-up" : "flash-down");
    // Update trend arrow
    const trendEl = el(`trend-${id}`);
    if (trendEl) {
      trendEl.textContent = val > prev ? "▲" : "▼";
      trendEl.className = `metric-trend ${val > prev ? "up" : "down"}`;
    }
    // Update card border color
    const card = el(`card-${id}`);
    if (card && signed) {
      card.classList.toggle("positive-card", val >= 0);
      card.classList.toggle("negative-card", val < 0);
    }
  }
  _prevMetrics[id] = val;
  node.textContent = formatted;
  node.className   = "metric-value" +
    (signed ? (val >= 0 ? " positive" : " negative") : "");
}

/** Tooltip copy for desk ``MarketRegime`` (see ``agents/features.py`` ``classify_regime``). */
const REGIME_HELP = {
  HIGH_VOL:
    "ATM implied vol ≥60% or inverted IV term structure (backwardation). Desk treats this as a high-volatility regime: expect wider moves and richer option prices.",
  LOW_VOL: "ATM IV around 18% or below — relatively quiet implied volatility.",
  MEAN_REVERTING: "Middle IV band without a strong trend fingerprint — default neutral regime.",
  TRENDING_UP: "Low IV with steep contango in the term structure — upward-trend heuristic.",
  TRENDING_DOWN: "Steep term structure with elevated skew — downward-trend heuristic.",
  UNKNOWN: "Not enough IV / surface data to classify regime.",
};

function _regimeBadgeKind(regime) {
  const r = regime ?? "UNKNOWN";
  if (r === "TRENDING_UP" || r === "LOW_VOL") return "ok";
  if (r === "TRENDING_DOWN" || r === "HIGH_VOL") return "warn";
  if (r === "MEAN_REVERTING") return "neutral";
  return "unknown";
}

function updateRegimeBadge(regime) {
  const node = el("regime-badge");
  if (!node) return;
  const r = regime ?? "UNKNOWN";
  node.textContent = `REGIME: ${r}`;
  node.className = `topbar-badge badge-${_regimeBadgeKind(r)}`;
  node.title = REGIME_HELP[r] || REGIME_HELP.UNKNOWN;
}

function setCircuitBreakerUI(tripped) {
  const node = el("cb-badge");
  if (!node) return;
  node.textContent = tripped ? "⚠ CB TRIPPED" : "CIRCUIT OK";
  node.className   = `topbar-badge ${tripped ? "badge-danger" : "badge-ok"}`;
}

// ── News tape ─────────────────────────────────────────────────────────────────

const _seenNews = new Set();

function _newsItemId(item) {
  return String(item?.published_at ?? "") + String(item?.headline ?? "");
}

function _updateNewsLastSync() {
  const node = el("news-last-sync");
  if (!node) return;
  const t = new Date();
  node.hidden = false;
  node.textContent = ` · Sync ${t.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true })}`;
  node.title = `Last ${BACKEND}/news pull: ${t.toLocaleString("en-US")}`;
}

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
  const modal   = el("news-modal");
  const metaEl  = el("news-modal-meta");
  const headEl  = el("news-modal-headline");
  const badgeEl = el("news-modal-badges");
  const sumEl   = el("news-modal-summary");
  const footEl  = el("news-modal-footer");
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
    sumEl.classList.remove("news-modal-no-summary");
  } else {
    sumEl.textContent = "No summary available for this article.";
    sumEl.style.display = "";
    sumEl.classList.add("news-modal-no-summary");
  }

  // Cross-stock impacts (if AI-processed data available)
  const impactsContainer = el("news-modal-impacts");
  if (impactsContainer) {
    impactsContainer.innerHTML = "";
    impactsContainer.hidden = true;
  }

  const articleTickers = (item.tickers || []).filter(t => t && t.length <= 6);
  if (articleTickers.length > 0 && impactsContainer) {
    impactsContainer.hidden = false;
    impactsContainer.innerHTML = `<div class="news-modal-impacts-title">CROSS-STOCK IMPACTS</div><div class="news-modal-impacts-loading">Loading…</div>`;
    _fetchArticleImpacts(articleTickers).then(impacts => {
      if (!impactsContainer.isConnected) return;
      if (impacts.length > 0) {
        impactsContainer.innerHTML = `
          <div class="news-modal-impacts-title">CROSS-STOCK IMPACTS</div>
          ${impacts.map(i => {
            const scCls = i.total_impact > 0 ? "pos" : i.total_impact < 0 ? "neg" : "";
            const sign = i.total_impact > 0 ? "+" : "";
            return `<div class="modal-impact-row">
              <span class="modal-impact-ticker">${esc(i.ticker)}</span>
              <span class="modal-impact-score ${scCls}">${sign}${i.total_impact.toFixed(2)}</span>
              <span class="modal-impact-rel">${esc((i.relationships || []).slice(0, 2).join(", "))}</span>
            </div>`;
          }).join("")}`;
        impactsContainer.hidden = false;
      } else {
        impactsContainer.hidden = true;
      }
    });
  }

  const url = (item.url || "").trim();
  footEl.innerHTML = url
    ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" class="news-modal-link">
         Read full article ↗
       </a>`
    : `<span class="news-modal-no-link">No source URL available</span>`;

  modal.removeAttribute("hidden");
  document.body.classList.add("news-modal-open");
  el("news-modal-close")?.focus();
}

function _closeNewsModal() {
  const modal = el("news-modal");
  if (!modal) return;
  modal.setAttribute("hidden", "");
  document.body.classList.remove("news-modal-open");
}

// Wire up modal close controls once DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  el("news-modal-close")
    ?.addEventListener("click", _closeNewsModal);
  el("news-modal")
    ?.addEventListener("click", e => { if (e.target === e.currentTarget) _closeNewsModal(); });
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  const newsModal = el("news-modal");
  if (newsModal && !newsModal.hasAttribute("hidden")) {
    _closeNewsModal();
    e.preventDefault();
  }
});

// ── News sorting (UI) ─────────────────────────────────────────────────────────
let _newsSortMode = "time_desc";
const _newsBuffer = []; // holds newest items (deduped by _seenNews)
let _newsExposureOnly = false;
let _lastNewsRenderKey = "";

function _newsSetExposureOnly(on) {
  _newsExposureOnly = Boolean(on);
  const btn = el("news-exposure-btn");
  if (btn) {
    btn.setAttribute("aria-pressed", _newsExposureOnly ? "true" : "false");
    btn.textContent = _newsExposureOnly ? "EXPOSURE ✓" : "EXPOSURE";
    btn.title = _newsExposureOnly
      ? "Showing only news for tickers you currently hold / are exposed to"
      : "Show only news for holdings / exposure";
  }
  _renderNewsTapeFromBuffer();
}

function _newsPrimaryTicker(item) {
  const t = (item?.tickers && item.tickers.length) ? item.tickers[0] : "";
  return String(t || "").toUpperCase();
}

function _renderNewsTapeFromBuffer() {
  const tape = el("news-tape");
  if (!tape) return;
  tape.querySelector(".news-empty")?.remove();

  let items = [..._newsBuffer];

  if (_newsExposureOnly) {
    const exposure = new Set();
    try {
      // Stock positions
      const ps = window.__atlasLastState?.stock_positions || [];
      ps.forEach(p => { if (p?.ticker) exposure.add(String(p.ticker).toUpperCase()); });
    } catch {}
    try {
      // Option positions: symbol starts with underlying
      const ops = window.__atlasLastState?.open_positions || [];
      ops.forEach(p => {
        const sym = String(p?.symbol || "").toUpperCase();
        const m = sym.match(/^([A-Z]{1,6})/);
        if (m) exposure.add(m[1]);
      });
    } catch {}
    items = items.filter(it => {
      const ts = (it?.tickers || []).map(x => String(x || "").toUpperCase());
      const ms = (it?.mentioned_tickers || []).map(x => String(x || "").toUpperCase());
      for (const t of [...ts, ...ms]) if (t && exposure.has(t)) return true;
      return false;
    });
  }

  const byTime = (a, b) => new Date(a.published_at) - new Date(b.published_at);
  const byTicker = (a, b) => _newsPrimaryTicker(a).localeCompare(_newsPrimaryTicker(b));

  if (_newsSortMode === "time_asc") items.sort(byTime);
  else if (_newsSortMode === "time_desc") items.sort((a, b) => byTime(b, a));
  else if (_newsSortMode === "ticker_desc") items.sort((a, b) => byTicker(b, a) || byTime(b, a));
  else items.sort((a, b) => byTicker(a, b) || byTime(b, a)); // ticker_asc default

  // Preserve scroll position for comfortable reading.
  const prevScrollTop = tape.scrollTop;
  const prevScrollH = tape.scrollHeight || 0;
  const atTop = prevScrollTop < 6;

  const view = items.slice(0, 60);
  const renderKey = [
    _newsSortMode,
    _newsExposureOnly ? "expo" : "all",
    ...view.map(_newsItemId),
  ].join("|");
  if (renderKey === _lastNewsRenderKey) return;
  _lastNewsRenderKey = renderKey;

  // Rebuild DOM (simple + stable for sorting)
  tape.innerHTML = "";
  for (const item of view) {
    const s      = Number(item.sentiment ?? 0);
    const conf   = Number(item.confidence ?? 0);
    const sCls   = s > 0.1 ? "sentiment-pos" : s < -0.1 ? "sentiment-neg" : "sentiment-neu";
    const isHigh = (item.priority || "NORMAL") === "HIGH";
    const impact = Math.max(0, Math.min(1, Number(item.impact_score ?? 0)));
    const urg    = String(item.urgency_tier || "T2").toUpperCase();

    const cat   = _CAT_META[item.category] || _CAT_META.general;
    const tier  = _TIER_LABEL[item.ticker_tier] || _TIER_LABEL.top;

    const catBadge = cat.label
      ? `<span class="news-cat ${cat.cls}">${cat.label}</span>`
      : "";
    const tierBadge = tier.label
      ? `<span class="news-tier ${tier.cls}">${tier.label}</span>`
      : "";

    const tickers = (item.tickers || [])
      .filter(t => t && String(t).length <= 6)
      .map(t => String(t).toUpperCase());
    const primary = tickers[0];
    const extraCount = Math.max(0, tickers.length - 1);
    const tickerTags = primary
      ? `<button type="button" class="news-ticker" data-ticker="${escAttr(primary)}" title="Jump to ${escAttr(primary)}">${esc(primary)}</button>` +
        (extraCount ? `<span class="news-ticker-more" title="${escAttr(tickers.slice(1, 6).join(", "))}">+${extraCount}</span>` : "")
      : "";

    const heatLeft = 50;
    const heatW = Math.round((s + 1) * 50); // 0..100
    const impactPct = Math.round(impact * 100);
    const summary = String(item.summary || "").trim();
    const summaryLine = summary
      ? `<div class="news-row3"><span class="news-summary">${esc(summary).slice(0, 180)}${summary.length > 180 ? "…" : ""}</span></div>`
      : "";

    const div = document.createElement("div");
    div.className = [
      "news-item",
      s > 0.1 ? "news-item-bull" : s < -0.1 ? "news-item-bear" : "news-item-neu",
      isHigh ? "news-item-high" : "",
      urg === "T0" ? "news-urg-t0" : urg === "T1" ? "news-urg-t1" : urg === "T3" ? "news-urg-t3" : "news-urg-t2",
      impact >= 0.75 ? "news-impact-xl" : impact >= 0.5 ? "news-impact-lg" : impact >= 0.3 ? "news-impact-md" : "news-impact-sm",
    ].join(" ").trim();
    div.setAttribute("role", "button");
    div.setAttribute("tabindex", "0");
    div.title = "Click to expand";
    div.style.setProperty("--impact", String(impact));

    const hiBadge = isHigh ? `<span class="news-flag">HIGH</span>` : "";
    const urgBadge = (urg === "T0" || urg === "T1") ? `<span class="news-flag">${esc(urg)}</span>` : "";
    div.innerHTML = `
      <div class="news-row1">
        <span class="news-time">${_newsRelTime(item.published_at)}</span>
        ${_newsSrcBadge(item.source)}
        ${tickerTags}
        ${hiBadge}
        ${urgBadge}
        <span class="news-sentiment ${sCls}" title="Sentiment">${s >= 0 ? "+" : ""}${s.toFixed(2)}</span>
      </div>
      <div class="news-row2">
        <span class="news-text">${esc(item.headline)}</span>
      </div>
      ${summaryLine}`;

    // Sentiment heat strip (quick “force” visualization)
    const heat = document.createElement("div");
    heat.className = "news-heat";
    heat.innerHTML = `<span class="news-heat-fill ${sCls}" style="left:${heatLeft}%;width:${heatW}%"></span>`;
    div.appendChild(heat);

    div._newsItem = item;
    div.addEventListener("click", () => _openNewsModal(item));
    div.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") _openNewsModal(item); });
    div.addEventListener("mouseover", () => {
      // Predictive prefetch: warm fundamentals/options/quote for primary ticker
      const t = _newsPrimaryTicker(item);
      if (t) _prefetchTickerSoon(t);
    });
    tape.appendChild(div);
  }

  // Restore scroll position (unless the user is already at the top).
  if (!atTop) {
    // If height changed, keep approx position from bottom stable.
    const newH = tape.scrollHeight || 0;
    const delta = newH - prevScrollH;
    tape.scrollTop = prevScrollTop + (Number.isFinite(delta) ? delta : 0);
  } else {
    tape.scrollTop = 0;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const sel = el("news-sort");
  if (!sel) return;
  _newsSortMode = sel.value || "time_desc";
  sel.addEventListener("change", () => {
    _newsSortMode = sel.value || "time_desc";
    _renderNewsTapeFromBuffer();
  });

  const exposureBtn = el("news-exposure-btn");
  if (exposureBtn) {
    _newsSetExposureOnly(false);
    exposureBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      _newsSetExposureOnly(!_newsExposureOnly);
    });
  }

  // Ticker chip → jump the desk without opening the modal.
  const tape = el("news-tape");
  if (tape && tape.dataset.tickerWired !== "1") {
    tape.dataset.tickerWired = "1";
    tape.addEventListener("click", (e) => {
      const btn = e.target.closest(".news-ticker");
      if (!btn || !tape.contains(btn)) return;
      const t = btn.getAttribute("data-ticker");
      if (!t) return;
      e.preventDefault();
      e.stopPropagation();
      try { setActiveTicker(String(t)); } catch {}
    });
  }
});

function _updateNewsFeedStatus(state) {
  const badge = el("news-feed-status");
  const tape  = el("news-tape");
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

async function _fetchArticleImpacts(tickers) {
  const results = [];
  try {
    for (const t of tickers.slice(0, 3)) {
      const r = await fetchWithTimeout(`${BACKEND}/news/impacts/${t}`, {}, 2000);
      if (r.ok) {
        const data = await r.json();
        if (data && data.article_count > 0) results.push(data);
      }
    }
  } catch { /* ignore */ }
  return results;
}

function renderNews(items, opts = {}) {
  if (!items?.length) return;
  const tape = el("news-tape");
  if (!tape) return;
  tape.querySelector(".news-empty")?.remove();

  const sortedIncoming = [...items].sort((a, b) => new Date(b.published_at) - new Date(a.published_at));

  if (opts.replace) {
    // Full replace from GET /news (authoritative; avoids stale buffer / dedupe drift)
    _newsBuffer.length = 0;
    _seenNews.clear();
    for (const item of sortedIncoming.slice(0, 240)) {
      _seenNews.add(_newsItemId(item));
      _newsBuffer.push(item);
    }
  } else {
    [...sortedIncoming].reverse().forEach(item => {
      const id = _newsItemId(item);
      if (_seenNews.has(id)) return;
      _seenNews.add(id);
      _newsBuffer.unshift(item);
    });
    if (_newsBuffer.length > 240) _newsBuffer.length = 240;
  }
  _renderNewsTapeFromBuffer();
}

// ── Reasoning log ─────────────────────────────────────────────────────────────

const _seenReasoning = new Set();

// ── Agent roster (derived from reasoning stream) ──────────────────────────────
const _agentRoster = new Map(); // name -> { lastTs, lastAction, lastText, errors, count }

/** Maps backend ``ReasoningEntry.agent`` strings to human-readable labels (matches graph agents). */
const AGENT_DISPLAY_NAMES = {
  BullResearcher: "Long Thesis",
  BearResearcher: "Short Thesis",
  SentimentAnalyst: "Sentiment Engine",
  OptionsSpecialist: "Derivatives Desk",
  RiskManager: "Risk Control",
  Strategist: "Strategy Lead",
  DeskHead: "Desk Chief",
  Trader: "Floor Trader",
  AdversarialDebate: "Red Team",
  System: "System",
  "News Processor": "News Wire",
  "Movement Tracker": "Tape Reader",
  "Sentiment Monitor": "Social Pulse",
  Execution: "Broker Bridge",
};

function _displayAgentName(agentKey) {
  if (agentKey == null || agentKey === "") return "Agent";
  const k = String(agentKey).trim();
  if (AGENT_DISPLAY_NAMES[k]) return AGENT_DISPLAY_NAMES[k];
  return k.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ");
}

/** Two-letter avatar for agent rail pills */
function _agentAvatar(agentKey) {
  const s = _displayAgentName(agentKey).trim();
  const parts = s.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) {
    const a = (parts[0][0] || "?") + (parts[1][0] || "");
    return a.toUpperCase();
  }
  if (parts.length === 1 && parts[0].length >= 2) return parts[0].slice(0, 2).toUpperCase();
  return String(agentKey || "A").replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase() || "AI";
}

function _fmtRel(ts) {
  const t = Date.parse(ts || "");
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function _fmtActionDisplay(act) {
  const a = String(act || "info").toUpperCase();
  const map = {
    PROCEED: "Proceed",
    HOLD: "Hold",
    ABORT: "Abort",
    ERROR: "Error",
    INFO: "Info",
    IDLE: "Idle",
  };
  return map[a] || (a.charAt(0) + a.slice(1).toLowerCase());
}

function _agentKind(name) {
  const n = String(name || "").toLowerCase();
  if (n.includes("optionspecialist") || n.includes("options")) return "agent";
  if (n.includes("strategist")) return "trader";
  if (n.includes("sentiment")) return "agent";
  if (n.includes("movement")) return "agent";
  if (n.includes("desk") || n.includes("head") || n.includes("trader")) return "trader";
  if (n.includes("bull")) return "bull";
  if (n.includes("bear")) return "bear";
  if (n.includes("adversarial")) return "risk";
  if (n.includes("news")) return "news";
  if (n.includes("risk")) return "risk";
  if (n.includes("exec") || n.includes("broker") || n.includes("order")) return "exec";
  return "agent";
}

/** Shorten OpenRouter-style model id for the header chip. */
function _shortModelId(modelId) {
  if (modelId == null || modelId === "") return "—";
  const s = String(modelId);
  const tail = s.includes("/") ? s.split("/").pop() : s;
  return tail.length > 28 ? `${tail.slice(0, 26)}…` : tail;
}

function renderLlmModelsHint(agentRuntime) {
  const node = el("llm-models");
  if (!node) return;
  const m = agentRuntime && agentRuntime.llm_models;
  if (!m || typeof m !== "object") {
    node.textContent = "";
    node.title = "";
    return;
  }
  const line = [
    `desk ${_shortModelId(m.desk_head)}`,
    `strat ${_shortModelId(m.strategist)}`,
    `risk ${_shortModelId(m.risk_manager)}`,
  ].join(" · ");
  node.textContent = line;
  node.title = Object.entries(m)
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n");
}

/** Heuristic: model output that is not safe to show as a “summary” line (JSON-ish, numbering junk, etc.). */
function _looksLikeCorruptedReasoning(s) {
  const t = String(s || "").trim();
  if (t.length < 28) return false;
  const letters = (t.match(/[a-zA-Z]/g) || []).length;
  if (letters / t.length < 0.27) return true;
  const punct = (t.match(/[{}[\]|"']/g) || []).length;
  if (punct / t.length > 0.1) return true;
  if (/\b\d+\.\s*[A-Za-z]\s+\d+\./.test(t)) return true;
  if (/(?:BULL|BEAR)\b[^a-z]{0,6}\|/.test(t) && punct >= 5) return true;
  if ((t.match(/",\s*"/g) || []).length >= 2) return true;
  return false;
}

function _isErrorReasoningSnippet(s) {
  const t = String(s || "").trim();
  return /^(RuntimeError|Error|Traceback|Exception|TypeError|ValueError|ConnectionError|HTTPError|OSError|KeyError)/i.test(
    t,
  );
}

function _renderAgentRoster() {
  const host = el("agent-roster");
  if (!host) return;

  /** Default keys align with ``agents/graph`` + tier monitors (idle placeholders). */
  const DEFAULT_AGENT_KEYS = [
    "OptionsSpecialist",
    "SentimentAnalyst",
    "BullResearcher",
    "BearResearcher",
    "Strategist",
    "RiskManager",
    "DeskHead",
    "Trader",
    "AdversarialDebate",
    "News Processor",
    "Movement Tracker",
    "Sentiment Monitor",
    "Execution",
  ];

  // Merge idle defaults + observed agents (so the UI shows the full cast).
  const merged = new Map();
  DEFAULT_AGENT_KEYS.forEach(n =>
    merged.set(n, { lastTs: "", lastAction: "IDLE", lastText: "", errors: 0, count: 0 }),
  );
  for (const [name, v] of _agentRoster.entries()) merged.set(name, v);

  const items = [...merged.entries()]
    .map(([name, v]) => ({ name, ...v }))
    .sort((a, b) => (Date.parse(b.lastTs || "") || 0) - (Date.parse(a.lastTs || "") || 0))
    .slice(0, 18);

  const metaEl = document.getElementById("atlas-agent-deck-meta");
  if (metaEl) {
    const totalEv = items.reduce((s, a) => s + (typeof a.count === "number" ? a.count : 0), 0);
    const activeN = items.filter(a => a.lastTs && String(a.lastAction || "").toUpperCase() !== "IDLE").length;
    metaEl.textContent = `${items.length} roles · ${totalEv} lines · ${activeN} active`;
  }

  if (!items.length) {
    host.innerHTML = `<div class="agent-deck-empty">Waiting for agent events…</div>`;
    return;
  }

  host.innerHTML = items.map(a => {
    const age = _fmtRel(a.lastTs);
    const act = (a.lastAction || "INFO").toUpperCase();
    const label = _displayAgentName(a.name);
    const cnt = typeof a.count === "number" ? a.count : 0;
    const keyAttr = escAttr(a.name);
    const av = esc(_agentAvatar(a.name));
    const idle = !a.lastTs || act === "IDLE";
    const fault = act === "ERROR" || _isErrorReasoningSnippet(a.lastText || "");
    const corrupt = !fault && _looksLikeCorruptedReasoning(a.lastText || "");
    let state = "pulse";
    if (fault) state = "fault";
    else if (corrupt) state = "noise";
    else if (idle) state = "idle";
    else if (act === "HOLD") state = "hold";

    const actDisp = _fmtActionDisplay(act);
    const tip = escAttr(
      `${label}. ${actDisp}${age ? `. ${age}.` : ""}${cnt ? ` ${cnt} lines.` : ""} Click for full log.`,
    );

    return `
      <button type="button" class="agent-pill agent-pill--${state} agent-card-log-btn"
        data-agent="${keyAttr}" title="${tip}">
        <span class="agent-pill-avatar" aria-hidden="true">${av}</span>
        <span class="agent-pill-stack">
          <span class="agent-pill-title">${esc(label || "Agent")}</span>
          <span class="agent-pill-line">${esc(actDisp)}${age ? ` · ${age}` : ""}</span>
        </span>
        <span class="agent-pill-n" aria-label="Lines this session">${cnt > 99 ? "99+" : cnt}</span>
      </button>`;
  }).join("");
}

/** Update per-agent summary from one log row (live stream or bulk hydrate). */
function _touchAgentRosterFromEntry(entry) {
  const name = (entry.agent ?? "Agent").trim() || "Agent";
  const prev = _agentRoster.get(name) || { errors: 0, count: 0 };
  const act = String(entry.action ?? "INFO").toUpperCase();
  const next = {
    lastTs: entry.timestamp || new Date().toISOString(),
    lastAction: act,
    lastText: entry.reasoning || "",
    errors: prev.errors + (act === "ERROR" ? 1 : 0),
    count: (prev.count || 0) + 1,
  };
  _agentRoster.set(name, next);
}

/** Replay today’s JSONL (oldest→newest) into per-agent stats without double-counting vs poll. */
function _hydrateAgentRosterFromLogEntries(entries) {
  if (!Array.isArray(entries) || !entries.length) return;
  const byAgent = new Map();
  for (const e of entries) {
    const key = `${e.timestamp}|${e.agent}|${e.action}`;
    _seenReasoning.add(key);
    const name = (e.agent ?? "Agent").trim() || "Agent";
    const prev = byAgent.get(name) || { errors: 0, count: 0 };
    const act = String(e.action ?? "INFO").toUpperCase();
    byAgent.set(name, {
      count: prev.count + 1,
      errors: prev.errors + (act === "ERROR" ? 1 : 0),
      lastTs: e.timestamp || prev.lastTs || new Date().toISOString(),
      lastAction: act,
      lastText: e.reasoning != null ? String(e.reasoning) : "",
    });
  }
  for (const [name, v] of byAgent) {
    _agentRoster.set(name, v);
  }
  _renderAgentRoster();
}

function appendReasoningEntry(entry) {
  if (!_agentsLive) return;
  if (!entry) return;
  const key = `${entry.timestamp}|${entry.agent}|${entry.action}`;
  if (_seenReasoning.has(key)) return;
  _seenReasoning.add(key);

  const panel = el("reasoning-panel");
  if (panel) {
    const div = buildReasoningEntryElement(entry);
    panel.prepend(div);
    while (panel.children.length > 120) panel.removeChild(panel.lastChild);
  }

  _touchAgentRosterFromEntry(entry);
  _renderAgentRoster();

  // Mirror into the Atlas agent-focus timeline if present (keeps AI front-and-center)
  const focus = el("agent-focus-reasoning");
  if (focus) {
    const clone = buildReasoningEntryElement(entry);
    focus.prepend(clone);
    while (focus.children.length > 80) focus.removeChild(focus.lastChild);
  }

  setAgentActive(true);
  setTimeout(() => setAgentActive(false), 2000);
}

/** Paint idle agent cards immediately; merge today’s JSONL so badges match after refresh. */
function bootstrapAgentRoster() {
  try {
    _renderAgentRoster();
  } catch (e) {
    console.warn("bootstrapAgentRoster", e);
  }
  if (!_agentsLive) return;
  void (async () => {
    try {
      const r = await fetchWithTimeout(`${BACKEND}/reasoning_log?tail=300`, {}, 15000);
      if (!r.ok) return;
      const entries = await r.json();
      _hydrateAgentRosterFromLogEntries(entries);
    } catch { /* silent */ }
  })();
}

function setAgentActive(active) {
  const dot = el("agent-indicator");
  if (dot) dot.className = `agent-dot ${active ? "dot-active" : "dot-idle"}`;
}

// ── Stock Info (fundamentals, peers, ecosystem) ───────────────────────────────

function _stockInfoSkeleton(ticker) {
  const t = String(ticker || "—").toUpperCase();
  _siSet("si-name", `${t} — loading…`);
  _siSet("si-sector", "—");
  for (const id of [
    "si-mktcap", "si-pe", "si-fpe", "si-peg", "si-eps", "si-rev", "si-gm", "si-nm",
    "si-beta", "si-div", "si-52h", "si-52l", "si-roe",
  ]) {
    _siSet(id, "—");
  }
  const recEl = el("si-rec");
  if (recEl) {
    recEl.textContent = "—";
    recEl.className = "si-val";
  }
  const descEl = el("si-desc");
  if (descEl) descEl.textContent = "Loading company profile…";
  _makeChips([], el("si-competitors"), switchTicker);
  _makeChips([], el("si-similar"), switchTicker);
  _makeChips([], el("si-depends-on"), t => switchTicker(t));
  _makeChips([], el("si-depended-by"), t => switchTicker(t));
}

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
  const node = el(id);
  if (!node) return;
  node.textContent = text ?? "—";
  if (cls) node.className = `si-val ${cls}`;
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
  const want = String(activeTicker || "").toUpperCase();
  const got = String(info.ticker || "").toUpperCase();
  if (got && want && got !== want) return;

  _siSet("si-name", info.name || info.ticker);
  const secEl = el("si-sector");
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
  const recEl = el("si-rec");
  if (recEl) {
    recEl.textContent = rec || "—";
    recEl.className   = `si-val ${rec.includes("buy") ? "pos" : rec.includes("sell") ? "neg" : ""}`;
  }

  const descEl = el("si-desc");
  if (descEl) descEl.textContent = info.description || "No description available.";

  // Peers tab
  _makeChips(info.competitors,     el("si-competitors"), switchTicker);
  _makeChips(info.similar_tickers, el("si-similar"),     switchTicker);

  // Ecosystem tab
  _makeChips(info.depends_on,   el("si-depends-on"),   t => switchTicker(t));
  _makeChips(info.depended_by,  el("si-depended-by"),  t => switchTicker(t));

  // If backend returned a fast "pending" stub, retry once shortly after so the
  // panel fills as soon as SQLite is warmed (without blocking the initial click).
  if ((info.data_source || "").toLowerCase() === "pending") {
    const current = String(activeTicker || "").toUpperCase();
    const t = String(info.ticker || "").toUpperCase();
    if (t && current === t) {
      const attempt = Number(renderStockInfo._pendingRetryN || 0) + 1;
      renderStockInfo._pendingRetryN = attempt;
      clearTimeout(renderStockInfo._pendingRetryT);
      const delay = Math.min(2200, 600 + attempt * 350);
      renderStockInfo._pendingRetryT = setTimeout(() => {
        if (String(activeTicker || "").toUpperCase() !== t) return;
        if (attempt <= 5) loadStockInfo(t);
      }, delay);
    }
  }
}

async function loadStockInfo(ticker) {
  try {
    const t = String(ticker || "").toUpperCase();
    // Instant paint from cache (then refresh) — only if still the active ticker
    const cached = _getCache(_stockInfoCache, t, 6 * 60 * 60 * 1000);
    if (cached?.info && String(activeTicker || "").toUpperCase() === t) {
      renderStockInfo(cached.info);
    }

    _stockInfoSeq += 1;
    const seq = _stockInfoSeq;
    try { _stockInfoController?.abort(); } catch { /* ignore */ }
    _stockInfoController = new AbortController();

    const nameEl = el("si-name");
    if (nameEl && String(activeTicker || "").toUpperCase() === t && !cached?.info) {
      nameEl.textContent = `${t} — loading…`;
    }
    const r = await fetchWithTimeout(
      `${BACKEND}/stock_info/${encodeURIComponent(t)}`,
      { signal: _stockInfoController.signal },
      45000,
    );
    if (seq !== _stockInfoSeq) return; // stale
    if (String(activeTicker || "").toUpperCase() !== t) return;
    if (!r.ok) {
      _siSet("si-name", `${t} — error (${r.status})`);
      const d = el("si-desc");
      if (d) d.textContent = "Could not load fundamentals from the API. Is the server running?";
      return;
    }
    const data = await r.json();
    if (seq !== _stockInfoSeq) return; // stale
    if (String(activeTicker || "").toUpperCase() !== t) return;
    _stockInfoCache.set(t, { info: data, at: _now() });
    renderStockInfo(data);
  } catch (e) {
    if (e?.name === "AbortError" || String(e?.message || e).toLowerCase().includes("aborted")) return;
    console.warn("loadStockInfo", e);
    const t = String(ticker || "").toUpperCase();
    if (String(activeTicker || "").toUpperCase() === t) {
      _siSet("si-name", `${t} — failed`);
      const d = el("si-desc");
      if (d) d.textContent = String(e?.message || e) || "Network error loading stock info.";
    }
  }
}

// Tab switching inside the Stock Info panel
document.querySelectorAll(".si-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll(".si-tab").forEach(b => {
      b.classList.remove("si-tab-active");
      b.setAttribute("aria-selected", "false");
    });
    document.querySelectorAll(".si-section").forEach(s => s.classList.remove("si-section-active"));
    btn.classList.add("si-tab-active");
    btn.setAttribute("aria-selected", "true");
    const section = el(`si-${tab}`);
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
  // Prevent overlapping 1Hz polls from piling up on slow networks/WebViews
  if (pollQuote._inFlight) return;
  pollQuote._inFlight = true;
  const elBid   = el("sq-bid");
  const elAsk   = el("sq-ask");
  const elLast  = el("sq-last");
  const elPct   = el("sq-daypct");
  const elSrc   = el("sq-src");
  const elSess  = el("sq-session");
  // Price pill elements
  const ppTicker = el("pp-ticker");
  const ppPrice  = el("pp-price");
  const ppChange = el("pp-change");
  try {
    _quoteSeq += 1;
    const seq = _quoteSeq;
    try { _quoteController?.abort(); } catch { /* ignore */ }
    _quoteController = new AbortController();

    // Fast paint from cache if we have it (makes 1Hz feel instant after switching back)
    const cached = _getCache(_quoteCache, t, 20 * 1000);
    if (cached?.q && ppTicker && ppPrice && ppChange) {
      try {
        const q0 = cached.q;
        if (ppTicker) ppTicker.textContent = t;
        if (ppPrice && q0.last != null) ppPrice.textContent = `$${fmtNum(Number(q0.last), 2)}`;
        const p0 = q0.change_pct;
        const pNum0 = p0 != null && Number.isFinite(Number(p0)) ? Number(p0) : null;
        if (ppChange && pNum0 != null) ppChange.className = pNum0 >= 0 ? "pos" : "neg";
      } catch { /* ignore */ }
    }

    const r = await fetchWithTimeout(
      `${BACKEND}/quote/${encodeURIComponent(t)}`,
      { signal: _quoteController.signal },
      6000,
    );
    if (seq !== _quoteSeq) return; // stale
    if (!r.ok) return;
    const q = await r.json();
    if (seq !== _quoteSeq) return; // stale
    _quoteCache.set(t, { q, at: _now() });
    const asOf = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).format(new Date());

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
      const src =
        s === "alpaca" ? "Alpaca"
        : s === "alphavantage" ? "AlphaV"
        : s === "yfinance" ? "Yahoo"
        : s === "underlying_proxy" ? "Est."
        : "";
      elSrc.textContent = src ? `${src} · ${asOf}` : asOf;
    }
    if (elSess) {
      const { text, title } = quoteSessionStripLabel(q.session);
      elSess.textContent = text;
      elSess.title = title;
    }

    const sdLast = el("sd-last");
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
      // Pulse on every successful quote poll so user sees “live” even if price doesn't change.
      ppPrice.classList.remove("pp-pulse");
      void ppPrice.offsetWidth;
      ppPrice.classList.add("pp-pulse");
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
  } catch (e) {
    if (e?.name === "AbortError" || String(e?.message || e).toLowerCase().includes("aborted")) return;
    /* silent */
  }
  finally {
    pollQuote._inFlight = false;
    // Optional debug hook for “static” complaints
    window.__lastQuotePollAt = Date.now();
  }
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
      const pill = el("ws-indicator");
      if (pill) { pill.textContent = "● LIVE"; pill.className = "topbar-badge ws-indicator ws-live"; }
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
          // Update tier signal bar with live T1 data
          if (msg.tiers) updateTierBar(msg.tiers);
          // Sync trading mode from server
          if (msg.trading_mode && msg.trading_mode !== _currentMode) _applyMode(msg.trading_mode);
        }
        if (msg.type === "quote") {
          // Instant quote push (removes 1Hz “wait” feel)
          const t = String(msg.ticker || "").toUpperCase();
          if (t) _quoteCache.set(t, { q: msg, at: _now() });
          if (t && t === String(activeTicker || "").toUpperCase()) {
            // Reuse existing renderer via pollQuote cache path:
            // paint directly with minimal DOM work
            const q = msg;
            const elBid   = el("sq-bid");
            const elAsk   = el("sq-ask");
            const elLast  = el("sq-last");
            const elPct   = el("sq-daypct");
            const elSrc   = el("sq-src");
            const elSess  = el("sq-session");
            const ppTicker = el("pp-ticker");
            const ppPrice  = el("pp-price");
            const ppChange = el("pp-change");

            if (elBid) elBid.textContent = q.bid  != null ? fmtNum(q.bid,  2) : "—";
            if (elAsk) elAsk.textContent = q.ask  != null ? fmtNum(q.ask,  2) : "—";
            if (elLast) elLast.textContent = q.last != null ? fmtNum(q.last, 2) : "—";

            const p = q.change_pct;
            const pNum = p != null && Number.isFinite(Number(p)) ? Number(p) : null;
            if (elPct) {
              if (pNum == null) { elPct.textContent = "—"; elPct.className = "sq-val"; }
              else {
                elPct.textContent = `${pNum >= 0 ? "+" : ""}${pNum.toFixed(2)}%`;
                elPct.className = `sq-val ${pNum >= 0 ? "pos" : "neg"}`;
              }
            }
            if (elSrc) {
              const s = (q.source || "").toString();
              const src = s === "alpaca" ? "Alpaca" : s === "alphavantage" ? "AlphaV" : s === "yfinance" ? "Yahoo" : s === "underlying_proxy" ? "Est." : "";
              elSrc.textContent = src ? `${src} · LIVE` : "LIVE";
            }
            if (elSess) {
              const { text, title } = quoteSessionStripLabel(q.session);
              elSess.textContent = text;
              elSess.title = title;
            }
            if (ppTicker) ppTicker.textContent = t;
            if (ppPrice && q.last != null) {
              const cur = Number(q.last);
              ppPrice.textContent = `$${fmtNum(cur, 2)}`;
              ppPrice.classList.remove("pp-pulse");
              void ppPrice.offsetWidth;
              ppPrice.classList.add("pp-pulse");
            }
            if (ppChange) {
              if (pNum == null) { ppChange.textContent = "—"; ppChange.className = ""; }
              else {
                const last  = Number(q.last ?? 0);
                const chgPct = pNum / 100;
                const prevClose = last / (1 + chgPct);
                const chgDollar = last - prevClose;
                ppChange.textContent = `${chgDollar >= 0 ? "+" : ""}${chgDollar.toFixed(2)} (${pNum >= 0 ? "+" : ""}${pNum.toFixed(2)}%)`;
                ppChange.className = pNum >= 0 ? "pos" : "neg";
              }
            }
          }
        }
        if (msg.type === "news") {
          // Fast-track urgent news (delta update)
          const item = msg.item;
          if (item) {
            renderNews([item]);
            if ((item.urgency_tier || "").toUpperCase() === "T0") {
              try { showToast(`URGENT: ${String(item.headline || "").slice(0, 90)}`, "warn", 4500); } catch {}
            }
          }
        }
      } catch { /* ignore malformed */ }
    };

    _ws.onerror = () => { /* handled by onclose */ };

    _ws.onclose = () => {
      _wsConnected = false;
      const pill = el("ws-indicator");
      if (pill) { pill.textContent = "● POLL"; pill.className = "topbar-badge ws-indicator ws-poll"; }
      // Exponential back-off (cap 30 s)
      _wsRetryMs = Math.min(30000, _wsRetryMs * 1.5);
      _wsRetryTimer = setTimeout(_connectWS, _wsRetryMs);
    };
  } catch (e) {
    _wsRetryTimer = setTimeout(_connectWS, _wsRetryMs);
  }
}

// ── Backend polling (fallback when WS is disconnected) ───────────────────────

async function pollState() {
  // Always poll REST /state: WebSocket pushes are incremental; REST is the source of truth
  // for account balances, fundamentals-adjacent fields, and avoids “stuck dashes” if WS is flaky.
  try {
    const r = await fetchWithTimeout(`${BACKEND}/state`, {}, 4000);
    if (!r.ok) return;
    const s = await r.json();
    if (!window.__atlasStateSeen) {
      window.__atlasStateSeen = true;
      showToast("Connected: /state OK", "ok", 2500);
    }
    updateMetrics(s);
  } catch { /* silent */ }
}

async function pollReasoningLog() {
  if (!_agentsLive) return;
  try {
    const r = await fetchWithTimeout(`${BACKEND}/reasoning_log?tail=200`, {}, 12000);
    if (!r.ok) return;
    const entries = await r.json();
    entries.slice(-100).forEach(appendReasoningEntry);
  } catch { /* silent */ }
}

/** Fallback if `/state` is an older backend without `agent_runtime`. */
async function pollAgentStatus() {
  try {
    const r = await fetchWithTimeout(`${BACKEND}/agent_status`, {}, 3000);
    if (!r.ok) return;
    const st = await r.json();
    renderAgentStatus(st);
    renderLlmModelsHint(st);
  } catch { /* silent */ }
}

// ── Tier signal bar ─────────────────────────────────────────────────────────────

function updateTierBar(tiers) {
  const fmtSignal = (v) => {
    const s = (v >= 0 ? "+" : "") + Number(v).toFixed(3);
    return s;
  };
  const signalClass = (v) => {
    const n = Number(v);
    if (n > 0.4)  return "tier-val strong-pos";
    if (n > 0.15) return "tier-val pos";
    if (n < -0.4) return "tier-val strong-neg";
    if (n < -0.15)return "tier-val neg";
    return "tier-val";
  };

  // Sentiment
  const sent = el("tb-sentiment");
  if (sent && tiers.sentiment_signal != null) {
    sent.textContent = fmtSignal(tiers.sentiment_signal);
    sent.className   = signalClass(tiers.sentiment_signal);
  }

  // Movement
  const mov = el("tb-movement");
  if (mov && tiers.movement_signal != null) {
    mov.textContent = fmtSignal(tiers.movement_signal);
    mov.className   = signalClass(tiers.movement_signal);
  }

  // Anomaly badge
  const anm = el("tb-anomaly");
  if (anm) anm.style.display = tiers.movement_anomaly ? "inline-flex" : "none";

  // Bull / Bear conviction
  const bull = el("tb-bull");
  if (bull && tiers.bull_conviction != null && tiers.bull_conviction > 0) {
    bull.textContent = `${tiers.bull_conviction}/10`;
  }
  const bear = el("tb-bear");
  if (bear && tiers.bear_conviction != null && tiers.bear_conviction > 0) {
    bear.textContent = `${tiers.bear_conviction}/10`;
  }

  // T3 status pill
  const t3s = el("tb-t3-status");
  const runBtn = el("tb-run-t3");
  if (t3s) {
    if (tiers.t3_active) {
      t3s.textContent = "RUNNING";
      t3s.className = "tier-t3-status tier-running";
      if (runBtn) runBtn.disabled = true;
    } else if (tiers.last_tier3_run) {
      t3s.textContent = "DONE";
      t3s.className = "tier-t3-status tier-done";
      if (runBtn) runBtn.disabled = false;
    } else {
      t3s.textContent = "IDLE";
      t3s.className = "tier-t3-status tier-idle";
      if (runBtn) runBtn.disabled = false;
    }
  }

  // Last run time
  const lr = el("tb-last-run");
  if (lr && tiers.last_tier3_run) {
    const d = new Date(tiers.last_tier3_run);
    lr.textContent = "last: " + d.toLocaleTimeString("en-US", {
      timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  }
}

// ── T3 run button ─────────────────────────────────────────────────────────────

el("tb-run-t3")?.addEventListener("click", async () => {
  const btn = el("tb-run-t3");
  const st  = el("tb-t3-status");
  if (btn) btn.disabled = true;
  if (st)  { st.textContent = "STARTING…"; st.className = "tier-t3-status tier-running"; }
  try {
    const r = await fetch(`${BACKEND}/tiers/trigger`, { method: "POST" });
    const j = await r.json();
    if (j.error) {
      if (st) { st.textContent = j.error.slice(0, 20).toUpperCase(); st.className = "tier-t3-status tier-idle"; }
      if (btn) btn.disabled = false;
    }
  } catch (e) {
    if (btn) btn.disabled = false;
  }
});

// ── Mode toggle (Advisory / Autopilot) ──────────────────────────────────────

let _currentMode = "advisory";

document.querySelectorAll("#atlas-mode-toggle .mode-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    const mode = btn.dataset.mode;
    if (mode === _currentMode) return;
    try {
      const r = await fetch(`${BACKEND}/mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      const j = await r.json();
      if (j.mode) _applyMode(j.mode);
    } catch { /* ignore */ }
  });
});

function _applyMode(mode) {
  _currentMode = mode;
  document.querySelectorAll("#atlas-mode-toggle .mode-btn").forEach(b => {
    b.classList.toggle("mode-active", b.dataset.mode === mode);
  });
  document.body.classList.toggle("autopilot-mode", mode === "autopilot");
}

// ── Recommendations panel ────────────────────────────────────────────────────

let _lastRecCount = -1;

/** Short relative time for recommendation cards (ISO string from API). */
function _fmtRecAge(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return "just now";
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch {
    return "";
  }
}

function _renderRecCard(rec) {
  const asset = String(rec.asset_type || "option");
  const confClass = rec.confidence >= 0.7 ? "rec-conf-high"
                  : rec.confidence >= 0.4 ? "rec-conf-mid"
                  : "rec-conf-low";
  const isPending = rec.status === "pending";
  const pct = Math.round((Number(rec.confidence) || 0) * 100);
  const bull = Math.min(10, Math.max(0, Number(rec.bull_conviction) || 0));
  const bear = Math.min(10, Math.max(0, Number(rec.bear_conviction) || 0));
  const age = _fmtRecAge(rec.created_at);
  const resolvedAge = _fmtRecAge(rec.resolved_at);

  const actionsHtml = isPending
    ? `<div class="rec-card-actions">
         <button type="button" class="rec-btn rec-btn-dismiss" data-rec-id="${esc(rec.id)}" data-action="dismiss">Dismiss</button>
         <button type="button" class="rec-btn rec-btn-approve" data-rec-id="${esc(rec.id)}" data-action="approve">Approve</button>
       </div>`
    : (() => {
        const s = String(rec.status || "");
        const pillCls = s === "approved" ? "approved"
          : s === "dismissed" ? "dismissed"
            : s === "expired" ? "expired" : "other";
        const pillLabel = s === "approved" ? "Approved"
          : s === "dismissed" ? "Dismissed"
            : s === "expired" ? "Expired" : s || "Settled";
        return `<span class="rec-status-pill rec-status-pill--${pillCls}">${pillLabel}</span>`;
      })();

  const maxRisk = rec.proposal ? Number(rec.proposal.max_risk || 0) : 0;
  const target = rec.proposal ? Number(rec.proposal.target_return || 0) : 0;
  const stockQty = rec.stock_proposal ? Number(rec.stock_proposal.qty || 0) : 0;
  const stockSide = rec.stock_proposal ? String(rec.stock_proposal.side || "") : "";
  const statsHtml = rec.proposal
    ? `<div class="rec-card-stats">
         <div class="rec-stat"><span class="rec-stat-label">Max risk</span><span class="rec-stat-val">$${maxRisk.toFixed(0)}</span></div>
         <div class="rec-stat"><span class="rec-stat-label">Target</span><span class="rec-stat-val">$${target.toFixed(0)}</span></div>
       </div>`
    : rec.stock_proposal
      ? `<div class="rec-card-stats">
           <div class="rec-stat"><span class="rec-stat-label">Order</span><span class="rec-stat-val">${esc(stockSide)} ${Number.isFinite(stockQty) ? stockQty : 0} sh</span></div>
         </div>`
    : "";

  const reasoning = esc(rec.desk_head_reasoning || "").slice(0, 900);

  const legs = rec.proposal?.legs || [];
  const pricingLegs = rec.pricing?.legs || [];
  const quoteBySym = new Map(pricingLegs.map(l => [String(l.symbol || ""), l]));
  const missingQuotes = rec.pricing?.missing_quotes || [];

  const quoteNote = rec.pricing?.quote_note ? esc(String(rec.pricing.quote_note)) : "";
  const legsTable = legs.length
    ? `<div class="rec-details-block">
         <div class="rec-details-title">Proposed legs</div>
         <div class="rec-details-sub">Multi‑leg strategies combine <b>BUY</b> and <b>SELL</b> in one order: you are opening both legs (e.g. buy a lower strike and sell a higher strike for a vertical spread). The <b>SELL</b> is <i>sell to open</i> the short leg — not selling an option you already own.</div>
         <div class="rec-legs-wrap">
           <table class="rec-legs">
             <thead>
               <tr>
                 <th>Side</th><th>Symbol</th><th class="num">Qty</th><th class="num">Bid</th><th class="num">Ask</th><th class="num">Mid</th>
               </tr>
             </thead>
             <tbody>
               ${legs.map(l => {
                 const sym = String(l.symbol || "");
                 const q = quoteBySym.get(sym) || {};
                 const bid = q.bid != null ? Number(q.bid).toFixed(2) : "—";
                 const ask = q.ask != null ? Number(q.ask).toFixed(2) : "—";
                 const mid = q.mid != null ? Number(q.mid).toFixed(2) : "—";
                 const side = esc(String(l.side || ""));
                 const qty = Number(l.qty || 1);
                 const exp = q.expired ? " <span class=\"rec-leg-expired\" title=\"Expired series — no live quote\">expired</span>" : "";
                 return `<tr>
                   <td><span class="rec-leg-side rec-leg-side--${side.startsWith("S") ? "sell" : "buy"}">${side}</span></td>
                   <td class="sym">${esc(sym)}${exp}</td>
                   <td class="num">${qty}</td>
                   <td class="num">${bid}</td>
                   <td class="num">${ask}</td>
                   <td class="num">${mid}</td>
                 </tr>`;
               }).join("")}
             </tbody>
           </table>
         </div>
         ${quoteNote ? `<div class="rec-warn">${quoteNote}</div>` : ""}
         ${missingQuotes?.length ? `<div class="rec-warn">Missing live quotes for: ${missingQuotes.map(esc).join(", ")}. Approve cannot submit until these have bid/ask (often because the OCC symbol is expired or not in the chain cache).</div>` : ""}
       </div>`
  : "";

  const netMid = rec.pricing?.net_premium_mid_usd;
  const maxLossEst = rec.pricing?.max_loss_estimate_usd;
  const riskMath = rec.pricing?.risk_math;
  const proposalRationale = esc(rec.proposal?.rationale || "").slice(0, 2000);
  const stockRationale = esc(rec.stock_proposal?.rationale || "").slice(0, 2000);
  const sl = rec.proposal?.stop_loss_pct != null ? Number(rec.proposal.stop_loss_pct) : null;
  const tp = rec.proposal?.take_profit_pct != null ? Number(rec.proposal.take_profit_pct) : null;

  const riskExplain = rec.proposal
    ? `<div class="rec-details-block">
         <div class="rec-details-title">Risk & target (why these numbers?)</div>
         <div class="rec-details-text">
           <div><b>Max risk</b> is the desk’s stated worst‑case loss (USD) for this 1× proposal. <b>Target</b> is the intended P&L objective (USD).</div>
           <div class="rec-details-sub">These are produced by the <b>Strategist</b> agent as part of the proposal (see strategy rules), then checked against your position cap by Risk.</div>
           ${(sl != null || tp != null) ? `<div class="rec-details-sub">Stops: stop‑loss ${(sl != null ? `${Math.round(sl * 100)}%` : "—")} · take‑profit ${(tp != null ? `${Math.round(tp * 100)}%` : "—")}.</div>` : ""}
         </div>
         <div class="rec-details-kv">
           <div class="rec-kv"><span>From proposal</span><span>$${maxRisk.toFixed(0)} max risk · $${target.toFixed(0)} target</span></div>
           ${netMid != null ? `<div class="rec-kv"><span>Net premium (mid)</span><span>${netMid >= 0 ? "+" : "−"}$${Math.abs(Number(netMid)).toFixed(2)}</span></div>` : `<div class="rec-kv"><span>Net premium (mid)</span><span>—</span></div>`}
           ${maxLossEst != null ? `<div class="rec-kv"><span>Max loss (est)</span><span>$${Number(maxLossEst).toFixed(2)}</span></div>` : ""}
         </div>
         ${riskMath ? `<div class="rec-details-sub">${esc(String(riskMath))}</div>` : ""}
       </div>`
    : rec.stock_proposal
      ? `<div class="rec-details-block">
           <div class="rec-details-title">Stock order</div>
           <div class="rec-details-text">
             <div><b>Side</b>: ${esc(stockSide)} · <b>Qty</b>: ${Number.isFinite(stockQty) ? stockQty : 0} shares</div>
             <div class="rec-details-sub">Order type: ${esc(String(rec.stock_proposal.order_type || "market"))}${rec.stock_proposal.limit_price != null ? ` · Limit $${Number(rec.stock_proposal.limit_price).toFixed(2)}` : ""}</div>
             ${(rec.stock_proposal.stop_loss_pct != null || rec.stock_proposal.take_profit_pct != null)
               ? `<div class="rec-details-sub">Guidance: stop‑loss ${rec.stock_proposal.stop_loss_pct != null ? `${Math.round(Number(rec.stock_proposal.stop_loss_pct)*100)}%` : "—"} · take‑profit ${rec.stock_proposal.take_profit_pct != null ? `${Math.round(Number(rec.stock_proposal.take_profit_pct)*100)}%` : "—"}.</div>`
               : ""}
           </div>
         </div>`
    : "";

  const rationaleBlock = proposalRationale
    ? `<div class="rec-details-block">
         <div class="rec-details-title">Strategist rationale</div>
         <div class="rec-details-text rec-details-text--mono">${proposalRationale}</div>
       </div>`
    : stockRationale
      ? `<div class="rec-details-block">
           <div class="rec-details-title">StockSpecialist rationale</div>
           <div class="rec-details-text rec-details-text--mono">${stockRationale}</div>
         </div>`
    : "";

  const detailsHtml = (legsTable || riskExplain || rationaleBlock)
    ? `<details class="rec-details">
         <summary class="rec-details-summary">Details</summary>
         <div class="rec-details-body">
           ${riskExplain}
           ${legsTable}
           ${rationaleBlock}
         </div>
       </details>`
    : "";

  const timeBadges = isPending
    ? (age ? `<span class="rec-card-age" title="Created">${age}</span>` : "")
    : [
        resolvedAge ? `<span class="rec-card-age rec-card-age--resolved" title="Resolved">${resolvedAge}</span>` : "",
        age ? `<span class="rec-card-age rec-card-age--created" title="Created">${age}</span>` : "",
      ].filter(Boolean).join("");

  return `
    <article class="rec-card ${isPending ? "rec-card--pending" : "rec-card--settled"}" data-rec-id="${esc(rec.id)}" data-rec-status="${esc(rec.status)}">
      <div class="rec-card-top">
        <div class="rec-card-identity">
          <span class="rec-card-ticker">${esc(rec.ticker)}</span>
          <h3 class="rec-card-strategy">${esc(rec.strategy_name)}</h3>
        </div>
        <div class="rec-card-badges">
          ${timeBadges}
          <span class="rec-card-asset" title="Asset type">${esc(asset.toUpperCase())}</span>
          <span class="rec-card-confidence ${confClass}" title="Desk confidence">${pct}%</span>
        </div>
      </div>
      <div class="rec-conviction-bars" aria-label="Bull and bear conviction">
        <div class="rec-conv-row">
          <span class="rec-conv-name">Bull</span>
          <div class="rec-conv-track" role="presentation"><div class="rec-conv-fill rec-conv-fill--bull" style="width:${bull * 10}%"></div></div>
          <span class="rec-conv-num">${bull}/10</span>
        </div>
        <div class="rec-conv-row">
          <span class="rec-conv-name">Bear</span>
          <div class="rec-conv-track" role="presentation"><div class="rec-conv-fill rec-conv-fill--bear" style="width:${bear * 10}%"></div></div>
          <span class="rec-conv-num">${bear}/10</span>
        </div>
      </div>
      ${reasoning ? `<p class="rec-card-reasoning">${reasoning}</p>` : ""}
      ${statsHtml}
      ${detailsHtml}
      <div class="rec-card-footer">
        ${actionsHtml}
      </div>
    </article>`;
}

async function pollRecommendations() {
  try {
    const r = await fetchWithTimeout(`${BACKEND}/recommendations`, {}, 3000);
    if (!r.ok) return;
    const recs = await r.json();
    renderRecommendations(recs);
  } catch { /* ignore */ }
}

function renderRecommendations(recs) {
  const panel = el("recommendations-panel");
  const count = el("rec-count");
  if (!panel) return;

  // Preserve UX across the 5s poll refresh:
  // - keep which cards had <details open>
  // - keep scroll position so reading doesn't collapse/jump
  const prevScrollTop = panel.scrollTop;
  const openIds = new Set(
    Array.from(panel.querySelectorAll("article.rec-card details.rec-details[open]"))
      .map(d => d.closest("article.rec-card")?.dataset?.recId)
      .filter(Boolean),
  );

  const pending = recs.filter(r => r.status === "pending");
  const recSortTs = r => {
    const t = r.resolved_at || r.created_at;
    const ms = t ? Date.parse(t) : 0;
    return Number.isFinite(ms) ? ms : 0;
  };
  const history = recs
    .filter(r => r.status !== "pending")
    .sort((a, b) => recSortTs(b) - recSortTs(a));

  if (count) {
    const parts = [];
    if (pending.length) parts.push(`${pending.length} open`);
    if (history.length) parts.push(`${history.length} in history`);
    count.textContent = parts.join(" · ") || "";
  }

  if (pending.length === 0 && history.length === 0) {
    panel.innerHTML = `
      <div class="rec-empty">
        <div class="rec-empty-title">No recommendations</div>
        <div class="rec-empty-sub">When the desk proposes a trade in advisory mode, it will appear here for your review.</div>
      </div>`;
    return;
  }

  const parts = [];
  if (pending.length) {
    parts.push(
      `<div class="rec-section">
        <div class="rec-section-label">Awaiting your decision</div>
        ${pending.map(_renderRecCard).join("")}
      </div>`,
    );
  }
  if (history.length) {
    parts.push(
      `<div class="rec-section rec-section--history">
        <div class="rec-section-head">
          <div class="rec-section-label">History</div>
          <div class="rec-section-hint">Approved &amp; dismissed this session (newest first)</div>
        </div>
        <div class="rec-history-scroll">
          ${history.map(_renderRecCard).join("")}
        </div>
      </div>`,
    );
  }
  panel.innerHTML = parts.join("");

  // Restore expanded state + scroll after re-render.
  if (openIds.size) {
    panel.querySelectorAll("article.rec-card").forEach(card => {
      const id = card.dataset.recId;
      if (!id || !openIds.has(id)) return;
      const details = card.querySelector("details.rec-details");
      if (details) details.open = true;
    });
  }
  panel.scrollTop = prevScrollTop;

  // Wire approve/dismiss buttons
  panel.querySelectorAll(".rec-btn").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      const id = e.currentTarget.dataset.recId;
      const action = e.currentTarget.dataset.action;
      if (!id || !action) return;
      const row = e.currentTarget.closest(".rec-card-actions");
      row?.querySelectorAll(".rec-btn").forEach(b => {
        b.disabled = true;
      });
      e.currentTarget.classList.add("rec-btn--working");
      const label = action === "approve" ? "Approving…" : "Dismissing…";
      e.currentTarget.textContent = label;
      try {
        const res = await fetch(`${BACKEND}/recommendations/${id}/${action}`, { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          const detail = data.detail || data.message || res.statusText || "Request failed";
          showToast(String(detail), "err", 7000);
        } else if (data.error) {
          showToast(String(data.error), "warn", 8000);
        } else if (data.note) {
          showToast(String(data.note), "info", 8000);
        } else if (action === "approve" && data.status === "approved") {
          const tail = data.order_result ? ` ${String(data.order_result).slice(0, 120)}` : "";
          showToast(`Approved.${tail}`, "ok", 6000);
        } else if (action === "dismiss") {
          showToast("Recommendation dismissed.", "info", 4000);
        } else {
          showToast(action === "approve" ? "Approved." : "Updated.", "ok", 4000);
        }
        pollRecommendations();
      } catch (err) {
        showToast(`Could not ${action}: ${err?.message || err}`, "err", 7000);
        pollRecommendations();
      }
    });
  });
}

// Poll recommendations every 5s
setInterval(pollRecommendations, 5000);
setTimeout(pollRecommendations, 1000);

// ── News Impact Strip (cross-stock impacts from AI-processed news) ───────────

let _lastImpactData = {};

async function pollNewsImpacts() {
  try {
    const r = await fetchWithTimeout(`${BACKEND}/news/impacts`, {}, 4000);
    if (!r.ok) return;
    const data = await r.json();
    if (JSON.stringify(data) !== JSON.stringify(_lastImpactData)) {
      _lastImpactData = data;
      renderImpactStrip(data);
    }
  } catch { /* ignore */ }
}

function renderImpactStrip(impacts) {
  const strip = el("news-impact-strip");
  const countEl = el("news-processed-count");
  if (!strip) return;

  const entries = Object.entries(impacts)
    .map(([ticker, info]) => ({ ticker, ...info }))
    .filter(e => Math.abs(e.total_impact) > 0.05)
    .sort((a, b) => Math.abs(b.total_impact) - Math.abs(a.total_impact))
    .slice(0, 10);

  if (countEl) countEl.textContent = entries.length > 0 ? `${Object.keys(impacts).length} impacts` : "";

  if (entries.length === 0) {
    strip.innerHTML = "";
    return;
  }

  const lead = `<span class="impact-strip-label">TOP IMPACTS</span>`;
  strip.innerHTML = lead + entries.map(e => {
    const score = e.total_impact;
    const cls = score > 0 ? "pos" : score < 0 ? "neg" : "";
    const sign = score > 0 ? "+" : "";
    const rel = (e.relationships || []).slice(0, 2).join(", ");
    const tip = `${e.ticker}: impact ${sign}${score.toFixed(2)} from ${e.article_count} article${e.article_count === 1 ? "" : "s"}${rel ? ` — ${rel}` : ""}`;
    return `<button type="button" class="impact-chip" title="${escAttr(tip)}" aria-label="${escAttr(tip)}">
      <span class="impact-chip-ticker">${esc(e.ticker)}</span>
      <span class="impact-chip-score ${cls}">${sign}${score.toFixed(2)}</span>
      <span class="impact-chip-count">(${e.article_count})</span>
    </button>`;
  }).join("");

  // Click on impact chip → change active ticker
  strip.querySelectorAll(".impact-chip").forEach(chip => {
    chip.addEventListener("click", () => {
      const ticker = chip.querySelector(".impact-chip-ticker")?.textContent?.trim();
      if (ticker) switchTicker(ticker);
    });
  });
}

// Poll impacts every 30s (they update every 5min on backend)
setInterval(pollNewsImpacts, 30000);
setTimeout(pollNewsImpacts, 5000);

// ─────────────────────────────────────────────────────────────────────────────

function renderAgentStatus(st) {
  const badgeEl = el("agent-status");
  if (!badgeEl) return;

  const inProgress = !!st.in_progress;
  const lastOkAge  = typeof st.age_since_success_s === "number" ? st.age_since_success_s : null;
  const lastErrAge = typeof st.age_since_error_s === "number" ? st.age_since_error_s : null;
  const decision   = st.last_trader_decision || "--";
  const cyclesTotal = typeof st.cycles_total === "number" ? st.cycles_total : 0;

  let cls = "topbar-badge agent-status agent-status-unknown";
  let label = `AGENTS: ${decision}`;

  if (inProgress) {
    cls = "topbar-badge agent-status agent-status-running";
    label = "AGENTS: RUN";
  } else if (cyclesTotal === 0) {
    cls = "topbar-badge agent-status agent-status-unknown";
    label = "AGENTS: IDLE";
  } else if (lastErrAge !== null && lastErrAge < 300) {
    cls = "topbar-badge agent-status agent-status-error";
    const hint = shortAgentErrorHint(st.last_error);
    label = hint ? `AGENTS: ERR · ${hint}` : "AGENTS: ERR";
  } else if (lastOkAge !== null && lastOkAge <= 300) {
    cls = "topbar-badge agent-status agent-status-ok";
    label = `AGENTS: ${decision}`;
  } else if (lastOkAge !== null && lastOkAge > 300) {
    cls = "topbar-badge agent-status agent-status-stale";
    label = "AGENTS: STALE";
  }

  badgeEl.className = cls;
  badgeEl.textContent = label;

  const parts = [];
  if (typeof st.cycles_total === "number") parts.push(`cycles=${st.cycles_total} ok=${st.cycles_ok} err=${st.cycles_error}`);
  if (typeof st.last_cycle_duration_s === "number" && st.last_cycle_duration_s) parts.push(`last=${st.last_cycle_duration_s.toFixed(2)}s`);
  if (lastOkAge !== null) parts.push(`ok_age=${fmtAge(lastOkAge)}`);
  if (lastErrAge !== null) parts.push(`err_age=${fmtAge(lastErrAge)}`);
  if (st.last_error) parts.push(`last_error=${String(st.last_error).slice(0, 140)}`);
  if (st.llm_models && typeof st.llm_models === "object") {
    parts.push(
      `models=${Object.entries(st.llm_models)
        .map(([k, v]) => `${k}:${_shortModelId(v)}`)
        .join(" ")}`,
    );
  }
  badgeEl.title = parts.join(" · ");
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

el("gobar")?.addEventListener("keydown", e => {
  if (e.key !== "Enter") return;
  const cmd = e.target.value.trim().toUpperCase();
  e.target.value = "";

  if (/^[A-Z]{1,5}$/.test(cmd)) {
    switchTicker(cmd);
  } else if (cmd === "KILL") {
    handleKillSwitch();
  } else if (cmd === "CLEAR") {
    el("reasoning-panel").innerHTML = "";
    _seenReasoning.clear();
  } else if (cmd.startsWith("SORT ")) {
    const s = cmd.slice(5).toLowerCase();
    if (["iv","pc","oi","ticker","price","chg"].includes(s)) {
      scannerSort = s;
      el("scanner-sort").value = s;
      pollScanner();
    }
  }
});

// ── Kill switch ───────────────────────────────────────────────────────────────

el("kill-btn")?.addEventListener("click", () => {
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

function escAttr(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

function _benchmarkSectionsMetaSig(sections, items) {
  if (Array.isArray(sections) && sections.length) {
    return sections
      .map((s) => {
        const id = String(s.id || "");
        const tickers = (Array.isArray(s.items) ? s.items : [])
          .map((it) => String(it.ticker || "").toUpperCase())
          .join(",");
        return `${id}:${tickers}`;
      })
      .join("|");
  }
  if (Array.isArray(items) && items.length) {
    return `flat:${items.map((it) => String(it.ticker || "").toUpperCase()).join(",")}`;
  }
  return "";
}

/** Preserve horizontal scroll on each benchmark chip row across DOM refresh. */
function _snapshotBenchChipsScroll(wrap) {
  /** @type {Record<string, number>} */
  const out = {};
  if (!wrap) return out;
  wrap.querySelectorAll(".atlas-bench-group[data-bench-section]").forEach((grp) => {
    const id = grp.getAttribute("data-bench-section");
    const chips = grp.querySelector(".atlas-bench-group-chips");
    if (id && chips) out[id] = chips.scrollLeft || 0;
  });
  return out;
}

function _restoreBenchChipsScroll(wrap, scrollBySection) {
  if (!wrap || !scrollBySection) return;
  requestAnimationFrame(() => {
    wrap.querySelectorAll(".atlas-bench-group[data-bench-section]").forEach((grp) => {
      const id = grp.getAttribute("data-bench-section");
      const chips = grp.querySelector(".atlas-bench-group-chips");
      if (!id || !chips) return;
      if (Object.prototype.hasOwnProperty.call(scrollBySection, id)) {
        chips.scrollLeft = scrollBySection[id];
      }
    });
  });
}

function _benchmarkChipHtml(it) {
  const sym = String(it.ticker || "").toUpperCase();
  const tEsc = esc(sym);
  const tAttr = escAttr(sym);
  const lastRaw = it.last;
  const last =
    lastRaw != null && Number.isFinite(Number(lastRaw))
      ? Number(lastRaw).toFixed(2)
      : "—";
  const chgRaw = it.change_pct;
  let chgText = "—";
  let chgCls = "atlas-bench-chg";
  if (chgRaw != null && Number.isFinite(Number(chgRaw))) {
    const c = Number(chgRaw);
    const sign = c >= 0 ? "+" : "";
    chgText = `${sign}${c.toFixed(2)}%`;
    chgCls += c >= 0 ? " atlas-bench-chg--up" : " atlas-bench-chg--down";
  }
  const src = it.quote_source ? String(it.quote_source) : "";
  const titleRaw = src ? `${sym} · last & day % (${src})` : `${sym} · last & day %`;
  const titleAttr = escAttr(titleRaw);
  return (
    `<button type="button" class="atlas-bench-chip" data-ticker="${tAttr}" title="${titleAttr}">` +
    `<span class="atlas-bench-sym">${tEsc}</span>` +
    `<div class="atlas-bench-metrics">` +
    `<span class="atlas-bench-last">${last}</span>` +
    `<span class="${chgCls}">${chgText}</span>` +
    `</div></button>`
  );
}

/** Index / sector ETF strip under tier bar (GET /quotes/benchmarks). */
async function pollBenchmarkStrip() {
  if (document.visibilityState !== "visible") return;
  const wrap = document.getElementById("atlas-context-strip-scroll");
  if (!wrap) return;
  try {
    const r = await fetchWithTimeout(`${BACKEND}/quotes/benchmarks`, {}, 10000);
    if (!r.ok) return;
    const data = await r.json();
    const sections = Array.isArray(data.sections) ? data.sections : [];
    const items = Array.isArray(data.items) ? data.items : [];
    const metaSig = _benchmarkSectionsMetaSig(sections, items);
    const prevChipsScroll = _snapshotBenchChipsScroll(wrap);

    if (sections.length) {
      _scannerBenchSections = sections.map((s) => ({
        id: String(s.id || ""),
        label: String(s.label || ""),
        tickers: (Array.isArray(s.items) ? s.items : []).map((it) =>
          String(it.ticker || "").toUpperCase()
        ),
      }));
      _benchmarkTickerSet = new Set();
      for (const s of _scannerBenchSections) {
        for (const t of s.tickers) _benchmarkTickerSet.add(t);
      }
    } else {
      _scannerBenchSections = null;
      _benchmarkTickerSet = null;
    }

    if (!sections.length && !items.length) {
      _benchmarkMetaSig = "";
      wrap.innerHTML = "<span class=\"atlas-bench-hint\">No benchmark list</span>";
      try {
        renderScanner();
      } catch { /* ignore */ }
      return;
    }

    if (sections.length) {
      const parts = [];
      for (const sec of sections) {
        const label = esc(String(sec.label || ""));
        const secId = String(sec.id || "").trim() || `sec-${parts.length}`;
        const secIdAttr = escAttr(secId);
        const chips = (Array.isArray(sec.items) ? sec.items : []).map(_benchmarkChipHtml).join("");
        if (!chips) continue;
        parts.push(
          `<div class="atlas-bench-group" data-bench-section="${secIdAttr}">` +
          `<span class="atlas-bench-group-label">${label}</span>` +
          `<div class="atlas-bench-group-chips">${chips}</div>` +
          `</div>`
        );
      }
      wrap.innerHTML = `<div class="atlas-bench-groups">${parts.join("")}</div>`;
    } else {
      wrap.innerHTML = `<div class="atlas-bench-groups"><div class="atlas-bench-group" data-bench-section="benchmarks">` +
        `<span class="atlas-bench-group-label">Benchmarks</span>` +
        `<div class="atlas-bench-group-chips">${items.map(_benchmarkChipHtml).join("")}</div></div></div>`;
    }

    _restoreBenchChipsScroll(wrap, prevChipsScroll);

    wrap.querySelectorAll(".atlas-bench-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tk = btn.getAttribute("data-ticker");
        if (tk) switchTicker(tk);
      });
    });
    if (metaSig !== _benchmarkMetaSig) {
      _benchmarkMetaSig = metaSig;
      try {
        renderScanner();
      } catch { /* ignore */ }
    }
  } catch {
    /* ignore */
  }
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
    document.querySelectorAll("#atlas-order-ticket .ot-section").forEach(s => s.classList.remove("ot-section-active"));
    btn.classList.add("ot-tab-active");
    const sec = el(`ot-${tab}`);
    if (sec) sec.classList.add("ot-section-active");
  });
});

// ── Stock order ticket ──────────────────────────────────────────────────────

let _otStockSide = "buy";

function _otStockSideUpdate() {
  const isBuy = _otStockSide === "buy";
  el("ot-s-side-buy").classList.toggle("ot-side-active", isBuy);
  el("ot-s-side-sell").classList.toggle("ot-side-active", !isBuy);
  el("ot-s-side-buy").classList.toggle("ot-btn-buy", isBuy);
  el("ot-s-side-sell").classList.toggle("ot-btn-sell", !isBuy);
  const btn = el("ot-s-submit");
  btn.textContent = isBuy ? "PLACE BUY ORDER" : "PLACE SELL ORDER";
  btn.className   = `ot-submit ${isBuy ? "ot-submit-buy" : "ot-submit-sell"}`;
}

el("ot-s-side-buy").addEventListener("click",  () => { _otStockSide = "buy";  _otStockSideUpdate(); });
el("ot-s-side-sell").addEventListener("click", () => { _otStockSide = "sell"; _otStockSideUpdate(); });

el("ot-s-type").addEventListener("change", e => {
  el("ot-s-lmt-row").style.display =
    e.target.value === "limit" ? "" : "none";
});

el("ot-s-submit").addEventListener("click", async () => {
  const ticker  = el("ot-s-ticker").value.trim().toUpperCase();
  const qty     = parseFloat(el("ot-s-qty").value);
  const oType   = el("ot-s-type").value;
  const lmt     = oType === "limit" ? parseFloat(el("ot-s-lmt").value) : null;
  const tif     = el("ot-s-tif").value;
  const statusEl = el("ot-s-status");

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

  const btn = el("ot-s-submit");
  btn.disabled    = true;
  statusEl.textContent = "Submitting…";
  statusEl.className   = "ot-status";

  try {
    const body = { ticker, side: _otStockSide, qty, order_type: oType, tif };
    if (lmt) body.limit_price = lmt;
    const r = await fetchWithTimeout(`${BACKEND}/order/stock`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    }, 15000);
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
  el("ot-o-side-buy").classList.toggle("ot-side-active", isBuy);
  el("ot-o-side-sell").classList.toggle("ot-side-active", !isBuy);
  el("ot-o-side-buy").classList.toggle("ot-btn-buy", isBuy);
  el("ot-o-side-sell").classList.toggle("ot-btn-sell", !isBuy);
  const btn = el("ot-o-submit");
  btn.textContent = isBuy ? "PLACE BUY ORDER" : "PLACE SELL ORDER";
  btn.className   = `ot-submit ${isBuy ? "ot-submit-buy" : "ot-submit-sell"}`;
}

el("ot-o-side-buy").addEventListener("click",  () => { _otOptSide = "buy";  _otOptSideUpdate(); });
el("ot-o-side-sell").addEventListener("click", () => { _otOptSide = "sell"; _otOptSideUpdate(); });

// Preference: restrict what option rights the agents propose (CALL/PUT/BOTH).
el("ot-rights")?.addEventListener("change", async (e) => {
  const rights = String(e.target.value || "BOTH").toUpperCase();
  try {
    await fetchWithTimeout(`${BACKEND}/set_option_rights`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rights }),
    }, 4000);
    try { pollState(); } catch { /* ignore */ }
  } catch { /* ignore */ }
});

el("ot-o-type").addEventListener("change", e => {
  el("ot-o-lmt-row").style.display =
    e.target.value === "limit" ? "" : "none";
});

/** Called when the user clicks an options chain row. Pre-fills the option ticket. */
export function prefillOptionTicket(contract) {
  // Switch to option tab
  document.querySelectorAll(".ot-tab").forEach(b => b.classList.remove("ot-tab-active"));
  document.querySelectorAll("#atlas-order-ticket .ot-section").forEach(s => s.classList.remove("ot-section-active"));
  const optTab = document.querySelector('.ot-tab[data-otab="option"]');
  if (optTab) optTab.classList.add("ot-tab-active");
  const optSec = el("ot-option");
  if (optSec) optSec.classList.add("ot-section-active");

  el("ot-o-symbol").value = contract.symbol || "";
  const mid = (contract.bid != null && contract.ask != null)
    ? ((contract.bid + contract.ask) / 2).toFixed(2)
    : "";
  if (mid) el("ot-o-lmt").value = mid;

  const preEl = el("ot-opt-preview");
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

  el("ot-s-status") && (el("ot-o-status").textContent = "");
  // Scroll order ticket into view
  el("order-ticket")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

el("ot-o-submit").addEventListener("click", async () => {
  const symbol  = el("ot-o-symbol").value.trim();
  const qty     = parseInt(el("ot-o-qty").value, 10);
  const oType   = el("ot-o-type").value;
  const lmt     = oType === "limit" ? parseFloat(el("ot-o-lmt").value) : null;
  const tif     = el("ot-o-tif").value;
  const statusEl = el("ot-o-status");

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

  const btn = el("ot-o-submit");
  btn.disabled    = true;
  statusEl.textContent = "Submitting…";
  statusEl.className   = "ot-status";

  try {
    const body = { symbol, side: _otOptSide, qty, order_type: oType, tif };
    if (lmt) body.limit_price = lmt;
    const r = await fetchWithTimeout(`${BACKEND}/order/option`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    }, 15000);
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
  const tickerEl = el("ot-s-ticker");
  if (tickerEl && tickerEl.value !== t) tickerEl.value = t;
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
    const r = await fetchWithTimeout(`${BACKEND}/llm/status`, {}, 4000);
    if (!r.ok) return;
    const d = await r.json();
    _renderLlmBadge(d);
  } catch { /* silent */ }
}

function _renderLlmBadge(d) {
  const badge = el("llm-backend-badge");
  if (!badge) return;
  const cloudOn = Boolean(d.openrouter_enabled);
  const isLocal = d.primary === "local" && d.local_healthy;
  const isCooldown = d.primary === "local" && !d.local_healthy && d.cooldown_remaining_s > 0;
  const lastUsed = d.last_backend_used || "unknown";

  if (d.primary === "local") {
    if (isLocal) {
      badge.textContent = cloudOn ? "LLM: LOCAL" : "LLM: LOCAL ONLY";
      badge.className = "topbar-badge llm-badge-local";
      badge.title = cloudOn
        ? `llama.cpp is online at ${d.local_base_url}. Last used: ${lastUsed}`
        : `Local-only mode (OpenRouter off). ${d.local_base_url} — last used: ${lastUsed}`;
    } else if (!cloudOn) {
      const mins = Math.ceil((d.cooldown_remaining_s || 0) / 60);
      badge.textContent = mins > 0 ? `LLM: DOWN (${mins}m)` : "LLM: DOWN";
      badge.className = "topbar-badge llm-badge-fallback";
      badge.title = `llama.cpp unreachable; no cloud fallback (OPENROUTER_ENABLED=false). ${d.local_base_url}`;
    } else if (isCooldown) {
      const mins = Math.ceil(d.cooldown_remaining_s / 60);
      badge.textContent = `LLM: CLOUD (local down ${mins}m)`;
      badge.className = "topbar-badge llm-badge-fallback";
      badge.title = `llama.cpp unreachable. Using OpenRouter. Retry in ${d.cooldown_remaining_s}s.`;
    } else {
      badge.textContent = "LLM: CLOUD";
      badge.className = "topbar-badge llm-badge-cloud";
      badge.title = `Primary=local but local is offline. Using OpenRouter.`;
    }
  } else {
    badge.textContent = "LLM: CLOUD";
    badge.className = "topbar-badge llm-badge-cloud";
    badge.title = `OpenRouter is primary (LLAMA_LOCAL_PRIMARY=false). Last used: ${lastUsed}`;
  }
}

// ── Market clock ──────────────────────────────────────────────────────────────
let _marketClock = { is_open: null, next_open: null, next_close: null };

async function pollMarketClock() {
  try {
    const r = await fetchWithTimeout(`${BACKEND}/market/clock`, {}, 6000);
    if (!r.ok) return;
    const data = await r.json();
    _marketClock = data;
    _renderMarketClock(data);
  } catch { /* silent */ }
}

function _renderMarketClock(data) {
  const badge = el("market-clock-badge");
  if (!badge) return;
  if (data.is_open === null || data.is_open === undefined) {
    badge.textContent = "MKT: --";
    badge.className = "topbar-badge market-closed";
    return;
  }
  if (data.is_open) {
    // Market is open — show close time
    const closeStr = data.next_close
      ? new Date(data.next_close).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York" })
      : "";
    badge.textContent = closeStr ? `MKT OPEN · closes ${closeStr}` : "MKT: OPEN";
    badge.className = "topbar-badge market-open";
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
    badge.className = "topbar-badge market-closed";
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

  const stockBody = el("stock-positions-body");
  const optBody   = el("positions-body");
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
  const tbody = el("orders-body");
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
          method: "DELETE",
        });
        if (r.ok) pollOrders();
        else btn.textContent = "err";
      } catch { btn.disabled = false; }
    });
  });
}

async function pollOrders() {
  try {
    const r = await fetchWithTimeout(`${BACKEND}/orders?limit=30`, {}, 8000);
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
el("refresh-orders-btn")?.addEventListener("click", async () => {
  const btn = el("refresh-orders-btn");
  btn?.classList.add("spinning");
  await pollOrders();
  setTimeout(() => btn?.classList.remove("spinning"), 600);
});

// Positions sync button
el("refresh-positions-btn")?.addEventListener("click", async () => {
  const btn = el("refresh-positions-btn");
  if (btn) { btn.textContent = "⟳ …"; btn.disabled = true; }
  try {
    const r = await fetchWithTimeout(`${BACKEND}/positions/refresh`, { method: "POST" }, 12000);
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
const CHART_DAILY_HISTORY_TF = new Set(["5D", "1M", "3M", "6M", "1Y", "2Y", "5Y", "MAX", "1Day"]);

function _chartPollIntervalMs(tf) {
  if (tf === "MAX" || tf === "5Y") return 120000;
  return CHART_DAILY_HISTORY_TF.has(tf) ? 90000 : 15000;
}

function scheduleChartPoll() {
  if (_chartPollTimer) {
    clearInterval(_chartPollTimer);
    _chartPollTimer = null;
  }
  const tfSel = el("ticker-chart-tf");
  const tick = () => {
    if (document.visibilityState !== "visible") return;
    const { tf, limit } = _resolveBackendTf();
    if (tfSel) tfSel.value = tf;
    const intraday = !CHART_DAILY_HISTORY_TF.has(tf);
    loadTickerBars(activeTicker, tf, {
      preserveRange: true,
      followRealtime: intraday,
      limit: limit ?? undefined,
    });
  };
  const ms = _chartPollIntervalMs(_resolveBackendTf().tf);
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
  _wireAgentLiveToggle();
  _wireAgentFlowModal();
  _wireL2Overlay();
  _wireKeyboardShortcuts();
  scheduleChartPoll();
}

requestAnimationFrame(() => {
  try {
    bootstrapCharts();
    // Second pass after layout settles — re-read real pixel dimensions
    setTimeout(() => {
      try {
        resizeTerminalCharts();
        {
          const r = _resolveBackendTf();
          if (el("ticker-chart-tf")) el("ticker-chart-tf").value = r.tf;
          loadTickerBars(activeTicker, r.tf, { bust: true, limit: r.limit ?? undefined });
        }
        loadPortfolioSeries();
      } catch (e) {
        console.error("Atlas charts settle pass failed", e);
      }
    }, 400);
  } catch (e) {
    console.error("Atlas bootstrapCharts failed", e);
    showToast(`UI boot error: ${e?.message || e}`, "err", 8000);
  }
});

// Initial data load
try { _wireAgentLogModal(); } catch (e) { console.error("_wireAgentLogModal", e); }
try { bootstrapAgentRoster(); } catch (e) { console.error("bootstrapAgentRoster", e); }
try { _connectWS(); } catch (e) { console.error("_connectWS", e); }
try { pollState(); } catch (e) { console.error("pollState", e); }
try { pollMarketClock(); } catch (e) { console.error("pollMarketClock", e); }
try { pollLlmStatus(); } catch (e) { console.error("pollLlmStatus", e); }
try { pollScanner(); } catch (e) { console.error("pollScanner", e); }
try { pollBenchmarkStrip(); } catch (e) { console.error("pollBenchmarkStrip", e); }
try { pollReasoningLog(); } catch (e) { console.error("pollReasoningLog", e); }
try { fetchOptionsChain(activeTicker); } catch (e) { console.error("fetchOptionsChain", e); }
try { pollAgentStatus(); } catch (e) { console.error("pollAgentStatus", e); }
try { pollOrders(); } catch (e) { console.error("pollOrders", e); }
try { _syncStockTicker(activeTicker); } catch (e) { console.error("_syncStockTicker", e); }

// Auto-sync positions from broker shortly after page load
// (gives the server time to complete its initial Alpaca sync)
setTimeout(async () => {
  try {
    const r = await fetchWithTimeout(`${BACKEND}/positions/refresh`, { method: "POST" }, 12000);
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

// ── News feed polling ────────────────────────────────────────────────────────
async function pollNewsFeed() {
  try {
    const r = await fetchWithTimeout(`${BACKEND}/news?limit=80`, {}, 4000);
    if (!r.ok) return;
    const articles = await r.json();
    if (articles?.length) renderNews(articles, { replace: true });
    _updateNewsLastSync();
  } catch { /* silent */ }
}
pollNewsFeed();

// ── Data Dashboard (tables: queue / processed / impacts) ──────────────────────
let _dashCache = { queue: null, processed: null, impacts: null, at: 0 };
let _dashFilter = "";

function _dashEls() {
  return {
    stats: document.getElementById("dash-stats"),
    filter: document.getElementById("dash-filter"),
    refresh: document.getElementById("dash-refresh"),
    tabs: document.querySelectorAll("#dash-tabs .dash-tab"),
    panes: {
      queue: document.getElementById("dash-pane-queue"),
      processed: document.getElementById("dash-pane-processed"),
      impacts: document.getElementById("dash-pane-impacts"),
      db: document.getElementById("dash-pane-db"),
    },
    bodies: {
      queue: document.getElementById("dash-queue-body"),
      processed: document.getElementById("dash-processed-body"),
      impacts: document.getElementById("dash-impacts-body"),
    },
    db: {
      source: document.getElementById("db-source"),
      table: document.getElementById("db-table"),
      load: document.getElementById("db-load"),
      meta: document.getElementById("db-meta"),
      schemaBody: document.getElementById("db-schema-body"),
      rowsHead: document.getElementById("db-rows-head"),
      rowsBody: document.getElementById("db-rows-body"),
    },
  };
}

function _dashSetTab(id) {
  const { tabs, panes } = _dashEls();
  tabs?.forEach(b => {
    const on = b.dataset.tab === id;
    b.classList.toggle("dash-tab-active", on);
  });
  Object.entries(panes).forEach(([k, el]) => {
    if (!el) return;
    el.style.display = (k === id) ? "" : "none";
  });
}

function _dashMatch(s) {
  if (!_dashFilter) return true;
  return String(s || "").toLowerCase().includes(_dashFilter);
}

function _dashFmtPct(x, digits = 0) {
  const v = Number(x ?? 0);
  if (!Number.isFinite(v)) return "—";
  return `${Math.round(v * 100).toFixed(digits)}%`;
}

function _dashFmtNum(x, digits = 2) {
  const v = Number(x ?? 0);
  if (!Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}

function _dashRenderQueue(data) {
  const { bodies } = _dashEls();
  const tb = bodies.queue;
  if (!tb) return;
  const items = (data?.items || []).map(x => ({
    priority: x.priority ?? 0,
    seen: (x.seen || []).join(","),
    item: x.item || {},
  }));
  const rows = items
    .filter(x => _dashMatch(x.item?.headline) || _dashMatch(x.item?.source) || _dashMatch(x.seen))
    .slice(0, 120);
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="8" class="empty-row">No matching queue rows</td></tr>`;
    return;
  }
  tb.innerHTML = "";
  for (const r of rows) {
    const it = r.item || {};
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${_dashFmtNum(r.priority, 3)}</td>
      <td>${esc(String(it.urgency_tier || "T2").toUpperCase())}</td>
      <td>${_dashFmtNum(it.impact_score ?? 0, 2)}</td>
      <td>${_dashFmtNum(it.sentiment ?? 0, 2)}</td>
      <td>${_dashFmtPct(it.confidence ?? 0, 0)}</td>
      <td>${esc(_newsRelTime(it.published_at || it.added_at || ""))}</td>
      <td class="mono" title="${esc(it.headline || "")}">${esc((it.headline || "").slice(0, 120))}</td>
      <td class="mono">${esc(r.seen || "")}</td>
    `;
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => {
      // Reuse the existing news modal if payload matches NewsItem shape.
      if (it?.headline) _openNewsModal(it);
    });
    tb.appendChild(tr);
  }
}

function _dashRenderProcessed(items) {
  const { bodies } = _dashEls();
  const tb = bodies.processed;
  if (!tb) return;
  const rows = (items || [])
    .filter(x => _dashMatch(x.headline) || _dashMatch((x.original_tickers || []).join(",")) || _dashMatch(x.category))
    .slice(0, 120);
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="7" class="empty-row">No matching processed rows</td></tr>`;
    return;
  }
  tb.innerHTML = "";
  for (const a of rows) {
    const tr = document.createElement("tr");
    const t = a.published_at ? new Date(a.published_at).toLocaleString("en-US", { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit", hour12:false }) : "—";
    tr.innerHTML = `
      <td class="mono">${esc(t)}</td>
      <td class="mono">${esc((a.original_tickers || []).slice(0, 4).join(","))}</td>
      <td>${esc(String(a.category || "general"))}</td>
      <td>${esc(String(a.impact_magnitude ?? 1))}</td>
      <td>${_dashFmtNum(a.sentiment ?? 0, 2)}</td>
      <td>${_dashFmtPct(a.confidence ?? 0, 0)}</td>
      <td title="${esc(a.headline || "")}">${esc((a.headline || "").slice(0, 140))}</td>
    `;
    tb.appendChild(tr);
  }
}

function _dashRenderImpacts(map) {
  const { bodies } = _dashEls();
  const tb = bodies.impacts;
  if (!tb) return;
  const rows = Object.values(map || {})
    .filter(x => _dashMatch(x.ticker) || _dashMatch((x.relationships || []).join(",")))
    .sort((a, b) => Math.abs(Number(b.total_impact ?? 0)) - Math.abs(Number(a.total_impact ?? 0)))
    .slice(0, 160);
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="4" class="empty-row">No matching impact rows</td></tr>`;
    return;
  }
  tb.innerHTML = "";
  for (const x of rows) {
    const tr = document.createElement("tr");
    const v = Number(x.total_impact ?? 0);
    const cls = v > 0 ? "sentiment-pos" : v < 0 ? "sentiment-neg" : "sentiment-neu";
    tr.innerHTML = `
      <td class="mono">${esc(String(x.ticker || ""))}</td>
      <td class="${cls} mono">${esc((v >= 0 ? "+" : "") + _dashFmtNum(v, 2))}</td>
      <td class="mono">${esc(String(x.article_count ?? 0))}</td>
      <td title="${esc((x.relationships || []).join(", "))}">${esc((x.relationships || []).slice(0, 2).join(", "))}</td>
    `;
    tb.appendChild(tr);
  }
}

async function pollDashboard({ force = false } = {}) {
  // Only update if dashboard exists (keeps older UI versions compatible)
  const { stats } = _dashEls();
  if (!stats) return;

  const now = Date.now();
  if (!force && (now - (_dashCache.at || 0)) < 8000) return; // 8s cache

  try {
    const [rq, rp, ri] = await Promise.all([
      fetchWithTimeout(`${BACKEND}/news/queue?limit=80`, {}, 4500).then(r => r.ok ? r.json() : null).catch(() => null),
      fetchWithTimeout(`${BACKEND}/news/processed?limit=80`, {}, 4500).then(r => r.ok ? r.json() : null).catch(() => null),
      fetchWithTimeout(`${BACKEND}/news/impacts`, {}, 4500).then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    _dashCache = { queue: rq, processed: rp, impacts: ri, at: now };

    const qStats = rq?.stats || {};
    const total = qStats.total ?? "—";
    const u1 = qStats.unseen_news_analyst ?? "—";
    const u2 = qStats.unseen_sentiment_analyst ?? "—";
    stats.textContent = `Q:${total}  unseen NA:${u1}  SA:${u2}`;

    if (rq) _dashRenderQueue(rq);
    if (rp) _dashRenderProcessed(rp);
    if (ri) _dashRenderImpacts(ri);
  } catch { /* silent */ }
}

// ── DB Explorer (SQLite + Postgres) ───────────────────────────────────────────
let _dbCache = { sources: null, tables: new Map(), schema: new Map(), rows: new Map() };

async function _dbLoadSources() {
  const { db } = _dashEls();
  if (!db?.source) return;
  try {
    const r = await fetchWithTimeout(`${BACKEND}/db/sources`, {}, 5000);
    if (!r.ok) return;
    const data = await r.json();
    const sources = data?.sources || [];
    _dbCache.sources = sources;
    db.source.innerHTML = sources.map(s => {
      const extra = s.kind === "sqlite"
        ? (s.exists === false ? " (missing)" : "")
        : "";
      return `<option value="${esc(s.key)}">${esc(s.label)}${extra}</option>`;
    }).join("");
  } catch {}
}

async function _dbLoadTables(sourceKey) {
  const { db } = _dashEls();
  if (!db?.table || !sourceKey) return;
  try {
    if (_dbCache.tables.has(sourceKey)) {
      const t = _dbCache.tables.get(sourceKey) || [];
      db.table.innerHTML = t.map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
      return;
    }
    const r = await fetchWithTimeout(`${BACKEND}/db/${encodeURIComponent(sourceKey)}/tables`, {}, 6000);
    if (!r.ok) return;
    const data = await r.json();
    const tables = data?.tables || [];
    _dbCache.tables.set(sourceKey, tables);
    db.table.innerHTML = tables.map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
  } catch {}
}

function _dbRenderSchema(schema) {
  const { db } = _dashEls();
  const body = db?.schemaBody;
  if (!body) return;
  const cols = schema?.columns || [];
  const fks = schema?.foreign_keys || [];
  const fkMap = new Map();
  for (const fk of fks) {
    const from = fk.from || fk["from"];
    const toTable = fk.to_table || fk.table || fk.to_table_name || fk.to_table;
    const toCol = fk.to_col || fk.to || fk.foreign_column_name || fk.to_col;
    if (from) fkMap.set(from, `${toTable || ""}.${toCol || ""}`);
  }
  if (!cols.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty-row">No columns returned</td></tr>`;
    return;
  }
  body.innerHTML = "";
  for (const c of cols) {
    const tr = document.createElement("tr");
    const pk = c.pk ? "✓" : "";
    const nn = c.notnull ? "✓" : "";
    tr.innerHTML = `
      <td class="mono">${esc(c.name || "")}</td>
      <td class="mono">${esc(c.type || "")}</td>
      <td>${pk}</td>
      <td>${nn}</td>
      <td class="mono">${esc(String(c.dflt_value ?? ""))}</td>
      <td class="mono">${esc(fkMap.get(c.name) || "")}</td>
    `;
    body.appendChild(tr);
  }
}

function _dbRenderRows(rowsResp) {
  const { db } = _dashEls();
  const head = db?.rowsHead;
  const body = db?.rowsBody;
  if (!head || !body) return;

  const rows = rowsResp?.rows || [];
  const count = rowsResp?.count;
  if (!rows.length) {
    head.innerHTML = "";
    body.innerHTML = `<tr><td colspan="6" class="empty-row">No rows returned</td></tr>`;
    return;
  }
  const cols = Object.keys(rows[0]).slice(0, 12); // cap columns for readability
  head.innerHTML = `<tr>${cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr>`;
  body.innerHTML = "";
  for (const r of rows.slice(0, 120)) {
    const tr = document.createElement("tr");
    tr.innerHTML = cols.map(c => {
      let v = r[c];
      if (v == null) v = "";
      const s = typeof v === "string" ? v : JSON.stringify(v);
      return `<td class="mono" title="${esc(s)}">${esc(s.length > 120 ? s.slice(0, 117) + "…" : s)}</td>`;
    }).join("");
    body.appendChild(tr);
  }
  if (db?.meta) db.meta.textContent = `Rows: ${count ?? "—"} (showing ${rows.length})`;
}

async function _dbLoadSelected() {
  const { db } = _dashEls();
  const sourceKey = db?.source?.value;
  const table = db?.table?.value;
  if (!sourceKey || !table) return;
  try {
    const schemaKey = `${sourceKey}:${table}:schema`;
    const rowsKey = `${sourceKey}:${table}:rows`;
    const [schema, rows] = await Promise.all([
      fetchWithTimeout(`${BACKEND}/db/${encodeURIComponent(sourceKey)}/table/${encodeURIComponent(table)}/schema`, {}, 7000).then(r => r.ok ? r.json() : null),
      fetchWithTimeout(`${BACKEND}/db/${encodeURIComponent(sourceKey)}/table/${encodeURIComponent(table)}/rows?limit=60&offset=0`, {}, 7000).then(r => r.ok ? r.json() : null),
    ]);
    if (schema) _dbCache.schema.set(schemaKey, schema);
    if (rows) _dbCache.rows.set(rowsKey, rows);
    if (schema) _dbRenderSchema(schema);
    if (rows) _dbRenderRows(rows);
  } catch {}
}

function _wireDashboard() {
  const { tabs, filter, refresh } = _dashEls();
  tabs?.forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      _dashSetTab(btn.dataset.tab);
      // render from cache immediately
      if (_dashCache.queue) _dashRenderQueue(_dashCache.queue);
      if (_dashCache.processed) _dashRenderProcessed(_dashCache.processed);
      if (_dashCache.impacts) _dashRenderImpacts(_dashCache.impacts);
    });
  });
  filter?.addEventListener("input", () => {
    _dashFilter = String(filter.value || "").trim().toLowerCase();
    if (_dashCache.queue) _dashRenderQueue(_dashCache.queue);
    if (_dashCache.processed) _dashRenderProcessed(_dashCache.processed);
    if (_dashCache.impacts) _dashRenderImpacts(_dashCache.impacts);
  });
  refresh?.addEventListener("click", (e) => {
    e.preventDefault();
    pollDashboard({ force: true });
  });
  // default tab
  _dashSetTab("queue");
}

try { _wireDashboard(); } catch (e) { console.error("_wireDashboard", e); }
try { pollDashboard({ force: true }); } catch (e) { console.error("pollDashboard", e); }

// DB Explorer wiring (safe if elements missing)
try {
  const { db } = _dashEls();
  if (db?.source && db?.table && db?.load) {
    _dbLoadSources().then(() => _dbLoadTables(db.source.value));
    db.source.addEventListener("change", () => _dbLoadTables(db.source.value));
    db.load.addEventListener("click", (e) => { e.preventDefault(); _dbLoadSelected(); });
  }
} catch (e) { console.error("db explorer wiring", e); }

// Polling intervals (pollState skips when WS is live)
setInterval(pollState,           2000);   // metrics fallback
setInterval(pollMarketClock,    60000);   // NYSE clock (every minute is enough)
setInterval(pollLlmStatus,      30000);   // LLM backend badge (updates after cooldown expires)
setInterval(pollScanner,        10000);   // full scanner (options metrics + quotes)
setInterval(pollScannerQuotes,   1000);   // live last / day % only
setInterval(pollBenchmarkStrip,  2000);   // index / sector strip under tier bar
setInterval(pollReasoningLog,    4000);   // XAI log
setInterval(loadPortfolioSeries, 15000);  // portfolio / greeks chart
// Quote is now pushed over /ws as well; keep REST poll as fallback but slower.
setInterval(pollQuote,            4000);  // fallback quote poll
setInterval(pollOrders,          15000);  // order blotter
setInterval(pollNewsFeed,       30000);   // news headlines — full replace from GET /news
// Recompute “Xm ago” without waiting for the next HTTP poll (tape-only).
setInterval(() => _renderNewsTapeFromBuffer(), 15000);
setInterval(() => { try { pollDashboard(); } catch {} }, 15000);
