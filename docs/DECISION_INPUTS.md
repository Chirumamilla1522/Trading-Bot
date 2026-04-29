## Decision Inputs: what we compute, store, and pass to agents

This document is the **single, detailed reference** for the properties this project computes and passes into AI agents (and deterministic gates) to decide:

- **Opening trades**: recommend **BUY / SELL / HOLD / ABORT** for **stocks** and **options**
- **Managing/closing positions**: generate deterministic **CLOSE** recommendations (and autopilot closes)

The core concept: **everything flows through `FirmState`** (`agents/state.py`). Tier‑3 agents operate on a snapshot of `FirmState` and write their outputs back into it.

---

## Key objects and where they live

- **`FirmState`**: shared state, read/write by all agents  
  - File: `agents/state.py`
- **`TechnicalContext`**: deterministic technical bundle computed from OHLCV  
  - File: `agents/technicals.py` → `build_technical_context_from_bars(...)`
  - Stored on state: `FirmState.technical_context`
- **`APlusSetup`**: deterministic “A+ setup” scorecard (multiple modes, including ATH weeklies)  
  - File: `agents/aplus_setup.py` → `compute_aplus_setup(state)`
  - Stored on state: `FirmState.aplus_setup`
- **Options chain analytics**: deterministic IV regime / skew / term structure / candidate slices  
  - File: `agents/features.py` → `build_chain_analytics(...)`, `compute_iv_metrics(...)`
  - Stored on state: `FirmState.iv_*` fields (plus embedded in prompts as `chain_analytics`)
- **Position exit rules**: deterministic mandates at entry time  
  - File: `agents/state.py` → `PositionMandate`
  - Recorded on approval: `agents/api_server.py` → `approve_recommendation(...)`
  - Enforced: `agents/position_monitor.py` → `build_close_recommendations(state)`

---

## 1) Market data inputs (raw) in `FirmState`

These are the “ground truth” market inputs. They are referenced by deterministic logic and passed to agents.

### Underlying spot and chain snapshot

- **`FirmState.ticker`**: current focus ticker (UI + Tier‑3 focus)
- **`FirmState.underlying_price`**: latest spot price used for:
  - chain filtering (ATM selection, delta targeting)
  - deterministic PoP/EV math
  - risk exposure normalization
- **`FirmState.latest_greeks: list[GreeksSnapshot]`**: option chain snapshot (one row per contract)
  - Each `GreeksSnapshot` has:
    - `symbol` (OCC), `expiry`, `strike`, `right`
    - `iv`, `delta`, `gamma`, `theta`, `vega`, `rho`
    - `bid`, `ask`, `timestamp`

Where defined:

```startLine:endLine:/Users/veera/Downloads/Trading-Bot/agents/state.py
class GreeksSnapshot(BaseModel):
    symbol:    str
    expiry:    str
    strike:    float
    right:     OptionRight
    iv:        float = 0.0
    delta:     float = 0.0
    gamma:     float = 0.0
    theta:     float = 0.0
    vega:      float = 0.0
    rho:       float = 0.0
    bid:       float = 0.0
    ask:       float = 0.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)
```

### Account and positions

- **`FirmState.open_positions: list[Position]`** (options positions)
  - `Position.symbol` is the OCC leg identifier and is used as the **key** for mandates and closes.
- **`FirmState.stock_positions: list[StockPosition]`**
- **`FirmState.cash_balance`, `buying_power`, `account_equity`**

Where defined:

```startLine:endLine:/Users/veera/Downloads/Trading-Bot/agents/state.py
class Position(BaseModel):
    leg_id:     str
    symbol:     str
    right:      OptionRight
    strike:     float
    expiry:     str
    quantity:   int
    avg_cost:   float
    current_pnl: float = 0.0
    greeks:     GreeksSnapshot | None = None
```

---

## 2) Deterministic technical context (`TechnicalContext`)

`TechnicalContext` is the core “market terms” bundle that makes agent outputs falsifiable (levels, regime, confirmation/invalidation, etc.).

### Computation source

- Built from OHLCV bars in `agents/technicals.py`:
  - EMA200 regime classification
  - EMA10 short-term magnet
  - volume participation labels + RVOL proxies
  - supports/resistances from pivots + prev week levels
  - triangle compression flags
  - outside-previous-week range marker
  - swing-based stop/target (3R)
  - inflection point signals (volume-price climax, divergence, Bollinger squeeze, candle shape)

### Key fields and how agents use them

#### Regime / trend filters

- `ema200`, `ema200_slope_5d`, `dist_to_ema200_pct`
- `regime_label`: `"trend_up" | "trend_down" | "range" | "unknown"`

Used for:
- **Directional bias** (bull vs bear)
- **Strategy compatibility** (range-fade vs breakout vs pullback)
- **Naked-only safety**: avoid naked short in runaway-move flags; prefer HOLD

#### Levels (structure)

- `supports[]` and `resistances[]` (each `TechnicalLevel` includes `price`, `source`, `distance_pct`, `confidence`, etc.)
- `stop_long`, `target_long_3r`, `stop_short`, `target_short_3r`

Used for:
- “Where am I wrong?” → invalidation pricing
- Deterministic A+ scorecard checks (confluence + risk/reward)
- Risk notes about gap-through-level / squeeze

#### Volume and participation

- `vol_ratio20`, `vol_ratio30` (RVOL proxy)
- `volume_state`: confirming/neutral/fading
- `unusual_volume` boolean
- `volume_confirms_direction`: up/down/neither

Used for:
- confirming breakouts vs fakeouts
- A+ “RVOL > 2” gates (ATH weekly mode)
- inflection signals (volume surge + stall)

#### ATH/VCP diagnostics (for “blue sky breakout”)

- `ath_252_high`, `dist_to_ath_pct`
- `range_std_5`, `range_std_20`, `vcp_contraction`
- `ema10`, `dist_to_ema10_pct`

Used for:
- selecting the ATH-weekly A+ mode
- ensuring “tightness” before breakout attempts

#### Inflection-point signals

- `candle_shape`: doji / hammer / shooting_star / other
- `vol_price_climax`: volume surge + stalled candle body (absorption/exhaustion proxy)
- `rsi_divergence`: bullish/bearish/none
- `macd_divergence`: bullish/bearish/none
- Bollinger:
  - `bb_bandwidth_20`
  - `bb_squeeze` (bandwidth ≤ threshold)
  - `bb_mid_20`, `bb_upper_20`, `bb_lower_20`
- MACD:
  - `macd`, `macd_signal`, `macd_hist`

Used for:
- “setup readiness” flags inside prompts (confirmation/invalidation narratives)
- future deterministic pre-gates (optional)

Where `TechnicalContext` is stored:

```startLine:endLine:/Users/veera/Downloads/Trading-Bot/agents/state.py
class FirmState(BaseModel):
    # ...
    technical_context: TechnicalContext | None = None
```

---

## 3) Deterministic options analytics (IV regime / skew / term structure)

These metrics are computed once (deterministically) so every agent sees **the same** surface interpretation.

### Core IV metrics (stored on state)

- `FirmState.iv_atm`: front-month ATM IV
- `FirmState.iv_skew_ratio`: put_25d / call_25d
- `FirmState.iv_regime`: LOW / NORMAL / ELEVATED / EXTREME
- `FirmState.iv_term_structure`: mapping of expiry bucket → IV proxy

Where written:

- `agents/agents/options_specialist.py` sets:
  - `state.iv_atm`, `state.iv_skew_ratio`, `state.iv_regime`, `state.iv_term_structure`

```startLine:endLine:/Users/veera/Downloads/Trading-Bot/agents/agents/options_specialist.py
    # Propagate IV metrics into state so other agents can read them
    state.iv_atm        = iv_metrics.atm_iv
    state.iv_skew_ratio = iv_metrics.skew_ratio
    state.iv_regime     = iv_metrics.iv_regime
    state.iv_term_structure = iv_metrics.term_structure
```

### Chain analytics passed into prompts

Agents are given `chain_analytics` (precomputed) including:
- `iv_metrics` (atm_iv, skew_ratio, iv_regime)
- `term_structure`
- curated contract slices (near ATM, highest IV, etc.)

Used for:
- deciding “sell premium vs avoid selling into expansion”
- selecting expression consistent with thesis and desk constraints

---

## 4) Deterministic A+ setup scorecard (`APlusSetup`)

The A+ scorecard is a **deterministic gate** that decides whether the system is allowed to auto‑construct a long call/put proposal without LLM discretion.

### Generic A+ mode

The generic scorecard checks confluence items like:
- trend alignment (EMA200 regime)
- key level confluence (distance to nearest major level)
- volume spike
- IV rank low
- catalyst recency (fresh news + strong sentiment)
- invalidation quality (stop distance + ≥3R target)

### ATH weekly call mode (`aplus_mode="ath_weekly_call"`)

When in a strong uptrend near ATH, the scorecard switches to an ATH weekly checklist:
- VCP contraction proxy
- EMA10 magnet
- RVOL > 2
- RSI 60–68
- power-hour time window (ET)

Where computed:

- `agents/aplus_setup.py` → `compute_aplus_setup(state)`

Where used:

- `agents/agents/strategist.py`: if `aplus_setup.recommendation == "PROCEED"`, it constructs a long single-leg option deterministically.

---

## 5) Deterministic PoP / EV diagnostics for long options

For long naked calls/puts, we compute quick “ranking diagnostics”:

- **Breakeven** at expiry:
  - call: \(K + premium\)
  - put: \(K - premium\)
- **PoP**: probability the option finishes profitable at expiry under a lognormal model
- **EV**: approximate expected value at expiry minus premium

Where implemented:

- `agents/options_math.py`

Where attached:

- `TradeProposal` fields (optional):
  - `dte`, `delta`, `breakeven`, `pop`, `ev`

Where shown:

- UI recommendations: `ui/src/main.js` renders PoP/EV when present.

---

## 6) News and sentiment properties

### Raw news in `FirmState`

- `FirmState.news_feed: list[NewsItem]`
  - Each `NewsItem` can include:
    - `impact_score`, `urgency_tier`, `mentioned_tickers`, `vol_prob`, etc.

### Tier‑2 NewsProcessor enrichment

The news processor produces **token-efficient digests** and a **cross‑ticker impact map**:

- `FirmState.news_impact_map`: per-ticker aggregated impact + relationships
- `FirmState.tier3_structured_digests`: short strings that are directly injected into Tier‑3 prompts

Where defined:

```startLine:endLine:/Users/veera/Downloads/Trading-Bot/agents/data/news_processor.py
def _build_llm_digest(a: ProcessedArticle) -> str:
    """
    Compact, low-token representation: one line with the fields agents actually use.
    Keep this short so prompts stay cheap.
    """
```

### Sentiment monitor and analyst outputs

There are two sentiment layers:

- **Tier‑1 SentimentMonitor** (always on): synthesizes structured news into an always-available desk score
- **Tier‑3 SentimentAnalyst** (on demand): runs in the Tier‑3 graph and can override/refine themes

Key fields on `FirmState`:

- `sentiment_monitor_score`: float in \([-1, +1]\)
- `sentiment_monitor_confidence`: 0..1
- `sentiment_monitor_reasoning`: short string for UI/debug
- `sentiment_monitor_source`: `llm_structured | fallback_structured | none`
- `aggregate_sentiment`: rolling score (Tier‑3)
- `sentiment_themes`, `sentiment_tail_risks`: lists of short strings
- `news_newest_age_minutes`, `news_timing_regime`: recency context (“fresh/moderate/stale/none”)
- `market_bias_score`: non-news bias \([-1, +1]\)

How agents use sentiment inputs:

- **Direction alignment**:
  - Calls / longs prefer positive score and trend_up regime
  - Puts / shorts prefer negative score and trend_down regime
- **Catalyst gating** (A+):
  - “fresh news” + strong sentiment threshold is required in the generic scorecard
- **Sizing/conviction**:
  - agents lower confidence when sentiment is stale or contradictory to technicals

---

## 7) Risk inputs and portfolio constraints

Risk is represented in two places:

1) **Portfolio metrics** on `FirmState.risk` (computed frequently)
2) **Hard policy gates** inside `RiskManager` (deterministic + LLM soft review)

### Portfolio metrics (`RiskMetrics`)

Key fields:
- `portfolio_delta/gamma/vega/theta`
- `daily_pnl`
- `opening_nav`, `current_nav`
- `drawdown_pct`
- `max_drawdown_pct` (kill-switch threshold)
- `position_cap_pct` (per-position cap)

Used for:
- halting execution when kill switch / circuit breaker trips
- preventing concentration and excessive delta notional exposure
- global flattening when drawdown exceeds limit (autopilot)

### Kill-switch behavior (global exit)

When `drawdown_pct >= max_drawdown_pct`:
- `kill_switch_active` is set
- all open orders are cancelled
- autopilot will flatten all positions (stocks and option legs)

Implementation:
- `agents/api_server.py` `_position_monitor_task()`

---

## 8) How “open” decisions are produced (buy/sell for new positions)

This system has **multiple layers** that contribute to an open-trade decision:

### Layer A — deterministic data ingestion

- `ingest_data_node` populates:
  - `technical_context`
  - `iv_rank_30d` (inside technical_context)
  - `aplus_setup`

### Layer B — Tier‑3 parallel analysis (fast)

`agents/graph.py` runs these concurrently:

- `stock_specialist_node` → produces stock proposal (BUY/SELL) or HOLD
- `options_specialist_node` → produces options surface view (PROCEED/HOLD/ABORT)
- `sentiment_analyst_node` → produces sentiment decision + themes

### Layer C — Strategist synthesis (single decision point)

`strategist_node` consumes:
- `technical_context` (structure + invalidation)
- `chain_analytics` (surface and candidates)
- sentiment themes + conviction
- desk constraints (allowed structures/rights)

Outputs one of:
- `FirmState.pending_proposal` (**options** trade)
- `FirmState.pending_stock_proposal` (**stock** trade)

### Layer D — RiskManager hard gate

`risk_manager_node` is **non‑negotiable**: if it ABORTs, we do not proceed.

### Layer E — DeskHead supervisor

Final “go/no-go” for advisory (recommendation) vs autopilot (execution).

---

## 9) What properties represent “BUY” vs “SELL” at entry

### Stocks

Stock proposals live in:
- `FirmState.pending_stock_proposal: StockTradeProposal`

Key decision fields:
- `side`: BUY or SELL
- `qty`: rounded to whole shares (min 1)
- `rationale`: must cite technical_context and invalidation
- optional guidance: `stop_loss_pct`, `take_profit_pct` (not broker-enforced)

### Options (single leg)

Options proposals live in:
- `FirmState.pending_proposal: TradeProposal`

Key decision fields:
- `legs[]`: each `TradeLeg` has `side` (BUY/SELL), `right` (CALL/PUT), strike/expiry, qty
- risk fields:
  - `max_risk`, `target_return`
  - `stop_loss_pct`, `take_profit_pct` (used to create position mandate on approval)
- diagnostics (long options only):
  - `dte`, `delta`, `breakeven`, `pop`, `ev`

---

## 10) How “close” decisions are produced (sell to close / buy to close)

All close decisions are deterministic and come from the position monitor:

- File: `agents/position_monitor.py`
- Entry mandates: `FirmState.position_mandates[key]`

### Option closes

For each open option position:
- compute return on cost basis:
  - `return_pct = current_pnl / (abs(qty) * avg_cost * 100)`
- triggers (in order):
  - take-profit
  - stop-loss
  - time-stop (days)
  - theta veto (hours + underlying band)
  - trailing EMA stop (after profit threshold)

Close side rule:
- if position qty > 0 (long), close is SELL
- if qty < 0 (short), close is BUY

### Stock closes

For each stock lot:
- compute return on cost basis:
  - `return_pct = unrealized_pl / cost_basis`
- triggers:
  - take-profit
  - stop-loss
  - time-stop

---

## 11) Position mandates: the “contract” for exits

Mandates are persisted at trade entry to ensure exits do not depend on an LLM remembering rules.

Where defined:
- `agents/state.py` → `PositionMandate`

Where recorded:
- `agents/api_server.py` → `approve_recommendation(...)`

For **BUY option legs**, mandates include:
- hard stop `stop_loss_pct` (weekly policy uses 0.25 by default unless proposal overrides)
- take profit `take_profit_pct` (default 0.75 unless proposal overrides)
- `time_stop_days` (default 2)
- theta veto (default 4h, ±0.5% band)
- trailing EMA (enabled, EMA10 on 15m once return ≥ 20%)

Where enforced:
- `agents/position_monitor.py`

---

## 12) “What is passed to each agent” (practical mapping)

Below is a practical mapping of **inputs → agent**:

### `OptionsSpecialist`

Passed in its context:
- `ticker`, `underlying_price`
- `technical_context` (structure)
- `aplus_setup` (scorecard result)
- `portfolio_delta/vega/theta`, `open_positions`
- `chain_analytics` (IV regime + term structure + curated contracts)
- `long_call_put_candidates_7_14d` (optional PoP/EV list)

Primary output:
- `analyst_decision` (PROCEED/HOLD/ABORT)
- writes IV metrics (`iv_atm`, `iv_skew_ratio`, `iv_regime`, `iv_term_structure`)

### `StockSpecialist`

Consumes:
- `technical_context`, sentiment layer, risk caps, underlying price

Outputs:
- `pending_stock_proposal` and stock decision/confidence

### `SentimentAnalyst`

Consumes:
- Tier‑2 digests + raw news slice + monitor score

Outputs:
- `aggregate_sentiment`, themes, tail risks, sentiment decision/confidence

### `BullResearcher` / `BearResearcher`

Consumes:
- technical_context + sentiment + chain analytics

Outputs:
- arguments + conviction scores

### `Strategist`

Consumes:
- everything above

Outputs:
- either `pending_proposal` (options) or `pending_stock_proposal` (stocks)
- may bypass LLM with deterministic A+ gates

### `RiskManager`

Consumes:
- portfolio metrics, proposals, caps, regime

Outputs:
- ABORT/HOLD/PROCEED (hard gate)

### `DeskHead`

Consumes:
- all decisions and reasoning

Outputs:
- final supervisor decision, recommendation/execution routing


