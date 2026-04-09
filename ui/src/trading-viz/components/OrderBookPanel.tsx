import { useMemo } from "react";
import { useMarketStore } from "../store/useMarketStore";

const MONO  = '"IBM Plex Mono", monospace';
const UP    = "#089981";
const DOWN  = "#f23645";
const MID   = "#c8c8e0";
const MUTED = "#3a3a58";
const DIM   = "#22223a";
const BG    = "#0b0b13";
const LEVELS = 12;

// Maximum allowed spread as a fraction of mid price before we consider the NBBO stale.
// IEX feed can show >10% spreads for thinly-quoted stocks.
const MAX_SPREAD_PCT = 0.005;   // 0.5 %
// Maximum deviation of best bid/ask from the last trade price to be shown as valid
const MAX_DEVIATE_PCT = 0.008;  // 0.8 %

function fmt(v: number | null, dp = 2) {
  if (v == null || isNaN(v)) return "—";
  return v.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
function fmtSz(v: number) {
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + "M";
  if (v >= 1_000)     return (v / 1_000).toFixed(1) + "k";
  return String(v);
}

// ─── Depth chart ──────────────────────────────────────────────────────────────
interface DepthProps {
  bidPrices: number[]; bidSizes: number[];
  askPrices: number[]; askSizes: number[];
  midPrice: number | null;
}
function DepthChart({ bidPrices, bidSizes, askPrices, askSizes, midPrice }: DepthProps) {
  const W = 240, H = 56;
  const allPrices = [...bidPrices, ...askPrices].filter(Boolean);
  if (allPrices.length < 2) return <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }} />;

  const minP = Math.min(...allPrices);
  const maxP = Math.max(...allPrices);
  const range = maxP - minP || 1;
  const px = (p: number) => ((p - minP) / range) * W;
  const py = (v: number, maxV: number) => H - (v / maxV) * H * 0.92;

  const bidCum: [number, number][] = [];
  let acc = 0;
  [...bidPrices].reverse().forEach((p, i) => { acc += [...bidSizes].reverse()[i]; bidCum.push([p, acc]); });
  bidCum.reverse();

  const askCum: [number, number][] = [];
  acc = 0;
  askPrices.forEach((p, i) => { acc += askSizes[i]; askCum.push([p, acc]); });

  const maxV = Math.max(...bidCum.map(b => b[1]), ...askCum.map(a => a[1])) || 1;

  const bidPath = bidCum.length > 1
    ? bidCum.map(([p, v], i) => `${i === 0 ? "M" : "L"}${px(p).toFixed(1)},${py(v, maxV).toFixed(1)}`).join(" ")
      + ` L${px(bidCum[bidCum.length - 1][0]).toFixed(1)},${H} L${px(bidCum[0][0]).toFixed(1)},${H} Z`
    : null;
  const askPath = askCum.length > 1
    ? askCum.map(([p, v], i) => `${i === 0 ? "M" : "L"}${px(p).toFixed(1)},${py(v, maxV).toFixed(1)}`).join(" ")
      + ` L${px(askCum[askCum.length - 1][0]).toFixed(1)},${H} L${px(askCum[0][0]).toFixed(1)},${H} Z`
    : null;

  const midX = midPrice != null ? px(midPrice) : null;

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block", height: 56 }} preserveAspectRatio="none">
      <defs>
        <linearGradient id="bg_b" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={UP} stopOpacity="0.28" />
          <stop offset="100%" stopColor={UP} stopOpacity="0.04" />
        </linearGradient>
        <linearGradient id="bg_a" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={DOWN} stopOpacity="0.28" />
          <stop offset="100%" stopColor={DOWN} stopOpacity="0.04" />
        </linearGradient>
      </defs>
      {bidPath && <path d={bidPath} fill="url(#bg_b)" />}
      {askPath && <path d={askPath} fill="url(#bg_a)" />}
      {bidCum.length > 1 && (
        <polyline points={bidCum.map(([p, v]) => `${px(p).toFixed(1)},${py(v, maxV).toFixed(1)}`).join(" ")}
          fill="none" stroke={UP} strokeWidth="1.2" strokeOpacity="0.7" />
      )}
      {askCum.length > 1 && (
        <polyline points={askCum.map(([p, v]) => `${px(p).toFixed(1)},${py(v, maxV).toFixed(1)}`).join(" ")}
          fill="none" stroke={DOWN} strokeWidth="1.2" strokeOpacity="0.7" />
      )}
      {midX != null && (
        <line x1={midX} y1={0} x2={midX} y2={H}
          stroke="rgba(200,200,224,0.25)" strokeWidth="1" strokeDasharray="3 3" />
      )}
    </svg>
  );
}

// ─── Main panel ───────────────────────────────────────────────────────────────
export function OrderBookPanel() {
  const bids   = useMarketStore(s => s.bids);
  const asks   = useMarketStore(s => s.asks);
  const trades = useMarketStore(s => s.trades);

  // Last traded price — the most reliable "current price" on an IEX-based feed
  const lastPrice = useMemo(() => {
    if (trades.length === 0) return null;
    return trades[trades.length - 1].price;
  }, [trades]);

  const book = useMemo(() => {
    const allBids = [...bids.entries()].sort((a, b) => b[0] - a[0]);
    const allAsks = [...asks.entries()].sort((a, b) => a[0] - b[0]);

    const rawBestBid = allBids[0]?.[0] ?? null;
    const rawBestAsk = allAsks[0]?.[0] ?? null;

    // Use last trade as the price anchor; fall back to raw NBBO mid only when no trades
    const anchor = lastPrice ?? (rawBestBid != null && rawBestAsk != null
      ? (rawBestBid + rawBestAsk) / 2
      : null);

    // Detect unreliable NBBO: spread too wide OR best bid/ask too far from trades
    const rawSpread = rawBestBid != null && rawBestAsk != null ? rawBestAsk - rawBestBid : null;
    const rawMid = rawBestBid != null && rawBestAsk != null ? (rawBestBid + rawBestAsk) / 2 : null;
    const spreadPct = rawMid ? (rawSpread ?? 0) / rawMid : 0;

    const bidDeviates = anchor != null && rawBestBid != null
      ? Math.abs(rawBestBid - anchor) / anchor : 0;
    const askDeviates = anchor != null && rawBestAsk != null
      ? Math.abs(rawBestAsk - anchor) / anchor : 0;

    const nbboReliable = spreadPct <= MAX_SPREAD_PCT
      && bidDeviates <= MAX_DEVIATE_PCT
      && askDeviates <= MAX_DEVIATE_PCT;

    // Valid levels: filter based on anchor price (last trade), not raw NBBO mid
    const pctWindow = anchor ? anchor * 0.015 : Infinity;
    const visibleBids = allBids
      .filter(([p]) => anchor == null || p >= anchor - pctWindow)
      .slice(0, LEVELS);
    const visibleAsks = allAsks
      .filter(([p]) => anchor == null || p <= anchor + pctWindow)
      .slice(0, LEVELS);

    // Use best valid bid/ask for header display (filtered, not raw NBBO)
    const bestBid   = nbboReliable ? (rawBestBid ?? visibleBids[0]?.[0] ?? null) : null;
    const bestAsk   = nbboReliable ? (rawBestAsk ?? visibleAsks[0]?.[0] ?? null) : null;
    const bestBidSz = nbboReliable ? allBids[0]?.[1] ?? null : null;
    const bestAskSz = nbboReliable ? allAsks[0]?.[1] ?? null : null;
    const mid       = bestBid != null && bestAsk != null ? (bestBid + bestAsk) / 2 : anchor;
    const spread    = bestBid != null && bestAsk != null ? bestAsk - bestBid : null;

    const maxSz = Math.max(
      ...visibleBids.map(b => b[1]),
      ...visibleAsks.map(a => a[1]),
      1,
    );
    const totalBidDepth = visibleBids.reduce((a, [, s]) => a + s, 0);
    const totalAskDepth = visibleAsks.reduce((a, [, s]) => a + s, 0);
    const imbalance = totalBidDepth + totalAskDepth > 0
      ? totalBidDepth / (totalBidDepth + totalAskDepth)
      : 0.5;

    return {
      bestBid, bestAsk, bestBidSz, bestAskSz, mid, spread,
      visibleBids, visibleAsks, maxSz,
      totalBidDepth, totalAskDepth, imbalance,
      nbboReliable, spreadPct,
      depthBidP: visibleBids.map(b => b[0]),
      depthBidS: visibleBids.map(b => b[1]),
      depthAskP: visibleAsks.map(a => a[0]),
      depthAskS: visibleAsks.map(a => a[1]),
    };
  }, [bids, asks, lastPrice]);

  const hasBook = book.visibleBids.length > 0 || book.visibleAsks.length > 0;
  const dp = (book.mid ?? lastPrice ?? 100) >= 100 ? 2 : (book.mid ?? 10) >= 10 ? 3 : 4;

  const cell: React.CSSProperties = {
    padding: "1px 6px", fontSize: 11, fontFamily: MONO, lineHeight: "18px",
  };

  return (
    <div style={{ background: BG, color: MID, fontFamily: MONO, fontSize: 11, userSelect: "none" }}>

      {/* ── Price header: last trade + NBBO ── */}
      <div style={{
        display: "grid", gridTemplateColumns: "1fr auto 1fr",
        padding: "8px 12px 4px", borderBottom: "1px solid #16162a",
        alignItems: "center", gap: 4,
      }}>
        {/* Bid */}
        <div>
          {book.bestBid != null ? (
            <>
              <div style={{ fontSize: 18, fontWeight: 700, color: UP, fontFamily: MONO }}>
                {fmt(book.bestBid, dp)}
              </div>
              <div style={{ fontSize: 10, color: "#2f6459", marginTop: 1 }}>
                {book.bestBidSz != null ? `${fmtSz(book.bestBidSz)} BID` : "BID"}
              </div>
            </>
          ) : (
            <div style={{ fontSize: 12, color: DIM }}>— BID</div>
          )}
        </div>

        {/* Centre: last trade (primary anchor) + spread */}
        <div style={{ textAlign: "center", padding: "0 4px" }}>
          {lastPrice != null && (
            <div style={{ fontSize: 13, color: MID, fontWeight: 700, fontFamily: MONO }}>
              {fmt(lastPrice, dp)}
            </div>
          )}
          {book.spread != null ? (
            <div style={{ fontSize: 9, color: MUTED, marginTop: 1 }}>
              spd {fmt(book.spread, dp)}
            </div>
          ) : !book.nbboReliable && (bids.size > 0 || asks.size > 0) ? (
            <div style={{ fontSize: 9, color: "#6a2a2a", marginTop: 1, letterSpacing: "0.06em" }}>
              WIDE QUOTE
            </div>
          ) : null}
          <div style={{ fontSize: 8, color: DIM, marginTop: 2, letterSpacing: "0.06em" }}>LAST</div>
        </div>

        {/* Ask */}
        <div style={{ textAlign: "right" }}>
          {book.bestAsk != null ? (
            <>
              <div style={{ fontSize: 18, fontWeight: 700, color: DOWN, fontFamily: MONO }}>
                {fmt(book.bestAsk, dp)}
              </div>
              <div style={{ fontSize: 10, color: "#5a2f33", marginTop: 1 }}>
                {book.bestAskSz != null ? `ASK ${fmtSz(book.bestAskSz)}` : "ASK"}
              </div>
            </>
          ) : (
            <div style={{ fontSize: 12, color: DIM, textAlign: "right" }}>ASK —</div>
          )}
        </div>
      </div>

      {/* ── Imbalance bar (only when NBBO is reliable) ── */}
      {hasBook && book.nbboReliable && (
        <div style={{ padding: "4px 12px 2px", borderBottom: "1px solid #16162a" }}>
          <div style={{ display: "flex", height: 6, borderRadius: 3, overflow: "hidden", background: "#16162a" }}>
            <div style={{ width: `${book.imbalance * 100}%`, background: UP, opacity: 0.7 }} />
            <div style={{ flex: 1, background: DOWN, opacity: 0.7 }} />
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: MUTED, marginTop: 2 }}>
            <span style={{ color: UP + "cc" }}>BID {(book.imbalance * 100).toFixed(0)}%</span>
            <span style={{ color: DOWN + "cc" }}>ASK {((1 - book.imbalance) * 100).toFixed(0)}%</span>
          </div>
        </div>
      )}

      {/* ── Depth chart ── */}
      {hasBook && book.depthBidP.length > 1 && (
        <div style={{ padding: "2px 0 0", borderBottom: "1px solid #16162a" }}>
          <DepthChart
            bidPrices={book.depthBidP} bidSizes={book.depthBidS}
            askPrices={book.depthAskP} askSizes={book.depthAskS}
            midPrice={book.mid}
          />
        </div>
      )}

      {/* ── Column headers ── */}
      {hasBook && (
        <div style={{
          display: "grid", gridTemplateColumns: "60px 1fr 1fr 60px",
          padding: "3px 6px", borderBottom: "1px solid #16162a",
          fontSize: 9, color: MUTED, letterSpacing: "0.08em",
        }}>
          <span>SIZE</span>
          <span style={{ color: UP + "aa" }}>BID</span>
          <span style={{ textAlign: "right", color: DOWN + "aa" }}>ASK</span>
          <span style={{ textAlign: "right" }}>SIZE</span>
        </div>
      )}

      {/* ── Paired bid/ask rows ── */}
      {hasBook && (() => {
        const rows = Math.max(book.visibleBids.length, book.visibleAsks.length);
        return Array.from({ length: rows }, (_, i) => {
          const [bidP, bidS] = book.visibleBids[i] ?? [null, null];
          const [askP, askS] = book.visibleAsks[i] ?? [null, null];
          const bidPct = bidS != null ? (bidS / book.maxSz) * 100 : 0;
          const askPct = askS != null ? (askS / book.maxSz) * 100 : 0;
          return (
            <div key={i} style={{
              display: "grid", gridTemplateColumns: "60px 1fr 1fr 60px",
              padding: "0 6px", minHeight: 18, alignItems: "center",
              borderBottom: i % 2 === 0 ? "1px solid #10101c" : "none",
              position: "relative",
            }}>
              {bidPct > 0 && (
                <div style={{ position: "absolute", left: 60, top: 0, bottom: 0,
                  width: `${bidPct * 0.42}%`, background: `${UP}14`, pointerEvents: "none" }} />
              )}
              {askPct > 0 && (
                <div style={{ position: "absolute", right: 60, top: 0, bottom: 0,
                  width: `${askPct * 0.42}%`, background: `${DOWN}14`, pointerEvents: "none" }} />
              )}
              <span style={{ ...cell, color: UP + "bb", textAlign: "left" }}>
                {bidS != null ? fmtSz(bidS) : ""}
              </span>
              <span style={{ ...cell, color: bidP != null ? UP : "transparent" }}>
                {bidP != null ? fmt(bidP, dp) : ""}
              </span>
              <span style={{ ...cell, color: askP != null ? DOWN : "transparent", textAlign: "right" }}>
                {askP != null ? fmt(askP, dp) : ""}
              </span>
              <span style={{ ...cell, color: DOWN + "bb", textAlign: "right" }}>
                {askS != null ? fmtSz(askS) : ""}
              </span>
            </div>
          );
        });
      })()}

      {/* ── Footer ── */}
      <div style={{
        display: "flex", justifyContent: "space-between",
        padding: "4px 10px", borderTop: "1px solid #16162a",
        fontSize: 9, color: DIM,
      }}>
        <span style={{ color: UP + "66" }}>
          {hasBook && book.totalBidDepth > 0 ? `∑ ${fmtSz(book.totalBidDepth)}` : ""}
        </span>
        <span style={{
          letterSpacing: "0.08em",
          color: book.nbboReliable ? DIM : "#4a1a1a",
        }}>
          {book.nbboReliable ? "ALPACA IEX NBBO" : "IEX QUOTE UNRELIABLE"}
        </span>
        <span style={{ color: DOWN + "66" }}>
          {hasBook && book.totalAskDepth > 0 ? `∑ ${fmtSz(book.totalAskDepth)}` : ""}
        </span>
      </div>

      {!hasBook && (
        <div style={{ padding: 20, textAlign: "center", color: DIM, fontSize: 11, letterSpacing: "0.08em" }}>
          AWAITING QUOTES…
        </div>
      )}
    </div>
  );
}
