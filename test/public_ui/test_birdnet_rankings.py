# SPDX-License-Identifier: MIT

import importlib.util
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


PUBLIC_UI_PATH = (
    Path(__file__).resolve().parents[2] / "docker" / "public-ui" / "main.py"
)


def load_public_ui_module():
    spec = importlib.util.spec_from_file_location("public_ui_main", PUBLIC_UI_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeCursor:
    def __init__(self):
        self.executed: list[str] = []
        self.params: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self.params.append(params)

    def fetchone(self):
        return (datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc),)

    def fetchall(self):
        return [
            (
                "Northern Cardinal (Cardinalis cardinalis)",
                1,
                0.91,
                2.73,
                3.0,
                0.91,
                0.91,
                None,
                0.018,
                0.91,
                datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc),
            )
        ]


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @contextmanager
    def cursor(self):
        yield self._cursor


class EmptyRowsCursor(FakeCursor):
    def fetchall(self):
        return []


class SiteMapCursor(FakeCursor):
    def fetchall(self):
        return [
            (
                "site-uuid",
                "10.88.1.2",
                "biosense",
                "biosense-1-2",
                "biosense-1-2",
                True,
                datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 4, 7, 11, 0, tzinfo=timezone.utc),
                30.2,
                -97.7,
                datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc),
                "biosense-1-2",
                "0.19.0",
                "ok",
            )
        ]


def test_site_map_uses_telemetry_free_view(monkeypatch):
    public_ui = load_public_ui_module()
    cursor = SiteMapCursor()
    monkeypatch.setattr(public_ui, "get_db", lambda: FakeConnection(cursor))

    sites = public_ui.fetch_sites()

    query = cursor.executed[0]
    assert "FROM sensos.public_site_map" in query
    assert "birdnet_detection_count" not in query
    assert "latest_i2c_upload_at" not in query
    assert "birdnet_detection_count" not in sites[0]
    assert sites[0]["last_check_in"] == "2026-04-07T12:00:00Z"


def test_passive_birdnet_views_use_score_by_likelihood_label():
    public_ui = load_public_ui_module()

    label_sql = public_ui.passive_birdnet_label_sql()

    assert label_sql == {
        "label": "weighted_label",
        "score": "weighted_score",
        "likely": "weighted_likely_score",
    }


def test_small_nonzero_chart_values_do_not_render_as_zero():
    public_ui = load_public_ui_module()

    assert public_ui._format_axis_value(0.0042) == "0.0042"
    assert public_ui._format_axis_value(0.000042) == "4.2e-05"
    assert public_ui._format_axis_value(0.0) == "0"


def test_birdnet_rankings_raw_mode_does_not_impute_missing_likelihood(monkeypatch):
    public_ui = load_public_ui_module()
    cursor = FakeCursor()

    monkeypatch.setattr(
        public_ui,
        "fetch_site_detail",
        lambda *args, **kwargs: {
            "peer_uuid": "peer-1",
            "wg_ip": "10.0.1.7",
            "synoptic_url": "/sites/peer-1/synoptic",
            "status_url": "/sites/peer-1/status",
        },
    )
    monkeypatch.setattr(public_ui, "get_db", lambda: FakeConnection(cursor))
    monkeypatch.setattr(public_ui, "relation_has_column", lambda *args: True)

    site = public_ui.fetch_site_birdnet_rankings(
        "peer-1",
        variable_key="score",
        statistic_key="max",
        weight_key="no",
        range_key="day",
        label_mode="raw",
    )

    ranking_sql = cursor.executed[-1]
    assert "coalesce((top_likely_score)::double precision, 1.0)" not in ranking_sql
    assert "max((top_score)::double precision) AS selected_metric" in ranking_sql
    assert site["birdnet_rankings"][0]["selected_metric"] == 0.91


def test_birdnet_rankings_raw_label_mode_can_weight_statistics(monkeypatch):
    public_ui = load_public_ui_module()
    cursor = FakeCursor()

    monkeypatch.setattr(
        public_ui,
        "fetch_site_detail",
        lambda *args, **kwargs: {
            "peer_uuid": "peer-1",
            "wg_ip": "10.0.1.7",
            "synoptic_url": "/sites/peer-1/synoptic",
            "status_url": "/sites/peer-1/status",
        },
    )
    monkeypatch.setattr(public_ui, "get_db", lambda: FakeConnection(cursor))
    monkeypatch.setattr(public_ui, "relation_has_column", lambda *args: True)

    site = public_ui.fetch_site_birdnet_rankings(
        "peer-1",
        variable_key="score",
        statistic_key="sum",
        weight_key="yes",
        range_key="day",
        label_mode="raw",
    )

    ranking_sql = cursor.executed[-1]
    assert "coalesce((top_likely_score)::double precision, 1.0)" not in ranking_sql
    assert (
        "sum(((top_score)::double precision) * (top_likely_score)) AS selected_metric"
        in ranking_sql
    )
    assert site["birdnet_ranking_weight"] == "yes"
    assert site["birdnet_ranking_metric_label"] == "Sum Score \u00d7 occup"


def test_birdnet_rankings_weighted_label_mode_requires_real_weighted_fields(monkeypatch):
    public_ui = load_public_ui_module()
    cursor = FakeCursor()

    monkeypatch.setattr(
        public_ui,
        "fetch_site_detail",
        lambda *args, **kwargs: {
            "peer_uuid": "peer-1",
            "wg_ip": "10.0.1.7",
            "synoptic_url": "/sites/peer-1/synoptic",
            "status_url": "/sites/peer-1/status",
        },
    )
    monkeypatch.setattr(public_ui, "get_db", lambda: FakeConnection(cursor))
    monkeypatch.setattr(public_ui, "relation_has_column", lambda *args: True)

    public_ui.fetch_site_birdnet_rankings(
        "peer-1",
        variable_key="score",
        statistic_key="sum",
        weight_key="no",
        range_key="day",
        label_mode="weighted",
    )

    ranking_sql = cursor.executed[-1]
    assert "coalesce(weighted_label, top_label)" not in ranking_sql
    assert "weighted_label AS selected_label" in ranking_sql
    assert "weighted_label IS NOT NULL AND weighted_score IS NOT NULL" in ranking_sql


def test_birdnet_species_page_uses_raw_or_weighted_label(monkeypatch):
    public_ui = load_public_ui_module()
    cursor = EmptyRowsCursor()

    monkeypatch.setattr(
        public_ui,
        "fetch_site_detail",
        lambda *args, **kwargs: {
            "peer_uuid": "peer-1",
            "wg_ip": "10.0.1.7",
            "synoptic_url": "/sites/peer-1/synoptic",
            "status_url": "/sites/peer-1/status",
            "public_url": "/sites/peer-1",
            "latitude": 30.0,
            "longitude": -97.0,
        },
    )
    monkeypatch.setattr(public_ui, "get_db", lambda: FakeConnection(cursor))
    monkeypatch.setattr(public_ui, "relation_has_column", lambda *args: True)

    site = public_ui.fetch_site_birdnet_species(
        "peer-1",
        "White-winged Dove (Zenaida asiatica)",
        range_key="all",
        label_mode="raw",
    )

    species_sql = cursor.executed[-1]
    assert "top_label = %s" in species_sql
    assert "weighted_label = %s" not in species_sql
    assert cursor.params[-1] == ("10.0.1.7", "White-winged Dove (Zenaida asiatica)")
    assert "label_mode=raw" in site["birdnet_species_url"]


def test_birdnet_species_weighted_range_anchors_on_site_before_label(monkeypatch):
    public_ui = load_public_ui_module()
    cursor = EmptyRowsCursor()
    monkeypatch.setattr(
        public_ui,
        "fetch_site_detail",
        lambda *args, **kwargs: {
            "peer_uuid": "peer-1",
            "wg_ip": "10.0.1.7",
            "synoptic_url": "/sites/peer-1/synoptic",
            "status_url": "/sites/peer-1/status",
            "public_url": "/sites/peer-1",
            "latitude": 30.0,
            "longitude": -97.0,
        },
    )
    monkeypatch.setattr(public_ui, "get_db", lambda: FakeConnection(cursor))
    monkeypatch.setattr(public_ui, "relation_has_column", lambda *args: True)

    site = public_ui.fetch_site_birdnet_species(
        "peer-1",
        "White-winged Dove (Zenaida asiatica)",
        range_key="day",
        label_mode="weighted",
    )

    species_sql = cursor.executed[-1]
    assert "d.weighted_label = %s" in species_sql
    assert "d.weighted_score IS NOT NULL" in species_sql
    assert cursor.params[-1] == (
        "10.0.1.7",
        "10.0.1.7",
        "White-winged Dove (Zenaida asiatica)",
        86400,
    )
    assert "label_mode=weighted" in site["birdnet_species_url"]
