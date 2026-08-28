# llm-gateway-proxy

Generic LLM gateway + smart proxy built on top of [OmniRoute](https://github.com/omniroute/omniroute) (v3.8.49).

Routes CLI requests (e.g. OpenCode, agy, codex) through a shared proxy that enforces **per-connection exclusivity** and **priority-ordered fallback**, so concurrent sessions never reuse the same account.

No provider names, keys or connection IDs are committed. All secrets stay in `proxy/config.json` and `seed/seed.json` (gitignored).

## Stack

- **Gateway**: [OmniRoute](https://github.com/omniroute/omniroute) — combos with `priority` + `weight` + `failoverBeforeRetry`, dashboard, health checks
- **Proxy**: `proxy/server.py` — single process, two combos (ports 20129/20133), shared `_inflight` tracking, `_disabled` TTL, retry-before-first-byte, `GET /status`/`/logs`/`/health`

The proxy is the single choke point. OmniRoute handles per-provider routing; the proxy adds cross-combo exclusivity and observability.

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

## OpenCode integration

```bash
./scripts/setup-opencode.sh   # prints snippet for ~/.config/opencode/opencode.json
# then Ctrl+x m → Gateway Combo nvidia-start / auto
```

## Moving to another device

Copy the repo, fill `proxy/config.json` and `seed/seed.json`, then `./scripts/start.sh`. To replace OmniRoute later, only `GATEWAY_URL` and `scripts/bootstrap.sh` need to change.

## Docs

- `docs/ARCHITECTURE.md` — flow and components
- `docs/CONTEXT-COMPACTION.md` — anti-hang layers for context-window exceeded

## License

MIT
