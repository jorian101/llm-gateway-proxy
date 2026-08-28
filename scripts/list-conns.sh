#!/usr/bin/env bash
# list-conns.sh - descubre connection IDs desde OmniRoute y emite plantillas de config
# Uso: ./scripts/list-conns.sh [--config] [--json]
#   (sin flags) lista conexiones: id, provider, name, status
#   --config   además imprime proxy/config.json y seed/seed.json prellenados
#   --json     salida JSON cruda de /api/providers
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
API_KEY="${GATEWAY_API_KEY:-${OMNIROUTE_API_KEY:-<GATEWAY_API_KEY>}}"
BASE="${GATEWAY_URL:-http://127.0.0.1:20128}"

python3 - "$BASE" "$API_KEY" "$@" << 'PY'
import json, os, sys
base, key = sys.argv[1], sys.argv[2]
flags = set(sys.argv[3:])

import urllib.request
req = urllib.request.Request(f"{base}/api/providers", headers={"Authorization": f"Bearer {key}"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
except Exception as e:
    print(f"ERROR: no se pudo consultar {base}/api/providers ({e})", file=sys.stderr)
    print("¿está el gateway arriba? ./scripts/start.sh", file=sys.stderr)
    sys.exit(1)

conns = data.get("connections", [])
if not conns:
    print("No hay conexiones. Conectá providers en el dashboard: http://127.0.0.1:20128", file=sys.stderr)
    sys.exit(1)

if flags & {"--json"}:
    print(json.dumps(data, indent=2)); sys.exit(0)

print("== Conexiones (connection IDs) ==")
for c in sorted(conns, key=lambda c: (c.get("provider", ""), c.get("name", ""))):
    status = "active" if c.get("isActive") else "inactive"
    print(f"  {c['id']}  provider={c.get('provider','?'):<16} name={c.get('name','?'):<16} {status}")

if flags & {"--config"}:
    # agrupar IDs activos por provider, en orden estable
    by_provider = {}
    for c in conns:
        if not c.get("isActive"): continue
        by_provider.setdefault(c["provider"], []).append(c["id"])

    def pick(provider, index=0, placeholder=None):
        ids = by_provider.get(provider, [])
        return ids[index] if index < len(ids) else (placeholder or "<CONN_ID>")

    nvidia = {
        "nim-1": pick("nvidia", 0, "<CONN_ID_1>"),
        "nim-2": pick("nvidia", 1, "<CONN_ID_2>"),
        "nim-3": pick("nvidia", 2, "<CONN_ID_3>"),
    }
    vision = {
        "openrouter": pick("openrouter", 0, "<CONN_ID_V1>"),
        "groq": pick("groq", 0, "<CONN_ID_V2>"),
        "gemini-1": pick("gemini", 0, "<CONN_ID_V3>"),
        "gemini-2": pick("gemini", 1, "<CONN_ID_V4>"),
        "mistral": pick("mistral", 0, "<CONN_ID_V5>"),
    }

    cfg = {
        "target": "http://127.0.0.1:20128/v1",
        "api_key": "<GATEWAY_API_KEY>",
        "port_combo": {"20129": "nvidia-start", "20133": "nvidia-vision"},
        "nvidia_conns": nvidia,
        "vision_conns": vision,
    }
    print("\n== proxy/config.json ==")
    print(json.dumps(cfg, indent=2))
PY
echo "== Done =="
