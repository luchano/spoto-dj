#!/usr/bin/env bash
#
# dev.sh — start/stop the Spoto DJ development server (uvicorn + --reload).
#
#   ./dev.sh start     start in the background, print the clickable URL
#   ./dev.sh stop      stop it
#   ./dev.sh restart   stop then start
#   ./dev.sh status    show whether it's running
#
# Host/port are overridable:  PORT=8001 ./dev.sh start
#
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
URL="http://${HOST}:${PORT}"
PIDFILE=".dev-server.pid"
LOGFILE=".dev-server.log"
UVICORN=".venv/bin/uvicorn"

is_running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

start() {
  if is_running; then
    echo "Already running (pid $(cat "$PIDFILE"))."
    echo "  →  $URL"
    return 0
  fi
  rm -f "$PIDFILE"
  if [[ ! -x "$UVICORN" ]]; then
    echo "uvicorn not found at $UVICORN — set up the venv first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
  if lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
    echo "Port ${PORT} is already in use by another process:" >&2
    lsof -i "tcp:${PORT}" >&2
    echo "Stop it, or run with a different port:  PORT=8001 ./dev.sh start" >&2
    exit 1
  fi

  echo "Starting Spoto DJ dev server on ${URL} …"
  nohup "$UVICORN" main:app --reload --host "$HOST" --port "$PORT" > "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"

  # Wait until it answers (or dies), up to ~20s.
  for _ in $(seq 1 40); do
    if curl -sf -o /dev/null "$URL" 2>/dev/null; then
      echo ""
      echo "  Ready →  $URL"
      echo ""
      echo "  logs:  tail -f $LOGFILE   ·   stop:  ./dev.sh stop"
      return 0
    fi
    if ! is_running; then
      echo "Server exited during startup. Last log lines:" >&2
      tail -n 20 "$LOGFILE" >&2
      rm -f "$PIDFILE"
      exit 1
    fi
    sleep 0.5
  done
  echo "Started (pid $(cat "$PIDFILE")) but not responding yet — check $LOGFILE" >&2
  echo "  →  $URL"
}

stop() {
  local stopped=0
  if is_running; then
    local pid; pid="$(cat "$PIDFILE")"
    echo "Stopping (pid ${pid}) …"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    kill -9 "$pid" 2>/dev/null || true
    stopped=1
  fi
  # Fallback: reap any lingering --reload child still holding the port.
  local strays; strays="$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)"
  if [[ -n "$strays" ]]; then
    kill $strays 2>/dev/null || true
    stopped=1
  fi
  rm -f "$PIDFILE"
  [[ "$stopped" -eq 1 ]] && echo "Stopped." || echo "Not running."
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)
    if is_running; then echo "Running (pid $(cat "$PIDFILE"))  →  $URL";
    else echo "Not running."; fi ;;
  *) echo "Usage: ./dev.sh {start|stop|restart|status}" >&2; exit 1 ;;
esac
