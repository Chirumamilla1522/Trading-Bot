import { create } from "zustand";
import type { OHLC, TickTrade } from "../types/market";
import { applyBookDelta, mergeBookSnapshot, upsertCandle } from "../data/normalize";
import type { OrderBookDelta, OrderBookSnapshot } from "../types/market";

const MAX_TRADES = 500;
const MAX_CANDLES = 5000;

export interface FormingCandle {
  bucketMs: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface MarketState {
  symbol: string;
  /** L2: price → size */
  bids: Map<number, number>;
  asks: Map<number, number>;
  /** L3 ring buffer (newest at end) */
  trades: TickTrade[];
  /** Closed / aggregated candles by bucket start (ms) */
  candles: Map<number, OHLC>;
  /** Current in-progress candle for default TF */
  forming: FormingCandle | null;
  lastTickTs: number;
  bookRevision: number;

  setSymbol: (s: string) => void;
  applySnapshot: (s: OrderBookSnapshot) => void;
  applyDelta: (d: OrderBookDelta) => void;
  pushTrade: (t: TickTrade) => void;
  pushCandle: (c: OHLC) => void;
  /** Replace candle history from persisted /bars series */
  setCandlesFromBars: (bars: OHLC[]) => void;
  /** Aggregate tick into 1s buckets */
  aggregateTickToBucket: (t: TickTrade, bucketMs?: number) => void;
  reset: () => void;
}

function emptyMaps() {
  return { bids: new Map<number, number>(), asks: new Map<number, number>() };
}

export const useMarketStore = create<MarketState>((set, get) => ({
  symbol: "DEMO",
  ...emptyMaps(),
  trades: [],
  candles: new Map(),
  forming: null,
  lastTickTs: 0,
  bookRevision: 0,

  setSymbol: (symbol) => set({ symbol }),

  applySnapshot: (snap) => {
    const { bids, asks } = mergeBookSnapshot(snap);
    set({ bids, asks, bookRevision: get().bookRevision + 1 });
  },

  applyDelta: (delta) => {
    const { bids, asks } = get();
    const nb = new Map(bids);
    const na = new Map(asks);
    applyBookDelta(nb, na, delta);
    set({ bids: nb, asks: na, bookRevision: get().bookRevision + 1 });
  },

  pushTrade: (t) => {
    set((s) => {
      const next = [...s.trades, t];
      while (next.length > MAX_TRADES) next.shift();
      return { trades: next, lastTickTs: t.timestamp };
    });
  },

  pushCandle: (c) => {
    set((s) => {
      const m = new Map(s.candles);
      upsertCandle(m, c);
      while (m.size > MAX_CANDLES) {
        const first = [...m.keys()].sort((a, b) => a - b)[0];
        m.delete(first);
      }
      return { candles: m };
    });
  },

  setCandlesFromBars: (bars) => {
    const m = new Map<number, OHLC>();
    for (const b of bars || []) {
      if (!b) continue;
      const t = Math.floor(Number(b.time));
      if (!Number.isFinite(t) || t <= 0) continue;
      const o = Number(b.open), h = Number(b.high), lo = Number(b.low), c = Number(b.close);
      if (![o, h, lo, c].every(Number.isFinite)) continue;
      m.set(t * 1000, { time: t, open: o, high: h, low: lo, close: c, volume: b.volume });
    }
    const keys = [...m.keys()].sort((a, b) => a - b);
    while (keys.length > MAX_CANDLES) {
      const k = keys.shift();
      if (k != null) m.delete(k);
    }
    set({ candles: m, forming: null });
  },

  aggregateTickToBucket: (t, bucketMs = 1000) => {
    const bucket = Math.floor(t.timestamp / bucketMs) * bucketMs;
    set((s) => {
      let candles = s.candles;
      let forming = s.forming;

      if (!forming || forming.bucketMs !== bucket) {
        if (forming) {
          candles = new Map(s.candles);
          upsertCandle(candles, {
            time: Math.floor(forming.bucketMs / 1000),
            open: forming.open,
            high: forming.high,
            low: forming.low,
            close: forming.close,
            volume: forming.volume,
          });
          while (candles.size > MAX_CANDLES) {
            const first = [...candles.keys()].sort((a, b) => a - b)[0];
            candles.delete(first);
          }
        }
        forming = {
          bucketMs: bucket,
          open: t.price,
          high: t.price,
          low: t.price,
          close: t.price,
          volume: t.size,
        };
      } else {
        forming = {
          ...forming,
          high: Math.max(forming.high, t.price),
          low: Math.min(forming.low, t.price),
          close: t.price,
          volume: forming.volume + t.size,
        };
      }
      return { candles, forming };
    });
  },

  reset: () =>
    set({
      ...emptyMaps(),
      trades: [],
      candles: new Map(),
      forming: null,
      lastTickTs: 0,
      bookRevision: 0,
    }),
}));
