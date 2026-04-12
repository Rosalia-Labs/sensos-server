# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import json
import os
import html
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import psycopg

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse


POSTGRES_USER = "sensos_public"
POSTGRES_PASSWORD = os.getenv("PUBLIC_DB_PASSWORD", "sensos-public")
DATABASE_URL = (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@sensos-database/postgres"
)

VERSION_MAJOR = os.getenv("VERSION_MAJOR", "Unknown")
VERSION_MINOR = os.getenv("VERSION_MINOR", "Unknown")
VERSION_PATCH = os.getenv("VERSION_PATCH", "Unknown")
VERSION_SUFFIX = os.getenv("VERSION_SUFFIX", "")

SYNOPTIC_RANGES = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}


def current_version() -> str:
    base = f"{VERSION_MAJOR}.{VERSION_MINOR}.{VERSION_PATCH}"
    return f"{base}-{VERSION_SUFFIX}" if VERSION_SUFFIX else base


def get_db(retries: int = 10, delay: int = 3):
    import time

    for attempt in range(retries):
        try:
            return psycopg.connect(DATABASE_URL, autocommit=True)
        except psycopg.OperationalError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)


def format_rfc3339_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def escape_html(value) -> str:
    return html.escape(str(value or ""))


def normalize_synoptic_range(value: str | None) -> str:
    candidate = (value or "day").strip().lower()
    return candidate if candidate in SYNOPTIC_RANGES else "day"


def downsample_points(points: list[dict], limit: int) -> list[dict]:
    if len(points) <= limit:
        return points
    if limit <= 2:
        return [points[0], points[-1]]
    step = (len(points) - 1) / float(limit - 1)
    sampled = [points[min(round(index * step), len(points) - 1)] for index in range(limit - 1)]
    sampled.append(points[-1])
    deduped: list[dict] = []
    seen: set[tuple[str, float]] = set()
    for point in sampled:
        marker = (str(point.get("recorded_at") or point.get("processed_at")), float(point.get("value", point.get("activity", 0.0))))
        if marker in seen:
            continue
        deduped.append(point)
        seen.add(marker)
    return deduped


def bucket_birdnet_timestamp(value: datetime, range_key: str) -> datetime:
    ts = value.astimezone(timezone.utc)
    if range_key == "hour":
        minute = (ts.minute // 5) * 5
        return ts.replace(minute=minute, second=0, microsecond=0)
    if range_key == "day":
        return ts.replace(minute=0, second=0, microsecond=0)
    if range_key == "week":
        hour = (ts.hour // 6) * 6
        return ts.replace(hour=hour, minute=0, second=0, microsecond=0)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _format_axis_value(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_time_tick(value: str) -> str:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return timestamp.strftime("%m-%d %H:%M")


def _chart_bounds(width: int, height: int) -> dict:
    return {
        "left": 58,
        "right": width - 18,
        "top": 14,
        "bottom": height - 32,
    }


def _render_axes(
    bounds: dict,
    min_value: float,
    max_value: float,
    x_labels: list[tuple[float, str]],
) -> str:
    left = bounds["left"]
    right = bounds["right"]
    top = bounds["top"]
    bottom = bounds["bottom"]
    span = max(max_value - min_value, 1e-9)
    parts = [
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="rgba(23,32,29,0.28)" stroke-width="1"></line>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="rgba(23,32,29,0.28)" stroke-width="1"></line>',
    ]
    for frac in (0.0, 0.5, 1.0):
        value = min_value + span * frac
        y = bottom - (bottom - top) * frac
        parts.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{right}" y2="{y:.2f}" stroke="rgba(23,32,29,0.08)" stroke-width="1" stroke-dasharray="4 6"></line>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" font-size="11" fill="rgba(23,32,29,0.62)">{escape_html(_format_axis_value(value))}</text>'
        )
    for x, label in x_labels:
        parts.append(
            f'<line x1="{x:.2f}" y1="{bottom}" x2="{x:.2f}" y2="{bottom + 6}" stroke="rgba(23,32,29,0.24)" stroke-width="1"></line>'
        )
        parts.append(
            f'<text x="{x:.2f}" y="{bottom + 18}" text-anchor="middle" font-size="11" fill="rgba(23,32,29,0.62)">{escape_html(label)}</text>'
        )
    return "".join(parts)


def render_line_chart_svg(
    points: list[dict],
    value_key: str,
    stroke: str,
    width: int = 760,
    height: int = 180,
) -> str:
    if not points:
        return ""
    values = [float(point[value_key]) for point in points if point.get(value_key) is not None]
    if not values:
        return ""
    bounds = _chart_bounds(width, height)
    left = bounds["left"]
    right = bounds["right"]
    top = bounds["top"]
    bottom = bounds["bottom"]
    min_value = min(values)
    max_value = max(values)
    span = max(max_value - min_value, 1e-9)
    step_x = (right - left) / max(1, len(points) - 1)
    coords = []
    for index, point in enumerate(points):
        value = float(point[value_key])
        x = left + index * step_x
        y = bottom - ((value - min_value) / span) * (bottom - top)
        coords.append((x, y))
    x_labels = []
    if points and ("recorded_at" in points[0] or "processed_at" in points[0]):
        time_key = "recorded_at" if "recorded_at" in points[0] else "processed_at"
        tick_indexes = sorted({0, max(0, len(points) // 2), len(points) - 1})
        x_labels = [(coords[index][0], _format_time_tick(points[index][time_key])) for index in tick_indexes]
    path = " ".join(
        ["M {:.2f} {:.2f}".format(coords[0][0], coords[0][1])]
        + ["L {:.2f} {:.2f}".format(x, y) for x, y in coords[1:]]
    )
    area = " ".join(
        ["M {:.2f} {:.2f}".format(coords[0][0], bottom)]
        + ["L {:.2f} {:.2f}".format(x, y) for x, y in coords]
        + ["L {:.2f} {:.2f} Z".format(coords[-1][0], bottom)]
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">'
        f'{_render_axes(bounds, min_value, max_value, x_labels)}'
        f'<path d="{area}" fill="{stroke}" opacity="0.12"></path>'
        f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>'
        f"</svg>"
    )


def render_bar_chart_svg(
    points: list[dict],
    value_key: str,
    fill: str,
    width: int = 760,
    height: int = 180,
) -> str:
    if not points:
        return ""
    values = [float(point[value_key]) for point in points if point.get(value_key) is not None]
    if not values:
        return ""
    bounds = _chart_bounds(width, height)
    left = bounds["left"]
    right = bounds["right"]
    top = bounds["top"]
    bottom = bounds["bottom"]
    max_value = max(values)
    if max_value <= 0:
        max_value = 1.0
    bar_width = max(6, (right - left) / max(1, len(points)) - 4)
    step_x = (right - left) / max(1, len(points))
    rects = []
    for index, point in enumerate(points):
        value = float(point[value_key])
        bar_height = (value / max_value) * (bottom - top)
        x = left + index * step_x + max(1, (step_x - bar_width) / 2)
        y = bottom - bar_height
        rects.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{bar_height:.2f}" rx="4" fill="{fill}" opacity="0.82"></rect>'
        )
    x_labels = []
    if points and "processed_at" in points[0]:
        tick_indexes = sorted({0, max(0, len(points) // 2), len(points) - 1})
        x_labels = [
            (
                left + index * step_x + step_x / 2,
                _format_time_tick(points[index]["processed_at"]),
            )
            for index in tick_indexes
        ]
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">'
        f'{_render_axes(bounds, 0.0, max_value, x_labels)}'
        + "".join(rects)
        + "</svg>"
    )


def render_event_timeline_svg(
    points: list[dict],
    value_key: str,
    stroke: str,
    width: int = 760,
    height: int = 120,
) -> str:
    if not points:
        return ""
    timestamps = [datetime.fromisoformat(point["processed_at"].replace("Z", "+00:00")) for point in points]
    values = [float(point[value_key]) for point in points]
    if not timestamps or not values:
        return ""
    bounds = _chart_bounds(width, height)
    left = bounds["left"]
    right = bounds["right"]
    top = bounds["top"]
    bottom = bounds["bottom"]
    min_ts = min(timestamps)
    max_ts = max(timestamps)
    total_seconds = max((max_ts - min_ts).total_seconds(), 1.0)
    circles = []
    x_labels = [
        (left, _format_time_tick(points[0]["processed_at"])),
        ((left + right) / 2, _format_time_tick(points[len(points) // 2]["processed_at"])),
        (right, _format_time_tick(points[-1]["processed_at"])),
    ]
    guides = [_render_axes(bounds, 0.0, 1.0, x_labels)]
    for ts, value, point in zip(timestamps, values, points):
        x = left + ((ts - min_ts).total_seconds() / total_seconds) * (right - left)
        y = bottom - value * (bottom - top)
        radius = 3.5 + value * 4.5
        circles.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius:.2f}" fill="{stroke}" opacity="0.82"><title>{escape_html(point["processed_at"])} · {value:.2f}</title></circle>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">'
        + "".join(guides)
        + "".join(circles)
        + "</svg>"
    )


def fetch_sites() -> list[dict]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT peer_uuid,
                       wg_ip,
                       network_name,
                       note,
                       site_label,
                       is_active,
                       registered_at,
                       location_recorded_at,
                       latitude,
                       longitude,
                       last_check_in,
                       hostname,
                       version,
                       status_message,
                       birdnet_batch_count,
                       birdnet_source_count,
                       latest_birdnet_upload_at
                FROM sensos.public_sites
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY site_label, wg_ip;
                """
            )
            rows = cur.fetchall()
    return [
        {
            "peer_uuid": row[0],
            "site_id": row[0],
            "wg_ip": row[1],
            "network_name": row[2],
            "note": row[3],
            "site_label": row[4],
            "is_active": row[5],
            "registered_at": format_rfc3339_utc(row[6]),
            "location_recorded_at": format_rfc3339_utc(row[7]),
            "latitude": float(row[8]),
            "longitude": float(row[9]),
            "last_check_in": format_rfc3339_utc(row[10]),
            "hostname": row[11],
            "client_version": row[12],
            "status_message": row[13],
            "birdnet_batch_count": int(row[14]),
            "birdnet_source_count": int(row[15]),
            "latest_birdnet_upload_at": format_rfc3339_utc(row[16]),
            "public_url": f"/sites/{row[0]}",
        }
        for row in rows
    ]


def fetch_site_detail(site_id: str) -> dict:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT peer_uuid,
                       wg_ip,
                       network_name,
                       note,
                       site_label,
                       is_active,
                       registered_at,
                       location_recorded_at,
                       latitude,
                       longitude,
                       last_check_in,
                       hostname,
                       version,
                       status_message,
                       birdnet_batch_count,
                       birdnet_source_count,
                       latest_birdnet_upload_at
                FROM sensos.public_sites
                WHERE peer_uuid = %s OR wg_ip = %s;
                """,
                (site_id, site_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Site not found.")
            lookup_wg_ip = row[1]
            cur.execute(
                """
                SELECT count(*)::integer,
                       max(processed_at)
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s;
                """,
                (lookup_wg_ip,),
            )
            birdnet_summary = cur.fetchone()
            cur.execute(
                """
                SELECT count(*)::integer,
                       max(recorded_at)
                FROM sensos.public_site_i2c_recent
                WHERE wg_ip = %s;
                """,
                (lookup_wg_ip,),
            )
            i2c_summary = cur.fetchone()
            cur.execute(
                """
                SELECT receipt_id,
                       hostname,
                       client_version,
                       batch_id,
                       ownership_mode,
                       source_count,
                       first_processed_at,
                       last_processed_at,
                       server_received_at
                FROM sensos.public_site_birdnet_recent
                WHERE wg_ip = %s
                  AND batch_rank <= 12
                ORDER BY server_received_at DESC;
                """,
                (lookup_wg_ip,),
            )
            batches = cur.fetchall()
            cur.execute(
                """
                SELECT top_label,
                       count(*)::integer AS detection_count,
                       max(top_score) AS best_score,
                       max(processed_at) AS latest_processed_at
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s
                GROUP BY top_label
                ORDER BY detection_count DESC, best_score DESC, top_label ASC
                LIMIT 10;
                """,
                (lookup_wg_ip,),
            )
            top_birdnet_labels = cur.fetchall()
            cur.execute(
                """
                SELECT top_label,
                       count(*)::integer AS detection_count,
                       avg(top_score) AS average_score,
                       max(top_score) AS best_score
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s
                GROUP BY top_label
                ORDER BY best_score DESC, average_score DESC, detection_count DESC, top_label ASC
                LIMIT 10;
                """,
                (lookup_wg_ip,),
            )
            top_birdnet_scores = cur.fetchall()
            cur.execute(
                """
                SELECT top_label,
                       count(*)::integer AS detection_count,
                       avg(top_likely_score) AS average_occupancy_score,
                       max(top_likely_score) AS best_occupancy_score
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s
                  AND top_likely_score IS NOT NULL
                GROUP BY top_label
                ORDER BY best_occupancy_score DESC,
                         average_occupancy_score DESC,
                         detection_count DESC,
                         top_label ASC
                LIMIT 10;
                """,
                (lookup_wg_ip,),
            )
            top_birdnet_occupancy = cur.fetchall()
            cur.execute(
                """
                SELECT receipt_id,
                       hostname,
                       client_version,
                       batch_id,
                       source_path,
                       processed_at,
                       channel_index,
                       start_sec,
                       end_sec,
                       top_label,
                       top_score,
                       top_likely_score
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s
                  AND detection_rank <= 12
                ORDER BY processed_at DESC, batch_id DESC, channel_index, start_sec;
                """,
                (lookup_wg_ip,),
            )
            detections = cur.fetchall()
            cur.execute(
                """
                SELECT receipt_id,
                       hostname,
                       client_version,
                       batch_id,
                       recorded_at,
                       device_address,
                       sensor_type,
                       reading_key,
                       reading_value,
                       server_received_at
                FROM sensos.public_site_i2c_recent
                WHERE wg_ip = %s
                  AND reading_rank <= 12
                ORDER BY recorded_at DESC, server_received_at DESC;
                """,
                (lookup_wg_ip,),
            )
            readings = cur.fetchall()
    return {
        "peer_uuid": row[0],
        "site_id": row[0],
        "wg_ip": row[1],
        "network_name": row[2],
        "note": row[3],
        "site_label": row[4],
        "is_active": row[5],
        "registered_at": format_rfc3339_utc(row[6]),
        "location_recorded_at": format_rfc3339_utc(row[7]),
        "latitude": float(row[8]),
        "longitude": float(row[9]),
        "last_check_in": format_rfc3339_utc(row[10]),
        "hostname": row[11],
        "client_version": row[12],
        "status_message": row[13],
        "birdnet_batch_count": int(row[14]),
        "birdnet_source_count": int(row[15]),
        "latest_birdnet_upload_at": format_rfc3339_utc(row[16]),
        "public_url": f"/sites/{row[0]}",
        "birdnet_detection_count": int((birdnet_summary[0] or 0) if birdnet_summary else 0),
        "latest_birdnet_result_at": format_rfc3339_utc(
            birdnet_summary[1] if birdnet_summary else None
        ),
        "i2c_reading_count": int((i2c_summary[0] or 0) if i2c_summary else 0),
        "latest_i2c_reading_at": format_rfc3339_utc(i2c_summary[1] if i2c_summary else None),
        "recent_birdnet_batches": [
            {
                "receipt_id": batch[0],
                "hostname": batch[1],
                "client_version": batch[2],
                "batch_id": batch[3],
                "ownership_mode": batch[4],
                "source_count": batch[5],
                "first_processed_at": format_rfc3339_utc(batch[6]),
                "last_processed_at": format_rfc3339_utc(batch[7]),
                "server_received_at": format_rfc3339_utc(batch[8]),
            }
            for batch in batches
        ],
        "top_birdnet_summaries": [
            {
                "label": summary[0],
                "detection_count": int(summary[1]),
                "best_score": float(summary[2]) if summary[2] is not None else None,
                "latest_processed_at": format_rfc3339_utc(summary[3]),
            }
            for summary in top_birdnet_labels
        ],
        "top_birdnet_score_summaries": [
            {
                "label": summary[0],
                "detection_count": int(summary[1]),
                "average_score": float(summary[2]) if summary[2] is not None else None,
                "best_score": float(summary[3]) if summary[3] is not None else None,
            }
            for summary in top_birdnet_scores
        ],
        "top_birdnet_occupancy_summaries": [
            {
                "label": summary[0],
                "detection_count": int(summary[1]),
                "average_occupancy_score": float(summary[2]) if summary[2] is not None else None,
                "best_occupancy_score": float(summary[3]) if summary[3] is not None else None,
            }
            for summary in top_birdnet_occupancy
        ],
        "recent_birdnet_detections": [
            {
                "receipt_id": detection[0],
                "hostname": detection[1],
                "client_version": detection[2],
                "batch_id": detection[3],
                "source_path": detection[4],
                "processed_at": format_rfc3339_utc(detection[5]),
                "channel_index": detection[6],
                "start_sec": float(detection[7]),
                "end_sec": float(detection[8]),
                "top_label": detection[9],
                "top_score": float(detection[10]),
                "top_likely_score": float(detection[11]) if detection[11] is not None else None,
            }
            for detection in detections
        ],
        "recent_i2c_readings": [
            {
                "receipt_id": reading[0],
                "hostname": reading[1],
                "client_version": reading[2],
                "batch_id": reading[3],
                "recorded_at": format_rfc3339_utc(reading[4]),
                "device_address": reading[5],
                "sensor_type": reading[6],
                "reading_key": reading[7],
                "reading_value": float(reading[8]),
                "server_received_at": format_rfc3339_utc(reading[9]),
            }
            for reading in readings
        ],
    }


def fetch_site_synoptic(site_id: str, range_key: str = "day") -> dict:
    site = fetch_site_detail(site_id)
    lookup_wg_ip = site["wg_ip"]
    normalized_range = normalize_synoptic_range(range_key)
    cutoff = datetime.now(timezone.utc) - SYNOPTIC_RANGES[normalized_range]
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT recorded_at,
                       sensor_type,
                       reading_key,
                       reading_value
                FROM sensos.public_site_i2c_recent
                WHERE wg_ip = %s
                  AND recorded_at >= %s
                ORDER BY recorded_at DESC
                LIMIT 12000;
                """,
                (lookup_wg_ip, cutoff),
            )
            sensor_rows = cur.fetchall()
            cur.execute(
                """
                SELECT processed_at,
                       top_label,
                       top_score,
                       top_likely_score
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s
                  AND processed_at >= %s
                ORDER BY processed_at DESC
                LIMIT 12000;
                """,
                (lookup_wg_ip, cutoff),
            )
            birdnet_rows = cur.fetchall()

    sensor_series_map: dict[str, list[dict]] = {}
    for recorded_at, sensor_type, reading_key, reading_value in reversed(sensor_rows):
        series_key = f"{sensor_type} · {reading_key}"
        sensor_series_map.setdefault(series_key, []).append(
            {
                "recorded_at": format_rfc3339_utc(recorded_at),
                "value": float(reading_value),
            }
        )
    sensor_series = sorted(
        (
            {
                "label": label,
                "points": downsample_points(points, 160),
                "latest_value": points[-1]["value"],
                "latest_at": points[-1]["recorded_at"],
            }
            for label, points in sensor_series_map.items()
            if points
        ),
        key=lambda item: len(item["points"]),
        reverse=True,
    )[:6]

    activity_buckets: dict[str, float] = {}
    species_activity: dict[str, float] = {}
    species_events: dict[str, list[dict]] = {}
    for processed_at, top_label, top_score, top_likely_score in reversed(birdnet_rows):
        processed = format_rfc3339_utc(processed_at)
        occupancy = float(top_likely_score) if top_likely_score is not None else float(top_score)
        quality = float(top_score)
        activity = quality * occupancy
        bucket = bucket_birdnet_timestamp(processed_at, normalized_range)
        bucket_key = format_rfc3339_utc(bucket)
        activity_buckets[bucket_key] = activity_buckets.get(bucket_key, 0.0) + activity
        species_activity[top_label] = species_activity.get(top_label, 0.0) + activity
        species_events.setdefault(top_label, []).append(
            {
                "processed_at": processed,
                "activity": activity,
                "top_score": quality,
                "occupancy_score": occupancy,
            }
        )

    birdnet_activity_series = [
        {"processed_at": timestamp, "activity": value}
        for timestamp, value in sorted(activity_buckets.items())
    ]

    dominant_species = [
        label
        for label, _ in sorted(
            species_activity.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:5]
    ]
    dominant_species_timelines = [
        {
            "label": label,
            "points": downsample_points(species_events[label], 120),
            "activity_total": species_activity[label],
            "event_count": len(species_events[label]),
        }
        for label in dominant_species
        if species_events.get(label)
    ]

    site["synoptic_url"] = f"/sites/{site['peer_uuid']}/synoptic"
    site["synoptic_range"] = normalized_range
    site["sensor_series"] = sensor_series
    site["birdnet_activity_series"] = downsample_points(birdnet_activity_series, 120)
    site["dominant_species_timelines"] = dominant_species_timelines
    return site


def render_site_detail_html(site: dict) -> str:
    birdnet_cards = "".join(
        f"""
        <article class="record-card">
          <div><strong>{detection['top_label']}</strong> <span class="dim">score {detection['top_score']:.2f}</span></div>
          <div class="dim">{detection['processed_at'] or 'Unknown time'} · batch {detection['batch_id']} · ch {detection['channel_index']}</div>
          <div class="dim">{detection['start_sec']:.1f}s to {detection['end_sec']:.1f}s</div>
          <div class="mono">{detection['source_path']}</div>
        </article>
        """
        for detection in site["recent_birdnet_detections"]
    ) or '<div class="empty">No BirdNET detections are visible yet for this site.</div>'

    i2c_cards = "".join(
        f"""
        <article class="record-card">
          <div><strong>{reading['sensor_type']}</strong> <span class="dim">{reading['reading_key']}</span></div>
          <div class="dim">value {reading['reading_value']:.3f} · {reading['recorded_at'] or 'Unknown time'}</div>
          <div class="mono">{reading['device_address']} · batch {reading['batch_id']}</div>
        </article>
        """
        for reading in site["recent_i2c_readings"]
    ) or '<div class="empty">No sensor readings are visible yet for this site.</div>'

    batch_cards = "".join(
        f"""
        <article class="record-card">
          <div><strong>Batch {batch['batch_id']}</strong> <span class="dim">{batch['ownership_mode']}</span></div>
          <div class="dim">{batch['source_count']} source files · received {batch['server_received_at'] or 'Unknown time'}</div>
          <div class="mono">{batch['receipt_id']}</div>
        </article>
        """
        for batch in site["recent_birdnet_batches"]
    ) or '<div class="empty">No BirdNET batches are visible yet for this site.</div>'

    top_birdnet_summary_cards = "".join(
        f"""
        <article class="record-card">
          <div><strong>{summary['label']}</strong> <span class="dim">best score {summary['best_score']:.2f}</span></div>
          <div class="dim">{summary['detection_count']} detections · latest {summary['latest_processed_at'] or 'Unknown time'}</div>
        </article>
        """
        for summary in site["top_birdnet_summaries"]
    ) or '<div class="empty">No BirdNET summary labels are visible yet for this site.</div>'

    top_birdnet_score_cards = "".join(
        f"""
        <article class="record-card">
          <div><strong>{summary['label']}</strong> <span class="dim">best {summary['best_score']:.2f}</span></div>
          <div class="dim">avg score {summary['average_score']:.2f} · {summary['detection_count']} detections</div>
        </article>
        """
        for summary in site["top_birdnet_score_summaries"]
    ) or '<div class="empty">No BirdNET score summaries are visible yet for this site.</div>'

    top_birdnet_occupancy_cards = "".join(
        f"""
        <article class="record-card">
          <div><strong>{summary['label']}</strong> <span class="dim">best {summary['best_occupancy_score']:.2f}</span></div>
          <div class="dim">avg occupancy {summary['average_occupancy_score']:.2f} · {summary['detection_count']} detections</div>
        </article>
        """
        for summary in site["top_birdnet_occupancy_summaries"]
    ) or '<div class="empty">No BirdNET occupancy summaries are visible yet for this site.</div>'

    note_html = (
        f"<p class='lede'>{site['note']}</p>" if (site.get("note") or "").strip() else ""
    )
    synoptic_url = f"/sites/{site['peer_uuid']}/synoptic"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{site['site_label']} · SensOS Public Site</title>
  <style>
    :root {{
      --bg: #f3efe6;
      --ink: #1b2420;
      --muted: #61706a;
      --panel: rgba(255,255,255,0.86);
      --border: rgba(27,36,32,0.12);
      --accent: #0c6d62;
      --accent-2: #b45309;
      --shadow: 0 22px 58px rgba(27,36,32,0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      background:
        radial-gradient(circle at top left, rgba(12,109,98,0.18), transparent 26rem),
        radial-gradient(circle at right, rgba(180,83,9,0.14), transparent 24rem),
        linear-gradient(180deg, #faf6ef 0%, var(--bg) 100%);
    }}
    a {{ color: var(--accent); }}
    .shell {{ max-width: 1320px; margin: 0 auto; padding: 1.25rem; }}
    .masthead {{
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      align-items: flex-start;
      margin-bottom: 1rem;
    }}
    .kicker {{
      display: inline-block;
      padding: 0.35rem 0.7rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.75);
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .back-button {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.55rem 0.9rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.78);
      color: var(--ink);
      font: inherit;
      cursor: pointer;
      box-shadow: 0 10px 24px rgba(27,36,32,0.08);
    }}
    .primary-link {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      margin-top: 0.85rem;
      padding: 0.75rem 1rem;
      border-radius: 999px;
      background: linear-gradient(135deg, rgba(12,109,98,0.96), rgba(25,148,135,0.9));
      color: white;
      text-decoration: none;
      font-weight: 700;
      box-shadow: 0 12px 28px rgba(12,109,98,0.22);
    }}
    h1 {{
      margin: 0.55rem 0 0;
      font-size: clamp(2.2rem, 4vw, 4rem);
      letter-spacing: -0.05em;
    }}
    .lede {{ color: var(--muted); max-width: 42rem; }}
    .meta {{ color: var(--muted); font-size: 0.95rem; }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr);
      gap: 1rem;
    }}
    .stack {{ display: grid; gap: 1rem; align-content: start; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
      padding: 1rem;
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(280px, 0.9fr);
      gap: 1rem;
      align-items: stretch;
    }}
    .coord-card {{
      min-height: 15rem;
      border-radius: 20px;
      background:
        linear-gradient(135deg, rgba(12,109,98,0.92), rgba(12,109,98,0.62)),
        linear-gradient(180deg, #b6d4cf, #8db7ae);
      color: white;
      padding: 1rem;
      display: grid;
      align-content: end;
    }}
    .coord-card .mono {{ opacity: 0.92; }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.8rem;
    }}
    .metric {{
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 0.85rem 0.9rem;
      background: rgba(255,255,255,0.72);
      min-width: 0;
    }}
    .metric-label {{
      color: var(--muted);
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .metric-value {{
      margin-top: 0.25rem;
      font-size: 1.5rem;
      letter-spacing: -0.05em;
      font-weight: 700;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .section-title {{
      margin: 0 0 0.75rem;
      font-size: 1rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .record-list {{ display: grid; gap: 0.7rem; }}
    .record-card {{
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 0.85rem 0.9rem;
      background: rgba(255,255,255,0.7);
    }}
    .mono {{
      font-family: "SFMono-Regular", "Menlo", "Consolas", monospace;
      font-size: 0.9rem;
      word-break: break-word;
    }}
    .dim {{ color: var(--muted); }}
    .empty {{
      border: 1px dashed var(--border);
      border-radius: 16px;
      padding: 1rem;
      color: var(--muted);
      background: rgba(255,255,255,0.45);
    }}
    @media (max-width: 980px) {{
      .layout, .hero {{ grid-template-columns: 1fr; }}
      .metric-grid {{ grid-template-columns: 1fr 1fr; }}
    }}
  </style>
</head>
  <body>
  <div class="shell">
    <div class="masthead">
      <div>
        <button class="back-button" type="button" onclick="goBack()">← Return to previous view</button>
        <div class="kicker">SensOS Public Site</div>
        <h1>{site['site_label']}</h1>
        {note_html}
        <a class="primary-link" href="{synoptic_url}">Open Synoptic Time Series</a>
      </div>
      <div class="meta">
        <div><a href="/">Back to all field sites</a></div>
        <div>{site['network_name']} · {site['client_version'] or 'unknown client version'}</div>
        <div>{site['last_check_in'] or 'No check-in yet'}</div>
      </div>
    </div>
    <div class="layout">
      <main class="stack">
        <section class="panel hero">
          <div class="coord-card">
            <div class="dim">Coordinates</div>
            <div style="font-size:2rem;font-weight:700;letter-spacing:-0.05em;">{site['latitude']:.4f}, {site['longitude']:.4f}</div>
            <div class="mono">{site['wg_ip']} · {site['peer_uuid']}</div>
          </div>
          <div class="metric-grid">
            <div class="metric"><div class="metric-label">Latest Check-In</div><div class="metric-value">{site['last_check_in'] or 'Never'}</div></div>
            <div class="metric"><div class="metric-label">BirdNET Detections</div><div class="metric-value">{site['birdnet_detection_count']}</div></div>
            <div class="metric"><div class="metric-label">Sensor Readings</div><div class="metric-value">{site['i2c_reading_count']}</div></div>
            <div class="metric"><div class="metric-label">BirdNET Batches</div><div class="metric-value">{site['birdnet_batch_count']}</div></div>
            <div class="metric"><div class="metric-label">BirdNET Sources</div><div class="metric-value">{site['birdnet_source_count']}</div></div>
            <div class="metric"><div class="metric-label">Status</div><div class="metric-value">{site['status_message'] or ('Active' if site['is_active'] else 'Inactive')}</div></div>
          </div>
        </section>
        <section class="panel">
          <h2 class="section-title">Recent BirdNET Detections</h2>
          <div class="record-list">{birdnet_cards}</div>
        </section>
        <section class="panel">
          <h2 class="section-title">Recent Sensor Readings</h2>
          <div class="record-list">{i2c_cards}</div>
        </section>
      </main>
      <aside class="stack">
        <section class="panel">
          <h2 class="section-title">BirdNET Upload Batches</h2>
          <div class="record-list">{batch_cards}</div>
        </section>
        <section class="panel">
          <h2 class="section-title">Top BirdNET Summaries</h2>
          <div class="record-list">{top_birdnet_summary_cards}</div>
        </section>
        <section class="panel">
          <h2 class="section-title">Top Detection Scores</h2>
          <div class="record-list">{top_birdnet_score_cards}</div>
        </section>
        <section class="panel">
          <h2 class="section-title">Top Occupancy Scores</h2>
          <div class="record-list">{top_birdnet_occupancy_cards}</div>
        </section>
      </aside>
    </div>
  </div>
  <script>
    function goBack() {{
      if (window.history.length > 1) {{
        window.history.back();
        return;
      }}
      window.location.assign("/");
    }}
  </script>
</body>
</html>"""


def render_synoptic_html(site: dict) -> str:
    range_key = normalize_synoptic_range(site.get("synoptic_range"))
    range_links = "".join(
        f'<a class="range-pill{" active" if key == range_key else ""}" href="{escape_html(site["synoptic_url"])}?range={key}">{label}</a>'
        for key, label in (("hour", "Hour"), ("day", "Day"), ("week", "Week"), ("month", "Month"))
    )
    sensor_sections = "".join(
        f"""
        <section class="chart-card">
          <div class="chart-head">
            <div>
              <strong>{escape_html(series['label'])}</strong>
              <div class="dim">latest {series['latest_value']:.3f} at {escape_html(series['latest_at'])}</div>
            </div>
          </div>
          <div class="chart">{render_line_chart_svg(series['points'], 'value', '#0c6d62')}</div>
        </section>
        """
        for series in site["sensor_series"]
    ) or '<div class="empty">No sensor time series are visible yet for this site.</div>'

    activity_sections = (
        f"""
        <section class="chart-card">
          <div class="chart-head">
            <div>
              <strong>Bird Activity Intensity</strong>
              <div class="dim">Hourly aggregate of quality score × occupancy score across all detections</div>
            </div>
          </div>
          <div class="chart">{render_bar_chart_svg(site['birdnet_activity_series'], 'activity', '#b45309')}</div>
        </section>
        """
        if site["birdnet_activity_series"]
        else '<div class="empty">No BirdNET activity series are visible yet for this site.</div>'
    )

    dominant_species_sections = "".join(
        f"""
        <section class="chart-card">
          <div class="chart-head">
            <div>
              <strong>{escape_html(series['label'])}</strong>
              <div class="dim">{series['event_count']} events · total activity {series['activity_total']:.2f}</div>
            </div>
          </div>
          <div class="chart">{render_event_timeline_svg(series['points'], 'activity', '#2563eb')}</div>
        </section>
        """
        for series in site["dominant_species_timelines"]
    ) or '<div class="empty">No dominant BirdNET species event series are visible yet for this site.</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape_html(site['site_label'])} · Synoptic Time Series</title>
    <style>
    :root {{
      --bg: #eff3ea;
      --ink: #17201d;
      --muted: #61706a;
      --panel: rgba(255,255,255,0.9);
      --border: rgba(23,32,29,0.12);
      --shadow: 0 20px 54px rgba(23,32,29,0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      background:
        radial-gradient(circle at top left, rgba(12,109,98,0.15), transparent 24rem),
        radial-gradient(circle at right, rgba(180,83,9,0.10), transparent 22rem),
        linear-gradient(180deg, #f7f4ed 0%, var(--bg) 100%);
    }}
    a {{ color: #0c6d62; }}
    .shell {{ max-width: 1480px; margin: 0 auto; padding: 0.8rem 1rem 1.1rem; }}
    .masthead {{
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      align-items: flex-start;
      margin-bottom: 0.65rem;
    }}
    .back-button {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.45rem 0.75rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.78);
      color: var(--ink);
      font: inherit;
      cursor: pointer;
      box-shadow: 0 10px 24px rgba(27,36,32,0.08);
    }}
    h1 {{ margin: 0.25rem 0 0; font-size: clamp(2rem, 3.5vw, 3.2rem); letter-spacing: -0.05em; line-height: 0.96; }}
    .lede {{ color: var(--muted); max-width: 46rem; margin-top: 0.35rem; margin-bottom: 0; font-size: 0.98rem; }}
    .meta {{ color: var(--muted); font-size: 0.95rem; }}
    .stack {{ display: grid; gap: 1rem; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
      padding: 0.8rem 0.9rem;
    }}
    .section-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.7rem;
      margin-bottom: 0.55rem;
      flex-wrap: wrap;
    }}
    .section-title {{
      margin: 0;
      font-size: 1rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .chart-grid {{ display: grid; gap: 0.75rem; }}
    .chart-card {{
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 0.72rem 0.8rem;
      background: rgba(255,255,255,0.72);
    }}
    .chart-head {{ display: flex; justify-content: space-between; gap: 1rem; margin-bottom: 0.45rem; }}
    .chart {{
      height: 180px;
      border-radius: 14px;
      background: linear-gradient(180deg, rgba(247,244,237,0.9), rgba(239,243,234,0.7));
      border: 1px solid rgba(23,32,29,0.08);
      overflow: hidden;
    }}
    .chart svg {{ width: 100%; height: 100%; display: block; }}
    .empty {{
      border: 1px dashed var(--border);
      border-radius: 16px;
      padding: 1rem;
      color: var(--muted);
      background: rgba(255,255,255,0.45);
    }}
    .dim {{ color: var(--muted); }}
    .range-pills {{
      display: inline-flex;
      gap: 0.35rem;
      align-items: center;
      flex-wrap: wrap;
    }}
    .range-pill {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 3.5rem;
      padding: 0.28rem 0.58rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.78);
      color: var(--muted);
      text-decoration: none;
      font-size: 0.82rem;
      line-height: 1;
    }}
    .range-pill.active {{
      background: #0c6d62;
      border-color: #0c6d62;
      color: #f7f4ed;
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div>
        <button class="back-button" type="button" onclick="goBack()">← Return to previous view</button>
        <h1>{escape_html(site['site_label'])}</h1>
        <div class="lede">Synoptic view highlighting environmental sensor patterns, overall bird activity intensity, and discrete event timelines for dominant species at this site.</div>
      </div>
      <div class="meta">
        <div><a href="{escape_html(site['public_url'])}">Back to client dashboard</a></div>
        <div>{escape_html(site['network_name'])} · {escape_html(site['client_version'] or 'unknown client version')}</div>
      </div>
    </div>
    <div class="stack">
      <section class="panel">
        <div class="section-bar">
          <h2 class="section-title">Environmental Sensor Time Series</h2>
          <div class="range-pills">{range_links}</div>
        </div>
        <div class="chart-grid">{sensor_sections}</div>
      </section>
      <section class="panel">
        <h2 class="section-title">Bird Activity Intensity</h2>
        <div class="chart-grid">{activity_sections}</div>
      </section>
      <section class="panel">
        <h2 class="section-title">Dominant Species Event Timelines</h2>
        <div class="chart-grid">{dominant_species_sections}</div>
      </section>
    </div>
  </div>
  <script>
    function goBack() {{
      if (window.history.length > 1) {{
        window.history.back();
        return;
      }}
      window.location.assign("{escape_html(site['public_url'])}");
    }}
  </script>
</body>
</html>"""


def render_index_html() -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SensOS Public Dashboard</title>
  <style>
    :root {{
      --bg: #edf1ea;
      --panel: rgba(255,255,255,0.9);
      --ink: #17201d;
      --muted: #5c6760;
      --accent: #0c6d62;
      --accent-2: #d97706;
      --border: rgba(23,32,29,0.12);
      --shadow: 0 24px 60px rgba(23,32,29,0.12);
      --marker: #0c6d62;
      --marker-active: #d97706;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      background:
        radial-gradient(circle at top left, rgba(12,109,98,0.18), transparent 28rem),
        radial-gradient(circle at top right, rgba(217,119,6,0.14), transparent 22rem),
        linear-gradient(180deg, #f7f4ed 0%, var(--bg) 100%);
    }}
    .shell {{ max-width: 1480px; margin: 0 auto; padding: 1.25rem; }}
    .masthead {{
      display: flex; justify-content: space-between; align-items: flex-start;
      gap: 1rem; margin-bottom: 1rem;
    }}
    h1 {{ margin: 0; font-size: clamp(2rem, 4vw, 3.4rem); letter-spacing: -0.05em; }}
    .subhead {{ color: var(--muted); max-width: 52rem; margin-top: 0.45rem; }}
    .meta {{ color: var(--muted); font-size: 0.92rem; }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.8fr) minmax(320px, 0.9fr);
      gap: 1rem;
      min-height: calc(100vh - 8rem);
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      overflow: hidden;
    }}
    .map-wrap {{
      position: relative;
      min-height: 72vh;
      background: linear-gradient(180deg, #dfeae6 0%, #cedbd4 100%);
    }}
    .map-toolbar {{
      position: absolute;
      top: 1rem;
      left: 1rem;
      z-index: 4;
      display: flex;
      gap: 0.6rem;
      align-items: center;
      flex-wrap: wrap;
    }}
    .toolbar-button {{
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.82);
      color: var(--ink);
      padding: 0.65rem 0.9rem;
      font: inherit;
      box-shadow: 0 10px 30px rgba(23,32,29,0.08);
    }}
    .toolbar-button {{
      cursor: pointer;
    }}
    .map-stage {{
      position: absolute;
      inset: 0;
      overflow: hidden;
    }}
    canvas#mapCanvas {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
    }}
    .markers {{
      position: absolute;
      inset: 0;
      pointer-events: none;
    }}
    .marker {{
      position: absolute;
      width: 18px;
      height: 18px;
      margin-left: -9px;
      margin-top: -9px;
      border-radius: 999px;
      border: 3px solid rgba(255,255,255,0.96);
      background: var(--marker);
      box-shadow: 0 0 0 1px rgba(23,32,29,0.18), 0 8px 18px rgba(12,109,98,0.25);
      pointer-events: none;
      opacity: 0.9;
      transition: transform 160ms ease, opacity 160ms ease, background 160ms ease;
    }}
    .marker.active {{
      background: var(--marker-active);
      transform: scale(1.22);
      opacity: 1;
      z-index: 3;
    }}
    .marker.dim {{
      opacity: 0.58;
    }}
    .map-caption {{
      position: absolute;
      left: 1rem;
      bottom: 1rem;
      z-index: 4;
      color: var(--muted);
      font-size: 0.92rem;
      max-width: 28rem;
    }}
    .sidebar {{
      padding: 1rem;
      display: grid;
      gap: 1rem;
      align-content: start;
    }}
    .sidebar h2 {{
      margin: 0;
      font-size: 1.4rem;
      letter-spacing: -0.04em;
    }}
    .sidebar p, .dim {{
      color: var(--muted);
    }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.7rem;
    }}
    .metric {{
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 0.85rem 0.9rem;
      background: rgba(255,255,255,0.62);
      min-width: 0;
    }}
    .metric-value {{
      font-size: 1.5rem;
      font-weight: 700;
      letter-spacing: -0.05em;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .section-title {{
      margin: 0 0 0.55rem;
      font-size: 1rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .site-list {{
      display: grid;
      gap: 0.65rem;
    }}
    .site-list button {{
      width: 100%;
      text-align: left;
      background: rgba(255,255,255,0.76);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 0.8rem 0.9rem;
      cursor: pointer;
      font: inherit;
      color: var(--ink);
    }}
    .record-list {{
      display: grid;
      gap: 0.65rem;
      max-height: 20rem;
      overflow: auto;
    }}
    .record-card {{
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 0.8rem 0.9rem;
      background: rgba(255,255,255,0.7);
    }}
    .mono {{
      font-family: "SFMono-Regular", "Menlo", "Consolas", monospace;
      font-size: 0.92rem;
    }}
    @media (max-width: 980px) {{
      .layout {{ grid-template-columns: 1fr; }}
      .map-wrap {{ min-height: 56vh; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div>
        <h1>Field Sites</h1>
        <div class="subhead">All instrumented sites stay visible at every zoom level. Click a site to inspect it. If one click resolves multiple nearby sites, the map automatically zooms to that local group instead of collapsing points into a cluster.</div>
      </div>
      <div class="meta">Public dashboard · version {current_version()}</div>
    </div>
    <div class="layout">
      <section class="panel map-wrap">
        <div class="map-toolbar">
          <button class="toolbar-button" id="resetViewButton" type="button">Reset View</button>
        </div>
        <div class="map-stage" id="mapStage">
          <canvas id="mapCanvas"></canvas>
          <div class="markers" id="markersLayer"></div>
        </div>
        <div class="map-caption" id="mapCaption">Loading mapped sites…</div>
      </section>
      <aside class="panel sidebar">
        <div>
          <div class="section-title">Selected Site</div>
          <h2 id="siteTitle">Choose a site</h2>
          <p id="siteSubtitle">Click a mapped point to open site details.</p>
        </div>
        <div class="metric-grid" id="metricGrid"></div>
        <section>
          <div class="section-title">Disambiguation</div>
          <div id="chooserBlock" class="dim">When multiple nearby points are clicked together, the map will zoom to their local bounds. If they still overlap at max zoom, they appear here as an explicit list.</div>
        </section>
        <section>
          <div class="section-title">Recent BirdNET Results</div>
          <div id="birdnetList" class="record-list"><div class="dim">Select a site to inspect recent BirdNET detections.</div></div>
        </section>
        <section>
          <div class="section-title">Recent Sensor Data</div>
          <div id="i2cList" class="record-list"><div class="dim">Select a site to inspect recent sensor readings.</div></div>
        </section>
      </aside>
    </div>
  </div>
  <script>
    const worldBounds = {{ lonMin: -180, lonMax: 180, latMin: -90, latMax: 90 }};
    const minLonSpan = 1.4;
    const minLatSpan = 1.0;
    const tileSize = 256;
    const maxTileZoom = 17;
    const satelliteTileTemplate = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}";
    let currentView = {{ ...worldBounds }};
    let sites = [];
    let activeSiteId = null;
    let chooserSites = [];
    const tileCache = new Map();

    const mapStage = document.getElementById("mapStage");
    const canvas = document.getElementById("mapCanvas");
    const markersLayer = document.getElementById("markersLayer");
    const mapCaption = document.getElementById("mapCaption");
    const resetViewButton = document.getElementById("resetViewButton");
    const siteTitle = document.getElementById("siteTitle");
    const siteSubtitle = document.getElementById("siteSubtitle");
    const metricGrid = document.getElementById("metricGrid");
    const chooserBlock = document.getElementById("chooserBlock");
    const birdnetList = document.getElementById("birdnetList");
    const i2cList = document.getElementById("i2cList");

    function clampView(view) {{
      const lonSpan = Math.max(view.lonMax - view.lonMin, minLonSpan);
      const latSpan = Math.max(view.latMax - view.latMin, minLatSpan);
      const lonCenter = (view.lonMin + view.lonMax) / 2;
      const latCenter = (view.latMin + view.latMax) / 2;
      return {{
        lonMin: Math.max(-180, lonCenter - lonSpan / 2),
        lonMax: Math.min(180, lonCenter + lonSpan / 2),
        latMin: Math.max(-90, latCenter - latSpan / 2),
        latMax: Math.min(90, latCenter + latSpan / 2),
      }};
    }}

    function isAtMaxZoom() {{
      return (currentView.lonMax - currentView.lonMin) <= minLonSpan * 1.02 &&
             (currentView.latMax - currentView.latMin) <= minLatSpan * 1.02;
    }}

    function resizeCanvas() {{
      const rect = mapStage.getBoundingClientRect();
      canvas.width = rect.width * window.devicePixelRatio;
      canvas.height = rect.height * window.devicePixelRatio;
      canvas.style.width = rect.width + "px";
      canvas.style.height = rect.height + "px";
      render();
    }}

    function project(lon, lat) {{
      const rect = mapStage.getBoundingClientRect();
      const x = ((lon - currentView.lonMin) / (currentView.lonMax - currentView.lonMin)) * rect.width;
      const y = ((currentView.latMax - lat) / (currentView.latMax - currentView.latMin)) * rect.height;
      return {{ x, y }};
    }}

    function visibleSites() {{
      return sites.filter((site) =>
        site.longitude >= currentView.lonMin &&
        site.longitude <= currentView.lonMax &&
        site.latitude >= currentView.latMin &&
        site.latitude <= currentView.latMax
      );
    }}

    function drawMap() {{
      const ctx = canvas.getContext("2d");
      const rect = mapStage.getBoundingClientRect();
      ctx.setTransform(window.devicePixelRatio, 0, 0, window.devicePixelRatio, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);
      drawSatelliteBasemap(ctx, rect);

      ctx.strokeStyle = "rgba(23,32,29,0.12)";
      ctx.lineWidth = 1;

      const lonStep = (currentView.lonMax - currentView.lonMin) > 40 ? 10 : 5;
      const latStep = (currentView.latMax - currentView.latMin) > 30 ? 10 : 5;

      for (let lon = Math.ceil(currentView.lonMin / lonStep) * lonStep; lon <= currentView.lonMax; lon += lonStep) {{
        const p = project(lon, 0);
        ctx.beginPath();
        ctx.moveTo(p.x, 0);
        ctx.lineTo(p.x, rect.height);
        ctx.stroke();
      }}

      for (let lat = Math.ceil(currentView.latMin / latStep) * latStep; lat <= currentView.latMax; lat += latStep) {{
        const p = project(0, lat);
        ctx.beginPath();
        ctx.moveTo(0, p.y);
        ctx.lineTo(rect.width, p.y);
        ctx.stroke();
      }}

      ctx.strokeStyle = "rgba(23,32,29,0.35)";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(0.75, 0.75, rect.width - 1.5, rect.height - 1.5);
    }}

    function clampLatitude(lat) {{
      return Math.max(-85.05112878, Math.min(85.05112878, lat));
    }}

    function mercatorWorldPoint(lon, lat, zoom) {{
      const scale = tileSize * (2 ** zoom);
      const clampedLat = clampLatitude(lat);
      const sinLat = Math.sin((clampedLat * Math.PI) / 180);
      const x = ((lon + 180) / 360) * scale;
      const y = (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * scale;
      return {{ x, y }};
    }}

    function chooseBasemapZoom(rect) {{
      const lonSpan = Math.max(0.0001, currentView.lonMax - currentView.lonMin);
      const topLeft = mercatorWorldPoint(currentView.lonMin, currentView.latMax, 0);
      const bottomRight = mercatorWorldPoint(currentView.lonMax, currentView.latMin, 0);
      const mercatorWidth = Math.max(1, bottomRight.x - topLeft.x);
      const mercatorHeight = Math.max(1, bottomRight.y - topLeft.y);
      const zoomFromWidth = Math.log2(rect.width / mercatorWidth);
      const zoomFromHeight = Math.log2(rect.height / mercatorHeight);
      const zoom = Math.floor(Math.min(zoomFromWidth, zoomFromHeight));
      if (!Number.isFinite(zoom)) return 1;
      return Math.max(1, Math.min(maxTileZoom, zoom));
    }}

    function tileUrl(z, x, y) {{
      return satelliteTileTemplate
        .replace("{{z}}", String(z))
        .replace("{{x}}", String(x))
        .replace("{{y}}", String(y));
    }}

    function requestTile(z, x, y) {{
      const maxIndex = 2 ** z;
      if (y < 0 || y >= maxIndex) return null;
      const wrappedX = ((x % maxIndex) + maxIndex) % maxIndex;
      const key = `${{z}}/${{wrappedX}}/${{y}}`;
      let entry = tileCache.get(key);
      if (entry) return entry;
      const image = new Image();
      image.crossOrigin = "anonymous";
      entry = {{ status: "loading", image }};
      image.onload = () => {{
        entry.status = "ready";
        render();
      }};
      image.onerror = () => {{
        entry.status = "error";
        render();
      }};
      image.src = tileUrl(z, wrappedX, y);
      tileCache.set(key, entry);
      return entry;
    }}

    function drawSatelliteBasemap(ctx, rect) {{
      const bg = ctx.createLinearGradient(0, 0, 0, rect.height);
      bg.addColorStop(0, "#16332f");
      bg.addColorStop(1, "#6d8376");
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, rect.width, rect.height);

      const zoom = chooseBasemapZoom(rect);
      const topLeft = mercatorWorldPoint(currentView.lonMin, currentView.latMax, zoom);
      const bottomRight = mercatorWorldPoint(currentView.lonMax, currentView.latMin, zoom);
      const worldWidth = Math.max(1, bottomRight.x - topLeft.x);
      const worldHeight = Math.max(1, bottomRight.y - topLeft.y);
      const scaleX = rect.width / worldWidth;
      const scaleY = rect.height / worldHeight;

      const xStart = Math.floor(topLeft.x / tileSize);
      const xEnd = Math.floor(bottomRight.x / tileSize);
      const yStart = Math.floor(topLeft.y / tileSize);
      const yEnd = Math.floor(bottomRight.y / tileSize);

      for (let tileX = xStart; tileX <= xEnd; tileX += 1) {{
        for (let tileY = yStart; tileY <= yEnd; tileY += 1) {{
          const screenX = (tileX * tileSize - topLeft.x) * scaleX;
          const screenY = (tileY * tileSize - topLeft.y) * scaleY;
          const screenW = tileSize * scaleX;
          const screenH = tileSize * scaleY;
          const tile = requestTile(zoom, tileX, tileY);

          if (tile && tile.status === "ready") {{
            ctx.drawImage(tile.image, screenX, screenY, screenW, screenH);
            continue;
          }}

          ctx.fillStyle = "rgba(255,255,255,0.08)";
          ctx.fillRect(screenX, screenY, screenW, screenH);
        }}
      }}

      const haze = ctx.createLinearGradient(0, 0, rect.width, rect.height);
      haze.addColorStop(0, "rgba(12,109,98,0.12)");
      haze.addColorStop(0.55, "rgba(255,255,255,0.02)");
      haze.addColorStop(1, "rgba(17,24,39,0.16)");
      ctx.fillStyle = haze;
      ctx.fillRect(0, 0, rect.width, rect.height);
    }}

    function renderMarkers() {{
      markersLayer.innerHTML = "";
      for (const site of visibleSites()) {{
        const pos = project(site.longitude, site.latitude);
        const el = document.createElement("div");
        el.className = "marker";
        if (activeSiteId && site.site_id === activeSiteId) {{
          el.classList.add("active");
        }} else if (activeSiteId) {{
          el.classList.add("dim");
        }}
        el.style.left = pos.x + "px";
        el.style.top = pos.y + "px";
        el.dataset.siteId = site.site_id;
        markersLayer.appendChild(el);
      }}
    }}

    function render() {{
      drawMap();
      renderMarkers();
      mapCaption.textContent = `${{sites.length}} mapped sites. Markers stay fixed in screen size so site visibility does not depend on zoom. Local overlap is intentional.`;
    }}

    function fitSites(targetSites) {{
      if (!targetSites.length) return;
      const lonValues = targetSites.map((site) => site.longitude);
      const latValues = targetSites.map((site) => site.latitude);
      const lonPad = Math.max((Math.max(...lonValues) - Math.min(...lonValues)) * 0.35, 0.25);
      const latPad = Math.max((Math.max(...latValues) - Math.min(...latValues)) * 0.35, 0.2);
      currentView = clampView({{
        lonMin: Math.min(...lonValues) - lonPad,
        lonMax: Math.max(...lonValues) + lonPad,
        latMin: Math.min(...latValues) - latPad,
        latMax: Math.max(...latValues) + latPad,
      }});
      render();
    }}

    function setChooserSites(targetSites) {{
      chooserSites = targetSites;
      if (!targetSites.length) {{
        chooserBlock.className = "dim";
        chooserBlock.textContent = "When multiple nearby points are clicked together, the map will zoom to their local bounds. If they still overlap at max zoom, they appear here as an explicit list.";
        return;
      }}
      chooserBlock.className = "site-list";
      chooserBlock.innerHTML = "";
      for (const site of targetSites) {{
        const button = document.createElement("button");
        button.type = "button";
        button.innerHTML = `<strong>${{escapeHtml(site.site_label)}}</strong><div class="dim mono">${{escapeHtml(site.wg_ip)}}</div>`;
        button.addEventListener("click", () => openSiteDashboard(site));
        chooserBlock.appendChild(button);
      }}
    }}

    function escapeHtml(text) {{
      const replacements = {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}};
      return String(text ?? "").replace(/[&<>"']/g, (ch) => replacements[ch]);
    }}

    function formatNumber(value, digits = 2) {{
      if (value === null || value === undefined || !Number.isFinite(Number(value))) return "n/a";
      return Number(value).toFixed(digits).replace(/\\.0+$/, "").replace(/(\\.\\d*?)0+$/, "$1");
    }}

    function basename(path) {{
      const text = String(path ?? "");
      const parts = text.split("/");
      return parts[parts.length - 1] || text;
    }}

    function openSiteDashboard(site) {{
      if (!site || !site.public_url) return;
      window.location.assign(site.public_url);
    }}

    async function loadSiteDetail(siteId) {{
      const response = await fetch(`/api/sites/${{encodeURIComponent(siteId)}}`);
      if (!response.ok) return;
      const site = await response.json();
      activeSiteId = site.site_id;
      setChooserSites([]);
      siteTitle.textContent = site.site_label;
      siteSubtitle.innerHTML = `<a href="${{escapeHtml(site.public_url)}}" target="_blank" rel="noopener">Open public site page</a> · <span class="mono">${{escapeHtml(site.wg_ip)}}</span> · ${{escapeHtml(site.network_name)}}`;
      metricGrid.innerHTML = `
        <div class="metric"><div class="section-title">Latest Check-In</div><div class="metric-value">${{escapeHtml(relativeTime(site.last_check_in))}}</div></div>
        <div class="metric"><div class="section-title">BirdNET Detections</div><div class="metric-value">${{site.birdnet_detection_count}}</div><div class="dim">${{escapeHtml(relativeTime(site.latest_birdnet_result_at))}}</div></div>
        <div class="metric"><div class="section-title">Sensor Readings</div><div class="metric-value">${{site.i2c_reading_count}}</div><div class="dim">${{escapeHtml(relativeTime(site.latest_i2c_reading_at))}}</div></div>
        <div class="metric"><div class="section-title">BirdNET Batches</div><div class="metric-value">${{site.birdnet_batch_count}}</div></div>
        <div class="metric"><div class="section-title">BirdNET Sources</div><div class="metric-value">${{site.birdnet_source_count}}</div></div>
        <div class="metric"><div class="section-title">Coordinates</div><div class="metric-value mono">${{site.latitude.toFixed(4)}}, ${{site.longitude.toFixed(4)}}</div></div>
      `;
      birdnetList.innerHTML = "";
      if (!site.recent_birdnet_detections.length) {{
        birdnetList.innerHTML = '<div class="dim">No BirdNET detections are visible yet for this site.</div>';
      }} else {{
        for (const detection of site.recent_birdnet_detections) {{
          const card = document.createElement("div");
          card.className = "record-card";
          card.innerHTML = `
            <div><strong>${{escapeHtml(detection.top_label)}}</strong> <span class="dim">· score ${{escapeHtml(formatNumber(detection.top_score, 2))}}</span></div>
            <div class="dim">${{escapeHtml(basename(detection.source_path))}} · processed ${{escapeHtml(relativeTime(detection.processed_at))}}</div>
            <div class="dim">ch ${{detection.channel_index}} · ${{escapeHtml(formatNumber(detection.start_sec, 1))}}s-${{escapeHtml(formatNumber(detection.end_sec, 1))}}s · batch ${{detection.batch_id}}</div>
            <div class="mono">${{escapeHtml(detection.source_path)}}</div>
          `;
          birdnetList.appendChild(card);
        }}
      }}
      i2cList.innerHTML = "";
      if (!site.recent_i2c_readings.length) {{
        i2cList.innerHTML = '<div class="dim">No sensor readings are visible yet for this site.</div>';
      }} else {{
        for (const reading of site.recent_i2c_readings) {{
          const card = document.createElement("div");
          card.className = "record-card";
          card.innerHTML = `
            <div><strong>${{escapeHtml(reading.sensor_type)}}</strong> <span class="dim">· ${{escapeHtml(reading.reading_key)}}</span></div>
            <div class="dim">value ${{escapeHtml(formatNumber(reading.reading_value, 3))}} · recorded ${{escapeHtml(relativeTime(reading.recorded_at))}}</div>
            <div class="dim">device ${{escapeHtml(reading.device_address)}} · batch ${{reading.batch_id}}</div>
          `;
          i2cList.appendChild(card);
        }}
      }}
      const selected = sites.find((site) => site.site_id === siteId);
      if (selected) {{
        fitSites([selected]);
      }}
      render();
    }}

    function relativeTime(value) {{
      if (!value) return "Never";
      const deltaMs = Date.now() - Date.parse(value);
      if (!Number.isFinite(deltaMs)) return value;
      const sec = Math.max(0, Math.floor(deltaMs / 1000));
      if (sec < 60) return `${{sec}}s ago`;
      if (sec < 3600) return `${{Math.floor(sec / 60)}}m ago`;
      if (sec < 86400) return `${{Math.floor(sec / 3600)}}h ago`;
      return `${{Math.floor(sec / 86400)}}d ago`;
    }}

    function resolveClick(clientX, clientY) {{
      const rect = mapStage.getBoundingClientRect();
      const x = clientX - rect.left;
      const y = clientY - rect.top;
      const candidates = visibleSites()
        .map((site) => {{
          const pos = project(site.longitude, site.latitude);
          const dx = pos.x - x;
          const dy = pos.y - y;
          return {{ site, distance: Math.sqrt(dx * dx + dy * dy) }};
        }})
        .filter((entry) => entry.distance <= 16)
        .sort((a, b) => a.distance - b.distance);

      if (!candidates.length) return;
      if (candidates.length === 1) {{
        openSiteDashboard(candidates[0].site);
        return;
      }}

      const matchedSites = candidates.map((entry) => entry.site);
      if (!isAtMaxZoom()) {{
        fitSites(matchedSites);
        setChooserSites([]);
        return;
      }}

      setChooserSites(matchedSites);
    }}

    async function boot() {{
      const response = await fetch("/api/sites");
      sites = await response.json();
      render();
      if (sites.length) {{
        fitSites(sites);
      }}
    }}

    mapStage.addEventListener("click", (event) => resolveClick(event.clientX, event.clientY));
    resetViewButton.addEventListener("click", () => {{
      currentView = {{ ...worldBounds }};
      activeSiteId = null;
      setChooserSites([]);
      siteTitle.textContent = "Choose a site";
      siteSubtitle.textContent = "Click a mapped point to open site details.";
      metricGrid.innerHTML = "";
      birdnetList.innerHTML = '<div class="dim">Select a site to inspect recent BirdNET detections.</div>';
      i2cList.innerHTML = '<div class="dim">Select a site to inspect recent sensor readings.</div>';
      render();
      if (sites.length) fitSites(sites);
    }});
    window.addEventListener("resize", resizeCanvas);
    resizeCanvas();
    boot();
  </script>
</body>
</html>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ready = False
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM sensos.public_sites;")
                cur.fetchone()
        app.state.ready = True
    except Exception:
        app.state.ready = False
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz():
    if getattr(app.state, "ready", False):
        return {"status": "ok"}
    return JSONResponse(status_code=503, content={"status": "starting"})


@app.get("/api/sites")
def api_sites():
    return fetch_sites()


@app.get("/api/sites/{site_id}")
def api_site(site_id: str):
    return fetch_site_detail(site_id)


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_page(site_id: str):
    return HTMLResponse(render_site_detail_html(fetch_site_detail(site_id)))


@app.get("/sites/{site_id}/synoptic", response_class=HTMLResponse)
def synoptic_site_page(site_id: str, request: Request):
    return HTMLResponse(
        render_synoptic_html(
            fetch_site_synoptic(site_id, request.query_params.get("range"))
        )
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(render_index_html())
