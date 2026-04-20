// WebWorker for options chain filtering/sorting.
// Keeps main thread smooth when chain updates frequently.

function sortRows(rows, sortKey) {
  const sorters = {
    iv_desc:     (a, b) => (b.iv ?? 0) - (a.iv ?? 0),
    iv_asc:      (a, b) => (a.iv ?? 0) - (b.iv ?? 0),
    strike_asc:  (a, b) => (a.strike ?? 0) - (b.strike ?? 0),
    strike_desc: (a, b) => (b.strike ?? 0) - (a.strike ?? 0),
    exp_asc:     (a, b) => String(a.expiry ?? "").localeCompare(String(b.expiry ?? "")),
    delta_desc:  (a, b) => Math.abs(b.delta ?? 0) - Math.abs(a.delta ?? 0),
    bid_desc:    (a, b) => (b.bid ?? 0) - (a.bid ?? 0),
  };
  rows.sort(sorters[sortKey] || sorters.iv_desc);
}

function applyView(raw, filter, strike) {
  let rows = Array.isArray(raw) ? raw.slice() : [];

  if (filter === "calls") rows = rows.filter(g => g.right === "CALL");
  if (filter === "puts")  rows = rows.filter(g => g.right === "PUT");

  if (strike != null && !isNaN(strike)) {
    const range = Math.max(strike * 0.05, 10);
    rows = rows.filter(g => Math.abs((g.strike ?? 0) - strike) <= range);
  }
  return rows;
}

self.onmessage = (ev) => {
  const msg = ev.data || {};
  if (msg.type !== "apply") return;
  try {
    const raw = msg.raw || [];
    const filter = msg.filter || "all";
    const sort = msg.sort || "iv_desc";
    const strike = msg.strike;
    let rows = applyView(raw, filter, strike);
    sortRows(rows, sort);
    const total = rows.length;
    rows = rows.slice(0, msg.cap || 300);
    self.postMessage({ type: "result", rows, total, rawTotal: Array.isArray(raw) ? raw.length : 0 });
  } catch (e) {
    self.postMessage({ type: "error", message: String(e?.message || e) });
  }
};

