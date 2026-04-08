# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

from unittest import mock

import admin_api
import api
import client_api
import pytest

from fastapi import FastAPI
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

from api import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.state.schema_ready = True
    app.dependency_overrides[admin_api.authenticate_admin] = lambda: HTTPBasicCredentials(
        username="admin", password="admin"
    )
    app.dependency_overrides[client_api.authenticate_client] = lambda: HTTPBasicCredentials(
        username="sensos", password="test"
    )
    app.dependency_overrides[client_api.authenticate_peer] = (
        lambda: {"peer_id": 123, "peer_uuid": "peer-123", "wg_ip": "10.0.1.7"}
    )
    return TestClient(app)


def test_healthz_reports_starting_before_schema_ready():
    app = FastAPI()
    app.include_router(router)
    app.state.schema_ready = False
    client = TestClient(app)

    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "starting"}


def test_healthz_reports_ok_when_schema_ready():
    app = FastAPI()
    app.include_router(router)
    app.state.schema_ready = True
    client = TestClient(app)

    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_network_invalid_port(client):
    resp = client.post(
        "/api/v1/admin/networks",
        json={"name": "test", "wg_public_ip": "1.2.3.4", "wg_port": 99999},
    )
    assert resp.status_code == 422


def test_create_network_waits_for_readiness(monkeypatch, client):
    monkeypatch.setattr(
        admin_api,
        "create_network_entry",
        lambda cur, name, wg_public_ip, wg_port: (
            {
                "id": 1,
                "name": name,
                "ip_range": "10.0.0.0/16",
                "wg_public_ip": wg_public_ip,
                "wg_port": wg_port,
                "wg_public_key": None,
            },
            True,
        ),
    )
    monkeypatch.setattr(
        admin_api,
        "wait_for_network_ready",
        lambda name: (1, "10.0.0.0/16", "server-pubkey", "1.2.3.4", 51820),
    )
    mock_conn = mock.MagicMock()
    monkeypatch.setattr(admin_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.post(
        "/api/v1/admin/networks",
        json={"name": "test", "wg_public_ip": "1.2.3.4", "wg_port": 51820},
    )
    assert resp.status_code == 200
    assert resp.json()["wg_public_key"] == "server-pubkey"


def test_create_network_without_port_uses_allocator(monkeypatch, client):
    captured = {}

    def fake_create_network_entry(cur, name, wg_public_ip, wg_port):
        captured["wg_port"] = wg_port
        return (
            {
                "id": 1,
                "name": name,
                "ip_range": "10.0.0.0/16",
                "wg_public_ip": wg_public_ip,
                "wg_port": 51281,
                "wg_public_key": None,
            },
            True,
        )

    monkeypatch.setattr(admin_api, "create_network_entry", fake_create_network_entry)
    monkeypatch.setattr(
        admin_api,
        "wait_for_network_ready",
        lambda name: (1, "10.0.0.0/16", "server-pubkey", "1.2.3.4", 51281),
    )
    mock_conn = mock.MagicMock()
    monkeypatch.setattr(admin_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.post(
        "/api/v1/admin/networks",
        json={"name": "test", "wg_public_ip": "1.2.3.4"},
    )
    assert resp.status_code == 200
    assert captured["wg_port"] is None
    assert resp.json()["wg_port"] == 51281


def test_create_network_accepts_hostname_endpoint(monkeypatch, client):
    captured = {}

    def fake_create_network_entry(cur, name, wg_public_ip, wg_port):
        captured["wg_public_ip"] = wg_public_ip
        return (
            {
                "id": 1,
                "name": name,
                "ip_range": "10.0.0.0/16",
                "wg_public_ip": wg_public_ip,
                "wg_port": 51820,
                "wg_public_key": "server-pubkey",
            },
            False,
        )

    monkeypatch.setattr(admin_api, "create_network_entry", fake_create_network_entry)
    mock_conn = mock.MagicMock()
    monkeypatch.setattr(admin_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.post(
        "/api/v1/admin/networks",
        json={"name": "test", "wg_public_ip": "server.example.org", "wg_port": 51820},
    )
    assert resp.status_code == 200
    assert captured["wg_public_ip"] == "server.example.org"
    assert resp.json()["wg_public_ip"] == "server.example.org"


def test_update_network_endpoint_invalid_port(client):
    resp = client.put(
        "/api/v1/admin/networks/test/endpoint",
        json={"wg_public_ip": "10.0.2.2", "wg_port": 99999},
    )
    assert resp.status_code == 422


def test_update_network_endpoint_returns_updated_network(monkeypatch, client):
    captured = {}

    def fake_update_network_endpoint(cur, name, wg_public_ip, wg_port):
        captured["name"] = name
        captured["wg_public_ip"] = wg_public_ip
        captured["wg_port"] = wg_port
        return {
            "id": 1,
            "name": name,
            "ip_range": "10.0.0.0/16",
            "wg_public_ip": wg_public_ip,
            "wg_port": wg_port,
            "wg_public_key": "server-pubkey",
        }

    monkeypatch.setattr(admin_api, "update_network_endpoint", fake_update_network_endpoint)
    mock_conn = mock.MagicMock()
    monkeypatch.setattr(admin_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.put(
        "/api/v1/admin/networks/test/endpoint",
        json={"wg_public_ip": "10.0.2.2", "wg_port": 15182},
    )
    assert resp.status_code == 200
    assert captured["name"] == "test"
    assert captured["wg_public_ip"] == "10.0.2.2"
    assert captured["wg_port"] == 15182


def test_register_peer_invalid_subnet(monkeypatch, client):
    monkeypatch.setattr(
        client_api,
        "get_network_details",
        lambda name: (1, "10.0.0.0/16", "pubkey", "1.2.3.4", 51820),
    )

    resp = client.post("/api/v1/client/peers/enroll", json={"network_name": "test", "subnet_offset": 9999})
    assert resp.status_code == 400


def test_register_peer_defaults_to_first_client_subnet(monkeypatch, client):
    monkeypatch.setattr(
        client_api,
        "get_network_details",
        lambda name: (1, "10.0.0.0/16", "pubkey", "1.2.3.4", 51820),
    )
    monkeypatch.setattr(
        client_api,
        "insert_peer",
        lambda network_id, wg_ip, note=None: (123, "peer-uuid", "peer-secret"),
    )

    captured = {}

    def fake_search(subnet, network_id, start_third_octet=1):
        captured["start_third_octet"] = start_third_octet
        return "10.0.1.1"

    monkeypatch.setattr(client_api, "search_for_next_available_ip", fake_search)

    resp = client.post("/api/v1/client/peers/enroll", json={"network_name": "test"})
    assert resp.status_code == 200
    assert captured["start_third_octet"] == 1
    assert resp.json()["wg_ip"] == "10.0.1.1"
    assert resp.json()["peer_uuid"] == "peer-uuid"
    assert resp.json()["peer_api_password"] == "peer-secret"


def test_register_wireguard_key_not_found(monkeypatch, client):
    monkeypatch.setattr(client_api, "register_wireguard_key_in_db", lambda wg_ip, pubkey: None)
    resp = client.post(
        "/api/v1/client/peer/wireguard-key",
        json={"wg_public_key": "dummy"},
    )
    assert resp.status_code == 404


def test_get_network_info_accepts_client_auth(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.state.schema_ready = True
    app.dependency_overrides[client_api.authenticate_client] = lambda: HTTPBasicCredentials(
        username="client", password="client"
    )
    app.dependency_overrides[admin_api.authenticate_admin] = lambda: (_ for _ in ()).throw(
        AssertionError("admin auth should not be required")
    )
    client = TestClient(app)

    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = (
        "testnet",
        "10.0.0.0/16",
        "server.example.org",
        51820,
        "server-pubkey",
    )
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(client_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.get("/api/v1/client/networks/testnet")
    assert resp.status_code == 200
    assert resp.json() == {
        "name": "testnet",
        "ip_range": "10.0.0.0/16",
        "wg_public_ip": "server.example.org",
        "wg_port": 51820,
        "wg_public_key": "server-pubkey",
    }


def test_set_peer_active_updates_inventory_state(monkeypatch, client):
    captured = {}

    def fake_set_peer_active_state(wg_ip, is_active):
        captured["wg_ip"] = wg_ip
        captured["is_active"] = is_active
        return True

    monkeypatch.setattr(admin_api, "set_peer_active_state", fake_set_peer_active_state)

    resp = client.patch("/api/v1/admin/peers/10.0.1.7/active", json={"wg_ip": "10.0.1.7", "is_active": False})
    assert resp.status_code == 200
    assert captured == {"wg_ip": "10.0.1.7", "is_active": False}
    assert resp.json() == {"wg_ip": "10.0.1.7", "is_active": False}


def test_delete_peer_purges_inventory_entry(monkeypatch, client):
    monkeypatch.setattr(admin_api, "delete_peer", lambda wg_ip: wg_ip == "10.0.1.7")

    resp = client.delete("/api/v1/admin/peers/10.0.1.7")
    assert resp.status_code == 200
    assert resp.json() == {"wg_ip": "10.0.1.7", "deleted": True}


def test_delete_peer_returns_not_found(monkeypatch, client):
    monkeypatch.setattr(admin_api, "delete_peer", lambda wg_ip: False)

    resp = client.delete("/api/v1/admin/peers/10.0.1.99")
    assert resp.status_code == 404


def test_delete_network_cascades_inventory_entry(monkeypatch, client):
    monkeypatch.setattr(admin_api, "delete_network", lambda network_name: network_name == "testing")

    resp = client.delete("/api/v1/admin/networks/testing")
    assert resp.status_code == 200
    assert resp.json() == {"network_name": "testing", "deleted": True}


def test_delete_network_returns_not_found(monkeypatch, client):
    monkeypatch.setattr(admin_api, "delete_network", lambda network_name: False)

    resp = client.delete("/api/v1/admin/networks/missing")
    assert resp.status_code == 404


def test_client_status_uses_authenticated_peer(monkeypatch, client):
    fake_cur = mock.MagicMock()
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(client_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.post(
        "/api/v1/client/peer/status",
        json={
            "hostname": "test-node",
            "uptime_seconds": 42,
            "disk_available_gb": 10.5,
            "memory_used_mb": 256,
            "memory_total_mb": 512,
            "load_1m": 0.1,
            "load_5m": 0.2,
            "load_15m": 0.3,
            "version": "0.5.0",
            "status_message": "OK",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"message": "Client status updated successfully"}
    executed = "\n".join(call.args[0] for call in fake_cur.execute.call_args_list)
    assert "INSERT INTO sensos.client_status" in executed
    assert "peer_id, last_check_in" in executed


def test_exchange_ssh_keys_returns_ops_public_key(monkeypatch, client):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.side_effect = [(321,), ("inserted",)]
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(client_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))
    monkeypatch.setattr(
        client_api,
        "get_runtime_operator_ssh_key",
        lambda: "ssh-ed25519 AAAATEST sensos-ops",
    )

    resp = client.post(
        "/api/v1/client/peer/ssh-key",
        json={
            "username": "sensos-admin",
            "uid": 1000,
            "ssh_public_key": "ssh-ed25519 AAAACLIENT client",
            "key_type": "ssh-ed25519",
            "key_size": 256,
            "key_comment": "client",
            "fingerprint": "SHA256:test",
            "expires_at": None,
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"ssh_public_key": "ssh-ed25519 AAAATEST sensos-ops"}


def test_exchange_ssh_keys_returns_503_when_ops_key_missing(monkeypatch, client):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.side_effect = [(321,), ("inserted",)]
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(client_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))
    monkeypatch.setattr(client_api, "get_runtime_operator_ssh_key", lambda: None)

    resp = client.post(
        "/api/v1/client/peer/ssh-key",
        json={
            "username": "sensos-admin",
            "uid": 1000,
            "ssh_public_key": "ssh-ed25519 AAAACLIENT client",
            "key_type": "ssh-ed25519",
            "key_size": 256,
            "key_comment": "client",
            "fingerprint": "SHA256:test",
            "expires_at": None,
        },
    )

    assert resp.status_code == 503
    assert "Operator SSH public key not published yet" in resp.text


def test_i2c_readings_upload_returns_receipt(monkeypatch, client):
    fake_conn = mock.MagicMock()
    monkeypatch.setattr(client_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: fake_conn))
    monkeypatch.setattr(
        client_api,
        "store_i2c_readings_upload",
        lambda conn, upload, wireguard_ip: {
            "status": "ok",
            "receipt_id": "receipt-123",
            "accepted_count": upload.reading_count,
            "server_received_at": "2026-04-07T12:00:00Z",
        },
    )

    resp = client.post(
        "/api/v1/client/peer/i2c-readings/batches",
        json={
            "schema_version": 1,
            "hostname": "sensor-node",
            "client_version": "1.2.3",
            "batch_id": 41,
            "sent_at": "2026-04-07T11:59:00Z",
            "ownership_mode": "client-retains",
            "reading_count": 2,
            "first_reading_id": 100,
            "last_reading_id": 101,
            "first_recorded_at": "2026-04-07T11:58:00Z",
            "last_recorded_at": "2026-04-07T11:58:05Z",
            "readings": [
                {
                    "id": 100,
                    "timestamp": "2026-04-07T11:58:00Z",
                    "device_address": "0x76",
                    "sensor_type": "BME280",
                    "key": "temperature_c",
                    "value": 23.5,
                },
                {
                    "id": 101,
                    "timestamp": "2026-04-07T11:58:05Z",
                    "device_address": "0x76",
                    "sensor_type": "BME280",
                    "key": "humidity_pct",
                    "value": 51.2,
                },
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "receipt_id": "receipt-123",
        "accepted_count": 2,
        "server_received_at": "2026-04-07T12:00:00Z",
    }


def test_birdnet_results_upload_returns_receipt(monkeypatch, client):
    fake_conn = mock.MagicMock()
    monkeypatch.setattr(client_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: fake_conn))
    monkeypatch.setattr(
        client_api,
        "store_birdnet_results_upload",
        lambda conn, upload, wireguard_ip: {
            "status": "ok",
            "receipt_id": "receipt-456",
            "accepted_count": upload.source_count,
            "server_received_at": "2026-04-07T12:00:00Z",
        },
    )

    resp = client.post(
        "/api/v1/client/peer/birdnet/batches",
        json={
            "schema_version": 1,
            "hostname": "sensor-node",
            "client_version": "1.2.3",
            "batch_id": 41,
            "sent_at": "2026-04-07T11:59:00Z",
            "ownership_mode": "client-retains",
            "source_count": 1,
            "first_source_path": "audio_recordings/compressed/a.flac",
            "last_source_path": "audio_recordings/compressed/a.flac",
            "first_processed_at": "2026-04-07T11:58:00Z",
            "last_processed_at": "2026-04-07T11:58:00Z",
            "processed_files": [
                {
                    "source_path": "audio_recordings/compressed/a.flac",
                    "sample_rate": 48000,
                    "channels": 1,
                    "frames": 144000,
                    "started_at": "2026-04-07T11:57:00Z",
                    "processed_at": "2026-04-07T11:58:00Z",
                    "status": "done",
                    "error": None,
                    "output_dir": "2026/04/07",
                    "deleted_source": True,
                    "detections": [],
                    "flac_runs": [],
                }
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "receipt_id": "receipt-456",
        "accepted_count": 1,
        "server_received_at": "2026-04-07T12:00:00Z",
    }


def test_i2c_readings_upload_validates_metadata(client):
    resp = client.post(
        "/api/v1/client/peer/i2c-readings/batches",
        json={
            "schema_version": 1,
            "hostname": "sensor-node",
            "client_version": "1.2.3",
            "batch_id": 41,
            "sent_at": "2026-04-07T11:59:00Z",
            "ownership_mode": "client-retains",
            "reading_count": 3,
            "first_reading_id": 100,
            "last_reading_id": 101,
            "first_recorded_at": "2026-04-07T11:58:00Z",
            "last_recorded_at": "2026-04-07T11:58:05Z",
            "readings": [
                {
                    "id": 100,
                    "timestamp": "2026-04-07T11:58:00Z",
                    "device_address": "0x76",
                    "sensor_type": "BME280",
                    "key": "temperature_c",
                    "value": 23.5,
                },
                {
                    "id": 101,
                    "timestamp": "2026-04-07T11:58:05Z",
                    "device_address": "0x76",
                    "sensor_type": "BME280",
                    "key": "humidity_pct",
                    "value": 51.2,
                },
            ],
        },
    )

    assert resp.status_code == 422
    assert "reading_count must equal the number of readings" in resp.text


def test_i2c_readings_upload_returns_conflict_for_payload_mismatch(monkeypatch, client):
    fake_conn = mock.MagicMock()
    monkeypatch.setattr(client_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: fake_conn))

    def fail_store(conn, upload, wireguard_ip):
        raise RuntimeError("batch retry payload does not match the previously stored batch")

    monkeypatch.setattr(client_api, "store_i2c_readings_upload", fail_store)

    resp = client.post(
        "/api/v1/client/peer/i2c-readings/batches",
        json={
            "schema_version": 1,
            "hostname": "sensor-node",
            "client_version": "1.2.3",
            "batch_id": 41,
            "sent_at": "2026-04-07T11:59:00Z",
            "ownership_mode": "client-retains",
            "reading_count": 1,
            "first_reading_id": 100,
            "last_reading_id": 100,
            "first_recorded_at": "2026-04-07T11:58:00Z",
            "last_recorded_at": "2026-04-07T11:58:00Z",
            "readings": [
                {
                    "id": 100,
                    "timestamp": "2026-04-07T11:58:00Z",
                    "device_address": "0x76",
                    "sensor_type": "BME280",
                    "key": "temperature_c",
                    "value": 23.5,
                }
            ],
        },
    )

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "batch retry payload does not match the previously stored batch"
    }


def test_get_defined_networks_requires_authentication(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    unauthenticated_client = TestClient(app)

    resp = unauthenticated_client.get("/api/v1/admin/networks")
    assert resp.status_code == 401


def test_i2c_readings_upload_requires_peer_auth(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.state.schema_ready = True
    client = TestClient(app)

    resp = client.post(
        "/api/v1/client/peer/i2c-readings/batches",
        json={
            "schema_version": 1,
            "hostname": "sensor-node",
            "client_version": "1.2.3",
            "batch_id": 41,
            "sent_at": "2026-04-07T11:59:00Z",
            "ownership_mode": "client-retains",
            "reading_count": 1,
            "first_reading_id": 100,
            "last_reading_id": 100,
            "first_recorded_at": "2026-04-07T11:58:00Z",
            "last_recorded_at": "2026-04-07T11:58:00Z",
            "readings": [
                {
                    "id": 100,
                    "timestamp": "2026-04-07T11:58:00Z",
                    "device_address": "0x76",
                    "sensor_type": "BME280",
                    "key": "temperature_c",
                    "value": 23.5,
                }
            ],
        },
    )

    assert resp.status_code == 401


def test_client_credentials_cannot_access_admin_route(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.state.schema_ready = True
    app.dependency_overrides[client_api.authenticate_client] = lambda: HTTPBasicCredentials(
        username="client", password="client"
    )
    client = TestClient(app)

    resp = client.get("/api/v1/admin/networks")
    assert resp.status_code == 401


def test_get_defined_networks_returns_names(monkeypatch, client):
    fake_cur = mock.MagicMock()
    fake_cur.fetchall.return_value = [
        ("testing", "10.0.0.0/16", "server.example.org", 51820, "pubkey-1"),
        ("biosense", "10.1.0.0/16", "server.example.org", 51821, "pubkey-2"),
    ]
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(admin_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.get("/api/v1/admin/networks")
    assert resp.status_code == 200
    assert resp.json() == {
        "networks": [
            {"name": "testing", "ip_range": "10.0.0.0/16", "wg_public_ip": "server.example.org", "wg_port": 51820, "wg_public_key": "pubkey-1"},
            {"name": "biosense", "ip_range": "10.1.0.0/16", "wg_public_ip": "server.example.org", "wg_port": 51821, "wg_public_key": "pubkey-2"},
        ]
    }


def test_wireguard_status_uses_database_rows(monkeypatch, client):
    fake_cur = mock.MagicMock()

    def execute_side_effect(query, *args, **kwargs):
        if "FROM sensos.runtime_wireguard_status" in query:
            fake_cur.fetchall.return_value = [
                (
                    "sensos-wireguard",
                    "server",
                    "network1",
                    "network1",
                    "ready",
                    "server-pubkey",
                    "interface: network1\npeer: peerpub\n  allowed ips: 10.0.0.2/32\n",
                    None,
                    "2026-04-02T12:00:00Z",
                )
            ]

    fake_cur.execute.side_effect = execute_side_effect
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(admin_api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.get("/api/v1/admin/runtime/wireguard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["components"][0]["component"] == "sensos-wireguard"
    assert body["components"][0]["network_name"] == "network1"
