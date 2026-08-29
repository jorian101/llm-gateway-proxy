# Architecture

```
CLI (opencode / agy / codex ...)
  │  X-Session-Id
  ▼
Proxy (proxy/server.py)  20129 nvidia-start  20133 nvidia-vision
  │  _inflight per conn_id (shared), _disabled TTL, retry
  ▼
Gateway (omniroute)  20128  combos: priority + weight + failoverBeforeRetry
  │  per-connection routing
  ▼
Providers (N primary accounts + M fallback)
```

- **Combos** (keys of `HIERARCHIES`: `nvidia-start`, `nvidia-vision`) define ordered hierarchies: top models × 3 NIM accounts, then second cycle, then fallbacks.
- **Hierarchy source is JSON, not code**: the proxy loads `proxy/hierarchies.json` (gitignored) if present, else falls back to `proxy/hierarchies.example.json`. Format per combo: `{ "model": <name>, "weight": <int>, "conns": [aliases from config.json] }`; the proxy expands to one `(provider, conn, model, weight)` tuple per alias. Edit the file to add/remove models — no code change. **Hot reload** on file change (stat per request, atomic swap under lock).
- **Admin API** (`/admin/hierarchy` on proxy ports): GET (read), POST (replace), PATCH (incremental ops: add_model, remove_model, set_weight, set_conns). Auth via `X-Admin-Key` header (`admin_key` in config.json).
- **Per-request override**: request body accepts `preferred_model` (prioritizes that model in NVIDIA pass) and `max_weight` (filters to weight <= value). Compatible with Hermes Agent `/v1/runs`.
- **Proxy** is the single choke point: `pick_free_connection` skips busy `_inflight` and `_disabled` entries, waits 3s for a primary slot before using fallback. Fallback only when primary full or disabled.
- **`proxy/config.json` → `port_combo`** values MUST be keys of the loaded `HIERARCHIES` (`nvidia-start`, `nvidia-vision`). Anything else (e.g. a stray `combo-a`) yields an empty hierarchy → `503 ALL_BUSY` on that port.
- **Gateway** does its own failover; proxy adds cross-combo exclusivity and observability.
- Portable: `proxy/config.json` + `seed/seed.json` + `docker-compose.yml` + `scripts/bootstrap.sh`.

## Moving to another device

Copy the repo, connect your providers in OmniRoute (dashboard :20128), get the connection IDs with `scripts/list-conns.sh`, fill `proxy/config.json` and `seed/seed.json`, then `./scripts/start.sh`.
To replace the gateway later, only `GATEWAY_URL` and `scripts/bootstrap.sh` change.
