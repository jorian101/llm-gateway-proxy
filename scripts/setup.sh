#!/usr/bin/env bash
# setup.sh - setup end-to-end para OmniRoute + gateway-proxy
# Uso: ./scripts/setup.sh [--reconfigure] [--interactive] [--dry-run]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RECONFIGURE=false
INTERACTIVE=false
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --reconfigure) RECONFIGURE=true ;;
        --interactive) INTERACTIVE=true ;;
        --dry-run) DRY_RUN=true ;;
        *) echo "Uso: $0 [--reconfigure] [--interactive] [--dry-run]" >&2; exit 1 ;;
    esac
done

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_err()  { echo -e "${RED}[ERR]${NC} $*"; }

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# Lockfile para evitar ejecuciones paralelas
LOCKFILE="/tmp/llm-gateway-proxy-setup.lock"
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    log_err "Otro setup.sh está corriendo. Esperá o borrá $LOCKFILE"
    exit 1
fi

# Cargar .env si existe
if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -a; . ./.env; set +a
fi

# Helper: confirmar si interactive
confirm() {
    if [ "$INTERACTIVE" = true ]; then
        read -rp "$1 [y/N] " ans
        [[ "$ans" =~ ^[Yy]$ ]]
    else
        return 0
    fi
}

# Paso 1: Pre-checks
log_info "Paso 1/8: Pre-checks..."

if [ ! -f .env ]; then
    log_err "No existe .env. Copiá .env.example y llenalo:"
    echo "  cp .env.example .env && nano .env"
    exit 1
fi

if [ -z "${OMNIROUTE_MANAGE_KEY:-}" ]; then
    log_err "OMNIROUTE_MANAGE_KEY no está en .env"
    echo "  Conseguila en Dashboard → API Keys → scope=manage"
    exit 1
fi

# Verificar docker y gateway
log_info "Verificando Docker..."
if ! command -v docker >/dev/null; then
    log_err "Docker no instalado"
    exit 1
fi

# Verificar si gateway ya está corriendo
if ! docker ps --format '{{.Names}}' | grep -q '^omniroute$'; then
    log_info "Gateway no está corriendo. Levantando con docker compose..."
    if [ ! -f docker-compose.yml ]; then
        log_err "docker-compose.yml no encontrado"
        exit 1
    fi
    docker compose up -d omniroute
    log_info "Esperando healthcheck :20128..."
    for i in {1..30}; do
        if curl -s -m 5 "http://127.0.0.1:20128/api/providers" \
           -H "Authorization: Bearer $OMNIROUTE_MANAGE_KEY" >/dev/null 2>&1; then
            log_ok "Gateway listo"
            break
        fi
        if [ "$i" = 30 ]; then
            log_err "Gateway no respondió en 30 intentos"
            docker logs omniroute --tail 30 2>&1 | tail -30
            exit 1
        fi
        sleep 2
    done
else
    log_ok "Gateway ya corriendo"
fi

# Paso 2: ADMIN_KEY
log_info "Paso 2/8: ADMIN_KEY..."
if [ -z "${ADMIN_KEY:-}" ]; then
    ADMIN_KEY=$(python3 -c "import secrets; print(secrets.token_hex(16))")
    echo "ADMIN_KEY=$ADMIN_KEY" >> .env
    log_ok "ADMIN_KEY generado y guardado en .env"
else
    log_ok "ADMIN_KEY ya existe"
fi

# Paso 3: Providers
log_info "Paso 3/8: Agregando/verificando providers..."

# Provider list: provider:name:env_var
PROVIDERS=(
    "nvidia:nim-1:${NVIDIA_API_KEY_1:-${NVIDIA_API_KEY:-}}"
    "nvidia:nim-2:${NVIDIA_API_KEY_2:-${NVIDIA_API_KEY:-}}"
    "nvidia:nim-3:${NVIDIA_API_KEY_3:-${NVIDIA_API_KEY:-}}"
    "openrouter:openrouter-1:${OPENROUTER_API_KEY:-}"
    "groq:groq-1:${GROQ_API_KEY:-}"
    "gemini:gemini-1:${GEMINI_API_KEY_1:-${GEMINI_API_KEY:-}}"
    "gemini:gemini-2:${GEMINI_API_KEY_2:-}"
    "mistral:mistral-1:${MISTRAL_API_KEY:-}"
)

declare -A CONN_IDS
declare -A ALIASES=(
    ["nim-1"]="nim-1" ["nim-2"]="nim-2" ["nim-3"]="nim-3"
    ["openrouter-1"]="openrouter"
    ["groq-1"]="groq"
    ["gemini-1"]="gemini-1" ["gemini-2"]="gemini-2"
    ["mistral-1"]="mistral"
)

PROVIDER_COUNT=0
for entry in "${PROVIDERS[@]}"; do
    IFS=':' read -r provider name api_key <<< "$entry"
    if [ -z "$api_key" ]; then
        log_warn "Saltando $provider:$name (no API key en .env)"
        continue
    fi

    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] Agregaría $provider:$name"
        continue
    fi

    log_info "Agregando $provider:$name..."
    CONN_ID=$(./scripts/add-provider.sh "$provider" "$api_key" "$name" \
        --manage-key "$OMNIROUTE_MANAGE_KEY" 2>&1 | grep '^CONN_ID=' | cut -d= -f2)

    if [ -z "$CONN_ID" ]; then
        log_err "Falló agregando $provider:$name"
        continue
    fi

    CONN_IDS["$name"]="$CONN_ID"
    PROVIDER_COUNT=$((PROVIDER_COUNT + 1))
    log_ok "$provider:$name -> $CONN_ID"
done

if [ "$PROVIDER_COUNT" -eq 0 ]; then
    log_err "No se configuró ningún provider. Verificá .env"
    exit 1
fi

log_ok "$PROVIDER_COUNT providers configurados"

# Paso 4: proxy/config.json
log_info "Paso 4/8: Generando proxy/config.json..."

if [ -f proxy/config.json ] && [ "$RECONFIGURE" = false ] && [ "$INTERACTIVE" = true ]; then
    if ! confirm "proxy/config.json existe. Sobrescribir?"; then
        log_info "Manteniendo config.json existente"
    else
        RECONFIGURE=true
    fi
fi

if [ "$DRY_RUN" = true ]; then
    log_info "[DRY-RUN] Generaría proxy/config.json"
else
    # Usar config.example.json como template
    if [ ! -f proxy/config.example.json ]; then
        log_err "proxy/config.example.json no encontrado"
        exit 1
    fi

    python3 - << PY
import json, os
from pathlib import Path

example = json.load(open("proxy/config.example.json"))
cfg = {**example}

# Rellenar nvidia_conns
cfg["nvidia_conns"] = {
    "nim-1": os.environ.get("CONN_IDS_nim_1", "<CONN_ID_1>"),
    "nim-2": os.environ.get("CONN_IDS_nim_2", "<CONN_ID_2>"),
    "nim-3": os.environ.get("CONN_IDS_nim_3", "<CONN_ID_3>"),
}

# Rellenar vision_conns
cfg["vision_conns"] = {
    "openrouter": os.environ.get("CONN_IDS_openrouter_1", "<CONN_ID_V1>"),
    "groq": os.environ.get("CONN_IDS_groq_1", "<CONN_ID_V2>"),
    "gemini-1": os.environ.get("CONN_IDS_gemini_1", "<CONN_ID_V3>"),
    "gemini-2": os.environ.get("CONN_IDS_gemini_2", "<CONN_ID_V4>"),
    "mistral": os.environ.get("CONN_IDS_mistral_1", "<CONN_ID_V5>"),
}

# Preservar api_key y admin_key si ya existen
if os.path.exists("proxy/config.json"):
    existing = json.load(open("proxy/config.json"))
    if "api_key" in existing:
        cfg["api_key"] = existing["api_key"]
    if "admin_key" in existing:
        cfg["admin_key"] = existing["admin_key"]

# Inyectar CONN_IDS reales
for name, cid in os.environ.items():
    if name.startswith("CONN_IDS_"):
        alias = name[9:].lower().replace("_", "-")
        # Los alias están en nvidia_conns o vision_conns
        for k in cfg.get("nvidia_conns", {}):
            if k == alias:
                cfg["nvidia_conns"][k] = os.environ[name]
        for k in cfg.get("vision_conns", {}):
            if k == alias:
                cfg["vision_conns"][k] = os.environ[name]

Path("proxy/config.json").write_text(json.dumps(cfg, indent=2))
print("proxy/config.json generado")
PY

    # Inyectar los CONN_IDS reales capturados
    for name in "${!CONN_IDS[@]}"; do
        alias=${ALIASES[$name]}
        if [ -n "$alias" ]; then
            sed -i "s/\"$alias\": \"<CONN_ID_[^\"]*\"/\"$alias\": \"${CONN_IDS[$name]}\"/" proxy/config.json
        fi
    done

    log_ok "proxy/config.json generado"
fi

# Paso 5: seed/seed.json
log_info "Paso 5/8: Generando seed/seed.json..."

if [ -f seed/seed.json ] && [ "$RECONFIGURE" = false ] && [ "$INTERACTIVE" = true ]; then
    if ! confirm "seed/seed.json existe. Sobrescribir?"; then
        log_info "Manteniendo seed.json existente"
    else
        RECONFIGURE=true
    fi
fi

if [ "$DRY_RUN" = true ]; then
    log_info "[DRY-RUN] Generaría seed/seed.json"
else
    if [ ! -f seed/seed.example.json ]; then
        log_err "seed/seed.example.json no encontrado"
        exit 1
    fi

    python3 - << PY
import json, os
from pathlib import Path

example = json.load(open("seed/seed.example.json"))

# Mapear connection IDs reales
conn_map = {}
for name, cid in os.environ.items():
    if name.startswith("CONN_IDS_"):
        alias = name[9:].lower().replace("_", "-")
        conn_map[alias] = cid

seed = {"combos": []}
for combo in example.get("combos", []):
    if "models" not in combo:
        continue
    new_combo = {k: v for k, v in combo.items() if k != "models"}
    new_combo["models"] = []
    for m in combo["models"]:
        if "connectionId" in m and m["connectionId"].startswith("<CONN_ID_"):
            placeholder = m["connectionId"]
            alias = placeholder[1:-1].lower().replace("_", "-")
            if alias in os.environ:
                m["connectionId"] = os.environ[alias]
        new_combo["models"].append(m)
    seed["combos"].append(new_combo)

Path("seed/seed.json").write_text(json.dumps(seed, indent=2))
print("seed/seed.json generado")
PY

    log_ok "seed/seed.json generado"
fi

# Paso 6: Bootstrap combos
log_info "Paso 6/8: Bootstrap combos en OmniRoute..."

if [ "$DRY_RUN" = true ]; then
    log_info "[DRY-RUN] Ejecutaría bootstrap.sh"
else
    BASE="http://127.0.0.1:20128" API_KEY="$OMNIROUTE_MANAGE_KEY" \
        ./scripts/bootstrap.sh seed/seed.json 2>&1 | tail -5
    log_ok "Combos sincronizados"
fi

# Paso 7: Chat API key
log_info "Paso 7/8: API key de chat..."

if [ "$DRY_RUN" = true ]; then
    log_info "[DRY-RUN] Verificaría/crearía chat key"
else
    # Buscar key con scope chat
    CHAT_KEY=$(python3 - << PY
import json, sys, urllib.request
base = os.environ.get("GATEWAY_URL", "http://127.0.0.1:20128")
key = os.environ["OMNIROUTE_MANAGE_KEY"]
req = urllib.request.Request(f"{base}/api/keys", headers={"Authorization": f"Bearer {key}"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    for k in data.get("keys", []):
        if "chat" in k.get("scopes", []):
            print(k.get("key", k.get("keyValue", "")))
            break
except:
    pass
PY
)

    if [ -z "$CHAT_KEY" ]; then
        log_info "No hay chat key. Creando..."
        CHAT_KEY=$(python3 - << PY
import json, os, urllib.request
base = os.environ.get("GATEWAY_URL", "http://127.0.0.1:20128")
key = os.environ["OMNIROUTE_MANAGE_KEY"]
payload = json.dumps({"name": "gateway-proxy", "scopes": ["chat"]}).encode()
req = urllib.request.Request(f"{base}/api/keys", data=payload,
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.load(r)
    print(resp.get("key", resp.get("keyValue", "")))
except urllib.error.HTTPError as e:
    print(f"ERROR: {e.code} {e.read().decode()}", file=sys.stderr)
    sys.exit(1)
PY
)
        if [ -z "$CHAT_KEY" ]; then
            log_warn "No se pudo crear chat key"
        else
            # Actualizar .env
            if grep -q "^GATEWAY_API_KEY=" .env; then
                sed -i "s/^GATEWAY_API_KEY=.*/GATEWAY_API_KEY=$CHAT_KEY/" .env
            else
                echo "GATEWAY_API_KEY=$CHAT_KEY" >> .env
            fi
            log_ok "Chat key creada y guardada en .env"
        fi
    else
        log_ok "Chat key ya existe"
    fi
fi

# Paso 8: Resumen
log_info "Paso 8/8: Resumen final"

echo ""
echo "================================"
echo "  SETUP COMPLETO"
echo "================================"
echo ""
echo "Gateway:     http://127.0.0.1:20128  (manage key OK)"
echo "Proxy:       20129 (nvidia-start), 20133 (nvidia-vision)"
echo "Providers:   $PROVIDER_COUNT conexiones activas"
echo "Config:      proxy/config.json ✓"
echo "Seed:        seed/seed.json ✓"
echo "Chat key:    en .env (GATEWAY_API_KEY)"
echo ""
echo "Próximos pasos:"
echo "  ./scripts/start.sh              # arrancar proxy"
echo "  ./scripts/setup-opencode.sh     # snippet para OpenCode"
echo ""
log_ok "¡Listo para usar!"