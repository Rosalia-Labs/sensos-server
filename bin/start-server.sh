#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="$REPO_ROOT/docker"

cd "$WORK_DIR"
echo "Working directory: $(pwd)"

# Default options
REBUILD=false
NO_CACHE=false
DETACH=true
RESTART=false

# Load environment variables from .env if available
ENV_FILE="$WORK_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
else
    echo "❌ .env file not found at $ENV_FILE. Exiting." >&2
    exit 1
fi

# Load versioning information from VERSION in the repo root.
VERSION_FILE="$REPO_ROOT/VERSION"
if [ -f "$VERSION_FILE" ]; then
    VERSION_TEXT="$(head -n 1 "$VERSION_FILE" | tr -d '[:space:]')"
    if [[ "$VERSION_TEXT" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)([-.]([A-Za-z0-9.-]+))?$ ]]; then
        VERSION_MAJOR="${BASH_REMATCH[1]}"
        VERSION_MINOR="${BASH_REMATCH[2]}"
        VERSION_PATCH="${BASH_REMATCH[3]}"
        VERSION_SUFFIX="${BASH_REMATCH[5]:-}"
    else
        echo "⚠️ VERSION file at $VERSION_FILE is not valid semver. Proceeding without version overrides." >&2
        VERSION_MAJOR=""
        VERSION_MINOR=""
        VERSION_PATCH=""
        VERSION_SUFFIX=""
    fi
else
    echo "⚠️ VERSION file not found at $VERSION_FILE. Proceeding without version overrides." >&2
    VERSION_MAJOR=""
    VERSION_MINOR=""
    VERSION_PATCH=""
    VERSION_SUFFIX=""
fi

# Get Git metadata dynamically
GIT_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
GIT_TAG=$(git describe --tags --always 2>/dev/null || echo "unknown")
GIT_DIRTY=$(test -n "$(git status --porcelain 2>/dev/null)" && echo "true" || echo "false")

# Ensure defaults for any empty version fields
VERSION_MAJOR="${VERSION_MAJOR:-unknown}"
VERSION_MINOR="${VERSION_MINOR:-unknown}"
VERSION_PATCH="${VERSION_PATCH:-unknown}"
VERSION_SUFFIX="${VERSION_SUFFIX:-}"
GIT_COMMIT="${GIT_COMMIT:-unknown}"
GIT_BRANCH="${GIT_BRANCH:-unknown}"
GIT_TAG="${GIT_TAG:-unknown}"
GIT_DIRTY="${GIT_DIRTY:-false}"

export VERSION_MAJOR VERSION_MINOR VERSION_PATCH VERSION_SUFFIX
export GIT_COMMIT GIT_BRANCH GIT_TAG GIT_DIRTY

# Parse CLI arguments
while [ $# -gt 0 ]; do
    case "$1" in
    --rebuild-containers) REBUILD=true ;;
    --no-cache) NO_CACHE=true ;;
    --no-detach) DETACH=false ;;
    --restart) RESTART=true ;;
    --help)
        echo "Usage: $0 [--rebuild-containers] [--no-cache] [--no-detach] [--restart]"
        exit 0
        ;;
    *)
        echo "Unknown option: $1" >&2
        exit 1
        ;;
    esac
    shift
done

# If containers are already running, handle --restart or exit
if docker ps --filter "name=sensos-" --format '{{.Names}}' | grep -q .; then
    if [ "$RESTART" = true ]; then
        echo "ℹ️  Restart option enabled. Stopping running SensOS containers..."
        "$SCRIPT_DIR/stop-server.sh"

        echo "⏳ Waiting for containers to stop..."
        TIMEOUT=60
        ELAPSED=0
        while docker ps --filter "name=sensos-" --format '{{.Names}}' | grep -q .; do
            if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
                echo "❌ Timeout reached while waiting for containers to stop."
                exit 1
            fi
            sleep 5
            ((ELAPSED += 5))
        done
    else
        echo "❌ SensOS containers are already running. Exiting." >&2
        echo "ℹ️  Use ./stop-server.sh or run with --restart." >&2
        exit 1
    fi
fi

#  Build step (if requested)
if [ "$REBUILD" = true ]; then
    BUILD_CMD=(docker compose build)
    if [ "$NO_CACHE" = true ]; then
        BUILD_CMD+=(--no-cache)
    fi
    export COMPOSE_BAKE=true
    echo "🔨 Building containers: ${BUILD_CMD[*]}"
    "${BUILD_CMD[@]}"
fi

#  Up step
UP_CMD=(docker compose up)
if [ "$DETACH" = true ]; then
    UP_CMD+=(-d)
fi

echo "🚀 Executing command: ${UP_CMD[*]}"
echo "📌 Running software version: ${VERSION_MAJOR}.${VERSION_MINOR}.${VERSION_PATCH}-${VERSION_SUFFIX}"
echo "   Git commit=${GIT_COMMIT}, branch=${GIT_BRANCH}, tag=${GIT_TAG}, dirty=${GIT_DIRTY}"

exec "${UP_CMD[@]}"
