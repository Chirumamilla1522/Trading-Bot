## Troubleshooting

### Symptom: reasoning panel shows old records / not all agents
**What it usually means**: agent cycles aren’t completing cleanly, so the `xai_log` persistence step is not writing new entries.

**Check**
- Is the agent loop running? (UI agent status pill; backend logs)
- Is llama.cpp reachable at `LLAMA_LOCAL_BASE_URL`?
- Are you seeing `SYSTEM / ERROR` lines in `logs/xai/reasoning_YYYYMMDD.jsonl`?

### Symptom: `APITimeoutError: Request timed out`
**Cause**: local LLM request exceeded the configured timeout.

**Fix**
- Increase `LLAMA_LOCAL_TIMEOUT_S` (try 120–300+ depending on model/hardware).
- Reduce prompt sizes (e.g., fewer headlines per sentiment batch).
- Use a smaller GGUF / faster quantization.

### Symptom: news updates but agent reasoning doesn’t
News ingestion is independent: it continuously fills `FirmState.news_feed`.
Reasoning updates only when the LangGraph cycle runs and persists `ReasoningEntry` rows.

### Symptom: OpenRouter-specific errors (if enabled)
- 401/403: bad key / permissions
- 404: wrong model slug or privacy/data policy guardrails
- 429: rate limit → retries may apply

See `docs/CONFIG.md` for OpenRouter knobs.

