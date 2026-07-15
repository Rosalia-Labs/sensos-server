# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import ipaddress
import socket
from unittest import mock
from datetime import datetime, timezone

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


def test_allocate_network_ip_range_uses_next_free_range():
    mock_cur = mock.MagicMock()
    preferred = core.generate_default_ip_range("network1")
    next_range = ipaddress.ip_network(
        f"10.{(int(str(preferred.network_address).split('.')[1]) + 1) % 256}.0.0/16"
    )
    mock_cur.fetchall.return_value = [(str(preferred),)]

    allocated = core.allocate_network_ip_range(mock_cur, "network1")
    assert allocated == next_range


def test_allocate_network_ip_range_raises_when_exhausted():
    mock_cur = mock.MagicMock()
    mock_cur.fetchall.return_value = [(f"10.{i}.0.0/16",) for i in range(256)]

    with pytest.raises(RuntimeError, match="no available default 10.x.0.0/16 network ranges remain"):
        core.allocate_network_ip_range(mock_cur, "network1")


def test_resolve_hostname_ip_direct():
    assert core.resolve_hostname("8.8.8.8") == "8.8.8.8"
    assert core.resolve_hostname("::1") == "::1"


def test_resolve_hostname_dns(monkeypatch):
    def fake_getaddrinfo(host, port, family):
        return [(socket.AF_INET, None, None, None, ("93.184.216.34", None))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert core.resolve_hostname("example.com") == "93.184.216.34"


@mock.patch("core.get_assigned_ips", return_value=set())
def test_search_for_next_available_ip_starts_in_first_client_subnet(mock_get_assigned):
    ip = core.search_for_next_available_ip("10.254.0.0/16", network_id=42)
    assert ip == ipaddress.ip_address("10.254.1.1")


@mock.patch(
    "core.get_assigned_ips",
    return_value={ipaddress.ip_address(f"10.254.1.{i}") for i in range(1, 10)},
)
def test_search_for_next_available_ip_skips_used_hosts(mock_get_assigned):
    ip = core.search_for_next_available_ip("10.254.0.0/16", network_id=42)
    assert ip == ipaddress.ip_address("10.254.1.10")


@mock.patch("core.get_assigned_ips", return_value=set())
def test_search_for_next_available_ip_can_start_in_infra_subnet(mock_get_assigned):
    ip = core.search_for_next_available_ip(
        "10.254.0.0/16", network_id=42, start_third_octet=0
    )
    assert ip == ipaddress.ip_address("10.254.0.2")


@mock.patch(
    "core.get_assigned_ips",
    return_value={
        *(ipaddress.ip_address(f"10.254.1.{i}") for i in range(1, 255)),
    },
)
def test_search_for_next_available_ip_skips_dot_zero_and_dot_255(mock_get_assigned):
    ip = core.search_for_next_available_ip("10.254.0.0/16", network_id=42)
    assert ip == ipaddress.ip_address("10.254.2.1")


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

    result = core.insert_peer(1, "10.0.0.2", note="test note")
    assert result[:2] == (123, "some-uuid")
    assert isinstance(result[2], str)
    assert result[2]


@mock.patch("core.get_db")
def test_delete_network_success(mock_get_db):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = (42,)
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    assert core.delete_network("testing") is True
    fake_cur.execute.assert_any_call(
        "DELETE FROM sensos.networks WHERE name = %s RETURNING id;",
        ("testing",),
    )
    mock_conn.commit.assert_called_once()


@mock.patch("core.get_db")
def test_delete_peer_success_relies_on_fk_cascade(mock_get_db):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = (42,)
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    assert core.delete_peer("10.0.1.7") is True
    fake_cur.execute.assert_any_call(
        "SELECT id FROM sensos.wireguard_peers WHERE wg_ip = %s;",
        ("10.0.1.7",),
    )
    fake_cur.execute.assert_any_call(
        "DELETE FROM sensos.wireguard_peers WHERE id = %s;",
        (42,),
    )
    executed_sql = [call.args[0] for call in fake_cur.execute.call_args_list]
    assert all("sensos.i2c_readings" not in sql for sql in executed_sql)
    assert all("sensos.birdnet_detections" not in sql for sql in executed_sql)
    mock_conn.commit.assert_called_once()


@mock.patch("core.get_db")
def test_delete_network_not_found(mock_get_db):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = None
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    assert core.delete_network("missing") is False
    mock_conn.commit.assert_not_called()


def test_parse_version_key_orders_release_after_prerelease():
    assert core.parse_version_key("1.1.2-dev") < core.parse_version_key("1.1.2")


def test_apply_schema_migrations_records_applied_versions():
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.side_effect = [None] * 40
    fake_cur.fetchall.return_value = []

    core.apply_schema_migrations(fake_cur, "0.15.0")

    executed = "\n".join(str(call.args[0]) for call in fake_cur.execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS sensos.schema_migrations" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.runtime_wireguard_status" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.runtime_operator_keys" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.i2c_readings" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.birdnet_detections" in executed
    assert "ADD COLUMN IF NOT EXISTS weighted_label TEXT" in executed
    assert "CREATE OR REPLACE VIEW sensos.public_sites" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.admin_users" in executed
    assert "INSERT INTO sensos.schema_migrations" in executed
    assert "wg_public_ip TEXT NOT NULL" in executed
    assert "peer_id INTEGER REFERENCES sensos.wireguard_peers(id) ON DELETE CASCADE" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.ssh_keys" in executed
    assert "UNIQUE (peer_id, ssh_public_key)" in executed


def test_apply_schema_migrations_runs_0_6_0_after_0_5_0():
    fake_cur = mock.MagicMock()
    fake_cur.fetchall.return_value = [("0.5.0",)]

    core.apply_schema_migrations(fake_cur, "0.15.0")

    executed = "\n".join(str(call.args[0]) for call in fake_cur.execute.call_args_list)
    assert "ALTER COLUMN wg_public_ip TYPE TEXT" in executed
    assert "ADD COLUMN IF NOT EXISTS peer_id INTEGER;" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.runtime_operator_keys" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.i2c_readings" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.birdnet_detections" in executed
    assert "ADD COLUMN IF NOT EXISTS weighted_label TEXT" in executed
    assert "CREATE OR REPLACE VIEW sensos.public_sites" in executed
    insert_calls = [
        call.args[1]
        for call in fake_cur.execute.call_args_list
        if "INSERT INTO sensos.schema_migrations" in call.args[0]
    ]
    assert ("0.6.0", "reconcile legacy network endpoint and client status schema") in insert_calls
    assert ("0.7.0", "add i2c readings upload schema") in insert_calls
    assert ("0.15.0", "add weighted BirdNET label fields and public views") in insert_calls
    assert ("0.8.0", "add per-peer api credentials") in insert_calls
    assert ("0.9.0", "add runtime operator key publication") in insert_calls
    assert ("0.10.0", "add birdnet results upload schema") in insert_calls


def test_apply_schema_migrations_repairs_legacy_birdnet_weighted_fallbacks():
    fake_cur = mock.MagicMock()
    fake_cur.fetchall.return_value = [("0.17.0",)]

    core.apply_schema_migrations(fake_cur, "0.18.0")

    executed = "\n".join(str(call.args[0]) for call in fake_cur.execute.call_args_list)
    assert "SET weighted_label = NULL" in executed
    assert "weighted_score = NULL" in executed
    assert "weighted_likely_score = NULL" in executed
    assert "client_version ~" in executed
    assert "^0\\.[0-9]\\." in executed
    assert "weighted_label = label" in executed
    assert "weighted_score = score" in executed
    insert_calls = [
        call.args[1]
        for call in fake_cur.execute.call_args_list
        if "INSERT INTO sensos.schema_migrations" in call.args[0]
    ]
    assert ("0.18.0", "clear legacy BirdNET weighted label fallbacks") in insert_calls
    assert ("0.11.0", "add public dashboard views and read-only role") in insert_calls


def test_fast_public_site_map_migration_excludes_telemetry_summaries():
    fake_cur = mock.MagicMock()
    fake_cur.fetchall.return_value = [("public_site_map",)]

    core.migrate_0_19_0_fast_public_site_map(fake_cur)

    executed = "\n".join(str(call.args[0]) for call in fake_cur.execute.call_args_list)
    map_view_sql = executed.split(
        "CREATE OR REPLACE VIEW sensos.public_site_map AS", 1
    )[1].split("DROP VIEW", 1)[0]
    assert "sensos.client_status" in map_view_sql
    assert "sensos.peer_locations" in map_view_sql
    assert "sensos.birdnet_detections" not in map_view_sql
    assert "sensos.i2c_readings" not in map_view_sql
    assert "birdnet_detection_count" not in map_view_sql
    assert "row_number() OVER" not in executed
    assert "idx_birdnet_detections_wg_ip_clip_time" in executed
    assert "idx_i2c_readings_wg_ip_recorded_at" in executed
    assert "host(d.wireguard_ip)" not in executed
    assert "host(r.wireguard_ip)" not in executed
    assert "DROP VIEW IF EXISTS sensos.public_sites" in executed


def test_durable_client_identity_migration_backfills_existing_peers():
    fake_cur = mock.MagicMock()

    core.migrate_0_20_0_durable_client_identities(fake_cur)

    executed = "\n".join(str(call.args[0]) for call in fake_cur.execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS sensos.clients" in executed
    assert "access_token_hash TEXT" in executed
    assert "location public.geography(Point, 4326)" in executed
    assert "ADD COLUMN IF NOT EXISTS client_id INTEGER" in executed
    assert "p.api_password_hash" not in executed
    assert "p.note" in executed
    assert "FROM sensos.peer_locations pl" in executed
    assert "ORDER BY pl.recorded_at DESC, pl.id DESC" in executed
    assert "ON CONFLICT (uuid) DO NOTHING" in executed
    assert "SET client_id = c.id" in executed
    assert "wireguard_peers_client_id_fkey" in executed
    assert "REFERENCES sensos.clients(id)" in executed
    assert "ON DELETE CASCADE" in executed
    assert "ALTER COLUMN client_id SET NOT NULL" not in executed
    assert "idx_wireguard_peers_client_id" in executed


def test_issue_client_access_token_stores_hash_and_returns_plaintext(monkeypatch):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = ("client-uuid",)
    fake_conn = mock.MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(core, "get_db", lambda: mock.MagicMock(__enter__=lambda _: fake_conn))
    monkeypatch.setattr(core.secrets, "token_urlsafe", lambda length: "plain-token")

    token = core.issue_client_access_token("client-uuid")

    assert token == "plain-token"
    params = fake_cur.execute.call_args.args[1]
    assert params[1] == "client-uuid"
    assert params[0] != token
    assert core.verify_password(token, params[0])
    assert "is_active = TRUE" in fake_cur.execute.call_args.args[0]
    assert "token_last_used_at = NULL" in fake_cur.execute.call_args.args[0]


def test_create_client_identity_stores_hash_and_returns_plaintext(monkeypatch):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = ("client-uuid",)
    fake_conn = mock.MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(core, "get_db", lambda: mock.MagicMock(__enter__=lambda _: fake_conn))
    monkeypatch.setattr(core.secrets, "token_urlsafe", lambda length: "plain-token")

    client_uuid, token = core.create_client_identity("field sensor")

    assert (client_uuid, token) == ("client-uuid", "plain-token")
    params = fake_cur.execute.call_args.args[1]
    assert params[1] == "field sensor"
    assert params[0] != token
    assert core.verify_password(token, params[0])


@mock.patch("core.get_db")
def test_authenticate_client_enrollment_accepts_server_token(mock_get_db):
    token = "server-issued-token"
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = (456, "client-uuid", core.password_hash(token), None)
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    credentials = HTTPBasicCredentials(username="client-uuid", password=token)
    assert core.authenticate_client_enrollment(credentials) == {
        "client_id": 456,
        "client_uuid": "client-uuid",
        "auth_source": "token",
    }


def test_issue_client_access_token_returns_none_for_unknown_or_inactive_client(monkeypatch):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = None
    fake_conn = mock.MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur
    monkeypatch.setattr(core, "get_db", lambda: mock.MagicMock(__enter__=lambda _: fake_conn))

    assert core.issue_client_access_token("missing-client") is None


def test_schema_migrations_include_durable_client_identity_migration():
    fake_cur = mock.MagicMock()
    fake_cur.fetchall.return_value = [("0.19.0",)]

    core.apply_schema_migrations(fake_cur, "0.20.0")

    insert_calls = [
        call.args[1]
        for call in fake_cur.execute.call_args_list
        if "INSERT INTO sensos.schema_migrations" in call.args[0]
    ]
    assert ("0.20.0", "add durable client identities") in insert_calls


def test_create_client_status_table_reconciles_legacy_schema():
    fake_cur = mock.MagicMock()

    core.create_client_status_table(fake_cur)

    executed = "\n".join(call.args[0] for call in fake_cur.execute.call_args_list)
    assert "ALTER TABLE sensos.client_status" in executed
    assert "ADD COLUMN IF NOT EXISTS peer_id INTEGER;" in executed
    assert "column_name = 'wireguard_ip'" in executed
    assert "SET peer_id = p.id" in executed
    assert "ADD CONSTRAINT client_status_peer_id_fkey" in executed
    assert "CREATE INDEX IF NOT EXISTS idx_client_status_peer_id_last_check_in" in executed


def test_create_networks_table_reconciles_legacy_wg_public_ip_type():
    fake_cur = mock.MagicMock()

    core.create_networks_table(fake_cur)

    executed = "\n".join(call.args[0] for call in fake_cur.execute.call_args_list)
    assert "wg_public_ip TEXT NOT NULL" in executed
    assert "column_name = 'wg_public_ip'" in executed
    assert "data_type = 'inet'" in executed
    assert "ALTER COLUMN wg_public_ip TYPE TEXT" in executed


def test_create_public_sites_view_uses_status_posts_for_last_activity():
    fake_cur = mock.MagicMock()

    core.create_public_sites_view(fake_cur)

    executed = "\n".join(call.args[0] for call in fake_cur.execute.call_args_list)
    assert "CREATE OR REPLACE VIEW sensos.public_sites AS" in executed
    assert "ls.last_check_in AS last_activity_at" in executed
    assert "latest_i2c_upload_at" in executed
    assert "latest_birdnet_upload_at" in executed


@pytest.mark.asyncio
@mock.patch("core.get_db")
async def test_lifespan_runs_schema_setup(mock_get_db):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.side_effect = [None] * 20
    fake_cur.fetchall.return_value = []
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    async with core.lifespan(FastAPI()):
        pass

    executed = "\n".join(str(call.args[0]) for call in fake_cur.execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS sensos.schema_migrations" in executed
    assert "CREATE SCHEMA IF NOT EXISTS sensos;" in executed
    assert 'CREATE EXTENSION IF NOT EXISTS "pgcrypto" WITH SCHEMA public;' in executed
    assert 'CREATE EXTENSION IF NOT EXISTS "postgis" WITH SCHEMA public;' in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.runtime_wireguard_status" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.runtime_operator_keys" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.i2c_readings" in executed
    assert "CREATE TABLE IF NOT EXISTS sensos.birdnet_detections" in executed


@mock.patch("core.get_db")
def test_get_runtime_operator_ssh_key_found(mock_get_db):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = ("ssh-ed25519 AAAATEST sensos-ops",)
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    assert core.get_runtime_operator_ssh_key() == "ssh-ed25519 AAAATEST sensos-ops"


@mock.patch("core.psycopg.connect")
def test_get_db_retries_and_fails(mock_connect):
    mock_connect.side_effect = psycopg.OperationalError()
    with pytest.raises(psycopg.OperationalError):
        core.get_db(retries=3, delay=0)
    assert mock_connect.call_count == 3


def test_authenticate_admin_success(monkeypatch):
    monkeypatch.setattr(core, "ADMIN_API_PASSWORD", "admin-secret")
    credentials = HTTPBasicCredentials(username="sensos", password="admin-secret")
    assert core.authenticate_admin(credentials) == {
        "username": "sensos",
        "display_name": "Bootstrap admin",
        "role": "owner",
        "source": "bootstrap",
    }


@mock.patch("core.get_db")
def test_authenticate_admin_failure(mock_get_db, monkeypatch):
    monkeypatch.setattr(core, "ADMIN_API_PASSWORD", "admin-secret")
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = None
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    credentials = HTTPBasicCredentials(username="other", password="admin-secret")
    with pytest.raises(HTTPException) as exc_info:
        core.authenticate_admin(credentials)
    assert exc_info.value.status_code == 401


@mock.patch("core.get_db")
def test_authenticate_admin_accepts_database_user(mock_get_db, monkeypatch):
    password = "named-admin-secret"
    monkeypatch.setattr(core, "ADMIN_API_PASSWORD", "bootstrap-secret")
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = (
        "alice",
        "Alice Example",
        "operator",
        core.password_hash(password),
    )
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    credentials = HTTPBasicCredentials(username="alice", password=password)
    assert core.authenticate_admin(credentials) == {
        "username": "alice",
        "display_name": "Alice Example",
        "role": "operator",
        "source": "database",
    }


def test_require_admin_write_rejects_viewer():
    with pytest.raises(HTTPException) as exc_info:
        core.require_admin_write({"username": "viewer", "role": "viewer"})
    assert exc_info.value.status_code == 403


def test_authenticate_client_accepts_client_password(monkeypatch):
    monkeypatch.setattr(core, "CLIENT_API_PASSWORD", "client-secret")
    credentials = HTTPBasicCredentials(username="sensos", password="client-secret")
    assert core.authenticate_client(credentials) == credentials


def test_authenticate_client_rejects_other_username(monkeypatch):
    monkeypatch.setattr(core, "CLIENT_API_PASSWORD", "client-secret")
    credentials = HTTPBasicCredentials(username="other", password="client-secret")
    with pytest.raises(HTTPException) as exc_info:
        core.authenticate_client(credentials)
    assert exc_info.value.status_code == 401


def test_authenticate_client_rejects_other_password(monkeypatch):
    monkeypatch.setattr(core, "CLIENT_API_PASSWORD", "client-secret")
    credentials = HTTPBasicCredentials(username="sensos", password="wrongpassword")
    with pytest.raises(HTTPException) as exc_info:
        core.authenticate_client(credentials)
    assert exc_info.value.status_code == 401


@mock.patch("core.get_db")
def test_authenticate_peer_accepts_per_peer_password(mock_get_db):
    peer_password = "peer-secret"
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.return_value = (
        123,
        "10.0.1.7",
        core.hash_peer_api_password(peer_password),
    )
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    credentials = HTTPBasicCredentials(username="peer-123", password=peer_password)
    assert core.authenticate_peer(credentials) == {
        "peer_id": 123,
        "peer_uuid": "peer-123",
        "wg_ip": "10.0.1.7",
    }


@mock.patch("core.get_db")
def test_authenticate_peer_accepts_single_durable_identity(mock_get_db):
    token = "server-issued-token"
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.side_effect = [
        None,
        (123, "peer-123", "10.0.1.7", core.password_hash(token), None),
    ]
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    credentials = HTTPBasicCredentials(username="client-uuid", password=token)
    assert core.authenticate_peer(credentials) == {
        "peer_id": 123,
        "peer_uuid": "peer-123",
        "wg_ip": "10.0.1.7",
        "client_uuid": "client-uuid",
    }


@mock.patch("core.get_db")
def test_authenticate_peer_rejects_invalid_password(mock_get_db):
    fake_cur = mock.MagicMock()
    fake_cur.fetchone.side_effect = [
        (123, "10.0.1.7", core.hash_peer_api_password("peer-secret")),
        None,
    ]
    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = fake_cur
    mock_get_db.return_value.__enter__.return_value = mock_conn

    credentials = HTTPBasicCredentials(username="peer-123", password="wrong")
    with pytest.raises(HTTPException) as exc_info:
        core.authenticate_peer(credentials)
    assert exc_info.value.status_code == 401


def test_create_network_entry_new():
    mock_cur = mock.MagicMock()
    mock_cur.fetchone.side_effect = [None, (42,)]
    mock_cur.fetchall.return_value = []

    result, created = core.create_network_entry(
        cur=mock_cur,
        name="testnet",
        wg_public_ip="10.0.0.1",
        wg_port=None,
    )

    assert created is True
    assert result["id"] == 42
    assert result["wg_public_key"] is None
    assert result["wg_port"] == core.PUBLIC_WG_PORT_START
    assert result["ip_range"] == str(core.generate_default_ip_range("testnet"))


def test_store_i2c_readings_upload_inserts_readings():
    fake_cur = mock.MagicMock()
    fake_conn = mock.MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur
    upload = mock.MagicMock()
    upload.hostname = "sensor-node"
    upload.client_version = "1.2.3"
    upload.sent_at = datetime(2026, 4, 7, 11, 59, tzinfo=timezone.utc)
    upload.readings = [
        mock.MagicMock(
            id=100,
            timestamp=datetime(2026, 4, 7, 11, 58, tzinfo=timezone.utc),
            device_address="0x76",
            sensor_type="BME280",
            key="temperature_c",
            value=23.5,
        ),
        mock.MagicMock(
            id=101,
            timestamp=datetime(2026, 4, 7, 11, 58, 5, tzinfo=timezone.utc),
            device_address="0x76",
            sensor_type="BME280",
            key="humidity_pct",
            value=51.2,
        ),
    ]

    result = core.store_i2c_readings_upload(fake_conn, upload, "10.0.1.7")

    assert result["status"] == "ok"
    assert result["accepted_count"] == 2
    assert result["receipt_id"]
    assert result["server_received_at"]
    fake_cur.executemany.assert_called_once()
    fake_conn.transaction.assert_called_once()


def test_store_birdnet_results_upload_inserts_detections():
    fake_cur = mock.MagicMock()
    fake_conn = mock.MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur
    upload = mock.MagicMock()
    upload.schema_version = 1
    upload.hostname = "sensor-node"
    upload.client_version = "1.2.3"
    upload.sent_at = datetime(2026, 4, 7, 11, 59, tzinfo=timezone.utc)
    upload.detections = [
        mock.MagicMock(
            source_path="audio_recordings/compressed/a.flac",
            channel_index=0,
            window_index=0,
            max_score_start_frame=0,
            label="Bird",
            score=0.9,
            likely_score=0.7,
            weighted_label=None,
            weighted_score=None,
            weighted_likely_score=None,
            volume=0.018,
            clip_start_time=datetime(2026, 4, 7, 11, 57, tzinfo=timezone.utc),
            clip_end_time=datetime(2026, 4, 7, 11, 58, tzinfo=timezone.utc),
        )
    ]

    result = core.store_birdnet_results_upload(fake_conn, upload, "10.0.1.7")

    assert result["status"] == "ok"
    assert result["accepted_count"] == 1
    assert result["receipt_id"]
    assert result["server_received_at"]
    fake_cur.executemany.assert_called_once()
    stored_detection = fake_cur.executemany.call_args.args[1][0]
    assert stored_detection[13] is None
    assert stored_detection[14] is None
    assert stored_detection[15] is None
    fake_conn.transaction.assert_called_once()


def test_create_network_entry_accepts_hostname_endpoint():
    mock_cur = mock.MagicMock()
    mock_cur.fetchone.side_effect = [None, (42,)]
    mock_cur.fetchall.return_value = []

    result, created = core.create_network_entry(
        cur=mock_cur,
        name="testnet",
        wg_public_ip="server.example.org",
        wg_port=51820,
    )

    assert created is True
    assert result["wg_public_ip"] == "server.example.org"


def test_create_network_entry_rejects_endpoint_change_without_reconcile():
    mock_cur = mock.MagicMock()
    mock_cur.fetchone.return_value = (
        42,
        "10.0.0.0/16",
        "45.20.196.87",
        51281,
        "server-pubkey",
    )

    with pytest.raises(RuntimeError, match="explicit network endpoint update path"):
        core.create_network_entry(
            cur=mock_cur,
            name="testnet",
            wg_public_ip="10.0.2.2",
            wg_port=15182,
        )


def test_create_network_entry_rejects_public_ip_change_without_port_override():
    mock_cur = mock.MagicMock()
    mock_cur.fetchone.return_value = (
        42,
        "10.0.0.0/16",
        "45.20.196.87",
        51281,
        "server-pubkey",
    )

    with pytest.raises(RuntimeError, match="explicit network endpoint update path"):
        core.create_network_entry(
            cur=mock_cur,
            name="testnet",
            wg_public_ip="10.0.2.2",
            wg_port=None,
        )


def test_update_network_endpoint_updates_existing_row():
    mock_cur = mock.MagicMock()
    mock_cur.fetchone.return_value = (
        42,
        "testnet",
        "10.0.0.0/16",
        "10.0.2.2",
        15182,
        "server-pubkey",
    )

    result = core.update_network_endpoint(
        cur=mock_cur,
        name="testnet",
        wg_public_ip="10.0.2.2",
        wg_port=15182,
    )

    assert result["name"] == "testnet"
    assert result["wg_public_ip"] == "10.0.2.2"
    assert result["wg_port"] == 15182
    sql, params = mock_cur.execute.call_args.args
    assert "UPDATE sensos.networks" in sql
    assert "SET wg_public_ip = %s, wg_port = %s" in sql
    assert "RETURNING id, name, ip_range, wg_public_ip, wg_port, wg_public_key" in sql
    assert params == ("10.0.2.2", 15182, "testnet")


def test_update_network_endpoint_rejects_missing_network():
    mock_cur = mock.MagicMock()
    mock_cur.fetchone.return_value = None

    with pytest.raises(RuntimeError, match="does not exist"):
        core.update_network_endpoint(
            cur=mock_cur,
            name="missing",
            wg_public_ip="10.0.2.2",
            wg_port=15182,
        )


def test_allocate_public_wg_port_uses_next_available():
    mock_cur = mock.MagicMock()
    mock_cur.fetchall.return_value = [(51281,), (51282,), (51284,)]

    assert core.allocate_public_wg_port(mock_cur) == 51283


def test_allocate_public_wg_port_raises_when_range_exhausted():
    mock_cur = mock.MagicMock()
    mock_cur.fetchall.return_value = [
        (port,) for port in range(core.PUBLIC_WG_PORT_START, core.PUBLIC_WG_PORT_END + 1)
    ]

    with pytest.raises(RuntimeError, match="no available public WireGuard ports remain"):
        core.allocate_public_wg_port(mock_cur)


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
