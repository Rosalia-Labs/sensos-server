# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

# core.py
import fcntl
import json
import os
import stat
import ipaddress
import logging
import psycopg
import socket
import docker
import time
import tempfile

from psycopg import Cursor

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Tuple, Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from wireguard import (
    WireGuardService,
    WireGuardInterface,
    WireGuardInterfaceEntry,
    WireGuardPeerEntry,
    WireGuard,
)

# ------------------------------------------------------------
# Logging & Configuration
# ------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
if not POSTGRES_PASSWORD:
    raise ValueError("POSTGRES_PASSWORD is not set. Exiting.")
DATABASE_URL = (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@sensos-database/postgres"
)

API_PASSWORD = os.getenv("API_PASSWORD")
if not API_PASSWORD:
    raise ValueError("API_PASSWORD is not set. Exiting.")

VERSION_MAJOR = os.getenv("VERSION_MAJOR", "Unknown")
VERSION_MINOR = os.getenv("VERSION_MINOR", "Unknown")
VERSION_PATCH = os.getenv("VERSION_PATCH", "Unknown")
VERSION_SUFFIX = os.getenv("VERSION_SUFFIX", "")
GIT_COMMIT = os.getenv("GIT_COMMIT", "Unknown")
GIT_BRANCH = os.getenv("GIT_BRANCH", "Unknown")
GIT_TAG = os.getenv("GIT_TAG", "Unknown")
GIT_DIRTY = os.getenv("GIT_DIRTY", "false")

CONTROLLER_CONFIG_DIR = Path("/etc/wireguard")
API_PROXY_CONFIG_DIR = Path("/api_proxy_config")
WG_CONTAINER_CONFIG_DIR = Path("/wireguard_config")
WG_STATE_DIR = WG_CONTAINER_CONFIG_DIR / "state"

# ensure dirs
CONTROLLER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
API_PROXY_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
WG_CONTAINER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
WG_STATE_DIR.mkdir(parents=True, exist_ok=True)

wg = WireGuard()
wgs = WireGuardService()


# ------------------------------------------------------------
# Application Lifespan
# ------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Async context manager for handling the application's startup and shutdown procedures.

    During startup, it:
      - Creates the 'sensos' schema if it doesn't exist.
      - Sets the search path to include the schema.
      - Creates and/or updates required database tables.
      - Initializes network configuration and WireGuard interfaces.

    During shutdown, it logs the shutdown procedure.

    Parameters:
        app (FastAPI): The FastAPI application instance.

    Yields:
        None
    """
    logger.info("Called lifespan async context manager...")
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                logger.info("Creating schema 'sensos' if not exists...")
                cur.execute("CREATE SCHEMA IF NOT EXISTS sensos;")
                cur.execute("SET search_path TO sensos, public;")
                create_version_history_table(cur)
                update_version_history_table(cur)
                create_networks_table(cur)
                create_wireguard_peers_table(cur)
                create_wireguard_keys_table(cur)
                create_ssh_keys_table(cur)
                create_client_status_table(cur)
                create_hardware_profile_table(cur)
                create_peer_location_table(cur)
                sync_network_public_keys_from_shared_state(cur)
                verify_wireguard_keys_against_database(cur)
        logger.info("✅ Database schema and tables initialized successfully.")
    except Exception as e:
        logger.error(f"❌ Error initializing database: {e}", exc_info=True)
    yield
    logger.info("Shutting down!")


# ------------------------------------------------------------
# Security & Authentication
# ------------------------------------------------------------
security = HTTPBasic()


def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    """
    Verifies HTTP Basic credentials against the API_PASSWORD environment variable.

    Parameters:
        credentials (HTTPBasicCredentials): The credentials provided by the client.

    Returns:
        HTTPBasicCredentials: The same credentials if authentication is successful.

    Raises:
        HTTPException: If the provided password does not match the expected API_PASSWORD.
    """
    if credentials.password != API_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials


# ------------------------------------------------------------
# Database Connection
# ------------------------------------------------------------
def get_db(retries: int = 10, delay: int = 3):
    """
    Establishes and returns a PostgreSQL database connection.

    The function will attempt to connect to the database for a specified number of times,
    with a delay between attempts, to handle potential startup race conditions.

    Parameters:
        retries (int): Number of connection attempts (default: 10).
        delay (int): Delay in seconds between attempts (default: 3).

    Returns:
        connection: A psycopg connection object with autocommit enabled.

    Raises:
        psycopg.OperationalError: If connection fails after all attempts.
    """
    for attempt in range(retries):
        try:
            return psycopg.connect(DATABASE_URL, autocommit=True)
        except psycopg.OperationalError:
            if attempt == retries - 1:
                raise
            logger.info(
                f"Database not ready, retrying in {delay} seconds... (Attempt {attempt+1}/{retries})"
            )
            time.sleep(delay)


# ------------------------------------------------------------
# Core Utility Functions
# ------------------------------------------------------------


def lookup_client_id(conn, wireguard_ip):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM sensos.wireguard_peers WHERE wg_ip = %s", (wireguard_ip,)
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(400, detail=f"Unknown wireguard_ip: {wireguard_ip}")
        return row[0]


def get_network_details(network_name: str):
    """
    Retrieves network details from the database based on the network name.

    Parameters:
        network_name (str): The name of the network.

    Returns:
        tuple or None: A tuple containing (id, ip_range, wg_public_key, wg_public_ip, wg_port)
                       if the network is found; otherwise, None.
    """
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


def network_ready(network_name: str) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            sync_network_public_keys_from_shared_state(cur)
            cur.execute(
                """
                SELECT wg_public_key
                FROM sensos.networks
                WHERE name = %s;
                """,
                (network_name,),
            )
            row = cur.fetchone()
            return row is not None and bool(row[0])


def wait_for_network_ready(
    network_name: str,
    timeout_seconds: int = 30,
    poll_interval_seconds: float = 1.0,
):
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        with get_db() as conn:
            with conn.cursor() as cur:
                generate_wireguard_container_configs(cur)
                sync_network_public_keys_from_shared_state(cur)
                cur.execute(
                    """
                    SELECT id, ip_range, wg_public_key, wg_public_ip, wg_port
                    FROM sensos.networks
                    WHERE name = %s;
                    """,
                    (network_name,),
                )
                row = cur.fetchone()
                if row and row[2]:
                    return row

        time.sleep(poll_interval_seconds)

    raise TimeoutError(
        f"network '{network_name}' was created but did not become ready within {timeout_seconds} seconds"
    )


def restart_container(container_name: str):
    """
    Restarts a Docker container identified by its name.

    If the container is not running, logs a warning and attempts to restart it.

    Parameters:
        container_name (str): The name of the container to restart.

    Returns:
        None
    """
    try:
        client = docker.from_env()
        container = client.containers.get(container_name)
        if container.status != "running":
            logger.warning(
                f"Container '{container_name}' is not running but will be restarted."
            )
        container.restart()
        logger.info(f"Container '{container_name}' restarted successfully.")
    except Exception as e:
        logger.error(f"Error restarting container '{container_name}': {e}")


def resolve_hostname(value: str):
    """
    Resolves a hostname or returns the value if it is already a valid IP address.

    Attempts to interpret the input as an IPv4 or IPv6 address. If not, performs a DNS
    lookup to resolve the hostname.

    Parameters:
        value (str): A hostname or IP address.

    Returns:
        str or None: The resolved IP address as a string, or None if resolution fails.
    """
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


def get_container_ip(container_name: str):
    """
    Retrieves the IP address of a Docker container using the Docker SDK.

    Parameters:
        container_name (str): The name of the container.

    Returns:
        str or None: The container's IP address if found, otherwise None.

    Raises:
        ValueError: If no valid IP address is found.
    """
    try:
        client = docker.from_env()
        container = client.containers.get(container_name)
        networks = container.attrs["NetworkSettings"]["Networks"]
        for network_name, network_info in networks.items():
            if "IPAddress" in network_info and network_info["IPAddress"]:
                return network_info["IPAddress"]
        raise ValueError(
            f"❌ No valid IP address found for container '{container_name}'"
        )
    except Exception as e:
        logger.error(f"❌ Error getting container IP for '{container_name}': {e}")
    return None


def generate_default_ip_range(name: str) -> ipaddress.IPv4Network:
    hash_val = sum(ord(c) for c in name) % 256
    return ipaddress.ip_network(f"10.{hash_val}.0.0/16")


def insert_peer(
    network_id: int, wg_ip: str, note: Optional[str] = None
) -> Tuple[int, str]:
    """
    Inserts a new WireGuard peer entry into the database.

    Parameters:
        network_id (int): The ID of the network.
        wg_ip (str): The WireGuard IP to assign to the peer.
        note (str, optional): An optional note or description.

    Returns:
        tuple: A tuple containing the new peer's id and uuid.
    """
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
    """
    Registers a WireGuard public key in the database for an existing peer,
    deactivating any previous keys for that peer.

    Parameters:
        wg_ip (str): The WireGuard IP address of the peer.
        wg_public_key (str): The public key to register.

    Returns:
        dict or None: A dictionary containing the wg_ip and wg_public_key if successful,
                      otherwise None if the peer does not exist.
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sensos.wireguard_peers WHERE wg_ip = %s;", (wg_ip,)
            )
            peer = cur.fetchone()
            if not peer:
                return None

            peer_id = peer[0]

            # Deactivate all existing keys for this peer
            cur.execute(
                "UPDATE sensos.wireguard_keys SET is_active = FALSE WHERE peer_id = %s;",
                (peer_id,),
            )

            # Insert the new key
            cur.execute(
                "INSERT INTO sensos.wireguard_keys (peer_id, wg_public_key, is_active) VALUES (%s, %s, TRUE);",
                (peer_id, wg_public_key),
            )

    return {"wg_ip": wg_ip, "wg_public_key": wg_public_key}


def create_network_entry(
    cur: Cursor,
    name: str,
    wg_public_ip: str,
    wg_port: int,
) -> tuple[dict, bool]:
    """
    Creates a new network entry in the DB plus desired-state metadata for the
    WireGuard container.
    If a network with this name already exists, returns its details immediately.
    The boolean return value indicates whether a new network was created.
    """
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

    ip_range = generate_default_ip_range(name)

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


def reconcile_runtime_configs() -> None:
    with get_db() as conn:
        with conn.cursor() as cur:
            sync_network_public_keys_from_shared_state(cur)
            generate_api_proxy_wireguard_configs(cur)
            generate_controller_wireguard_configs(cur)
            generate_wireguard_container_configs(cur)


def update_wireguard_configs():
    with get_db() as conn:
        with conn.cursor() as cur:
            sync_network_public_keys_from_shared_state(cur)
            generate_wireguard_container_configs(cur)


def _network_state_path(name: str) -> Path:
    return WG_STATE_DIR / f"{name}.json"


def update_network_state(name: str, mutator) -> dict:
    state_path = _network_state_path(name)
    with state_path.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read().strip()
        state = json.loads(raw) if raw else {}
        state = mutator(state)
        f.seek(0)
        f.truncate()
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return state


def write_wireguard_network_state(
    name: str,
    ip_range: str,
    wg_public_ip: str,
    wg_port: int,
    peers: list[tuple[str, str]],
) -> None:
    desired = {
        "ip_range": ip_range,
        "peers": [
            {"wg_ip": wg_ip, "wg_public_key": public_key}
            for wg_ip, public_key in peers
        ],
        "wg_port": wg_port,
        "wg_public_ip": wg_public_ip,
    }

    update_network_state(
        name,
        lambda current: {
            **current,
            "network_name": name,
            "desired": desired,
            "observed": current.get("observed") or {},
        },
    )


def sync_network_public_keys_from_shared_state(cur: Cursor) -> None:
    for state_path in WG_STATE_DIR.glob("*.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        network_name = state.get("network_name") or state_path.stem
        public_key = (state.get("observed") or {}).get("wg_public_key")
        if not public_key:
            continue

        cur.execute(
            """
            UPDATE sensos.networks
               SET wg_public_key = %s
             WHERE name = %s AND wg_public_key IS DISTINCT FROM %s;
            """,
            (public_key, network_name, public_key),
        )


def generate_api_proxy_wireguard_configs(
    cur: Cursor,
    restart_api_proxy_container: bool = True,
) -> None:
    """
    For each network in the DB, ensure a valid WireGuard config exists for the API proxy container.
    - On-disk private keys are never overwritten.
    - New keys are only generated by WireGuardInterface.set_interface().
    - Registers the new public key in the DB exactly once.
    """

    cur.execute(
        """
        SELECT id, name, ip_range, wg_public_key, wg_port
        FROM sensos.networks;
        """
    )
    networks = cur.fetchall()

    for network_id, name, ip_range_cidr, server_pub_key, wg_port in networks:
        if not server_pub_key:
            logger.info(
                "skipping API proxy WireGuard config for %s until server public key is published",
                name,
            )
            continue

        ip_range = ipaddress.ip_network(ip_range_cidr, strict=False)
        proxy_ip = ip_range.network_address + 1
        proxy_ip_str = str(proxy_ip)

        iface = WireGuardInterface(name=name, config_dir=API_PROXY_CONFIG_DIR)
        if iface.config_exists():
            iface.load_config()
            priv_key = iface.get_private_key()
        else:
            priv_key = wg.genkey()

        iface.set_interface(
            WireGuardInterfaceEntry(
                Address=f"{proxy_ip_str}/32",
                ListenPort=wg_port,
                PrivateKey=priv_key,
            )
        )

        iface.peer_defs.clear()
        iface.add_peer(
            WireGuardPeerEntry(
                PublicKey=server_pub_key,
                Endpoint=f"sensos-wireguard:{wg_port}",
                AllowedIPs=str(ip_range),
                PersistentKeepalive="25",
            )
        )

        iface.save_config(overwrite=True)

        cur.execute(
            "SELECT 1 FROM sensos.wireguard_peers WHERE network_id = %s AND wg_ip = %s",
            (network_id, proxy_ip_str),
        )
        if cur.fetchone() is None:
            insert_peer(network_id, proxy_ip_str, note="API Proxy Container")
            register_wireguard_key_in_db(
                proxy_ip_str, wg.pubkey(iface.get_private_key())
            )

    if restart_api_proxy_container:
        restart_container("sensos-api-proxy")
    logger.info("✅ Reconciled API proxy configs for all networks.")


def generate_controller_wireguard_configs(
    cur: Cursor,
) -> None:
    """
    For each network in the DB, ensure a valid WireGuard config exists for the API proxy container.
    - On-disk private keys are never overwritten.
    - New keys are only generated by WireGuardInterface.set_interface().
    - Registers the new public key in the DB exactly once.
    """

    cur.execute(
        """
        SELECT id, name, ip_range, wg_public_key, wg_port
        FROM sensos.networks;
        """
    )
    networks = cur.fetchall()

    for network_id, name, ip_range_cidr, server_pub_key, wg_port in networks:
        if not server_pub_key:
            logger.info(
                "skipping controller WireGuard config for %s until server public key is published",
                name,
            )
            continue

        ip_range = ipaddress.ip_network(ip_range_cidr, strict=False)
        controller_ip = ip_range.network_address + 2
        controller_ip_str = str(controller_ip)

        iface = WireGuardInterface(name=name, config_dir=CONTROLLER_CONFIG_DIR)
        if iface.config_exists():
            iface.load_config()
            priv_key = iface.get_private_key()
        else:
            priv_key = wg.genkey()

        iface.set_interface(
            WireGuardInterfaceEntry(
                Address=f"{controller_ip_str}/32",
                PrivateKey=priv_key,
            )
        )

        iface.peer_defs.clear()
        iface.add_peer(
            WireGuardPeerEntry(
                PublicKey=server_pub_key,
                Endpoint=f"sensos-wireguard:{wg_port}",
                AllowedIPs=str(ip_range),
                PersistentKeepalive="25",
            )
        )

        iface.save_config(overwrite=True)

        cur.execute(
            "SELECT 1 FROM sensos.wireguard_peers WHERE network_id = %s AND wg_ip = %s",
            (network_id, controller_ip_str),
        )
        if cur.fetchone() is None:
            insert_peer(network_id, controller_ip_str, note="Controller Container")
            register_wireguard_key_in_db(
                controller_ip_str, wg.pubkey(iface.get_private_key())
            )

        wgs.bring_up(name)

    logger.info("✅ Reconciled controller configs for all networks.")


def generate_wireguard_container_configs(
    cur: Cursor, restart_wireguard_container: bool = True
) -> None:
    """
    Publish the desired non-secret WireGuard server configuration for each
    network into the shared coordination volume.
    """
    cur.execute("SELECT id, name, ip_range, wg_public_ip, wg_port FROM sensos.networks;")
    networks = cur.fetchall()

    for network_id, name, ip_range_cidr, wg_public_ip, wg_port in networks:
        cur.execute(
            """
            SELECT p.wg_ip, k.wg_public_key
              FROM sensos.wireguard_peers p
              JOIN sensos.wireguard_keys k
                ON p.id = k.peer_id
             WHERE p.network_id = %s AND k.is_active = TRUE;
            """,
            (network_id,),
        )
        write_wireguard_network_state(
            name=name,
            ip_range=str(ip_range_cidr),
            wg_public_ip=str(wg_public_ip),
            wg_port=wg_port,
            peers=cur.fetchall(),
        )

    logger.info("✅ Reconciled WireGuard configs for all networks.")


def get_assigned_ips(network_id: int) -> set[ipaddress.IPv4Address]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT wg_ip FROM sensos.wireguard_peers WHERE network_id = %s;",
                (network_id,),
            )
            return {ipaddress.ip_address(row[0]) for row in cur.fetchall()}


def search_for_next_available_ip(
    network: str,
    network_id: int,
    start_third_octet: int = 0,
) -> Optional[ipaddress.IPv4Address]:
    """
    Finds the next available IP in the given network range, starting from start_third_octet.
    Walks through each /24 block (<prefix>.<third octet>.1–254) until an available IP is found.
    """
    ip_range = ipaddress.ip_network(network, strict=False)
    used_ips = get_assigned_ips(network_id)

    used_ips.add(ip_range.network_address + 1)
    used_ips.add(ip_range.network_address + 2)

    base_bytes = bytearray(ip_range.network_address.packed)
    max_subnet = ip_range.num_addresses // 256

    for third_octet in range(start_third_octet, max_subnet):
        base_bytes[2] = third_octet
        base_bytes[3] = 0
        subnet_base = ipaddress.IPv4Address(bytes(base_bytes))
        subnet_net = ipaddress.ip_network(f"{subnet_base}/24", strict=False)

        for host_ip in subnet_net.hosts():
            if host_ip not in used_ips:
                return host_ip

    return None


def create_version_history_table(cur):
    """
    Creates the version_history table to track version and Git information.

    Parameters:
        cur: The database cursor.

    Returns:
        None
    """
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
    """
    Creates the networks table to store network configurations.

    Parameters:
        cur: The database cursor.

    Returns:
        None
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.networks (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            ip_range CIDR UNIQUE NOT NULL,
            wg_public_ip INET NOT NULL,
            wg_port INTEGER NOT NULL CHECK (wg_port > 0 AND wg_port <= 65535),
            wg_public_key TEXT UNIQUE,
            UNIQUE (wg_public_ip, wg_port)
        );
        """
    )
    cur.execute(
        """
        ALTER TABLE sensos.networks
        ALTER COLUMN wg_public_key DROP NOT NULL;
        """
    )


def create_wireguard_peers_table(cur):
    """
    Creates the wireguard_peers table to store peer information for WireGuard.

    Parameters:
        cur: The database cursor.

    Returns:
        None
    """
    cur.execute(
        """
        CREATE EXTENSION IF NOT EXISTS "pgcrypto";
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


def create_wireguard_keys_table(cur):
    """
    Creates the wireguard_keys table to store WireGuard public keys for peers.

    Parameters:
        cur: The database cursor.

    Returns:
        None
    """
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
    """
    Creates the ssh_keys table to store SSH key information associated with peers.

    Parameters:
        cur: The database cursor.

    Returns:
        None
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.ssh_keys (
            id SERIAL PRIMARY KEY,
            network_id INTEGER REFERENCES sensos.networks(id) ON DELETE CASCADE,
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
    """
    Creates the client_status table to log periodic status information from clients.

    Parameters:
        cur: The database cursor.

    Returns:
        None
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensos.client_status (
            id SERIAL PRIMARY KEY,
            client_id INTEGER NOT NULL,
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


def update_version_history_table(cur):
    """
    Inserts a new version history record into the version_history table.

    Parameters:
        cur: The database cursor.

    Returns:
        None
    """
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
    """
    Creates the hardware_profiles table to store hardware profile data for peers.

    Parameters:
        cur: The database cursor.

    Returns:
        None
    """
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
    """
    Creates the peer_locations table to store geographical location data for peers.

    Parameters:
        cur: The database cursor.

    Returns:
        None
    """
    cur.execute(
        """
        CREATE EXTENSION IF NOT EXISTS postgis;
        CREATE TABLE IF NOT EXISTS sensos.peer_locations (
            id SERIAL PRIMARY KEY,
            peer_id INTEGER REFERENCES sensos.wireguard_peers(id) ON DELETE CASCADE,
            location GEOGRAPHY(POINT, 4326) NOT NULL,
            recorded_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )


def verify_wireguard_keys_against_database(cur):
    """
    Verifies that the public key derived from each WireGuard private key
    matches the expected key in the database. Different checks are performed
    based on the config source directory:

    - WG_STATE_DIR observed state: compares against sensos.networks.wg_public_key
    - API_PROXY_CONFIG_DIR: compares against sensos.wireguard_keys for the peer with IP <base>.0.1
    - CONTROLLER_CONFIG_DIR: compares against sensos.wireguard_keys for the peer with IP <base>.0.2

    Logs warnings for mismatches or missing records.
    """
    logger.info(
        "🔍 Verifying WireGuard key consistency across config files and database..."
    )
    mismatches = 0

    # Handle published WireGuard server public metadata in per-network state files.
    for file in WG_STATE_DIR.glob("*.json"):
        try:
            state = json.loads(file.read_text(encoding="utf-8"))
            name = state.get("network_name") or file.stem
            derived_pubkey = (state.get("observed") or {}).get("wg_public_key")
            if not derived_pubkey:
                logger.warning(f"⚠️ No observed wg_public_key found in {file}")
                continue

            cur.execute(
                "SELECT wg_public_key FROM sensos.networks WHERE name = %s;",
                (name,),
            )
            row = cur.fetchone()
            if row is None:
                logger.warning(f"⚠️ No network found for name '{name}' (from {file})")
                continue

            expected_pubkey = row[0]
            if derived_pubkey != expected_pubkey:
                logger.warning(
                    f"❌ Mismatch for network '{name}': derived {derived_pubkey}, expected {expected_pubkey}"
                )
                mismatches += 1
            else:
                logger.info(f"✅ Match for network '{name}': {derived_pubkey}")
        except Exception as e:
            logger.error(f"❌ Error verifying {file}: {e}")

    # Helper for API_PROXY_CONFIG_DIR and CONTROLLER_CONFIG_DIR
    def verify_peer_config(file, ip_suffix, label):
        try:
            name = file.stem
            iface = WireGuardInterface(name=name, config_dir=file.parent)
            iface.load_config()
            priv_key = iface.get_private_key()
            derived_pubkey = wg.pubkey(priv_key)

            # Get network ID and base IP
            cur.execute(
                "SELECT id, ip_range FROM sensos.networks WHERE name = %s;",
                (name,),
            )
            row = cur.fetchone()
            if row is None:
                logger.warning(f"⚠️ No network found for name '{name}' (from {file})")
                return

            network_id, ip_range = row
            ip_net = ipaddress.IPv4Network(ip_range)
            expected_ip = str(list(ip_net.hosts())[ip_suffix - 1])

            cur.execute(
                """
                SELECT k.wg_public_key
                FROM sensos.wireguard_peers p
                JOIN sensos.wireguard_keys k ON p.id = k.peer_id
                WHERE p.network_id = %s AND p.wg_ip = %s AND k.is_active = TRUE;
                """,
                (network_id, expected_ip),
            )
            row = cur.fetchone()
            if row is None:
                logger.warning(
                    f"⚠️ No active key found for {label} {expected_ip} (from {file})"
                )
                return

            expected_pubkey = row[0]
            if derived_pubkey != expected_pubkey:
                logger.warning(
                    f"❌ Mismatch for {label} '{name}' ({expected_ip}): derived {derived_pubkey}, expected {expected_pubkey}"
                )
                nonlocal mismatches
                mismatches += 1
            else:
                logger.info(
                    f"✅ Match for {label} '{name}' ({expected_ip}): {derived_pubkey}"
                )
        except Exception as e:
            logger.error(f"❌ Error verifying {label} {file}: {e}")

    # API proxy = <base>.0.1
    for file in API_PROXY_CONFIG_DIR.glob("*.conf"):
        verify_peer_config(file, ip_suffix=1, label="API proxy")

    # Controller peer = <base>.0.2
    for file in CONTROLLER_CONFIG_DIR.glob("*.conf"):
        verify_peer_config(file, ip_suffix=2, label="Controller peer")

    if mismatches == 0:
        logger.info("🎉 All WireGuard keys match the database.")
    else:
        logger.warning(f"⚠️ {mismatches} mismatches found in WireGuard keys.")
