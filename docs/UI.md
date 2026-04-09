## UI (Tauri terminal)

UI sources live in `ui/src/`.

### Key files

- `ui/src/index.html`: base layout / panels
- `ui/src/main.js`: UI logic (WebSocket connection, polling fallback, rendering)
- `ui/src/charts.js`: chart helpers
- `ui/src/terminal.css`: styling/theme

### Data sources

- **WebSocket** `/ws`: preferred realtime updates
- **REST polling fallback**:
  - `/state`
  - `/reasoning_log`
  - `/agent_status`
  - plus quote/scanner/orders endpoints

### Common UI behaviors

- Reasoning panel deduplicates entries by `(timestamp|agent|action)`
- News tape highlights HIGH priority and shows cached sentiment markers
- Topbar shows backend health badges (LLM backend, WS vs polling, etc.)

