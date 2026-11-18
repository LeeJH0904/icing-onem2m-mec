#!/usr/bin/env bash
# Stop the demo AEs started by demo_up.sh (the CSEs keep running). Uses the PID files demo_up.sh wrote.
cd "$(dirname "$0")/.."
LOG="${LOG_DIR:-logs}"
for name in warning store edge dashboard; do
  f="$LOG/$name.pid"
  if [[ -f "$f" ]]; then kill "$(cat "$f")" 2>/dev/null; rm -f "$f"; fi
done
sleep 0.5
echo "demo AEs stopped"
