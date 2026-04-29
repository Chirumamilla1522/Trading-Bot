## Technical Context (Deterministic “chart features” for agents)

This project computes a compact, deterministic `technical_context` bundle from OHLCV bars and
attaches it to `FirmState`. LLM agents are expected to **interpret** these computed features,
not “draw” their own chart lines or invent levels.

The goals are:

- **Market thesis first**: trend/range + participation + levels + range events + patterns.
- **Options expression second**: pick the best allowed **naked short call/put** only after the thesis is clear.
- **Falsifiability**: every recommendation should name confirmation + invalidation and include worst‑case path risks.

---

## Where `technical_context` lives

The state schema is defined in:

- `agents/state.py`
  - `TechnicalContext`
  - `TechnicalLevel`
  - `TrianglePattern`
  - `FirmState.technical_context`

`technical_context` is **optional** (`None` when there isn’t enough data).

---

## When it is computed (Tier‑3 deterministic ingest)

`technical_context` is computed inside the deterministic Tier‑3 ingest node:

- `agents/graph.py` → `ingest_data_node()`

At ingest time:

1. We fetch daily bars (same code path the UI uses).
2. We compute `technical_context` deterministically from those bars.
3. After option-chain IV analytics are computed, we optionally attach an **IV rank** based on persisted ATM IV history.

Key code path:

- `agents/graph.py`
  - fetch daily bars: `agents.data.chart_data.fetch_bars(t, timeframe="1Day", limit=260)`
  - compute context: `agents.technicals.build_technical_context_from_bars(...)`
  - persist + rank IV: `agents.data.iv_history_db.append_atm_iv(...)` and `iv_rank(...)`

---

## Data sources

### Daily OHLCV bars

Bars are fetched via:

- `agents/data/chart_data.py` → `fetch_bars()`

Sources (fallback chain):

- local SQLite daily bars cache (when available)
- yfinance
- Alpaca
- Alpha Vantage

Bars are returned as a list of dicts with keys:

- `time` (unix seconds)
- `open`, `high`, `low`, `close`
- optional `volume`

### IV time series for IV rank

IV rank requires a historical time series of ATM IV, which is not available from a single option chain snapshot.
This project builds that series opportunistically by persisting ATM IV values each time Tier‑3 ingest runs.

Implementation:

- `agents/data/iv_history_db.py`
  - SQLite DB: `cache/iv_history.sqlite3` (configurable via `IV_HISTORY_DB_PATH`)
  - table: `iv_history(ticker, ts_unix, atm_iv)`
  - retention: opportunistically deletes data older than ~120 days
  - rolling rank: `iv_rank(ticker, current_atm_iv, lookback_days=30)`

Note: `iv_rank_30d` will be `None` until enough samples exist (minimum ~10 points).

---

## What we compute (feature set)

All computations below are deterministic and live in:

- `agents/technicals.py` → `build_technical_context_from_bars(...)`

### A) Regime (trend vs range)

Inputs:

- `px_last`
- `ema200` (EMA of daily closes)
- `ema200_slope_5d` (approx slope over last ~5 days)
- `dist_to_ema200_pct`

Outputs:

- `regime_label`: `"trend_up" | "trend_down" | "range" | "unknown"`

Heuristic:

- `range` when distance to EMA200 is small or slope is near-flat (conservative thresholds)
- `trend_up` when above EMA200 and slope is positive
- `trend_down` when below EMA200 and slope is negative

### B) Participation (volume confirmation)

Inputs:

- `vol_last`
- `vol_avg20`
- `vol_ratio20 = vol_last / vol_avg20`

Outputs:

- `volume_state`: `"confirming" | "neutral" | "fading" | "unknown"`
- `unusual_volume`: boolean (elevated participation)
- `volume_confirms_direction`: `"up" | "down" | "neither" | "unknown"`

Notes:

- This is a daily-bars proxy (not intraday microstructure).
- “Confirms direction” is conservative: only set to up/down when volume is clearly elevated.

### C) Levels (support/resistance)

Outputs:

- `supports[]`, `resistances[]` as `TechnicalLevel`:
  - `kind`: support/resistance
  - `price`
  - `source`: `pivot_low` / `pivot_high` / `prev_week_low` / `prev_week_high`
  - `distance_pct` (from current `px_last`)
  - `confidence` (deterministic heuristic)
  - `last_reaction_unix` (when derived from pivots)

How levels are derived:

- Pivot highs/lows from daily bars using a small left/right window.
- Adds prior-week high/low as additional objective levels when available.

### D) Pattern compression (triangles)

Output:

- `triangle` (`TrianglePattern`):
  - `type`: `ASCENDING | DESCENDING | SYMMETRICAL | NONE`
  - `upper`, `lower`
  - `breakout_rule`, `invalidation_rule`
  - `target` (simple measured move)
  - `confidence`

Important:

- This is intentionally conservative and only meant as a “compression risk” flag, not a chartist oracle.

### E) Outside previous week’s range (range event marker)

Outputs:

- `prev_week_high`, `prev_week_low`
- `curr_week_high`, `curr_week_low`
- `outside_prev_week`: bool
- `outside_week_state`: `"CONFIRMED" | "REJECTED" | "UNCLEAR"`

Interpretation:

- Outside-week is treated as a volatility regime marker (tail-risk) rather than a directional signal by itself.

### Momentum (RSI)

Outputs:

- `rsi14` (Wilder RSI)
- `rsi_state`: `oversold | neutral | overbought | unknown`

### Swing stop/target guidance (underlying levels)

Outputs:

- `stop_long`, `target_long_3r`
- `stop_short`, `target_short_3r`

Heuristic:

- Uses nearest support below (for long stop) and nearest resistance above (for short stop).
- Targets are a simple 3R projection from current price to the stop.

These are guidance levels for thesis falsifiability and reward/risk framing, not execution instructions.

---

## How agents are expected to use it (prompt contract)

### Decision stack ordering (A→E)

Agents that produce trade-relevant outputs (notably `OptionsSpecialist` and `Strategist`) are instructed to follow:

1. **Regime** (`regime_label`)
2. **Participation** (`volume_state`)
3. **Levels** (nearest support/resistance; invalidation)
4. **Pattern** (triangle breakout/invalidation/target if present)
5. **Outside-week** (tail risk marker)

This ordering reduces “indicator cherry-picking”.

### “No hallucinated charting”

Agents must:

- Use only the provided `technical_context` fields for levels/patterns/EMA/volume.
- Not invent trendlines or new levels.
- Mark `insufficient_data=true` if the context is missing/unclear.

### Required thesis fields (LLM outputs)

The project enforces a prompt contract (and schema fields) including:

- `bias`: bullish/bearish/neutral
- `setup_type`: range_fade / breakout_continuation / breakout_rejection / trend_pullback
- `key_levels`: supports/resistances used
- `confirmation`: what must happen next
- `invalidation`: what breaks the thesis
- `risk_notes`: must include worst-case path risks

---

## Naked short options guardrails

Because the desk is currently restricted to **naked short call/put only**, the system is conservative about runaway move conditions.

### Prompt-level safety

`OptionsSpecialist` and `Strategist` prompts instruct the model to **prefer HOLD (“wait for confirmation”)** under risk flags such as:

- trend (`trend_up` / `trend_down`) **with confirming volume**
- **outside-week confirmed**
- triangle present **with confirming volume** (compression → expansion risk)

### Deterministic safety gate (Strategist)

Before calling the Strategist LLM, the code may return HOLD early if `technical_context` indicates runaway risk
or proximity-to-level with rising participation (to avoid “steamroll” setups for naked shorts).

Implementation:

- `agents/agents/strategist.py` → `strategist_node()` (early “naked-only safety gate”)

---

## Practical debugging checklist

- If `technical_context` is always `None`:
  - verify daily bars are available via `fetch_bars(... timeframe="1Day")`
  - check yfinance/alpaca/alpha vantage config
  - ensure you have enough bars (EMA200 needs ~210)
- If `iv_rank_30d` is always `None`:
  - you likely don’t have enough persisted samples yet
  - confirm `cache/iv_history.sqlite3` is being written and ingest runs periodically
- If agents mention levels not present:
  - review prompt contract and output schemas; the intended behavior is to use only `technical_context`

