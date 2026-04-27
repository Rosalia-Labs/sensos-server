# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import base64
import ipaddress
import logging
import os
import psycopg
import re
import secrets
import socket
import time
from hashlib import sha256

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from psycopg import Cursor
from psycopg import sql

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
PUBLIC_DB_PASSWORD = os.getenv("PUBLIC_DB_PASSWORD", "sensos-public")
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
RUNTIME_COMPONENT_OPS = "sensos-ops"
RUNTIME_ROLE_SERVER = "server"
RUNTIME_ROLE_API_PROXY = "api-proxy"
RUNTIME_ROLE_OPS = "ops"
PUBLIC_WG_PORT_START = 51281
PUBLIC_WG_PORT_END = 51289
PUBLIC_DB_ROLE = "sensos_public"


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


def relation_has_column(cur, schema_name: str, relation_name: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND column_name = %s
        LIMIT 1;
        """,
        (schema_name, relation_name, column_name),
    )
    return cur.fetchone() is not None


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
    if not secrets.compare_digest(credentials.username, "sensos"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not secrets.compare_digest(credentials.password, ADMIN_API_PASSWORD):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials


def authenticate_client(credentials: HTTPBasicCredentials = Depends(security)):
    return authenticate_named_client(credentials, username="sensos")


def authenticate_named_client(
    credentials: HTTPBasicCredentials,
    username: str,
):
    if not secrets.compare_digest(credentials.username, username):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not secrets.compare_digest(credentials.password, CLIENT_API_PASSWORD):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials


def authenticate_sensos_client(credentials: HTTPBasicCredentials = Depends(security)):
    return authenticate_named_client(credentials, username="sensos")


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
    create_runtime_operator_keys_table(cur)
    create_i2c_readings_table(cur)


def migrate_0_6_0_schema_updates(cur):
    ensure_shared_extensions(cur)
    cur.execute("SET search_path TO sensos, public;")
    create_networks_table(cur)
    create_client_status_table(cur)


def migrate_0_7_0_i2c_readings_upload(cur):
    ensure_shared_extensions(cur)
    cur.execute("SET search_path TO sensos, public;")
    create_i2c_readings_table(cur)


def migrate_0_8_0_peer_api_credentials(cur):
    ensure_shared_extensions(cur)
    cur.execute("SET search_path TO sensos, public;")
    create_wireguard_peers_table(cur)


def migrate_0_9_0_runtime_operator_keys(cur):
    ensure_shared_extensions(cur)
    cur.execute("SET search_path TO sensos, public;")
    create_runtime_operator_keys_table(cur)


def migrate_0_10_0_birdnet_upload_schema(cur):
    ensure_shared_extensions(cur)
    cur.execute("SET search_path TO sensos, public;")
    create_birdnet_detections_table(cur)


def migrate_0_11_0_public_dashboard_views(cur):
    ensure_shared_extensions(cur)
    cur.execute("SET search_path TO sensos, public;")
    create_public_sites_view(cur)
    create_public_site_birdnet_recent_view(cur)
    ensure_public_dashboard_role(cur)


def migrate_0_12_0_public_site_detail_views(cur):
    ensure_shared_extensions(cur)
    cur.execute("SET search_path TO sensos, public;")
    create_public_sites_view(cur)
    create_public_site_birdnet_recent_view(cur)
    create_public_site_birdnet_detections_view(cur)
    create_public_site_i2c_recent_view(cur)
    ensure_public_dashboard_role(cur)


def migrate_0_12_1_birdnet_volume_schema(cur):
    ensure_shared_extensions(cur)
    cur.execute("SET search_path TO sensos, public;")
    create_birdnet_detections_table(cur)
    create_public_site_birdnet_detections_view(cur)
    ensure_public_dashboard_role(cur)


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
    SchemaMigration(
        version=parse_version_key("0.7.0"),
        name="add i2c readings upload schema",
        apply=migrate_0_7_0_i2c_readings_upload,
    ),
    SchemaMigration(
        version=parse_version_key("0.8.0"),
        name="add per-peer api credentials",
        apply=migrate_0_8_0_peer_api_credentials,
    ),
    SchemaMigration(
        version=parse_version_key("0.9.0"),
        name="add runtime operator key publication",
        apply=migrate_0_9_0_runtime_operator_keys,
    ),
    SchemaMigration(
        version=parse_version_key("0.10.0"),
        name="add birdnet results upload schema",
        apply=migrate_0_10_0_birdnet_upload_schema,
    ),
    SchemaMigration(
        version=parse_version_key("0.11.0"),
        name="add public dashboard views and read-only role",
        apply=migrate_0_11_0_public_dashboard_views,
    ),
    SchemaMigration(
        version=parse_version_key("0.12.0"),
        name="add public site detail result views",
        apply=migrate_0_12_0_public_site_detail_views,
    ),
    SchemaMigration(
        version=parse_version_key("0.12.1"),
        name="add birdnet window volume to detections and public views",
        apply=migrate_0_12_1_birdnet_volume_schema,
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


def lookup_peer_identity(conn, peer_uuid: str) -> tuple[int, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, wg_ip::text FROM sensos.wireguard_peers WHERE uuid = %s",
            (peer_uuid,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(400, detail=f"Unknown peer_uuid: {peer_uuid}")
        return row[0], row[1]


def hash_peer_api_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = sha256(salt + password.encode("utf-8")).hexdigest()
    return f"{base64.b64encode(salt).decode('ascii')}:{digest}"


def verify_peer_api_password(password: str, encoded_hash: str) -> bool:
    try:
        encoded_salt, expected_digest = encoded_hash.split(":", 1)
        salt = base64.b64decode(encoded_salt.encode("ascii"))
    except Exception:
        return False
    actual_digest = sha256(salt + password.encode("utf-8")).hexdigest()
    return secrets.compare_digest(actual_digest, expected_digest)


def authenticate_peer(credentials: HTTPBasicCredentials = Depends(security)):
    try:
        peer_uuid = str(credentials.username)
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, wg_ip::text, api_password_hash
                FROM sensos.wireguard_peers
                WHERE uuid = %s;
                """,
                (peer_uuid,),
            )
            row = cur.fetchone()

    if row is None or not row[2]:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not verify_peer_api_password(credentials.password, row[2]):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"peer_id": row[0], "peer_uuid": peer_uuid, "wg_ip": row[1]}


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
) -> Tuple[int, str, str]:
    peer_api_password = secrets.token_urlsafe(32)
    password_hash = hash_peer_api_password(peer_api_password)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sensos.wireguard_peers
                    (network_id, wg_ip, note, api_password_hash)
                VALUES (%s, %s, %s, %s)
                RETURNING id, uuid;
                """,
                (network_id, wg_ip, note, password_hash),
            )
            peer_id, peer_uuid = cur.fetchone()
            return peer_id, peer_uuid, peer_api_password


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
            api_password_hash TEXT,
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
    cur.execute(
        """
        ALTER TABLE sensos.wireguard_peers
        ADD COLUMN IF NOT EXISTS api_password_hash TEXT;
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


def create_runtime_operator_keys_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.runtime_operator_keys (
            component TEXT PRIMARY KEY,
            ssh_public_key TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def get_runtime_operator_ssh_key(
    component: str = RUNTIME_COMPONENT_OPS,
) -> str | None:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ssh_public_key
                FROM sensos.runtime_operator_keys
                WHERE component = %s;
                """,
                (component,),
            )
            row = cur.fetchone()
    return row[0] if row else None


def create_i2c_readings_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.i2c_readings (
            id BIGSERIAL PRIMARY KEY,
            wireguard_ip INET NOT NULL,
            hostname TEXT NOT NULL,
            client_version TEXT NOT NULL,
            sent_at TIMESTAMPTZ NOT NULL,
            server_received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            client_reading_id BIGINT NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL,
            device_address TEXT NOT NULL,
            sensor_type TEXT NOT NULL,
            reading_key TEXT NOT NULL,
            reading_value DOUBLE PRECISION NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_i2c_readings_site_recorded_at
        ON sensos.i2c_readings (wireguard_ip, recorded_at DESC, id DESC);
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_i2c_readings_dedupe
        ON sensos.i2c_readings (
            wireguard_ip,
            client_reading_id,
            recorded_at,
            device_address,
            sensor_type,
            reading_key
        );
        """
    )


def create_birdnet_detections_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.birdnet_detections (
            id BIGSERIAL PRIMARY KEY,
            wireguard_ip INET NOT NULL,
            hostname TEXT NOT NULL,
            client_version TEXT NOT NULL,
            sent_at TIMESTAMPTZ NOT NULL,
            server_received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source_path TEXT NOT NULL,
            channel_index INTEGER NOT NULL,
            window_index INTEGER NOT NULL,
            max_score_start_frame BIGINT NOT NULL,
            label TEXT NOT NULL,
            score DOUBLE PRECISION NOT NULL,
            likely_score DOUBLE PRECISION,
            volume DOUBLE PRECISION,
            clip_start_time TIMESTAMPTZ NOT NULL,
            clip_end_time TIMESTAMPTZ NOT NULL,
            clip_path TEXT
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_birdnet_detections_site_clip_time
        ON sensos.birdnet_detections (wireguard_ip, clip_start_time DESC, channel_index, window_index);
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_birdnet_detections_dedupe
        ON sensos.birdnet_detections (
            wireguard_ip,
            channel_index,
            clip_start_time,
            clip_end_time
        );
        """
    )


def create_public_sites_view(cur):
    cur.execute(
        """
        CREATE OR REPLACE VIEW sensos.public_sites AS
        WITH latest_status AS (
            SELECT DISTINCT ON (peer_id)
                peer_id,
                last_check_in,
                hostname,
                version,
                status_message
            FROM sensos.client_status
            ORDER BY peer_id, last_check_in DESC
        ),
        latest_location AS (
            SELECT DISTINCT ON (peer_id)
                peer_id,
                recorded_at,
                public.ST_Y(location::public.geometry)::float AS latitude,
                public.ST_X(location::public.geometry)::float AS longitude
            FROM sensos.peer_locations
            ORDER BY peer_id, recorded_at DESC
        ),
        birdnet_summary AS (
            SELECT wireguard_ip,
                   count(*)::integer AS birdnet_detection_count,
                   count(DISTINCT source_path)::integer AS birdnet_source_count,
                   max(clip_end_time) AS latest_birdnet_result_at
            FROM sensos.birdnet_detections
            GROUP BY wireguard_ip
        )
        SELECT p.uuid::text AS peer_uuid,
               host(p.wg_ip)::text AS wg_ip,
               n.name AS network_name,
               p.note,
               coalesce(nullif(p.note, ''), host(p.wg_ip)::text) AS site_label,
               p.is_active,
               p.registered_at,
               ll.recorded_at AS location_recorded_at,
               ll.latitude,
               ll.longitude,
               ls.last_check_in,
               ls.hostname,
               ls.version,
               ls.status_message,
               coalesce(bs.birdnet_detection_count, 0) AS birdnet_detection_count,
               coalesce(bs.birdnet_source_count, 0) AS birdnet_source_count,
               bs.latest_birdnet_result_at
        FROM sensos.wireguard_peers p
        JOIN sensos.networks n ON n.id = p.network_id
        LEFT JOIN latest_status ls ON ls.peer_id = p.id
        LEFT JOIN latest_location ll ON ll.peer_id = p.id
        LEFT JOIN birdnet_summary bs ON bs.wireguard_ip = p.wg_ip;
        """
    )


def create_public_site_birdnet_recent_view(cur):
    cur.execute(
        """
        CREATE OR REPLACE VIEW sensos.public_site_birdnet_recent AS
        SELECT host(d.wireguard_ip)::text AS wg_ip,
               d.hostname,
               d.client_version,
               d.source_path,
               d.channel_index,
               d.window_index,
               d.clip_start_time,
               d.clip_end_time,
               d.label,
               d.score,
               d.likely_score,
               d.volume,
               row_number() OVER (
                   PARTITION BY d.wireguard_ip
                   ORDER BY d.clip_start_time DESC,
                            d.clip_end_time DESC,
                            d.channel_index,
                            d.window_index,
                            d.id DESC
               ) AS detection_rank
        FROM sensos.birdnet_detections d
        ;
        """
    )


def create_public_site_birdnet_detections_view(cur):
    cur.execute(
        """
        CREATE OR REPLACE VIEW sensos.public_site_birdnet_detections AS
        SELECT host(d.wireguard_ip)::text AS wg_ip,
               d.hostname,
               d.client_version,
               d.source_path,
               d.clip_start_time AS processed_at,
               d.clip_end_time,
               d.channel_index,
               d.window_index,
               d.max_score_start_frame,
               0::double precision AS start_sec,
               GREATEST(EXTRACT(EPOCH FROM (d.clip_end_time - d.clip_start_time)), 0)::double precision AS end_sec,
               d.label AS top_label,
               d.score AS top_score,
               d.likely_score AS top_likely_score,
               row_number() OVER (
                   PARTITION BY d.wireguard_ip
                   ORDER BY d.clip_start_time DESC,
                            d.server_received_at DESC,
                            d.id DESC,
                            d.channel_index,
                            d.window_index
               ) AS detection_rank,
               d.volume
        FROM sensos.birdnet_detections d;
        """
    )


def create_public_site_i2c_recent_view(cur):
    cur.execute(
        """
        CREATE OR REPLACE VIEW sensos.public_site_i2c_recent AS
        SELECT host(r.wireguard_ip)::text AS wg_ip,
               r.hostname,
               r.client_version,
               r.recorded_at,
               r.device_address,
               r.sensor_type,
               r.reading_key,
               r.reading_value,
               r.server_received_at,
               row_number() OVER (
                   PARTITION BY r.wireguard_ip
                   ORDER BY r.recorded_at DESC,
                            r.server_received_at DESC,
                            r.id DESC
               ) AS reading_rank
        FROM sensos.i2c_readings r;
        """
    )


def ensure_public_dashboard_role(cur):
    cur.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s;",
        (PUBLIC_DB_ROLE,),
    )
    if cur.fetchone() is None:
        cur.execute(
            sql.SQL(
                "CREATE ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;"
            ).format(
                sql.Identifier(PUBLIC_DB_ROLE),
                sql.Literal(PUBLIC_DB_PASSWORD),
            )
        )
    else:
        cur.execute(
            sql.SQL(
                "ALTER ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;"
            ).format(
                sql.Identifier(PUBLIC_DB_ROLE),
                sql.Literal(PUBLIC_DB_PASSWORD),
            )
        )

    cur.execute(f"GRANT CONNECT ON DATABASE postgres TO {PUBLIC_DB_ROLE};")
    cur.execute(f"GRANT USAGE ON SCHEMA sensos TO {PUBLIC_DB_ROLE};")
    cur.execute(
        """
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'sensos'
          AND c.relname = ANY(%s)
          AND c.relkind IN ('v', 'm', 'r');
        """,
        (
            [
                "public_sites",
                "public_site_birdnet_recent",
                "public_site_birdnet_detections",
                "public_site_i2c_recent",
            ],
        ),
    )
    existing_relations = [row[0] for row in cur.fetchall()]
    if existing_relations:
        relation_list = ", ".join(f"sensos.{name}" for name in existing_relations)
        cur.execute(f"GRANT SELECT ON {relation_list} TO {PUBLIC_DB_ROLE};")


def format_rfc3339_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def store_i2c_readings_upload(conn, upload, wireguard_ip: str) -> dict:
    received_at = datetime.now(timezone.utc)
    receipt_id = str(uuid4())
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO sensos.i2c_readings (
                    wireguard_ip,
                    hostname,
                    client_version,
                    sent_at,
                    server_received_at,
                    client_reading_id,
                    recorded_at,
                    device_address,
                    sensor_type,
                    reading_key,
                    reading_value
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    wireguard_ip,
                    client_reading_id,
                    recorded_at,
                    device_address,
                    sensor_type,
                    reading_key
                ) DO NOTHING;
                """,
                [
                    (
                        wireguard_ip,
                        upload.hostname,
                        upload.client_version,
                        upload.sent_at,
                        received_at,
                        reading.id,
                        reading.timestamp,
                        reading.device_address,
                        reading.sensor_type,
                        reading.key,
                        reading.value,
                    )
                    for reading in upload.readings
                ],
            )

    return {
        "status": "ok",
        "receipt_id": receipt_id,
        "accepted_count": len(upload.readings),
        "server_received_at": format_rfc3339_utc(received_at),
    }


def store_birdnet_results_upload(conn, upload, wireguard_ip: str) -> dict:
    received_at = datetime.now(timezone.utc)
    receipt_id = str(uuid4())
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO sensos.birdnet_detections (
                    wireguard_ip,
                    hostname,
                    client_version,
                    sent_at,
                    server_received_at,
                    source_path,
                    channel_index,
                    window_index,
                    max_score_start_frame,
                    label,
                    score,
                    likely_score,
                    volume,
                    clip_start_time,
                    clip_end_time,
                    clip_path
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    wireguard_ip,
                    channel_index,
                    clip_start_time,
                    clip_end_time
                ) DO NOTHING;
                """,
                [
                    (
                        wireguard_ip,
                        upload.hostname,
                        upload.client_version,
                        upload.sent_at,
                        received_at,
                        detection.source_path,
                        detection.channel_index,
                        detection.window_index,
                        detection.max_score_start_frame,
                        detection.label,
                        detection.score,
                        detection.likely_score,
                        detection.volume,
                        detection.clip_start_time,
                        detection.clip_end_time,
                        None,
                    )
                    for detection in upload.detections
                ],
            )

    return {
        "status": "ok",
        "receipt_id": receipt_id,
        "accepted_count": len(upload.detections),
        "server_received_at": format_rfc3339_utc(received_at),
    }
