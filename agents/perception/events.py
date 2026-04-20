"""Phase 1 — Rule-based event detection (volume / return / vol spikes)."""
from __future__ import annotations

import statistics
from typing import Any

from agents.perception.schemas import (
    EventDetectionReport,
    EventKind,
    EventSeverity,
    EventSignal,
)


def _returns(closes: list[float]) -> list[float]:
    return [closes[i] - closes[i - 1] for i in range(1, len(closes))]


def build_event_report(bars: list[dict[str, Any]]) -> EventDetectionReport:
    if len(bars) < 25:
        return EventDetectionReport()

    closes = [float(b["close"]) for b in bars]
    vols = [float(b.get("volume") or 0.0) for b in bars]

    events: list[EventSignal] = []

    # Volume z-score on last bar vs prior 20
    vtail = vols[-21:-1]
    v_last = vols[-1]
    if vtail and statistics.mean(vtail) > 0:
        m = statistics.mean(vtail)
        s = statistics.stdev(vtail) if len(vtail) > 1 else m * 0.5
        z = (v_last - m) / (s or 1e-9)
        if z > 3.0:
            events.append(
                EventSignal(
                    kind=EventKind.VOLUME_SPIKE,
                    severity=EventSeverity.HIGH if z > 5 else EventSeverity.MEDIUM,
                    detail=f"Volume z={z:.2f} vs 20-bar history",
                    metric_value=round(z, 3),
                )
            )
        elif z > 2.0:
            events.append(
                EventSignal(
                    kind=EventKind.VOLUME_SPIKE,
                    severity=EventSeverity.LOW,
                    detail=f"Elevated volume z={z:.2f}",
                    metric_value=round(z, 3),
                )
            )

    # Return spike
    rets = _returns(closes)
    if len(rets) >= 20:
        window = rets[-21:-1]
        mu = statistics.mean(window)
        sd = statistics.stdev(window) if len(window) > 1 else abs(mu) + 1e-9
        r_last = rets[-1]
        if sd > 0 and abs(r_last - mu) / sd > 3.0:
            events.append(
                EventSignal(
                    kind=EventKind.RETURN_SPIKE,
                    severity=EventSeverity.HIGH,
                    detail=f"Return spike {(r_last - mu) / sd:.2f} sigma",
                    metric_value=round(r_last, 4),
                )
            )

    # Gap: open vs prev close
    o_last = float(bars[-1]["open"])
    pc = float(bars[-2]["close"])
    if pc > 0:
        gap_pct = (o_last - pc) / pc
        if abs(gap_pct) > 0.02:
            events.append(
                EventSignal(
                    kind=EventKind.GAP,
                    severity=EventSeverity.MEDIUM if abs(gap_pct) > 0.04 else EventSeverity.LOW,
                    detail=f"Session gap {gap_pct*100:.2f}%",
                    metric_value=round(gap_pct, 5),
                )
            )

    # Realized vol spike (std of last 5 returns vs prior 20)
    if len(rets) >= 25:
        short_v = statistics.stdev(rets[-5:])
        long_v = statistics.stdev(rets[-25:-5])
        if long_v > 1e-12 and short_v / long_v > 2.0:
            events.append(
                EventSignal(
                    kind=EventKind.VOLATILITY_SPIKE,
                    severity=EventSeverity.MEDIUM,
                    detail=f"Short/long vol ratio {short_v/long_v:.2f}",
                    metric_value=round(short_v / long_v, 3),
                )
            )

    mode: Any = "normal"
    if any(e.severity == EventSeverity.HIGH for e in events):
        mode = "event_driven"
    elif len(events) >= 2 or any(e.severity == EventSeverity.MEDIUM for e in events):
        mode = "elevated"

    return EventDetectionReport(events=events, mode=mode)
