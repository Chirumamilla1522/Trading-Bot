import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import TradingVizApp from "./trading-viz/App";
import "./trading-viz/index.css";

const el = document.getElementById("atlas-trading-viz-root");
if (el) {
  el.classList.add("trading-viz-root");
  createRoot(el).render(
    <StrictMode>
      <TradingVizApp />
    </StrictMode>
  );
}
