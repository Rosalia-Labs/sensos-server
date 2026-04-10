# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import psycopg

from fastapi import FastAPI, HTTPException
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
            "site_id": row[1],
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
                WHERE wg_ip = %s;
                """,
                (site_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Site not found.")
            cur.execute(
                """
                SELECT count(*)::integer,
                       max(processed_at)
                FROM sensos.public_site_birdnet_detections
                WHERE wg_ip = %s;
                """,
                (site_id,),
            )
            birdnet_summary = cur.fetchone()
            cur.execute(
                """
                SELECT count(*)::integer,
                       max(recorded_at)
                FROM sensos.public_site_i2c_recent
                WHERE wg_ip = %s;
                """,
                (site_id,),
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
                (site_id,),
            )
            batches = cur.fetchall()
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
                (site_id,),
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
                (site_id,),
            )
            readings = cur.fetchall()
    return {
        "peer_uuid": row[0],
        "site_id": row[1],
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
    .toolbar-chip, .toolbar-button {{
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
    }}
    .metric-value {{
      font-size: 1.5rem;
      font-weight: 700;
      letter-spacing: -0.05em;
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
          <div class="toolbar-chip" id="siteCountChip">Loading sites…</div>
          <button class="toolbar-button" id="resetViewButton" type="button">Reset View</button>
        </div>
        <div class="map-stage" id="mapStage">
          <canvas id="mapCanvas"></canvas>
          <div class="markers" id="markersLayer"></div>
        </div>
        <div class="map-caption">Markers stay fixed in screen size so site visibility does not depend on zoom. Local overlap is intentional.</div>
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
          <div class="section-title">Recent I2C Results</div>
          <div id="i2cList" class="record-list"><div class="dim">Select a site to inspect recent I2C readings.</div></div>
        </section>
      </aside>
    </div>
  </div>
  <script>
    const worldBounds = {{ lonMin: -180, lonMax: 180, latMin: -90, latMax: 90 }};
    const minLonSpan = 1.4;
    const minLatSpan = 1.0;
    let currentView = {{ ...worldBounds }};
    let sites = [];
    let activeSiteId = null;
    let chooserSites = [];

    const mapStage = document.getElementById("mapStage");
    const canvas = document.getElementById("mapCanvas");
    const markersLayer = document.getElementById("markersLayer");
    const siteCountChip = document.getElementById("siteCountChip");
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

      const grad = ctx.createLinearGradient(0, 0, 0, rect.height);
      grad.addColorStop(0, "#deebe7");
      grad.addColorStop(1, "#cad8d1");
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, rect.width, rect.height);

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
      siteCountChip.textContent = `${{sites.length}} mapped sites`;
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
        button.addEventListener("click", () => loadSiteDetail(site.site_id));
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

    async function loadSiteDetail(siteId) {{
      const response = await fetch(`/api/sites/${{encodeURIComponent(siteId)}}`);
      if (!response.ok) return;
      const site = await response.json();
      activeSiteId = site.site_id;
      setChooserSites([]);
      siteTitle.textContent = site.site_label;
      siteSubtitle.innerHTML = `<span class="mono">${{escapeHtml(site.wg_ip)}}</span> · ${{escapeHtml(site.network_name)}}`;
      metricGrid.innerHTML = `
        <div class="metric"><div class="section-title">Latest Check-In</div><div class="metric-value">${{escapeHtml(relativeTime(site.last_check_in))}}</div></div>
        <div class="metric"><div class="section-title">BirdNET Detections</div><div class="metric-value">${{site.birdnet_detection_count}}</div><div class="dim">${{escapeHtml(relativeTime(site.latest_birdnet_result_at))}}</div></div>
        <div class="metric"><div class="section-title">I2C Readings</div><div class="metric-value">${{site.i2c_reading_count}}</div><div class="dim">${{escapeHtml(relativeTime(site.latest_i2c_reading_at))}}</div></div>
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
        i2cList.innerHTML = '<div class="dim">No I2C readings are visible yet for this site.</div>';
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
        loadSiteDetail(candidates[0].site.site_id);
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
      i2cList.innerHTML = '<div class="dim">Select a site to inspect recent I2C readings.</div>';
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


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(render_index_html())
