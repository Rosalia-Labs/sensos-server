# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import json
import logging
import os
import html
import math
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import psycopg

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

POSTGRES_USER = "sensos_public"
POSTGRES_PASSWORD = os.getenv("PUBLIC_DB_PASSWORD", "sensos-public")
POSTGRES_HOST = "sensos-database"
POSTGRES_DB = "postgres"

logger = logging.getLogger(__name__)

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

BIRDNET_RANKING_VARIABLES = {
    "detection": {"label": "Detection"},
    "duration": {"label": "Duration"},
    "score": {"label": "Score"},
    "occup": {"label": "Occupancy"},
    "volume": {"label": "Volume"},
}

BIRDNET_RANKING_STATISTICS = {
    "sum": {"label": "Sum"},
    "mean": {"label": "Mean"},
    "min": {"label": "Min"},
    "max": {"label": "Max"},
    "median": {"label": "Median"},
}

BIRDNET_RANKING_WEIGHTING = {
    "no": {"label": "Weighted: no"},
    "yes": {"label": "Weighted: yes"},
}


def _theme_override_css() -> str:
    theme = (os.getenv("SENSOS_UI_THEME", "default") or "default").strip().lower()
    if theme != "vscode-dark":
        return ""
    tokens = _vscode_dark_theme_tokens()
    return """
    :root {
      --bg: %(bg)s;
      --panel: %(panel)s;
      --ink: %(ink)s;
      --muted: %(muted)s;
      --accent: %(accent)s;
      --accent-2: %(accent_2)s;
      --border: %(border)s;
      --shadow: %(shadow)s;
      --marker: %(marker)s;
      --marker-active: %(marker_active)s;
    }
    body {
      background:
        radial-gradient(circle at top left, %(bg_glow_1)s, transparent 28rem),
        radial-gradient(circle at top right, %(bg_glow_2)s, transparent 24rem),
        linear-gradient(180deg, %(bg_top)s 0%%, var(--bg) 100%%);
    }
    a, .nav-link-inline { color: var(--accent); }
    .panel { background: var(--panel); }
    select {
      background: %(control_bg)s;
      border-color: %(control_border)s;
      color: %(ink)s;
    }
    .range-pill.active { background: var(--accent); border-color: var(--accent); color: %(bg_top)s; }
    .summary-bar { background: linear-gradient(90deg, %(accent)s, %(weighted)s); }
    .metric-pill { background: %(metric_pill_bg)s; color: %(metric_pill_fg)s; }
    .plot-shell {
      background: linear-gradient(180deg, %(plot_bg_top)s, %(plot_bg_bottom)s);
      border-color: %(plot_border)s;
    }
    """ % tokens


def _vscode_dark_theme_tokens() -> dict[str, str]:
    tokens = {
        "bg": "#0f1115",
        "panel": "rgba(30,34,40,0.94)",
        "ink": "#d4d4d4",
        "muted": "#a7adb5",
        "accent": "#4ec9b0",
        "accent_2": "#ce9178",
        "border": "rgba(255,255,255,0.16)",
        "shadow": "0 24px 60px rgba(0,0,0,0.52)",
        "marker": "#4ec9b0",
        "marker_active": "#d19a66",
        "bg_glow_1": "rgba(78,201,176,0.12)",
        "bg_glow_2": "rgba(209,154,102,0.1)",
        "bg_top": "#0a0c10",
        "control_bg": "rgba(24,27,33,0.95)",
        "control_border": "rgba(255,255,255,0.2)",
        "weighted": "#d19a66",
        "metric_pill_bg": "rgba(78,201,176,0.16)",
        "metric_pill_fg": "#7fe6d2",
        "plot_bg_top": "rgba(17,19,24,0.96)",
        "plot_bg_bottom": "rgba(12,14,18,0.93)",
        "plot_border": "rgba(255,255,255,0.14)",
        "occupancy": "#9cdcfe",
        "alt": "#b5cea8",
    }
    raw = (os.getenv("SENSOS_UI_THEME_VSCODE_DARK_TOKENS", "") or "").strip()
    if not raw:
        return tokens
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in tokens and isinstance(value, str) and value.strip():
                    tokens[key] = value.strip()
    except Exception:
        pass
    return tokens


def _plot_color(name: str) -> str:
    theme = (os.getenv("SENSOS_UI_THEME", "default") or "default").strip().lower()
    if theme == "vscode-dark":
        tokens = _vscode_dark_theme_tokens()
        palette = {
            "accent": tokens["accent"],
            "occupancy": tokens["occupancy"],
            "weighted": tokens["weighted"],
            "alt": tokens["alt"],
        }
        return palette.get(name, palette["accent"])
    palette = {
        "accent": "#0c6d62",
        "occupancy": "#2563eb",
        "weighted": "#b45309",
        "alt": "#1d8b78",
    }
    return palette.get(name, palette["accent"])


def current_version() -> str:
    base = f"{VERSION_MAJOR}.{VERSION_MINOR}.{VERSION_PATCH}"
    return f"{base}-{VERSION_SUFFIX}" if VERSION_SUFFIX else base


def get_db(retries: int = 10, delay: int = 3):
    import time

    for attempt in range(retries):
        try:
            return psycopg.connect(
                host=POSTGRES_HOST,
                dbname=POSTGRES_DB,
                user=POSTGRES_USER,
                password=POSTGRES_PASSWORD,
                autocommit=True,
            )
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
      const twoDigits = (num) => String(num).padStart(2, "0");
      for (const node of scope.querySelectorAll("[data-utc]")) {
        const utcValue = node.getAttribute("data-utc");
        const mode = node.getAttribute("data-time-style") || "full";
        if (mode === "tick") {
          const parsed = new Date(utcValue || "");
          if (Number.isNaN(parsed.getTime())) continue;
          const timeText = formatLocalTimestamp(utcValue, { hour: "2-digit", minute: "2-digit" });
          const dateText = `${twoDigits(parsed.getMonth() + 1)}/${twoDigits(parsed.getDate())}/${parsed.getFullYear()}`;
          if ((node.namespaceURI || "").includes("svg")) {
            const x = node.getAttribute("x") || "0";
            while (node.firstChild) node.removeChild(node.firstChild);
            const ns = "http://www.w3.org/2000/svg";
            const topLine = document.createElementNS(ns, "tspan");
            topLine.setAttribute("x", x);
            topLine.setAttribute("dy", "0");
            topLine.textContent = timeText;
            const bottomLine = document.createElementNS(ns, "tspan");
            bottomLine.setAttribute("x", x);
            bottomLine.setAttribute("dy", "12");
            bottomLine.textContent = dateText;
            node.appendChild(topLine);
            node.appendChild(bottomLine);
          } else {
            node.textContent = `${timeText} ${dateText}`;
          }
        } else {
          const options = { year: "numeric", month: "short", day: "2-digit", hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short" };
          node.textContent = formatLocalTimestamp(utcValue, options);
        }
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


def normalize_birdnet_ranking_variable(value: str | None) -> str:
    candidate = (value or "detection").strip().lower()
    if candidate == "frequency":
        candidate = "detection"
    return candidate if candidate in BIRDNET_RANKING_VARIABLES else "detection"


def normalize_birdnet_ranking_statistic(value: str | None) -> str:
    candidate = (value or "sum").strip().lower()
    return candidate if candidate in BIRDNET_RANKING_STATISTICS else "sum"


def normalize_birdnet_ranking_weight(value: str | None) -> str:
    candidate = (value or "yes").strip().lower()
    return candidate if candidate in BIRDNET_RANKING_WEIGHTING else "yes"


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


def _format_sensor_label(sensor_type: str | None, reading_key: str | None) -> str:
    sensor = (sensor_type or "").strip()
    key = (reading_key or "").strip().replace("_", " ")
    key = " ".join(part for part in key.split() if part)
    if not key:
        return sensor or "Sensor"
    if key.lower() == "co2 ppm":
        key = "CO2 ppm"
    elif key.lower() == "humidity pct":
        key = "Humidity %"
    elif key.lower() == "temperature c":
        key = "Temperature C"
    elif key.lower() == "pressure hpa":
        key = "Pressure hPa"
    else:
        key = key.title()
    return f"{sensor} · {key}" if sensor else key


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
        "top": 10,
        "bottom": height - 34,
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
                f'<text x="{x:.2f}" y="{bottom + 14}" text-anchor="middle" font-size="11" fill="rgba(23,32,29,0.62)" data-utc="{safe_utc}" data-time-style="tick">{fallback}</text>'
            )
        else:
            parts.append(
                f'<text x="{x:.2f}" y="{bottom + 14}" text-anchor="middle" font-size="11" fill="rgba(23,32,29,0.62)">{escape_html(label)}</text>'
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
    time_key = None
    if points and ("recorded_at" in points[0] or "processed_at" in points[0]):
        time_key = "recorded_at" if "recorded_at" in points[0] else "processed_at"
    timestamps: list[datetime] = []
    if time_key is not None:
        try:
            timestamps = [
                datetime.fromisoformat(str(point[time_key]).replace("Z", "+00:00"))
                for point in points
            ]
        except Exception:
            timestamps = []
    min_ts = min(timestamps) if timestamps else None
    max_ts = max(timestamps) if timestamps else None
    total_seconds = (
        max((max_ts - min_ts).total_seconds(), 1.0)
        if min_ts is not None and max_ts is not None
        else None
    )
    step_x = (right - left) / max(1, len(points) - 1)
    coords = []
    for index, point in enumerate(points):
        value = float(point[value_key])
        if timestamps and min_ts is not None and total_seconds is not None:
            x = left + (
                ((timestamps[index] - min_ts).total_seconds() / total_seconds)
                * (right - left)
            )
        else:
            x = left + index * step_x
        y = bottom - ((value - min_value) / span) * (bottom - top)
        coords.append((x, y))
    x_labels = []
    if time_key is not None:
        tick_indexes = sorted({0, max(0, len(points) // 2), len(points) - 1})
        x_labels = [
            {"x": coords[index][0], "utc": points[index][time_key]}
            for index in tick_indexes
        ]
    segment_bounds: list[tuple[int, int]] = [(0, len(coords))]

    line_paths = []
    area_paths = []
    for start_idx, end_idx in segment_bounds:
        segment_coords = coords[start_idx:end_idx]
        if len(segment_coords) < 2:
            continue
        line_paths.append(
            " ".join(
                ["M {:.2f} {:.2f}".format(segment_coords[0][0], segment_coords[0][1])]
                + ["L {:.2f} {:.2f}".format(x, y) for x, y in segment_coords[1:]]
            )
        )
        area_paths.append(
            " ".join(
                ["M {:.2f} {:.2f}".format(segment_coords[0][0], bottom)]
                + ["L {:.2f} {:.2f}".format(x, y) for x, y in segment_coords]
                + ["L {:.2f} {:.2f} Z".format(segment_coords[-1][0], bottom)]
            )
        )
    svg_body = (
        _render_axes(bounds, min_value, max_value, x_labels)
        + "".join(
            f'<path d="{area}" fill="{stroke}" opacity="0.12"></path>'
            for area in area_paths
        )
        + "".join(
            f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>'
            for path in line_paths
        )
    )
    return f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">{svg_body}</svg>'


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
    timestamps: list[datetime] = []
    if points and "processed_at" in points[0]:
        try:
            timestamps = [
                datetime.fromisoformat(
                    str(point["processed_at"]).replace("Z", "+00:00")
                )
                for point in points
            ]
        except Exception:
            timestamps = []
    min_ts = min(timestamps) if timestamps else None
    max_ts = max(timestamps) if timestamps else None
    total_seconds = (
        max((max_ts - min_ts).total_seconds(), 1.0)
        if min_ts is not None and max_ts is not None
        else None
    )
    step_x = (right - left) / max(1, len(points))
    bar_width = max(6, step_x - 4)
    rects = []
    x_centers: list[float] = []
    for index, point in enumerate(points):
        value = float(point[value_key])
        bar_height = (value / max_value) * (bottom - top)
        if timestamps and min_ts is not None and total_seconds is not None:
            center_x = left + (
                ((timestamps[index] - min_ts).total_seconds() / total_seconds)
                * (right - left)
            )
            x_centers.append(center_x)
            x = center_x - (bar_width / 2)
        else:
            center_x = left + index * step_x + step_x / 2
            x_centers.append(center_x)
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
                "x": x_centers[index],
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
    label_font_size: int = 12,
) -> str:
    plot_rows = [row for row in rows if row.get(value_key) is not None]
    if not plot_rows:
        return ""
    values = [float(row[value_key]) for row in plot_rows]
    if not values:
        return ""
    height = max(180, 68 + len(plot_rows) * row_height)
    trimmed_labels = []
    for row in plot_rows:
        label = str(row.get("label") or row.get("top_label") or "Unknown")
        # Keep full canonical label for links/keys, but shorten displayed plot label.
        label_display = re.sub(r"\s+\([^)]*\)\s*$", "", label).strip() or label
        trimmed_labels.append(label_display)

    max_label_chars = max((len(label) for label in trimmed_labels), default=0)
    # Dynamic label gutter prevents excess whitespace after trimming BirdNET parentheticals.
    label_width = min(max(120, int(max_label_chars * (label_font_size * 0.6))), 290)
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
        label_display = trimmed_labels[index]
        label_text = escape_html(label_display)
        label_markup = f'<text x="{axis_x - 12}" y="{y + (label_font_size * 0.33):.2f}" text-anchor="end" font-size="{label_font_size}" fill="rgba(23,32,29,0.88)">{label_text}</text>'
        href = (label_href_map or {}).get(label)
        if href:
            label_markup = (
                f'<a href="{escape_html(href)}" target="_self" rel="noopener">'
                f'<text x="{axis_x - 12}" y="{y + (label_font_size * 0.33):.2f}" text-anchor="end" font-size="{label_font_size}" fill="{_plot_color("accent")}" text-decoration="underline">{label_text}</text>'
                f"</a>"
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
            cur.execute("""
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
                """)
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
                SELECT max(processed_at)
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s;
                """,
                (lookup_wg_ip,),
            )
            latest_birdnet_at = cur.fetchone()[0]
            anchored_evidence_cutoff = window_cutoff_from_latest(
                latest_birdnet_at,
                evidence_window,
            )
            if anchored_evidence_cutoff is None:
                cur.execute(
                    """
                    SELECT count(*)::integer,
                           max(processed_at)
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s;
                    """,
                    (lookup_wg_ip,),
                )
            else:
                cur.execute(
                    """
                    SELECT count(*)::integer,
                           max(processed_at)
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND processed_at >= %s;
                    """,
                    (lookup_wg_ip, anchored_evidence_cutoff),
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
                (
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
                    """
                    if anchored_evidence_cutoff is None
                    else """
                    SELECT top_label,
                           count(*)::integer AS detection_count,
                           max(top_score) AS best_score,
                           max(processed_at) AS latest_processed_at
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND processed_at >= %s
                    GROUP BY top_label
                    ORDER BY detection_count DESC, best_score DESC, top_label ASC
                    LIMIT 10;
                    """
                ),
                (
                    (lookup_wg_ip,)
                    if anchored_evidence_cutoff is None
                    else (lookup_wg_ip, anchored_evidence_cutoff)
                ),
            )
            top_birdnet_labels = cur.fetchall()
            cur.execute(
                (
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
                    """
                    if anchored_evidence_cutoff is None
                    else """
                    SELECT top_label,
                           count(*)::integer AS detection_count,
                           avg(top_score) AS average_score,
                           max(top_score) AS best_score
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND processed_at >= %s
                    GROUP BY top_label
                    ORDER BY best_score DESC, average_score DESC, detection_count DESC, top_label ASC
                    LIMIT 10;
                    """
                ),
                (
                    (lookup_wg_ip,)
                    if anchored_evidence_cutoff is None
                    else (lookup_wg_ip, anchored_evidence_cutoff)
                ),
            )
            top_birdnet_scores = cur.fetchall()
            cur.execute(
                (
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
                    """
                    if anchored_evidence_cutoff is None
                    else """
                    SELECT top_label,
                           count(*)::integer AS detection_count,
                           avg(top_likely_score) AS average_occupancy_score,
                           max(top_likely_score) AS best_occupancy_score
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                      AND top_likely_score IS NOT NULL
                      AND processed_at >= %s
                    GROUP BY top_label
                    ORDER BY best_occupancy_score DESC,
                             average_occupancy_score DESC,
                             detection_count DESC,
                             top_label ASC
                    LIMIT 10;
                    """
                ),
                (
                    (lookup_wg_ip,)
                    if anchored_evidence_cutoff is None
                    else (lookup_wg_ip, anchored_evidence_cutoff)
                ),
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
                    ORDER BY processed_at DESC, channel_index, max_score_start_frame
                    LIMIT 12;
                    """
                    if has_window_volume and anchored_evidence_cutoff is None
                    else (
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
                      AND processed_at >= %s
                    ORDER BY processed_at DESC, channel_index, max_score_start_frame
                    LIMIT 12;
                    """
                        if has_window_volume
                        else (
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
                           NULL::double precision AS volume,
                           top_label,
                           top_score,
                           top_likely_score
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                    ORDER BY processed_at DESC, channel_index, max_score_start_frame
                    LIMIT 12;
                    """
                            if anchored_evidence_cutoff is None
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
                      AND processed_at >= %s
                    ORDER BY processed_at DESC, channel_index, max_score_start_frame
                    LIMIT 12;
                    """
                        )
                    )
                ),
                (
                    (lookup_wg_ip,)
                    if anchored_evidence_cutoff is None
                    else (lookup_wg_ip, anchored_evidence_cutoff)
                ),
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
                evidence_window,
            )
            cur.execute(
                (
                    """
                    SELECT recorded_at,
                           sensor_type,
                           reading_key,
                           reading_value
                    FROM sensos.public_site_i2c_recent
                    WHERE wg_ip = %s
                    ORDER BY recorded_at DESC
                    LIMIT 12000;
                    """
                    if sensor_cutoff is None
                    else """
                    SELECT recorded_at,
                           sensor_type,
                           reading_key,
                           reading_value
                    FROM sensos.public_site_i2c_recent
                    WHERE wg_ip = %s
                      AND recorded_at >= %s
                    ORDER BY recorded_at DESC
                    LIMIT 12000;
                    """
                ),
                (
                    (lookup_wg_ip,)
                    if sensor_cutoff is None
                    else (lookup_wg_ip, sensor_cutoff)
                ),
            )
            sensor_focus_rows = cur.fetchall()

    sensor_series_map: dict[str, list[dict]] = {}
    for recorded_at, sensor_type, reading_key, reading_value in reversed(
        sensor_focus_rows
    ):
        label = _format_sensor_label(sensor_type, reading_key)
        sensor_series_map.setdefault(label, []).append(
            {
                "recorded_at": format_rfc3339_utc(recorded_at),
                "value": float(reading_value),
            }
        )

    sensor_plot_series = sorted(
        (
            {
                "label": label,
                "points": downsample_points(points, 140),
            }
            for label, points in sensor_series_map.items()
            if points
        ),
        key=lambda item: len(item["points"]),
        reverse=True,
    )

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
                "volume": (float(detection[9]) if detection[9] is not None else None),
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
        "sensor_plot_series": sensor_plot_series,
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
        "synoptic_url": f"/sites/{row[0]}/synoptic",
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
    infrastructure_rows = [
        ("Site label", site["site_label"], False),
        ("Hostname", site.get("hostname") or "unknown", False),
        ("WireGuard IP", site["wg_ip"], False),
        ("Network", site["network_name"], False),
        ("Client version", site.get("client_version") or "unknown", False),
        ("Client active", "yes" if site.get("is_active") else "no", False),
        ("Status message", site.get("status_message") or "none", False),
        ("Coordinates", f"{site['latitude']:.6f}, {site['longitude']:.6f}", False),
        ("Registered at", render_local_time(site.get("registered_at"), "unknown"), True),
        (
            "Location updated",
            render_local_time(site.get("location_recorded_at"), "unknown"),
            True,
        ),
        ("Last check-in", render_local_time(site.get("last_check_in"), "unknown"), True),
    ]
    infra_cards = "".join(f"""
        <article class="row-card">
          <div class="dim">{escape_html(label)}</div>
          <div><strong>{value if is_html else escape_html(value)}</strong></div>
        </article>
        """ for label, value, is_html in infrastructure_rows)
    top_species_cards = (
        "".join(f"""
            <div class="summary-row">
              <div>
                <div class="summary-label">{escape_html(summary["label"])}</div>
                <div class="summary-bar" style="width:{max((float(summary.get("evidence_weight") or 0.0) / max(float(site["top_birdnet_evidence"][0].get("evidence_weight") or 0.0), 1e-9)) * 100.0, 3.0):.1f}%"></div>
              </div>
              <div class="summary-value">{escape_html(f"{float(summary.get('evidence_weight') or 0.0):.2f}")}</div>
            </div>
            """ for summary in site["top_birdnet_evidence"])
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
      align-items: center;
      gap: 1rem;
      margin-bottom: 0.3rem;
    }}
    .nav-row {{
      display: inline-flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.65rem;
      margin-bottom: 0;
    }}
    .nav-link {{
      color: var(--muted);
      text-decoration: none;
      font-size: 0.96rem;
    }}
    .nav-link-inline {{
      color: #0c6d62;
      text-decoration: underline;
      text-underline-offset: 0.16em;
      text-decoration-thickness: 0.08em;
      font-size: 0.96rem;
    }}
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
    {_theme_override_css()}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div class="nav-row">
        <a class="nav-link-inline" href="{escape_html(site['public_url'])}">Overview</a>
        <a class="nav-link-inline" href="{escape_html(site['synoptic_url'])}">Time series</a>
        <a class="nav-link-inline" href="{escape_html(site['birdnet_rankings_url'])}">BirdNET rankings</a>
        <span class="nav-link">Status</span>
      </div>
      <div class="meta">
        <a href="/">Back to all field sites</a>
      </div>
    </div>
    <div class="grid">
      <section class="panel"><div class="metric-label">Client Status</div><div class="metric-value">{escape_html(site['status_message'] or ('Active' if site['is_active'] else 'Inactive'))}</div></section>
      <section class="panel"><div class="metric-label">Hostname</div><div class="metric-value">{escape_html(site['hostname'] or 'unknown')}</div></section>
      <section class="panel"><div class="metric-label">Coordinates</div><div class="metric-value">{site['latitude']:.4f}, {site['longitude']:.4f}</div><div class="dim mono">{escape_html(site['wg_ip'])}</div></section>
      <section class="panel"><div class="metric-label">Last Check-In</div><div class="metric-value">{render_local_time(site['last_check_in'], 'No check-in yet')}</div></section>
    </div>
    <div class="layout">
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

    site["synoptic_url"] = f"/sites/{site['peer_uuid']}/synoptic"
    site["birdnet_rankings_url"] = f"/sites/{site['peer_uuid']}/birdnet-rankings"
    site["synoptic_range"] = normalized_range
    site["sensor_series"] = sensor_series
    return site


def fetch_site_birdnet_rankings(
    site_id: str,
    variable_key: str | None = None,
    statistic_key: str | None = None,
    weight_key: str | None = None,
    range_key: str | None = None,
) -> dict:
    site = fetch_site_detail(site_id)
    normalized_variable = normalize_birdnet_ranking_variable(variable_key)
    normalized_statistic = normalize_birdnet_ranking_statistic(statistic_key)
    normalized_weight = normalize_birdnet_ranking_weight(weight_key)
    if normalized_variable in {"occup", "volume"}:
        normalized_weight = "no"
    normalized_range = normalize_birdnet_ranking_range(range_key)
    range_cutoff = BIRDNET_RANKING_RANGES[normalized_range]

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

            base_variable_expr_map = {
                "detection": "1::double precision",
                "duration": "greatest(end_sec - start_sec, 0)::double precision",
                "score": "top_score::double precision",
                "occup": "coalesce(top_likely_score, top_score)::double precision",
                "volume": (
                    "volume::double precision"
                    if has_window_volume
                    else "NULL::double precision"
                ),
            }
            variable_expr = base_variable_expr_map[normalized_variable]
            if normalized_weight == "yes" and normalized_variable != "occup":
                variable_expr = (
                    f"({variable_expr}) * coalesce(top_likely_score, top_score)::double precision"
                )

            statistic_expr_map = {
                "sum": f"sum({variable_expr})",
                "mean": f"avg({variable_expr})",
                "min": f"min({variable_expr})",
                "max": f"max({variable_expr})",
                "median": f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {variable_expr})",
            }
            selected_metric_expr = statistic_expr_map[normalized_statistic]
            selected_metric_label = (
                f"{BIRDNET_RANKING_STATISTICS[normalized_statistic]['label']} "
                f"{BIRDNET_RANKING_VARIABLES[normalized_variable]['label']}"
                f"{' × occup' if normalized_weight == 'yes' and normalized_variable != 'occup' else ''}"
            )
            selected_metric_description = (
                f"{BIRDNET_RANKING_STATISTICS[normalized_statistic]['label']} of "
                f"{BIRDNET_RANKING_VARIABLES[normalized_variable]['label'].lower()}"
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
                           {selected_metric_expr} AS selected_metric,
                           max(processed_at) AS latest_processed_at
                    FROM sensos.public_site_birdnet_detections
                    WHERE wg_ip = %s
                    GROUP BY top_label
                    ORDER BY selected_metric DESC NULLS LAST, detection_count DESC, top_label ASC
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
                               {selected_metric_expr} AS selected_metric,
                               max(processed_at) AS latest_processed_at
                        FROM sensos.public_site_birdnet_detections
                        WHERE wg_ip = %s
                        GROUP BY top_label
                        ORDER BY selected_metric DESC NULLS LAST, detection_count DESC, top_label ASC
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
                               {selected_metric_expr} AS selected_metric,
                               max(processed_at) AS latest_processed_at
                        FROM sensos.public_site_birdnet_detections
                        WHERE wg_ip = %s
                          AND processed_at >= %s
                        GROUP BY top_label
                        ORDER BY selected_metric DESC NULLS LAST, detection_count DESC, top_label ASC
                        """,
                        (site["wg_ip"], anchored_cutoff),
                    )
            ranking_rows = cur.fetchall()

    site["birdnet_rankings_url"] = f"/sites/{site['peer_uuid']}/birdnet-rankings"
    site["synoptic_url"] = f"/sites/{site['peer_uuid']}/synoptic"
    site["birdnet_ranking_variable"] = normalized_variable
    site["birdnet_ranking_statistic"] = normalized_statistic
    site["birdnet_ranking_weight"] = normalized_weight
    site["birdnet_ranking_range"] = normalized_range
    site["birdnet_ranking_metric_label"] = selected_metric_label
    site["birdnet_ranking_description"] = selected_metric_description
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
            "selected_metric": float(row[9]) if row[9] is not None else None,
            "latest_processed_at": format_rfc3339_utc(row[10]),
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
                WHERE wg_ip = %s;
                """,
                (site["wg_ip"],),
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
    volume_dbfs_points = []
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
        occupancy_value = (
            float(top_likely_score) if top_likely_score is not None else score_value
        )
        duration_sec = max(float(end_sec) - float(start_sec), 0.0)
        weighted_value = duration_sec * score_value * occupancy_value
        score_points.append({"processed_at": processed_text, "value": score_value})
        if volume is not None:
            volume_value = float(volume)
            volume_dbfs = 20.0 * math.log10(max(volume_value, 1e-6))
            volume_dbfs_points.append(
                {"processed_at": processed_text, "value": volume_dbfs}
            )
        occupancy_points.append(
            {"processed_at": processed_text, "value": occupancy_value}
        )
        weighted_points.append(
            {"processed_at": processed_text, "activity": weighted_value}
        )
        detections.append(
            {
                "processed_at": processed_text,
                "top_score": score_value,
                "top_likely_score": (
                    float(top_likely_score) if top_likely_score is not None else None
                ),
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
    site["birdnet_species_url"] = birdnet_species_url(
        site["peer_uuid"], label, normalized_range
    )
    site["birdnet_rankings_url"] = f"/sites/{site['peer_uuid']}/birdnet-rankings"
    site["synoptic_url"] = f"/sites/{site['peer_uuid']}/synoptic"
    site["species_score_series"] = downsample_points(score_points, 180)
    site["species_volume_dbfs_series"] = downsample_points(volume_dbfs_points, 180)
    site["species_occupancy_series"] = downsample_points(occupancy_points, 180)
    site["species_weighted_series"] = downsample_points(weighted_points, 180)
    site["species_detection_count"] = len(detections)
    site["species_latest_at"] = detections[-1]["processed_at"] if detections else None
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
        render_event_timeline_svg(
            site["species_score_series"], "value", _plot_color("accent")
        )
        if site["species_score_series"]
        else ""
    )
    volume_dbfs_chart = (
        render_event_timeline_svg(
            site["species_volume_dbfs_series"], "value", _plot_color("weighted")
        )
        if site["species_volume_dbfs_series"]
        else ""
    )
    recent_cards = (
        "".join(f"""
        <article class="record-card">
          <div><strong>{escape_html(site['species_label'])}</strong> <span class="dim">score {item['top_score']:.2f} · occup {'n/a' if item['top_likely_score'] is None else f"{item['top_likely_score']:.2f}"} · vol {'n/a' if item.get('volume') is None else f"{item['volume']:.3f}"}</span></div>
          <div class="dim">{render_local_time(item['processed_at'])} · ch {item['channel_index']} · {item['start_sec']:.1f}s-{item['end_sec']:.1f}s</div>
          <div class="mono">{escape_html(item['source_path'])}</div>
        </article>
        """ for item in site["species_recent_detections"])
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
    .shell {{ max-width: 1320px; margin: 0 auto; padding: 0.7rem 1rem 1rem; }}
    .masthead {{ display:flex; justify-content:space-between; gap:1rem; align-items:center; margin-bottom:0.3rem; }}
    .nav-row {{
      display: inline-flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.65rem;
      margin-bottom: 0;
    }}
    .nav-link {{
      color: var(--muted);
      text-decoration: none;
      font-size: 0.96rem;
    }}
    .nav-link strong {{ color: var(--ink); }}
    .nav-link-inline {{
      color: #0c6d62;
      text-decoration: underline;
      text-underline-offset: 0.16em;
      text-decoration-thickness: 0.08em;
      font-size: 0.96rem;
    }}
    .meta {{ color: var(--muted); font-size: 0.95rem; text-align: right; }}
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
    {_theme_override_css()}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div class="nav-row">
        <a class="nav-link-inline" href="{escape_html(site['public_url'])}">Overview</a>
        <a class="nav-link-inline" href="{escape_html(site['synoptic_url'])}">Time series</a>
        <a class="nav-link-inline" href="{escape_html(site['birdnet_rankings_url'])}">BirdNET rankings</a>
        <a class="nav-link-inline" href="{escape_html(site['status_url'])}">Status</a>
      </div>
      <div class="meta">
        <a href="/">Back to all field sites</a>
      </div>
    </div>
    <div class="stack">
      <section class="panel">
        <div class="range-pills">{range_links}</div>
      </section>
      <section class="panel">
        <h2 class="section-title">Detection Score Timeline</h2>
        <div class="chart-wrap">{score_chart or '<div class="empty">No score timeline available.</div>'}</div>
      </section>
      <section class="panel">
        <h2 class="section-title">Volume (dBFS)</h2>
        <div class="chart-wrap">{volume_dbfs_chart or '<div class="empty">No volume series available.</div>'}</div>
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
        _plot_color("accent"),
        None,
        width=1040,
        row_height=26,
    )
    sensor_plot_series = site.get("sensor_plot_series") or []
    synoptic_url = f"/sites/{site['peer_uuid']}/synoptic"
    synoptic_range_url = f"{synoptic_url}?range={evidence_range}"
    sensor_cards_html = "".join(f"""
            <article class="sensor-focus-card">
              <h3 class="sensor-focus-title">{escape_html(series['label'])}</h3>
              <a class="sensor-focus-link" href="{escape_html(synoptic_range_url)}">
                <div class="sensor-focus-chart">{render_line_chart_svg(series['points'], 'value', _plot_color("accent"), width=560, height=186) or '<div class="empty">No data in this range.</div>'}</div>
              </a>
            </article>
        """ for series in sensor_plot_series)

    birdnet_rankings_url = f"/sites/{site['peer_uuid']}/birdnet-rankings"
    birdnet_rankings_range_url = f"{birdnet_rankings_url}?range={evidence_range}"

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
    .shell {{ max-width: 1320px; margin: 0 auto; padding: 0.7rem 1rem 1rem; }}
    .masthead {{
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      align-items: center;
      margin-bottom: 0.3rem;
    }}
    .nav-row {{
      display: inline-flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.65rem;
      margin-bottom: 0;
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
    .meta {{
      color: var(--muted);
      font-size: 0.95rem;
      text-align: right;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 1rem;
    }}
    .stack {{ display: grid; gap: 0.6rem; align-content: start; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 22px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
      padding: 0.65rem 0.7rem;
    }}
    .summary-strip {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 0.7rem;
    }}
    .evidence-chart-link {{
      display: block;
      text-decoration: none;
      color: inherit;
    }}
    .evidence-chart-wrap {{
      min-height: 10.5rem;
      border: 1px solid rgba(23,32,29,0.08);
      border-radius: 14px;
      overflow-x: auto;
      background: rgba(255,255,255,0.55);
    }}
    .evidence-chart-wrap svg {{
      width: 100%;
      min-width: 800px;
      display: block;
    }}
    .sensor-focus-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 0.6rem;
    }}
    .sensor-focus-card {{
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 0.5rem 0.55rem;
      background: rgba(255,255,255,0.62);
      min-width: 0;
    }}
    .sensor-focus-link {{
      display: block;
      text-decoration: none;
      color: inherit;
    }}
    .sensor-focus-title {{
      margin: 0 0 0.3rem;
      color: var(--muted);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .sensor-focus-chart {{
      height: 10.8rem;
      border: 1px solid rgba(23,32,29,0.08);
      border-radius: 12px;
      overflow: hidden;
      background: rgba(255,255,255,0.5);
    }}
    .sensor-focus-chart svg {{
      width: 100%;
      height: 100%;
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
      .summary-strip, .detail-grid, .sensor-focus-grid {{ grid-template-columns: 1fr; }}
      .meta {{ text-align: left; }}
    }}
    {_theme_override_css()}
  </style>
</head>
  <body>
  <div class="shell">
    <div class="masthead">
      <div class="nav-row">
        <span class="nav-link">Overview</span>
        <a class="nav-link-inline" href="{synoptic_url}">Time series</a>
        <a class="nav-link-inline" href="{birdnet_rankings_url}">BirdNET rankings</a>
        <a class="nav-link-inline" href="{escape_html(site['status_url'])}">Status</a>
      </div>
      <div class="meta">
        <a href="/">Back to all field sites</a>
      </div>
    </div>
    <div class="layout">
      <main class="stack">
        <section class="panel">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:0.8rem;flex-wrap:wrap;margin-bottom:0.8rem;">
            <div>
              <h2 class="section-title" style="margin-bottom:0.2rem;">Top Species</h2>
            </div>
            <div class="range-pills">{evidence_range_links}</div>
          </div>
          <a class="evidence-chart-link" href="{escape_html(birdnet_rankings_range_url)}">
            <div class="evidence-chart-wrap">{evidence_chart or '<div class="empty">No BirdNET detections are visible yet for this site.</div>'}</div>
          </a>
          {"<div class='sensor-focus-grid' style='margin-top:0.6rem;'>" + sensor_cards_html + "</div>" if sensor_cards_html else ""}
        </section>
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
    species_href = lambda label: birdnet_species_url(
        site["peer_uuid"], str(label), range_key
    )
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
        "".join(f"""
        <section class="chart-card">
          <div class="chart-head">
            <div>
              <strong>{escape_html(series['label'])}</strong>
              <div class="dim">latest {series['latest_value']:.3f} at {render_local_time(series['latest_at'])}</div>
            </div>
          </div>
          <div class="chart">{render_line_chart_svg(series['points'], 'value', _plot_color("accent"))}</div>
        </section>
        """ for series in site["sensor_series"])
        or '<div class="empty">No sensor time series are visible yet for this site.</div>'
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
    .shell {{ max-width: 1320px; margin: 0 auto; padding: 0.7rem 1rem 1rem; }}
    .masthead {{
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      align-items: center;
      margin-bottom: 0.3rem;
    }}
    .nav-row {{
      display: inline-flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.65rem;
      margin-bottom: 0;
    }}
    .nav-link {{
      color: var(--muted);
      text-decoration: none;
      font-size: 0.96rem;
    }}
    .nav-link strong {{ color: var(--ink); }}
    .nav-link-inline {{
      color: #0c6d62;
      text-decoration: underline;
      text-underline-offset: 0.16em;
      text-decoration-thickness: 0.08em;
      font-size: 0.96rem;
    }}
    .meta {{ color: var(--muted); font-size: 0.95rem; text-align: right; }}
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
    {_theme_override_css()}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div class="nav-row">
        <a class="nav-link-inline" href="{escape_html(site['public_url'])}">Overview</a>
        <span class="nav-link">Time series</span>
        <a class="nav-link-inline" href="{escape_html(site['birdnet_rankings_url'])}">BirdNET rankings</a>
        <a class="nav-link-inline" href="{escape_html(site['status_url'])}">Status</a>
      </div>
      <div class="meta">
        <a href="/">Back to all field sites</a>
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
    </div>
  </div>
{render_local_time_script()}
</body>
</html>"""


def render_birdnet_rankings_html(site: dict) -> str:
    selected_variable = normalize_birdnet_ranking_variable(
        site.get("birdnet_ranking_variable")
    )
    selected_statistic = normalize_birdnet_ranking_statistic(
        site.get("birdnet_ranking_statistic")
    )
    selected_weight = normalize_birdnet_ranking_weight(site.get("birdnet_ranking_weight"))
    selected_range = normalize_birdnet_ranking_range(site.get("birdnet_ranking_range"))
    selected_metric = "selected_metric"
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
        f'<div class="plot-shell">{render_horizontal_lollipop_svg(site["birdnet_rankings"], selected_metric, _plot_color("accent"), species_href_map, row_height=32, label_font_size=14)}</div>'
        if plotted_species_count
        else '<div class="empty">No values are available for the selected metric in this time window.</div>'
    )

    ranking_cards = (
        "".join(f"""
        <article class="rank-card">
          <div class="rank-main">
            <strong><a href="{escape_html(species_href_map[str(item['label'])])}">{escape_html(item['label'])}</a></strong>
            <span class="metric-pill">{escape_html(site['birdnet_ranking_metric_label'])}: {'n/a' if item.get(selected_metric) is None else _format_axis_value(float(item[selected_metric]))}</span>
          </div>
          <div class="dim">Detections {item['detection_count']} · max score {'n/a' if item['max_score'] is None else _format_axis_value(item['max_score'])} · max occup {'n/a' if item['max_occup'] is None else _format_axis_value(item['max_occup'])}</div>
          <div class="dim">duration-weighted score x occup {'n/a' if item['sum_score_x_occup'] is None else _format_axis_value(item['sum_score_x_occup'])} · avg volume {'n/a' if item['avg_volume'] is None else _format_axis_value(item['avg_volume'])}</div>
          <div class="dim">latest {render_local_time(item['latest_processed_at'])}</div>
        </article>
        """ for item in site["birdnet_rankings"])
        or '<div class="empty">No ranked BirdNET species are visible for the selected time window.</div>'
    )

    variable_options = "".join(
        f'<option value="{key}"{" selected" if key == selected_variable else ""}>{escape_html(config["label"])}</option>'
        for key, config in BIRDNET_RANKING_VARIABLES.items()
    )
    statistic_options = "".join(
        f'<option value="{key}"{" selected" if key == selected_statistic else ""}>{escape_html(config["label"])}</option>'
        for key, config in BIRDNET_RANKING_STATISTICS.items()
    )
    weight_options = "".join(
        f'<option value="{key}"{" selected" if key == selected_weight else ""}>{escape_html(config["label"])}</option>'
        for key, config in BIRDNET_RANKING_WEIGHTING.items()
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
    .shell {{ max-width: 1480px; margin: 0 auto; padding: 0.95rem 1.1rem 1.2rem; }}
    .masthead {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      margin-bottom: 0.3rem;
    }}
    .nav-row {{
      display: inline-flex;
      gap: 0.65rem;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 0;
    }}
    .nav-link {{
      color: var(--muted);
      text-decoration: none;
      font-size: 0.96rem;
    }}
    .nav-link strong {{ color: var(--ink); }}
    .nav-link-inline {{
      color: var(--accent);
      text-decoration: none;
      text-decoration: underline;
      text-underline-offset: 0.16em;
      text-decoration-thickness: 0.08em;
      font-size: 0.96rem;
    }}
    .meta {{ color: var(--muted); font-size: 0.95rem; text-align: right; }}
    .stack {{ display: grid; gap: 1.2rem; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      padding: 1.15rem;
    }}
    .panel.rankings-panel {{
      padding-top: 0.8rem;
      padding-bottom: 0.95rem;
    }}
    .controls {{
      display: grid;
      grid-template-columns: minmax(160px, 1.35fr) minmax(120px, 0.9fr) minmax(170px, 1fr) minmax(120px, 0.8fr);
      gap: 0.65rem;
      align-items: center;
    }}
    select {{
      width: 100%;
      padding: 0.36rem 0.82rem;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.88);
      color: var(--ink);
      font: inherit;
      font-size: 0.82rem;
      line-height: 1.2;
    }}
    .section-title {{
      margin: 0;
      font-size: 0.88rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .section-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.45rem;
      margin-bottom: 0.55rem;
      flex-wrap: wrap;
    }}
    .plot-shell {{
      min-height: 19rem;
      border-radius: 18px;
      border: 1px solid rgba(23,32,29,0.08);
      background: linear-gradient(180deg, rgba(247,244,237,0.92), rgba(237,241,234,0.72));
      overflow-x: auto;
      overflow-y: hidden;
    }}
    .plot-shell svg {{ width: 100%; min-width: 1120px; display: block; }}
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
    {_theme_override_css()}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div class="nav-row">
        <a class="nav-link-inline" href="{escape_html(site['public_url'])}">Overview</a>
        <a class="nav-link-inline" href="{escape_html(site['synoptic_url'])}">Time series</a>
        <span class="nav-link">BirdNET rankings</span>
        <a class="nav-link-inline" href="{escape_html(site['status_url'])}">Status</a>
      </div>
      <div class="meta">
        <a href="/">Back to all field sites</a>
      </div>
    </div>
    <div class="stack">
      <section class="panel rankings-panel">
        <div class="section-bar">
          <h2 class="section-title">Rankings</h2>
          <form method="get" action="{escape_html(site['birdnet_rankings_url'])}" class="controls" id="birdnetRankingControls">
            <select name="variable" onchange="handleBirdnetVariableChange()" aria-label="Variable">{variable_options}</select>
            <select name="stat" onchange="submitBirdnetRankingControls()" aria-label="Statistic">{statistic_options}</select>
            <select name="weight" onchange="submitBirdnetRankingControls()" aria-label="Weighted">{weight_options}</select>
            <select name="range" onchange="submitBirdnetRankingControls()" aria-label="Time window">{range_options}</select>
          </form>
        </div>
        {plot_markup}
      </section>
      <section class="panel">
        <h2 class="section-title">Ranked Species</h2>
        <div class="rank-list">{ranking_cards}</div>
      </section>
    </div>
  </div>
  <script>
    function handleBirdnetVariableChange() {{
      const form = document.getElementById("birdnetRankingControls");
      if (!form) return;
      const variableSelect = form.elements.namedItem("variable");
      const weightSelect = form.elements.namedItem("weight");
      const variableValue = variableSelect ? String(variableSelect.value || "") : "";
      if (weightSelect && (variableValue === "occup" || variableValue === "volume")) {{
        weightSelect.value = "no";
      }}
      form.requestSubmit();
    }}

    function submitBirdnetRankingControls() {{
      const form = document.getElementById("birdnetRankingControls");
      if (!form) return;
      form.requestSubmit();
    }}
  </script>
{render_local_time_script()}
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
        logger.exception("public-ui startup health probe failed")
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
                request.query_params.get("variable"),
                request.query_params.get("stat"),
                request.query_params.get("weight"),
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


def render_index_html() -> str:
    """Render the public field-site map as a minimal Leaflet view.

    The root page is intentionally map-first:
    - click an ambiguous point cluster to zoom in
    - click an isolated point to open a compact details popup
    - use the popup links to open the dashboard, status, time series, or BirdNET page

    The critical Leaflet CSS is kept inline so the map still lays out correctly if
    CDN CSS fails to load or if application CSS would otherwise alter image sizing.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SensOS Field Sites</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root {{
      --bg: #d8e4df;
      --panel: rgba(255,255,255,0.86);
      --ink: #17201d;
      --muted: #52615b;
      --accent: #0c6d62;
      --accent-2: #d97706;
      --border: rgba(23,32,29,0.18);
      --shadow: 0 12px 32px rgba(23,32,29,0.18);
      --marker: #0c6d62;
      --marker-active: #d97706;
    }}

    * {{ box-sizing: border-box; }}

    html, body {{
      width: 100%;
      height: 100%;
      margin: 0;
    }}

    body {{
      overflow: hidden;
      color: var(--ink);
      background: var(--bg);
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
    }}

    #map {{
      position: fixed;
      inset: 0;
      width: 100vw;
      height: 100vh;
      background: var(--bg);
      z-index: 0;
    }}

    .leaflet-container {{
      position: relative;
      overflow: hidden;
      outline: 0;
      touch-action: none;
      background: var(--bg);
      font: inherit;
    }}

    .leaflet-pane,
    .leaflet-tile,
    .leaflet-marker-icon,
    .leaflet-marker-shadow,
    .leaflet-tile-container,
    .leaflet-pane > svg,
    .leaflet-pane > canvas,
    .leaflet-zoom-box,
    .leaflet-image-layer,
    .leaflet-layer {{
      position: absolute;
      left: 0;
      top: 0;
    }}

    .leaflet-container img.leaflet-tile,
    .leaflet-container .leaflet-tile {{
      max-width: none !important;
      max-height: none !important;
      min-width: 0 !important;
      min-height: 0 !important;
      padding: 0 !important;
      margin: 0 !important;
      border: 0 !important;
      object-fit: fill !important;
      box-sizing: content-box !important;
    }}

    .leaflet-container img.leaflet-marker-icon,
    .leaflet-container img.leaflet-marker-shadow,
    .leaflet-container .leaflet-image-layer {{
      max-width: none !important;
      max-height: none !important;
    }}

    .leaflet-tile {{
      filter: none !important;
      opacity: 1 !important;
      mix-blend-mode: normal !important;
      visibility: hidden;
      user-select: none;
      -webkit-user-drag: none;
    }}

    .basemap-tile {{
      filter: none !important;
      opacity: 1 !important;
      mix-blend-mode: normal !important;
    }}

    .leaflet-tile-loaded {{ visibility: inherit; }}
    .leaflet-zoom-animated {{ transform-origin: 0 0; }}
    .leaflet-interactive {{ cursor: pointer; }}

    .leaflet-control {{
      position: relative;
      z-index: 800;
      pointer-events: auto;
      float: left;
      clear: both;
    }}

    .leaflet-top,
    .leaflet-bottom {{
      position: absolute;
      z-index: 900;
      pointer-events: none;
    }}

    .leaflet-top {{ top: 0; }}
    .leaflet-right {{ right: 0; }}
    .leaflet-bottom {{ bottom: 0; }}
    .leaflet-left {{ left: 0; }}

    .leaflet-control-zoom {{
      margin-right: 12px;
      margin-bottom: 24px;
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }}

    .leaflet-control-zoom a {{
      display: block;
      width: 34px;
      height: 34px;
      line-height: 34px;
      text-align: center;
      text-decoration: none;
      background: rgba(255,255,255,0.88);
      color: var(--ink);
      font: bold 18px/34px system-ui, sans-serif;
      border-bottom: 1px solid var(--border);
    }}

    .leaflet-control-zoom a:last-child {{ border-bottom: 0; }}

    .leaflet-control-layers {{
      margin-top: 12px;
      margin-right: 12px;
      border: 1px solid var(--border);
      border-radius: 10px;
      box-shadow: var(--shadow);
      background: rgba(255,255,255,0.84);
      backdrop-filter: blur(10px);
    }}

    .network-filter-control {{
      margin-top: 8px;
    }}

    .leaflet-control-layers-toggle {{
      width: 34px;
      height: 34px;
      background-size: 22px 22px;
    }}

    .leaflet-control-attribution {{
      margin: 0;
      padding: 0 5px;
      color: rgba(23,32,29,0.72);
      background: rgba(255,255,255,0.66);
      font-size: 10px;
    }}

    .leaflet-popup {{
      position: absolute;
      text-align: center;
      margin-bottom: 20px;
    }}

    .leaflet-popup-content-wrapper {{
      padding: 1px;
      text-align: left;
      border-radius: 12px;
      background: white;
      box-shadow: 0 3px 14px rgba(0,0,0,0.28);
    }}

    .leaflet-popup-content {{
      margin: 13px 19px;
      line-height: 1.4;
      font-size: 13px;
      min-height: 1px;
    }}

    .leaflet-popup-tip-container {{
      width: 40px;
      height: 20px;
      position: absolute;
      left: 50%;
      margin-top: -1px;
      margin-left: -20px;
      overflow: hidden;
      pointer-events: none;
    }}

    .leaflet-popup-tip {{
      width: 17px;
      height: 17px;
      padding: 1px;
      margin: -10px auto 0;
      transform: rotate(45deg);
      background: white;
      box-shadow: 0 3px 14px rgba(0,0,0,0.28);
    }}

    .leaflet-popup-close-button {{
      position: absolute;
      top: 0;
      right: 0;
      padding: 4px 4px 0 0;
      border: none;
      text-align: center;
      width: 18px;
      height: 14px;
      font: 16px/14px Tahoma, Verdana, sans-serif;
      color: #757575;
      text-decoration: none;
      background: transparent;
    }}

    .leaflet-tooltip {{
      position: absolute;
      padding: 5px 7px;
      background: rgba(255,255,255,0.92);
      border: 1px solid rgba(0,0,0,0.16);
      border-radius: 8px;
      color: var(--ink);
      white-space: nowrap;
      user-select: none;
      pointer-events: none;
      box-shadow: 0 2px 8px rgba(0,0,0,0.18);
    }}

    .leaflet-tooltip-top {{ margin-top: -6px; }}
    .leaflet-tooltip-bottom {{ margin-top: 6px; }}
    .leaflet-tooltip-left {{ margin-left: -6px; }}
    .leaflet-tooltip-right {{ margin-left: 6px; }}

    .map-title {{
      position: fixed;
      left: 12px;
      top: 12px;
      z-index: 1000;
      display: inline-flex;
      align-items: baseline;
      gap: 0.65rem;
      max-width: calc(100vw - 94px);
      padding: 0.38rem 0.62rem;
      border: 1px solid var(--border);
      border-radius: 10px;
      color: var(--ink);
      background: rgba(255,255,255,0.78);
      box-shadow: 0 8px 24px rgba(23,32,29,0.13);
      backdrop-filter: blur(10px);
      pointer-events: none;
    }}

    .map-title strong {{
      font-size: 1rem;
      letter-spacing: -0.02em;
      white-space: nowrap;
    }}

    .map-title span {{
      color: var(--muted);
      font-size: 0.86rem;
      white-space: nowrap;
    }}


    .show-all-button {{
      position: fixed;
      left: 12px;
      bottom: 24px;
      z-index: 1000;
      border: 1px solid var(--border);
      border-radius: 10px;
      color: var(--ink);
      background: rgba(255,255,255,0.78);
      box-shadow: 0 8px 24px rgba(23,32,29,0.13);
      backdrop-filter: blur(10px);
      padding: 0.42rem 0.64rem;
      font: inherit;
      cursor: pointer;
    }}

    .show-all-button:hover {{
      background: rgba(255,255,255,0.92);
    }}

    .site-dot {{
      width: 17px;
      height: 17px;
      border-radius: 999px;
      background: var(--marker);
      border: 3px solid rgba(255,255,255,0.98);
      box-shadow:
        0 0 0 1px rgba(23,32,29,0.26),
        0 4px 12px rgba(12,109,98,0.28);
    }}

    .site-dot.inactive {{
      background: #66736e;
      opacity: 0.78;
    }}

    .site-popup {{
      min-width: 13rem;
      max-width: 18rem;
      color: var(--ink);
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
    }}

    .site-popup-title {{
      margin: 0 0 0.35rem;
      font-size: 1rem;
      line-height: 1.2;
    }}

    .site-popup-meta {{
      color: var(--muted);
      font-size: 0.88rem;
      line-height: 1.35;
      margin-bottom: 0.55rem;
    }}

    .site-popup-actions {{
      display: flex;
      gap: 0.45rem;
      flex-wrap: wrap;
    }}

    .site-popup-actions a {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      background: rgba(12,109,98,0.10);
      color: var(--accent);
      text-decoration: none;
      padding: 0.32rem 0.52rem;
      font-weight: 700;
      font-size: 0.86rem;
    }}

    .mono {{
      font-family: "SFMono-Regular", "Menlo", "Consolas", monospace;
      font-size: 0.86rem;
    }}

    a {{ color: var(--accent); }}

    @media (max-width: 640px) {{
      .map-title {{
        left: 8px;
        top: 8px;
        max-width: calc(100vw - 76px);
      }}

      .map-title strong {{ font-size: 0.94rem; }}
      .map-title span {{ font-size: 0.8rem; }}

      .leaflet-control-layers {{
        margin-top: 8px;
        margin-right: 8px;
      }}

      .network-filter-control {{
        margin-top: 6px;
      }}

      .leaflet-control-zoom {{
        margin-right: 8px;
        margin-bottom: 20px;
      }}
    }}

    {_theme_override_css()}
  </style>
</head>
<body>
  <div id="map" aria-label="Mapped field sites"></div>
  <div class="map-title">
    <strong>Field Sites</strong>
    <span id="site-count">Loading…</span>
  </div>
  <button class="show-all-button" id="show-all-button" type="button">Show all</button>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const SATELLITE_TILES =
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}";

    const SATELLITE_ATTRIBUTION =
      "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community";

    const OSM_TILES = "https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png";

    const OSM_ATTRIBUTION =
      "&copy; OpenStreetMap contributors";

    const DEFAULT_CENTER = [30.2672, -97.7431];
    const DEFAULT_ZOOM = 6;

    const siteCount = document.getElementById("site-count");
    const showAllButton = document.getElementById("show-all-button");
    let sites = [];

    function assertLeafletCssLooksLoaded() {{
      const probe = document.createElement("div");
      probe.className = "leaflet-pane";
      probe.style.visibility = "hidden";
      document.body.appendChild(probe);
      const position = window.getComputedStyle(probe).position;
      document.body.removeChild(probe);
      if (position !== "absolute") {{
        console.warn("Leaflet critical CSS is not active; map tiles may render incorrectly.");
      }}
    }}

    assertLeafletCssLooksLoaded();

    const map = L.map("map", {{
      zoomControl: false,
      preferCanvas: true,
      worldCopyJump: true,
      scrollWheelZoom: false,
      doubleClickZoom: true,
    }}).setView(DEFAULT_CENTER, DEFAULT_ZOOM);

    L.control.zoom({{ position: "bottomright" }}).addTo(map);

    const satelliteLayer = L.tileLayer(SATELLITE_TILES, {{
      attribution: SATELLITE_ATTRIBUTION,
      maxZoom: 19,
      detectRetina: false,
      className: "basemap-tile",
    }}).addTo(map);

    const osmLayer = L.tileLayer(OSM_TILES, {{
      attribution: OSM_ATTRIBUTION,
      maxZoom: 19,
      detectRetina: false,
      className: "basemap-tile",
    }});

    const siteMarkers = [];
    const networkLayerGroups = new Map();
    let networkControl = null;

    L.control.layers(
      {{
        "Satellite": satelliteLayer,
        "OpenStreetMap": osmLayer,
      }},
      null,
      {{ position: "topright", collapsed: true }}
    ).addTo(map);

    function networkLabel(site) {{
      const name = String(site?.network_name || "").trim();
      return name || "Unassigned";
    }}

    function markerVisible(marker) {{
      return map.hasLayer(marker);
    }}

    function escapeHtml(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }}

    function displaySiteName(site) {{
      const note = String(site?.note || "").trim();
      if (note) return note;
      const label = String(site?.site_label || "").trim();
      if (label) return label;
      const hostname = String(site?.hostname || "").trim();
      if (hostname) return hostname;
      return String(site?.wg_ip || "unknown");
    }}

    function formatRelativeTime(value) {{
      if (!value) return "never";
      const parsed = Date.parse(value);
      if (!Number.isFinite(parsed)) return value;
      const deltaMs = Date.now() - parsed;
      const sec = Math.max(0, Math.floor(deltaMs / 1000));
      if (sec < 60) return `${{sec}}s ago`;
      if (sec < 3600) return `${{Math.floor(sec / 60)}}m ago`;
      if (sec < 86400) return `${{Math.floor(sec / 3600)}}h ago`;
      return `${{Math.floor(sec / 86400)}}d ago`;
    }}

    function validCoordinates(site) {{
      return (
        Number.isFinite(site.latitude) &&
        Number.isFinite(site.longitude) &&
        Math.abs(site.latitude) <= 90 &&
        Math.abs(site.longitude) <= 180 &&
        !(site.latitude === 0 && site.longitude === 0)
      );
    }}

    function markerIcon(site) {{
      return L.divIcon({{
        className: "",
        html: `<div class="site-dot ${{site.is_active ? "" : "inactive"}}"></div>`,
        iconSize: [17, 17],
        iconAnchor: [8.5, 8.5],
        popupAnchor: [0, -12],
      }});
    }}

    function siteUrl(site) {{
      return site.public_url || `/sites/${{encodeURIComponent(site.site_id)}}`;
    }}

    function popupHtml(site) {{
      const publicUrl = siteUrl(site);

      return `
        <div class="site-popup">
          <h2 class="site-popup-title">${{escapeHtml(displaySiteName(site))}}</h2>
          <div class="site-popup-meta">
            <div><span class="mono">${{escapeHtml(site.wg_ip || "")}}</span></div>
            <div>${{escapeHtml(site.hostname || site.network_name || "")}}</div>
            <div>Last check-in: ${{escapeHtml(formatRelativeTime(site.last_check_in))}}</div>
            <div>BirdNET detections: ${{escapeHtml(site.birdnet_detection_count ?? 0)}}</div>
          </div>
          <div class="site-popup-actions">
            <a href="${{escapeHtml(publicUrl)}}">Open dashboard</a>
          </div>
        </div>
      `;
    }}

    function addSite(site) {{
      if (!validCoordinates(site)) return;

      const marker = L.marker([site.latitude, site.longitude], {{
        icon: markerIcon(site),
        title: displaySiteName(site),
        riseOnHover: true,
      }});

      marker.site = site;

      marker.bindTooltip(displaySiteName(site), {{
        direction: "top",
        sticky: true,
        opacity: 0.94,
      }});

      marker.bindPopup(() => popupHtml(site), {{
        maxWidth: 320,
        closeButton: true,
      }});

      marker.on("click", () => {{
        handleSiteMarkerClick(marker);
      }});

      const network = networkLabel(site);
      let networkGroup = networkLayerGroups.get(network);
      if (!networkGroup) {{
        networkGroup = L.layerGroup();
        networkLayerGroups.set(network, networkGroup);
      }}

      marker.addTo(networkGroup);
      siteMarkers.push(marker);
    }}

    function renderNetworkControl() {{
      if (networkControl) {{
        networkControl.remove();
      }}

      const overlays = {{}};
      const orderedNames = Array.from(networkLayerGroups.keys()).sort((a, b) =>
        a.localeCompare(b)
      );

      for (const name of orderedNames) {{
        const group = networkLayerGroups.get(name);
        if (!group) continue;
        overlays[name] = group;
        group.addTo(map);
      }}

      networkControl = L.control.layers({{}}, overlays, {{
        position: "topright",
        collapsed: true,
      }});
      networkControl.addTo(map);

      const controlContainer = networkControl.getContainer();
      if (controlContainer) {{
        controlContainer.classList.add("network-filter-control");
      }}
    }}

    function markersNearMarker(clickedMarker, pixelRadius = 34) {{
      const clickedPoint = map.latLngToLayerPoint(clickedMarker.getLatLng());

      return siteMarkers.filter((candidate) => {{
        if (!map.hasLayer(candidate)) return false;
        const candidatePoint = map.latLngToLayerPoint(candidate.getLatLng());
        return clickedPoint.distanceTo(candidatePoint) <= pixelRadius;
      }});
    }}

    function zoomIntoMarkerGroup(markers) {{
      if (!markers.length) return;

      const bounds = L.latLngBounds(markers.map((marker) => marker.getLatLng()));
      const currentZoom = map.getZoom();
      const maxZoom = map.getMaxZoom() || 19;
      const targetZoom = Math.min(currentZoom + 2, maxZoom);

      if (currentZoom >= maxZoom - 1) {{
        markers[0].openPopup();
        return;
      }}

      map.closePopup();

      if (bounds.isValid()) {{
        map.flyToBounds(bounds, {{
          padding: [90, 90],
          maxZoom: targetZoom,
          duration: 0.35,
        }});
      }}
    }}

    function handleSiteMarkerClick(marker) {{
      const nearby = markersNearMarker(marker, 34);
      const uniqueCoordCount = new Set(
        nearby.map((candidate) => {{
          const latLng = candidate.getLatLng();
          return `${{latLng.lat.toFixed(6)}},${{latLng.lng.toFixed(6)}}`;
        }})
      ).size;

      // If multiple markers collapse into one physical point, treat it as unambiguous.
      if (nearby.length > 1 && uniqueCoordCount > 1) {{
        zoomIntoMarkerGroup(nearby);
        return;
      }}

      marker.openPopup();
    }}

    function fitToSites() {{
      const visibleMarkers = siteMarkers.filter((marker) => markerVisible(marker));
      if (!visibleMarkers.length) {{
        map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
        return;
      }}

      const bounds = L.latLngBounds(visibleMarkers.map((marker) => marker.getLatLng()));
      map.fitBounds(bounds, {{
        paddingTopLeft: [96, 96],
        paddingBottomRight: [96, 96],
        maxZoom: 10,
      }});
    }}

    function updateMappedCount() {{
      const mappedCount = siteMarkers.filter((marker) => markerVisible(marker)).length;
      siteCount.textContent = `${{mappedCount}} mapped site${{mappedCount === 1 ? "" : "s"}}`;
    }}

    async function loadSites() {{
      const response = await fetch("/api/sites", {{
        headers: {{ "Accept": "application/json" }},
      }});
      if (!response.ok) {{
        throw new Error(`HTTP ${{response.status}} while loading /api/sites`);
      }}

      sites = await response.json();
      siteMarkers.length = 0;
      for (const group of networkLayerGroups.values()) {{
        group.clearLayers();
        map.removeLayer(group);
      }}
      networkLayerGroups.clear();

      for (const site of sites) {{
        addSite(site);
      }}

      renderNetworkControl();

      map.off("overlayadd", updateMappedCount);
      map.off("overlayremove", updateMappedCount);
      map.on("overlayadd", updateMappedCount);
      map.on("overlayremove", updateMappedCount);

      updateMappedCount();

      requestAnimationFrame(() => {{
        map.invalidateSize();
        fitToSites();
      }});
    }}

    loadSites().catch((error) => {{
      console.error(error);
      siteCount.textContent = "Map failed to load";
      requestAnimationFrame(() => {{
        map.invalidateSize();
        map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
      }});
    }});

    showAllButton.addEventListener("click", () => {{
      map.closePopup();
      fitToSites();
    }});

    window.addEventListener("load", () => {{
      map.invalidateSize();
    }});
  </script>
</body>
</html>"""
