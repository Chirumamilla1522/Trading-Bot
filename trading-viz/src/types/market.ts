/** L3 tick — normalized to ms timestamps */
export type TradeSide = "buy" | "sell";

export interface TickTrade {
  timestamp: number;
  price: number;
  size: number;
  side: TradeSide;
  tradeId: string;
}

export interface OrderBookSnapshot {
  type: "snapshot";
  bids: [number, number][];
  asks: [number, number][];
}

export interface OrderBookDelta {
  type: "delta";
  bids: [number, number][];
  asks: [number, number][];
}

export type OrderBookMessage = OrderBookSnapshot | OrderBookDelta;

export interface OHLC {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

export type WsInbound =
  | { channel: "tick"; payload: TickTrade }
  | { channel: "book"; payload: OrderBookMessage }
  | { channel: "candle"; payload: OHLC }
  | { channel: "reset" };
