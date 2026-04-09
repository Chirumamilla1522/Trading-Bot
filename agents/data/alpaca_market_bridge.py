"""
Bridge Alpaca stock streaming (trades + quotes) into the UI's market WebSocket shape.

**One shared StockDataStream per api_server process** — each browser tab must NOT open its own
Alpaca connection (Alpaca enforces a small per-account limit → "connection limit exceeded" / HTTP 429).

All `/ws/market` clients receive the same broadcast feed; focus follows ``firm_state.ticker`` (POST
``/set_ticker`` or WS ``{"symbol":...}``). Alpaca runs only for symbols in the top-50 universe;
ticks/quotes/reset are appended to ``logs/market_data/{SYMBOL}.jsonl`` while streaming.
"""
from __future__ import annotations

import logging
import os
import queue
import ssl
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

from agents.data import market_activity
from agents.data import market_data_store

log = logging.getLogger(__name__)

_hub_singleton: Optional["AlpacaMarketHub"] = None
_hub_singleton_lock = threading.Lock()


def get_market_hub() -> "AlpacaMarketHub":
    """Process-wide singleton (one Alpaca stream shared by all UI clients)."""
    global _hub_singleton
    with _hub_singleton_lock:
        if _hub_singleton is None:
            _hub_singleton = AlpacaMarketHub()
        return _hub_singleton


def _alpaca_stream_ssl_context() -> ssl.SSLContext:
    """
    TLS context for wss:// Alpaca market stream.

    macOS / some Python builds lack a usable default CA bundle; certifi fixes that.
    Set ALPACA_STREAM_SSL_VERIFY=false only for local debugging (insecure).
    """
    verify = os.getenv("ALPACA_STREAM_SSL_VERIFY", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    if not verify:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        log.warning(
            "Alpaca market stream: TLS certificate verification disabled "
            "(ALPACA_STREAM_SSL_VERIFY=false) — use only for debugging"
        )
        return ctx

    try:
        import certifi

        cafile = certifi.where()
    except ImportError:
        cafile = None
        log.warning("certifi not installed; using default SSL context for Alpaca stream")

    ctx = ssl.create_default_context()
    if cafile:
        ctx.load_verify_locations(cafile=cafile)
    return ctx


def _alpaca_stream_websocket_params() -> dict[str, Any]:
    """Kwargs merged into websockets.connect (must include Alpaca defaults if overriding)."""
    return {
        "ping_interval": 10,
        "ping_timeout": 180,
        "max_queue": 1024,
        "ssl": _alpaca_stream_ssl_context(),
    }


def _stock_feed() -> DataFeed:
    from agents.config import ALPACA_STOCK_DATA_FEED

    name = (ALPACA_STOCK_DATA_FEED or "iex").strip().lower()
    if name == "sip":
        return DataFeed.SIP
    return DataFeed.IEX


def _tick_size(price: float) -> float:
    """Rough price increment for display levels (NBBO-only feed)."""
    if price >= 1.0:
        return 0.01
    if price >= 0.1:
        return 0.001
    return 0.0001


def quote_to_book_snapshot(q: Any) -> dict[str, Any]:
    """Map Alpaca Quote to UI OrderBookSnapshot (NBBO + stepped levels for depth UI)."""
    bp = float(q.bid_price)
    ap = float(q.ask_price)
    bs = max(1.0, float(q.bid_size))
    a_sz = max(1.0, float(q.ask_size))
    tick = _tick_size(bp)
    bids: list[list[float]] = []
    asks: list[list[float]] = []
    for i in range(10):
        bids.append([round(bp - tick * i, 6), max(1.0, round(bs * (0.72**i)))])
        asks.append([round(ap + tick * i, 6), max(1.0, round(a_sz * (0.72**i)))])
    return {"type": "snapshot", "bids": bids, "asks": asks}


def trade_to_tick(trade: Any, last_mid: float) -> dict[str, Any]:
    """Map Alpaca Trade to UI tick payload."""
    price = float(trade.price)
    size = float(trade.size)
    ts = trade.timestamp
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ms = int(ts.timestamp() * 1000)
    else:
        ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    mid = last_mid if last_mid > 0 else price
    side = "buy" if price >= mid else "sell"
    tid = trade.id
    trade_id = str(tid) if tid is not None else f"{ms}-{price}-{size}"
    return {
        "timestamp": ms,
        "price": price,
        "size": size,
        "side": side,
        "tradeId": trade_id,
    }


class AlpacaMarketHub:
    """
    Single Alpaca StockDataStream shared across all FastAPI `/ws/market` clients.
    Each client registers a queue; every tick/quote is broadcast to all queues.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._symbol = "SPY"
        self._stream: Optional[StockDataStream] = None
        self._thread: Optional[threading.Thread] = None
        self._last_mid: float = 0.0

    def _alpaca_stream_running(self) -> bool:
        """True while the hub thread is starting or ``stream.run()`` is active."""
        with self._lock:
            if self._stream is not None:
                return True
            if self._thread is not None and self._thread.is_alive():
                return True
            return False

    def register(self, out_q: queue.Queue, symbol: str) -> None:
        """Subscribe ``out_q``; start Alpaca when focus is in the top-50 universe."""
        sym = (symbol or "SPY").upper().strip()
        with self._lock:
            self._subscribers.add(out_q)

        if not market_activity.should_stream_realtime(sym):
            try:
                out_q.put_nowait(self._meta_universe_disabled_message(sym))
            except queue.Full:
                log.debug("market hub: client queue full (meta)")
            return

        with self._lock:
            prev = self._symbol
            running = self._stream is not None or (
                self._thread is not None and self._thread.is_alive()
            )

        if not running:
            self._symbol = sym
            log.info("Alpaca market hub: starting shared stream for %s", self._symbol)
            self._start_stream()
        elif sym != prev:
            log.info("Alpaca market hub: client requested %s — resubscribing for all", sym)
            self.resubscribe(sym)

    def unregister(self, out_q: queue.Queue) -> None:
        """Remove subscriber; stop Alpaca when the last client disconnects."""
        stop_stream = False
        with self._lock:
            self._subscribers.discard(out_q)
            stop_stream = len(self._subscribers) == 0
        if stop_stream:
            log.info("Alpaca market hub: no subscribers — stopping shared stream")
            self.stop()

    @staticmethod
    def _meta_universe_disabled_message(sym: str) -> dict[str, Any]:
        return {
            "channel": "meta",
            "payload": {
                "realtime": False,
                "reason": "not_in_universe",
                "symbol": sym,
                "message": (
                    f"{sym} is outside the top-50 scanner universe — "
                    "live Alpaca stream disabled. Select a top-50 symbol for real-time."
                ),
            },
        }

    def _emit_universe_disabled(self, sym: str) -> None:
        """Broadcast: focus left the universe (all WS clients should see this)."""
        self._emit(self._meta_universe_disabled_message(sym))

    def _emit(self, obj: dict[str, Any]) -> None:
        ch = obj.get("channel")
        if ch in ("tick", "book", "reset") and market_activity.should_persist_market_data(self._symbol):
            market_data_store.append_event(
                self._symbol,
                str(ch),
                obj.get("payload") if ch != "reset" else {"reset": True},
            )
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(obj)
            except queue.Full:
                log.debug("market hub: subscriber queue full, dropping message")

    async def _on_trade(self, trade: Any) -> None:
        try:
            t = trade_to_tick(trade, self._last_mid)
            self._emit({"channel": "tick", "payload": t})
        except Exception as e:
            log.debug("trade handler: %s", e)

    async def _on_quote(self, quote: Any) -> None:
        try:
            bp = float(quote.bid_price)
            ap = float(quote.ask_price)
            self._last_mid = (bp + ap) / 2.0
            snap = quote_to_book_snapshot(quote)
            self._emit({"channel": "book", "payload": snap})
        except Exception as e:
            log.debug("quote handler: %s", e)

    def _start_stream(self) -> None:
        from agents.config import ALPACA_API_KEY, ALPACA_SECRET_KEY
        from agents.data.alpaca_stream_limit_patch import install

        install()

        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            raise ValueError("Alpaca API keys are not configured")

        feed = _stock_feed()

        def runner() -> None:
            stream = StockDataStream(
                ALPACA_API_KEY,
                ALPACA_SECRET_KEY,
                feed=feed,
                websocket_params=_alpaca_stream_websocket_params(),
            )
            self._stream = stream
            stream.subscribe_trades(self._on_trade, self._symbol)
            stream.subscribe_quotes(self._on_quote, self._symbol)
            self._emit({"channel": "reset"})
            try:
                stream.run()
            finally:
                self._stream = None

        with self._lock:
            if self._stream is not None:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            t = threading.Thread(target=runner, name="alpaca-market-hub", daemon=True)
            self._thread = t
            t.start()

    def resubscribe(self, new_symbol: str) -> None:
        new_symbol = new_symbol.upper().strip()
        if not new_symbol or new_symbol == self._symbol:
            return
        if not market_activity.should_stream_realtime(new_symbol):
            log.info("Alpaca hub: %s not in universe — stopping stream", new_symbol)
            self.stop()
            self._symbol = new_symbol
            self._emit_universe_disabled(new_symbol)
            return
        stream = self._stream
        if stream is None:
            self._symbol = new_symbol
            return
        old = self._symbol
        self._symbol = new_symbol
        try:
            stream.unsubscribe_trades(old)
            stream.unsubscribe_quotes(old)
            stream.subscribe_trades(self._on_trade, new_symbol)
            stream.subscribe_quotes(self._on_quote, new_symbol)
            self._emit({"channel": "reset"})
        except Exception as e:
            log.warning("Alpaca hub resubscribe %s -> %s failed: %s", old, new_symbol, e)

    def resubscribe_on_focus_change(self, focus_ticker: str) -> None:
        """
        Called when ``firm_state.ticker`` changes (user or agent). Starts the hub if
        subscribers exist and focus is in-universe; stops when focus leaves universe.
        """
        t = (focus_ticker or "").upper().strip()
        if not t:
            return
        if not market_activity.should_stream_realtime(t):
            if self._stream is not None:
                log.info("Focus %s outside top-50 — stopping Alpaca hub", t)
                self.stop()
            self._symbol = t
            self._emit_universe_disabled(t)
            return
        with self._lock:
            n = len(self._subscribers)
        if not self._alpaca_stream_running():
            self._symbol = t
            if n > 0:
                log.info("Alpaca hub: focus %s — starting stream (%d subscriber(s))", t, n)
                self._start_stream()
        else:
            self.resubscribe(t)

    def stop(self) -> None:
        stream = self._stream
        if stream is not None:
            try:
                stream.stop()
            except Exception as e:
                log.debug("stream.stop: %s", e)
        thr = self._thread
        if thr is not None and thr.is_alive() and thr is not threading.current_thread():
            thr.join(timeout=25.0)
        self._thread = None
