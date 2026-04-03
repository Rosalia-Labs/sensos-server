# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

from unittest import mock

import api
import pytest

from fastapi import FastAPI
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

from api import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[api.authenticate] = lambda: HTTPBasicCredentials(
        username="test", password="test"
    )
    return TestClient(app)


def test_dashboard_success(monkeypatch, client):
    fake_cur = mock.MagicMock()

    def execute_side_effect(query, *args, **kwargs):
        if "FROM sensos.version_history" in query:
            fake_cur.fetchone.return_value = (
                1,
                "1",
                "0",
                "0",
                None,
                "abcdef",
                "main",
                "v1.0.0",
                "false",
                "2024-01-01T00:00:00Z",
            )
        elif "FROM sensos.networks" in query:
            fake_cur.fetchall.return_value = [("network1", "10.0.0.0/16", "1.2.3.4", 51820)]

    fake_cur.execute.side_effect = execute_side_effect
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Sensor Network Manager" in resp.text


def test_create_network_invalid_port(client):
    resp = client.post(
        "/create-network",
        data={"name": "test", "wg_public_ip": "1.2.3.4", "wg_port": "99999"},
    )
    assert resp.status_code == 400
    assert "Invalid WireGuard port" in resp.text


def test_create_network_waits_for_readiness(monkeypatch, client):
    monkeypatch.setattr(
        api,
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
        api,
        "wait_for_network_ready",
        lambda name: (1, "10.0.0.0/16", "server-pubkey", "1.2.3.4", 51820),
    )
    mock_conn = mock.MagicMock()
    monkeypatch.setattr(api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.post(
        "/create-network",
        data={"name": "test", "wg_public_ip": "1.2.3.4", "wg_port": "51820"},
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

    monkeypatch.setattr(api, "create_network_entry", fake_create_network_entry)
    monkeypatch.setattr(
        api,
        "wait_for_network_ready",
        lambda name: (1, "10.0.0.0/16", "server-pubkey", "1.2.3.4", 51281),
    )
    mock_conn = mock.MagicMock()
    monkeypatch.setattr(api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.post(
        "/create-network",
        data={"name": "test", "wg_public_ip": "1.2.3.4"},
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

    monkeypatch.setattr(api, "create_network_entry", fake_create_network_entry)
    mock_conn = mock.MagicMock()
    monkeypatch.setattr(api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.post(
        "/create-network",
        data={"name": "test", "wg_public_ip": "server.example.org", "wg_port": "51820"},
    )
    assert resp.status_code == 200
    assert captured["wg_public_ip"] == "server.example.org"
    assert resp.json()["wg_public_ip"] == "server.example.org"


def test_register_peer_invalid_subnet(monkeypatch, client):
    monkeypatch.setattr(
        api,
        "get_network_details",
        lambda name: (1, "10.0.0.0/16", "pubkey", "1.2.3.4", 51820),
    )

    resp = client.post("/register-peer", json={"network_name": "test", "subnet_offset": 9999})
    assert resp.status_code == 400


def test_register_peer_defaults_to_first_client_subnet(monkeypatch, client):
    monkeypatch.setattr(
        api,
        "get_network_details",
        lambda name: (1, "10.0.0.0/16", "pubkey", "1.2.3.4", 51820),
    )
    monkeypatch.setattr(api, "insert_peer", lambda network_id, wg_ip, note=None: (123, "peer-uuid"))

    captured = {}

    def fake_search(subnet, network_id, start_third_octet=1):
        captured["start_third_octet"] = start_third_octet
        return "10.0.1.1"

    monkeypatch.setattr(api, "search_for_next_available_ip", fake_search)

    resp = client.post("/register-peer", json={"network_name": "test"})
    assert resp.status_code == 200
    assert captured["start_third_octet"] == 1
    assert resp.json()["wg_ip"] == "10.0.1.1"


def test_register_wireguard_key_not_found(monkeypatch, client):
    monkeypatch.setattr(api, "register_wireguard_key_in_db", lambda wg_ip, pubkey: None)
    resp = client.post(
        "/register-wireguard-key",
        json={"wg_ip": "10.0.0.2", "wg_public_key": "dummy"},
    )
    assert resp.status_code == 404


def test_set_peer_active_updates_inventory_state(monkeypatch, client):
    captured = {}

    def fake_set_peer_active_state(wg_ip, is_active):
        captured["wg_ip"] = wg_ip
        captured["is_active"] = is_active
        return True

    monkeypatch.setattr(api, "set_peer_active_state", fake_set_peer_active_state)

    resp = client.post("/set-peer-active", json={"wg_ip": "10.0.1.7", "is_active": False})
    assert resp.status_code == 200
    assert captured == {"wg_ip": "10.0.1.7", "is_active": False}
    assert resp.json() == {"wg_ip": "10.0.1.7", "is_active": False}


def test_delete_peer_purges_inventory_entry(monkeypatch, client):
    monkeypatch.setattr(api, "delete_peer", lambda wg_ip: wg_ip == "10.0.1.7")

    resp = client.post("/delete-peer", json={"wg_ip": "10.0.1.7"})
    assert resp.status_code == 200
    assert resp.json() == {"wg_ip": "10.0.1.7", "deleted": True}


def test_delete_peer_returns_not_found(monkeypatch, client):
    monkeypatch.setattr(api, "delete_peer", lambda wg_ip: False)

    resp = client.post("/delete-peer", json={"wg_ip": "10.0.1.99"})
    assert resp.status_code == 404


def test_client_status_accepts_wireguard_ip_payload(monkeypatch, client):
    fake_cur = mock.MagicMock()
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))
    monkeypatch.setattr(api, "lookup_peer_id", lambda conn, wireguard_ip: 123 if wireguard_ip == "10.0.1.7" else None)

    resp = client.post(
        "/client-status",
        json={
            "wireguard_ip": "10.0.1.7",
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


def test_get_defined_networks_requires_authentication(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    unauthenticated_client = TestClient(app)

    resp = unauthenticated_client.get("/get-wireguard-network-names")
    assert resp.status_code == 401


def test_get_defined_networks_returns_names(monkeypatch, client):
    fake_cur = mock.MagicMock()
    fake_cur.fetchall.return_value = [("testing",), ("biosense",)]
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.get("/get-wireguard-network-names")
    assert resp.status_code == 200
    assert resp.json() == {"networks": ["testing", "biosense"]}


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
    monkeypatch.setattr(api, "get_db", lambda: mock.MagicMock(__enter__=lambda _: mock_conn))

    resp = client.get("/wireguard-status")
    assert resp.status_code == 200
    assert "sensos-wireguard" in resp.text
    assert "network1" in resp.text
