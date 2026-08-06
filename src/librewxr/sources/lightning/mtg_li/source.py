# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
"""EUMETSAT MTG-LI (Meteosat Third Generation Lightning Imager) source.

LI is the first space-based lightning instrument over Europe, Africa and
South America from geostationary orbit (0° sub-satellite point).  EUMETSAT
publishes Level-2 lightning products through the Data Store; we read the
flash-level product and stamp each flash with its event time, then hand
the points to the shared ``FlashStore`` exactly like GLM.

Access model (differs from GLM's anonymous S3):
  1. OAuth2 client-credentials token from ``{base}/token`` using the
     EUMDAC consumer key + secret as HTTP Basic auth.
  2. OpenSearch product search over the LI flash collection, filtered to
     the recent window.
  3. Download each product (NetCDF), decode flash lat/lon/time.

License: free, no fee, attribution to EUMETSAT required (surfaced in the
API payload).  The source stays dormant unless ``eumetsat_key`` and
``eumetsat_secret`` are configured — the provider returns ``None`` then,
so none of this code runs without credentials.

NOTE: this network path is exercised only when credentials are present
and could not be integration-tested without a EUMETSAT account.  It is
written against EUMETSAT's documented EUMDAC token + Data Store
OpenSearch API; treat the variable-name fallbacks below as the most
likely-correct mapping pending a live verification pass.
"""
from __future__ import annotations

import logging
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import ClassVar

import numpy as np

from librewxr.config import settings
from librewxr.sources._helpers import HDF5_LOCK

from .._common import Flash, FlashStore

logger = logging.getLogger(__name__)


class MTGLILightningSource(FlashStore):
    """EUMETSAT MTG-LI flashes over the 0° disk (EU / Africa / S. America)."""

    friendly_name: ClassVar[str] = "MTG-LI"

    def __init__(self, retention_minutes: int | None = None) -> None:
        super().__init__(retention_minutes=retention_minutes)
        self._key = settings.eumetsat_key
        self._secret = settings.eumetsat_secret
        self._base = settings.eumetsat_base_url.rstrip("/")
        self._collection = settings.mtg_li_collection
        self._window_minutes = settings.glm_fetch_window_minutes
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._seen_ids: set[str] = set()

    # ── Auth ──

    def _get_token(self) -> str | None:
        """Return a cached OAuth token, refreshing when within 60 s of expiry."""
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        import requests
        try:
            resp = requests.post(
                f"{self._base}/token",
                auth=(self._key, self._secret),
                data={"grant_type": "client_credentials"},
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            logger.warning("%s: token request failed", self.friendly_name)
            return None
        self._token = payload.get("access_token")
        self._token_expiry = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    # ── Fetch (blocking; runs under FlashStore.fetch's to_thread) ──

    def _fetch_flashes(self) -> list[Flash]:
        token = self._get_token()
        if not token:
            return []
        import requests
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=self._window_minutes)
        search = (
            f"{self._base}/data/search-products/1.0.0/os"
        )
        params = {
            "format": "json",
            "pi": self._collection,
            "dtstart": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dtend": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "si": 0,
            "c": 20,
        }
        try:
            resp = requests.get(search, params=params, timeout=30)
            resp.raise_for_status()
            features = resp.json().get("features", [])
        except Exception:
            logger.warning("%s: product search failed", self.friendly_name)
            return []

        flashes: list[Flash] = []
        headers = {"Authorization": f"Bearer {token}"}
        for feat in features:
            product_id = feat.get("id") or feat.get("properties", {}).get("identifier")
            if not product_id or product_id in self._seen_ids:
                continue
            download_url = self._download_url(feat)
            if not download_url:
                continue
            granule = self._download_and_decode(requests, download_url, headers)
            self._seen_ids.add(product_id)
            flashes.extend(granule)

        if len(self._seen_ids) > 2000:
            self._seen_ids = set(list(self._seen_ids)[-1000:])
        return flashes

    @staticmethod
    def _download_url(feature: dict) -> str | None:
        """Pull the product download href from an OpenSearch feature."""
        props = feature.get("properties", {})
        links = props.get("links", {}) or feature.get("links", {})
        # EUMDAC nests download links under properties.links.data[].href.
        data_links = links.get("data") if isinstance(links, dict) else None
        if isinstance(data_links, list) and data_links:
            href = data_links[0].get("href")
            if href:
                return href
        return None

    def _download_and_decode(self, requests_mod, url: str, headers: dict) -> list[Flash]:
        """Download one LFL product (a ZIP) and decode its BODY NetCDF(s).

        The Data Store ships LFL products as a ZIP bundling quicklooks,
        manifests, and the flash data split across ``…BODY…`` and
        ``…TRAIL…`` NetCDF chunks — only BODY carries the flash vectors.
        """
        import io
        import zipfile
        try:
            resp = requests_mod.get(url, headers=headers, timeout=120)
            resp.raise_for_status()
            content = resp.content
        except Exception:
            logger.exception("%s: download failed", self.friendly_name)
            return []

        flashes: list[Flash] = []
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                body_members = [
                    n for n in zf.namelist()
                    if n.endswith(".nc") and "BODY" in n
                ]
                for member in body_members:
                    with tempfile.NamedTemporaryFile(suffix=".nc") as tmp:
                        tmp.write(zf.read(member))
                        tmp.flush()
                        flashes.extend(self._decode_netcdf(tmp.name))
        except zipfile.BadZipFile:
            logger.warning("%s: download was not a valid ZIP", self.friendly_name)
        return flashes

    # MTG-LI flash_time epoch: "seconds since 2000-01-01 00:00:00.0" UTC.
    _EPOCH_2000 = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()

    def _decode_netcdf(self, path: str) -> list[Flash]:
        import xarray as xr

        # Same HDF5 thread-safety constraint as the GLM decoder — the
        # BODY NetCDF is HDF5, decoded in a to_thread worker alongside
        # OPERA / WRF-SMN / GMGSI parses.  Hold HDF5_LOCK across the lazy
        # reads (_first_var / _flash_times touch the file), release it
        # before the per-flash Python loop so the lock isn't held over
        # tens of thousands of iterations.
        with HDF5_LOCK:
            ds = xr.open_dataset(path, engine="netcdf4", decode_times=False)
            try:
                lat = self._first_var(ds, ("latitude", "flash_lat", "lat"))
                lon = self._first_var(ds, ("longitude", "flash_lon", "lon"))
                if lat is None or lon is None or lat.size == 0:
                    return []
                radiance = self._first_var(ds, ("radiance", "flash_energy"))
                times = self._flash_times(ds, lat.size)
            finally:
                ds.close()

        flashes: list[Flash] = []
        for i in range(lat.size):
            la = float(lat[i])
            lo = float(lon[i])
            if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
                continue
            en = float(radiance[i]) if radiance is not None and i < radiance.size else 0.0
            if not np.isfinite(en):
                en = 0.0
            flashes.append((int(times[i]), la, lo, en))
        return flashes

    @staticmethod
    def _first_var(ds, names: tuple[str, ...]) -> np.ndarray | None:
        for n in names:
            if n in ds.variables:
                return np.asarray(ds[n].values, dtype=np.float64).ravel()
        return None

    def _flash_times(self, ds, n: int) -> np.ndarray:
        """Per-flash unix times from ``flash_time`` (seconds since 2000-01-01).

        Falls back to wall clock if the variable is missing/garbage — a
        few-second error is invisible against the retention window, but a
        NaN would poison the age computation.
        """
        if "flash_time" in ds.variables:
            vals = np.asarray(ds["flash_time"].values, dtype=np.float64).ravel()
            unix = self._EPOCH_2000 + vals
            finite = np.isfinite(unix)
            if finite.any():
                fallback = float(np.nanmedian(unix[finite]))
                return np.where(finite, unix, fallback).astype(np.int64)
        return np.full(n, int(time.time()), dtype=np.int64)
