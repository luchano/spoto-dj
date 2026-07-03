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

# Port resolution: env override → PORT in .env → port inside SPOTIFY_REDIRECT_URI
# in .env → 8000. The Spotify OAuth redirect URI is registered EXACTLY in the
# Spotify developer dashboard, so the server port and the redirect URI must
# always move together — deriving both from .env keeps them in sync.
_env_port=""
if [[ -z "${PORT:-}" && -f .env ]]; then
  _env_port="$(grep -E '^PORT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]' || true)"
  if [[ -z "$_env_port" ]]; then
    _env_port="$(grep -E '^SPOTIFY_REDIRECT_URI=' .env | tail -1 | grep -oE ':[0-9]+/' | tr -d ':/' || true)"
  fi
fi
PORT="${PORT:-${_env_port:-8000}}"
URL="http://${HOST}:${PORT}"
PIDFILE=".dev-server.pid"
LOGFILE=".dev-server.log"
UVICORN=".venv/bin/uvicorn"

# Warn if the redirect URI in .env points at a DIFFERENT port — Spotify login
# would redirect the browser to a dead port after auth.
if [[ -f .env ]]; then
  _redir_port="$(grep -E '^SPOTIFY_REDIRECT_URI=' .env | tail -1 | grep -oE ':[0-9]+/' | tr -d ':/' || true)"
  if [[ -n "$_redir_port" && "$_redir_port" != "$PORT" ]]; then
    echo "WARNING: server port ${PORT} != SPOTIFY_REDIRECT_URI port ${_redir_port} (.env)" >&2
    echo "         Spotify login will break — align PORT and SPOTIFY_REDIRECT_URI." >&2
  fi
fi

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
  # A just-stopped server can hold the port for a moment while it dies
  # (restart race) — wait briefly for it to free before declaring conflict.
  if lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
    for _ in $(seq 1 16); do
      lsof -ti "tcp:${PORT}" >/dev/null 2>&1 || break
      sleep 0.25
    done
  fi
  if lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
    echo "Port ${PORT} is already in use by another process:" >&2
    lsof -i "tcp:${PORT}" >&2
    echo "Stop it, or run with a different port:  PORT=8001 ./dev.sh start" >&2
    exit 1
  fi

  # No --reload: it spawns a worker child that can orphan (reparent to launchd)
  # and keep running the analysis after the parent is killed. A single process
  # means stop is clean. Editing code? re-apply with ./dev.sh restart.
  echo "Starting Spoto DJ dev server on ${URL} …"
  nohup "$UVICORN" main:app --host "$HOST" --port "$PORT" > "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"

  # Wait until it answers (or dies), up to ~20s.
  for _ in $(seq 1 40); do
    if curl -sf -o /dev/null "$URL" 2>/dev/null; then
      echo ""
      echo "  Ready →  $URL"
      echo ""
      echo "  logs:  tail -f $LOGFILE   ·   stop:  ./dev.sh stop"
      command -v open >/dev/null 2>&1 && open "$URL"   # pop the web UI (macOS)
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
  # uvicorn --reload spawns a worker child that can outlive its parent (it gets
  # reparented to launchd and keeps running the analysis + a zotify download).
  # Reap any such orphaned worker for THIS app, plus a lingering download.
  if pkill -f "uvicorn main:app" 2>/dev/null; then stopped=1; fi
  pkill -f "venv-dl/bin/zotify" 2>/dev/null || true
  # Reap anything still on the port.
  local strays; strays="$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)"
  if [[ -n "$strays" ]]; then
    kill $strays 2>/dev/null || true
    stopped=1
  fi
  rm -f "$PIDFILE"
  [[ "$stopped" -eq 1 ]] && echo "Stopped." || echo "Not running."
}

queue_status() {
  local log="server.log"

  # How many tracks are analyzed (persisted in the cache) and downloaded.
  local analyzed="?"
  if [[ -f .audio_cache.json ]]; then
    analyzed="$(.venv/bin/python -c 'import json;print(len(json.load(open(".audio_cache.json"))))' 2>/dev/null || echo '?')"
  fi
  local oggs; oggs="$(find .audio_files -maxdepth 1 -name '*.ogg' 2>/dev/null | wc -l | tr -d ' ')"
  # Library total ≈ the largest "…: N new tracks" ever logged (the first full
  # run, before the cache filled up). Max is robust to later small re-runs.
  local total=""
  [[ -f "$log" ]] && total="$(grep -oE ': [0-9]+ new tracks' "$log" 2>/dev/null | grep -oE '[0-9]+' | sort -n | tail -1 || true)"

  echo ""
  echo "Analysis queue:"
  echo "  analyzed:     ${analyzed}${total:+ / ${total}} tracks   (${oggs} audio files on disk)"

  # What zotify is downloading right now, and for how long. Read the pacing
  # straight from the live process args — that's the truth, not the config.
  local zpid; zpid="$(pgrep -f 'venv-dl/bin/zotify' 2>/dev/null | head -1 || true)"
  if [[ -n "$zpid" ]]; then
    local info et tid rate pace
    info="$(ps -o etime=,command= -p "$zpid" 2>/dev/null || true)"
    et="$(echo "$info" | awk '{print $1}')"
    tid="$(echo "$info" | grep -oE 'track/[A-Za-z0-9]+' | head -1 | cut -d/ -f2 || true)"
    rate="$(echo "$info" | grep -oE 'download-rate-limiter [0-9.]+' | awk '{print $2}' || true)"
    case "$rate" in
      ""|0|0.0) pace="full speed" ;;
      1|1.0)    pace="real-time paced (1x), ~track length" ;;
      *)        pace="paced at ${rate}x of track length" ;;
    esac
    echo "  downloading:  ${tid:-?}  (elapsed ${et:-?})  — ${pace}"
  else
    echo "  downloading:  idle (no download in progress)"
  fi

  # Last track that finished analysis.
  if [[ -f "$log" ]]; then
    local last
    last="$(grep -E "Local analysis OK for" "$log" 2>/dev/null | tail -1 || true)"
    if [[ -n "$last" ]]; then
      local ts title bpm
      ts="$(echo "$last" | awk '{print $2}')"
      title="$(echo "$last" | sed -E "s/.*OK for '([^']*)'.*/\1/")"
      bpm="$(echo "$last" | grep -oE 'bpm=[0-9.]+' | head -1 || true)"
      echo "  last done:    '${title}'  ${bpm}  [${ts}]"
    fi
  fi
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)
    if is_running; then
      echo "Running (pid $(cat "$PIDFILE"))  →  $URL"
      queue_status
    else
      echo "Not running."
      queue_status   # cache/download counts are still useful when stopped
    fi ;;
  *) echo "Usage: ./dev.sh {start|stop|restart|status}" >&2; exit 1 ;;
esac
