import { useMemo } from "react";
import { useMarketStore } from "../store/useMarketStore";

const LEVELS = 14;

export function OrderBookPanel() {
  const bids = useMarketStore((s) => s.bids);
  const asks = useMarketStore((s) => s.asks);
  const rev = useMarketStore((s) => s.bookRevision);

  const book = useMemo(() => {
    const askPrices = [...asks.keys()].sort((a, b) => a - b).slice(0, LEVELS);
    const bidPrices = [...bids.keys()].sort((a, b) => b - a).slice(0, LEVELS);
    const bestAsk = askPrices.length ? askPrices[0] : null;
    const bestBid = bidPrices.length ? bidPrices[0] : null;
    let bv = 0;
    let av = 0;
    bids.forEach((s) => (bv += s));
    asks.forEach((s) => (av += s));
    const tot = bv + av;
    const imbalance = tot > 0 ? (bv - av) / tot : 0;
    return {
      askRows: askPrices.map((p) => [p, asks.get(p) ?? 0] as [number, number]),
      bidRows: bidPrices.map((p) => [p, bids.get(p) ?? 0] as [number, number]),
      bestAsk,
      bestBid,
      spread: bestBid != null && bestAsk != null ? bestAsk - bestBid : null,
      imbalance,
    };
  }, [bids, asks, rev]);

  return (
    <div className="panel ob">
      <div className="panel-h">Order book (L2)</div>
      {book.spread != null && (
        <div className="ob-meta">
          <span>Spread {book.spread.toFixed(2)}</span>
          <span title="(bidVol − askVol) / total">Imb {(book.imbalance * 100).toFixed(1)}%</span>
        </div>
      )}
      <div className="ob-table">
        <div className="ob-th">
          <span>Price</span>
          <span>Ask sz</span>
        </div>
        {book.askRows.map(([p, sz]) => (
          <div key={`a-${p}`} className="ob-tr ask">
            <span>{p.toFixed(2)}</span>
            <span>{sz.toFixed(0)}</span>
          </div>
        ))}
        <div className="ob-th bidh">
          <span>Price</span>
          <span>Bid sz</span>
        </div>
        {book.bidRows.map(([p, sz]) => (
          <div key={`b-${p}`} className="ob-tr bid">
            <span>{p.toFixed(2)}</span>
            <span>{sz.toFixed(0)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
