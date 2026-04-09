# Trading Viz — real-time charting prototype

Production-style **separation of concerns** for an algorithmic trading UI:

| Layer | Implementation |
|--------|------------------|
| **UI** | React 18 + TypeScript |
| **State** | Zustand (`Map` for L2, arrays for L3, OHLC map + forming candle) |
| **Transport** | WebSocket JSON envelopes `{ channel, payload }` |
| **Throttling** | `requestAnimationFrame` batch helper (`src/data/throttle.ts`) |
| **Chart engine** | [lightweight-charts](https://github.com/tradingview/lightweight-charts) (canvas-based) + SMA overlay |
| **Depth** | HTML5 Canvas cumulative bid/ask curves |
| **Indicators** | Web Worker: SMA, EMA, RSI (`src/workers/indicators.worker.ts`) |
| **Downsampling** | LTTB (`src/data/downsample.ts`) for 100k+ points |

Data flow (unidirectional):

```
WebSocket → normalize.ts → useMarketStore → ChartEngine / OrderBook / Tape / DepthChart
```

## Run

```bash
cd trading-viz
npm install
```

**Terminal A — mock market server**

```bash
npm run mock:ws
# ws://localhost:8765
```

**Terminal B — Vite dev**

```bash
npm run dev
# http://localhost:5174
```

Optional env:

```bash
VITE_WS_URL=ws://127.0.0.1:8765 npm run dev
```

## Performance check

```bash
npm run bench
```

Worker micro-benchmark: use the **Run worker bench** button in the UI (SMA on 200k points).

## Integration with the Python terminal

This package is **standalone**. To embed later:

- Point `VITE_WS_URL` at a FastAPI WebSocket that emits the same JSON shapes (`tick`, `book`, `candle`, `reset`), or
- Add a thin adapter in `useWebSocketMarket.ts` to map your backend’s schema.

## Scope vs full institutional stack

Delivered here: candle chart + SMA, L2 panel, L3 tape, depth canvas, worker indicators, mock WS, LTTB util, rAF throttle.

Not included (would be separate milestones): WebGL heatmaps, full MACD panel, footprint charts, Level-3 exchange-native feeds, Redux instead of Zustand, full replay backtester.
