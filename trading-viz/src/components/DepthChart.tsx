import { useEffect, useRef } from "react";
import { useMarketStore } from "../store/useMarketStore";

/** Canvas: cumulative bid/ask size vs price (depth curve). */
export function DepthChart() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const bids = useMarketStore((s) => s.bids);
  const asks = useMarketStore((s) => s.asks);
  const rev = useMarketStore((s) => s.bookRevision);

  const draw = () => {
    const c = canvasRef.current;
    if (!c) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth;
    const h = c.clientHeight;
    c.width = w * dpr;
    c.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    ctx.fillStyle = "#0a0a0f";
    ctx.fillRect(0, 0, w, h);

    const bidP = [...bids.keys()].sort((a, b) => b - a);
    const askP = [...asks.keys()].sort((a, b) => a - b);
    if (!bidP.length && !askP.length) return;

    let cumB = 0;
    const bidCurve: [number, number][] = [];
    for (const p of bidP.slice(0, 40)) {
      cumB += bids.get(p) ?? 0;
      bidCurve.push([p, cumB]);
    }
    let cumA = 0;
    const askCurve: [number, number][] = [];
    for (const p of askP.slice(0, 40)) {
      cumA += asks.get(p) ?? 0;
      askCurve.push([p, cumA]);
    }

    const prices = [...bidCurve.map((x) => x[0]), ...askCurve.map((x) => x[0])];
    const minP = Math.min(...prices);
    const maxP = Math.max(...prices);
    const maxC = Math.max(cumB, cumA, 1);

    const x = (p: number) => ((p - minP) / (maxP - minP || 1)) * (w - 16) + 8;
    const y = (v: number) => h - 8 - (v / maxC) * (h - 16);

    ctx.strokeStyle = "rgba(0, 230, 118, 0.85)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    bidCurve.forEach(([pr, v], i) => {
      const X = x(pr);
      const Y = y(v);
      if (i === 0) ctx.moveTo(X, Y);
      else ctx.lineTo(X, Y);
    });
    ctx.stroke();

    ctx.strokeStyle = "rgba(255, 82, 82, 0.85)";
    ctx.beginPath();
    askCurve.forEach(([pr, v], i) => {
      const X = x(pr);
      const Y = y(v);
      if (i === 0) ctx.moveTo(X, Y);
      else ctx.lineTo(X, Y);
    });
    ctx.stroke();

    ctx.fillStyle = "#5a5a70";
    ctx.font = "10px system-ui";
    ctx.fillText("Bid depth", 8, 14);
    ctx.fillText("Ask depth", 8, 28);
  };

  useEffect(() => {
    draw();
    const c = canvasRef.current;
    if (!c) return;
    const ro = new ResizeObserver(() => draw());
    ro.observe(c);
    return () => ro.disconnect();
  }, [bids, asks, rev]);

  return <canvas ref={canvasRef} className="depth-canvas" />;
}
