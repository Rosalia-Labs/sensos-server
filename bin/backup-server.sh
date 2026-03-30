#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${SENSOS_BACKUP_DIR:-${REPO_ROOT}/backups}"
DEFAULT_POST_HOOK="${REPO_ROOT}/local/hooks/post-backup.sh"
EXPORT_AFTER_BACKUP=0
EXPORT_ARGS=()
POST_HOOK="${SENSOS_BACKUP_POST_HOOK:-}"

log() {
    printf '[backup-server] %s\n' "$*"
}

die() {
    printf '[backup-server] ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Create the standard server backups. Optionally export them with rclone and/or
run a user-supplied post-backup hook.

Options:
  --export          Export backups after creation using bin/export-backups.sh
  --remote PATH     rclone destination such as 'box:sensos-server-backups'
  --move            Move backup files during export instead of copying
  --copy            Copy backup files during export (default)
  --post-hook PATH  Run a user-supplied script after backups complete
  --help            Show this help and exit

Hook contract:
  The post-hook receives the backup directory as argv[1], followed by the newly
  created backup file paths as argv[2+].

Default local hook path:
  ${DEFAULT_POST_HOOK}
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --export)
                EXPORT_AFTER_BACKUP=1
                shift
                ;;
            --remote)
                [[ $# -ge 2 ]] || die "--remote requires a value"
                EXPORT_AFTER_BACKUP=1
                EXPORT_ARGS+=("$1" "$2")
                shift 2
                ;;
            --move|--copy)
                EXPORT_AFTER_BACKUP=1
                EXPORT_ARGS+=("$1")
                shift
                ;;
            --post-hook)
                [[ $# -ge 2 ]] || die "--post-hook requires a value"
                POST_HOOK="$2"
                shift 2
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

resolve_post_hook() {
    if [[ -n "${POST_HOOK}" ]]; then
        return
    fi

    if [[ -x "${DEFAULT_POST_HOOK}" ]]; then
        POST_HOOK="${DEFAULT_POST_HOOK}"
    fi
}

collect_new_backups() {
    find "${BACKUP_DIR}" -maxdepth 1 -type f \( -name 'db_backup_*.gz' -o -name 'wg_*.tgz' \) -print | sort
}

main() {
    local before_list
    local after_list
    local backup_file
    local -a new_files=()

    parse_args "$@"
    resolve_post_hook
    before_list="$(collect_new_backups)"

    log "running database backup"
    "${SCRIPT_DIR}/backup-database.sh"

    log "running wireguard backup"
    "${SCRIPT_DIR}/backup-wireguard.sh"

    if [[ "${EXPORT_AFTER_BACKUP}" == "1" ]]; then
        log "exporting backups"
        "${SCRIPT_DIR}/export-backups.sh" "${EXPORT_ARGS[@]}"
    fi

    after_list="$(collect_new_backups)"
    while IFS= read -r backup_file; do
        [[ -n "${backup_file}" ]] || continue
        if ! grep -Fqx "${backup_file}" <<< "${before_list}"; then
            new_files+=("${backup_file}")
        fi
    done <<< "${after_list}"

    if [[ -n "${POST_HOOK}" ]]; then
        [[ -x "${POST_HOOK}" ]] || die "post-hook is not executable: ${POST_HOOK}"
        log "running post-hook ${POST_HOOK}"
        "${POST_HOOK}" "${BACKUP_DIR}" "${new_files[@]}"
    fi

    log "backup workflow complete"
}

main "$@"
