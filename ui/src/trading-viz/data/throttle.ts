/** Batch callbacks to next animation frame (~16ms) */
export function rafThrottle<T extends unknown[]>(fn: (...args: T) => void): (...args: T) => void {
  let id: number | null = null;
  let last: T | null = null;
  return (...args: T) => {
    last = args;
    if (id != null) return;
    id = requestAnimationFrame(() => {
      id = null;
      if (last) fn(...last);
      last = null;
    });
  };
}

/** Fixed window batching (e.g. 16ms) */
export function timeThrottle<T extends unknown[]>(ms: number, fn: (...args: T) => void): (...args: T) => void {
  let t: ReturnType<typeof setTimeout> | null = null;
  let pending: T | null = null;
  return (...args: T) => {
    pending = args;
    if (t) return;
    t = setTimeout(() => {
      t = null;
      if (pending) fn(...pending);
      pending = null;
    }, ms);
  };
}
