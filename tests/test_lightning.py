# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
"""Tests for the lightning layer (GOES-GLM + EUMETSAT MTG-LI).

Offline only — no network.  Covers the shared ``FlashStore`` (retention,
bbox/since query, pickle round-trip), provider discovery + gating, and
the ``/v2/lightning`` endpoint (shape, bbox parse, 503 when disabled).
The network/decode halves of each source are exercised against live
buckets in manual runs, not here.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.sources


def _store():
    from librewxr.sources.lightning._common import FlashStore

    s = FlashStore.__new__(FlashStore)
    # Bypass settings dependency for a deterministic 30-min window.
    import threading
    s.name = "test"
    s._flashes = []
    s._lock = threading.Lock()
    s._retention_seconds = 1800
    s._newest_ts = 0
    return s


def test_flashstore_ingest_and_since_window():
    s = _store()
    now = int(time.time())
    added = s._ingest([
        (now - 60, 40.0, -100.0, 1e-14),    # fresh
        (now - 1700, 41.0, -101.0, 2e-14),  # old but in window
        (now - 5000, 42.0, -102.0, 3e-14),  # outside retention → dropped
    ])
    assert added == 2
    assert s.flash_count == 2
    # since=300 only catches the 60-s-old strike.
    recent = s.flashes_since(300)
    assert len(recent) == 1
    assert recent[0][1] == 40.0


def test_flashstore_bbox_filter():
    s = _store()
    now = int(time.time())
    s._ingest([
        (now - 10, 40.0, -100.0, 0.0),  # CONUS
        (now - 10, 48.0, 10.0, 0.0),    # Europe
    ])
    conus = s.flashes_since(600, (-125.0, 24.0, -66.0, 50.0))
    assert len(conus) == 1 and conus[0][2] == -100.0
    europe = s.flashes_since(600, (-10.0, 35.0, 30.0, 60.0))
    assert len(europe) == 1 and europe[0][2] == 10.0


def test_flashstore_dedup_by_high_water_mark():
    s = _store()
    now = int(time.time())
    s._ingest([(now - 100, 1.0, 1.0, 0.0)])
    # Re-ingesting the same (or older) timestamp adds nothing.
    assert s._ingest([(now - 100, 1.0, 1.0, 0.0)]) == 0
    assert s._ingest([(now - 50, 2.0, 2.0, 0.0)]) == 1


def test_flashstore_pickle_roundtrip():
    import pickle

    s = _store()
    now = int(time.time())
    s._ingest([(now - 30, 5.0, 6.0, 1e-13)])
    clone = pickle.loads(pickle.dumps(s))
    assert clone.flash_count == 1
    assert clone.flashes_since(600)[0][1] == 5.0


def test_glm_filename_parse():
    from librewxr.sources.lightning.glm.source import GLMLightningSource

    dt = GLMLightningSource._parse_start_time(
        "OR_GLM-L2-LCFA_G19_s20261591240200_e20261591240400_c20261591240414.nc",
    )
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 6, 8, 12, 40)
    assert GLMLightningSource._parse_start_time("garbage.nc") is None


def test_glm_provider_gating():
    from librewxr.config import Settings
    from librewxr.sources.lightning.glm import lightning_provider

    on = lightning_provider(Settings(glm_enabled=True), None)
    assert on is not None and on.name == "GOES-GLM" and on.slug == "glm_grid"
    assert lightning_provider(Settings(glm_enabled=False), None) is None


def test_mtg_li_dormant_without_credentials():
    from librewxr.config import Settings
    from librewxr.sources.lightning.mtg_li import lightning_provider

    # No creds → dormant even when enabled.  Explicit empty kwargs
    # override any ambient .env credentials a dev may have configured.
    assert lightning_provider(
        Settings(mtg_li_enabled=True, eumetsat_key="", eumetsat_secret=""),
        None,
    ) is None
    # With creds → a contribution appears.
    got = lightning_provider(
        Settings(eumetsat_key="k", eumetsat_secret="s"), None,
    )
    assert got is not None and got.name == "MTG-LI"


def test_collect_lightning_respects_master_toggle():
    from librewxr.config import Settings
    from librewxr.sources import collect_lightning_contributions

    assert collect_lightning_contributions(
        Settings(lightning_enabled=False), None,
    ) == []
    # GLM is anonymous, so it appears with the layer enabled and no creds.
    names = [
        c.name for c in collect_lightning_contributions(
            Settings(lightning_enabled=True), None,
        )
    ]
    assert "GOES-GLM" in names


def _router_only_client():
    """A TestClient over just the router — no lifespan, so no live fetch.

    Mirrors ``tests/test_api.py``'s ``_make_test_app``: the full-app
    lifespan would run radar/NWP/lightning network fetches, which the
    endpoint tests neither need nor should trigger.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from librewxr.api import routes

    test_app = FastAPI()
    test_app.include_router(routes.router)
    return TestClient(test_app, raise_server_exceptions=False)


@pytest.mark.api
def test_lightning_endpoint_disabled_returns_503():
    from librewxr.api import routes

    saved = routes.lightning_grids
    routes.lightning_grids = {}
    try:
        client = _router_only_client()
        assert client.get("/v2/lightning").status_code == 503
    finally:
        routes.lightning_grids = saved


@pytest.mark.api
def test_lightning_endpoint_shape_and_bbox_parse():
    from librewxr.api import routes

    now = int(time.time())

    class _Fake:
        name = "GOES-GLM"
        flash_count = 1
        timestamps = [now]

        def flashes_since(self, seconds, bbox=None):
            return [(now - 5, 40.0, -100.0, 1e-14)]

    saved = routes.lightning_grids
    routes.lightning_grids = {"glm_grid": _Fake()}
    try:
        client = _router_only_client()
        r = client.get("/v2/lightning?since=600")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        f = body["flashes"][0]
        assert f["lat"] == 40.0 and f["lon"] == -100.0 and "age" in f
        assert "NOAA" in body["attribution"]
        # Malformed bbox → 400.
        assert client.get("/v2/lightning?bbox=1,2,3").status_code == 400
    finally:
        routes.lightning_grids = saved
