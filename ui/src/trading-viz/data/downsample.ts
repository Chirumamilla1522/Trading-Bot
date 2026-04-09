/**
 * Largest-Triangle-Three-Buckets — reduce points for rendering while preserving shape.
 * O(n) bucket-based implementation suitable for chart windowing.
 */
export function lttb<T>(data: T[], threshold: number, x: (d: T) => number, y: (d: T) => number): T[] {
  if (threshold >= data.length || threshold <= 2) return data;
  const sampled: T[] = [];
  const bucketSize = (data.length - 2) / (threshold - 2);
  let a = 0;
  sampled.push(data[a]);
  for (let i = 0; i < threshold - 2; i++) {
    const start = Math.floor((i + 1) * bucketSize) + 1;
    const end = Math.floor((i + 2) * bucketSize) + 1;
    const bucket = data.slice(start, end);
    if (!bucket.length) continue;
    const avgX = bucket.reduce((s, d) => s + x(d), 0) / bucket.length;
    const avgY = bucket.reduce((s, d) => s + y(d), 0) / bucket.length;
    let maxArea = -1;
    let maxIdx = start;
    const ax = x(data[a]);
    const ay = y(data[a]);
    for (let j = start; j < end; j++) {
      const bx = x(data[j]);
      const by = y(data[j]);
      const area = Math.abs((ax - avgX) * (by - ay) - (ax - bx) * (avgY - ay)) * 0.5;
      if (area > maxArea) {
        maxArea = area;
        maxIdx = j;
      }
    }
    sampled.push(data[maxIdx]);
    a = maxIdx;
  }
  sampled.push(data[data.length - 1]);
  return sampled;
}
