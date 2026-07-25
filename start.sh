#!/bin/bash
# Launch the design-loop orchestrator, wait for its server, and push provider config.
#
# Usage: ./start.sh "<design intent>" [extra orchestrator flags...]
#   e.g. ./start.sh "a dashboard for visualizing llama.cpp phases" --max-iterations 8
set -u
# Run from the script's own directory (portable; no GNU readlink dependency).
cd "$(dirname "$0")" || exit 1

if [ "$#" -lt 1 ] || [ -z "$1" ]; then
    echo "Usage: $0 \"<design intent>\" [extra orchestrator flags...]" >&2
    exit 2
fi
INTENT="$1"
shift

if [ ! -f providers.json ]; then
    echo "providers.json not found in $(pwd) — create it before running." >&2
    exit 1
fi

nohup python3 orchestrator.py "$INTENT" --fresh "$@" > app.log 2>&1 &
ORCH_PID=$!
echo "Orchestrator PID: $ORCH_PID"

# Wait for server, find port
for i in $(seq 1 30); do
    for p in $(seq 8000 8010); do
        if curl -s -m 1 "http://localhost:$p/status" >/dev/null 2>&1; then
            echo "Server found on port $p"
            # Send provider config
            curl -s -X POST "http://localhost:$p/save-providers" \
                -H "Content-Type: application/json" \
                -d @providers.json
            echo ""
            echo "Provider config sent"
            echo "VIEW: http://localhost:$p/design_shell.html"
            exit 0
        fi
    done
    sleep 1
done
echo "Server not found after 30s"
cat app.log
