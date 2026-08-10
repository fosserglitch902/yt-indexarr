#!/bin/sh
# Run the Torznab indexer (9117) and the qBittorrent download spoofer (9177)
# in the same container, and exit if either process dies so the container
# orchestrator can restart it.
set -e

python3 /app/indexer.py &
INDEXER_PID=$!

python3 /app/qbt.py &
QBT_PID=$!

trap 'kill "$INDEXER_PID" "$QBT_PID" 2>/dev/null || true' INT TERM

while kill -0 "$INDEXER_PID" 2>/dev/null && kill -0 "$QBT_PID" 2>/dev/null; do
    sleep 2
done

kill "$INDEXER_PID" "$QBT_PID" 2>/dev/null || true
wait "$INDEXER_PID" "$QBT_PID" 2>/dev/null || true
exit 0
