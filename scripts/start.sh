#!/usr/bin/env bash
set -e
# start.sh - levanta gateway + proxy en 1 comando (portable)
# Uso: ./scripts/start.sh | ./scripts/start.sh --stop | ./scripts/start.sh --status
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
API_KEY="${GATEWAY_API_KEY:-${OMNIROUTE_API_KEY:-}}"
API_KEY="${API_KEY:-<GATEWAY_API_KEY>}"
COMPOSE="$PROJECT_DIR/docker-compose.yml"
SEED="$PROJECT_DIR/seed/seed.json"
PROXY_PY="$PROJECT_DIR/proxy/server.py"
LOG="/tmp/gateway-proxy.log"

stop() {
  echo "== Stop =="
  pkill -f "proxy/server.py" 2>/dev/null && echo "proxy detenido" || echo "proxy no corría"
  pkill -f "omniroute-unified-proxy" 2>/dev/null || true
  docker stop omniroute 2>/dev/null && echo "gateway detenido" || true
}

status() {
  echo "== Status =="
  echo "-- docker --"
  docker ps --format "{{.Names}} {{.Status}} {{.Ports}}" 2>&1 | grep -E "omniroute|gateway|NAMES" || echo "gateway no está en docker ps"
  echo "-- proxy --"
  pgrep -f "proxy/server.py" >/dev/null && echo "proxy corriendo (PID $(pgrep -f "proxy/server.py"))" || echo "proxy detenido"
  pgrep -f "omniroute-unified-proxy" >/dev/null && echo "proxy (legacy path) corriendo" || true
  echo "-- health --"
  timeout 8 curl -s http://127.0.0.1:20128/api/combos -H "Authorization: Bearer $API_KEY" 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"gateway 20128 OK combos={len(d.get('combos',[]))}\")" 2>&1 || echo "gateway 20128 FAIL"
  timeout 5 curl -s http://127.0.0.1:20129/health 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"proxy 20129 {d.get('combo')} uptime={d.get('uptime_sec')}s\")" 2>&1 || echo "proxy 20129 FAIL"
  timeout 5 curl -s http://127.0.0.1:20133/health 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"proxy 20133 {d.get('combo')} uptime={d.get('uptime_sec')}s\")" 2>&1 || echo "proxy 20133 FAIL"
}

start() {
  echo "== Start Gateway + Proxy =="
  if [ ! -f "$COMPOSE" ]; then echo "falta $COMPOSE"; exit 1; fi
  echo "[1/3] docker compose up gateway..."
  docker compose -f "$COMPOSE" up -d omniroute 2>&1 | tail -5
  echo "esperando healthcheck 20128..."
  for i in $(seq 1 30); do
    if timeout 8 curl -s http://127.0.0.1:20128/api/combos -H "Authorization: Bearer $API_KEY" >/dev/null 2>&1; then
      echo "gateway listo (intento $i)"
      break
    fi
    if [ "$i" = 30 ]; then echo "gateway no respondió en 30 intentos"; docker logs omniroute --tail 20 2>&1 | tail -20; exit 1; fi
    sleep 2
  done
  if [ -f "$SEED" ] && [ -f "$PROJECT_DIR/scripts/bootstrap.sh" ]; then
    echo "[2/3] bootstrap combos desde $SEED..."
    BASE="http://127.0.0.1:20128" API_KEY="$API_KEY" "$PROJECT_DIR/scripts/bootstrap.sh" "$SEED" 2>&1 | tail -10 || echo "bootstrap warn (puede ser que combos ya existan)"
  else
    echo "[2/3] skip bootstrap (no seed/bootstrap)"
  fi
  echo "[3/3] levantando proxy..."
  pkill -f "proxy/server.py" 2>/dev/null || true
  pkill -f "omniroute-unified-proxy" 2>/dev/null || true
  sleep 1
  nohup python3 "$PROXY_PY" > "$LOG" 2>&1 &
  sleep 3
  timeout 5 curl -s http://127.0.0.1:20129/health >/dev/null 2>&1 && echo "proxy 20129 OK" || { echo "proxy 20129 FAIL"; cat "$LOG" | tail -20; exit 1; }
  timeout 5 curl -s http://127.0.0.1:20133/health >/dev/null 2>&1 && echo "proxy 20133 OK" || { echo "proxy 20133 FAIL"; cat "$LOG" | tail -20; exit 1; }
  echo "== Todo OK =="
  echo "20128 gateway | 20129 combo-a | 20133 combo-b"
  echo "logs: tail -f $LOG"
  echo "status: $0 --status"
}

case "${1:-}" in
  --stop) stop ;;
  --status) status ;;
  --restart) stop; sleep 2; start ;;
  *) start ;;
esac
