# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC
#
# Reconciles each peer's manifest.db against the clip files actually on disk,
# in both directions:
#
#   backfill() -- a clip file exists but isn't in its peer's manifest.db yet
#                 (clips uploaded before the manifest existed, or any clip
#                 that otherwise ended up missing from it). Looks the file up
#                 in Postgres by detection id and writes the manifest row.
#   prune()    -- a manifest.db row exists but its clip file is gone. Deletes
#                 the row.
#
# The manifest's only job is to make an opaque "<peer>/<detection_id>.flac"
# filename interpretable without a database connection -- label, score,
# timing. A row describing a file that isn't there serves that purpose for
# nothing and is just a second, independently-stale copy of metadata Postgres
# already has, so prune() matters as much as backfill() for keeping that
# invariant (manifest row <=> real file) true rather than "usually true."
#
# Both directions reuse the exact manifest-writing/deleting functions the
# live upload path uses -- idempotent (upsert / delete-if-present) -- so this
# is safe to run as often as needed, not just once.
#
# Normally invoked via `bin/audio-archive repair` from the host. Can also be
# run directly inside the controller container:
#   docker exec sensos-controller python3 reconcile_birdnet_manifest.py

import os
import sqlite3

from core import (
    BIRDNET_AUDIO_ROOT,
    append_clip_manifest_entry,
    delete_clip_manifest_entry,
    get_db,
    upsert_client_manifest_entry,
)


def backfill() -> tuple[int, int]:
    if not os.path.isdir(BIRDNET_AUDIO_ROOT):
        print(f"No audio root at {BIRDNET_AUDIO_ROOT}; nothing to backfill.")
        return 0, 0

    manifested = 0
    skipped = 0

    for entry in sorted(os.listdir(BIRDNET_AUDIO_ROOT)):
        peer_dir = os.path.join(BIRDNET_AUDIO_ROOT, entry)
        if not entry.isdigit() or not os.path.isdir(peer_dir):
            continue
        peer_id = int(entry)

        for filename in sorted(os.listdir(peer_dir)):
            if not filename.endswith(".flac"):
                continue
            try:
                detection_id = int(filename[: -len(".flac")])
            except ValueError:
                print(f"[WARN] Unexpected filename, skipping: {entry}/{filename}")
                skipped += 1
                continue

            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT d.label, d.score, d.likely_score, d.weighted_label,
                               d.weighted_score, d.weighted_likely_score,
                               d.human_vocal_score, d.volume,
                               d.channel_index, d.clip_start_time, d.clip_end_time,
                               p.uuid::text, p.wg_ip::text, n.name, p.deployed_at,
                               (SELECT hostname FROM sensos.client_status
                                WHERE peer_id = p.id ORDER BY last_check_in DESC LIMIT 1)
                        FROM sensos.birdnet_detections d
                        JOIN sensos.wireguard_peers p ON p.id = d.peer_id
                        JOIN sensos.networks n ON n.id = p.network_id
                        WHERE d.id = %s AND d.peer_id = %s;
                        """,
                        (detection_id, peer_id),
                    )
                    row = cur.fetchone()

            if row is None:
                print(
                    f"[WARN] No matching detection row for {entry}/{filename}; "
                    "file exists but Postgres doesn't know it -- skipping."
                )
                skipped += 1
                continue

            (
                label,
                score,
                likely_score,
                weighted_label,
                weighted_score,
                weighted_likely_score,
                human_vocal_score,
                volume,
                channel_index,
                clip_start_time,
                clip_end_time,
                peer_uuid,
                wg_ip,
                network_name,
                deployed_at,
                hostname,
            ) = row

            # Same as-of-clip-time logic as the live upload path: location
            # when THIS clip was recorded, not wherever the client is now.
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT ST_Y(location::geometry), ST_X(location::geometry)
                        FROM sensos.peer_locations
                        WHERE peer_id = %s AND recorded_at <= %s
                        ORDER BY recorded_at DESC
                        LIMIT 1;
                        """,
                        (peer_id, clip_start_time),
                    )
                    location_row = cur.fetchone()
            latitude, longitude = location_row if location_row else (None, None)

            upsert_client_manifest_entry(
                peer_id, peer_uuid, wg_ip, network_name, hostname, deployed_at
            )
            append_clip_manifest_entry(
                peer_id,
                {
                    "detection_id": detection_id,
                    "clip_filename": filename,
                    "label": label,
                    "score": score,
                    "likely_score": likely_score,
                    "weighted_label": weighted_label,
                    "weighted_score": weighted_score,
                    "weighted_likely_score": weighted_likely_score,
                    "human_vocal_score": human_vocal_score,
                    "volume": volume,
                    "channel_index": channel_index,
                    "clip_start_time": clip_start_time.isoformat(),
                    "clip_end_time": clip_end_time.isoformat(),
                    "latitude": latitude,
                    "longitude": longitude,
                },
            )
            manifested += 1

    return manifested, skipped


def prune() -> tuple[int, int]:
    """Inverse of backfill(): removes a peer's manifest.db rows whose clip
    file is no longer on disk. Reads each manifest.db directly (read-only)
    since this just needs the list of detection ids it currently claims, not
    anything core.py's connection helpers manage beyond that."""
    if not os.path.isdir(BIRDNET_AUDIO_ROOT):
        return 0, 0

    pruned = 0
    checked_peers = 0

    for entry in sorted(os.listdir(BIRDNET_AUDIO_ROOT)):
        peer_dir = os.path.join(BIRDNET_AUDIO_ROOT, entry)
        if not entry.isdigit() or not os.path.isdir(peer_dir):
            continue
        peer_id = int(entry)
        manifest_path = os.path.join(peer_dir, "manifest.db")
        if not os.path.isfile(manifest_path):
            continue
        checked_peers += 1

        manifest_conn = sqlite3.connect(f"file:{manifest_path}?mode=ro", uri=True, timeout=10)
        try:
            try:
                detection_ids = [
                    row[0] for row in manifest_conn.execute("SELECT detection_id FROM clips")
                ]
            except sqlite3.OperationalError:
                detection_ids = []
        finally:
            manifest_conn.close()

        for detection_id in detection_ids:
            clip_path = os.path.join(peer_dir, f"{detection_id}.flac")
            if os.path.isfile(clip_path):
                continue
            if delete_clip_manifest_entry(peer_id, detection_id):
                print(f"[INFO] Pruned stale manifest row (file missing): {entry}/{detection_id}.flac")
                pruned += 1

    return pruned, checked_peers


def main() -> int:
    manifested, skipped = backfill()
    pruned, checked_peers = prune()
    print(
        f"Reconcile complete: {manifested} clip(s) manifested, {skipped} skipped, "
        f"{pruned} stale manifest row(s) pruned across {checked_peers} peer manifest(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
