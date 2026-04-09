import { useEffect, useRef } from "react";
import { rafThrottle } from "../data/throttle";
import { normalizeTick } from "../data/normalize";
import { useMarketStore } from "../store/useMarketStore";
import type { OHLC, OrderBookMessage } from "../types/market";

const WS_URL = import.meta.env.VITE_WS_URL ?? "ws://localhost:8765";

function parseMessage(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function useWebSocketMarket(enabled: boolean) {
  const wsRef = useRef<WebSocket | null>(null);

  const flushTrades = useRef(
    rafThrottle(() => {
      /* store updates are synchronous per tick; raf used if we batch in future */
    })
  );

  useEffect(() => {
    if (!enabled) return;

    const pushTrade = useMarketStore.getState().pushTrade;
    const applySnapshot = useMarketStore.getState().applySnapshot;
    const applyDelta = useMarketStore.getState().applyDelta;
    const pushCandle = useMarketStore.getState().pushCandle;
    const aggregateTickToBucket = useMarketStore.getState().aggregateTickToBucket;
    const reset = useMarketStore.getState().reset;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onmessage = (ev) => {
      const data = parseMessage(String(ev.data));
      if (!data || typeof data !== "object") return;
      const msg = data as Record<string, unknown>;

      if (msg.channel === "tick" && msg.payload) {
        const t = normalizeTick(msg.payload as Record<string, unknown>);
        if (t) {
          pushTrade(t);
          aggregateTickToBucket(t, 1000);
          flushTrades.current();
        }
        return;
      }

      if (msg.channel === "book" && msg.payload) {
        const p = msg.payload as OrderBookMessage;
        if (p.type === "snapshot") applySnapshot(p);
        else if (p.type === "delta") applyDelta(p);
        return;
      }

      if (msg.channel === "candle" && msg.payload) {
        const c = msg.payload as OHLC;
        pushCandle(c);
        return;
      }

      if (msg.channel === "reset") {
        reset();
      }
    };

    ws.onerror = () => {
      /* dev: mock server may be down */
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, [enabled]);
}
