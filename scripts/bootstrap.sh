#!/usr/bin/env bash
# Bootstrap gateway combos from seed.json - portable, idempotente
# Usage: ./scripts/bootstrap.sh [seed.json]
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SEED="${1:-$PROJECT_DIR/seed/seed.json}"
API_KEY="${GATEWAY_API_KEY:-${OMNIROUTE_API_KEY:-<GATEWAY_API_KEY>}}"
BASE="${GATEWAY_URL:-http://127.0.0.1:20128}"
echo "== Gateway bootstrap from $SEED =="
if [ ! -f "$SEED" ]; then echo "seed not found: $SEED"; exit 1; fi
python3 << 'PY'
import json, requests, os, sys
seed = sys.argv[1] if len(sys.argv)>1 else "seed.json"
# allow env override
import pathlib
# read seed path from argv if passed via bash
if len(sys.argv) > 1:
    seed = sys.argv[1]
else:
    seed = os.path.join(os.path.dirname(__file__), "..", "seed", "seed.json")
base = os.getenv("BASE", "http://127.0.0.1:20128")
key = os.getenv("API_KEY", "<GATEWAY_API_KEY>")
with open(seed) as f: data=json.load(f)
for c in data.get("combos",[]):
    if "id" not in c: continue
    name=c["name"]; cid=c["id"]
    payload={"name":name,"strategy":c.get("strategy","priority"),"config":c.get("config",{}),"models":c["models"]}
    r=requests.put(f"{base}/api/combos/{cid}", headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"}, json=payload, timeout=15)
    if r.status_code in (200,201):
        print(f"PUT {name} {cid[:8]} -> {r.status_code}")
    else:
        r2=requests.post(f"{base}/api/combos", headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"}, json=payload, timeout=15)
        print(f"POST {name} -> {r2.status_code} {r2.text[:200]}")
PY
echo "== Done =="
