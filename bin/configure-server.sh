#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -euo pipefail

WORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../docker" && pwd)"
cd "$WORK_DIR"

echo "Working directory: $(pwd)"

# Define default values
DEFAULT_DB_PORT=5432
DEFAULT_API_PORT=8765
DEFAULT_WG_PORT=51820
DEFAULT_WG_SERVER_IP="127.0.0.1"
DEFAULT_POSTGRES_PASSWORD="sensos"
DEFAULT_API_PASSWORD="sensos"
DEFAULT_EXPOSE_CONTAINERS="false"

# Print help message
print_help() {
    cat <<EOF
Usage: $0 [options]

Options:
  --db-port PORT           Set database port (default: $DEFAULT_DB_PORT)
  --api-port PORT          Set API port (default: $DEFAULT_API_PORT)
  --wg-server-ip IP        Set WireGuard IP (default: $DEFAULT_WG_SERVER_IP)
  --wg-port PORT           Set WireGuard port (default: $DEFAULT_WG_PORT)
  --postgres-password PWD  Set PostgreSQL password (default: $DEFAULT_POSTGRES_PASSWORD)
  --api-password PWD       Set API password (default: $DEFAULT_API_PASSWORD)
  --expose-containers      Add containers to WireGuard (default: $DEFAULT_EXPOSE_CONTAINERS)
  -h, --help               Show this help message
EOF
    exit 0
}

# Allow command-line overrides
while [[ $# -gt 0 ]]; do
    case "$1" in
    --db-port)
        DB_PORT="$2"
        shift 2
        ;;
    --api-port)
        API_PORT="$2"
        shift 2
        ;;
    --wg-port)
        WG_PORT="$2"
        shift 2
        ;;
    --wg-server-ip)
        WG_SERVER_IP="$2"
        shift 2
        ;;
    --postgres-password)
        POSTGRES_PASSWORD="$2"
        shift 2
        ;;
    --api-password)
        API_PASSWORD="$2"
        shift 2
        ;;
    --expose-containers)
        EXPOSE_CONTAINERS="true"
        shift
        ;;
    -h | --help)
        print_help
        ;;
    *)
        echo "Unknown option: $1" >&2
        print_help
        ;;
    esac
done

# Set defaults if variables not provided
DB_PORT=${DB_PORT:-$DEFAULT_DB_PORT}
API_PORT=${API_PORT:-$DEFAULT_API_PORT}
WG_PORT=${WG_PORT:-$DEFAULT_WG_PORT}
WG_SERVER_IP=${WG_SERVER_IP:-$DEFAULT_WG_SERVER_IP}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-$DEFAULT_POSTGRES_PASSWORD}
API_PASSWORD=${API_PASSWORD:-$DEFAULT_API_PASSWORD}
EXPOSE_CONTAINERS=${EXPOSE_CONTAINERS:-$DEFAULT_EXPOSE_CONTAINERS}

# Backup existing .env if it exists
if [ -f .env ]; then
    mv .env .env.bak
    chmod 600 .env.bak
    echo "✅ Current environment configuration backed up to .env.bak."
fi

# Write configuration to .env
cat >.env <<EOF
DB_PORT=$DB_PORT
API_PORT=$API_PORT
WG_PORT=$WG_PORT
WG_SERVER_IP=$WG_SERVER_IP
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
API_PASSWORD=$API_PASSWORD
EXPOSE_CONTAINERS=$EXPOSE_CONTAINERS
EOF

chmod 600 .env
echo "✅ Environment configuration written to .env."
echo "✅ Setup completed successfully."
