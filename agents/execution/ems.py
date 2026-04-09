"""
Execution Management System (EMS)
Routes orders to the configured broker via:
  - Alpaca (paper + live) – REST / WebSocket
  - Interactive Brokers – TWS API
  - Lime Trading – FIX 4.2 / Binary DMA
  - Tastytrade – REST with multi-leg combo support

Multi-leg orders are always submitted as a single atomic combo to prevent
"legging out" (partial fill risk).
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from agents.state import FirmState, ReasoningEntry
from agents.config import (
    BROKER, ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
)

log = logging.getLogger(__name__)


# ─── Order model ────────────────────────────────────────────────────────────────

@dataclass
class OrderLeg:
    symbol:      str
    side:        str   # "BUY" | "SELL"
    qty:         int
    order_type:  str   # "LMT" | "MKT"
    limit_price: float | None = None

@dataclass
class MultiLegOrder:
    strategy:   str
    legs:       list[OrderLeg] = field(default_factory=list)
    tif:        str = "DAY"
    notes:      str = ""


# ─── Abstract broker interface ───────────────────────────────────────────────────

class BrokerAdapter(ABC):
    @abstractmethod
    def submit_multi_leg(self, order: MultiLegOrder) -> dict[str, Any]: ...

    @abstractmethod
    def submit_stock_order(self, ticker: str, side: str, qty: float,
                           order_type: str, limit_price: float | None,
                           tif: str) -> dict[str, Any]: ...

    @abstractmethod
    def submit_option_order(self, symbol: str, side: str, qty: int,
                            order_type: str, limit_price: float | None,
                            tif: str) -> dict[str, Any]: ...

    @abstractmethod
    def get_orders(self, limit: int) -> list[dict]: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def cancel_all(self) -> None: ...

    @abstractmethod
    def get_positions(self) -> list[dict]: ...


# ─── Alpaca adapter ─────────────────────────────────────────────────────────────

class AlpacaAdapter(BrokerAdapter):
    """
    Uses Alpaca's Options multi-leg endpoint (single-order entry).
    Docs: https://docs.alpaca.markets/reference/postorder-1
    """
    BASE = ALPACA_BASE_URL

    def __init__(self):
        self.headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type":        "application/json",
        }

    def _to_occ_symbol(self, leg: OrderLeg) -> str:
        """Already expects OCC format from the Trader agent."""
        return leg.symbol

    def submit_multi_leg(self, order: MultiLegOrder) -> dict[str, Any]:
        if len(order.legs) == 1:
            return self._submit_single(order.legs[0])

        # Alpaca multi-leg: submit as legs array in one request
        payload = {
            "type":        "market" if order.legs[0].order_type == "MKT" else "limit",
            "time_in_force": order.tif.lower(),
            "order_class": "mleg",
            "legs": [
                {
                    "symbol":         leg.symbol,
                    "side":           leg.side.lower(),
                    "ratio_qty":      leg.qty,
                    "position_intent": "bto" if leg.side == "BUY" else "sto",
                }
                for leg in order.legs
            ],
        }
        if order.legs[0].limit_price:
            payload["limit_price"] = str(
                sum(
                    (l.limit_price or 0) * (1 if l.side == "BUY" else -1)
                    for l in order.legs
                )
            )

        log.info("Alpaca multi-leg submission: %s", json.dumps(payload, indent=2))
        t0 = time.perf_counter_ns()
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(
                    f"{self.BASE}/v2/orders",
                    headers=self.headers,
                    json=payload,
                )
                resp.raise_for_status()
                latency_ms = (time.perf_counter_ns() - t0) / 1_000_000
                log.info("Order submitted in %.2f ms | id=%s",
                         latency_ms, resp.json().get("id"))
                return resp.json()
        except httpx.HTTPError as e:
            log.error("Alpaca submission error: %s", e)
            return {"error": str(e)}

    def _submit_single(self, leg: OrderLeg) -> dict[str, Any]:
        payload = {
            "symbol":        leg.symbol,
            "qty":           str(leg.qty),
            "side":          leg.side.lower(),
            "type":          "limit" if leg.order_type == "LMT" else "market",
            "time_in_force": "day",
        }
        if leg.limit_price:
            payload["limit_price"] = str(leg.limit_price)
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(f"{self.BASE}/v2/orders",
                               headers=self.headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    def submit_stock_order(
        self,
        ticker: str,
        side: str,           # "buy" | "sell"
        qty: float,
        order_type: str,     # "market" | "limit"
        limit_price: float | None = None,
        tif: str = "day",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol":        ticker.upper(),
            "qty":           str(qty),
            "side":          side.lower(),
            "type":          order_type.lower(),
            "time_in_force": tif.lower(),
        }
        if limit_price is not None:
            payload["limit_price"] = str(round(limit_price, 2))
        log.info("Stock order: %s", payload)
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(f"{self.BASE}/v2/orders", headers=self.headers, json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:400] if e.response else ""
            log.error("Stock order rejected: %s – %s", e, body)
            return {"error": str(e), "detail": body}
        except Exception as e:
            log.error("Stock order error: %s", e)
            return {"error": str(e)}

    def submit_option_order(
        self,
        symbol: str,        # OCC option symbol
        side: str,          # "buy" | "sell"
        qty: int,
        order_type: str,    # "limit" | "market"
        limit_price: float | None = None,
        tif: str = "day",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol":        symbol,
            "qty":           str(qty),
            "side":          side.lower(),
            "type":          order_type.lower(),
            "time_in_force": tif.lower(),
        }
        if limit_price is not None:
            payload["limit_price"] = str(round(limit_price, 2))
        log.info("Option order: %s", payload)
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(f"{self.BASE}/v2/orders", headers=self.headers, json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:400] if e.response else ""
            log.error("Option order rejected: %s – %s", e, body)
            return {"error": str(e), "detail": body}
        except Exception as e:
            log.error("Option order error: %s", e)
            return {"error": str(e)}

    def get_orders(self, limit: int = 20) -> list[dict]:
        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.get(
                    f"{self.BASE}/v2/orders",
                    headers=self.headers,
                    params={"limit": limit, "status": "all", "direction": "desc"},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            log.debug("get_orders: %s", e)
            return []

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.delete(f"{self.BASE}/v2/orders/{order_id}", headers=self.headers)
                resp.raise_for_status()
                return {"status": "cancelled", "id": order_id}
        except Exception as e:
            return {"error": str(e)}

    def cancel_all(self) -> None:
        with httpx.Client(timeout=5.0) as client:
            client.delete(f"{self.BASE}/v2/orders", headers=self.headers)
        log.warning("All orders cancelled.")

    def get_positions(self) -> list[dict]:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{self.BASE}/v2/positions", headers=self.headers)
            resp.raise_for_status()
            return resp.json()


# ─── IBKR stub ──────────────────────────────────────────────────────────────────

class IBKRAdapter(BrokerAdapter):
    """Connects to TWS / IB Gateway via ib_insync. Stub for now."""
    def submit_multi_leg(self, order: MultiLegOrder) -> dict[str, Any]:
        log.info("[IBKR STUB] Would submit: %s", order.strategy)
        return {"status": "stub", "strategy": order.strategy}

    def submit_stock_order(self, ticker, side, qty, order_type, limit_price, tif):
        log.info("[IBKR STUB] stock order: %s %s %s", side, qty, ticker)
        return {"status": "stub"}

    def submit_option_order(self, symbol, side, qty, order_type, limit_price, tif):
        log.info("[IBKR STUB] option order: %s %s %s", side, qty, symbol)
        return {"status": "stub"}

    def get_orders(self, limit=20) -> list[dict]:
        return []

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"status": "stub"}

    def cancel_all(self) -> None:
        log.warning("[IBKR STUB] cancel_all called")

    def get_positions(self) -> list[dict]:
        return []


# ─── Lime Trading FIX stub ───────────────────────────────────────────────────────

class LimeAdapter(BrokerAdapter):
    """
    Lime Trading Binary/FIX 4.2 adapter – production requires Lime's licensed SDK.
    Target latency: < 10 microseconds (co-located).
    """
    def submit_multi_leg(self, order: MultiLegOrder) -> dict[str, Any]:
        from agents.execution.fix_client import FIXClient
        fix = FIXClient()
        return fix.send_new_order_single(order)

    def submit_stock_order(self, ticker, side, qty, order_type, limit_price, tif):
        log.info("[LIME STUB] stock order: %s %s %s", side, qty, ticker)
        return {"status": "stub"}

    def submit_option_order(self, symbol, side, qty, order_type, limit_price, tif):
        log.info("[LIME STUB] option order: %s %s %s", side, qty, symbol)
        return {"status": "stub"}

    def get_orders(self, limit=20) -> list[dict]:
        return []

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"status": "stub"}

    def cancel_all(self) -> None:
        log.warning("[LIME] cancel_all via FIX OrderCancelRequest")

    def get_positions(self) -> list[dict]:
        return []


# ─── EMS facade ─────────────────────────────────────────────────────────────────

class ExecutionManagementSystem:
    def __init__(self):
        self.adapter: BrokerAdapter = {
            "alpaca": AlpacaAdapter,
            "ibkr":   IBKRAdapter,
            "lime":   LimeAdapter,
        }.get(BROKER, AlpacaAdapter)()

    def place_stock_order(
        self,
        ticker: str,
        side: str,
        qty: float,
        order_type: str = "market",
        limit_price: float | None = None,
        tif: str = "day",
    ) -> dict[str, Any]:
        """Submit a plain equity order (paper or live). Returns broker response dict."""
        return self.adapter.submit_stock_order(
            ticker=ticker.upper(),
            side=side.lower(),
            qty=qty,
            order_type=order_type.lower(),
            limit_price=limit_price,
            tif=tif,
        )

    def place_option_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        order_type: str = "limit",
        limit_price: float | None = None,
        tif: str = "day",
    ) -> dict[str, Any]:
        """Submit a single-leg option order. Returns broker response dict."""
        return self.adapter.submit_option_order(
            symbol=symbol,
            side=side.lower(),
            qty=qty,
            order_type=order_type.lower(),
            limit_price=limit_price,
            tif=tif,
        )

    def get_orders(self, limit: int = 20) -> list[dict]:
        return self.adapter.get_orders(limit=limit)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self.adapter.cancel_order(order_id)

    def submit(self, order_payload: dict, state: FirmState) -> None:
        if state.circuit_breaker_tripped or state.kill_switch_active:
            log.error("EMS blocked: kill switch active")
            return

        # Parse the Trader agent's JSON payload
        try:
            legs = [
                OrderLeg(
                    symbol      = l["symbol"],
                    side        = l["side"],
                    qty         = int(l["qty"]),
                    order_type  = l.get("order_type", "LMT"),
                    limit_price = l.get("limit_price"),
                )
                for l in order_payload.get("legs", [])
            ]
            order = MultiLegOrder(
                strategy = order_payload.get("strategy", "unknown"),
                legs     = legs,
                tif      = order_payload.get("tif", "DAY"),
                notes    = order_payload.get("notes", ""),
            )
        except (KeyError, TypeError) as e:
            log.error("Invalid order payload: %s – %s", order_payload, e)
            return

        result = self.adapter.submit_multi_leg(order)
        state.reasoning_log.append(ReasoningEntry(
            agent     = "EMS",
            action    = "ORDER_DISPATCHED",
            reasoning = f"Dispatched via {BROKER.upper()} adapter",
            inputs    = {"order": order.strategy, "legs": len(legs)},
            outputs   = result,
        ))
        log.info("EMS dispatch complete: %s", result)
