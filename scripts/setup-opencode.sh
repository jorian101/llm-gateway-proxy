#!/usr/bin/env bash
# setup-opencode.sh - muestra snippet para opencode.json sin tocar nada por defecto
# Uso: ./scripts/setup-opencode.sh [--apply]
set -e
API_KEY="${GATEWAY_API_KEY:-<GATEWAY_API_KEY>}"
SNIPPET=$(cat << JSON
    "gateway": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Gateway",
      "options": { "baseURL": "http://127.0.0.1:20128/v1", "apiKey": "$API_KEY" }
    },
    "gateway-combo-a": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Gateway Combo A",
      "options": { "baseURL": "http://127.0.0.1:20129/v1", "apiKey": "$API_KEY" },
      "models": { "auto": { "name": "Auto (combo-a)" } }
    },
    "gateway-combo-b": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Gateway Combo B",
      "options": { "baseURL": "http://127.0.0.1:20133/v1", "apiKey": "$API_KEY" },
      "models": { "auto": { "name": "Auto (combo-b)" } }
    }
JSON
)
echo "Snippet para ~/.config/opencode/opencode.json -> provider:"
echo "$SNIPPET"
if [ "${1:-}" = "--apply" ]; then
  echo "Apply no implementado: copia manualmente el snippet a opencode.json provider"
fi
