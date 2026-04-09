"""
FIX 4.2 Client – Low-Latency Order Routing
Targets Lime Trading's binary FIX gateway (sub-10ms, co-located NJ Triangle).

For production:
  1. Obtain Lime's FIX specification and session credentials.
  2. Replace the stub session with the quickfix / simplefix library.
  3. Set up SSL/TLS cert pinning for the FIX session.

Message flow: NewOrderSingle (D) → ExecutionReport (8) → OrderCancelRequest (F)
"""
from __future__ import annotations

import logging
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# FIX 4.2 tag constants
TAG_BEGIN_STRING    = 8
TAG_BODY_LENGTH     = 9
TAG_MSG_TYPE        = 35
TAG_SENDER_COMP_ID  = 49
TAG_TARGET_COMP_ID  = 56
TAG_MSG_SEQ_NUM     = 34
TAG_SENDING_TIME    = 52
TAG_CHECKSUM        = 10
TAG_CLORD_ID        = 11
TAG_SYMBOL          = 55
TAG_SIDE            = 54
TAG_ORDER_QTY       = 38
TAG_ORD_TYPE        = 40
TAG_PRICE           = 44
TAG_TIME_IN_FORCE   = 59
TAG_TRANSACT_TIME   = 60

SOH = b"\x01"


@dataclass
class FIXSession:
    host:          str   = "fix.limetrading.com"
    port:          int   = 4200
    sender_comp:   str   = "MYDESK"
    target_comp:   str   = "LIME"
    seq_num:       int   = 1


def _encode_field(tag: int, value: str) -> bytes:
    return f"{tag}={value}".encode() + SOH


def _compute_checksum(body: bytes) -> str:
    return str(sum(body) % 256).zfill(3)


class FIXClient:
    def __init__(self):
        self.session = FIXSession()
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(2.0)
        try:
            self._sock.connect((self.session.host, self.session.port))
            self._send_logon()
            log.info("FIX session established: %s → %s",
                     self.session.sender_comp, self.session.target_comp)
        except (OSError, TimeoutError) as e:
            log.warning("FIX connect failed (stub mode): %s", e)
            self._sock = None

    def _send_logon(self) -> None:
        """FIX Logon (35=A)."""
        fields = (
            _encode_field(TAG_MSG_TYPE, "A") +
            _encode_field(TAG_SENDER_COMP_ID, self.session.sender_comp) +
            _encode_field(TAG_TARGET_COMP_ID, self.session.target_comp) +
            _encode_field(TAG_MSG_SEQ_NUM, str(self.session.seq_num)) +
            _encode_field(TAG_SENDING_TIME, _utc_timestamp()) +
            _encode_field(108, "30")  # HeartBtInt = 30s
        )
        self._send_message(fields)

    def send_new_order_single(self, order: Any) -> dict[str, Any]:
        """
        FIX NewOrderSingle (35=D) for a single-leg order.
        Multi-leg: submit each leg as separate messages or use Lime's combo extension.
        """
        if self._sock is None:
            self.connect()

        clord_id = f"ORD{int(time.time() * 1000)}"

        # Build single-leg payload (first leg only for FIX stub)
        legs = getattr(order, "legs", [])
        if not legs:
            return {"error": "no_legs"}
        leg = legs[0]

        fields = (
            _encode_field(TAG_MSG_TYPE, "D") +
            _encode_field(TAG_SENDER_COMP_ID, self.session.sender_comp) +
            _encode_field(TAG_TARGET_COMP_ID, self.session.target_comp) +
            _encode_field(TAG_MSG_SEQ_NUM, str(self.session.seq_num)) +
            _encode_field(TAG_SENDING_TIME, _utc_timestamp()) +
            _encode_field(TAG_CLORD_ID, clord_id) +
            _encode_field(TAG_SYMBOL, leg.symbol) +
            _encode_field(TAG_SIDE, "1" if leg.side == "BUY" else "2") +
            _encode_field(TAG_ORDER_QTY, str(leg.qty)) +
            _encode_field(TAG_ORD_TYPE, "2" if leg.order_type == "LMT" else "1") +
            (_encode_field(TAG_PRICE, f"{leg.limit_price:.2f}") if leg.limit_price else b"") +
            _encode_field(TAG_TIME_IN_FORCE, "0") +  # DAY
            _encode_field(TAG_TRANSACT_TIME, _utc_timestamp())
        )

        t0 = time.perf_counter_ns()
        self._send_message(fields)
        latency_us = (time.perf_counter_ns() - t0) / 1_000
        log.info("FIX NewOrderSingle sent: %s | latency=%.1f μs", clord_id, latency_us)
        self.session.seq_num += 1

        return {"clord_id": clord_id, "latency_us": latency_us, "status": "SENT"}

    def _send_message(self, body: bytes) -> None:
        header = (
            _encode_field(TAG_BEGIN_STRING, "FIX.4.2") +
            _encode_field(TAG_BODY_LENGTH, str(len(body)))
        )
        full   = header + body
        cksum  = _compute_checksum(full)
        full  += _encode_field(TAG_CHECKSUM, cksum)
        if self._sock:
            try:
                self._sock.sendall(full)
            except OSError as e:
                log.error("FIX send error: %s", e)
        else:
            log.debug("[FIX STUB] Would send %d bytes", len(full))


def _utc_timestamp() -> str:
    t = time.gmtime()
    ms = int((time.time() % 1) * 1000)
    return (f"{t.tm_year}{t.tm_mon:02}{t.tm_mday:02}-"
            f"{t.tm_hour:02}:{t.tm_min:02}:{t.tm_sec:02}.{ms:03}")
