# llm-gateway-proxy

Generic LLM gateway + smart proxy built on top of [OmniRoute](https://github.com/omniroute/omniroute) (v3.8.49).

Routes CLI requests (e.g. OpenCode, agy, codex) through a shared proxy that enforces **per-connection exclusivity** and **priority-ordered fallback**, so concurrent sessions never reuse the same account.

No provider names, keys or connection IDs are committed. All secrets stay in `proxy/config.json` and `seed/seed.json` (gitignored).

## Stack

- **Gateway**: [OmniRoute](https://github.com/omniroute/omniroute) — combos with `priority` + `weight` + `failoverBeforeRetry`, dashboard, health checks
- **Proxy**: `proxy/server.py` — single process, two combos (ports 20129/20133), shared `_inflight` tracking, `_disabled` TTL, retry-before-first-byte, `GET /status`/`/logs`/`/health`

The proxy is the single choke point. OmniRoute handles per-provider routing; the proxy adds cross-combo exclusivity and observability.

## Prerequisites — where the connection IDs come from

The proxy needs the OmniRoute **connection IDs** of your provider accounts. They
are not created by this repo — you must first hook up your accounts in OmniRoute:

1. Start the gateway and open the dashboard: `./scripts/start.sh` → http://127.0.0.1:20128
2. **Endpoints → create an API key**, set it in `.env` as `GATEWAY_API_KEY`.
3. **Providers → connect your accounts** (OAuth or API key). Each connected
   account gets a connection ID.
4. Discover them with the helper script:

```bash
./scripts/list-conns.sh         # list every connection ID + provider + name
./scripts/list-conns.sh --config  # print proxy/config.json pre-filled with real IDs
```

5. Copy the output into `proxy/config.json` (gitignored, never committed).
   Keys must stay as-is: `nvidia_conns.{nim-1,nim-2,nim-3}` and
   `vision_conns.{openrouter,groq,gemini-1,gemini-2,mistral}` — these are the
   exact keys `proxy/server.py` reads.

## Quickstart

```bash
cp proxy/config.example.json proxy/config.json  # fill <GATEWAY_API_KEY> and <CONN_ID_*>
cp seed/seed.example.json seed/seed.json        # fill real combos (or use gateway UI at :20128)
cp .env.example .env                            # fill GATEWAY_API_KEY

./scripts/start.sh          # gateway (20128) + bootstrap combos + proxy (20129, 20133)
./scripts/start.sh --status # health checks
./scripts/start.sh --stop
```

## Ports

- `20128` OmniRoute gateway
- `20129` combo `nvidia-start` (text/agentic)
- `20133` combo `nvidia-vision` (vision)

## Proxy behaviour

- 1 request per connection ID (per account). 4 concurrent chats → 3 primary accounts + wait 3s → fallback providers.
- Disabled-model detection (404 / model not found) with TTL and retry to next in hierarchy, never hang (60s timeout, 2 retries before first byte).
- `GET /status` shows `connections`, `disabled_models`, `inflight_count`. `GET /logs` shows last 80 entries. `GET /health` per port.

## Model hierarchy (edit without touching code)

The pick order per combo lives in JSON, not `server.py`. Edit `proxy/hierarchies.json` —
one entry per model, `conns` lists the config aliases it can use:

```json
{ "model": "nvidia/z-ai/glm-5.2", "weight": 90, "conns": ["nim-1", "nim-2", "nim-3"] }
```

- **Add a model**: new entry in the combo of your choice (`nvidia-start` / `nvidia-vision`), higher `weight` = picked sooner.
- **Remove a model**: delete its entry(ies).
- If `hierarchies.json` is missing the proxy falls back to `proxy/hierarchies.example.json`.
- Aliases come from `proxy/config.json` (`nim-1..3`, `openrouter`, `groq`, `gemini-1/2`, `mistral`).
- Restart the proxy after editing (`./scripts/start.sh`).

## OpenCode integration

```bash
./scripts/setup-opencode.sh   # prints snippet for ~/.config/opencode/opencode.json
# then Ctrl+x m → Gateway Combo nvidia-start / auto
```

## Moving to another device

Copy the repo, connect your providers in OmniRoute (dashboard :20128), run `./scripts/list-conns.sh --config` to get the connection IDs, fill `proxy/config.json` and `seed/seed.json`, then `./scripts/start.sh`. To replace OmniRoute later, only `GATEWAY_URL` and `scripts/bootstrap.sh` need to change.

## Docs

- `docs/ARCHITECTURE.md` — flow and components
- `docs/CONTEXT-COMPACTION.md` — anti-hang layers for context-window exceeded

## License

MIT
