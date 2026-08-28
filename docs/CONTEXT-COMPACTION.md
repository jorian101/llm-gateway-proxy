# Context compaction — anti-hang

Goal: never hang on context-window exceeded.

## Layers

1. **Model-aware routing** — each hierarchy entry will carry `context_window`; proxy estimates tokens as `sum(len(content))/4` and filters to models where `context_window >= estimated`. (Planned)
2. **Proxy auto-compact** — if no model fits, proxy calls the engram HTTP API (`127.0.0.1:7437`) to summarize `messages[]` into `engram_mem_session_summary` format (Goal/Instructions/Discoveries/Accomplished/NextSteps/RelevantFiles), rebuilds as `[system + summary + last 3 turns]`, re-routes. Falls back to 400 `X-Compact-Required` if engram fails. (Planned)
3. **CLI-side** — opencode on `400 X-Compact-Required` calls `engram_mem_session_summary` via MCP and retries with compacted context. (Planned)

## Guarantees

- Short timeouts (engram 10s, model 60s). Any failure escalates; final response is always a 4xx/5xx, never a hang.
- The summary format preserves important facts; the proxy keeps RAM flat (no extra cache).

## Current state

Layer 1 partial (no `context_window` yet). Layers 2-3 are spec'd, not yet implemented.
