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


def test_register_peer_invalid_subnet(monkeypatch, client):
    monkeypatch.setattr(
        api,
        "get_network_details",
        lambda name: (1, "10.0.0.0/16", "pubkey", "1.2.3.4", 51820),
    )

    resp = client.post("/register-peer", json={"network_name": "test", "subnet_offset": 9999})
    assert resp.status_code == 400


def test_register_wireguard_key_not_found(monkeypatch, client):
    monkeypatch.setattr(api, "register_wireguard_key_in_db", lambda wg_ip, pubkey: None)
    resp = client.post(
        "/register-wireguard-key",
        json={"wg_ip": "10.0.0.2", "wg_public_key": "dummy"},
    )
    assert resp.status_code == 404


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
