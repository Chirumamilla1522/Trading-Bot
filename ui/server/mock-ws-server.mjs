/**
 * Mock WebSocket market feed for development.
 * Streams: L3 ticks, L2 snapshots + deltas, optional candle updates.
 *
 * Run: npm run mock:ws   (default ws://0.0.0.0:8765)
 */
import { WebSocketServer } from "ws";

const PORT = Number(process.env.WS_PORT || 8765);
const BASE = 150 + Math.random() * 20;
let last = BASE;

function jitter(x, s = 0.02) {
  return x * (1 + (Math.random() - 0.5) * s);
}

function buildSnapshot(price) {
  const bids = [];
  const asks = [];
  for (let i = 0; i < 10; i++) {
    bids.push([jitter(price - 0.05 * (i + 1), 0.005), 100 + Math.random() * 400]);
    asks.push([jitter(price + 0.05 * (i + 1), 0.005), 100 + Math.random() * 400]);
  }
  return { type: "snapshot", bids, asks };
}

const wss = new WebSocketServer({ port: PORT });
console.log(`[mock-ws] listening on ws://localhost:${PORT}`);

wss.on("connection", (ws) => {
  last = BASE;
  ws.send(JSON.stringify({ channel: "reset" }));
  ws.send(JSON.stringify({ channel: "book", payload: buildSnapshot(last) }));

  const tickIv = setInterval(() => {
    last = jitter(last, 0.06);
    const side = Math.random() > 0.48 ? "buy" : "sell";
    const tick = {
      timestamp: Date.now(),
      price: last,
      size: Math.max(1, Math.round(10 + Math.random() * 200)),
      side,
      tradeId: `t-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    };
    ws.send(JSON.stringify({ channel: "tick", payload: tick }));
  }, 35);

  const bookIv = setInterval(() => {
    const d = 0.02 + Math.random() * 0.04;
    const payload = {
      type: "delta",
      bids: [[jitter(last - d), 50 + Math.random() * 300]],
      asks: [[jitter(last + d), 50 + Math.random() * 300]],
    };
    ws.send(JSON.stringify({ channel: "book", payload }));
  }, 450);

  ws.on("close", () => {
    clearInterval(tickIv);
    clearInterval(bookIv);
  });
});
