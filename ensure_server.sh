#!/usr/bin/env bash
# ensure_server.sh — arranque idempotente de opencode-quota-dashboard (puerto 8099).
# Lo invoca el watchdog del host (apps-watchdog.timer) vía docker exec.
set -u

PORT="${PORT:-8099}"
PROJ="${PROJ:-$(cd "$(dirname "$0")" && pwd)}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/quota-dashboard-runtime}"
LOG_FILE="${LOG_FILE:-${RUNTIME_DIR}/server.log}"

mkdir -p "$RUNTIME_DIR"

# ¿El puerto está servido por un proceso de ESTE proyecto?
_is_serving() {
  local pid
  pid=$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)
  [ -z "$pid" ] && return 1
  [ "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" = "$PROJ" ]
}

if _is_serving; then
  exit 0
fi

# Puerto ocupado por un proceso ajeno: no lo tocamos, solo avisamos.
foreign=$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)
if [ -n "$foreign" ] && [ "$(readlink "/proc/$foreign/cwd" 2>/dev/null || true)" != "$PROJ" ]; then
  echo "[$(date -Iseconds)] ERROR: puerto $PORT ocupado por PID $foreign (cwd=$(readlink /proc/$foreign/cwd 2>/dev/null)) — no es este proyecto"
  exit 1
fi

# Mata únicamente serve.py cuyo cwd sea este proyecto.
for pid in $(pgrep -f "serve\.py" 2>/dev/null || true); do
  [ "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" = "$PROJ" ] && kill "$pid" 2>/dev/null && sleep 1
done

cd "$PROJ" || exit 1
nohup python3 serve.py --bind 0.0.0.0 --port "$PORT" >>"$LOG_FILE" 2>&1 &
sleep 3

if _is_serving; then
  echo "[$(date -Iseconds)] QuotaDash disponible en $PORT"
  exit 0
fi

echo "[$(date -Iseconds)] ERROR: QuotaDash no arrancó en $PORT"
tail -20 "$LOG_FILE" >&2 || true
exit 1
