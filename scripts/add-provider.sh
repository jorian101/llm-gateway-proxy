#!/usr/bin/env bash
# add-provider.sh - agregar un provider a OmniRoute vía API
# Uso: ./scripts/add-provider.sh <provider> <api_key> <connection_name> [--manage-key <key>] [--url <url>]
# Ejemplo: ./scripts/add-provider.sh nvidia "nvapi-..." "nim-1"
# Output: CONN_ID=<uuid> (en stdout, para capture)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PROVIDER="${1:-}"
API_KEY="${2:-}"
NAME="${3:-}"

if [ -z "$PROVIDER" ] || [ -z "$API_KEY" ] || [ -z "$NAME" ]; then
    echo "Uso: $0 <provider> <api_key> <connection_name> [--manage-key <key>] [--url <url>]" >&2
    exit 1
fi

shift 3
MANAGE_KEY="${OMNIROUTE_MANAGE_KEY:-}"
BASE="${GATEWAY_URL:-http://127.0.0.1:20128}"

while [ $# -gt 0 ]; do
    case "$1" in
        --manage-key) MANAGE_KEY="$2"; shift 2 ;;
        --url) BASE="$2"; shift 2 ;;
        *) echo "Flag desconocida: $1" >&2; exit 1 ;;
    esac
done

if [ -z "$MANAGE_KEY" ]; then
    echo "ERROR: OMNIROUTE_MANAGE_KEY no configurado (env o --manage-key)" >&2
    exit 1
fi

python3 - "$BASE" "$MANAGE_KEY" "$PROVIDER" "$API_KEY" "$NAME" << 'PY'
import json, sys, urllib.request, urllib.error

base, key, provider, api_key, name = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

# 1. List existing
req = urllib.request.Request(f"{base}/api/providers", headers=headers)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
except urllib.error.HTTPError as e:
    print(f"ERROR listando providers: {e.code} {e.read().decode()}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"ERROR listando providers: {e}", file=sys.stderr)
    sys.exit(1)

# Buscar por name
for c in data.get("connections", []):
    if c.get("name") == name:
        if c.get("isActive"):
            print(f"CONN_ID={c['id']}")  # ya existe y activo
            sys.exit(0)
        else:
            print(f"WARN: connection '{name}' existe pero está inactiva (id={c['id']}). No crear duplicado.", file=sys.stderr)
            print(f"CONN_ID={c['id']}")
            sys.exit(0)

# 2. Crear nueva
payload = json.dumps({"provider": provider, "apiKey": api_key, "name": name}).encode()
req = urllib.request.Request(f"{base}/api/providers", data=payload, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.load(r)
    print(f"CONN_ID={resp.get('id', resp.get('connectionId', ''))}")
except urllib.error.HTTPError as e:
    print(f"ERROR creando provider: {e.code} {e.read().decode()}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"ERROR creando provider: {e}", file=sys.stderr)
    sys.exit(1)
PY