import { useState } from "react";
import { OrderBookPanel } from "./components/OrderBookPanel";
import { TradeTape } from "./components/TradeTape";
import { useWebSocketMarket } from "./hooks/useWebSocketMarket";

const BG     = "#0b0b13";
const BORDER = "1px solid #18182a";
const MONO   = '"IBM Plex Mono", monospace';

export default function App() {
  const [wsOn, setWsOn] = useState(false);
  const { feedStatus, feedMessage } = useWebSocketMarket(wsOn);

  const isLive  = feedStatus === "open";
  const isError = feedStatus === "error";
  const dotColor = isLive ? "#089981" : isError ? "#f23645" : feedStatus === "connecting" ? "#f0c040" : "#2a2a45";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: BG, fontFamily: MONO }}>

      {/* ── Toggle bar ─────────────────────────────────────────────────── */}
      <div style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "4px 12px", background: "#08080f", borderBottom: BORDER,
        fontSize: 11, flexShrink: 0,
      }}>
        <button
          onClick={() => setWsOn(v => !v)}
          style={{
            padding: "3px 14px", borderRadius: 4,
            border: `1px solid ${wsOn ? "#089981" : "#22223a"}`,
            background: wsOn ? "rgba(8,153,129,0.10)" : "transparent",
            color: wsOn ? "#089981" : "#44445a",
            fontFamily: MONO, fontSize: 11, cursor: "pointer", fontWeight: 600,
            transition: "all 0.15s", letterSpacing: "0.06em",
          }}
        >
          {wsOn ? "● L2/L3 LIVE" : "○ Enable L2/L3"}
        </button>

        <span style={{ display: "flex", alignItems: "center", gap: 5, color: isError ? "#f23645" : "#34344e", fontSize: 10 }}>
          <span style={{
            width: 6, height: 6, borderRadius: "50%", background: dotColor, flexShrink: 0,
            boxShadow: isLive ? `0 0 5px ${dotColor}99` : "none",
          }} />
          {wsOn
            ? (feedMessage || (isLive ? "Streaming NBBO quotes & trades" : feedStatus))
            : "Click to start streaming real-time L2/L3 data"}
        </span>

        {wsOn && (
          <span style={{ marginLeft: "auto", fontSize: 9, color: "#22223a", letterSpacing: "0.06em" }}>
            SOURCE: ALPACA NBBO QUOTES
          </span>
        )}
      </div>

      {/* ── Content ────────────────────────────────────────────────────── */}
      {wsOn ? (
        <div style={{ flex: 1, display: "grid", gridTemplateColumns: "1fr 260px", minHeight: 0, overflow: "hidden" }}>

          {/* Order book — full left column */}
          <div style={{ borderRight: BORDER, display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
            <div style={{
              padding: "3px 10px", borderBottom: BORDER, flexShrink: 0,
              fontSize: 9, color: "#2a2a45", letterSpacing: "0.1em", background: "#08080f",
            }}>ORDER BOOK · L2</div>
            <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
              <OrderBookPanel />
            </div>
          </div>

          {/* Time & Sales — right column */}
          <div style={{ display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
            <div style={{
              padding: "3px 10px", borderBottom: BORDER, flexShrink: 0,
              fontSize: 9, color: "#2a2a45", letterSpacing: "0.1em", background: "#08080f",
            }}>TIME &amp; SALES · L3</div>
            <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
              <TradeTape />
            </div>
          </div>
        </div>
      ) : (
        <div style={{
          flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
          color: "#1e1e30", fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase",
        }}>
          L2 / L3 disabled
        </div>
      )}
    </div>
  );
}
