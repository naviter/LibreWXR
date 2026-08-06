# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
"""Tests for the MVT lightning tile encoder and /v2/lightning/{ts}/{z}/{x}/{y}.mvt route.

Round-trips the hand-rolled protobuf encoder through the ``mapbox-vector-tile``
library (dev dep only) to catch wire-format bugs.  Route tests cover auth,
validation, Cache-Control split, and Mercator quantization.
"""
from __future__ import annotations

import math
import time

import pytest

pytestmark = pytest.mark.tiles


# ── Encoder unit tests ────────────────────────────────────────────────────────

def test_encode_empty_tile_returns_empty_bytes():
    from librewxr.tiles.mvt import encode_point_tile, LIGHTNING_LAYER

    assert encode_point_tile(LIGHTNING_LAYER, []) == b""


def test_encode_single_point_roundtrip():
    mapbox_vector_tile = pytest.importorskip("mapbox_vector_tile")
    from librewxr.tiles.mvt import encode_point_tile, LIGHTNING_LAYER

    now = int(time.time())
    data = encode_point_tile(LIGHTNING_LAYER, [(100, 200, now)])
    assert len(data) > 0

    decoded = mapbox_vector_tile.decode(data)
    assert LIGHTNING_LAYER in decoded
    layer = decoded[LIGHTNING_LAYER]
    assert len(layer["features"]) == 1
    feat = layer["features"][0]
    assert feat["properties"]["t"] == now
    assert feat["geometry"]["type"] == "Point"


def test_encode_multiple_points_dedup_values():
    mapbox_vector_tile = pytest.importorskip("mapbox_vector_tile")
    from librewxr.tiles.mvt import encode_point_tile, LIGHTNING_LAYER

    now = int(time.time())
    # Two points share the same t — the value table should deduplicate.
    points = [(100, 200, now), (300, 400, now), (500, 600, now - 60)]
    data = encode_point_tile(LIGHTNING_LAYER, points)
    decoded = mapbox_vector_tile.decode(data)
    layer = decoded[LIGHTNING_LAYER]
    assert len(layer["features"]) == 3
    ts_vals = {f["properties"]["t"] for f in layer["features"]}
    assert ts_vals == {now, now - 60}


def test_encode_negative_pixel_coords_zigzag():
    """Out-of-extent (buffer) pixels must survive the zigzag encoding."""
    mapbox_vector_tile = pytest.importorskip("mapbox_vector_tile")
    from librewxr.tiles.mvt import encode_point_tile, LIGHTNING_LAYER

    now = int(time.time())
    # Negative coords are valid buffer pixels (MVT spec §4.3.2).
    data = encode_point_tile(LIGHTNING_LAYER, [(-64, -64, now)])
    assert len(data) > 0
    decoded = mapbox_vector_tile.decode(data)
    assert LIGHTNING_LAYER in decoded


def test_zigzag_encoding():
    from librewxr.tiles.mvt import _zigzag

    assert _zigzag(0) == 0
    assert _zigzag(-1) == 1
    assert _zigzag(1) == 2
    assert _zigzag(-2) == 3
    assert _zigzag(2147483647) == 4294967294


# ── Mercator quantization ─────────────────────────────────────────────────────

def test_mercator_quantize_not_linear():
    """Mercator quantization must differ from linear lat interpolation.

    Properties verified:
    1. The equator maps to tile center (py ≈ 2048) — basic Mercator sanity.
    2. 45° north maps to upper half (py < 2048).
    3. 85° north maps even higher than 45° (smaller py).
    4. The Mercator pixel for 85° differs from linear-lat interpolation.
       In Mercator, the poleward *gap* is exaggerated: the 0.05° between 85°
       and the 85.05° tile edge takes more pixels than linear would give, so
       py_85_mercator > py_85_linear (farther from north in pixel space).
    """
    from librewxr.tiles.mvt import merc_y, quantize_point
    from librewxr.tiles.coordinates import tile_bounds

    west, south, east, north = tile_bounds(0, 0, 0)
    north_merc = merc_y(north)
    south_merc = merc_y(south)

    _, py_eq = quantize_point(0.0, 0.0, west, east, north_merc, south_merc)
    _, py_45 = quantize_point(45.0, 0.0, west, east, north_merc, south_merc)
    _, py_85 = quantize_point(85.0, 0.0, west, east, north_merc, south_merc)

    # Equator must land at tile center in Mercator projection.
    assert abs(py_eq - 2048) <= 1, f"Equator should be near tile center, got py={py_eq}"

    # Both 45° and 85° are in the upper half.
    assert py_45 < 4096 // 2, "45° should be in the northern (upper) half"
    assert py_85 < py_45, "85° should render higher (smaller py) than 45°"

    # Near the pole, Mercator stretches the gap between lat=85° and the 85.05° limit
    # into MORE pixels than a linear interpolation would, so Mercator py > linear py
    # (both are small, but Mercator is *larger*).
    linear_py_85 = int((north - 85.0) / (north - south) * 4096)
    assert py_85 != linear_py_85, "Mercator pixel must differ from linear lat pixel"
    assert py_85 > linear_py_85, (
        f"Mercator stretches the polar gap so py_85={py_85} > linear={linear_py_85}"
    )


# ── /v2/lightning/{ts}/{z}/{x}/{y}.mvt route ─────────────────────────────────

def _mvt_client(fake_grid=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from librewxr.api import routes

    test_app = FastAPI()
    test_app.include_router(routes.router)
    if fake_grid is not None:
        routes.lightning_grids = {"glm_grid": fake_grid}
    return TestClient(test_app, raise_server_exceptions=False)


class _FakeGrid:
    name = "GOES-GLM"
    flash_count = 0

    def __init__(self, flashes=None):
        self._flashes = flashes or []

    @property
    def timestamps(self):
        interval = 600
        return sorted({(ts // interval) * interval for ts, *_ in self._flashes})

    def flashes_since(self, seconds, bbox=None):
        cutoff = int(time.time()) - seconds
        return [f for f in self._flashes if f[0] >= cutoff]


@pytest.mark.api
def test_mvt_disabled_returns_503():
    from librewxr.api import routes

    saved = routes.lightning_grids
    routes.lightning_grids = {}
    try:
        client = _mvt_client()
        now_ts = (int(time.time()) // 600) * 600
        r = client.get(f"/v2/lightning/{now_ts}/4/8/5.mvt")
        assert r.status_code == 503
    finally:
        routes.lightning_grids = saved


@pytest.mark.api
def test_mvt_unaligned_cycle_ts_returns_404():
    from librewxr.api import routes

    saved_grids = routes.lightning_grids
    routes.lightning_grids = {"glm_grid": _FakeGrid()}
    try:
        client = _mvt_client()
        bad_ts = (int(time.time()) // 600) * 600 + 1  # off by 1
        r = client.get(f"/v2/lightning/{bad_ts}/4/8/5.mvt")
        assert r.status_code == 404
    finally:
        routes.lightning_grids = saved_grids


@pytest.mark.api
def test_mvt_expired_cycle_ts_returns_404():
    from librewxr.api import routes

    saved_grids = routes.lightning_grids
    routes.lightning_grids = {"glm_grid": _FakeGrid()}
    try:
        client = _mvt_client()
        very_old_ts = 0  # aligned but way outside retention
        r = client.get(f"/v2/lightning/{very_old_ts}/4/8/5.mvt")
        assert r.status_code == 404
    finally:
        routes.lightning_grids = saved_grids


@pytest.mark.api
def test_mvt_empty_ocean_tile_returns_200_empty_body():
    from librewxr.api import routes

    saved_grids = routes.lightning_grids
    routes.lightning_grids = {"glm_grid": _FakeGrid(flashes=[])}
    try:
        client = _mvt_client()
        now_ts = (int(time.time()) // 600) * 600
        r = client.get(f"/v2/lightning/{now_ts}/4/8/5.mvt")
        assert r.status_code == 200
        assert r.content == b""
        assert "application/vnd.mapbox-vector-tile" in r.headers.get("content-type", "")
    finally:
        routes.lightning_grids = saved_grids


@pytest.mark.api
def test_mvt_content_type():
    from librewxr.api import routes

    now = int(time.time())
    now_ts = (now // 600) * 600
    saved_grids = routes.lightning_grids
    routes.lightning_grids = {"glm_grid": _FakeGrid(flashes=[(now - 30, 40.0, -100.0, 0.0)])}
    try:
        client = _mvt_client()
        r = client.get(f"/v2/lightning/{now_ts}/4/8/5.mvt")
        assert r.status_code == 200
        assert "application/vnd.mapbox-vector-tile" in r.headers["content-type"]
    finally:
        routes.lightning_grids = saved_grids


@pytest.mark.api
def test_mvt_cache_control_newest_slot_short_ttl():
    from librewxr.api import routes

    now = int(time.time())
    now_ts = (now // 600) * 600
    saved_grids = routes.lightning_grids
    routes.lightning_grids = {"glm_grid": _FakeGrid()}
    try:
        client = _mvt_client()
        r = client.get(f"/v2/lightning/{now_ts}/4/8/5.mvt")
        assert r.status_code == 200
        cc = r.headers.get("cache-control", "")
        assert "max-age=60" in cc
    finally:
        routes.lightning_grids = saved_grids


@pytest.mark.api
def test_mvt_cache_control_old_slot_long_ttl():
    from librewxr.api import routes

    now = int(time.time())
    # Use a slot 20 min ago — within the 30-min retention window but not newest.
    old_ts = ((now - 1200) // 600) * 600
    saved_grids = routes.lightning_grids
    routes.lightning_grids = {"glm_grid": _FakeGrid()}
    try:
        client = _mvt_client()
        r = client.get(f"/v2/lightning/{old_ts}/4/8/5.mvt")
        assert r.status_code == 200
        cc = r.headers.get("cache-control", "")
        assert "max-age=3600" in cc
    finally:
        routes.lightning_grids = saved_grids


@pytest.mark.api
def test_manifest_includes_lightning_when_enabled():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from librewxr.api import routes
    from librewxr.data.store import FrameStore

    now = int(time.time())
    now_ts = (now // 600) * 600

    test_app = FastAPI()
    test_app.include_router(routes.router)
    saved_grids = routes.lightning_grids
    saved_store = routes.frame_store
    routes.lightning_grids = {"glm_grid": _FakeGrid(flashes=[(now - 30, 40.0, -100.0, 0.0)])}
    # Provide a minimal frame_store stub so weather-maps doesn't crash.
    class _FakeStore:
        async def get_timestamps(self): return []
        async def frame_count(self): return 0
    routes.frame_store = _FakeStore()
    try:
        client = TestClient(test_app, raise_server_exceptions=False)
        r = client.get("/public/weather-maps.json")
        assert r.status_code == 200
        body = r.json()
        assert body["lightning"] is not None
        ld = body["lightning"]
        assert "time" in ld and "path" in ld and "attribution" in ld
        assert ld["path"].startswith("/v2/lightning/")
        assert "NOAA" in ld["attribution"]
    finally:
        routes.lightning_grids = saved_grids
        routes.frame_store = saved_store


@pytest.mark.api
def test_manifest_lightning_absent_when_disabled():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from librewxr.api import routes

    test_app = FastAPI()
    test_app.include_router(routes.router)
    saved_grids = routes.lightning_grids
    saved_store = routes.frame_store
    routes.lightning_grids = {}
    class _FakeStore:
        async def get_timestamps(self): return []
        async def frame_count(self): return 0
    routes.frame_store = _FakeStore()
    try:
        client = TestClient(test_app, raise_server_exceptions=False)
        r = client.get("/public/weather-maps.json")
        assert r.status_code == 200
        body = r.json()
        assert body.get("lightning") is None
    finally:
        routes.lightning_grids = saved_grids
        routes.frame_store = saved_store
