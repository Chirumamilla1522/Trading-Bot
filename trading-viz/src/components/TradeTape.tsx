import { useMemo } from "react";
import { useMarketStore } from "../store/useMarketStore";

const ET_TZ = "America/New_York";

export function TradeTape() {
  const trades = useMarketStore((s) => s.trades);

  const stats = useMemo(() => {
    if (!trades.length) return { buyV: 0, sellV: 0, rate: 0 };
    const win = trades.slice(-200);
    let buyV = 0;
    let sellV = 0;
    for (const t of win) {
      if (t.side === "buy") buyV += t.size;
      else sellV += t.size;
    }
    const t0 = win[0].timestamp;
    const t1 = win[win.length - 1].timestamp;
    const dt = Math.max(1, (t1 - t0) / 1000);
    const rate = win.length / dt;
    return { buyV, sellV, rate };
  }, [trades]);

  const rows = trades.slice(-40);

  return (
    <div className="panel tape">
      <div className="panel-h">Time &amp; sales (L3)</div>
      <div className="tape-stats">
        <span>Buy vol {stats.buyV.toFixed(0)}</span>
        <span>Sell vol {stats.sellV.toFixed(0)}</span>
        <span title="trades/sec (recent window)">~{stats.rate.toFixed(1)} t/s</span>
      </div>
      <div className="tape-head">
        <span>Time</span>
        <span>Price</span>
        <span>Size</span>
        <span>S</span>
      </div>
      <div className="tape-body">
        {rows.map((t) => (
          <div key={t.tradeId} className={`tape-row ${t.side}`}>
            <span>{new Date(t.timestamp).toLocaleTimeString("en-US", { timeZone: ET_TZ })}</span>
            <span>{t.price.toFixed(2)}</span>
            <span>{t.size.toFixed(0)}</span>
            <span>{t.side === "buy" ? "B" : "S"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
