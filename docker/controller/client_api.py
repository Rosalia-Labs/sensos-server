# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import json
import logging
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from core import (
    authenticate_client,
    authenticate_peer,
    get_db,
    get_network_details,
    get_runtime_operator_ssh_key,
    insert_peer,
    register_wireguard_key_in_db,
    search_for_next_available_ip,
    store_i2c_readings_upload,
)
from models import (
    ClientStatusRequest,
    HardwareProfile,
    I2CReadingsUploadRequest,
    LocationUpdateRequest,
    RegisterPeerRequest,
    RegisterSSHKeyRequest,
    RegisterWireguardKeyRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/client", tags=["client"])


def error_response(status_code: int, message: str):
    return JSONResponse(status_code=status_code, content={"error": message})


@router.get("/healthz")
def healthz(request: Request):
    if getattr(request.app.state, "schema_ready", False):
        return {"status": "ok"}
    return JSONResponse(status_code=503, content={"status": "starting"})


@router.get("/networks/{network_name}")
def get_network_info(
    network_name: str, credentials=Depends(authenticate_client)
):
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


@router.post("/peers/enroll")
def register_peer(
    request: RegisterPeerRequest,
    credentials=Depends(authenticate_client),
):
    network_details = get_network_details(request.network_name)
    if not network_details:
        return error_response(404, f"Network '{request.network_name}' not found.")

    network_id, subnet, public_key, wg_public_ip, wg_port = network_details
    if not public_key:
        return error_response(
            409, f"Network '{request.network_name}' exists but is not ready yet."
        )

    network = ipaddress.ip_network(subnet, strict=False)
    if request.subnet_offset < 0 or request.subnet_offset >= network.num_addresses // 256:
        return error_response(
            400,
            f"Invalid subnet_offset {request.subnet_offset}. Must be between 0 and {network.num_addresses // 256 - 1}.",
        )

    wg_ip = search_for_next_available_ip(
        subnet, network_id, start_third_octet=request.subnet_offset
    )
    if not wg_ip:
        return error_response(
            409, f"No available IPs in subnet {request.subnet_offset}."
        )

    _, peer_uuid, peer_api_password = insert_peer(network_id, wg_ip, note=request.note)
    return {
        "wg_ip": wg_ip,
        "wg_public_key": public_key,
        "wg_public_ip": wg_public_ip,
        "wg_port": wg_port,
        "peer_uuid": peer_uuid,
        "peer_api_password": peer_api_password,
    }


@router.post("/peer/wireguard-key")
def register_wireguard_key(
    request: RegisterWireguardKeyRequest,
    peer: dict = Depends(authenticate_peer),
):
    result = register_wireguard_key_in_db(peer["wg_ip"], request.wg_public_key)
    if result is None:
        return error_response(404, f"Peer '{peer['wg_ip']}' not found.")
    return result


@router.post("/peer/ssh-key")
def exchange_ssh_keys(
    request: RegisterSSHKeyRequest,
    peer: dict = Depends(authenticate_peer),
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sensos.wireguard_peers WHERE wg_ip = %s;",
                (peer["wg_ip"],),
            )
            result = cur.fetchone()
            if not result:
                raise HTTPException(
                    status_code=404,
                    detail=f"Peer with WireGuard IP '{peer['wg_ip']}' not found.",
                )

            peer_id = result[0]
            cur.execute(
                """
                INSERT INTO sensos.ssh_keys 
                (peer_id, username, uid, ssh_public_key, key_type, key_size, 
                 key_comment, fingerprint, expires_at, last_used)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (peer_id, ssh_public_key) DO NOTHING
                RETURNING *;
                """,
                (
                    peer_id,
                    request.username,
                    request.uid,
                    request.ssh_public_key,
                    request.key_type,
                    request.key_size,
                    request.key_comment,
                    request.fingerprint,
                    request.expires_at,
                ),
            )
            inserted_key = cur.fetchone()
            if not inserted_key:
                raise HTTPException(
                    status_code=409, detail="SSH key already exists for this peer."
                )
        conn.commit()

    ssh_public_key = get_runtime_operator_ssh_key()
    if not ssh_public_key:
        raise HTTPException(
            status_code=503,
            detail="Operator SSH public key not published yet.",
        )

    return {"ssh_public_key": ssh_public_key}


@router.post("/peer/status")
def client_status(
    status_update: ClientStatusRequest,
    peer: dict = Depends(authenticate_peer),
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sensos.client_status (
                    peer_id, last_check_in, hostname, uptime_seconds,
                    disk_available_gb, memory_used_mb, memory_total_mb,
                    load_1m, load_5m, load_15m, version, status_message
                ) VALUES (
                    %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    peer["peer_id"],
                    status_update.hostname,
                    status_update.uptime_seconds,
                    status_update.disk_available_gb,
                    status_update.memory_used_mb,
                    status_update.memory_total_mb,
                    status_update.load_1m,
                    status_update.load_5m,
                    status_update.load_15m,
                    status_update.version,
                    status_update.status_message,
                ),
            )
            conn.commit()
    return {"message": "Client status updated successfully"}


@router.put("/peer/hardware-profile")
def upload_hardware_profile(
    profile: HardwareProfile,
    peer: dict = Depends(authenticate_peer),
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sensos.hardware_profiles (peer_id, profile_json)
                VALUES (%s, %s)
                ON CONFLICT (peer_id) DO UPDATE
                SET profile_json = EXCLUDED.profile_json, uploaded_at = NOW();
                """,
                (peer["peer_id"], json.dumps(profile.model_dump())),
            )
            conn.commit()
    logger.info("hardware profile stored for peer_uuid '%s'", peer["peer_uuid"])
    return {"status": "success", "wg_ip": peer["wg_ip"]}


@router.post("/peer/i2c-readings/batches")
def upload_i2c_readings(
    upload: I2CReadingsUploadRequest,
    peer: dict = Depends(authenticate_peer),
):
    try:
        with get_db() as conn:
            return store_i2c_readings_upload(conn, upload, peer["wg_ip"])
    except RuntimeError as exc:
        return error_response(status.HTTP_409_CONFLICT, str(exc))
    except Exception:
        logger.error("i2c readings upload failed", exc_info=True)
        return error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to store I2C readings upload.",
        )


@router.put("/peer/location")
def set_client_location(
    req: LocationUpdateRequest,
    peer: dict = Depends(authenticate_peer),
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sensos.wireguard_peers WHERE wg_ip = %s;",
                (peer["wg_ip"],),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Peer not found.")
            cur.execute(
                """
                INSERT INTO sensos.peer_locations (peer_id, location)
                VALUES (%s, sensos.ST_SetSRID(sensos.ST_MakePoint(%s, %s), 4326));
                """,
                (row[0], req.longitude, req.latitude),
            )
            conn.commit()
    return {"status": "location stored"}
