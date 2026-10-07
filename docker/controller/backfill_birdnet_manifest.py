# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC
#
# Populates clients.db and each client's manifest.db from clip files already
# on disk, for clips uploaded before this manifest existed (or any clip that
# otherwise ended up missing from it). The clip filename is the detection id,
# so this is a straight lookup against Postgres per file, reusing the exact
# same manifest-writing functions the live upload path uses -- idempotent
# (both are upserts), so safe to run as often as needed, not just once.
#
# Run inside the controller container:
#   docker exec sensos-controller python3 backfill_birdnet_manifest.py

import os

from core import (
    BIRDNET_AUDIO_ROOT,
    append_clip_manifest_entry,
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
                               d.weighted_score, d.weighted_likely_score, d.volume,
                               d.channel_index, d.clip_start_time, d.clip_end_time,
                               p.uuid::text, p.wg_ip::text, n.name,
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
                volume,
                channel_index,
                clip_start_time,
                clip_end_time,
                peer_uuid,
                wg_ip,
                network_name,
                hostname,
            ) = row

            upsert_client_manifest_entry(peer_id, peer_uuid, wg_ip, network_name, hostname)
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
                    "volume": volume,
                    "channel_index": channel_index,
                    "clip_start_time": clip_start_time.isoformat(),
                    "clip_end_time": clip_end_time.isoformat(),
                },
            )
            manifested += 1

    return manifested, skipped


def main() -> int:
    manifested, skipped = backfill()
    print(f"Backfill complete: {manifested} clip(s) manifested, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
