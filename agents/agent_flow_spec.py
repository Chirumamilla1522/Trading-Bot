"""
Static description of how data moves through Atlas tiers and the T3 LangGraph.
Served at GET /agents/flow for the UI and integrations.
"""
from __future__ import annotations

AGENT_FLOW_RESPONSE: dict = {
    "summary": (
        "Tier 1: SentimentMonitor (LLM on Tier-2 structured DB) + MovementTracker. "
        "Tier 2: Fundamentals + NewsProcessor. Tier 3: ingest (IV + digests) → OptionsSpecialist → "
        "SentimentAnalyst (reconciled with monitor) → Bull/Bear → Strategist → Risk → Desk → trade. "
        "Auto triggers: sentiment+movement, anomaly, fundamentals change, market_bias+movement; "
        "manual/scanner/timer. Each run appends the XAI log."
    ),
    "tier1": {
        "title": "Tier 1 — always on (movement: no LLM; monitor: LLM on structured news)",
        "interval_sec": {"sentiment_monitor": 60, "movement_tracker": 30},
        "components": [
            {
                "name": "SentimentMonitor",
                "outputs": [
                    "sentiment_monitor_score (LLM synthesis over structured Tier-2 articles)",
                    "sentiment_monitor_confidence",
                    "sentiment_monitor_source (llm_structured | fallback_structured | none)",
                    "aggregate_sentiment",
                    "news_timing_regime",
                    "news_newest_age_minutes",
                ],
            },
            {
                "name": "MovementTracker",
                "outputs": [
                    "movement_signal",
                    "movement_anomaly",
                    "market_bias_score (non-news structure)",
                    "price_change_pct",
                    "momentum",
                    "vol_ratio",
                ],
            },
        ],
        "note": (
            "Feeds the tier bar. T3 auto-triggers include sentiment+movement, anomaly+movement, "
            "material fundamentals change, or strong market_bias+movement (cooldown applies)."
        ),
    },
    "tier2": {
        "title": "Tier 2 — periodic (APIs / optional LLM)",
        "interval_sec": {"fundamentals": 4 * 3600, "news_processor": 120},
        "components": [
            {
                "name": "FundamentalsRefresher",
                "outputs": [
                    "fundamentals",
                    "fundamentals_updated",
                    "fundamentals_material_change (when key fields fingerprint changes)",
                ],
            },
            {
                "name": "NewsProcessor",
                "outputs": ["news_impact_map", "enriched headlines in news_feed"],
            },
        ],
        "note": "Options chain refresh runs in the API server (~15s), not in tier loops.",
    },
    "tier3": {
        "title": "Tier 3 — triggered LangGraph pipeline",
        "triggers": [
            "POST /run_cycle (manual)",
            "Auto: |sentiment|+|movement|, technical anomaly, fundamentals change, or market_bias+movement",
            "Scanner anomaly",
            "api_server fallback timer (respects T3 cooldown)",
        ],
        "nodes": [
            {
                "id": "ingest_data",
                "label": "Ingest",
                "detail": "IV/regime/skew, news timing, market_bias, tier3_structured_digests (Tier-2) → FirmState",
            },
            {
                "id": "early_abort",
                "label": "Early abort",
                "detail": "If ingest gates closed (e.g. circuit breaker) → xai_log",
            },
            {
                "id": "options_specialist",
                "label": "Options specialist",
                "detail": "Vol / structure narrative",
            },
            {
                "id": "sentiment_analyst",
                "label": "Sentiment analyst",
                "detail": "News + themes",
            },
            {
                "id": "bull_researcher",
                "label": "Bull researcher",
                "detail": "bull_argument, bull_conviction",
            },
            {
                "id": "bear_researcher",
                "label": "Bear researcher",
                "detail": "bear_argument, bear_conviction",
            },
            {
                "id": "strategist",
                "label": "Strategist",
                "detail": "Synthesis → proposal direction",
            },
            {
                "id": "risk_manager",
                "label": "Risk manager",
                "detail": "risk_decision, limits",
            },
            {
                "id": "adversarial_debate",
                "label": "Adversarial debate",
                "detail": "Optional if ENABLE_ADVERSARIAL_DEBATE and proposal exists",
            },
            {
                "id": "desk_head",
                "label": "Desk head",
                "detail": "Final desk view before execution gate",
            },
            {
                "id": "trader",
                "label": "Trader",
                "detail": "Autopilot: may place orders",
            },
            {
                "id": "recommend",
                "label": "Recommend",
                "detail": "Advisory: human-in-the-loop proposal",
            },
            {
                "id": "xai_log",
                "label": "XAI log",
                "detail": "Reasoning log persist → END",
            },
        ],
        "edges": [
            ["START", "ingest_data"],
            ["ingest_data", "options_specialist"],
            ["ingest_data", "early_abort"],
            ["early_abort", "xai_log"],
            ["options_specialist", "sentiment_analyst"],
            ["sentiment_analyst", "bull_researcher"],
            ["bull_researcher", "bear_researcher"],
            ["bear_researcher", "strategist"],
            ["strategist", "risk_manager"],
            ["risk_manager", "adversarial_debate"],
            ["risk_manager", "desk_head"],
            ["adversarial_debate", "desk_head"],
            ["desk_head", "trader"],
            ["desk_head", "recommend"],
            ["desk_head", "xai_log"],
            ["trader", "xai_log"],
            ["recommend", "xai_log"],
            ["xai_log", "END"],
        ],
        "branch_notes": {
            "ingest_data": "Conditional: full pipeline vs early_abort → xai_log",
            "risk_manager": "Conditional: optional adversarial_debate before desk_head",
            "desk_head": "Conditional: trader (autopilot) vs recommend (advisory) vs xai_log if no trade",
        },
    },
}
