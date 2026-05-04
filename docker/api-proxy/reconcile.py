#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import ipaddress
import shutil
import subprocess
import time
import os

import psycopg

from pathlib import Path

POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
if not POSTGRES_PASSWORD:
    raise ValueError("POSTGRES_PASSWORD is required")

POSTGRES_HOST = "sensos-database"
POSTGRES_DB = "postgres"
COMPONENT = "sensos-api-proxy"
ROLE = "api-proxy"
SERVER_COMPONENT = "sensos-wireguard"
WG_LOCAL_STATE_DIR = Path("/var/lib/sensos-api-proxy")
WG_PRIVATE_KEY_DIR = WG_LOCAL_STATE_DIR / "private"
WG_RENDERED_DIR = WG_LOCAL_STATE_DIR / "rendered"
WG_CONFIG_DIR = Path("/etc/wireguard")


def log(message: str) -> None:
    print(f"[api-proxy-reconcile] {message}", flush=True)


def get_db(retries: int = 10, delay: int = 3):
    for attempt in range(retries):
        try:
            return psycopg.connect(
                host=POSTGRES_HOST,
                dbname=POSTGRES_DB,
                user="postgres",
                password=POSTGRES_PASSWORD,
                autocommit=True,
            )
        except psycopg.OperationalError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)


def run_command(args: list[str], input_text: str | None = None, check: bool = True) -> str:
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )
    return result.stdout.strip()


def ensure_dirs() -> None:
    WG_PRIVATE_KEY_DIR.mkdir(parents=True, exist_ok=True)
    WG_RENDERED_DIR.mkdir(parents=True, exist_ok=True)
    WG_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    WG_PRIVATE_KEY_DIR.chmod(0o700)
    WG_RENDERED_DIR.chmod(0o700)
    WG_CONFIG_DIR.chmod(0o700)


def ensure_private_key(interface_name: str) -> Path:
    key_path = WG_PRIVATE_KEY_DIR / f"{interface_name}.key"
    if not key_path.exists():
        private_key = run_command(["wg", "genkey"])
        key_path.write_text(f"{private_key}\n", encoding="utf-8")
        key_path.chmod(0o600)
    else:
        key_path.chmod(0o600)
    return key_path


def derive_public_key(private_key_path: Path) -> str:
    return run_command(["wg", "pubkey"], input_text=private_key_path.read_text())


def interface_exists(interface_name: str) -> bool:
    result = subprocess.run(
        ["ip", "link", "show", interface_name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def apply_interface(interface_name: str) -> None:
    if interface_exists(interface_name):
        subprocess.run(["wg-quick", "down", interface_name], check=False)
    run_command(["wg-quick", "up", interface_name])


def remove_interface(interface_name: str) -> None:
    if interface_exists(interface_name):
        subprocess.run(["wg-quick", "down", interface_name], check=False)

    runtime_path = WG_CONFIG_DIR / f"{interface_name}.conf"
    rendered_path = WG_RENDERED_DIR / f"{interface_name}.conf"
    if runtime_path.exists():
        runtime_path.unlink()
    if rendered_path.exists():
        rendered_path.unlink()


def current_status(interface_name: str) -> str:
    try:
        return run_command(["wg", "show", interface_name])
    except subprocess.CalledProcessError as exc:
        return (exc.stderr or "").strip()


def verify_interface_live(interface_name: str) -> str:
    if not interface_exists(interface_name):
        raise RuntimeError(f"wireguard interface '{interface_name}' is not present")

    status = current_status(interface_name)
    if not status or "Unable to access interface" in status:
        raise RuntimeError(f"wireguard interface '{interface_name}' is not active")

    return status


def upsert_runtime_status(
    conn,
    network_id: int,
    interface_name: str,
    status: str,
    public_key: str | None,
    raw_status: str | None,
    details: dict | None = None,
    last_error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sensos.runtime_wireguard_status
                (component, role, network_id, interface_name, status, public_key, raw_status, details, last_error, updated_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, NOW())
            ON CONFLICT (component, network_id) DO UPDATE SET
                interface_name = EXCLUDED.interface_name,
                status = EXCLUDED.status,
                public_key = EXCLUDED.public_key,
                raw_status = EXCLUDED.raw_status,
                details = EXCLUDED.details,
                last_error = EXCLUDED.last_error,
                updated_at = NOW();
            """,
            (COMPONENT, ROLE, network_id, interface_name, status, public_key, raw_status, json_dumps(details or {}), last_error),
        )


def json_dumps(value: dict) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def ensure_proxy_peer(conn, network_id: int, proxy_ip: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM sensos.wireguard_peers
            WHERE network_id = %s AND wg_ip = %s;
            """,
            (network_id, proxy_ip),
        )
        row = cur.fetchone()
        if row:
            return row[0]

        cur.execute(
            """
            INSERT INTO sensos.wireguard_peers (network_id, wg_ip, note)
            VALUES (%s, %s, %s)
            RETURNING id;
            """,
            (network_id, proxy_ip, "API Proxy Container"),
        )
        return cur.fetchone()[0]


def ensure_active_proxy_key(conn, peer_id: int, public_key: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT wg_public_key
            FROM sensos.wireguard_keys
            WHERE peer_id = %s AND is_active = TRUE
            ORDER BY created_at DESC
            LIMIT 1;
            """,
            (peer_id,),
        )
        row = cur.fetchone()
        if row and row[0] == public_key:
            return

        cur.execute(
            "UPDATE sensos.wireguard_keys SET is_active = FALSE WHERE peer_id = %s;",
            (peer_id,),
        )
        cur.execute(
            """
            INSERT INTO sensos.wireguard_keys (peer_id, wg_public_key, is_active)
            VALUES (%s, %s, TRUE);
            """,
            (peer_id, public_key),
        )


def render_interface_config(
    private_key_path: Path,
    proxy_ip: str,
    server_public_key: str,
    server_listen_port: int,
    ip_range: str,
) -> str:
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key_path.read_text(encoding='utf-8').strip()}\n"
        f"Address = {proxy_ip}/32\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {server_public_key}\n"
        f"Endpoint = sensos-wireguard:{server_listen_port}\n"
        f"AllowedIPs = {ip_range}\n"
        "PersistentKeepalive = 25\n"
    )


def reconcile_network(
    conn,
    network_id: int,
    name: str,
    ip_range_cidr: str,
    server_public_key: str,
    server_listen_port: int,
) -> None:
    ip_range = ipaddress.ip_network(ip_range_cidr, strict=False)
    proxy_ip = str(ip_range.network_address + 1)
    private_key_path = ensure_private_key(name)
    public_key = derive_public_key(private_key_path)
    peer_id = ensure_proxy_peer(conn, network_id, proxy_ip)
    ensure_active_proxy_key(conn, peer_id, public_key)

    rendered = render_interface_config(
        private_key_path=private_key_path,
        proxy_ip=proxy_ip,
        server_public_key=server_public_key,
        server_listen_port=server_listen_port,
        ip_range=str(ip_range),
    )
    rendered_path = WG_RENDERED_DIR / f"{name}.conf"
    runtime_path = WG_CONFIG_DIR / f"{name}.conf"
    rendered_path.write_text(rendered, encoding="utf-8")
    rendered_path.chmod(0o600)

    needs_apply = (
        not runtime_path.exists()
        or runtime_path.read_text(encoding="utf-8") != rendered
        or not interface_exists(name)
    )
    if needs_apply:
        shutil.copyfile(rendered_path, runtime_path)
        runtime_path.chmod(0o600)
        apply_interface(name)

    live_status = verify_interface_live(name)
    upsert_runtime_status(
        conn,
        network_id=network_id,
        interface_name=name,
        status="ready",
        public_key=public_key,
        raw_status=live_status,
        details={},
        last_error=None,
    )


def mark_error(conn, network_id: int, interface_name: str, public_key: str | None, exc: Exception) -> None:
    upsert_runtime_status(
        conn,
        network_id=network_id,
        interface_name=interface_name,
        status="error",
        public_key=public_key,
        raw_status=current_status(interface_name),
        details={},
        last_error=str(exc),
    )


def cleanup_removed_networks(conn, active_names: set[str], active_ids: set[int]) -> None:
    for config_path in sorted(WG_CONFIG_DIR.glob("*.conf")):
        if config_path.stem not in active_names:
            remove_interface(config_path.stem)

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM sensos.runtime_wireguard_status
            WHERE component = %s
              AND network_id <> ALL(%s::int[]);
            """,
            (COMPONENT, list(active_ids) or [0]),
        )


def reconcile_all() -> None:
    ensure_dirs()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.id,
                       n.name,
                       n.ip_range,
                       n.wg_public_key,
                       rs.details->>'listen_port'
                FROM sensos.networks n
                JOIN sensos.runtime_wireguard_status rs
                  ON rs.network_id = n.id
                 AND rs.component = %s
                WHERE n.wg_public_key IS NOT NULL
                  AND rs.details ? 'listen_port'
                ORDER BY name;
                """,
                (SERVER_COMPONENT,),
            )
            networks = cur.fetchall()

        active_names = {name for _, name, _, _, _ in networks}
        active_ids = {network_id for network_id, _, _, _, _ in networks}
        cleanup_removed_networks(conn, active_names, active_ids)

        for network_id, name, ip_range, server_public_key, server_listen_port in networks:
            public_key = None
            try:
                private_key_path = ensure_private_key(name)
                public_key = derive_public_key(private_key_path)
                reconcile_network(
                    conn,
                    network_id=network_id,
                    name=name,
                    ip_range_cidr=str(ip_range),
                    server_public_key=server_public_key,
                    server_listen_port=int(server_listen_port),
                )
            except Exception as exc:
                log(f"failed to reconcile {name}: {exc}")
                mark_error(conn, network_id, name, public_key, exc)


if __name__ == "__main__":
    reconcile_all()
