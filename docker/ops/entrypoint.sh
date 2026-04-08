#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -euo pipefail

RECONCILE_INTERVAL_SECONDS="${SENSOS_WG_RECONCILE_INTERVAL_SECONDS:-10}"

while true; do
    python3 /reconcile.py || true
    sleep "${RECONCILE_INTERVAL_SECONDS}"
done
