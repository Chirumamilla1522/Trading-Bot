import type { OHLC, OrderBookDelta, OrderBookSnapshot, TickTrade } from "../types/market";

/** Coerce any reasonable timestamp to unix milliseconds */
export function toMs(t: number): number {
  if (!Number.isFinite(t)) return Date.now();
  if (t < 1e12) return Math.round(t * 1000);
  return Math.round(t);
}

export function normalizeTick(raw: Record<string, unknown>): TickTrade | null {
  const ts = raw.timestamp ?? raw.t ?? raw.time;
  const price = raw.price ?? raw.p;
  const size = raw.size ?? raw.s ?? raw.qty;
  const side = raw.side === "sell" || raw.side === "S" ? "sell" : "buy";
  const tradeId = String(raw.tradeId ?? raw.id ?? `${ts}-${price}`);
  if (typeof price !== "number" || typeof size !== "number") return null;
  return {
    timestamp: toMs(Number(ts)),
    price,
    size,
    side: side as TickTrade["side"],
    tradeId,
  };
}

export function mergeBookSnapshot(snap: OrderBookSnapshot): {
  bids: Map<number, number>;
  asks: Map<number, number>;
} {
  const bids = new Map<number, number>();
  const asks = new Map<number, number>();
  for (const [p, sz] of snap.bids) {
    if (sz <= 0) bids.delete(p);
    else bids.set(p, sz);
  }
  for (const [p, sz] of snap.asks) {
    if (sz <= 0) asks.delete(p);
    else asks.set(p, sz);
  }
  return { bids, asks };
}

export function applyBookDelta(
  bids: Map<number, number>,
  asks: Map<number, number>,
  delta: OrderBookDelta
): void {
  for (const [p, sz] of delta.bids) {
    if (sz <= 0) bids.delete(p);
    else bids.set(p, sz);
  }
  for (const [p, sz] of delta.asks) {
    if (sz <= 0) asks.delete(p);
    else asks.set(p, sz);
  }
}

/** Insert OHLC; if same `time`, replace (idempotent resync) */
export function upsertCandle(map: Map<number, OHLC>, c: OHLC): void {
  map.set(c.time, { ...c });
}

/** Sort out-of-order ticks into buffer by timestamp (stable) */
export function insertSortedByTime<T extends { timestamp: number }>(arr: T[], item: T, maxLen: number): void {
  let i = 0;
  while (i < arr.length && arr[i].timestamp <= item.timestamp) i++;
  arr.splice(i, 0, item);
  while (arr.length > maxLen) arr.shift();
}
