import { useState } from "react";
import { ChartEngine } from "./components/ChartEngine";
import { DepthChart } from "./components/DepthChart";
import { OrderBookPanel } from "./components/OrderBookPanel";
import { TradeTape } from "./components/TradeTape";
import { useIndicatorWorker } from "./hooks/useIndicatorWorker";
import { useWebSocketMarket } from "./hooks/useWebSocketMarket";

export default function App() {
  const [wsOn, setWsOn] = useState(true);
  useWebSocketMarket(wsOn);
  const { run } = useIndicatorWorker();
  const [bench, setBench] = useState<string>("");

  const runBench = async () => {
    const n = 200_000;
    const vals = Array.from({ length: n }, (_, i) => 100 + Math.sin(i / 50) * 2 + (i % 7) * 0.01);
    const t0 = performance.now();
    const out = await run("sma", vals, 20);
    const ms = performance.now() - t0;
    setBench(`SMA(${n} pts, p=20) in worker: ${ms.toFixed(1)}ms · last=${out[out.length - 1]?.toFixed(4)}`);
  };

  return (
    <div className="app">
      <header className="top">
        <h1>Trading Viz</h1>
        <p className="sub">
          WebSocket → normalizer → Zustand → canvas chart + L2 DOM + L3 tape. Indicator math in Web Worker.
        </p>
        <div className="actions">
          <label>
            <input type="checkbox" checked={wsOn} onChange={(e) => setWsOn(e.target.checked)} /> Live WS
          </label>
          <button type="button" onClick={runBench}>
            Run worker bench
          </button>
          {bench && <span className="bench">{bench}</span>}
        </div>
      </header>

      <main className="grid">
        <section className="cell chart-cell">
          <ChartEngine />
        </section>
        <aside className="cell side">
          <OrderBookPanel />
          <div className="depth-wrap">
            <div className="panel-h">Depth curve</div>
            <DepthChart />
          </div>
        </aside>
        <section className="cell tape-cell">
          <TradeTape />
        </section>
      </main>

      <footer className="foot">
        <code>VITE_WS_URL</code> defaults to <code>ws://localhost:8765</code>. Run{" "}
        <code>npm run mock:ws</code> in another terminal.
      </footer>
    </div>
  );
}
