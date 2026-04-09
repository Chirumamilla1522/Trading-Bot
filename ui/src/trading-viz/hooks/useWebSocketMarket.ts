import { useEffect, useRef, useState } from "react";
import { rafThrottle } from "../data/throttle";
import { normalizeTick } from "../data/normalize";
import { useMarketStore } from "../store/useMarketStore";
import type { OHLC, OrderBookMessage } from "../types/market";

/** Default: FastAPI `/ws/market` (Alpaca trades + quotes). Override for mock or remote. */
const WS_URL =
  import.meta.env.VITE_MARKET_WS_URL ??
  import.meta.env.VITE_WS_URL ??
  "ws://localhost:8000/ws/market";

export type FeedStatus = "idle" | "connecting" | "open" | "error" | "closed";

function parseMessage(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function useWebSocketMarket(enabled: boolean): {
  feedStatus: FeedStatus;
  feedMessage: string;
} {
  const wsRef = useRef<WebSocket | null>(null);
  const [feedStatus, setFeedStatus] = useState<FeedStatus>("idle");
  const [feedMessage, setFeedMessage] = useState("");

  const flushTrades = useRef(
    rafThrottle(() => {
      /* store updates are synchronous per tick; raf used if we batch in future */
    })
  );

  useEffect(() => {
    if (!enabled) {
      setFeedStatus("idle");
      setFeedMessage("");
      return;
    }

    setFeedStatus("connecting");
    setFeedMessage(`Connecting to ${WS_URL}…`);

    const pushTrade = useMarketStore.getState().pushTrade;
    const applySnapshot = useMarketStore.getState().applySnapshot;
    const applyDelta = useMarketStore.getState().applyDelta;
    const pushCandle = useMarketStore.getState().pushCandle;
    const aggregateTickToBucket = useMarketStore.getState().aggregateTickToBucket;
    const reset = useMarketStore.getState().reset;
    const setSymbol = useMarketStore.getState().setSymbol;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setFeedStatus("open");
      setFeedMessage("Stream connected — waiting for quotes/trades (empty outside market hours is normal).");
      try {
        const fn = (window as Window & { __terminalActiveTicker?: () => string })
          .__terminalActiveTicker;
        if (typeof fn === "function") {
          const sym = fn();
          if (sym && /^[A-Z0-9.\-]+$/i.test(sym)) {
            const u = sym.toUpperCase();
            setSymbol(u);
            ws.send(JSON.stringify({ symbol: u }));
          }
        }
      } catch {
        /* ignore */
      }
    };

    ws.onmessage = (ev) => {
      const data = parseMessage(String(ev.data));
      if (!data || typeof data !== "object") return;
      const msg = data as Record<string, unknown>;

      if (msg.channel === "error") {
        const m = typeof msg.message === "string" ? msg.message : "Server error";
        setFeedStatus("error");
        setFeedMessage(m);
        return;
      }

      if (msg.channel === "meta") {
        const p = msg.payload as Record<string, unknown> | undefined;
        const m =
          typeof p?.message === "string"
            ? p.message
            : "Live feed unavailable for this symbol (outside top-50 universe).";
        setFeedStatus("open");
        setFeedMessage(m);
        return;
      }

      if (msg.channel === "tick" && msg.payload) {
        const t = normalizeTick(msg.payload as Record<string, unknown>);
        if (t) {
          pushTrade(t);
          aggregateTickToBucket(t, 1000);
          flushTrades.current();
        }
        setFeedMessage("Receiving live data.");
        return;
      }

      if (msg.channel === "book" && msg.payload) {
        const p = msg.payload as OrderBookMessage;
        // Alpaca free-tier provides NBBO (1 bid + 1 ask per update), not a real order book.
        // Always replace the entire book so stale price levels never accumulate across quotes.
        // On a paid L2 feed, change this to respect p.type for true delta handling.
        applySnapshot(p);
        setFeedMessage("Receiving live data.");
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
      setFeedStatus("error");
      setFeedMessage(
        "WebSocket failed — is api_server running on :8000? Check browser devtools Network → WS.",
      );
    };

    ws.onclose = (ev) => {
      setFeedStatus("closed");
      if (ev.code !== 1000) {
        setFeedMessage(`Disconnected (code ${ev.code}). Re-toggle “Live feed” or refresh.`);
      } else {
        setFeedMessage("Disconnected.");
      }
    };

    const onTerminalSymbol = (e: Event) => {
      const d = (e as CustomEvent<{ symbol?: string }>).detail;
      const sym = d?.symbol;
      if (!sym || ws.readyState !== WebSocket.OPEN) return;
      const u = sym.toUpperCase();
      // Clear stale book/trades before switching so old levels don't persist
      reset();
      setSymbol(u);
      ws.send(JSON.stringify({ symbol: u }));
    };
    window.addEventListener("terminal:symbol", onTerminalSymbol);

    return () => {
      window.removeEventListener("terminal:symbol", onTerminalSymbol);
      ws.close();
      wsRef.current = null;
    };
  }, [enabled]);

  return { feedStatus, feedMessage };
}
