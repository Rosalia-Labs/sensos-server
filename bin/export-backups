#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${SENSOS_BACKUP_DIR:-${REPO_ROOT}/backups}"
REMOTE_PATH="${SENSOS_BACKUP_REMOTE:-}"
TRANSFER_MODE="copy"

log() {
    printf '[export-backups] %s\n' "$*"
}

die() {
    printf '[export-backups] ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Export backup artifacts from ${BACKUP_DIR} using rclone.

Options:
  --remote PATH     rclone destination such as 'box:sensos-server-backups'
  --move            Move backup files to the remote after successful transfer
  --copy            Copy backup files to the remote and keep local files (default)
  --help            Show this help and exit

Environment:
  SENSOS_BACKUP_DIR
  SENSOS_BACKUP_REMOTE
EOF
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --remote)
                [[ $# -ge 2 ]] || die "--remote requires a value"
                REMOTE_PATH="$2"
                shift 2
                ;;
            --move)
                TRANSFER_MODE="move"
                shift
                ;;
            --copy)
                TRANSFER_MODE="copy"
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                die "unknown option: $1"
                ;;
        esac
    done
}

ensure_backup_dir() {
    [[ -d "${BACKUP_DIR}" ]] || die "backup directory not found: ${BACKUP_DIR}"
}

ensure_remote() {
    [[ -n "${REMOTE_PATH}" ]] || die "no remote configured; pass --remote or set SENSOS_BACKUP_REMOTE"
}

ensure_backup_files() {
    if ! find "${BACKUP_DIR}" -maxdepth 1 -type f \( -name 'db_backup_*.gz' -o -name 'wg_*.tgz' \) | grep -q .; then
        die "no backup artifacts found in ${BACKUP_DIR}"
    fi
}

main() {
    parse_args "$@"
    require_command rclone
    ensure_backup_dir
    ensure_remote
    ensure_backup_files

    log "${TRANSFER_MODE}ing backups from ${BACKUP_DIR} to ${REMOTE_PATH}"
    rclone "${TRANSFER_MODE}" \
        --include 'db_backup_*.gz' \
        --include 'wg_*.tgz' \
        "${BACKUP_DIR}" \
        "${REMOTE_PATH}"
    log "backup export complete"
}

main "$@"
