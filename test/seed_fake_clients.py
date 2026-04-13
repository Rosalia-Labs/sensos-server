#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rosalia Labs LLC

import argparse
import base64
import json
import random
import secrets
import subprocess
import sys
import uuid

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib import error, request


DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_DB_CONTAINER = "sensos-database"
DEFAULT_NETWORK_PREFIX = "fake-seed"
DEFAULT_CLIENT_VERSION = "fake-client/0.1.0"
DEFAULT_POSTGRES_PASSWORD = "sensos"

BIRD_SPECIES = [
    "Northern Cardinal",
    "Carolina Wren",
    "Blue Jay",
    "American Robin",
    "Mourning Dove",
    "Tufted Titmouse",
    "Red-bellied Woodpecker",
    "Barred Owl",
]

I2C_SENSOR_PROFILES = [
    {
        "device_address": "0x76",
        "sensor_type": "bme280",
        "keys": {
            "temperature_c": (14.0, 33.0),
            "humidity_pct": (35.0, 98.0),
            "pressure_hpa": (995.0, 1027.0),
        },
    },
    {
        "device_address": "0x44",
        "sensor_type": "sht31",
        "keys": {
            "temperature_c": (14.0, 33.0),
            "humidity_pct": (35.0, 98.0),
        },
    },
    {
        "device_address": "0x48",
        "sensor_type": "ads1115",
        "keys": {
            "battery_v": (3.55, 4.18),
            "solar_v": (0.2, 6.2),
        },
    },
]


@dataclass(frozen=True)
class PeerSeed:
    hostname: str
    note: str
    wg_ip: str
    peer_uuid: str
    peer_password: str
    latitude: float
    longitude: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed fake SensOS clients, locations, I2C, and BirdNET data."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--db-container", default=DEFAULT_DB_CONTAINER)
    parser.add_argument(
        "--postgres-password",
        default=None,
        help="PostgreSQL password for docker exec psql. Defaults to $POSTGRES_PASSWORD or 'sensos'.",
    )
    parser.add_argument("--tag", default=None, help="Dataset tag. Random when omitted.")
    parser.add_argument(
        "--network-prefix",
        default=DEFAULT_NETWORK_PREFIX,
        help="Prefix for the synthetic network name.",
    )
    parser.add_argument(
        "--client-count", type=int, default=4, help="Number of fake clients to create."
    )
    parser.add_argument(
        "--i2c-batches-per-client", type=int, default=3, help="I2C upload batches per client."
    )
    parser.add_argument(
        "--birdnet-batches-per-client",
        type=int,
        default=2,
        help="BirdNET upload batches per client.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete the tagged dataset instead of creating it.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete any existing dataset for the same tag before seeding.",
    )
    parser.add_argument(
        "--print-creds",
        action="store_true",
        help="Print peer UUID/password pairs for manual API testing.",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="Random seed for repeatable payload generation."
    )
    return parser.parse_args()


def hash_peer_api_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = sha256(salt + password.encode("utf-8")).hexdigest()
    return f"{base64.b64encode(salt).decode('ascii')}:{digest}"


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def sql_quote(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def run_psql(db_container: str, postgres_password: str, sql_text: str) -> str:
    cmd = [
        "docker",
        "exec",
        "-i",
        "-e",
        f"PGPASSWORD={postgres_password}",
        db_container,
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        "postgres",
        "-At",
        "-F",
        "\t",
    ]
    result = subprocess.run(
        cmd,
        input=sql_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "psql failed")
    return result.stdout.strip()


def api_call(
    method: str,
    url: str,
    payload: dict | None = None,
    username: str | None = None,
    password: str | None = None,
) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if username is not None and password is not None:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    req = request.Request(url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc
    if not body:
        return {}
    return json.loads(body)


def choose_network_second_octet(
    db_container: str,
    postgres_password: str,
    preferred: int,
) -> int:
    output = run_psql(
        db_container,
        postgres_password,
        "SELECT split_part(host(ip_range), '.', 2) FROM sensos.networks ORDER BY 1;\n",
    )
    used = {
        int(line.strip())
        for line in output.splitlines()
        if line.strip().isdigit()
    }
    for offset in range(256):
        candidate = (preferred + offset) % 256
        if candidate in (0, 1):
            continue
        if candidate not in used:
            return candidate
    raise RuntimeError("no free 10.x.0.0/16 range remains")


def cleanup_dataset(
    db_container: str,
    postgres_password: str,
    network_name: str,
) -> str:
    sql_text = f"""
BEGIN;
WITH target_peers AS (
    SELECT p.wg_ip::text AS wg_ip
    FROM sensos.wireguard_peers p
    JOIN sensos.networks n ON n.id = p.network_id
    WHERE n.name = {sql_quote(network_name)}
)
DELETE FROM sensos.birdnet_result_batches
WHERE wireguard_ip::text IN (SELECT wg_ip FROM target_peers);

WITH target_peers AS (
    SELECT p.wg_ip::text AS wg_ip
    FROM sensos.wireguard_peers p
    JOIN sensos.networks n ON n.id = p.network_id
    WHERE n.name = {sql_quote(network_name)}
)
DELETE FROM sensos.i2c_reading_batches
WHERE wireguard_ip::text IN (SELECT wg_ip FROM target_peers);

DELETE FROM sensos.networks
WHERE name = {sql_quote(network_name)};
COMMIT;
SELECT 'cleanup complete for {network_name}';
"""
    return run_psql(db_container, postgres_password, sql_text)


def create_network_and_peers(
    db_container: str,
    postgres_password: str,
    network_name: str,
    client_count: int,
    second_octet: int,
    rng: random.Random,
) -> list[PeerSeed]:
    peers: list[PeerSeed] = []
    for index in range(client_count):
        client_num = index + 1
        peers.append(
            PeerSeed(
                hostname=f"{network_name}-client-{client_num:02d}",
                note=f"{network_name} site {client_num:02d}",
                wg_ip=f"10.{second_octet}.1.{client_num}",
                peer_uuid=str(uuid.uuid4()),
                peer_password=secrets.token_urlsafe(24),
                latitude=round(29.15 + rng.uniform(-4.2, 4.2), 6),
                longitude=round(-91.8 + rng.uniform(-6.5, 6.5), 6),
            )
        )

    peer_rows = ",\n".join(
        "("
        + ", ".join(
            [
                "(SELECT id FROM inserted_network)",
                sql_quote(peer.wg_ip),
                sql_quote(peer.note),
                sql_quote(hash_peer_api_password(peer.peer_password)),
                "TRUE",
                sql_quote(datetime.now(timezone.utc).isoformat()),
                sql_quote(peer.peer_uuid),
            ]
        )
        + ")"
        for peer in peers
    )
    public_ip = f"{network_name}.invalid"
    public_key = (
        "fake-network-key-"
        + base64.b64encode(sha256(network_name.encode("utf-8")).digest()).decode("ascii")
    )
    wg_port = 52000 + (second_octet % 1000)
    sql_text = f"""
BEGIN;
WITH inserted_network AS (
    INSERT INTO sensos.networks (name, ip_range, wg_public_ip, wg_port, wg_public_key)
    VALUES (
        {sql_quote(network_name)},
        {sql_quote(f"10.{second_octet}.0.0/16")},
        {sql_quote(public_ip)},
        {sql_quote(wg_port)},
        {sql_quote(public_key)}
    )
    RETURNING id
)
INSERT INTO sensos.wireguard_peers
    (network_id, wg_ip, note, api_password_hash, is_active, registered_at, uuid)
VALUES
{peer_rows};
COMMIT;
SELECT 'seeded network {network_name}';
"""
    run_psql(db_container, postgres_password, sql_text)
    return peers


def build_status_payload(peer: PeerSeed, rng: random.Random) -> dict:
    memory_total = rng.choice([1024, 2048, 4096, 8192])
    memory_used = int(memory_total * rng.uniform(0.25, 0.82))
    return {
        "hostname": peer.hostname,
        "uptime_seconds": rng.randint(2 * 3600, 45 * 86400),
        "disk_available_gb": round(rng.uniform(4.0, 118.0), 2),
        "memory_used_mb": memory_used,
        "memory_total_mb": memory_total,
        "load_1m": round(rng.uniform(0.05, 2.9), 2),
        "load_5m": round(rng.uniform(0.05, 2.2), 2),
        "load_15m": round(rng.uniform(0.05, 1.8), 2),
        "version": DEFAULT_CLIENT_VERSION,
        "status_message": rng.choice(
            [
                "field test nominal",
                "collecting dawn chorus",
                "solar charging",
                "high humidity overnight",
                "uplink healthy",
            ]
        ),
    }


def build_i2c_payloads(
    peer: PeerSeed,
    batch_count: int,
    rng: random.Random,
) -> list[dict]:
    payloads: list[dict] = []
    reading_id = 0
    now = datetime.now(timezone.utc)
    for batch_index in range(batch_count):
        batch_time = now - timedelta(hours=(batch_count - batch_index) * 3)
        readings = []
        for sensor_profile in I2C_SENSOR_PROFILES:
            for key, bounds in sensor_profile["keys"].items():
                reading_id += 1
                timestamp = batch_time + timedelta(seconds=len(readings) * 17)
                readings.append(
                    {
                        "id": reading_id,
                        "timestamp": iso_utc(timestamp),
                        "device_address": sensor_profile["device_address"],
                        "sensor_type": sensor_profile["sensor_type"],
                        "key": key,
                        "value": round(rng.uniform(bounds[0], bounds[1]), 3),
                    }
                )
        payloads.append(
            {
                "schema_version": 1,
                "hostname": peer.hostname,
                "client_version": DEFAULT_CLIENT_VERSION,
                "batch_id": batch_index + 1,
                "sent_at": iso_utc(batch_time + timedelta(minutes=4)),
                "ownership_mode": rng.choice(["client-retains", "server-owns"]),
                "reading_count": len(readings),
                "first_reading_id": readings[0]["id"],
                "last_reading_id": readings[-1]["id"],
                "first_recorded_at": readings[0]["timestamp"],
                "last_recorded_at": readings[-1]["timestamp"],
                "readings": readings,
            }
        )
    return payloads


def build_birdnet_payloads(
    peer: PeerSeed,
    batch_count: int,
    rng: random.Random,
) -> list[dict]:
    payloads: list[dict] = []
    now = datetime.now(timezone.utc)
    for batch_index in range(batch_count):
        processed_files = []
        batch_time = now - timedelta(hours=(batch_count - batch_index) * 6)
        file_count = rng.randint(2, 4)
        for file_index in range(file_count):
            started_at = batch_time + timedelta(minutes=file_index * 11)
            processed_at = started_at + timedelta(minutes=2, seconds=rng.randint(10, 70))
            sample_rate = 48_000
            frames = sample_rate * 12
            detections = []
            flac_runs = []
            detection_count = rng.randint(2, 5)
            for detection_index in range(detection_count):
                start_sec = round(detection_index * 2.4 + rng.uniform(0.0, 0.8), 3)
                end_sec = round(start_sec + rng.uniform(0.8, 1.9), 3)
                label = rng.choice(BIRD_SPECIES)
                top_score = round(rng.uniform(0.72, 0.99), 4)
                likely_score = round(max(0.0, top_score - rng.uniform(0.03, 0.12)), 4)
                start_frame = int(start_sec * sample_rate)
                end_frame = int(end_sec * sample_rate)
                detections.append(
                    {
                        "channel_index": 0,
                        "window_index": detection_index,
                        "start_frame": start_frame,
                        "end_frame": end_frame,
                        "start_sec": start_sec,
                        "end_sec": end_sec,
                        "window_volume": round(rng.uniform(0.005, 0.2), 4),
                        "top_label": label,
                        "top_score": top_score,
                        "top_likely_score": likely_score,
                    }
                )
                flac_runs.append(
                    {
                        "channel_index": 0,
                        "run_index": detection_index,
                        "label": label,
                        "label_dir": label.lower().replace(" ", "_"),
                        "start_frame": start_frame,
                        "end_frame": end_frame,
                        "start_sec": start_sec,
                        "end_sec": end_sec,
                        "peak_score": top_score,
                        "peak_likely_score": likely_score,
                        "flac_path": (
                            f"/var/lib/sensos/birdnet/snippets/{peer.hostname}/"
                            f"batch-{batch_index + 1:02d}-file-{file_index + 1:02d}-"
                            f"run-{detection_index + 1:02d}.flac"
                        ),
                        "deleted_at": None,
                    }
                )
            processed_files.append(
                {
                    "source_path": (
                        f"/var/lib/sensos/audio/{peer.hostname}/"
                        f"batch-{batch_index + 1:02d}-clip-{file_index + 1:02d}.wav"
                    ),
                    "sample_rate": sample_rate,
                    "channels": 1,
                    "frames": frames,
                    "started_at": iso_utc(started_at),
                    "processed_at": iso_utc(processed_at),
                    "status": "done",
                    "error": None,
                    "output_dir": f"/var/lib/sensos/birdnet/out/{peer.hostname}",
                    "deleted_source": rng.choice([True, False]),
                    "detections": detections,
                    "flac_runs": flac_runs,
                }
            )
        source_paths = sorted(item["source_path"] for item in processed_files)
        processed_times = sorted(item["processed_at"] for item in processed_files)
        payloads.append(
            {
                "schema_version": 1,
                "hostname": peer.hostname,
                "client_version": DEFAULT_CLIENT_VERSION,
                "batch_id": batch_index + 1,
                "sent_at": iso_utc(batch_time + timedelta(minutes=5)),
                "ownership_mode": rng.choice(["client-retains", "server-owns"]),
                "source_count": len(processed_files),
                "first_source_path": source_paths[0],
                "last_source_path": source_paths[-1],
                "first_processed_at": processed_times[0],
                "last_processed_at": processed_times[-1],
                "processed_files": processed_files,
            }
        )
    return payloads


def seed_peer(base_url: str, peer: PeerSeed, args: argparse.Namespace, rng: random.Random) -> None:
    api_call(
        "POST",
        f"{base_url}/api/v1/client/peer/status",
        build_status_payload(peer, rng),
        peer.peer_uuid,
        peer.peer_password,
    )
    api_call(
        "PUT",
        f"{base_url}/api/v1/client/peer/location",
        {"latitude": peer.latitude, "longitude": peer.longitude},
        peer.peer_uuid,
        peer.peer_password,
    )
    for payload in build_i2c_payloads(peer, args.i2c_batches_per_client, rng):
        api_call(
            "POST",
            f"{base_url}/api/v1/client/peer/i2c-readings/batches",
            payload,
            peer.peer_uuid,
            peer.peer_password,
        )
    for payload in build_birdnet_payloads(peer, args.birdnet_batches_per_client, rng):
        api_call(
            "POST",
            f"{base_url}/api/v1/client/peer/birdnet/batches",
            payload,
            peer.peer_uuid,
            peer.peer_password,
        )


def main() -> int:
    args = parse_args()
    postgres_password = (
        args.postgres_password
        or __import__("os").environ.get("POSTGRES_PASSWORD")
        or DEFAULT_POSTGRES_PASSWORD
    )
    if args.client_count < 1:
        raise SystemExit("--client-count must be at least 1")

    tag = args.tag or secrets.token_hex(4)
    network_name = f"{args.network_prefix}-{tag}"
    rng = random.Random(args.seed if args.seed is not None else tag)

    api_call("GET", f"{args.base_url}/api/v1/client/healthz")

    if args.cleanup:
        message = cleanup_dataset(args.db_container, postgres_password, network_name)
        print(message)
        return 0

    if args.replace:
        cleanup_dataset(args.db_container, postgres_password, network_name)

    preferred_octet = 10 + (sum(ord(ch) for ch in tag) % 220)
    second_octet = choose_network_second_octet(
        args.db_container, postgres_password, preferred_octet
    )
    peers = create_network_and_peers(
        args.db_container,
        postgres_password,
        network_name,
        args.client_count,
        second_octet,
        rng,
    )
    for peer in peers:
        seed_peer(args.base_url.rstrip("/"), peer, args, rng)

    print(
        json.dumps(
            {
                "network_name": network_name,
                "client_count": len(peers),
                "base_url": args.base_url.rstrip("/"),
                "cleanup_command": (
                    f"./test/seed_fake_clients.py --tag {tag} --cleanup "
                    f"--db-container {args.db_container}"
                ),
                "clients": [
                    {
                        "hostname": peer.hostname,
                        "note": peer.note,
                        "wg_ip": peer.wg_ip,
                        "latitude": peer.latitude,
                        "longitude": peer.longitude,
                        **(
                            {
                                "peer_uuid": peer.peer_uuid,
                                "peer_password": peer.peer_password,
                            }
                            if args.print_creds
                            else {}
                        ),
                    }
                    for peer in peers
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
