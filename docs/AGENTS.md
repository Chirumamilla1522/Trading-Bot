## Agents (LangGraph pipeline)

The agent pipeline is wired in `agents/graph.py` and runs sequentially.

### Order of execution

- `ingest_data` (deterministic)
- `options_specialist` (LLM)
- `sentiment_analyst` (LLM)
- `strategist` (LLM)
- `risk_manager` (LLM + gates)
- `adversarial_debate` (LLM, optional)
- `desk_head` (LLM, final decision)
- `trader` (deterministic execution; no LLM)
- `xai_log` (persist reasoning)

### What each agent does

- **Options specialist** (`agents/agents/options_specialist.py`)
  - Interprets IV regime/skew/term structure using deterministic analytics.
  - Outputs `analyst_decision` + confidence + reasoning.

- **Sentiment analyst** (`agents/agents/sentiment_analyst.py`)
  - Scores recent `FirmState.news_feed` (recency-weighted).
  - Optional per-headline Redis semantic cache.
  - Outputs `sentiment_decision`, `aggregate_sentiment`, themes/tail risks.

- **Strategist** (`agents/agents/strategist.py`)
  - Creates a concrete `TradeProposal` aligned with regime + IV + sentiment.

- **Risk manager** (`agents/agents/risk_manager.py`)
  - Enforces “capital preservation first”.
  - ABORT here overrides downstream optimism.

- **Adversarial debate** (`agents/agents/adversarial_debate.py`)
  - Bull vs Bear rounds + Judge verdict.
  - Runs only when enabled and a proposal exists.

- **Desk head** (`agents/agents/desk_head.py`)
  - Synthesizes all signals into final `trader_decision` (PROCEED/HOLD/ABORT).

- **Trader** (`agents/agents/trader.py`)
  - Deterministically constructs and submits broker-ready orders from the proposal.
  - No LLM calls (avoid arithmetic/format errors).

### XAI reasoning behavior

Every agent appends a `ReasoningEntry` into `FirmState.reasoning_log`.
`xai_log` persists new entries to `logs/xai/reasoning_YYYYMMDD.jsonl`.

