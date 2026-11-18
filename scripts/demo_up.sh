#!/usr/bin/env bash
# Start the application side of the demo on one Linux host (WSL2 Ubuntu or similar).
# Prerequisites (see docs/SETUP.md): Mobius4 IN-CSE on :7599 and the patched tinyIoT MN-CSE on :3001
# are already running and the MN is registered to the IN.
#
#   scripts/demo_up.sh            # fresh resource trees, start model store + edge AE + dashboard, deploy model
#   scripts/demo_up.sh --keep     # keep existing resources (just restart the AEs)
#   MEC_LOC_BASE=<.../mep1/location/v2> MEC_VIS_BASE=<.../mep1/vis/v2> scripts/demo_up.sh   # + warning AE (M2)
#   scripts/demo_down.sh          # stop the AEs
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-python3}"
LOG="${LOG_DIR:-logs}"
mkdir -p "$LOG"

wait_http() {  # url, name, pid - the port must answer AND the process we just started must still be alive
  for _ in $(seq 1 40); do
    kill -0 "$3" 2>/dev/null || { echo "!! $2 exited right after start - see $LOG/*.log"; exit 1; }
    curl -s -o /dev/null "$1" && return 0; sleep 0.25
  done
  echo "!! $2 did not come up ($1)"; exit 1
}

ports_free() {  # refuse to start over a stale AE that still holds a port (its old code would keep serving)
  local busy=""
  for p in 8088 9800 8080 8081 9810; do
    if timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/$p" 2>/dev/null; then busy="$busy $p"; fi
  done
  if [[ -n "$busy" ]]; then
    echo "!! port(s)$busy still in use - an AE from an earlier run is probably still running:"
    if command -v ss >/dev/null; then ss -ltnp | grep -E ":($(echo $busy | tr ' ' '|'))\b" | sed 's/^/   /'; fi
    pgrep -af "cloud/dashboard_ae.py|edge/edge_ai_ae.py|cloud/model_store_server.py|edge/warning_ae.py" | sed 's/^/   /' || true
    echo "   stop them:  pkill -f 'cloud/dashboard_ae.py|edge/edge_ai_ae.py|cloud/model_store_server.py|edge/warning_ae.py'   then run this again"
    exit 1
  fi
}

check_cse() {  # name, url, origin - 5 s limit, so a frozen CSE fails fast instead of hanging here
  local code
  code=$(curl -s -m 5 -o /dev/null -w "%{http_code}" -H "X-M2M-Origin: $3" -H "X-M2M-RI: up" -H "X-M2M-RVI: 3" \
              -H "Accept: application/json" "$2") || true
  if [[ "$code" == "200" ]]; then echo "   $1 ok"; return 0; fi
  if [[ "$code" == "000" ]]; then
    echo "!! $1 did not answer within 5 s ($2)."
    echo "   Not running, or its terminal is paused: a text selection in that window (press Esc there),"
    echo "   or Ctrl+Z (type fg there). If neither, stop it with Ctrl+C and start it again."
  else
    echo "!! $1 answered HTTP $code ($2)"
  fi
  exit 1
}

echo "== checking CSEs"
check_cse "Mobius4 IN-CSE :7599" http://127.0.0.1:7599/Mobius CTest
check_cse "tinyIoT MN-CSE :3001" http://127.0.0.1:3001/EdgeMN CAdmin

LOG_DIR="$LOG" bash scripts/demo_down.sh >/dev/null 2>&1 || true
ports_free

if [[ "${1:-}" != "--keep" ]]; then
  echo "== resource trees (reset)"
  $PY scripts/setup_resources.py --reset | tail -1
fi

echo "== model store :8088";  nohup $PY -u cloud/model_store_server.py --port 8088 > "$LOG/store.log" 2>&1 &
echo $! > "$LOG/store.pid"
wait_http http://127.0.0.1:8088/ "model store" "$(cat "$LOG/store.pid")"
echo "== edge AE :9800";      nohup $PY -u edge/edge_ai_ae.py > "$LOG/edge.log" 2>&1 &
echo $! > "$LOG/edge.pid"
wait_http http://127.0.0.1:9800/stats "edge AE" "$(cat "$LOG/edge.pid")"
sleep 1
echo "== dashboard :8080";    nohup $PY -u cloud/dashboard_ae.py > "$LOG/dashboard.log" 2>&1 &
echo $! > "$LOG/dashboard.pid"
wait_http http://127.0.0.1:8080/api/state "dashboard" "$(cat "$LOG/dashboard.pid")"

if [[ -n "${MEC_LOC_BASE:-}" && -n "${MEC_VIS_BASE:-}" ]]; then
  echo "== MEC warning AE :9810 (MEC 013 + 030)"
  nohup $PY -u edge/warning_ae.py > "$LOG/warning.log" 2>&1 &
  echo $! > "$LOG/warning.pid"
  wait_http http://127.0.0.1:9810/stats "warning AE" "$(cat "$LOG/warning.pid")"
  sleep 1; grep -E "subscription|refused|failed" "$LOG/warning.log" | tail -n 2 | sed 's/^/   /'
else
  echo "== MEC warning AE skipped (set MEC_LOC_BASE and MEC_VIS_BASE to enable)"
fi

if [[ "${1:-}" != "--keep" ]]; then
  echo "== register + deploy model (cloud -> edge)"
  $PY cloud/publish_model.py | sed 's/^/   /'
fi
sleep 2
echo "== edge AE log"; tail -n 8 "$LOG/edge.log" | sed 's/^/   /'
echo
echo "dashboard: http://127.0.0.1:8080/"
echo "next:      $PY sim/virtual_node.py --pace 0.2     then  $PY scripts/e2e_check.py"
