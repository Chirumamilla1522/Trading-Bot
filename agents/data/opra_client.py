"""
OPRA / Options Data Client
Three operational modes:
  1. LIVE     – reads normalised EnrichedTick from the Rust SHM bridge
  2. DATABENTO – uses the Databento Python SDK (bypasses 40 Gbps raw OPRA requirement)
  3. DELAYED  – 15-min delayed data via Alpaca (zero cost during development)

The SHM mode is the primary path in production (sub-millisecond latency).
Databento is the recommended choice for teams without co-location.
"""
from __future__ import annotations

import json
import logging
import mmap
import os
import struct
import time
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from agents.state import FirmState, GreeksSnapshot, OptionRight, VolSurface, VolSurfacePoint

log = logging.getLogger(__name__)

DATA_SOURCE = os.getenv("OPTIONS_DATA_SOURCE", "delayed")   # shm | databento | delayed


# ─── Abstract feed ───────────────────────────────────────────────────────────────

class OptionsFeed(ABC):
    @abstractmethod
    async def stream(self) -> AsyncIterator[GreeksSnapshot]: ...

    @abstractmethod
    async def get_vol_surface(self, symbol: str) -> VolSurface: ...


# ─── Shared-memory reader (Rust bridge) ──────────────────────────────────────────

class SHMFeed(OptionsFeed):
    SHM_NAME    = os.getenv("SHM_NAME", "trading_ticks")
    SHM_SIZE    = 64 * 1024 * 1024
    HEADER_SIZE = 8

    def __init__(self):
        try:
            self._mm = mmap.mmap(-1, self.SHM_SIZE, tagname=self.SHM_NAME)
            log.info("SHM bridge connected: %s (%d MB)",
                     self.SHM_NAME, self.SHM_SIZE // 1_048_576)
        except OSError as e:
            log.warning("SHM unavailable (%s) – falling back to delayed feed", e)
            self._mm = None

    async def stream(self) -> AsyncIterator[GreeksSnapshot]:
        if self._mm is None:
            return
        last_cursor = self.HEADER_SIZE
        while True:
            self._mm.seek(0)
            cursor = struct.unpack("<Q", self._mm.read(8))[0]
            if cursor > last_cursor:
                self._mm.seek(last_cursor)
                raw = self._mm.read(cursor - last_cursor)
                last_cursor = cursor
                for line in raw.split(b"\n"):
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        yield _dict_to_greeks(data)
                    except json.JSONDecodeError:
                        pass
            else:
                import asyncio
                await asyncio.sleep(0.0001)  # 100 μs poll interval

    async def get_vol_surface(self, symbol: str) -> VolSurface:
        points: list[VolSurfacePoint] = []
        async for tick in self.stream():
            if tick.symbol.startswith(symbol):
                points.append(VolSurfacePoint(
                    strike=tick.strike, expiry=tick.expiry,
                    iv=tick.iv, delta=tick.delta,
                ))
            if len(points) >= 200:
                break
        return VolSurface(underlying=symbol, points=points)


# ─── Databento feed ───────────────────────────────────────────────────────────────

class DatabentoBFeed(OptionsFeed):
    """
    Databento normalises the full OPRA firehose into clean schemas.
    Use the `databento` Python SDK with the OPRA.PILLAR dataset.
    Eliminates need for 40-100 Gbps direct OPRA infrastructure.
    """
    def __init__(self):
        try:
            import databento as db
            key = os.getenv("DATABENTO_API_KEY", "")
            self._client = db.Live(key=key)
            log.info("Databento Live client initialised")
        except ImportError:
            log.error("databento package not installed – pip install databento")
            self._client = None

    async def stream(self) -> AsyncIterator[GreeksSnapshot]:
        if not self._client:
            return
        import asyncio
        self._client.subscribe(
            dataset = "OPRA.PILLAR",
            schema  = "mbp-1",
            stype_in= "parent",
            symbols = ["SPY", "QQQ", "AAPL"],
        )
        for record in self._client:
            yield _databento_to_greeks(record)
            await asyncio.sleep(0)

    async def get_vol_surface(self, symbol: str) -> VolSurface:
        return VolSurface(underlying=symbol, points=[])


# ─── Alpaca delayed feed (dev / zero-cost) ───────────────────────────────────────

class AlpacaDelayedFeed(OptionsFeed):
    """
    15-minute delayed options quotes via alpaca-py OptionHistoricalDataClient.
    Uses get_option_chain() which correctly resolves OCC symbols internally.
    """
    def __init__(self):
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from agents.config import ALPACA_API_KEY, ALPACA_SECRET_KEY
        self._client = OptionHistoricalDataClient(
            api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY
        )

    async def stream(self) -> AsyncIterator[GreeksSnapshot]:
        import asyncio
        while True:
            snaps = await asyncio.to_thread(self._fetch_snapshots, "SPY")
            for s in snaps:
                yield s
            await asyncio.sleep(15)  # poll every 15 s (delayed feed)

    def _fetch_snapshots(self, symbol: str) -> list[GreeksSnapshot]:
        from alpaca.data.requests import OptionChainRequest
        try:
            chain = self._client.get_option_chain(
                OptionChainRequest(underlying_symbol=symbol, limit=200)
            )
            return [_alpaca_chain_to_greeks(occ_sym, snap) for occ_sym, snap in chain.items()]
        except Exception as e:
            log.warning("Alpaca snapshot error: %s", e)
            return []

    async def get_vol_surface(self, symbol: str) -> VolSurface:
        import asyncio
        snaps = await asyncio.to_thread(self._fetch_snapshots, symbol)
        points = [
            VolSurfacePoint(strike=s.strike, expiry=s.expiry, iv=s.iv, delta=s.delta)
            for s in snaps
        ]
        return VolSurface(underlying=symbol, points=points)


# ─── Factory ─────────────────────────────────────────────────────────────────────

def create_feed() -> OptionsFeed:
    return {
        "shm":       SHMFeed,
        "databento": DatabentoBFeed,
        "delayed":   AlpacaDelayedFeed,
    }.get(DATA_SOURCE, AlpacaDelayedFeed)()


# ─── Conversion helpers ───────────────────────────────────────────────────────────

def _dict_to_greeks(d: dict) -> GreeksSnapshot:
    raw = d.get("raw", d)
    return GreeksSnapshot(
        symbol  = raw.get("symbol", ""),
        expiry  = raw.get("expiry", ""),
        strike  = float(raw.get("strike", 0)),
        right   = OptionRight(raw.get("right", "CALL")),
        iv      = float(d.get("iv", 0)),
        delta   = float(d.get("delta", 0)),
        gamma   = float(d.get("gamma", 0)),
        theta   = float(d.get("theta", 0)),
        vega    = float(d.get("vega", 0)),
        rho     = float(d.get("rho", 0)),
        bid     = float(raw.get("bid", 0)),
        ask     = float(raw.get("ask", 0)),
    )


def _databento_to_greeks(record: Any) -> GreeksSnapshot:
    return GreeksSnapshot(
        symbol = getattr(record, "instrument_id", ""),
        expiry = "",
        strike = 0.0,
        right  = OptionRight.CALL,
        bid    = float(getattr(record, "bid_px_00", 0) or 0) / 1e9,
        ask    = float(getattr(record, "ask_px_00", 0) or 0) / 1e9,
    )


def _alpaca_snapshot_to_greeks(occ_symbol: str, snap: dict) -> GreeksSnapshot:
    """Legacy dict-based converter (kept for reference)."""
    greeks = snap.get("greeks", {})
    quote  = snap.get("latestQuote", {})
    detail = snap.get("details", {})
    return GreeksSnapshot(
        symbol = occ_symbol,
        expiry = detail.get("expiration_date", ""),
        strike = float(detail.get("strike_price", 0)),
        right  = OptionRight.CALL if detail.get("type", "C") == "C" else OptionRight.PUT,
        iv     = float(snap.get("impliedVolatility", 0)),
        delta  = float(greeks.get("delta", 0)),
        gamma  = float(greeks.get("gamma", 0)),
        theta  = float(greeks.get("theta", 0)),
        vega   = float(greeks.get("vega", 0)),
        rho    = float(greeks.get("rho", 0)),
        bid    = float(quote.get("bp", 0)),
        ask    = float(quote.get("ap", 0)),
    )


def _alpaca_chain_to_greeks(occ_symbol: str, snap: Any) -> GreeksSnapshot:
    """Converts an alpaca-py OptionSnapshot object to our GreeksSnapshot."""
    def _f(obj, *attrs, default=0.0):
        for attr in attrs:
            obj = getattr(obj, attr, None)
            if obj is None:
                return default
        try:
            return float(obj)
        except (TypeError, ValueError):
            return default

    # OCC symbol: e.g. SPY260620C00500000 → expiry=260620, right=C, strike=500.00
    right = OptionRight.CALL if len(occ_symbol) >= 7 and occ_symbol[-9] == "C" else OptionRight.PUT
    try:
        strike = float(occ_symbol[-8:]) / 1000.0
        expiry = occ_symbol[-15:-9]   # YYMMDD
    except (ValueError, IndexError):
        strike, expiry = 0.0, ""

    return GreeksSnapshot(
        symbol = occ_symbol,
        expiry = expiry,
        strike = strike,
        right  = right,
        iv     = _f(snap, "implied_volatility"),
        delta  = _f(snap, "greeks", "delta"),
        gamma  = _f(snap, "greeks", "gamma"),
        theta  = _f(snap, "greeks", "theta"),
        vega   = _f(snap, "greeks", "vega"),
        rho    = _f(snap, "greeks", "rho"),
        bid    = _f(snap, "latest_quote", "bid_price"),
        ask    = _f(snap, "latest_quote", "ask_price"),
    )
