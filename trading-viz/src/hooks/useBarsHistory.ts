import { useEffect, useRef, useState } from "react";
import { useMarketStore } from "../store/useMarketStore";
import type { OHLC } from "../types/market";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

type BarsResponse = {
  ticker: string;
  timeframe: string;
  source: string;
  count: number;
  bars: OHLC[];
};

export type BarsStatus = "idle" | "loading" | "ready" | "error";

export function useBarsHistory(timeframe: string = "5D"): {
  barsStatus: BarsStatus;
  barsMessage: string;
} {
  const symbol = useMarketStore((s) => s.symbol);
  const setCandlesFromBars = useMarketStore((s) => s.setCandlesFromBars);

  const [barsStatus, setBarsStatus] = useState<BarsStatus>("idle");
  const [barsMessage, setBarsMessage] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const sym = (symbol || "").toUpperCase().trim();
    if (!sym || sym === "DEMO") return;

    setBarsStatus("loading");
    setBarsMessage("Loading chart history from stored bars…");

    try {
      abortRef.current?.abort();
    } catch {
      /* ignore */
    }
    const ac = new AbortController();
    abortRef.current = ac;

    const limit = timeframe === "MAX" || timeframe === "1Day" ? 8000 : 520;
    const url = `${API_URL}/bars/${encodeURIComponent(sym)}?timeframe=${encodeURIComponent(timeframe)}&limit=${limit}`;

    (async () => {
      try {
        const r = await fetch(url, { signal: ac.signal });
        if (!r.ok) throw new Error(`bars ${r.status}`);
        const data = (await r.json()) as BarsResponse;
        setCandlesFromBars(data.bars || []);
        setBarsStatus("ready");
        setBarsMessage(`Chart: ${data.source || "bars"} · ${data.count ?? (data.bars?.length ?? 0)} pts`);
      } catch (e) {
        if ((e as Error)?.name === "AbortError") return;
        setBarsStatus("error");
        setBarsMessage("Chart history load failed (API /bars).");
      }
    })();

    return () => {
      try {
        ac.abort();
      } catch {
        /* ignore */
      }
    };
  }, [symbol, timeframe, setCandlesFromBars]);

  return { barsStatus, barsMessage };
}

