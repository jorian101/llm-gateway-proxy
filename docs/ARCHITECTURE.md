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

- **Combos** define ordered hierarchies: 5 top models × 3 accounts, then second cycle, then fallbacks.
- **Proxy** is the single choke point: `pick_free_connection` skips busy `_inflight` and `_disabled` entries, waits 3s for a primary slot before using fallback. Fallback only when primary full or disabled.
- **`proxy/config.json` → `port_combo`** values MUST be keys of the proxy's internal `HIERARCHIES` (`nvidia-start`, `nvidia-vision`). Anything else (e.g. a stray `combo-a`) yields an empty hierarchy → `503 ALL_BUSY` on that port.
- **Gateway** does its own failover; proxy adds cross-combo exclusivity and observability.
- Portable: `proxy/config.json` + `seed/seed.json` + `docker-compose.yml` + `scripts/bootstrap.sh`.

## Moving to another device

Copy the repo, fill `proxy/config.json` and `seed/seed.json`, then `./scripts/start.sh`.
To replace the gateway later, only `GATEWAY_URL` and `scripts/bootstrap.sh` change.
