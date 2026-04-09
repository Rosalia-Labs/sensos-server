# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from core import (
    authenticate_admin,
    create_network_entry,
    delete_network,
    delete_peer,
    get_db,
    set_peer_active_state,
    update_network_endpoint,
    wait_for_network_ready,
)
from models import (
    CreateNetworkRequest,
    DeleteNetworkRequest,
    DeletePeerRequest,
    SetPeerActiveRequest,
    UpdateNetworkEndpointRequest,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def error_response(status_code: int, message: str):
    return JSONResponse(status_code=status_code, content={"error": message})


@router.get("/networks")
def get_defined_networks(credentials=Depends(authenticate_admin)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, ip_range, wg_public_ip, wg_port, wg_public_key
                FROM sensos.networks
                ORDER BY name;
                """
            )
            rows = cur.fetchall()
    return {
        "networks": [
            {
                "name": row[0],
                "ip_range": row[1],
                "wg_public_ip": row[2],
                "wg_port": row[3],
                "wg_public_key": row[4],
            }
            for row in rows
        ]
    }


@router.get("/networks/{network_name}")
def get_network_info(network_name: str, credentials=Depends(authenticate_admin)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, ip_range, wg_public_ip, wg_port, wg_public_key
                FROM sensos.networks
                WHERE name = %s;
                """,
                (network_name,),
            )
            result = cur.fetchone()
    if not result:
        return error_response(404, f"No network found with name '{network_name}'")
    return {
        "name": result[0],
        "ip_range": result[1],
        "wg_public_ip": result[2],
        "wg_port": result[3],
        "wg_public_key": result[4],
    }


@router.post("/networks")
def create_network(request: CreateNetworkRequest, credentials=Depends(authenticate_admin)):
    try:
        with get_db() as conn:
            result, created = create_network_entry(
                conn.cursor(), request.name, request.wg_public_ip, request.wg_port
            )
        if created or not result["wg_public_key"]:
            ready = wait_for_network_ready(request.name)
            result = {
                "id": ready[0],
                "name": request.name,
                "ip_range": ready[1],
                "wg_public_key": ready[2],
                "wg_public_ip": ready[3],
                "wg_port": ready[4],
            }
        return result
    except RuntimeError as exc:
        return error_response(status.HTTP_409_CONFLICT, str(exc))
    except Exception as exc:
        return error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


@router.put("/networks/{network_name}/endpoint")
def update_network_endpoint_route(
    network_name: str,
    request: UpdateNetworkEndpointRequest,
    credentials=Depends(authenticate_admin),
):
    try:
        with get_db() as conn:
            result = update_network_endpoint(
                conn.cursor(), network_name, request.wg_public_ip, request.wg_port
            )
        if not result["wg_public_key"]:
            ready = wait_for_network_ready(network_name)
            result = {
                "id": ready[0],
                "name": network_name,
                "ip_range": ready[1],
                "wg_public_key": ready[2],
                "wg_public_ip": ready[3],
                "wg_port": ready[4],
            }
        return result
    except RuntimeError as exc:
        return error_response(status.HTTP_409_CONFLICT, str(exc))
    except Exception as exc:
        return error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


@router.delete("/networks/{network_name}")
def delete_network_endpoint(network_name: str, credentials=Depends(authenticate_admin)):
    if not delete_network(network_name):
        return error_response(404, f"Network '{network_name}' not found.")
    return {"network_name": network_name, "deleted": True}


@router.get("/peers")
def list_peers(credentials=Depends(authenticate_admin)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.uuid::text, p.wg_ip::text, n.name, p.is_active, p.note, p.registered_at
                FROM sensos.wireguard_peers p
                JOIN sensos.networks n ON p.network_id = n.id
                ORDER BY n.name, p.wg_ip;
                """
            )
            rows = cur.fetchall()
    return {
        "peers": [
            {
                "peer_uuid": row[0],
                "wg_ip": row[1],
                "network_name": row[2],
                "is_active": row[3],
                "note": row[4],
                "registered_at": row[5],
            }
            for row in rows
        ]
    }


@router.get("/peers/{ip_address}")
def get_peer_info(ip_address: str, credentials=Depends(authenticate_admin)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, network_id, is_active FROM sensos.wireguard_peers WHERE wg_ip = %s;",
                (ip_address,),
            )
            peer = cur.fetchone()
            if not peer:
                return {
                    "exists": False,
                    "is_active": None,
                    "network_name": None,
                    "network_wg_public_key": None,
                    "peer_wg_public_key": None,
                    "ssh_public_key": None,
                }
            peer_id, network_id, is_active = peer
            cur.execute(
                "SELECT name, wg_public_key FROM sensos.networks WHERE id = %s;",
                (network_id,),
            )
            network = cur.fetchone()
            if network:
                network_name, network_wg_public_key = network
            else:
                network_name, network_wg_public_key = None, None
            cur.execute(
                "SELECT wg_public_key FROM sensos.wireguard_keys WHERE peer_id = %s AND is_active = TRUE ORDER BY created_at DESC LIMIT 1;",
                (peer_id,),
            )
            peer_wg_row = cur.fetchone()
            cur.execute(
                "SELECT ssh_public_key FROM sensos.ssh_keys WHERE peer_id = %s ORDER BY last_used DESC LIMIT 1;",
                (peer_id,),
            )
            ssh_row = cur.fetchone()
    return {
        "exists": True,
        "is_active": is_active,
        "network_name": network_name,
        "network_wg_public_key": network_wg_public_key,
        "peer_wg_public_key": peer_wg_row[0] if peer_wg_row else None,
        "ssh_public_key": ssh_row[0] if ssh_row else None,
    }


@router.patch("/peers/{wg_ip}/active")
def set_peer_active(
    wg_ip: str, request: SetPeerActiveRequest, credentials=Depends(authenticate_admin)
):
    if wg_ip != request.wg_ip:
        return error_response(400, "Path wg_ip must match request body wg_ip.")
    if not set_peer_active_state(request.wg_ip, request.is_active):
        return error_response(404, f"Peer '{request.wg_ip}' not found.")
    return {"wg_ip": request.wg_ip, "is_active": request.is_active}


@router.delete("/peers/{wg_ip}")
def delete_peer_endpoint(wg_ip: str, credentials=Depends(authenticate_admin)):
    if not delete_peer(wg_ip):
        return error_response(404, f"Peer '{wg_ip}' not found.")
    return {"wg_ip": wg_ip, "deleted": True}


@router.get("/peers/{wg_ip}/location")
def get_client_location(wg_ip: str, credentials=Depends(authenticate_admin)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT l.recorded_at,
                       public.ST_Y(l.location::public.geometry)::float AS latitude,
                       public.ST_X(l.location::public.geometry)::float AS longitude
                FROM sensos.peer_locations l
                JOIN sensos.wireguard_peers p ON l.peer_id = p.id
                WHERE p.wg_ip = %s
                ORDER BY l.recorded_at DESC
                LIMIT 1;
                """,
                (wg_ip,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="No location found.")
    return {"latitude": row[1], "longitude": row[2], "recorded_at": row[0]}


@router.get("/birdnet/batches")
def list_birdnet_batches(limit: int = 50, credentials=Depends(authenticate_admin)):
    bounded_limit = max(1, min(limit, 500))
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.receipt_id::text,
                       b.wireguard_ip::text,
                       p.note,
                       n.name,
                       b.hostname,
                       b.client_version,
                       b.batch_id,
                       b.ownership_mode,
                       b.source_count,
                       b.first_processed_at,
                       b.last_processed_at,
                       b.server_received_at
                FROM sensos.birdnet_result_batches b
                LEFT JOIN sensos.wireguard_peers p ON p.wg_ip = b.wireguard_ip
                LEFT JOIN sensos.networks n ON n.id = p.network_id
                ORDER BY b.server_received_at DESC
                LIMIT %s;
                """,
                (bounded_limit,),
            )
            rows = cur.fetchall()
    return {
        "batches": [
            {
                "receipt_id": row[0],
                "wg_ip": row[1],
                "note": row[2],
                "network_name": row[3],
                "hostname": row[4],
                "client_version": row[5],
                "batch_id": row[6],
                "ownership_mode": row[7],
                "source_count": row[8],
                "first_processed_at": row[9],
                "last_processed_at": row[10],
                "server_received_at": row[11],
            }
            for row in rows
        ]
    }


@router.get("/peers/{wg_ip}/birdnet/batches")
def get_peer_birdnet_batches(
    wg_ip: str,
    limit: int = 50,
    credentials=Depends(authenticate_admin),
):
    bounded_limit = max(1, min(limit, 500))
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.receipt_id::text,
                       b.hostname,
                       b.client_version,
                       b.batch_id,
                       b.ownership_mode,
                       b.source_count,
                       b.first_processed_at,
                       b.last_processed_at,
                       b.server_received_at
                FROM sensos.birdnet_result_batches b
                WHERE b.wireguard_ip = %s
                ORDER BY b.server_received_at DESC
                LIMIT %s;
                """,
                (wg_ip, bounded_limit),
            )
            rows = cur.fetchall()
    return {
        "wg_ip": wg_ip,
        "batches": [
            {
                "receipt_id": row[0],
                "hostname": row[1],
                "client_version": row[2],
                "batch_id": row[3],
                "ownership_mode": row[4],
                "source_count": row[5],
                "first_processed_at": row[6],
                "last_processed_at": row[7],
                "server_received_at": row[8],
            }
            for row in rows
        ],
    }


@router.get("/runtime/wireguard")
def wireguard_status(credentials=Depends(authenticate_admin)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.component,
                       c.role,
                       n.name,
                       c.interface_name,
                       c.status,
                       c.public_key,
                       c.raw_status,
                       c.last_error,
                       c.updated_at
                FROM sensos.runtime_wireguard_status c
                JOIN sensos.networks n ON n.id = c.network_id
                ORDER BY n.name, c.component;
                """
            )
            rows = cur.fetchall()

    def parse_peers(output: str):
        lines = output.strip().splitlines()
        peers = []
        current_peer = {}
        skip_interface = True
        for line in lines:
            line = line.strip()
            if skip_interface:
                if line.startswith("peer:"):
                    skip_interface = False
                else:
                    continue
            if line.startswith("peer:"):
                if current_peer:
                    peers.append(current_peer)
                current_peer = {"public_key": line.split(":", 1)[1].strip()}
            elif ":" in line:
                key, val = map(str.strip, line.split(":", 1))
                current_peer[key] = val
        if current_peer:
            peers.append(current_peer)
        return peers

    def parse_handshake(text):
        match = re.match(r"(\d+)\s+(\w+)\s+ago", text)
        if not match:
            return text
        num, unit = match.groups()
        try:
            delta = timedelta(**{unit: int(num)})
            ts = datetime.utcnow() - delta
            return ts.strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return text

    return {
        "components": [
            {
                "component": component,
                "role": role,
                "network_name": network_name,
                "interface_name": interface_name,
                "status": state,
                "public_key": public_key,
                "last_error": last_error,
                "updated_at": updated_at,
                "peers": [
                    {
                        "public_key": peer.get("public_key"),
                        "allowed_ips": peer.get("allowed ips", "—"),
                        "endpoint": peer.get("endpoint", "—"),
                        "last_contact": parse_handshake(
                            peer.get("latest handshake", "—")
                        ),
                        "transfer": peer.get("transfer", "—"),
                    }
                    for peer in parse_peers(raw_status or "")
                ],
            }
            for component, role, network_name, interface_name, state, public_key, raw_status, last_error, updated_at in rows
        ]
    }
