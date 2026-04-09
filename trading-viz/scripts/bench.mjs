/**
 * Main-thread throughput sanity check (LTTB-style loop).
 * Run: npm run bench
 */

const N = 400_000;
const points = Array.from({ length: N }, (_, i) => ({
  t: i,
  c: 100 + Math.sin(i / 2000) * 5,
}));
const threshold = 1000;
const bucketSize = (N - 2) / (threshold - 2);

const t0 = performance.now();
let a = 0;
let ops = 0;
for (let i = 0; i < threshold - 2; i++) {
  const start = Math.min(N - 1, Math.floor((i + 1) * bucketSize) + 1);
  const end = Math.min(N, Math.floor((i + 2) * bucketSize) + 1);
  let avgX = 0;
  let avgY = 0;
  const bucket = end - start;
  if (bucket <= 0) continue;
  for (let j = start; j < end; j++) {
    avgX += points[j].t;
    avgY += points[j].c;
    ops++;
  }
  avgX /= bucket;
  avgY /= bucket;
  let maxArea = -1;
  const ax = points[a].t;
  const ay = points[a].c;
  for (let j = start; j < end; j++) {
    const bx = points[j].t;
    const by = points[j].c;
    const area = Math.abs((ax - avgX) * (by - ay) - (ax - bx) * (avgY - ay));
    if (area > maxArea) maxArea = area;
  }
  a = start;
}
const ms = performance.now() - t0;
console.log(`LTTB inner scan: N=${N}, buckets=${threshold - 2}, innerOps~=${ops}`);
console.log(`Time: ${ms.toFixed(2)}ms (${(N / (ms / 1000) / 1e6).toFixed(2)}M input pts/sec)`);
