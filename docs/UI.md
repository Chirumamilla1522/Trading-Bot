## UI (Tauri terminal)

UI sources live in `ui/src/`.

### Key files

- `ui/src/index.html`: base layout / panels
- `ui/src/main.js`: UI logic (WebSocket connection, polling fallback, rendering)
- `ui/src/charts.js`: chart helpers
- `ui/src/terminal.css`: styling/theme

### Data sources

- **WebSocket** `/ws/market`: market hub (quotes/trades/orderbook panel)
- **REST polling** (core app state):
  - `/state`
  - `/reasoning_log`
  - `/agent_status`
  - plus quote/scanner/orders endpoints

### Common UI behaviors

- Reasoning panel deduplicates entries by `(timestamp|agent|action)`
- News tape highlights HIGH priority and shows cached sentiment markers
- Topbar shows backend health badges (LLM backend, WS vs polling, etc.)

### Recommendations UI: proposed legs vs quotes

The Recommendations “Proposed legs” table shows **two** things at once:

- **Proposal legs** (always present if recommendation has a proposal): side/symbol/qty.
- **Pricing legs** (may be missing): bid/ask/mid derived from `FirmState.latest_greeks`.

Why bid/ask/mid can be `—`:
- Missing chain quote in `latest_greeks` for that OCC symbol
- Contract is **expired** (no broker NBBO published)
- Data subscription limitations / stale caches

The backend now includes `expired` + `occ_expiry` for legs and a `quote_note` warning when this happens.

