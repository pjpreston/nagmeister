#!/usr/bin/env bash
# Start and stop the Nag Meister web server.
#
#   ./nag.sh start        # start it and wait until it answers
#   ./nag.sh stop         # stop it
#   ./nag.sh status       # is it up, and where
#   ./nag.sh restart
#
# Override the defaults with environment variables:
#   PORT=9000 HOST=0.0.0.0 DB=/other/path.duckdb ./nag.sh start

set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
DB="${DB:-data/nagmeister.duckdb}"
PYTHON=".venv/bin/python"
PIDFILE=".nag.pid"
LOGFILE=".nag.log"
APP="rbd_web.py"

die() { echo "nag: $*" >&2; exit 1; }

# The PID of a running server, or nothing. Checks the process is actually ours
# rather than trusting the file -- PIDs get recycled and we do not want `stop`
# killing whatever inherited the number.
running_pid() {
    [ -f "$PIDFILE" ] || return 1
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$APP" || return 1
    echo "$pid"
}

start() {
    local pid
    if pid=$(running_pid); then
        echo "nag: already running (pid $pid) — http://$HOST:$PORT"
        return 0
    fi
    [ -f "$PIDFILE" ] && rm -f "$PIDFILE"   # stale

    [ -x "$PYTHON" ] || die "no virtualenv at $PYTHON — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    [ -f "$APP" ]    || die "$APP not found — are you on a branch that has it?"
    [ -f "$DB" ]     || die "no database at $DB — run: $PYTHON rbd_import.py"

    # The server opens the database read-only and DuckDB refuses a reader while
    # a writer holds the file, so say so now rather than after a failed start.
    if ! "$PYTHON" - "$DB" <<'PY' 2>/dev/null
import sys, duckdb
duckdb.connect(sys.argv[1], read_only=True).close()
PY
    then
        die "$DB is locked by another process — close any 'duckdb' CLI session or rbd_import.py run"
    fi

    echo "nag: starting on http://$HOST:$PORT (db $DB)"
    nohup "$PYTHON" "$APP" --db "$DB" --host "$HOST" --port "$PORT" >"$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"

    for _ in $(seq 1 40); do
        if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            rm -f "$PIDFILE"
            echo "nag: server exited during startup —" >&2
            tail -n 15 "$LOGFILE" >&2
            exit 1
        fi
        if curl -fsS -o /dev/null "http://$HOST:$PORT/api/stats" 2>/dev/null; then
            echo "nag: ready — http://$HOST:$PORT  (pid $(cat "$PIDFILE"), log $LOGFILE)"
            return 0
        fi
        sleep 0.25
    done

    echo "nag: started but not answering after 10s — check $LOGFILE" >&2
    tail -n 15 "$LOGFILE" >&2
    exit 1
}

stop() {
    local pid
    if ! pid=$(running_pid); then
        [ -f "$PIDFILE" ] && rm -f "$PIDFILE" && echo "nag: not running (removed stale $PIDFILE)" && return 0
        echo "nag: not running"
        return 0
    fi

    echo "nag: stopping (pid $pid)"
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 40); do
        kill -0 "$pid" 2>/dev/null || { rm -f "$PIDFILE"; echo "nag: stopped"; return 0; }
        sleep 0.25
    done

    echo "nag: did not exit after 10s, sending SIGKILL" >&2
    kill -9 "$pid" 2>/dev/null
    sleep 0.5
    rm -f "$PIDFILE"
    echo "nag: stopped (forced)"
}

status() {
    local pid
    if pid=$(running_pid); then
        echo "nag: running (pid $pid) — http://$HOST:$PORT"
        curl -fsS "http://$HOST:$PORT/api/stats" 2>/dev/null && echo
        return 0
    fi
    echo "nag: not running"
    return 1
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    status)  status ;;
    *)       echo "usage: $(basename "$0") {start|stop|restart|status}" >&2; exit 2 ;;
esac
