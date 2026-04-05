# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import ipaddress
import logging
import os
import psycopg
import re
import socket
import time

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from psycopg import Cursor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
if not POSTGRES_PASSWORD:
    raise ValueError("POSTGRES_PASSWORD is not set. Exiting.")
DATABASE_URL = (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@sensos-database/postgres"
)

LEGACY_API_PASSWORD = os.getenv("API_PASSWORD")
ADMIN_API_PASSWORD = os.getenv("ADMIN_API_PASSWORD", LEGACY_API_PASSWORD or "")
CLIENT_API_PASSWORD = os.getenv("CLIENT_API_PASSWORD", LEGACY_API_PASSWORD or "")
if not ADMIN_API_PASSWORD:
    raise ValueError("ADMIN_API_PASSWORD or API_PASSWORD must be set. Exiting.")
if not CLIENT_API_PASSWORD:
    raise ValueError("CLIENT_API_PASSWORD or API_PASSWORD must be set. Exiting.")

VERSION_MAJOR = os.getenv("VERSION_MAJOR", "Unknown")
VERSION_MINOR = os.getenv("VERSION_MINOR", "Unknown")
VERSION_PATCH = os.getenv("VERSION_PATCH", "Unknown")
VERSION_SUFFIX = os.getenv("VERSION_SUFFIX", "")
GIT_COMMIT = os.getenv("GIT_COMMIT", "Unknown")
GIT_BRANCH = os.getenv("GIT_BRANCH", "Unknown")
GIT_TAG = os.getenv("GIT_TAG", "Unknown")
GIT_DIRTY = os.getenv("GIT_DIRTY", "false")

RUNTIME_COMPONENT_WIREGUARD = "sensos-wireguard"
RUNTIME_COMPONENT_API_PROXY = "sensos-api-proxy"
RUNTIME_ROLE_SERVER = "server"
RUNTIME_ROLE_API_PROXY = "api-proxy"
PUBLIC_WG_PORT_START = 51281
PUBLIC_WG_PORT_END = 51289


@dataclass(frozen=True, order=True)
class SchemaMigration:
    version: tuple[int, int, int, int, str]
    name: str
    apply: Callable[[Cursor], None]


SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:([.-])([A-Za-z0-9.-]+))?$")


def parse_version_key(version: str) -> tuple[int, int, int, int, str]:
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise ValueError(f"invalid version '{version}'")
    major, minor, patch, _, suffix = match.groups()
    return (
        int(major),
        int(minor),
        int(patch),
        1 if suffix is None else 0,
        suffix or "",
    )


def current_server_version() -> str:
    base = f"{VERSION_MAJOR}.{VERSION_MINOR}.{VERSION_PATCH}"
    return f"{base}-{VERSION_SUFFIX}" if VERSION_SUFFIX else base


def schema_migration_target_version() -> str:
    version = current_server_version()
    if SEMVER_RE.fullmatch(version):
        return version
    return render_version_key(SCHEMA_MIGRATIONS[-1].version)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.schema_ready = False
    logger.info("initializing database schema")
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                apply_schema_migrations(cur, schema_migration_target_version())
                update_version_history_table(cur)
        app.state.schema_ready = True
        logger.info("database schema initialized")
    except Exception as exc:
        logger.error("database initialization failed: %s", exc, exc_info=True)
    yield
    logger.info("shutting down")


security = HTTPBasic()


def authenticate_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.password != ADMIN_API_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials


def authenticate_client(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.password not in {CLIENT_API_PASSWORD, ADMIN_API_PASSWORD}:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials


def get_db(retries: int = 10, delay: int = 3):
    for attempt in range(retries):
        try:
            return psycopg.connect(DATABASE_URL, autocommit=True)
        except psycopg.OperationalError:
            if attempt == retries - 1:
                raise
            logger.info(
                "database not ready, retrying in %s seconds (attempt %s/%s)",
                delay,
                attempt + 1,
                retries,
            )
            time.sleep(delay)


def ensure_shared_extensions(cur):
    ensure_extension_in_public(cur, "pgcrypto")
    ensure_extension_in_public(cur, "postgis")


def ensure_extension_in_public(cur, extension_name: str):
    cur.execute(
        """
        SELECT n.nspname
        FROM pg_extension e
        JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = %s;
        """,
        (extension_name,),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension_name}" WITH SCHEMA public;')
        return
    if row[0] != "public":
        cur.execute(f'ALTER EXTENSION "{extension_name}" SET SCHEMA public;')


def ensure_schema_migrations_table(cur):
    cur.execute("CREATE SCHEMA IF NOT EXISTS sensos;")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.schema_migrations (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def create_initial_schema(cur):
    ensure_shared_extensions(cur)
    cur.execute("SET search_path TO sensos, public;")
    create_version_history_table(cur)
    create_networks_table(cur)
    create_wireguard_peers_table(cur)
    create_wireguard_keys_table(cur)
    create_ssh_keys_table(cur)
    create_client_status_table(cur)
    create_hardware_profile_table(cur)
    create_peer_location_table(cur)
    create_runtime_wireguard_status_table(cur)


def migrate_0_6_0_schema_updates(cur):
    ensure_shared_extensions(cur)
    cur.execute("SET search_path TO sensos, public;")
    create_networks_table(cur)
    create_client_status_table(cur)


SCHEMA_MIGRATIONS = [
    SchemaMigration(
        version=parse_version_key("0.5.0"),
        name="initial release schema",
        apply=create_initial_schema,
    ),
    SchemaMigration(
        version=parse_version_key("0.6.0"),
        name="reconcile legacy network endpoint and client status schema",
        apply=migrate_0_6_0_schema_updates,
    ),
]


def apply_schema_migrations(cur, target_version: str):
    ensure_schema_migrations_table(cur)
    target_key = parse_version_key(target_version)

    cur.execute("SELECT version FROM sensos.schema_migrations;")
    applied_versions = {row[0] for row in cur.fetchall()}

    for migration in SCHEMA_MIGRATIONS:
        migration_version = render_version_key(migration.version)
        if migration.version > target_key or migration_version in applied_versions:
            continue
        migration.apply(cur)
        cur.execute(
            """
            INSERT INTO sensos.schema_migrations (version, name)
            VALUES (%s, %s)
            ON CONFLICT (version) DO NOTHING;
            """,
            (migration_version, migration.name),
        )


def render_version_key(version_key: tuple[int, int, int, int, str]) -> str:
    major, minor, patch, release_rank, suffix = version_key
    base = f"{major}.{minor}.{patch}"
    if release_rank == 1:
        return base
    return f"{base}-{suffix}"


def lookup_peer_id(conn, wireguard_ip):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM sensos.wireguard_peers WHERE wg_ip = %s",
            (wireguard_ip,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(400, detail=f"Unknown wireguard_ip: {wireguard_ip}")
        return row[0]


def get_network_details(network_name: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ip_range, wg_public_key, wg_public_ip, wg_port
                FROM sensos.networks
                WHERE name = %s;
                """,
                (network_name,),
            )
            return cur.fetchone()


def wait_for_network_ready(
    network_name: str,
    timeout_seconds: int = 30,
    poll_interval_seconds: float = 1.0,
):
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT n.id, n.ip_range, n.wg_public_key, n.wg_public_ip, n.wg_port
                    FROM sensos.networks n
                    LEFT JOIN sensos.runtime_wireguard_status r
                      ON r.network_id = n.id
                     AND r.component = %s
                    WHERE n.name = %s;
                    """,
                    (RUNTIME_COMPONENT_WIREGUARD, network_name),
                )
                row = cur.fetchone()
                if row and row[2]:
                    return row

        time.sleep(poll_interval_seconds)

    raise TimeoutError(
        f"network '{network_name}' was created but did not become ready within {timeout_seconds} seconds"
    )


def resolve_hostname(value: str):
    try:
        socket.inet_pton(socket.AF_INET, value)
        return value
    except OSError:
        try:
            socket.inet_pton(socket.AF_INET6, value)
            return value
        except OSError:
            pass
    try:
        addr_info = socket.getaddrinfo(value, None, family=socket.AF_UNSPEC)
        for family, _, _, _, sockaddr in addr_info:
            if family in (socket.AF_INET, socket.AF_INET6):
                return sockaddr[0]
    except socket.gaierror:
        pass
    return None


def generate_default_ip_range(name: str) -> ipaddress.IPv4Network:
    hash_val = sum(ord(c) for c in name) % 256
    return ipaddress.ip_network(f"10.{hash_val}.0.0/16")


def allocate_network_ip_range(cur: Cursor, name: str) -> ipaddress.IPv4Network:
    preferred = generate_default_ip_range(name)
    preferred_second_octet = int(str(preferred.network_address).split(".")[1])

    cur.execute("SELECT ip_range FROM sensos.networks;")
    used_ranges = {str(ipaddress.ip_network(row[0], strict=False)) for row in cur.fetchall()}

    for offset in range(256):
        candidate_second_octet = (preferred_second_octet + offset) % 256
        candidate = ipaddress.ip_network(f"10.{candidate_second_octet}.0.0/16")
        if str(candidate) not in used_ranges:
            return candidate

    raise RuntimeError("no available default 10.x.0.0/16 network ranges remain")


def insert_peer(
    network_id: int, wg_ip: str, note: Optional[str] = None
) -> Tuple[int, str]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sensos.wireguard_peers (network_id, wg_ip, note)
                VALUES (%s, %s, %s)
                RETURNING id, uuid;
                """,
                (network_id, wg_ip, note),
            )
            return cur.fetchone()


def register_wireguard_key_in_db(wg_ip: str, wg_public_key: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sensos.wireguard_peers WHERE wg_ip = %s;",
                (wg_ip,),
            )
            peer = cur.fetchone()
            if not peer:
                return None

            peer_id = peer[0]
            cur.execute(
                "UPDATE sensos.wireguard_keys SET is_active = FALSE WHERE peer_id = %s;",
                (peer_id,),
            )
            cur.execute(
                """
                INSERT INTO sensos.wireguard_keys (peer_id, wg_public_key, is_active)
                VALUES (%s, %s, TRUE);
                """,
                (peer_id, wg_public_key),
            )

    return {"wg_ip": wg_ip, "wg_public_key": wg_public_key}


def create_network_entry(
    cur: Cursor,
    name: str,
    wg_public_ip: str,
    wg_port: int | None = None,
) -> tuple[dict, bool]:
    cur.execute(
        """
        SELECT id, ip_range, wg_public_ip, wg_port, wg_public_key
        FROM sensos.networks
        WHERE name = %s;
        """,
        (name,),
    )
    existing = cur.fetchone()
    if existing:
        endpoint_changed = existing[2] != wg_public_ip
        if wg_port is not None and existing[3] != wg_port:
            endpoint_changed = True
        if endpoint_changed:
            raise RuntimeError(
                f"network '{name}' already exists with endpoint "
                f"{existing[2]}:{existing[3]}; use the explicit network endpoint "
                "update path to change the published client endpoint"
            )
        return (
            {
                "id": existing[0],
                "name": name,
                "ip_range": existing[1],
                "wg_public_ip": existing[2],
                "wg_port": existing[3],
                "wg_public_key": existing[4],
            },
            False,
        )

    if wg_port is None:
        wg_port = allocate_public_wg_port(cur)

    ip_range = allocate_network_ip_range(cur, name)
    cur.execute(
        """
        INSERT INTO sensos.networks
          (name, ip_range, wg_public_ip, wg_port, wg_public_key)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (name, ip_range, wg_public_ip, wg_port, None),
    )
    network_id = cur.fetchone()[0]

    return (
        {
            "id": network_id,
            "name": name,
            "ip_range": str(ip_range),
            "wg_public_ip": wg_public_ip,
            "wg_port": wg_port,
            "wg_public_key": None,
        },
        True,
    )


def update_network_endpoint(
    cur: Cursor,
    name: str,
    wg_public_ip: str,
    wg_port: int,
) -> dict:
    cur.execute(
        """
        UPDATE sensos.networks
        SET wg_public_ip = %s, wg_port = %s
        WHERE name = %s
        RETURNING id, name, ip_range, wg_public_ip, wg_port, wg_public_key;
        """,
        (wg_public_ip, wg_port, name),
    )
    updated = cur.fetchone()
    if not updated:
        raise RuntimeError(f"network '{name}' does not exist")

    return {
        "id": updated[0],
        "name": updated[1],
        "ip_range": updated[2],
        "wg_public_ip": updated[3],
        "wg_port": updated[4],
        "wg_public_key": updated[5],
    }


def allocate_public_wg_port(cur: Cursor) -> int:
    cur.execute(
        """
        SELECT wg_port
        FROM sensos.networks
        WHERE wg_port BETWEEN %s AND %s;
        """,
        (PUBLIC_WG_PORT_START, PUBLIC_WG_PORT_END),
    )
    used_ports = {row[0] for row in cur.fetchall()}

    for candidate in range(PUBLIC_WG_PORT_START, PUBLIC_WG_PORT_END + 1):
        if candidate not in used_ports:
            return candidate

    raise RuntimeError(
        f"no available public WireGuard ports remain in {PUBLIC_WG_PORT_START}-{PUBLIC_WG_PORT_END}"
    )


def get_assigned_ips(network_id: int) -> set[ipaddress.IPv4Address]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT wg_ip FROM sensos.wireguard_peers WHERE network_id = %s;",
                (network_id,),
            )
            return {ipaddress.ip_address(row[0]) for row in cur.fetchall()}


def set_peer_active_state(wg_ip: str, is_active: bool) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sensos.wireguard_peers
                SET is_active = %s
                WHERE wg_ip = %s
                RETURNING id;
                """,
                (is_active, wg_ip),
            )
            row = cur.fetchone()
            if row is None:
                return False
            conn.commit()
            return True


def delete_peer(wg_ip: str) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sensos.wireguard_peers WHERE wg_ip = %s;",
                (wg_ip,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            peer_id = row[0]
            cur.execute(
                "DELETE FROM sensos.wireguard_peers WHERE id = %s;",
                (peer_id,),
            )
            conn.commit()
            return True


def delete_network(name: str) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sensos.networks WHERE name = %s RETURNING id;",
                (name,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            conn.commit()
            return True


def search_for_next_available_ip(
    network: str,
    network_id: int,
    start_third_octet: int = 1,
) -> Optional[ipaddress.IPv4Address]:
    ip_range = ipaddress.ip_network(network, strict=False)
    used_ips = get_assigned_ips(network_id)

    # .1 is reserved for the API proxy inside the tunnel.
    used_ips.add(ip_range.network_address + 1)
    start_ip = ipaddress.ip_address(
        int(ip_range.network_address) + start_third_octet * 256 + (2 if start_third_octet == 0 else 1)
    )

    for host_int in range(int(start_ip), int(ip_range.broadcast_address)):
        host_ip = ipaddress.ip_address(host_int)
        if host_ip.packed[-1] in (0, 255):
            continue
        if host_ip not in used_ips:
            return host_ip

    return None


def create_version_history_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.version_history (
            id SERIAL PRIMARY KEY,
            version_major TEXT NOT NULL,
            version_minor TEXT NOT NULL,
            version_patch TEXT NOT NULL,
            version_suffix TEXT,
            git_commit TEXT,
            git_branch TEXT,
            git_tag TEXT,
            git_dirty TEXT,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )


def create_networks_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.networks (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            ip_range CIDR UNIQUE NOT NULL,
            wg_public_ip TEXT NOT NULL,
            wg_port INTEGER NOT NULL CHECK (wg_port > 0 AND wg_port <= 65535),
            wg_public_key TEXT UNIQUE,
            UNIQUE (wg_public_ip, wg_port)
        );
        """
    )
    cur.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'sensos'
                  AND table_name = 'networks'
                  AND column_name = 'wg_public_ip'
                  AND data_type = 'inet'
            ) THEN
                ALTER TABLE sensos.networks
                ALTER COLUMN wg_public_ip TYPE TEXT
                USING wg_public_ip::text;
            END IF;
        END
        $$;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.networks
        ALTER COLUMN wg_public_key DROP NOT NULL;
        """
    )


def create_wireguard_peers_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.wireguard_peers (
            id SERIAL PRIMARY KEY,
            uuid UUID NOT NULL DEFAULT gen_random_uuid(),
            network_id INTEGER REFERENCES sensos.networks(id) ON DELETE CASCADE,
            wg_ip INET UNIQUE NOT NULL,
            note TEXT DEFAULT NULL,
            registered_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(uuid)
        );
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.wireguard_peers
        ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
        """
    )


def create_wireguard_keys_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.wireguard_keys (
            id SERIAL PRIMARY KEY,
            peer_id INTEGER REFERENCES sensos.wireguard_peers(id) ON DELETE CASCADE,
            wg_public_key TEXT UNIQUE NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )


def create_ssh_keys_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.ssh_keys (
            id SERIAL PRIMARY KEY,
            peer_id INTEGER REFERENCES sensos.wireguard_peers(id) ON DELETE CASCADE,
            username TEXT NOT NULL,
            uid INTEGER NOT NULL,
            ssh_public_key TEXT NOT NULL,
            key_type TEXT NOT NULL,
            key_size INTEGER NOT NULL,
            key_comment TEXT,
            fingerprint TEXT NOT NULL,
            expires_at TIMESTAMPTZ,
            last_used TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (peer_id, ssh_public_key)
        );
        """
    )


def create_client_status_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.client_status (
            id SERIAL PRIMARY KEY,
            peer_id INTEGER REFERENCES sensos.wireguard_peers(id) ON DELETE CASCADE,
            last_check_in TIMESTAMPTZ,
            uptime_seconds INTEGER,
            hostname TEXT,
            disk_available_gb REAL,
            memory_used_mb INTEGER,
            memory_total_mb INTEGER,
            load_1m REAL,
            load_5m REAL,
            load_15m REAL,
            version TEXT,
            status_message TEXT
        );
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS peer_id INTEGER;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS last_check_in TIMESTAMPTZ;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS uptime_seconds INTEGER;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS hostname TEXT;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS disk_available_gb REAL;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS memory_used_mb INTEGER;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS memory_total_mb INTEGER;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS load_1m REAL;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS load_5m REAL;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS load_15m REAL;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS version TEXT;
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.client_status
        ADD COLUMN IF NOT EXISTS status_message TEXT;
        """
    )
    cur.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'sensos'
                  AND table_name = 'client_status'
                  AND column_name = 'wireguard_ip'
            ) THEN
                UPDATE sensos.client_status cs
                SET peer_id = p.id
                FROM sensos.wireguard_peers p
                WHERE cs.peer_id IS NULL
                  AND p.wg_ip = cs.wireguard_ip;
            END IF;
        END
        $$;
        """
    )
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'client_status_peer_id_fkey'
                  AND conrelid = 'sensos.client_status'::regclass
            ) THEN
                ALTER TABLE sensos.client_status
                ADD CONSTRAINT client_status_peer_id_fkey
                FOREIGN KEY (peer_id)
                REFERENCES sensos.wireguard_peers(id)
                ON DELETE CASCADE;
            END IF;
        END
        $$;
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_client_status_peer_id_last_check_in
        ON sensos.client_status (peer_id, last_check_in DESC);
        """
    )


def update_version_history_table(cur):
    cur.execute(
        """
        INSERT INTO sensos.version_history
        (version_major, version_minor, version_patch, version_suffix, git_commit, git_branch, git_tag, git_dirty)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """,
        (
            VERSION_MAJOR,
            VERSION_MINOR,
            VERSION_PATCH,
            VERSION_SUFFIX,
            GIT_COMMIT,
            GIT_BRANCH,
            GIT_TAG,
            GIT_DIRTY,
        ),
    )


def create_hardware_profile_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.hardware_profiles (
            id SERIAL PRIMARY KEY,
            peer_id INTEGER REFERENCES sensos.wireguard_peers(id) ON DELETE CASCADE,
            profile_json JSONB NOT NULL,
            uploaded_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(peer_id)
        );
        """
    )


def create_peer_location_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.peer_locations (
            id SERIAL PRIMARY KEY,
            peer_id INTEGER REFERENCES sensos.wireguard_peers(id) ON DELETE CASCADE,
            location GEOGRAPHY(POINT, 4326) NOT NULL,
            recorded_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )


def create_runtime_wireguard_status_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.runtime_wireguard_status (
            id SERIAL PRIMARY KEY,
            component TEXT NOT NULL,
            role TEXT NOT NULL,
            network_id INTEGER REFERENCES sensos.networks(id) ON DELETE CASCADE,
            interface_name TEXT NOT NULL,
            status TEXT NOT NULL,
            public_key TEXT,
            raw_status TEXT,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_error TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(component, network_id)
        );
        """
    )
