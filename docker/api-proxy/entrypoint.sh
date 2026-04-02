#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -euo pipefail

RECONCILE_INTERVAL_SECONDS="${SENSOS_WG_RECONCILE_INTERVAL_SECONDS:-10}"

python3 /reconcile.py || true

cleanup() {
    kill "$loop_pid" 2>/dev/null || true
    wait "$loop_pid" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

while true; do
    sleep "$RECONCILE_INTERVAL_SECONDS"
    python3 /reconcile.py || true
done &
loop_pid=$!

exec nginx -g "daemon off;"
