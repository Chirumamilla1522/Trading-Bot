export type IndicatorReq =
  | { id: number; kind: "sma"; values: number[]; period: number }
  | { id: number; kind: "ema"; values: number[]; period: number }
  | { id: number; kind: "rsi"; values: number[]; period: number };

export type IndicatorRes = { id: number; values: (number | null)[]; error?: string };

function sma(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out.push(sum / period);
    else out.push(null);
  }
  return out;
}

function ema(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  const k = 2 / (period + 1);
  let prev: number | null = null;
  for (let i = period - 1; i < values.length; i++) {
    if (prev === null) {
      let s = 0;
      for (let j = 0; j < period; j++) s += values[i - j];
      prev = s / period;
    } else {
      prev = values[i] * k + prev * (1 - k);
    }
    out[i] = prev;
  }
  return out;
}

/** Smoothed RSI (Wilder-style smoothing on avg gain/loss). */
function rsi(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  if (values.length < period + 1) return out;
  let avgG = 0;
  let avgL = 0;
  for (let i = 1; i <= period; i++) {
    const ch = values[i] - values[i - 1];
    if (ch >= 0) avgG += ch;
    else avgL -= ch;
  }
  avgG /= period;
  avgL /= period;
  const rsiAt = () => (avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL));
  out[period] = rsiAt();
  for (let i = period + 1; i < values.length; i++) {
    const ch = values[i] - values[i - 1];
    const g = ch > 0 ? ch : 0;
    const l = ch < 0 ? -ch : 0;
    avgG = (avgG * (period - 1) + g) / period;
    avgL = (avgL * (period - 1) + l) / period;
    out[i] = rsiAt();
  }
  return out;
}

self.onmessage = (ev: MessageEvent<IndicatorReq>) => {
  const msg = ev.data;
  try {
    let values: (number | null)[];
    if (msg.kind === "sma") values = sma(msg.values, msg.period);
    else if (msg.kind === "ema") values = ema(msg.values, msg.period);
    else values = rsi(msg.values, msg.period);
    const res: IndicatorRes = { id: msg.id, values: values as number[] };
    (self as unknown as Worker).postMessage(res);
  } catch (e) {
    const res: IndicatorRes = {
      id: msg.id,
      values: [],
      error: e instanceof Error ? e.message : String(e),
    };
    (self as unknown as Worker).postMessage(res);
  }
};
