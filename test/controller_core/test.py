# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import ipaddress
import socket
from unittest import mock

import psycopg
import pytest

import core

from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPBasicCredentials


def test_generate_default_ip_range():
    assert core.generate_default_ip_range("network1").subnet_of(
        ipaddress.ip_network("10.0.0.0/8")
    )
    assert core.generate_default_ip_range("network1") != core.generate_default_ip_range(
        "network2"
    )


def test_resolve_hostname_ip_direct():
    assert core.resolve_hostname("8.8.8.8") == "8.8.8.8"
    assert core.resolve_hostname("::1") == "::1"


def test_resolve_hostname_dns(monkeypatch):
    def fake_getaddrinfo(host, port, family):
        return [(socket.AF_INET, None, None, None, ("93.184.216.34", None))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert core.resolve_hostname("example.com") == "93.184.216.34"


@mock.patch("core.get_assigned_ips", return_value=set())
def test_search_for_next_available_ip_reserves_only_proxy(mock_get_assigned):
    ip = core.search_for_next_available_ip("10.254.0.0/16", network_id=42)
    assert ip == ipaddress.ip_address("10.254.0.2")


@mock.patch(
    "core.get_assigned_ips",
    return_value={ipaddress.ip_address(f"10.254.0.{i}") for i in range(1, 10)},
)
def test_search_for_next_available_ip_skips_used_hosts(mock_get_assigned):
    ip = core.search_for_next_available_ip("10.254.0.0/16", network_id=42)
    assert ip == ipaddress.ip_address("10.254.0.10")


@mock.patch("core.get_db")
def test_get_network_details_found(mock_get_db):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = (1, "10.0.0.0/16", "pubkey", "1.2.3.4", 51820)
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    assert core.get_network_details("network1") == (
        1,
        "10.0.0.0/16",
        "pubkey",
        "1.2.3.4",
        51820,
    )


@mock.patch("core.get_db")
def test_insert_peer_success(mock_get_db):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = (123, "some-uuid")
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    assert core.insert_peer(1, "10.0.0.2", note="test note") == (123, "some-uuid")


@pytest.mark.asyncio
@mock.patch("core.get_db")
async def test_lifespan_runs_schema_setup(mock_get_db):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.side_effect = [None, None]
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    async with core.lifespan(FastAPI()):
        pass

    executed = "\n".join(call.args[0] for call in fake_cur.execute.call_args_list)
    assert "CREATE SCHEMA IF NOT EXISTS sensos;" in executed
    assert 'CREATE EXTENSION IF NOT EXISTS "pgcrypto" WITH SCHEMA public;' in executed
    assert "CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.runtime_wireguard_status" in executed


@mock.patch("core.psycopg.connect")
def test_get_db_retries_and_fails(mock_connect):
    mock_connect.side_effect = psycopg.OperationalError()
    with pytest.raises(psycopg.OperationalError):
        core.get_db(retries=3, delay=0)
    assert mock_connect.call_count == 3


def test_authenticate_success(monkeypatch):
    monkeypatch.setattr(core, "API_PASSWORD", "secret")
    credentials = HTTPBasicCredentials(username="any", password="secret")
    assert core.authenticate(credentials) == credentials


def test_authenticate_failure(monkeypatch):
    monkeypatch.setattr(core, "API_PASSWORD", "secret")
    credentials = HTTPBasicCredentials(username="any", password="wrongpassword")
    with pytest.raises(HTTPException) as exc_info:
        core.authenticate(credentials)
    assert exc_info.value.status_code == 401


def test_create_network_entry_new():
    mock_cur = mock.MagicMock()
    mock_cur.fetchone.side_effect = [None, (42,)]

    result, created = core.create_network_entry(
        cur=mock_cur,
        name="testnet",
        wg_public_ip="10.0.0.1",
        wg_port=51820,
    )

    assert created is True
    assert result["id"] == 42
    assert result["wg_public_key"] is None


@mock.patch("core.get_db")
def test_wait_for_network_ready_returns_row(mock_get_db):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = (
        1,
        "10.0.0.0/16",
        "server-pubkey",
        "1.2.3.4",
        51820,
    )
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    assert core.wait_for_network_ready("testnet", timeout_seconds=1) == (
        1,
        "10.0.0.0/16",
        "server-pubkey",
        "1.2.3.4",
        51820,
    )
