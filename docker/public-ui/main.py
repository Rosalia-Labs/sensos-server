def render_index_html() -> str:
    """Render the public field-site map.

    Drop-in replacement for the previous custom canvas map.

    Behavior:
    - loads sites from /api/sites
    - uses Leaflet for pan/zoom/touch behavior
    - uses Esri World Imagery as the satellite basemap
    - clicking a marker opens the site's public dashboard by default
    - holding Shift/Cmd/Ctrl/Alt while clicking shows a small popup instead
    - the "Open selected site" button is enabled after selecting a marker

    Notes:
    - Leaflet itself is open source. The satellite imagery service is not "open
      source"; replace SATELLITE_TILES below if you later self-host imagery.
    - For fully offline/self-hosted deployments, vendor leaflet.css and
      leaflet.js under /static/vendor/leaflet/ and change the two CDN URLs.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SensOS Public Dashboard</title>

  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQ2ATbK3qsfNHh8Qw9DyJw3y7pFvDo8="
    crossorigin=""
  >
  <style>
    :root {{
      --bg: #edf1ea;
      --panel: rgba(255,255,255,0.9);
      --ink: #17201d;
      --muted: #5c6760;
      --accent: #0c6d62;
      --accent-2: #d97706;
      --border: rgba(23,32,29,0.14);
      --shadow: 0 24px 60px rgba(23,32,29,0.12);
      --marker: #0c6d62;
      --marker-active: #d97706;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      width: 100%;
      height: 100%;
    }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      background:
        radial-gradient(circle at top left, rgba(12,109,98,0.18), transparent 28rem),
        radial-gradient(circle at top right, rgba(217,119,6,0.14), transparent 22rem),
        linear-gradient(180deg, #f7f4ed 0%, var(--bg) 100%);
    }}
    .shell {{
      height: 100vh;
      min-height: 28rem;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 0.7rem;
      padding: 0.7rem 0.9rem;
    }}
    .masthead {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 0.7rem;
      min-width: 0;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(1.35rem, 2.2vw, 1.9rem);
      letter-spacing: -0.03em;
      white-space: nowrap;
    }}
    .meta {{
      color: var(--muted);
      font-size: 0.85rem;
      text-align: right;
      overflow-wrap: anywhere;
    }}
    .map-shell {{
      position: relative;
      min-height: 0;
      border: 1px solid var(--border);
      border-radius: 22px;
      overflow: hidden;
      background: #dfeae6;
      box-shadow: var(--shadow);
    }}
    #fieldSitesMap {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      z-index: 1;
      background: #dfeae6;
    }}
    .map-toolbar {{
      position: absolute;
      left: 0.7rem;
      top: 0.7rem;
      z-index: 500;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
      max-width: calc(100% - 1.4rem);
      pointer-events: none;
    }}
    .toolbar-button,
    .toolbar-pill {{
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.86);
      color: var(--ink);
      padding: 0.58rem 0.82rem;
      font: inherit;
      box-shadow: 0 10px 30px rgba(23,32,29,0.10);
      backdrop-filter: blur(14px);
      pointer-events: auto;
    }}
    .toolbar-button {{
      cursor: pointer;
    }}
    .toolbar-button:disabled {{
      cursor: default;
      opacity: 0.58;
    }}
    .toolbar-pill {{
      color: var(--muted);
      font-size: 0.86rem;
    }}
    .map-caption {{
      position: absolute;
      right: 0.7rem;
      bottom: 0.7rem;
      z-index: 500;
      color: var(--muted);
      font-size: 0.82rem;
      max-width: min(30rem, calc(100% - 1.4rem));
      text-align: right;
      background: rgba(255,255,255,0.76);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0.34rem 0.66rem;
      backdrop-filter: blur(14px);
      pointer-events: none;
    }}
    .site-marker {{
      width: 19px;
      height: 19px;
      border-radius: 999px;
      border: 3px solid rgba(255,255,255,0.96);
      background: var(--marker);
      box-shadow:
        0 0 0 1px rgba(23,32,29,0.24),
        0 8px 18px rgba(12,109,98,0.30);
    }}
    .site-marker.is-inactive {{
      opacity: 0.68;
      filter: grayscale(0.35);
    }}
    .site-marker.is-selected {{
      background: var(--marker-active);
      transform: scale(1.22);
      box-shadow:
        0 0 0 1px rgba(23,32,29,0.28),
        0 0 0 7px rgba(217,119,6,0.20),
        0 10px 22px rgba(217,119,6,0.32);
    }}
    .site-popup {{
      min-width: 14rem;
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      color: var(--ink);
    }}
    .site-popup-title {{
      margin: 0 0 0.25rem;
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
    .leaflet-container {{
      font: inherit;
      color: var(--ink);
    }}
    .leaflet-control-attribution {{
      font-size: 0.68rem;
      background: rgba(255,255,255,0.72);
    }}
    .leaflet-control-layers,
    .leaflet-control-zoom a {{
      border-color: var(--border) !important;
    }}
    .leaflet-control-zoom a {{
      color: var(--ink) !important;
      background: rgba(255,255,255,0.88) !important;
    }}
    .dim {{ color: var(--muted); }}
    .mono {{
      font-family: "SFMono-Regular", "Menlo", "Consolas", monospace;
      font-size: 0.86rem;
    }}
    a {{ color: var(--accent); }}
    @media (max-width: 760px) {{
      .shell {{
        padding: 0;
        gap: 0;
      }}
      .masthead {{
        position: absolute;
        left: 0.7rem;
        right: 0.7rem;
        top: 0.7rem;
        z-index: 510;
        pointer-events: none;
      }}
      .masthead h1 {{
        display: none;
      }}
      .meta {{
        display: none;
      }}
      .map-shell {{
        border-radius: 0;
        border: 0;
      }}
      .map-toolbar {{
        top: 0.7rem;
      }}
      .toolbar-pill {{
        display: none;
      }}
      .map-caption {{
        left: 0.7rem;
        right: auto;
        text-align: left;
        max-width: calc(100% - 1.4rem);
      }}
    }}
    {_theme_override_css()}
  </style>
</head>
<body>
  <div class="shell">
    <header class="masthead">
      <div><h1>Field Sites</h1></div>
      <div class="meta">Public dashboard · version {current_version()}</div>
    </header>

    <main class="map-shell">
      <div id="fieldSitesMap" aria-label="Mapped field sites"></div>

      <div class="map-toolbar">
        <button class="toolbar-button" id="resetViewButton" type="button">Reset view</button>
        <button class="toolbar-button" id="openSelectedButton" type="button" disabled>Open selected site</button>
        <span class="toolbar-pill" id="mapStatus">Loading mapped sites…</span>
      </div>

      <div class="map-caption" id="mapCaption">
        Click a field site to open its data page. Shift-click previews details.
      </div>
    </main>
  </div>

  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin=""
  ></script>
  <script>
    const SATELLITE_TILES =
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}";

    const SATELLITE_ATTRIBUTION =
      "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community";

    const DEFAULT_CENTER = [30.2672, -97.7431];
    const DEFAULT_ZOOM = 5;

    let sites = [];
    let selectedSite = null;
    let selectedMarker = null;
    const markersBySiteId = new Map();

    const mapStatus = document.getElementById("mapStatus");
    const mapCaption = document.getElementById("mapCaption");
    const resetViewButton = document.getElementById("resetViewButton");
    const openSelectedButton = document.getElementById("openSelectedButton");

    const map = L.map("fieldSitesMap", {{
      zoomControl: true,
      preferCanvas: true,
      worldCopyJump: true,
    }}).setView(DEFAULT_CENTER, DEFAULT_ZOOM);

    const satelliteLayer = L.tileLayer(SATELLITE_TILES, {{
      attribution: SATELLITE_ATTRIBUTION,
      maxZoom: 19,
      detectRetina: true,
    }}).addTo(map);

    const markerLayer = L.featureGroup().addTo(map);

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

    function markerClassName(site, isSelected) {{
      return [
        "site-marker",
        site?.is_active ? "" : "is-inactive",
        isSelected ? "is-selected" : "",
      ].filter(Boolean).join(" ");
    }}

    function makeMarkerIcon(site, isSelected = false) {{
      return L.divIcon({{
        className: "",
        html: `<div class="${{markerClassName(site, isSelected)}}"></div>`,
        iconSize: [19, 19],
        iconAnchor: [9.5, 9.5],
        popupAnchor: [0, -12],
      }});
    }}

    function popupHtml(site) {{
      const siteUrl = site.public_url || `/sites/${{encodeURIComponent(site.site_id)}}`;
      const statusUrl = site.status_url || `${{siteUrl}}/status`;
      const synopticUrl = site.synoptic_url || `${{siteUrl}}/synoptic`;
      const birdnetUrl = site.birdnet_rankings_url || `${{siteUrl}}/birdnet-rankings`;

      return `
        <div class="site-popup">
          <h2 class="site-popup-title">${{escapeHtml(displaySiteName(site))}}</h2>
          <div class="site-popup-meta">
            <div><span class="mono">${{escapeHtml(site.wg_ip || "")}}</span></div>
            <div>${{escapeHtml(site.network_name || "unknown network")}}</div>
            <div>Last check-in: ${{escapeHtml(formatRelativeTime(site.last_check_in))}}</div>
            <div>BirdNET detections: ${{escapeHtml(site.birdnet_detection_count ?? 0)}}</div>
          </div>
          <div class="site-popup-actions">
            <a href="${{escapeHtml(siteUrl)}}">Dashboard</a>
            <a href="${{escapeHtml(statusUrl)}}">Status</a>
            <a href="${{escapeHtml(synopticUrl)}}">Time series</a>
            <a href="${{escapeHtml(birdnetUrl)}}">BirdNET</a>
          </div>
        </div>
      `;
    }}

    function setSelectedSite(site, marker) {{
      if (selectedMarker && selectedSite) {{
        selectedMarker.setIcon(makeMarkerIcon(selectedSite, false));
      }}

      selectedSite = site || null;
      selectedMarker = marker || null;

      if (selectedMarker && selectedSite) {{
        selectedMarker.setIcon(makeMarkerIcon(selectedSite, true));
        openSelectedButton.disabled = !selectedSite.public_url;
        mapCaption.textContent =
          `${{displaySiteName(selectedSite)}} · click button to open, or shift-click markers to preview`;
      }} else {{
        openSelectedButton.disabled = true;
        mapCaption.textContent =
          "Click a field site to open its data page. Shift-click previews details.";
      }}
    }}

    function openSite(site) {{
      if (!site) return;
      const url = site.public_url || `/sites/${{encodeURIComponent(site.site_id)}}`;
      window.location.assign(url);
    }}

    function addSiteMarker(site) {{
      if (!Number.isFinite(site.latitude) || !Number.isFinite(site.longitude)) return;

      const marker = L.marker([site.latitude, site.longitude], {{
        icon: makeMarkerIcon(site, false),
        title: displaySiteName(site),
        riseOnHover: true,
      }});

      marker.bindTooltip(displaySiteName(site), {{
        direction: "top",
        opacity: 0.92,
        sticky: true,
      }});

      marker.bindPopup(() => popupHtml(site), {{
        maxWidth: 320,
        closeButton: true,
      }});

      marker.on("click", (event) => {{
        setSelectedSite(site, marker);

        // Default behavior is the clean map-only flow: click point -> data page.
        // Modified clicks give you an inspection mode without adding a sidebar.
        const original = event.originalEvent || {{}};
        if (original.shiftKey || original.metaKey || original.ctrlKey || original.altKey) {{
          marker.openPopup();
          return;
        }}

        openSite(site);
      }});

      marker.addTo(markerLayer);
      markersBySiteId.set(site.site_id, marker);
    }}

    function fitToSites() {{
      if (!markerLayer.getLayers().length) {{
        map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
        return;
      }}
      map.fitBounds(markerLayer.getBounds(), {{
        padding: [42, 42],
        maxZoom: 16,
      }});
    }}

    async function boot() {{
      try {{
        const response = await fetch("/api/sites", {{
          headers: {{ "Accept": "application/json" }},
        }});
        if (!response.ok) {{
          throw new Error(`HTTP ${{response.status}}`);
        }}

        sites = await response.json();
        markerLayer.clearLayers();
        markersBySiteId.clear();

        for (const site of sites) {{
          addSiteMarker(site);
        }}

        fitToSites();
        mapStatus.textContent = `${{sites.length}} mapped site${{sites.length === 1 ? "" : "s"}}`;
      }} catch (error) {{
        console.error(error);
        mapStatus.textContent = "Map failed to load";
        mapCaption.textContent = "Could not load /api/sites.";
      }}
    }}

    resetViewButton.addEventListener("click", () => {{
      setSelectedSite(null, null);
      fitToSites();
    }});

    openSelectedButton.addEventListener("click", () => {{
      openSite(selectedSite);
    }});

    window.addEventListener("keydown", (event) => {{
      if (event.key === "Escape") {{
        map.closePopup();
        setSelectedSite(null, null);
      }}
    }});

    boot();
  </script>
</body>
</html>"""
