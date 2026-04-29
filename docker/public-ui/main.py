# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import json
import os
import html
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

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

DETAIL_EVIDENCE_RANGES = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "all": None,
}

BIRDNET_RANKING_RANGES = DETAIL_EVIDENCE_RANGES

BIRDNET_RANKING_SORTS = {
    "sum_score_x_likely": {
        "label": "Weighted frequency",
        "metric_label": "Weighted frequency",
        "description": "Summed BirdNET score multiplied by occupancy score across detections in the selected window.",
        "order_sql": "sum_score_x_likely DESC NULLS LAST, sum_score_x_occup DESC NULLS LAST, detection_count DESC, top_label ASC",
        "value_key": "sum_score_x_likely",
    },
    "sum_score_x_occup": {
        "label": "Weighted duration",
        "metric_label": "Weighted duration",
        "description": "Summed clip duration multiplied by BirdNET score and occupancy score across detections in the selected window.",
        "order_sql": "sum_score_x_occup DESC NULLS LAST, max_score_x_occup DESC NULLS LAST, detection_count DESC, top_label ASC",
        "value_key": "sum_score_x_occup",
    },
    "detection_count": {
        "label": "Frequency",
        "metric_label": "Frequency",
        "description": "Counts retained BirdNET detections (runs), treating each detection interval as one occurrence.",
        "order_sql": "detection_count DESC, sum_score_x_occup DESC NULLS LAST, max_score DESC NULLS LAST, top_label ASC",
        "value_key": "detection_count",
    },
    "duration_sec": {
        "label": "Duration",
        "metric_label": "Duration",
        "description": "Summed clip duration across detections in the selected window.",
        "order_sql": "duration_sec DESC NULLS LAST, sum_score_x_occup DESC NULLS LAST, detection_count DESC, top_label ASC",
        "value_key": "duration_sec",
    },
    "max_score": {
        "label": "Max. birdnet score",
        "metric_label": "Max. birdnet score",
        "description": "Largest BirdNET score observed in the selected window.",
        "order_sql": "max_score DESC NULLS LAST, sum_score_x_occup DESC NULLS LAST, detection_count DESC, top_label ASC",
        "value_key": "max_score",
    },
    "max_occup": {
        "label": "Max. prob. presense",
        "metric_label": "Max. prob. presense",
        "description": "Largest occupancy score observed in the selected window.",
        "order_sql": "max_occup DESC NULLS LAST, sum_score_x_occup DESC NULLS LAST, detection_count DESC, top_label ASC",
        "value_key": "max_occup",
    },
    "avg_volume": {
        "label": "Average volume",
        "metric_label": "Average volume",
        "description": "Average retained BirdNET window volume observed in the selected window.",
        "order_sql": "avg_volume DESC NULLS LAST, sum_score_x_occup DESC NULLS LAST, detection_count DESC, top_label ASC",
        "value_key": "avg_volume",
    },
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


def relation_has_column(
    cur, schema_name: str, relation_name: str, column_name: str
) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND column_name = %s
        LIMIT 1;
        """,
        (schema_name, relation_name, column_name),
    )
    return cur.fetchone() is not None


def format_rfc3339_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def escape_html(value) -> str:
    return html.escape(str(value or ""))


def render_local_time(value: str | None, fallback: str = "Unknown time") -> str:
    if not value:
        return escape_html(fallback)
    safe_value = escape_html(value)
    return f'<time class="local-time" data-utc="{safe_value}" datetime="{safe_value}">{safe_value}</time>'


def render_local_time_script() -> str:
    return """
  <script>
    function formatLocalTimestamp(value, options) {
      if (!value) return "";
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return value;
      return new Intl.DateTimeFormat(undefined, options || {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
        timeZoneName: "short"
      }).format(parsed);
    }

    function localizeUtcElements(root) {
      const scope = root || document;
      for (const node of scope.querySelectorAll("[data-utc]")) {
        const utcValue = node.getAttribute("data-utc");
        const mode = node.getAttribute("data-time-style") || "full";
        const options = mode === "tick"
          ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
          : { year: "numeric", month: "short", day: "2-digit", hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short" };
        node.textContent = formatLocalTimestamp(utcValue, options);
      }
      const timezoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || "your browser timezone";
      for (const node of scope.querySelectorAll("[data-browser-timezone]")) {
        node.textContent = timezoneName;
      }
    }

    document.addEventListener("DOMContentLoaded", function() {
      localizeUtcElements(document);
    });
  </script>"""


def normalize_synoptic_range(value: str | None) -> str:
    candidate = (value or "day").strip().lower()
    return candidate if candidate in SYNOPTIC_RANGES else "day"


def normalize_detail_range(value: str | None) -> str:
    candidate = (value or "day").strip().lower()
    return candidate if candidate in DETAIL_EVIDENCE_RANGES else "day"


def normalize_birdnet_ranking_range(value: str | None) -> str:
    candidate = (value or "day").strip().lower()
    return candidate if candidate in BIRDNET_RANKING_RANGES else "day"


def normalize_birdnet_ranking_sort(value: str | None) -> str:
    candidate = (value or "sum_score_x_likely").strip().lower()
    return candidate if candidate in BIRDNET_RANKING_SORTS else "sum_score_x_likely"


def birdnet_species_url(site_id: str, label: str, range_key: str | None = None) -> str:
    path = f"/sites/{site_id}/birdnet-species/{quote(label, safe='')}"
    if range_key:
        return f"{path}?range={quote(range_key, safe='')}"
    return path


def window_cutoff_from_latest(
    latest_timestamp: datetime | None,
    window: timedelta | None,
) -> datetime | None:
    if latest_timestamp is None or window is None:
        return None
    if latest_timestamp.tzinfo is None:
        latest_timestamp = latest_timestamp.replace(tzinfo=timezone.utc)
    return latest_timestamp.astimezone(timezone.utc) - window


def downsample_points(points: list[dict], limit: int) -> list[dict]:
    if len(points) <= limit:
        return points
    if limit <= 2:
        return [points[0], points[-1]]
    step = (len(points) - 1) / float(limit - 1)
    sampled = [
        points[min(round(index * step), len(points) - 1)] for index in range(limit - 1)
    ]
    sampled.append(points[-1])
    deduped: list[dict] = []
    seen: set[tuple[str, float]] = set()
    for point in sampled:
        marker = (
            str(point.get("recorded_at") or point.get("processed_at")),
            float(point.get("value", point.get("activity", 0.0))),
        )
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


def detection_event_timestamp(
    source_path: str | None, start_sec: float | None, processed_at: datetime
) -> datetime:
    if source_path:
        match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)", source_path)
        if match is not None:
            try:
                start_dt = datetime.strptime(
                    match.group(1), "%Y-%m-%dT%H-%M-%SZ"
                ).replace(tzinfo=timezone.utc)
                if start_sec is not None:
                    return start_dt + timedelta(seconds=float(start_sec))
                return start_dt
            except ValueError:
                pass
    if processed_at.tzinfo is None:
        return processed_at.replace(tzinfo=timezone.utc)
    return processed_at.astimezone(timezone.utc)


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
    x_labels: list,
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
    for item in x_labels:
        if isinstance(item, dict):
            x = float(item["x"])
            label = item.get("label")
            utc_value = item.get("utc")
        else:
            x, label = item
            utc_value = None
        parts.append(
            f'<line x1="{x:.2f}" y1="{bottom}" x2="{x:.2f}" y2="{bottom + 6}" stroke="rgba(23,32,29,0.24)" stroke-width="1"></line>'
        )
        if utc_value:
            safe_utc = escape_html(str(utc_value))
            fallback = escape_html(_format_time_tick(str(utc_value)))
            parts.append(
                f'<text x="{x:.2f}" y="{bottom + 18}" text-anchor="middle" font-size="11" fill="rgba(23,32,29,0.62)" data-utc="{safe_utc}" data-time-style="tick">{fallback}</text>'
            )
        else:
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
    values = [
        float(point[value_key]) for point in points if point.get(value_key) is not None
    ]
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
        x_labels = [
            {"x": coords[index][0], "utc": points[index][time_key]}
            for index in tick_indexes
        ]
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
        f"{_render_axes(bounds, min_value, max_value, x_labels)}"
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
    values = [
        float(point[value_key]) for point in points if point.get(value_key) is not None
    ]
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
            {
                "x": left + index * step_x + step_x / 2,
                "utc": points[index]["processed_at"],
            }
            for index in tick_indexes
        ]
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">'
        f"{_render_axes(bounds, 0.0, max_value, x_labels)}" + "".join(rects) + "</svg>"
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
    time_key = "event_at" if points and "event_at" in points[0] else "processed_at"
    timestamps = [
        datetime.fromisoformat(point[time_key].replace("Z", "+00:00"))
        for point in points
    ]
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
        {"x": left, "utc": points[0][time_key]},
        {"x": (left + right) / 2, "utc": points[len(points) // 2][time_key]},
        {"x": right, "utc": points[-1][time_key]},
    ]
    guides = [_render_axes(bounds, 0.0, 1.0, x_labels)]
    marker_radius = 2.2
    for ts, value, point in zip(timestamps, values, points):
        x = left + ((ts - min_ts).total_seconds() / total_seconds) * (right - left)
        y = bottom - value * (bottom - top)
        circles.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{marker_radius:.2f}" fill="{stroke}" opacity="0.9"><title>{escape_html(point[time_key])} · {value:.2f}</title></circle>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">'
        + "".join(guides)
        + "".join(circles)
        + "</svg>"
    )


def render_horizontal_lollipop_svg(
    rows: list[dict],
    value_key: str,
    accent: str,
    label_href_map: dict[str, str] | None = None,
    width: int = 1120,
    row_height: int = 36,
) -> str:
    plot_rows = [row for row in rows if row.get(value_key) is not None]
    if not plot_rows:
        return ""
    values = [float(row[value_key]) for row in plot_rows]
    if not values:
        return ""
    height = max(180, 68 + len(plot_rows) * row_height)
    label_width = 290
    right_pad = 72
    top = 28
    bottom = height - 34
    axis_x = label_width + 18
    right = width - right_pad
    chart_span = max(right - axis_x, 1)
    usable_values = [value for value in values if value >= 0]
    max_value = max(usable_values) if usable_values else max(values)
    max_value = max(max_value, 1e-9)
    row_step = (bottom - top) / max(len(plot_rows), 1)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">',
        f'<line x1="{axis_x}" y1="{top - 8}" x2="{axis_x}" y2="{bottom + 10}" stroke="rgba(23,32,29,0.28)" stroke-width="1"></line>',
    ]
    for frac in (0.25, 0.5, 0.75, 1.0):
        x = axis_x + chart_span * frac
        value = max_value * frac
        parts.append(
            f'<line x1="{x:.2f}" y1="{top - 8}" x2="{x:.2f}" y2="{bottom + 10}" stroke="rgba(23,32,29,0.08)" stroke-width="1" stroke-dasharray="4 6"></line>'
        )
        parts.append(
            f'<text x="{x:.2f}" y="{height - 10}" text-anchor="middle" font-size="11" fill="rgba(23,32,29,0.62)">{escape_html(_format_axis_value(value))}</text>'
        )
    for index, row in enumerate(plot_rows):
        value = float(row[value_key])
        y = top + row_step * index + row_step / 2
        x = axis_x + (max(value, 0.0) / max_value) * chart_span
        label = str(row.get("label") or row.get("top_label") or "Unknown")
        label_markup = (
            f'<text x="{axis_x - 12}" y="{y + 4:.2f}" text-anchor="end" font-size="12" fill="rgba(23,32,29,0.88)">{escape_html(label)}</text>'
        )
        href = (label_href_map or {}).get(label)
        if href:
            label_markup = (
                f'<a href="{escape_html(href)}" target="_self" rel="noopener">{label_markup}</a>'
            )
        parts.append(label_markup)
        parts.append(
            f'<line x1="{axis_x}" y1="{y:.2f}" x2="{x:.2f}" y2="{y:.2f}" stroke="{accent}" stroke-width="3" stroke-linecap="round" opacity="0.72"></line>'
        )
        parts.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="6.5" fill="{accent}" opacity="0.95"></circle>'
        )
        parts.append(
            f'<text x="{min(x + 10, width - 6):.2f}" y="{y + 4:.2f}" text-anchor="start" font-size="11" fill="rgba(23,32,29,0.66)">{escape_html(_format_axis_value(value))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


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
                       birdnet_detection_count,
                       birdnet_source_count,
                       latest_birdnet_result_at
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
            "birdnet_detection_count": int(row[14]),
            "birdnet_source_count": int(row[15]),
            "latest_birdnet_result_at": format_rfc3339_utc(row[16]),
            "public_url": f"/sites/{row[0]}",
        }
        for row in rows
    ]


def fetch_site_detail(site_id: str, evidence_range: str | None = None) -> dict:
    normalized_evidence_range = normalize_detail_range(evidence_range)
    evidence_window = DETAIL_EVIDENCE_RANGES[normalized_evidence_range]
    with get_db() as conn:
        with conn.cursor() as cur:
            has_window_volume = relation_has_column(
                cur,
                "sensos",
                "public_site_birdnet_detections",
                "volume",
            )
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
                       birdnet_detection_count,
                       birdnet_source_count,
                       latest_birdnet_result_at
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
            anchored_evidence_cutoff = window_cutoff_from_latest(
                birdnet_summary[1] if birdnet_summary else None,
                evidence_window,
            )
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
            if anchored_evidence_cutoff is None:
                cur.execute(
                    """
                    SELECT top_label,
                           count(*)::integer AS detection_count,
                           sum(top_score * coalesce(top_likely_score, top_score)) AS evidence_weight,
                           avg(top_score) AS average_score,
                           max(top_score) AS best_score,
                           max(processed_at) AS latest_processed_at
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                    GROUP BY top_label
                    ORDER BY evidence_weight DESC,
                             best_score DESC,
                             detection_count DESC,
                             top_label ASC
                    LIMIT 8;
                    """,
                    (lookup_wg_ip,),
                )
            else:
                cur.execute(
                    """
                    SELECT top_label,
                           count(*)::integer AS detection_count,
                           sum(top_score * coalesce(top_likely_score, top_score)) AS evidence_weight,
                           avg(top_score) AS average_score,
                           max(top_score) AS best_score,
                           max(processed_at) AS latest_processed_at
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND processed_at >= %s
                    GROUP BY top_label
                    ORDER BY evidence_weight DESC,
                             best_score DESC,
                             detection_count DESC,
                             top_label ASC
                    LIMIT 8;
                    """,
                    (lookup_wg_ip, anchored_evidence_cutoff),
                )
            top_birdnet_evidence = cur.fetchall()
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
                (
                    """
                    SELECT hostname,
                           client_version,
                           source_path,
                           processed_at,
                           clip_end_time,
                           channel_index,
                           max_score_start_frame,
                           start_sec,
                           end_sec,
                           volume,
                           top_label,
                           top_score,
                           top_likely_score
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND detection_rank <= 12
                    ORDER BY processed_at DESC, channel_index, max_score_start_frame;
                    """
                    if has_window_volume
                    else """
                    SELECT hostname,
                           client_version,
                           source_path,
                           processed_at,
                           clip_end_time,
                           channel_index,
                           max_score_start_frame,
                           start_sec,
                           end_sec,
                           NULL::double precision AS volume,
                           top_label,
                           top_score,
                           top_likely_score
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND detection_rank <= 12
                    ORDER BY processed_at DESC, channel_index, max_score_start_frame;
                    """
                ),
                (lookup_wg_ip,),
            )
            detections = cur.fetchall()
            cur.execute(
                """
                SELECT hostname,
                       client_version,
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
        "birdnet_detection_count": int(row[14]),
        "birdnet_source_count": int(row[15]),
        "latest_birdnet_result_at": format_rfc3339_utc(row[16]),
        "public_url": f"/sites/{row[0]}",
        "evidence_range": normalized_evidence_range,
        "birdnet_detection_count": int(
            (birdnet_summary[0] or 0) if birdnet_summary else 0
        ),
        "latest_birdnet_result_at": format_rfc3339_utc(
            birdnet_summary[1] if birdnet_summary else None
        ),
        "i2c_reading_count": int((i2c_summary[0] or 0) if i2c_summary else 0),
        "latest_i2c_reading_at": format_rfc3339_utc(
            i2c_summary[1] if i2c_summary else None
        ),
        "top_birdnet_summaries": [
            {
                "label": summary[0],
                "detection_count": int(summary[1]),
                "best_score": float(summary[2]) if summary[2] is not None else None,
                "latest_processed_at": format_rfc3339_utc(summary[3]),
            }
            for summary in top_birdnet_labels
        ],
        "top_birdnet_evidence": [
            {
                "label": summary[0],
                "detection_count": int(summary[1]),
                "evidence_weight": (
                    float(summary[2]) if summary[2] is not None else None
                ),
                "average_score": float(summary[3]) if summary[3] is not None else None,
                "best_score": float(summary[4]) if summary[4] is not None else None,
                "latest_processed_at": format_rfc3339_utc(summary[5]),
            }
            for summary in top_birdnet_evidence
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
                "average_occupancy_score": (
                    float(summary[2]) if summary[2] is not None else None
                ),
                "best_occupancy_score": (
                    float(summary[3]) if summary[3] is not None else None
                ),
            }
            for summary in top_birdnet_occupancy
        ],
        "recent_birdnet_detections": [
            {
                "hostname": detection[0],
                "client_version": detection[1],
                "source_path": detection[2],
                "processed_at": format_rfc3339_utc(detection[3]),
                "clip_end_time": format_rfc3339_utc(detection[4]),
                "channel_index": detection[5],
                "max_score_start_frame": int(detection[6]),
                "start_sec": float(detection[7]),
                "end_sec": float(detection[8]),
                "volume": (
                    float(detection[9]) if detection[9] is not None else None
                ),
                "top_label": detection[10],
                "top_score": float(detection[11]),
                "top_likely_score": (
                    float(detection[12]) if detection[12] is not None else None
                ),
            }
            for detection in detections
        ],
        "recent_i2c_readings": [
            {
                "hostname": reading[0],
                "client_version": reading[1],
                "recorded_at": format_rfc3339_utc(reading[2]),
                "device_address": reading[3],
                "sensor_type": reading[4],
                "reading_key": reading[5],
                "reading_value": float(reading[6]),
                "server_received_at": format_rfc3339_utc(reading[7]),
            }
            for reading in readings
        ],
    }


def fetch_site_status(site_id: str) -> dict:
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
                       birdnet_detection_count,
                       birdnet_source_count,
                       latest_birdnet_result_at
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
                SELECT top_label,
                       count(*)::integer AS detection_count,
                       sum((end_sec - start_sec) * top_score * coalesce(top_likely_score, top_score)) AS evidence_weight,
                       avg(top_score) AS average_score,
                       max(top_score) AS best_score,
                       max(processed_at) AS latest_processed_at
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s
                GROUP BY top_label
                ORDER BY evidence_weight DESC,
                         best_score DESC,
                         detection_count DESC,
                         top_label ASC
                LIMIT 5;
                """,
                (lookup_wg_ip,),
            )
            top_birdnet_evidence = cur.fetchall()
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
        "birdnet_detection_count": int(row[14]),
        "birdnet_source_count": int(row[15]),
        "latest_birdnet_result_at": format_rfc3339_utc(row[16]),
        "public_url": f"/sites/{row[0]}",
        "status_url": f"/sites/{row[0]}/status",
        "birdnet_rankings_url": f"/sites/{row[0]}/birdnet-rankings",
        "top_birdnet_evidence": [
            {
                "label": summary[0],
                "detection_count": int(summary[1]),
                "evidence_weight": (
                    float(summary[2]) if summary[2] is not None else None
                ),
                "average_score": float(summary[3]) if summary[3] is not None else None,
                "best_score": float(summary[4]) if summary[4] is not None else None,
                "latest_processed_at": format_rfc3339_utc(summary[5]),
            }
            for summary in top_birdnet_evidence
        ],
    }


def render_site_status_html(site: dict) -> str:
    note_html = (
        f'<p class="lede">{escape_html(site["note"])}</p>'
        if site.get("note")
        else '<p class="lede">Public status view sourced from the shared dashboard database.</p>'
    )
    infrastructure_rows = [
        ("Site label", site["site_label"]),
        ("Hostname", site.get("hostname") or "unknown"),
        ("WireGuard IP", site["wg_ip"]),
        ("Network", site["network_name"]),
        ("Client version", site.get("client_version") or "unknown"),
        ("Client active", "yes" if site.get("is_active") else "no"),
        ("Status message", site.get("status_message") or "none"),
        ("Coordinates", f"{site['latitude']:.6f}, {site['longitude']:.6f}"),
        ("Registered at", render_local_time(site.get("registered_at"), "unknown")),
        (
            "Location updated",
            render_local_time(site.get("location_recorded_at"), "unknown"),
        ),
        ("Last check-in", render_local_time(site.get("last_check_in"), "unknown")),
    ]
    infra_cards = "".join(
        f"""
        <article class="row-card">
          <div class="dim">{escape_html(label)}</div>
          <div><strong>{escape_html(value)}</strong></div>
        </article>
        """
        for label, value in infrastructure_rows
    )
    top_species_cards = (
        "".join(
            f"""
            <div class="summary-row">
              <div>
                <div class="summary-label">{escape_html(summary["label"])}</div>
                <div class="summary-bar" style="width:{max((float(summary.get("evidence_weight") or 0.0) / max(float(site["top_birdnet_evidence"][0].get("evidence_weight") or 0.0), 1e-9)) * 100.0, 3.0):.1f}%"></div>
              </div>
              <div class="summary-value">{escape_html(f"{float(summary.get('evidence_weight') or 0.0):.2f}")}</div>
            </div>
            """
            for summary in site["top_birdnet_evidence"]
        )
        if site.get("top_birdnet_evidence")
        else '<div class="empty">No weighted BirdNET detections are available yet.</div>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape_html(site['site_label'])} · Public Status</title>
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
    .shell {{ max-width: 1480px; margin: 0 auto; padding: 0.9rem 1rem 1.2rem; }}
    .masthead {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 1rem;
      margin-bottom: 0.75rem;
    }}
    h1 {{ margin: 0.2rem 0 0; font-size: clamp(2rem, 3.4vw, 3.1rem); letter-spacing: -0.05em; }}
    .lede {{ color: var(--muted); max-width: 52rem; margin: 0.35rem 0 0; }}
    .meta {{ color: var(--muted); font-size: 0.92rem; text-align: right; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 0.8rem;
      margin-bottom: 0.85rem;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: var(--shadow);
      padding: 0.8rem 0.9rem;
      min-width: 0;
    }}
    .metric-label {{
      color: var(--muted);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .metric-value {{
      margin-top: 0.25rem;
      font-size: 1.35rem;
      letter-spacing: -0.04em;
      font-weight: 700;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .layout {{ display: grid; grid-template-columns: minmax(0, 1fr); gap: 0.8rem; }}
    .section-title {{
      margin: 0 0 0.65rem;
      font-size: 0.95rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .list {{ display: grid; gap: 0.55rem; }}
    .row-card {{
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 0.65rem 0.75rem;
      background: rgba(255,255,255,0.66);
    }}
    .mono {{
      font-family: "SFMono-Regular", "Menlo", "Consolas", monospace;
      font-size: 0.86rem;
      word-break: break-word;
    }}
    .summary-card {{
      display: grid;
      gap: 0.6rem;
      margin-top: 0.5rem;
    }}
    .summary-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 0.75rem;
      align-items: center;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 0.58rem 0.72rem;
      background: rgba(255,255,255,0.66);
    }}
    .summary-label {{
      font-weight: 700;
      line-height: 1.2;
      margin-bottom: 0.35rem;
    }}
    .summary-bar {{
      height: 0.4rem;
      border-radius: 999px;
      background: linear-gradient(90deg, #0c6d62, #1d8b78);
    }}
    .summary-value {{
      font-weight: 700;
      letter-spacing: -0.02em;
      color: #0c6d62;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }}
    .dim {{ color: var(--muted); }}
    .empty {{
      border: 1px dashed var(--border);
      border-radius: 14px;
      padding: 0.8rem;
      color: var(--muted);
      background: rgba(255,255,255,0.45);
    }}
    a {{ color: #0c6d62; }}
    @media (max-width: 1080px) {{
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .meta {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div>
        <div><a href="/">← Back to field sites</a></div>
        <h1>{escape_html(site['site_label'])}</h1>
        {note_html}
      </div>
      <div class="meta">
        <div>{escape_html(site['network_name'])} · {escape_html(site['client_version'] or 'unknown client version')}</div>
        <div>{render_local_time(site['last_check_in'], 'No check-in yet')}</div>
        <div>Times shown in <span data-browser-timezone>your browser timezone</span></div>
      </div>
    </div>
    <div class="grid">
      <section class="panel"><div class="metric-label">Client Status</div><div class="metric-value">{escape_html(site['status_message'] or ('Active' if site['is_active'] else 'Inactive'))}</div></section>
      <section class="panel"><div class="metric-label">Hostname</div><div class="metric-value">{escape_html(site['hostname'] or 'unknown')}</div></section>
      <section class="panel"><div class="metric-label">Coordinates</div><div class="metric-value">{site['latitude']:.4f}, {site['longitude']:.4f}</div><div class="dim mono">{escape_html(site['wg_ip'])}</div></section>
      <section class="panel"><div class="metric-label">Last Check-In</div><div class="metric-value">{render_local_time(site['last_check_in'], 'No check-in yet')}</div></section>
      <section class="panel"><div class="metric-label">Public Site</div><div class="metric-value"><a href="{escape_html(site['public_url'])}">Open site dashboard</a></div></section>
    </div>
    <div class="layout">
      <section class="panel">
        <h2 class="section-title">Top 5 Species (Weighted)</h2>
        <div class="dim"><a href="{escape_html(site['birdnet_rankings_url'])}">Open full BirdNET rankings</a></div>
        <div class="summary-card">{top_species_cards}</div>
      </section>
      <section class="panel">
        <h2 class="section-title">Infrastructure Details</h2>
        <div class="list">{infra_cards}</div>
      </section>
    </div>
  </div>
{render_local_time_script()}
</body>
</html>"""


def fetch_site_synoptic(site_id: str, range_key: str = "day") -> dict:
    site = fetch_site_detail(site_id)
    lookup_wg_ip = site["wg_ip"]
    normalized_range = normalize_synoptic_range(range_key)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT max(recorded_at)
                FROM sensos.public_site_i2c_recent
                WHERE wg_ip = %s;
                """,
                (lookup_wg_ip,),
            )
            latest_sensor_at = cur.fetchone()[0]
            sensor_cutoff = window_cutoff_from_latest(
                latest_sensor_at,
                SYNOPTIC_RANGES[normalized_range],
            )
            if sensor_cutoff is None:
                cur.execute(
                    """
                    SELECT recorded_at,
                           sensor_type,
                           reading_key,
                           reading_value
                    FROM sensos.public_site_i2c_recent
                    WHERE wg_ip = %s
                    ORDER BY recorded_at DESC
                    LIMIT 12000;
                    """,
                    (lookup_wg_ip,),
                )
            else:
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
                    (lookup_wg_ip, sensor_cutoff),
                )
            sensor_rows = cur.fetchall()
            cur.execute(
                """
                SELECT max(processed_at)
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s;
                """,
                (lookup_wg_ip,),
            )
            latest_birdnet_at = cur.fetchone()[0]
            birdnet_cutoff = window_cutoff_from_latest(
                latest_birdnet_at,
                SYNOPTIC_RANGES[normalized_range],
            )
            cur.execute(
                (
                    """
                    SELECT source_path,
                           processed_at,
                           start_sec,
                           top_label,
                           top_score,
                           top_likely_score
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                    ORDER BY processed_at DESC
                    LIMIT 12000;
                    """
                    if birdnet_cutoff is None
                    else """
                    SELECT source_path,
                           processed_at,
                           start_sec,
                           top_label,
                           top_score,
                           top_likely_score
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND processed_at >= %s
                    ORDER BY processed_at DESC
                    LIMIT 12000;
                    """
                ),
                (
                    (lookup_wg_ip,)
                    if birdnet_cutoff is None
                    else (lookup_wg_ip, birdnet_cutoff)
                ),
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
    for (
        source_path,
        processed_at,
        start_sec,
        top_label,
        top_score,
        top_likely_score,
    ) in reversed(birdnet_rows):
        event_ts = detection_event_timestamp(source_path, start_sec, processed_at)
        event_at = format_rfc3339_utc(event_ts)
        processed = format_rfc3339_utc(processed_at)
        occupancy = (
            float(top_likely_score)
            if top_likely_score is not None
            else float(top_score)
        )
        quality = float(top_score)
        activity = quality * occupancy
        bucket = bucket_birdnet_timestamp(event_ts, normalized_range)
        bucket_key = format_rfc3339_utc(bucket)
        activity_buckets[bucket_key] = activity_buckets.get(bucket_key, 0.0) + activity
        species_activity[top_label] = species_activity.get(top_label, 0.0) + activity
        species_events.setdefault(top_label, []).append(
            {
                "event_at": event_at,
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


def fetch_site_birdnet_rankings(
    site_id: str,
    sort_key: str | None = None,
    range_key: str | None = None,
) -> dict:
    site = fetch_site_detail(site_id)
    normalized_sort = normalize_birdnet_ranking_sort(sort_key)
    normalized_range = normalize_birdnet_ranking_range(range_key)
    range_cutoff = BIRDNET_RANKING_RANGES[normalized_range]
    sort_config = BIRDNET_RANKING_SORTS[normalized_sort]

    with get_db() as conn:
        with conn.cursor() as cur:
            has_window_volume = relation_has_column(
                cur,
                "sensos",
                "public_site_birdnet_detections",
                "volume",
            )
            avg_volume_expr = (
                "avg(volume) AS avg_volume"
                if has_window_volume
                else "NULL::double precision AS avg_volume"
            )
            if range_cutoff is None:
                cur.execute(
                    f"""
                    SELECT top_label,
                           count(*)::integer AS detection_count,
                           sum(top_score * coalesce(top_likely_score, top_score)) AS sum_score_x_likely,
                           sum((end_sec - start_sec) * top_score * coalesce(top_likely_score, top_score)) AS sum_score_x_occup,
                           sum(greatest(end_sec - start_sec, 0)) AS duration_sec,
                           max(top_score * coalesce(top_likely_score, top_score)) AS max_score_x_occup,
                           max(top_score) AS max_score,
                           max(coalesce(top_likely_score, top_score)) AS max_occup,
                           {avg_volume_expr},
                           max(processed_at) AS latest_processed_at
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                    GROUP BY top_label
                    ORDER BY {sort_config["order_sql"]}
                    """,
                    (site["wg_ip"],),
                )
            else:
                cur.execute(
                    """
                    SELECT max(processed_at)
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s;
                    """,
                    (site["wg_ip"],),
                )
                latest_birdnet_at = cur.fetchone()[0]
                anchored_cutoff = window_cutoff_from_latest(
                    latest_birdnet_at, range_cutoff
                )
                if anchored_cutoff is None:
                    cur.execute(
                        f"""
                        SELECT top_label,
                               count(*)::integer AS detection_count,
                               sum(top_score * coalesce(top_likely_score, top_score)) AS sum_score_x_likely,
                               sum((end_sec - start_sec) * top_score * coalesce(top_likely_score, top_score)) AS sum_score_x_occup,
                               sum(greatest(end_sec - start_sec, 0)) AS duration_sec,
                               max(top_score * coalesce(top_likely_score, top_score)) AS max_score_x_occup,
                               max(top_score) AS max_score,
                               max(coalesce(top_likely_score, top_score)) AS max_occup,
                               {avg_volume_expr},
                               max(processed_at) AS latest_processed_at
                        FROM sensos.public_site_birdnet_detections
                        WHERE wg_ip = %s
                        GROUP BY top_label
                        ORDER BY {sort_config["order_sql"]}
                        """,
                        (site["wg_ip"],),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT top_label,
                               count(*)::integer AS detection_count,
                               sum(top_score * coalesce(top_likely_score, top_score)) AS sum_score_x_likely,
                               sum((end_sec - start_sec) * top_score * coalesce(top_likely_score, top_score)) AS sum_score_x_occup,
                               sum(greatest(end_sec - start_sec, 0)) AS duration_sec,
                               max(top_score * coalesce(top_likely_score, top_score)) AS max_score_x_occup,
                               max(top_score) AS max_score,
                               max(coalesce(top_likely_score, top_score)) AS max_occup,
                               {avg_volume_expr},
                               max(processed_at) AS latest_processed_at
                        FROM sensos.public_site_birdnet_detections
                        WHERE wg_ip = %s
                          AND processed_at >= %s
                        GROUP BY top_label
                        ORDER BY {sort_config["order_sql"]}
                        """,
                        (site["wg_ip"], anchored_cutoff),
                    )
            ranking_rows = cur.fetchall()

    site["birdnet_rankings_url"] = f"/sites/{site['peer_uuid']}/birdnet-rankings"
    site["synoptic_url"] = f"/sites/{site['peer_uuid']}/synoptic"
    site["birdnet_ranking_sort"] = normalized_sort
    site["birdnet_ranking_range"] = normalized_range
    site["birdnet_ranking_sort_label"] = sort_config["label"]
    site["birdnet_ranking_metric_label"] = sort_config["metric_label"]
    site["birdnet_ranking_description"] = sort_config["description"]
    site["birdnet_rankings"] = [
        {
            "label": row[0],
            "detection_count": int(row[1]),
            "sum_score_x_likely": float(row[2]) if row[2] is not None else None,
            "sum_score_x_occup": float(row[3]) if row[3] is not None else None,
            "duration_sec": float(row[4]) if row[4] is not None else None,
            "max_score_x_occup": float(row[5]) if row[5] is not None else None,
            "max_score": float(row[6]) if row[6] is not None else None,
            "max_occup": float(row[7]) if row[7] is not None else None,
            "avg_volume": float(row[8]) if row[8] is not None else None,
            "latest_processed_at": format_rfc3339_utc(row[9]),
        }
        for row in ranking_rows
    ]
    return site


def fetch_site_birdnet_species(
    site_id: str,
    label: str,
    range_key: str | None = None,
) -> dict:
    site = fetch_site_detail(site_id)
    normalized_range = normalize_birdnet_ranking_range(range_key)
    range_window = BIRDNET_RANKING_RANGES[normalized_range]
    with get_db() as conn:
        with conn.cursor() as cur:
            has_window_volume = relation_has_column(
                cur,
                "sensos",
                "public_site_birdnet_detections",
                "volume",
            )
            cur.execute(
                """
                SELECT max(processed_at)
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s
                  AND top_label = %s;
                """,
                (site["wg_ip"], label),
            )
            latest_at = cur.fetchone()[0]
            anchored_cutoff = window_cutoff_from_latest(latest_at, range_window)
            if anchored_cutoff is None:
                cur.execute(
                    (
                        """
                    SELECT processed_at,
                           top_score,
                           top_likely_score,
                           start_sec,
                           end_sec,
                           source_path,
                           channel_index,
                           volume
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND top_label = %s
                    ORDER BY processed_at ASC, channel_index, start_sec;
                    """
                        if has_window_volume
                        else """
                    SELECT processed_at,
                           top_score,
                           top_likely_score,
                           start_sec,
                           end_sec,
                           source_path,
                           channel_index,
                           NULL::double precision AS volume
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND top_label = %s
                    ORDER BY processed_at ASC, channel_index, start_sec;
                    """
                    ),
                    (site["wg_ip"], label),
                )
            else:
                cur.execute(
                    (
                        """
                    SELECT processed_at,
                           top_score,
                           top_likely_score,
                           start_sec,
                           end_sec,
                           source_path,
                           channel_index,
                           volume
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND top_label = %s
                      AND processed_at >= %s
                    ORDER BY processed_at ASC, channel_index, start_sec;
                    """
                        if has_window_volume
                        else """
                    SELECT processed_at,
                           top_score,
                           top_likely_score,
                           start_sec,
                           end_sec,
                           source_path,
                           channel_index,
                           NULL::double precision AS volume
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND top_label = %s
                      AND processed_at >= %s
                    ORDER BY processed_at ASC, channel_index, start_sec;
                    """
                    ),
                    (site["wg_ip"], label, anchored_cutoff),
                )
            rows = cur.fetchall()
    score_points = []
    occupancy_points = []
    weighted_points = []
    detections = []
    for (
        processed_at,
        top_score,
        top_likely_score,
        start_sec,
        end_sec,
        source_path,
        channel_index,
        volume,
    ) in rows:
        processed_text = format_rfc3339_utc(processed_at)
        if processed_text is None:
            continue
        score_value = float(top_score)
        occupancy_value = float(top_likely_score) if top_likely_score is not None else score_value
        duration_sec = max(float(end_sec) - float(start_sec), 0.0)
        weighted_value = duration_sec * score_value * occupancy_value
        score_points.append({"processed_at": processed_text, "value": score_value})
        occupancy_points.append({"processed_at": processed_text, "value": occupancy_value})
        weighted_points.append({"processed_at": processed_text, "activity": weighted_value})
        detections.append(
            {
                "processed_at": processed_text,
                "top_score": score_value,
                "top_likely_score": float(top_likely_score) if top_likely_score is not None else None,
                "start_sec": float(start_sec),
                "end_sec": float(end_sec),
                "source_path": source_path,
                "channel_index": int(channel_index),
                "volume": float(volume) if volume is not None else None,
            }
        )
    site["species_label"] = label
    site["species_range"] = normalized_range
    site["species_range_window"] = range_window
    site["birdnet_species_url"] = birdnet_species_url(site["peer_uuid"], label, normalized_range)
    site["birdnet_rankings_url"] = f"/sites/{site['peer_uuid']}/birdnet-rankings"
    site["species_score_series"] = downsample_points(score_points, 180)
    site["species_occupancy_series"] = downsample_points(occupancy_points, 180)
    site["species_weighted_series"] = downsample_points(weighted_points, 180)
    site["species_detection_count"] = len(detections)
    site["species_latest_at"] = (
        detections[-1]["processed_at"] if detections else None
    )
    site["species_recent_detections"] = list(reversed(detections[-20:]))
    return site


def render_birdnet_species_html(site: dict) -> str:
    selected_range = normalize_birdnet_ranking_range(site.get("species_range"))
    range_links = "".join(
        f'<a class="range-pill{" active" if key == selected_range else ""}" href="{escape_html(birdnet_species_url(site["peer_uuid"], site["species_label"], key))}">{label}</a>'
        for key, label in (
            ("hour", "Hour"),
            ("day", "Day"),
            ("week", "Week"),
            ("month", "Month"),
            ("all", "All"),
        )
    )
    score_chart = (
        render_event_timeline_svg(site["species_score_series"], "value", "#0c6d62")
        if site["species_score_series"]
        else ""
    )
    occupancy_chart = (
        render_line_chart_svg(site["species_occupancy_series"], "value", "#2563eb")
        if site["species_occupancy_series"]
        else ""
    )
    weighted_chart = (
        render_bar_chart_svg(site["species_weighted_series"], "activity", "#b45309")
        if site["species_weighted_series"]
        else ""
    )
    recent_cards = (
        "".join(
            f"""
        <article class="record-card">
          <div><strong>{escape_html(site['species_label'])}</strong> <span class="dim">score {item['top_score']:.2f} · occup {'n/a' if item['top_likely_score'] is None else f"{item['top_likely_score']:.2f}"} · vol {'n/a' if item.get('volume') is None else f"{item['volume']:.3f}"}</span></div>
          <div class="dim">{render_local_time(item['processed_at'])} · ch {item['channel_index']} · {item['start_sec']:.1f}s-{item['end_sec']:.1f}s</div>
          <div class="mono">{escape_html(item['source_path'])}</div>
        </article>
        """
            for item in site["species_recent_detections"]
        )
        or '<div class="empty">No detections for this species in the selected window.</div>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape_html(site['site_label'])} · {escape_html(site['species_label'])} · BirdNET Series</title>
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
    .shell {{ max-width: 1480px; margin: 0 auto; padding: 0.9rem 1rem 1.2rem; }}
    .masthead {{ display:flex; justify-content:space-between; gap:1rem; align-items:flex-start; margin-bottom:0.75rem; }}
    h1 {{ margin: 0.25rem 0 0; font-size: clamp(1.9rem, 3.2vw, 3rem); letter-spacing: -0.05em; }}
    .lede {{ color: var(--muted); max-width: 56rem; margin-top: 0.35rem; }}
    .meta {{ color: var(--muted); font-size: 0.92rem; }}
    .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 20px; box-shadow: var(--shadow); padding: 0.85rem 0.95rem; }}
    .stack {{ display:grid; gap: 0.8rem; }}
    .summary-grid {{ display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.7rem; }}
    .metric {{ border:1px solid var(--border); border-radius: 14px; padding: 0.65rem 0.75rem; background: rgba(255,255,255,0.7); }}
    .metric-label {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; }}
    .metric-value {{ margin-top: 0.2rem; font-size: 1.28rem; font-weight: 700; letter-spacing: -0.04em; }}
    .section-title {{ margin: 0 0 0.6rem; font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }}
    .range-pills {{ display:inline-flex; gap: 0.4rem; flex-wrap: wrap; }}
    .range-pill {{ display:inline-flex; align-items:center; padding:0.28rem 0.6rem; border-radius:999px; border:1px solid var(--border); background: rgba(255,255,255,0.8); color: var(--ink); text-decoration:none; font-size:0.82rem; }}
    .range-pill.active {{ background:#0c6d62; color:white; border-color:#0c6d62; }}
    .chart-wrap {{ min-height: 16rem; border:1px solid rgba(23,32,29,0.08); border-radius: 16px; overflow-x:auto; background: rgba(255,255,255,0.55); }}
    .chart-wrap svg {{ width:100%; min-width:920px; display:block; }}
    .record-list {{ display:grid; gap:0.6rem; }}
    .record-card {{ border:1px solid var(--border); border-radius:14px; padding: 0.68rem 0.75rem; background: rgba(255,255,255,0.68); }}
    .mono {{ font-family: "SFMono-Regular", "Menlo", "Consolas", monospace; font-size: 0.86rem; word-break: break-word; }}
    .dim {{ color: var(--muted); }}
    .empty {{ border:1px dashed var(--border); border-radius:14px; padding: 0.85rem; color: var(--muted); background: rgba(255,255,255,0.45); }}
    a {{ color: #0c6d62; }}
    @media (max-width: 980px) {{
      .masthead {{ flex-direction: column; }}
      .summary-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div>
        <div><a href="{escape_html(site['public_url'])}">← Site page</a> · <a href="{escape_html(site['birdnet_rankings_url'])}">BirdNET rankings</a></div>
        <h1>{escape_html(site['species_label'])}</h1>
        <div class="lede">Species-specific BirdNET score series for {escape_html(site['site_label'])}. Time windows are anchored to the latest timestamp for this species.</div>
      </div>
      <div class="meta">
        <div>{escape_html(site['network_name'])}</div>
        <div>{escape_html(site['client_version'] or 'unknown client version')}</div>
        <div>{render_local_time(site['last_check_in'], 'No check-in yet')}</div>
      </div>
    </div>
    <div class="stack">
      <section class="panel">
        <div class="range-pills">{range_links}</div>
      </section>
      <section class="panel">
        <div class="summary-grid">
          <div class="metric"><div class="metric-label">Detections</div><div class="metric-value">{site['species_detection_count']}</div></div>
          <div class="metric"><div class="metric-label">Latest Detection</div><div class="metric-value">{render_local_time(site['species_latest_at'], 'No detections')}</div></div>
          <div class="metric"><div class="metric-label">Time Window</div><div class="metric-value">{escape_html(selected_range.title())}</div></div>
        </div>
      </section>
      <section class="panel">
        <h2 class="section-title">Detection Score Timeline</h2>
        <div class="chart-wrap">{score_chart or '<div class="empty">No score timeline available.</div>'}</div>
      </section>
      <section class="panel">
        <h2 class="section-title">Occupancy Score</h2>
        <div class="chart-wrap">{occupancy_chart or '<div class="empty">No occupancy series available.</div>'}</div>
      </section>
      <section class="panel">
        <h2 class="section-title">Duration-Weighted Activity</h2>
        <div class="chart-wrap">{weighted_chart or '<div class="empty">No activity series available.</div>'}</div>
      </section>
      <section class="panel">
        <h2 class="section-title">Recent Detections</h2>
        <div class="record-list">{recent_cards}</div>
      </section>
    </div>
  </div>
{render_local_time_script()}
</body>
</html>"""


def render_site_detail_html(site: dict) -> str:
    evidence_range = normalize_detail_range(site.get("evidence_range"))
    species_href = lambda label: birdnet_species_url(site["peer_uuid"], str(label), evidence_range)
    evidence_range_links = "".join(
        f'<a class="range-pill{" active" if key == evidence_range else ""}" href="{escape_html(site["public_url"])}?range={key}">{label}</a>'
        for key, label in (
            ("hour", "Hour"),
            ("day", "Day"),
            ("week", "Week"),
            ("month", "Month"),
            ("all", "All Time"),
        )
    )
    evidence_chart = render_horizontal_lollipop_svg(
        site["top_birdnet_evidence"],
        "evidence_weight",
        "#0c6d62",
        {
            str(summary["label"]): species_href(summary["label"])
            for summary in site["top_birdnet_evidence"]
        },
        width=1160,
        row_height=42,
    )

    birdnet_cards = (
        "".join(
            f"""
        <article class="record-card">
          <div><strong><a href="{escape_html(species_href(detection['top_label']))}">{escape_html(detection['top_label'])}</a></strong> <span class="dim">score {detection['top_score']:.2f} · vol {'n/a' if detection.get('volume') is None else f"{detection['volume']:.3f}"}</span></div>
          <div class="dim">{render_local_time(detection['processed_at'])} · ch {detection['channel_index']}</div>
          <div class="dim">{detection['start_sec']:.1f}s to {detection['end_sec']:.1f}s</div>
          <div class="mono">{detection['source_path']}</div>
        </article>
        """
            for detection in site["recent_birdnet_detections"]
        )
        or '<div class="empty">No BirdNET detections are visible yet for this site.</div>'
    )

    i2c_cards = (
        "".join(
            f"""
        <article class="record-card">
          <div><strong>{reading['sensor_type']}</strong> <span class="dim">{reading['reading_key']}</span></div>
          <div class="dim">value {reading['reading_value']:.3f} · {render_local_time(reading['recorded_at'])}</div>
          <div class="mono">{reading['device_address']}</div>
        </article>
        """
            for reading in site["recent_i2c_readings"]
        )
        or '<div class="empty">No sensor readings are visible yet for this site.</div>'
    )

    top_birdnet_summary_cards = (
        "".join(
            f"""
        <article class="record-card">
          <div><strong><a href="{escape_html(species_href(summary['label']))}">{escape_html(summary['label'])}</a></strong> <span class="dim">best score {summary['best_score']:.2f}</span></div>
          <div class="dim">{summary['detection_count']} detections · latest {render_local_time(summary['latest_processed_at'])}</div>
        </article>
        """
            for summary in site["top_birdnet_summaries"]
        )
        or '<div class="empty">No BirdNET summary labels are visible yet for this site.</div>'
    )

    top_birdnet_score_cards = (
        "".join(
            f"""
        <article class="record-card">
          <div><strong><a href="{escape_html(species_href(summary['label']))}">{escape_html(summary['label'])}</a></strong> <span class="dim">best {summary['best_score']:.2f}</span></div>
          <div class="dim">avg score {summary['average_score']:.2f} · {summary['detection_count']} detections</div>
        </article>
        """
            for summary in site["top_birdnet_score_summaries"]
        )
        or '<div class="empty">No BirdNET score summaries are visible yet for this site.</div>'
    )

    top_birdnet_occupancy_cards = (
        "".join(
            f"""
        <article class="record-card">
          <div><strong><a href="{escape_html(species_href(summary['label']))}">{escape_html(summary['label'])}</a></strong> <span class="dim">best {summary['best_occupancy_score']:.2f}</span></div>
          <div class="dim">avg occupancy {summary['average_occupancy_score']:.2f} · {summary['detection_count']} detections</div>
        </article>
        """
            for summary in site["top_birdnet_occupancy_summaries"]
        )
        or '<div class="empty">No BirdNET occupancy summaries are visible yet for this site.</div>'
    )

    note_html = (
        f"<p class='lede'>{site['note']}</p>"
        if (site.get("note") or "").strip()
        else ""
    )
    synoptic_url = f"/sites/{site['peer_uuid']}/synoptic"
    birdnet_rankings_url = f"/sites/{site['peer_uuid']}/birdnet-rankings"

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
    .shell {{ max-width: 1320px; margin: 0 auto; padding: 0.9rem 1rem 1.25rem; }}
    .masthead {{
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      align-items: flex-start;
      margin-bottom: 0.8rem;
    }}
    .nav-row {{
      display: inline-flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.65rem;
      margin-bottom: 0.35rem;
    }}
    .nav-link {{
      color: var(--muted);
      text-decoration: none;
      font-size: 0.96rem;
    }}
    .nav-link strong {{ color: var(--ink); }}
    .nav-link-inline {{
      color: var(--accent);
      text-decoration: underline;
      text-underline-offset: 0.16em;
      text-decoration-thickness: 0.08em;
      font-size: 0.96rem;
    }}
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
      min-width: 3.6rem;
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
    h1 {{
      margin: 0;
      font-size: clamp(2rem, 3.2vw, 3.2rem);
      letter-spacing: -0.05em;
      line-height: 0.95;
    }}
    .lede {{ color: var(--muted); max-width: 44rem; margin: 0.35rem 0 0; }}
    .meta {{
      color: var(--muted);
      font-size: 0.95rem;
      text-align: right;
      display: grid;
      gap: 0.25rem;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 1rem;
    }}
    .stack {{ display: grid; gap: 1rem; align-content: start; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
      padding: 0.9rem;
    }}
    .summary-strip {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 0.7rem;
    }}
    .evidence-chart-wrap {{
      min-height: 16rem;
      border: 1px solid rgba(23,32,29,0.08);
      border-radius: 16px;
      overflow-x: auto;
      background: rgba(255,255,255,0.55);
    }}
    .evidence-chart-wrap svg {{
      width: 100%;
      min-width: 920px;
      display: block;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr);
      gap: 1rem;
    }}
    .metric {{
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 0.8rem 0.85rem;
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
      .masthead {{ grid-template-columns: 1fr; }}
      .summary-strip, .detail-grid {{ grid-template-columns: 1fr; }}
      .meta {{ text-align: left; }}
    }}
  </style>
</head>
  <body>
  <div class="shell">
    <div class="masthead">
      <div>
        <div class="nav-row">
          <a class="nav-link" href="/" onclick="if (window.history.length > 1) {{ event.preventDefault(); window.history.back(); }}">← <strong>Previous view</strong></a>
          <span class="nav-link">SensOS Public Site</span>
          <a class="nav-link-inline" href="{synoptic_url}">Time series</a>
          <a class="nav-link-inline" href="{birdnet_rankings_url}">BirdNET rankings</a>
        </div>
        <h1>{site['site_label']}</h1>
        {note_html}
      </div>
      <div class="meta">
        <div><a href="/">Back to all field sites</a></div>
        <div>{site['network_name']} · {site['client_version'] or 'unknown client version'}</div>
        <div>{render_local_time(site['last_check_in'], 'No check-in yet')}</div>
        <div>Times shown in <span data-browser-timezone>your browser timezone</span></div>
      </div>
    </div>
    <div class="layout">
      <main class="stack">
        <section class="panel">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:0.8rem;flex-wrap:wrap;margin-bottom:0.8rem;">
            <div>
              <h2 class="section-title" style="margin-bottom:0.2rem;">Top Species By Weighted Frequency</h2>
              <div class="dim">Ranked by summed BirdNET score × occupancy score across retained detections at this site.</div>
            </div>
            <div class="range-pills">{evidence_range_links}</div>
          </div>
          <div class="evidence-chart-wrap">{evidence_chart or '<div class="empty">No BirdNET detections are visible yet for this site.</div>'}</div>
        </section>
        <div class="detail-grid">
          <section class="panel">
            <h2 class="section-title">Recent BirdNET Detections</h2>
            <div class="record-list">{birdnet_cards}</div>
          </section>
          <div class="stack">
            <section class="panel">
              <h2 class="section-title">Top Detection Scores</h2>
              <div class="record-list">{top_birdnet_score_cards}</div>
            </section>
            <section class="panel">
              <h2 class="section-title">Recent Sensor Readings</h2>
              <div class="record-list">{i2c_cards}</div>
            </section>
            <section class="panel">
              <h2 class="section-title">Species Detection Counts</h2>
              <div class="record-list">{top_birdnet_summary_cards}</div>
            </section>
          </div>
        </div>
      </main>
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
{render_local_time_script()}
</body>
</html>"""


def render_synoptic_html(site: dict) -> str:
    range_key = normalize_synoptic_range(site.get("synoptic_range"))
    species_href = lambda label: birdnet_species_url(site["peer_uuid"], str(label), range_key)
    range_links = "".join(
        f'<a class="range-pill{" active" if key == range_key else ""}" href="{escape_html(site["synoptic_url"])}?range={key}">{label}</a>'
        for key, label in (
            ("hour", "Hour"),
            ("day", "Day"),
            ("week", "Week"),
            ("month", "Month"),
        )
    )
    sensor_sections = (
        "".join(
            f"""
        <section class="chart-card">
          <div class="chart-head">
            <div>
              <strong>{escape_html(series['label'])}</strong>
              <div class="dim">latest {series['latest_value']:.3f} at {render_local_time(series['latest_at'])}</div>
            </div>
          </div>
          <div class="chart">{render_line_chart_svg(series['points'], 'value', '#0c6d62')}</div>
        </section>
        """
            for series in site["sensor_series"]
        )
        or '<div class="empty">No sensor time series are visible yet for this site.</div>'
    )

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

    dominant_species_sections = (
        "".join(
            f"""
        <section class="chart-card">
          <div class="chart-head">
            <div>
              <strong><a href="{escape_html(species_href(series['label']))}">{escape_html(series['label'])}</a></strong>
              <div class="dim">{series['event_count']} events · total activity {series['activity_total']:.2f}</div>
            </div>
          </div>
          <div class="chart">{render_event_timeline_svg(series['points'], 'activity', '#2563eb')}</div>
        </section>
        """
            for series in site["dominant_species_timelines"]
        )
        or '<div class="empty">No dominant BirdNET species event series are visible yet for this site.</div>'
    )

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
        <div>Times shown in <span data-browser-timezone>your browser timezone</span></div>
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
{render_local_time_script()}
</body>
</html>"""


def render_birdnet_rankings_html(site: dict) -> str:
    selected_sort = normalize_birdnet_ranking_sort(site.get("birdnet_ranking_sort"))
    selected_range = normalize_birdnet_ranking_range(site.get("birdnet_ranking_range"))
    selected_metric = BIRDNET_RANKING_SORTS[selected_sort]["value_key"]
    plotted_species_count = sum(
        1 for item in site["birdnet_rankings"] if item.get(selected_metric) is not None
    )
    species_href_map = {
        str(item["label"]): birdnet_species_url(
            site["peer_uuid"], str(item["label"]), selected_range
        )
        for item in site["birdnet_rankings"]
    }

    plot_markup = (
        f'<div class="plot-shell">{render_horizontal_lollipop_svg(site["birdnet_rankings"], selected_metric, "#0c6d62", species_href_map)}</div>'
        if plotted_species_count
        else '<div class="empty">No values are available for the selected metric in this time window.</div>'
    )

    ranking_cards = (
        "".join(
            f"""
        <article class="rank-card">
          <div class="rank-main">
            <strong><a href="{escape_html(species_href_map[str(item['label'])])}">{escape_html(item['label'])}</a></strong>
            <span class="metric-pill">{escape_html(site['birdnet_ranking_metric_label'])}: {'n/a' if item.get(selected_metric) is None else _format_axis_value(float(item[selected_metric]))}</span>
          </div>
          <div class="dim">Detections {item['detection_count']} · max score {'n/a' if item['max_score'] is None else _format_axis_value(item['max_score'])} · max occup {'n/a' if item['max_occup'] is None else _format_axis_value(item['max_occup'])}</div>
          <div class="dim">duration-weighted score x occup {'n/a' if item['sum_score_x_occup'] is None else _format_axis_value(item['sum_score_x_occup'])} · avg volume {'n/a' if item['avg_volume'] is None else _format_axis_value(item['avg_volume'])}</div>
          <div class="dim">latest {render_local_time(item['latest_processed_at'])}</div>
        </article>
        """
            for item in site["birdnet_rankings"]
        )
        or '<div class="empty">No ranked BirdNET species are visible for the selected time window.</div>'
    )

    sort_options = "".join(
        f'<option value="{key}"{" selected" if key == selected_sort else ""}>{escape_html(config["label"])}</option>'
        for key, config in BIRDNET_RANKING_SORTS.items()
    )
    range_options = "".join(
        f'<option value="{key}"{" selected" if key == selected_range else ""}>{label}</option>'
        for key, label in (
            ("hour", "Hour"),
            ("day", "Day"),
            ("week", "Week"),
            ("month", "Month"),
            ("all", "All"),
        )
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape_html(site['site_label'])} · BirdNET Rankings</title>
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
    a {{ color: var(--accent); }}
    .shell {{ max-width: 1480px; margin: 0 auto; padding: 1rem; }}
    .masthead {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 1rem;
      margin-bottom: 1rem;
    }}
    .nav-row {{
      display: flex;
      gap: 0.55rem;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 0.55rem;
    }}
    .nav-link, .nav-link-inline {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.42rem 0.72rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.78);
      color: var(--ink);
      text-decoration: none;
    }}
    h1 {{ margin: 0; font-size: clamp(2rem, 3.8vw, 3.4rem); letter-spacing: -0.05em; }}
    .lede {{ color: var(--muted); max-width: 54rem; margin-top: 0.4rem; }}
    .meta {{ color: var(--muted); font-size: 0.95rem; }}
    .stack {{ display: grid; gap: 1rem; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      padding: 1rem;
    }}
    .controls {{
      display: grid;
      grid-template-columns: minmax(480px, 2.3fr) minmax(220px, 1fr);
      gap: 0.8rem;
      align-items: end;
    }}
    label {{
      display: grid;
      gap: 0.35rem;
      font-size: 0.85rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    select {{
      width: 100%;
      padding: 0.72rem 0.85rem;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.88);
      color: var(--ink);
      font: inherit;
    }}
    .section-title {{
      margin: 0 0 0.6rem;
      font-size: 1rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .plot-shell {{
      min-height: 16rem;
      border-radius: 18px;
      border: 1px solid rgba(23,32,29,0.08);
      background: linear-gradient(180deg, rgba(247,244,237,0.92), rgba(237,241,234,0.72));
      overflow-x: auto;
      overflow-y: hidden;
    }}
    .plot-shell svg {{ width: 100%; min-width: 980px; display: block; }}
    .rank-list {{ display: grid; gap: 0.7rem; }}
    .rank-card {{
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 0.85rem 0.9rem;
      background: rgba(255,255,255,0.72);
    }}
    .rank-main {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.8rem;
      flex-wrap: wrap;
      margin-bottom: 0.25rem;
    }}
    .metric-pill {{
      display: inline-flex;
      align-items: center;
      padding: 0.25rem 0.55rem;
      border-radius: 999px;
      background: rgba(12,109,98,0.12);
      color: var(--accent);
      font-size: 0.82rem;
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
      .masthead {{ flex-direction: column; }}
      .controls {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div>
        <div class="nav-row">
          <a class="nav-link" href="{escape_html(site['public_url'])}">← <strong>Site page</strong></a>
          <a class="nav-link-inline" href="{escape_html(site['synoptic_url'])}">Time series</a>
          <span class="nav-link">BirdNET rankings</span>
        </div>
        <h1>{escape_html(site['site_label'])}</h1>
      </div>
      <div class="meta">
        <div>{escape_html(site['network_name'])}</div>
        <div>{escape_html(site['client_version'] or 'unknown client version')}</div>
        <div>{render_local_time(site['last_check_in'], 'No check-in yet')}</div>
        <div>Times shown in <span data-browser-timezone>your browser timezone</span></div>
      </div>
    </div>
    <div class="stack">
      <section class="panel">
        <form method="get" action="{escape_html(site['birdnet_rankings_url'])}" class="controls" id="birdnetRankingControls">
          <label>
            Sort Criteria
            <select name="sort" onchange="submitBirdnetRankingControls()">{sort_options}</select>
          </label>
          <label>
            Time Window
            <select name="range" onchange="submitBirdnetRankingControls()">{range_options}</select>
          </label>
        </form>
      </section>
      <section class="panel">
        <h2 class="section-title">Rankings</h2>
        {plot_markup}
      </section>
      <section class="panel">
        <h2 class="section-title">Ranked Species</h2>
        <div class="rank-list">{ranking_cards}</div>
      </section>
    </div>
  </div>
  <script>
    function submitBirdnetRankingControls() {{
      const form = document.getElementById("birdnetRankingControls");
      if (!form) return;
      form.requestSubmit();
    }}
  </script>
{render_local_time_script()}
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
    .shell {{ max-width: 1480px; margin: 0 auto; padding: 0.7rem 0.9rem; }}
    .masthead {{
      display: flex; justify-content: space-between; align-items: baseline;
      gap: 0.7rem; margin-bottom: 0.55rem;
    }}
    h1 {{ margin: 0; font-size: clamp(1.35rem, 2.2vw, 1.9rem); letter-spacing: -0.03em; }}
    .subhead {{ color: var(--muted); max-width: 52rem; margin-top: 0.45rem; }}
    .meta {{ color: var(--muted); font-size: 0.85rem; }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 2.35fr) minmax(300px, 0.85fr);
      gap: 1rem;
      height: calc(100vh - 4.2rem);
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 20px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      overflow: hidden;
    }}
    .map-wrap {{
      position: relative;
      min-height: 100%;
      background: linear-gradient(180deg, #dfeae6 0%, #cedbd4 100%);
    }}
    .map-toolbar {{
      position: absolute;
      top: 0.7rem;
      left: 0.7rem;
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
      right: 0.7rem;
      bottom: 0.7rem;
      z-index: 4;
      color: var(--muted);
      font-size: 0.82rem;
      max-width: 20rem;
      text-align: right;
      background: rgba(255,255,255,0.7);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0.3rem 0.6rem;
    }}
    .sidebar {{
      padding: 0.75rem;
      display: grid;
      gap: 0.75rem;
      align-content: start;
      overflow: auto;
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
    .summary-link {{
      display: block;
      text-decoration: none;
      color: inherit;
    }}
    .summary-link:hover .summary-card {{
      border-color: rgba(12,109,98,0.45);
      box-shadow: 0 10px 24px rgba(12,109,98,0.12);
    }}
    .summary-card {{
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 0.75rem 0.8rem;
      background: rgba(255,255,255,0.72);
      transition: box-shadow 140ms ease, border-color 140ms ease;
    }}
    .summary-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(3.8rem, auto);
      gap: 0.5rem;
      align-items: center;
      margin-bottom: 0.42rem;
    }}
    .summary-row:last-child {{
      margin-bottom: 0;
    }}
    .summary-label {{
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-size: 0.9rem;
    }}
    .summary-bar {{
      height: 0.46rem;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(12,109,98,0.92), rgba(217,119,6,0.85));
      box-shadow: inset 0 0 0 1px rgba(23,32,29,0.08);
    }}
    .summary-value {{
      text-align: right;
      color: var(--muted);
      font-size: 0.82rem;
      font-variant-numeric: tabular-nums;
    }}
    .mini-plots {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 0.55rem;
    }}
    .mini-plot {{
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 0.45rem 0.5rem 0.5rem;
      background: rgba(255,255,255,0.7);
      min-height: 5.9rem;
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 0.2rem;
    }}
    .mini-plot-title {{
      font-size: 0.76rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      margin: 0;
      line-height: 1.1;
    }}
    .mini-plot-value {{
      font-size: 0.96rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      margin: 0;
      line-height: 1.1;
    }}
    .mini-plot-svg {{
      width: 100%;
      height: 3.2rem;
      display: block;
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
      max-height: 12.5rem;
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
      .layout {{ height: auto; }}
      .map-wrap {{ min-height: 68vh; }}
      .mini-plots {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div><h1>Field Sites</h1></div>
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
          <h2><a id="siteTitleLink" href="#" class="summary-link" style="display:inline;color:inherit;text-decoration:none;"><span id="siteTitle">Map</span></a></h2>
          <p id="siteSubtitle"></p>
        </div>
        <section>
          <div class="section-title">Top Species (Weighted)</div>
          <a id="birdnetSummaryLink" class="summary-link" href="#">
            <div id="birdnetSummary" class="summary-card"></div>
          </a>
        </section>
        <section>
          <div id="sensorMiniPlots" class="mini-plots"></div>
        </section>
        <div class="metric-grid" id="metricGrid"></div>
        <section>
          <div id="chooserBlock" class="site-list"></div>
        </section>
        <section>
          <div class="section-title">Recent BirdNET Results</div>
          <div id="birdnetList" class="record-list"></div>
        </section>
        <section>
          <div class="section-title">Recent Sensor Data</div>
          <div id="i2cList" class="record-list"></div>
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
    const siteTitleLink = document.getElementById("siteTitleLink");
    const siteTitle = document.getElementById("siteTitle");
    const siteSubtitle = document.getElementById("siteSubtitle");
    const birdnetSummaryLink = document.getElementById("birdnetSummaryLink");
    const birdnetSummary = document.getElementById("birdnetSummary");
    const sensorMiniPlots = document.getElementById("sensorMiniPlots");
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
      mapCaption.textContent = `${{sites.length}} sites`;
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
        chooserBlock.className = "site-list";
        chooserBlock.innerHTML = "";
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

    function birdnetSpeciesUrl(site, label) {{
      const range = site && site.evidence_range ? site.evidence_range : "day";
      return `${{site.public_url}}/birdnet-species/${{encodeURIComponent(String(label || ""))}}?range=${{encodeURIComponent(range)}}`;
    }}

    function renderBirdnetSummary(site) {{
      const rankingUrl = `${{site.public_url}}/birdnet-rankings?sort=sum_score_x_likely&range=${{encodeURIComponent(site.evidence_range || "day")}}`;
      birdnetSummaryLink.href = rankingUrl;
      const rows = Array.isArray(site.top_birdnet_evidence) ? site.top_birdnet_evidence.slice(0, 5) : [];
      if (!rows.length) {{
        birdnetSummary.innerHTML = '<div class="dim">No weighted BirdNET detections in this period.</div>';
        return;
      }}
      const maxWeight = Math.max(...rows.map((row) => Number(row.evidence_weight) || 0), 0);
      birdnetSummary.innerHTML = rows.map((row) => {{
        const value = Number(row.evidence_weight) || 0;
        const widthPct = maxWeight > 0 ? Math.max((value / maxWeight) * 100, 3) : 3;
        const href = birdnetSpeciesUrl(site, row.label);
        return `
          <div class="summary-row">
            <div>
              <div class="summary-label"><a href="${{escapeHtml(href)}}">${{escapeHtml(row.label)}}</a></div>
              <div class="summary-bar" style="width:${{widthPct.toFixed(1)}}%"></div>
            </div>
            <div class="summary-value">${{escapeHtml(formatNumber(value, 2))}}</div>
          </div>
        `;
      }}).join("");
    }}

    function sensorSeriesFromRecentReadings(readings, kind) {{
      const values = [];
      const ordered = Array.isArray(readings) ? [...readings].reverse() : [];
      for (const row of ordered) {{
        const key = String(row.reading_key || "").toLowerCase();
        const sensorType = String(row.sensor_type || "").toLowerCase();
        const value = Number(row.reading_value);
        if (!Number.isFinite(value)) continue;
        if (kind === "temperature" && (key.includes("temp") || sensorType.includes("temp"))) {{
          values.push(value);
          continue;
        }}
        if (kind === "humidity" && (key.includes("humid") || sensorType.includes("humid") || key === "rh")) {{
          values.push(value);
          continue;
        }}
        if (kind === "co2" && (key.includes("co2") || key.includes("ppm") || sensorType.includes("co2"))) {{
          values.push(value);
          continue;
        }}
      }}
      return values.slice(-20);
    }}

    function sparklineSvg(values) {{
      const width = 180;
      const height = 56;
      const padX = 4;
      const padY = 5;
      if (!values.length) {{
        return `<svg class="mini-plot-svg" viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none"></svg>`;
      }}
      const minV = Math.min(...values);
      const maxV = Math.max(...values);
      const span = Math.max(maxV - minV, 1e-9);
      const xStep = values.length > 1 ? (width - padX * 2) / (values.length - 1) : 0;
      const points = values.map((v, idx) => {{
        const x = padX + idx * xStep;
        const y = padY + (height - padY * 2) * (1 - ((v - minV) / span));
        return `${{x.toFixed(2)}},${{y.toFixed(2)}}`;
      }}).join(" ");
      const last = values[values.length - 1];
      const dotX = padX + (values.length - 1) * xStep;
      const dotY = padY + (height - padY * 2) * (1 - ((last - minV) / span));
      return `
        <svg class="mini-plot-svg" viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none" aria-hidden="true">
          <polyline fill="none" stroke="rgba(12,109,98,0.92)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" points="${{points}}"></polyline>
          <circle cx="${{dotX.toFixed(2)}}" cy="${{dotY.toFixed(2)}}" r="2.6" fill="rgba(217,119,6,0.95)"></circle>
        </svg>
      `;
    }}

    function renderMiniPlots(site) {{
      const metrics = [
        {{ kind: "temperature", label: "Temperature", unit: "°", valueDigits: 1 }},
        {{ kind: "humidity", label: "Humidity", unit: "%", valueDigits: 1 }},
        {{ kind: "co2", label: "CO₂", unit: "ppm", valueDigits: 0 }},
      ];
      sensorMiniPlots.innerHTML = metrics.map((metric) => {{
        const series = sensorSeriesFromRecentReadings(site.recent_i2c_readings, metric.kind);
        const latest = series.length ? series[series.length - 1] : null;
        const valueText = latest === null ? "n/a" : `${{formatNumber(latest, metric.valueDigits)}}${{metric.unit ? " " + metric.unit : ""}}`;
        return `
          <article class="mini-plot">
            <p class="mini-plot-title">${{escapeHtml(metric.label)}}</p>
            <p class="mini-plot-value">${{escapeHtml(valueText)}}</p>
            ${{sparklineSvg(series)}}
          </article>
        `;
      }}).join("");
    }}

    async function loadSiteDetail(siteId) {{
      const response = await fetch(`/api/sites/${{encodeURIComponent(siteId)}}`);
      if (!response.ok) return;
      const site = await response.json();
      activeSiteId = site.site_id;
      setChooserSites([]);
      siteTitle.textContent = site.site_label;
      siteTitleLink.href = `${{site.public_url}}/status`;
      siteSubtitle.innerHTML = `<a href="${{escapeHtml(site.public_url)}}" target="_blank" rel="noopener">Open public site page</a> · <span class="mono">${{escapeHtml(site.wg_ip)}}</span> · ${{escapeHtml(site.network_name)}}`;
      renderBirdnetSummary(site);
      renderMiniPlots(site);
      metricGrid.innerHTML = `
        <div class="metric"><div class="section-title">Latest Check-In</div><div class="metric-value">${{escapeHtml(relativeTime(site.last_check_in))}}</div></div>
        <div class="metric"><div class="section-title">BirdNET Detections</div><div class="metric-value">${{site.birdnet_detection_count}}</div><div class="dim">${{escapeHtml(relativeTime(site.latest_birdnet_result_at))}}</div></div>
        <div class="metric"><div class="section-title">Sensor Readings</div><div class="metric-value">${{site.i2c_reading_count}}</div><div class="dim">${{escapeHtml(relativeTime(site.latest_i2c_reading_at))}}</div></div>
        <div class="metric"><div class="section-title">BirdNET Sources</div><div class="metric-value">${{site.birdnet_source_count}}</div></div>
        <div class="metric"><div class="section-title">Coordinates</div><div class="metric-value mono">${{site.latitude.toFixed(4)}}, ${{site.longitude.toFixed(4)}}</div></div>
      `;
      birdnetList.innerHTML = "";
      if (!site.recent_birdnet_detections.length) {{
        birdnetList.innerHTML = '<div class="dim">No BirdNET detections are visible yet for this site.</div>';
      }} else {{
        for (const detection of site.recent_birdnet_detections) {{
          const labelHref = birdnetSpeciesUrl(site, detection.top_label);
          const card = document.createElement("div");
          card.className = "record-card";
          card.innerHTML = `
            <div><strong><a href="${{escapeHtml(labelHref)}}">${{escapeHtml(detection.top_label)}}</a></strong> <span class="dim">· score ${{escapeHtml(formatNumber(detection.top_score, 2))}} · vol ${{escapeHtml(formatNumber(detection.volume, 3))}}</span></div>
            <div class="dim">${{escapeHtml(basename(detection.source_path))}} · processed ${{escapeHtml(relativeTime(detection.processed_at))}}</div>
            <div class="dim">ch ${{detection.channel_index}} · ${{escapeHtml(formatNumber(detection.start_sec, 1))}}s-${{escapeHtml(formatNumber(detection.end_sec, 1))}}s</div>
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
            <div class="dim">device ${{escapeHtml(reading.device_address)}}</div>
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
      siteTitle.textContent = "Map";
      siteTitleLink.href = "#";
      siteSubtitle.textContent = "";
      birdnetSummaryLink.href = "#";
      birdnetSummary.innerHTML = "";
      sensorMiniPlots.innerHTML = "";
      metricGrid.innerHTML = "";
      birdnetList.innerHTML = "";
      i2cList.innerHTML = "";
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
def api_site(site_id: str, request: Request):
    return fetch_site_detail(site_id, request.query_params.get("range"))


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_page(site_id: str, request: Request):
    return HTMLResponse(
        render_site_detail_html(
            fetch_site_detail(site_id, request.query_params.get("range"))
        )
    )


@app.get("/sites/{site_id}/synoptic", response_class=HTMLResponse)
def synoptic_site_page(site_id: str, request: Request):
    return HTMLResponse(
        render_synoptic_html(
            fetch_site_synoptic(site_id, request.query_params.get("range"))
        )
    )


@app.get("/sites/{site_id}/birdnet-rankings", response_class=HTMLResponse)
def birdnet_rankings_site_page(site_id: str, request: Request):
    return HTMLResponse(
        render_birdnet_rankings_html(
            fetch_site_birdnet_rankings(
                site_id,
                request.query_params.get("sort"),
                request.query_params.get("range"),
            )
        )
    )


@app.get("/sites/{site_id}/birdnet-species/{label}", response_class=HTMLResponse)
def birdnet_species_site_page(site_id: str, label: str, request: Request):
    return HTMLResponse(
        render_birdnet_species_html(
            fetch_site_birdnet_species(
                site_id,
                label,
                request.query_params.get("range"),
            )
        )
    )


@app.get("/sites/{site_id}/status", response_class=HTMLResponse)
def status_site_page(site_id: str):
    return HTMLResponse(render_site_status_html(fetch_site_status(site_id)))


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(render_index_html())
