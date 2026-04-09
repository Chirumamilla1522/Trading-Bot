import { useCallback, useEffect, useRef } from "react";

type Pending = { resolve: (v: (number | null)[]) => void; reject: (e: Error) => void };

export function useIndicatorWorker() {
  const workerRef = useRef<Worker | null>(null);
  const pendingRef = useRef<Map<number, Pending>>(new Map());
  const idRef = useRef(0);

  useEffect(() => {
    const w = new Worker(new URL("../workers/indicators.worker.ts", import.meta.url), {
      type: "module",
    });
    w.onmessage = (ev: MessageEvent<{ id: number; values: (number | null)[]; error?: string }>) => {
      const { id, values, error } = ev.data;
      const p = pendingRef.current.get(id);
      if (!p) return;
      pendingRef.current.delete(id);
      if (error) p.reject(new Error(error));
      else p.resolve(values);
    };
    workerRef.current = w;
    return () => {
      w.terminate();
      workerRef.current = null;
    };
  }, []);

  const run = useCallback(
    (kind: "sma" | "ema" | "rsi", values: number[], period: number) => {
      return new Promise<(number | null)[]>((resolve, reject) => {
        const w = workerRef.current;
        if (!w) {
          reject(new Error("worker not ready"));
          return;
        }
        const id = ++idRef.current;
        pendingRef.current.set(id, { resolve, reject });
        w.postMessage({ id, kind, values, period });
      });
    },
    []
  );

  return { run };
}
