#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -euo pipefail

WG_SHARED_DIR="/wireguard_config"
WG_STATE_DIR="${WG_SHARED_DIR}/state"
RECONCILE_INTERVAL_SECONDS="${SENSOS_WG_RECONCILE_INTERVAL_SECONDS:-10}"

refresh_status() {
    python3 /reconcile.py >/dev/null 2>&1 || true
}

trap 'refresh_status' SIGUSR1

mkdir -p "${WG_STATE_DIR}"

while true; do
    python3 /reconcile.py || true
    sleep "${RECONCILE_INTERVAL_SECONDS}"
done
