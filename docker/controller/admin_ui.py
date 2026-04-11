# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import base64
import hashlib
import hmac
import html
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core import (
    ADMIN_API_PASSWORD,
    GIT_BRANCH,
    GIT_COMMIT,
    GIT_DIRTY,
    GIT_TAG,
    create_network_entry,
    current_server_version,
    delete_peer,
    get_db,
    set_peer_active_state,
    update_network_endpoint,
    wait_for_network_ready,
)

router = APIRouter(prefix="/admin", tags=["admin-ui"])

COOKIE_NAME = "sensos_admin_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_SECRET = hashlib.sha256(
    f"sensos-admin-ui:{ADMIN_API_PASSWORD}".encode("utf-8")
).digest()
HANDSHAKE_RE = re.compile(r"(\d+)\s+(\w+)\s+ago")


def issue_session_token() -> str:
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    nonce = secrets.token_urlsafe(8)
    payload = f"sensos|{expires_at}|{nonce}"
    signature = hmac.new(
        SESSION_SECRET, payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    token = f"{payload}|{signature}"
    return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")


def session_is_valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        username, expires_at_text, nonce, signature = decoded.split("|", 3)
        payload = f"{username}|{expires_at_text}|{nonce}"
    except Exception:
        return False
    expected_signature = hmac.new(
        SESSION_SECRET, payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return False
    if username != "sensos":
        return False
    try:
        expires_at = int(expires_at_text)
    except ValueError:
        return False
    return expires_at >= int(time.time())


def redirect_to_login(next_path: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/admin/login?next={quote_plus(sanitize_next_path(next_path))}",
        status_code=303,
    )


def require_session(request: Request) -> RedirectResponse | None:
    if session_is_valid(request.cookies.get(COOKIE_NAME)):
        return None
    return redirect_to_login(str(request.url.path))


def sanitize_next_path(next_path: str | None) -> str:
    value = (next_path or "").strip()
    if not value.startswith("/admin"):
        return "/admin"
    return value


def render_page(
    *,
    title: str,
    body: str,
    current_path: str,
    flash: str | None = None,
) -> HTMLResponse:
    flash_html = ""
    if flash:
        flash_html = f'<div class="flash">{html.escape(flash)}</div>'
    nav_items = [
        ("/admin", "Overview"),
        ("/admin/networks", "Networks"),
        ("/admin/peers", "Peers"),
        ("/admin/sensors", "Sensors"),
        ("/admin/birdnet", "BirdNET"),
        ("/admin/runtime", "Runtime"),
    ]
    nav_links = "".join(
        (
            f'<a class="nav-link{" active" if current_path == path else ""}" '
            f'href="{path}">{label}</a>'
        )
        for path, label in nav_items
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} | Sensos Admin</title>
  <style>
    :root {{
      --bg: #f2efe8;
      --panel: rgba(255,255,255,0.86);
      --ink: #1f2421;
      --muted: #5f685f;
      --accent: #0f766e;
      --accent-2: #d97706;
      --border: rgba(31,36,33,0.12);
      --danger: #b42318;
      --shadow: 0 20px 45px rgba(31, 36, 33, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15,118,110,0.18), transparent 28rem),
        radial-gradient(circle at top right, rgba(217,119,6,0.14), transparent 22rem),
        linear-gradient(180deg, #f8f5ef 0%, var(--bg) 100%);
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .shell {{ max-width: 1200px; margin: 0 auto; padding: 2rem 1.25rem 3rem; }}
    .masthead {{
      display: flex; justify-content: space-between; align-items: flex-start;
      gap: 1rem; margin-bottom: 1.5rem;
    }}
    .brand h1 {{ margin: 0; font-size: clamp(2rem, 3vw, 3rem); letter-spacing: -0.04em; }}
    .brand p {{ margin: 0.35rem 0 0; color: var(--muted); }}
    .top-actions {{ display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap; }}
    .meta {{ color: var(--muted); font-size: 0.92rem; }}
    .nav {{
      display: flex; gap: 0.6rem; flex-wrap: wrap; margin-bottom: 1.25rem;
    }}
    .nav-link {{
      padding: 0.7rem 1rem; border-radius: 999px; background: rgba(255,255,255,0.55);
      border: 1px solid var(--border); color: var(--ink);
    }}
    .nav-link.active {{ background: var(--ink); color: #fff; border-color: var(--ink); }}
    .panel {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 20px;
      box-shadow: var(--shadow); backdrop-filter: blur(18px);
      padding: 1.1rem 1.15rem;
    }}
    .flash {{
      margin-bottom: 1rem; padding: 0.9rem 1rem; border-radius: 14px;
      background: rgba(15,118,110,0.09); border: 1px solid rgba(15,118,110,0.18);
    }}
    .grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1rem; margin-bottom: 1rem;
    }}
    .stat-value {{ font-size: 2rem; font-weight: 700; letter-spacing: -0.04em; }}
    .stat-label, .help, .dim {{ color: var(--muted); }}
    .section-title {{ margin: 0 0 0.85rem; font-size: 1.2rem; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 0.7rem 0.55rem; border-bottom: 1px solid var(--border); vertical-align: top; }}
    th {{ font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }}
    .badge {{
      display: inline-block; padding: 0.22rem 0.55rem; border-radius: 999px; font-size: 0.8rem;
      background: rgba(95,104,95,0.12); color: var(--ink);
    }}
    .badge.ok {{ background: rgba(15,118,110,0.12); color: #0d5f58; }}
    .badge.warn {{ background: rgba(217,119,6,0.12); color: #9a6700; }}
    .badge.err {{ background: rgba(180,35,24,0.12); color: var(--danger); }}
    form.inline {{ display: inline-flex; gap: 0.45rem; align-items: center; flex-wrap: wrap; margin: 0; }}
    form.block {{ display: grid; gap: 0.8rem; }}
    label {{ display: grid; gap: 0.35rem; font-weight: 600; }}
    input, button, select {{
      font: inherit; border-radius: 12px; border: 1px solid var(--border);
      padding: 0.72rem 0.85rem; background: rgba(255,255,255,0.82); color: var(--ink);
    }}
    button {{
      cursor: pointer; background: var(--ink); color: #fff; border-color: var(--ink);
    }}
    button.secondary {{ background: transparent; color: var(--ink); }}
    button.warn {{ background: var(--accent-2); border-color: var(--accent-2); }}
    button.danger {{ background: var(--danger); border-color: var(--danger); }}
    .stack {{ display: grid; gap: 1rem; }}
    .split {{
      display: grid; grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
      gap: 1rem;
    }}
    .mono {{ font-family: "SFMono-Regular", "Menlo", "Consolas", monospace; font-size: 0.92rem; }}
    ul.clean {{ margin: 0; padding-left: 1.1rem; }}
    @media (max-width: 900px) {{
      .masthead {{ flex-direction: column; }}
      .split {{ grid-template-columns: 1fr; }}
      th:nth-child(5), td:nth-child(5) {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="masthead">
      <div class="brand">
        <h1>Sensos Admin</h1>
        <p>Operator dashboard on the existing controller service.</p>
      </div>
      <div class="top-actions">
        <span class="meta">Version {html.escape(current_server_version())}</span>
        <a class="nav-link" href="/admin/logout">Log out</a>
      </div>
    </div>
    <nav class="nav">{nav_links}</nav>
    {flash_html}
    {body}
  </div>
</body>
</html>
"""
    return HTMLResponse(page)


def render_login_page(next_path: str, error: str | None = None) -> HTMLResponse:
    error_html = ""
    if error:
        error_html = f'<div class="flash">{html.escape(error)}</div>'
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign In | Sensos Admin</title>
  <style>
    body {{
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      background:
        radial-gradient(circle at top, rgba(15,118,110,0.2), transparent 30rem),
        linear-gradient(180deg, #f8f5ef 0%, #ece7de 100%);
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      color: #1f2421;
    }}
    .card {{
      width: min(420px, calc(100vw - 2rem)); padding: 1.4rem;
      border-radius: 24px; background: rgba(255,255,255,0.9);
      border: 1px solid rgba(31,36,33,0.12); box-shadow: 0 18px 40px rgba(31,36,33,0.14);
    }}
    h1 {{ margin: 0 0 0.5rem; font-size: 2rem; letter-spacing: -0.05em; }}
    p {{ color: #5f685f; margin-top: 0; }}
    form {{ display: grid; gap: 0.8rem; }}
    label {{ display: grid; gap: 0.35rem; font-weight: 600; }}
    input, button {{
      font: inherit; padding: 0.8rem 0.9rem; border-radius: 12px; border: 1px solid rgba(31,36,33,0.12);
    }}
    button {{ background: #1f2421; color: #fff; border-color: #1f2421; cursor: pointer; }}
    .flash {{
      margin-bottom: 0.8rem; padding: 0.85rem 0.95rem; border-radius: 12px;
      background: rgba(180,35,24,0.08); border: 1px solid rgba(180,35,24,0.18); color: #7a271a;
    }}
    .help {{ color: #5f685f; font-size: 0.92rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Sign in</h1>
    <p>Use the existing admin API credential to open the operator dashboard.</p>
    {error_html}
    <form method="post" action="/admin/login">
      <input type="hidden" name="next" value="{html.escape(next_path)}">
      <label>
        Username
        <input type="text" name="username" value="sensos" autocomplete="username" required>
      </label>
      <label>
        Admin password
        <input type="password" name="password" autocomplete="current-password" required>
      </label>
      <button type="submit">Open dashboard</button>
    </form>
    <p class="help">This UI creates a same-site admin session cookie instead of relying on browser Basic auth prompts.</p>
  </div>
</body>
</html>
"""
    return HTMLResponse(page)


def badge_for_status(value: str | None) -> str:
    text = (value or "unknown").strip().lower()
    cls = "badge"
    if text in {"ready", "ok", "active", "healthy", "true"}:
        cls += " ok"
    elif text in {"error", "failed", "inactive", "false"}:
        cls += " err"
    elif text in {"starting", "pending", "warning"}:
        cls += " warn"
    return f'<span class="{cls}">{html.escape(value or "unknown")}</span>'


def format_timestamp(value) -> str:
    if value is None:
        return "Never"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return html.escape(str(value))


def summarize_age(value) -> str:
    if value is None:
        return "Never"
    if not isinstance(value, datetime):
        return html.escape(str(value))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value.astimezone(timezone.utc)
    if delta.total_seconds() < 0:
        delta = timedelta(0)
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m ago"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}h ago"
    return f"{total_seconds // 86400}d ago"


def peer_display_label(row: dict) -> str:
    note = str(row.get("note") or "").strip()
    if note:
        return note
    return str(row.get("wg_ip") or "Unknown")


def parse_wireguard_peers(output: str) -> list[dict[str, str]]:
    lines = output.strip().splitlines()
    peers: list[dict[str, str]] = []
    current_peer: dict[str, str] = {}
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
            key, value = map(str.strip, line.split(":", 1))
            current_peer[key] = value
    if current_peer:
        peers.append(current_peer)
    return peers


def normalize_handshake(text: str) -> str:
    match = HANDSHAKE_RE.match(text)
    if not match:
        return text
    count, unit = match.groups()
    try:
        delta = timedelta(**{unit: int(count)})
    except Exception:
        return text
    ts = datetime.now(timezone.utc) - delta
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def fetch_dashboard_overview() -> dict:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sensos.networks;")
            network_count = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM sensos.wireguard_peers;")
            peer_count = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM sensos.wireguard_peers WHERE is_active = TRUE;"
            )
            active_peer_count = cur.fetchone()[0]
            cur.execute(
                """
                WITH latest_status AS (
                    SELECT DISTINCT ON (peer_id)
                        peer_id, last_check_in, hostname, status_message, version
                    FROM sensos.client_status
                    ORDER BY peer_id, last_check_in DESC
                )
                SELECT count(*) FILTER (WHERE ls.last_check_in IS NOT NULL),
                       max(ls.last_check_in)
                FROM sensos.wireguard_peers p
                LEFT JOIN latest_status ls ON ls.peer_id = p.id;
                """
            )
            reporting_clients, latest_check_in = cur.fetchone()
            cur.execute(
                """
                SELECT component, role, network_id, status, last_error, updated_at
                FROM sensos.runtime_wireguard_status
                ORDER BY updated_at DESC;
                """
            )
            runtime_rows = cur.fetchall()
    ready_components = sum(1 for row in runtime_rows if row[3] == "ready")
    error_components = sum(1 for row in runtime_rows if row[4])
    return {
        "network_count": network_count,
        "peer_count": peer_count,
        "active_peer_count": active_peer_count,
        "reporting_clients": reporting_clients,
        "latest_check_in": latest_check_in,
        "runtime_count": len(runtime_rows),
        "ready_components": ready_components,
        "error_components": error_components,
    }


def fetch_network_rows() -> list[dict]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.id,
                       n.name,
                       n.ip_range::text,
                       n.wg_public_ip,
                       n.wg_port,
                       n.wg_public_key,
                       count(p.id) AS peer_count
                FROM sensos.networks n
                LEFT JOIN sensos.wireguard_peers p ON p.network_id = n.id
                GROUP BY n.id
                ORDER BY n.name;
                """
            )
            rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "ip_range": row[2],
            "wg_public_ip": row[3],
            "wg_port": row[4],
            "wg_public_key": row[5],
            "peer_count": row[6],
        }
        for row in rows
    ]


def fetch_peer_rows(
    network_name: str | None = None,
    sort_by: str = "network",
    direction: str = "asc",
) -> list[dict]:
    query = """
        WITH latest_status AS (
            SELECT DISTINCT ON (peer_id)
                peer_id,
                last_check_in,
                hostname,
                version,
                status_message
            FROM sensos.client_status
            ORDER BY peer_id, last_check_in DESC
        )
        SELECT p.uuid::text,
               p.wg_ip::text,
               n.name,
               p.is_active,
               p.note,
               p.registered_at,
               ls.last_check_in,
               ls.hostname,
               ls.version,
               ls.status_message
        FROM sensos.wireguard_peers p
        JOIN sensos.networks n ON n.id = p.network_id
        LEFT JOIN latest_status ls ON ls.peer_id = p.id
    """
    params: list[str] = []

    if network_name is not None:
        query += " WHERE n.name = %s"
        params.append(network_name)

    query += " ORDER BY n.name, p.wg_ip;"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    peers = [
        {
            "peer_uuid": row[0],
            "wg_ip": row[1],
            "network_name": row[2],
            "is_active": row[3],
            "note": row[4],
            "registered_at": row[5],
            "last_check_in": row[6],
            "hostname": row[7],
            "version": row[8],
            "status_message": row[9],
        }
        for row in rows
    ]

    sorters = {
        "network": lambda row: ((row["network_name"] or "").lower(), row["wg_ip"]),
        "host": lambda row: ((row["hostname"] or "").lower(), row["wg_ip"]),
        "checkin": lambda row: (
            row["last_check_in"] or datetime.min.replace(tzinfo=timezone.utc),
            row["wg_ip"],
        ),
        "state": lambda row: (row["is_active"], row["wg_ip"]),
        "client": lambda row: ((row["note"] or row["wg_ip"]).lower(), row["wg_ip"]),
    }

    key_func = sorters.get(sort_by, sorters["network"])
    peers.sort(key=key_func, reverse=(direction == "desc"))
    return peers


def fetch_network_names() -> list[str]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM sensos.networks ORDER BY name;")
            return [row[0] for row in cur.fetchall()]


def fetch_runtime_rows() -> list[dict]:
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
    return [
        {
            "component": row[0],
            "role": row[1],
            "network_name": row[2],
            "interface_name": row[3],
            "status": row[4],
            "public_key": row[5],
            "raw_status": row[6] or "",
            "last_error": row[7],
            "updated_at": row[8],
            "peers": [
                {
                    "public_key": peer.get("public_key", "—"),
                    "allowed_ips": peer.get("allowed ips", "—"),
                    "endpoint": peer.get("endpoint", "—"),
                    "last_contact": normalize_handshake(
                        peer.get("latest handshake", "—")
                    ),
                    "transfer": peer.get("transfer", "—"),
                }
                for peer in parse_wireguard_peers(row[6] or "")
            ],
        }
        for row in rows
    ]


def fetch_birdnet_rows(limit: int = 100) -> list[dict]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.wireguard_ip::text,
                       p.note,
                       n.name,
                       b.hostname,
                       b.client_version,
                       b.batch_id,
                       b.ownership_mode,
                       b.source_count,
                       b.first_processed_at,
                       b.last_processed_at,
                       b.server_received_at,
                       b.receipt_id::text
                FROM sensos.birdnet_result_batches b
                LEFT JOIN sensos.wireguard_peers p ON p.wg_ip = b.wireguard_ip
                LEFT JOIN sensos.networks n ON n.id = p.network_id
                ORDER BY b.server_received_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return [
        {
            "wg_ip": row[0],
            "note": row[1],
            "network_name": row[2] or "—",
            "hostname": row[3] or "—",
            "client_version": row[4] or "—",
            "batch_id": row[5],
            "ownership_mode": row[6],
            "source_count": row[7],
            "first_processed_at": row[8],
            "last_processed_at": row[9],
            "server_received_at": row[10],
            "receipt_id": row[11],
        }
        for row in rows
    ]


def fetch_birdnet_overview() -> dict:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sensos.birdnet_result_batches;")
            batch_count = cur.fetchone()[0]
            cur.execute(
                "SELECT COALESCE(sum(source_count), 0) FROM sensos.birdnet_result_batches;"
            )
            source_count = cur.fetchone()[0]
            cur.execute(
                "SELECT max(server_received_at) FROM sensos.birdnet_result_batches;"
            )
            latest_upload = cur.fetchone()[0]
    return {
        "batch_count": batch_count,
        "source_count": source_count,
        "latest_upload": latest_upload,
    }


def fetch_sensor_rows(limit: int = 100) -> list[dict]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.wireguard_ip::text,
                       p.note,
                       n.name,
                       b.hostname,
                       b.client_version,
                       b.batch_id,
                       b.ownership_mode,
                       b.reading_count,
                       b.first_recorded_at,
                       b.last_recorded_at,
                       b.server_received_at,
                       b.receipt_id::text
                FROM sensos.i2c_reading_batches b
                LEFT JOIN sensos.wireguard_peers p ON p.wg_ip = b.wireguard_ip
                LEFT JOIN sensos.networks n ON n.id = p.network_id
                ORDER BY b.server_received_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return [
        {
            "wg_ip": row[0],
            "note": row[1],
            "network_name": row[2] or "—",
            "hostname": row[3] or "—",
            "client_version": row[4] or "—",
            "batch_id": row[5],
            "ownership_mode": row[6],
            "reading_count": row[7],
            "first_recorded_at": row[8],
            "last_recorded_at": row[9],
            "server_received_at": row[10],
            "receipt_id": row[11],
        }
        for row in rows
    ]


def fetch_sensor_overview() -> dict:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sensos.i2c_reading_batches;")
            batch_count = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM sensos.i2c_readings;")
            reading_count = cur.fetchone()[0]
            cur.execute(
                "SELECT max(server_received_at) FROM sensos.i2c_reading_batches;"
            )
            latest_upload = cur.fetchone()[0]
    return {
        "batch_count": batch_count,
        "reading_count": reading_count,
        "latest_upload": latest_upload,
    }


def stat_card(label: str, value: str, help_text: str) -> str:
    return f"""
<section class="panel">
  <div class="stat-value">{html.escape(value)}</div>
  <div class="stat-label">{html.escape(label)}</div>
  <div class="help">{html.escape(help_text)}</div>
</section>
"""


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/admin"):
    if session_is_valid(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(url=sanitize_next_path(next), status_code=303)
    return render_login_page(next_path=sanitize_next_path(next))


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    next_path = sanitize_next_path(str(form.get("next", "/admin")) or "/admin")
    if username != "sensos" or password != ADMIN_API_PASSWORD:
        return render_login_page(
            next_path=next_path, error="Invalid admin credentials."
        )

    response = RedirectResponse(url=next_path, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        issue_session_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("", response_class=HTMLResponse)
def overview_page(request: Request, flash: str | None = None):
    redirect = require_session(request)
    if redirect:
        return redirect

    overview = fetch_dashboard_overview()
    networks = fetch_network_rows()[:5]
    peers = fetch_peer_rows()[:8]
    body = f"""
<div class="grid">
  {stat_card("Networks", str(overview["network_count"]), "Defined client network ranges.")}
  {stat_card("Peers", str(overview["peer_count"]), f'{overview["active_peer_count"]} currently active.')}
  {stat_card("Reporting clients", str(overview["reporting_clients"]), f'Last check-in {summarize_age(overview["latest_check_in"])}.')}
  {stat_card("Runtime rows", str(overview["runtime_count"]), f'{overview["ready_components"]} ready, {overview["error_components"]} with errors.')}
</div>
<div class="split">
  <section class="panel">
    <h2 class="section-title">Recent peers</h2>
    <table>
      <thead>
        <tr><th>Client</th><th>Network</th><th>Host</th><th>Last check-in</th></tr>
      </thead>
      <tbody>
        {''.join(
            f"<tr><td>{html.escape(peer_display_label(row))}</td><td>{html.escape(row['network_name'])}</td>"
            f"<td>{html.escape(row['hostname'] or 'Unknown')}</td><td>{html.escape(summarize_age(row['last_check_in']))}</td></tr>"
            for row in peers
        ) or '<tr><td colspan="4" class="dim">No peers registered.</td></tr>'}
      </tbody>
    </table>
  </section>
  <section class="panel">
    <h2 class="section-title">Recent networks</h2>
    <table>
      <thead>
        <tr><th>Name</th><th>CIDR</th><th>Endpoint</th><th>Peers</th></tr>
      </thead>
      <tbody>
        {''.join(
            f"<tr><td>{html.escape(row['name'])}</td><td class='mono'>{html.escape(row['ip_range'])}</td>"
            f"<td class='mono'>{html.escape(row['wg_public_ip'])}:{row['wg_port']}</td><td>{row['peer_count']}</td></tr>"
            for row in networks
        ) or '<tr><td colspan="4" class="dim">No networks defined.</td></tr>'}
      </tbody>
    </table>
  </section>
</div>
<section class="panel">
  <h2 class="section-title">Build metadata</h2>
  <ul class="clean">
    <li><span class="mono">version</span>: {html.escape(current_server_version())}</li>
    <li><span class="mono">git_commit</span>: {html.escape(GIT_COMMIT)}</li>
    <li><span class="mono">git_branch</span>: {html.escape(GIT_BRANCH)}</li>
    <li><span class="mono">git_tag</span>: {html.escape(GIT_TAG)}</li>
    <li><span class="mono">git_dirty</span>: {html.escape(GIT_DIRTY)}</li>
  </ul>
</section>
"""
    return render_page(
        title="Overview",
        body=body,
        current_path="/admin",
        flash=flash,
    )


@router.get("/networks", response_class=HTMLResponse)
def networks_page(request: Request, flash: str | None = None):
    redirect = require_session(request)
    if redirect:
        return redirect

    rows = fetch_network_rows()
    body = f"""
<div class="split">
  <section class="panel">
    <h2 class="section-title">Current published endpoints</h2>
    <table>
      <thead>
        <tr><th>Name</th><th>CIDR</th><th>Endpoint</th><th>Key ready</th><th>Peers</th></tr>
      </thead>
      <tbody>
        {''.join(
            "<tr>"
            f"<td>{html.escape(row['name'])}</td>"
            f"<td class='mono'>{html.escape(row['ip_range'])}</td>"
            f"<td class='mono'>{html.escape(row['wg_public_ip'])}:{row['wg_port']}</td>"
            f"<td>{badge_for_status('ready' if row['wg_public_key'] else 'starting')}</td>"
            f"<td>{row['peer_count']}</td>"
            "</tr>"
            for row in rows
        ) or '<tr><td colspan="5" class="dim">No networks defined.</td></tr>'}
      </tbody>
    </table>
  </section>
  <section class="panel">
    <h2 class="section-title">Create network</h2>
    <form class="block" method="post" action="/admin/networks">
      <label>Network name<input type="text" name="name" placeholder="testing" required></label>
      <label>Published WireGuard IP or hostname<input type="text" name="wg_public_ip" placeholder="server.example.org" required></label>
      <label>Published WireGuard port<input type="number" name="wg_port" min="1" max="65535" placeholder="51820"></label>
      <button type="submit">Create or reconcile network</button>
    </form>
    <p class="help">These fields start blank. Placeholder text is only an example, not the current saved endpoint.</p>
    <p class="help">This reuses the same network-creation path as the CLI and waits for the generated WireGuard public key when needed.</p>
  </section>
</div>
<section class="panel">
  <h2 class="section-title">Update published endpoint</h2>
  <p class="help">Enter the replacement endpoint explicitly. The fields below do not autofill from the current saved values.</p>
  <form class="inline" method="post" action="/admin/networks/endpoint">
    <label>Network<input type="text" name="name" placeholder="testing" required></label>
    <label>WireGuard IP<input type="text" name="wg_public_ip" placeholder="server.example.org" required></label>
    <label>WireGuard port<input type="number" name="wg_port" min="1" max="65535" placeholder="51820" required></label>
    <button class="warn" type="submit">Update endpoint</button>
  </form>
</section>
"""
    return render_page(
        title="Networks",
        body=body,
        current_path="/admin/networks",
        flash=flash,
    )


@router.post("/networks")
async def create_network_action(request: Request):
    redirect = require_session(request)
    if redirect:
        return redirect
    form = await request.form()
    name = str(form.get("name", "")).strip()
    wg_public_ip = str(form.get("wg_public_ip", "")).strip()
    wg_port_text = str(form.get("wg_port", "")).strip()
    if not name or not wg_public_ip:
        return RedirectResponse(
            url="/admin/networks?flash=Network+name+and+WireGuard+IP+are+required.",
            status_code=303,
        )

    wg_port = None
    if wg_port_text:
        try:
            wg_port = int(wg_port_text)
        except ValueError:
            return RedirectResponse(
                url="/admin/networks?flash=WireGuard+port+must+be+numeric.",
                status_code=303,
            )

    try:
        with get_db() as conn:
            result, created = create_network_entry(
                conn.cursor(), name=name, wg_public_ip=wg_public_ip, wg_port=wg_port
            )
        if created or not result["wg_public_key"]:
            ready = wait_for_network_ready(name)
            result["wg_public_key"] = ready[2]
        message = (
            f"Network '{name}' created." if created else f"Network '{name}' reconciled."
        )
    except Exception as exc:
        message = f"Network action failed: {exc}"
    return RedirectResponse(
        url=f"/admin/networks?flash={quote_plus(message)}", status_code=303
    )


@router.post("/networks/endpoint")
async def update_network_endpoint_action(request: Request):
    redirect = require_session(request)
    if redirect:
        return redirect
    form = await request.form()
    name = str(form.get("name", "")).strip()
    wg_public_ip = str(form.get("wg_public_ip", "")).strip()
    wg_port_text = str(form.get("wg_port", "")).strip()
    try:
        wg_port = int(wg_port_text)
    except ValueError:
        return RedirectResponse(
            url="/admin/networks?flash=WireGuard+port+must+be+numeric.",
            status_code=303,
        )
    try:
        with get_db() as conn:
            update_network_endpoint(
                conn.cursor(), name=name, wg_public_ip=wg_public_ip, wg_port=wg_port
            )
        message = f"Updated endpoint for '{name}'."
    except Exception as exc:
        message = f"Endpoint update failed: {exc}"
    return RedirectResponse(
        url=f"/admin/networks?flash={quote_plus(message)}", status_code=303
    )


@router.get("/peers", response_class=HTMLResponse)
def peers_page(
    request: Request,
    flash: str | None = None,
    network: str | None = None,
    sort: str = "network",
    direction: str = "asc",
):
    redirect = require_session(request)
    if redirect:
        return redirect

    selected_network = (network or "").strip() or None
    sort = (
        sort if sort in {"network", "host", "checkin", "state", "client"} else "network"
    )
    direction = direction if direction in {"asc", "desc"} else "asc"
    rows = fetch_peer_rows(selected_network, sort, direction)
    network_names = fetch_network_names()
    body_rows = []
    for row in rows:
        action_label = "Deactivate" if row["is_active"] else "Activate"
        action_value = "false" if row["is_active"] else "true"
        body_rows.append(
            "<tr>"
            f"<td><div class='mono'>{html.escape(row['wg_ip'])}</div>"
            f"<div class='dim'>{html.escape((row['note'] or '').strip() or '—')}</div></td>"
            f"<td>{html.escape(row['network_name'])}</td>"
            f"<td>{html.escape(row['hostname'] or 'Unknown')}</td>"
            f"<td>{badge_for_status('active' if row['is_active'] else 'inactive')}</td>"
            f"<td>{html.escape(summarize_age(row['last_check_in']))}</td>"
            "<td>"
            f"<div>{html.escape(row['status_message'] or row['note'] or '—')}</div>"
            f"<div class='dim mono'>{html.escape(row['version'] or row['peer_uuid'])}</div>"
            "</td>"
            "<td>"
            f"<form class='inline' method='post' action='/admin/peers/{quote_plus(row['peer_uuid'])}/active'>"
            f"<input type='hidden' name='is_active' value='{action_value}'>"
            f"<button class='secondary' type='submit'>{action_label}</button>"
            "</form>"
            f"<form class='inline' method='post' action='/admin/peers/{quote_plus(row['peer_uuid'])}/delete' "
            "onsubmit=\"return confirm('Delete this peer and all related state?');\">"
            "<button class='danger' type='submit'>Delete</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    filter_form = f"""
<form class="inline" method="get" action="/admin/peers" style="margin-bottom: 1rem;">
  <label>Network
    <select name="network">
      <option value="">All networks</option>
      {''.join(
          f"<option value='{html.escape(name)}'{' selected' if selected_network == name else ''}>{html.escape(name)}</option>"
          for name in network_names
      )}
    </select>
  </label>
  <label>Sort
    <select name="sort">
      <option value="network"{' selected' if sort == 'network' else ''}>Network</option>
      <option value="client"{' selected' if sort == 'client' else ''}>Client</option>
      <option value="host"{' selected' if sort == 'host' else ''}>Host</option>
      <option value="checkin"{' selected' if sort == 'checkin' else ''}>Last check-in</option>
      <option value="state"{' selected' if sort == 'state' else ''}>State</option>
    </select>
  </label>
  <label>Direction
    <select name="direction">
      <option value="asc"{' selected' if direction == 'asc' else ''}>Ascending</option>
      <option value="desc"{' selected' if direction == 'desc' else ''}>Descending</option>
    </select>
  </label>
  <button class="secondary" type="submit">Apply</button>
</form>
"""
    body = f"""
<section class="panel">
  <h2 class="section-title">Registered peers</h2>
  {filter_form}
  <table>
    <thead>
      <tr><th>Client</th><th>Network</th><th>Host</th><th>State</th><th>Last check-in</th><th>Status</th><th>Actions</th></tr>
    </thead>
    <tbody>
      {''.join(body_rows) or '<tr><td colspan="7" class="dim">No peers registered.</td></tr>'}
    </tbody>
  </table>
</section>
"""
    return render_page(
        title="Peers",
        body=body,
        current_path="/admin/peers",
        flash=flash,
    )


@router.post("/peers/{peer_uuid}/active")
async def peer_active_action(request: Request, peer_uuid: str):
    redirect = require_session(request)
    if redirect:
        return redirect
    form = await request.form()
    is_active = str(form.get("is_active", "")).strip().lower() == "true"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT wg_ip::text FROM sensos.wireguard_peers WHERE uuid = %s;",
                (peer_uuid,),
            )
            row = cur.fetchone()
    wg_ip = row[0] if row else None
    ok = set_peer_active_state(wg_ip, is_active) if wg_ip else False
    message = (
        f"Peer '{wg_ip}' set to {'active' if is_active else 'inactive'}."
        if ok
        else f"Peer '{peer_uuid}' was not found."
    )
    return RedirectResponse(
        url=f"/admin/peers?flash={quote_plus(message)}", status_code=303
    )


@router.post("/peers/{peer_uuid}/delete")
def peer_delete_action(request: Request, peer_uuid: str):
    redirect = require_session(request)
    if redirect:
        return redirect
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT wg_ip::text FROM sensos.wireguard_peers WHERE uuid = %s;",
                (peer_uuid,),
            )
            row = cur.fetchone()
    wg_ip = row[0] if row else None
    ok = delete_peer(wg_ip) if wg_ip else False
    message = f"Peer '{wg_ip}' deleted." if ok else f"Peer '{wg_ip}' was not found."
    return RedirectResponse(
        url=f"/admin/peers?flash={quote_plus(message)}", status_code=303
    )


@router.get("/runtime", response_class=HTMLResponse)
def runtime_page(request: Request, flash: str | None = None):
    redirect = require_session(request)
    if redirect:
        return redirect

    rows = fetch_runtime_rows()
    sections = []
    for row in rows:
        peer_markup = ""
        if row["peers"]:
            peer_markup = (
                "<table><thead><tr><th>Peer key</th><th>Allowed IPs</th><th>Endpoint</th>"
                "<th>Last contact</th><th>Transfer</th></tr></thead><tbody>"
                + "".join(
                    "<tr>"
                    f"<td class='mono'>{html.escape(peer['public_key'])}</td>"
                    f"<td class='mono'>{html.escape(peer['allowed_ips'])}</td>"
                    f"<td class='mono'>{html.escape(peer['endpoint'])}</td>"
                    f"<td>{html.escape(peer['last_contact'])}</td>"
                    f"<td>{html.escape(peer['transfer'])}</td>"
                    "</tr>"
                    for peer in row["peers"]
                )
                + "</tbody></table>"
            )
        else:
            peer_markup = '<p class="dim">No peer handshake data is currently reported for this component.</p>'
        sections.append(
            f"""
<section class="panel">
  <h2 class="section-title">{html.escape(row["network_name"])} · {html.escape(row["component"])}</h2>
  <div class="grid">
    <div><div class="dim">Role</div><div>{html.escape(row["role"])}</div></div>
    <div><div class="dim">Interface</div><div class="mono">{html.escape(row["interface_name"] or "—")}</div></div>
    <div><div class="dim">Status</div><div>{badge_for_status(row["status"])}</div></div>
    <div><div class="dim">Updated</div><div>{html.escape(format_timestamp(row["updated_at"]))}</div></div>
  </div>
  <p><strong>Public key:</strong> <span class="mono">{html.escape(row["public_key"] or "—")}</span></p>
  <p><strong>Last error:</strong> {html.escape(row["last_error"] or "—")}</p>
  {peer_markup}
</section>
"""
        )
    body = (
        "".join(sections)
        if sections
        else '<section class="panel"><p class="dim">No runtime WireGuard status rows exist yet.</p></section>'
    )
    return render_page(
        title="Runtime",
        body=body,
        current_path="/admin/runtime",
        flash=flash,
    )


@router.get("/birdnet", response_class=HTMLResponse)
def birdnet_page(request: Request, flash: str | None = None):
    redirect = require_session(request)
    if redirect:
        return redirect

    overview = fetch_birdnet_overview()
    rows = fetch_birdnet_rows()
    body = f"""
<div class="grid">
  {stat_card("Upload batches", str(overview["batch_count"]), "Accepted BirdNET upload batches stored on the server.")}
  {stat_card("Processed files", str(overview["source_count"]), "BirdNET processed-file records received across all batches.")}
  {stat_card("Latest upload", summarize_age(overview["latest_upload"]), "Time since the most recent BirdNET batch was accepted.")}
</div>
<section class="panel">
  <h2 class="section-title">Recent BirdNET uploads</h2>
  <table>
    <thead>
      <tr><th>Client</th><th>Network</th><th>Host</th><th>Batch</th><th>Ownership</th><th>Sources</th><th>Processed window</th><th>Received</th></tr>
    </thead>
    <tbody>
      {''.join(
          "<tr>"
          f"<td><div class='mono'>{html.escape(row['wg_ip'])}</div><div class='dim'>{html.escape((row['note'] or '').strip() or '—')}</div></td>"
          f"<td>{html.escape(row['network_name'])}</td>"
          f"<td>{html.escape(row['hostname'])}</td>"
          f"<td><div>{row['batch_id']}</div><div class='dim mono'>{html.escape(row['receipt_id'])}</div></td>"
          f"<td>{html.escape(row['ownership_mode'])}</td>"
          f"<td>{row['source_count']}</td>"
          f"<td><div>{html.escape(format_timestamp(row['first_processed_at']))}</div><div class='dim'>{html.escape(format_timestamp(row['last_processed_at']))}</div></td>"
          f"<td><div>{html.escape(format_timestamp(row['server_received_at']))}</div><div class='dim'>{html.escape(row['client_version'])}</div></td>"
          "</tr>"
          for row in rows
      ) or '<tr><td colspan="8" class="dim">No BirdNET uploads stored yet.</td></tr>'}
    </tbody>
  </table>
</section>
"""
    return render_page(
        title="BirdNET",
        body=body,
        current_path="/admin/birdnet",
        flash=flash,
    )


@router.get("/sensors", response_class=HTMLResponse)
def sensors_page(request: Request, flash: str | None = None):
    redirect = require_session(request)
    if redirect:
        return redirect

    overview = fetch_sensor_overview()
    rows = fetch_sensor_rows()
    body = f"""
<div class="grid">
  {stat_card("Upload batches", str(overview["batch_count"]), "Accepted sensor upload batches stored on the server.")}
  {stat_card("Readings", str(overview["reading_count"]), "Individual sensor readings received across all batches.")}
  {stat_card("Latest upload", summarize_age(overview["latest_upload"]), "Time since the most recent sensor batch was accepted.")}
</div>
<section class="panel">
  <h2 class="section-title">Recent sensor uploads</h2>
  <table>
    <thead>
      <tr><th>Client</th><th>Network</th><th>Host</th><th>Batch</th><th>Ownership</th><th>Readings</th><th>Recorded window</th><th>Received</th></tr>
    </thead>
    <tbody>
      {''.join(
          "<tr>"
          f"<td><div class='mono'>{html.escape(row['wg_ip'])}</div><div class='dim'>{html.escape((row['note'] or '').strip() or '—')}</div></td>"
          f"<td>{html.escape(row['network_name'])}</td>"
          f"<td>{html.escape(row['hostname'])}</td>"
          f"<td><div>{row['batch_id']}</div><div class='dim mono'>{html.escape(row['receipt_id'])}</div></td>"
          f"<td>{html.escape(row['ownership_mode'])}</td>"
          f"<td>{row['reading_count']}</td>"
          f"<td><div>{html.escape(format_timestamp(row['first_recorded_at']))}</div><div class='dim'>{html.escape(format_timestamp(row['last_recorded_at']))}</div></td>"
          f"<td><div>{html.escape(format_timestamp(row['server_received_at']))}</div><div class='dim'>{html.escape(row['client_version'])}</div></td>"
          "</tr>"
          for row in rows
      ) or '<tr><td colspan="8" class="dim">No sensor uploads stored yet.</td></tr>'}
    </tbody>
  </table>
</section>
"""
    return render_page(
        title="Sensors",
        body=body,
        current_path="/admin/sensors",
        flash=flash,
    )
