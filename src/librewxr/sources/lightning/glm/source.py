# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
"""NOAA GOES-GLM (Geostationary Lightning Mapper) lightning source.

GLM is the first operational geostationary optical lightning detector —
it watches the full disk continuously and reports *total* lightning
(in-cloud + cloud-to-ground).  NOAA publishes the Level-2 LCFA product
(Lightning Cluster-Filter Algorithm: events → groups → flashes) as one
~20-second NetCDF granule per satellite, anonymously on AWS Open Data:

    s3://noaa-goes19/GLM-L2-LCFA/{YYYY}/{DDD}/{HH}/
        OR_GLM-L2-LCFA_G19_s{YYYYDDDHHMMSSt}_e{...}_c{...}.nc

GOES-East (GOES-19 since 2025) covers the Americas + Atlantic; GOES-West
(GOES-18) covers the eastern Pacific + western Americas.  We read just
the flash-level vectors (``flash_lat`` / ``flash_lon`` / ``flash_energy``
/ ``flash_time_offset_of_first_event``) and stamp every flash with its
event time, then hand the points to the shared ``FlashStore``.

License: CC0-1.0 public domain via NOAA's Open Data Dissemination
Program — no restriction on commercial or free-tier use.  Attribution to
NOAA/NESDIS is requested (not required) and surfaced in the API payload.
"""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta, timezone
from typing import ClassVar

import fsspec
import numpy as np
import xarray as xr

from librewxr.config import settings
from librewxr.sources._helpers import HDF5_LOCK

from .._common import Flash, FlashStore

logger = logging.getLogger(__name__)

# GLM Level-2 LCFA product path under each GOES bucket.
_PRODUCT_PATH = "GLM-L2-LCFA"


class GLMLightningSource(FlashStore):
    """One or more GOES-GLM satellites, merged into a single flash window.

    A single instance fans out across every bucket in
    ``settings.glm_s3_buckets`` (GOES-East + GOES-West by default) so the
    API sees one "GLM" network spanning the full Americas disk rather
    than two half-disks the frontend would have to stitch.
    """

    friendly_name: ClassVar[str] = "GOES-GLM"

    def __init__(self, retention_minutes: int | None = None) -> None:
        super().__init__(retention_minutes=retention_minutes)
        self._buckets = [
            b.strip() for b in settings.glm_s3_buckets.split(",") if b.strip()
        ]
        self._window_minutes = settings.glm_fetch_window_minutes
        self._fs: fsspec.AbstractFileSystem | None = None
        # Remember granule keys already ingested so overlapping fetch
        # windows don't re-download + re-append the same flashes.  Bounded
        # by trimming to the retention window's worth of keys each fetch.
        self._seen_keys: set[str] = set()

    def _get_fs(self) -> fsspec.AbstractFileSystem:
        if self._fs is None:
            self._fs = fsspec.filesystem("s3", anon=True)
        return self._fs

    # ── Fetch (blocking; runs under FlashStore.fetch's to_thread) ──

    def _fetch_flashes(self) -> list[Flash]:
        if not self._buckets:
            return []
        fs = self._get_fs()
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=self._window_minutes)

        flashes: list[Flash] = []
        fresh_keys: list[str] = []
        for bucket in self._buckets:
            try:
                keys = self._list_granule_keys(fs, bucket, window_start, now)
            except Exception:
                logger.warning("%s: listing failed for %s", self.friendly_name, bucket)
                continue
            for key in keys:
                if key in self._seen_keys:
                    continue
                granule = self._download_and_decode(fs, key)
                fresh_keys.append(key)
                if granule:
                    flashes.extend(granule)

        # Mark the freshly-listed keys seen, then prune the set back to a
        # bounded size (anything outside the retention window can't recur).
        self._seen_keys.update(fresh_keys)
        if len(self._seen_keys) > 4000:
            # Keep the most recent ~2000 keys; granule keys sort by time.
            self._seen_keys = set(sorted(self._seen_keys)[-2000:])
        return flashes

    def _list_granule_keys(
        self,
        fs: fsspec.AbstractFileSystem,
        bucket: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[str]:
        """List LCFA granule keys in the window for one bucket.

        GLM lays granules under day-of-year + hour directories.  We walk
        each hour bucket the window touches (usually one, occasionally
        two across an hour boundary) and filter by the filename's start
        token so we don't pull granules older than the window.
        """
        results: list[str] = []
        cursor = window_start.replace(minute=0, second=0, microsecond=0)
        while cursor <= window_end:
            doy = cursor.timetuple().tm_yday
            prefix = (
                f"{bucket}/{_PRODUCT_PATH}/"
                f"{cursor.year:04d}/{doy:03d}/{cursor.hour:02d}/"
            )
            try:
                entries = fs.ls(prefix, detail=False)
            except FileNotFoundError:
                entries = []
            for entry in entries:
                name = entry.rsplit("/", 1)[-1]
                if not name.startswith("OR_GLM-L2-LCFA"):
                    continue
                start = self._parse_start_time(name)
                if start is None or start < window_start or start > window_end:
                    continue
                results.append(entry)
            cursor += timedelta(hours=1)
        return sorted(results)

    @staticmethod
    def _parse_start_time(filename: str) -> datetime | None:
        """Parse the ``_s{YYYYDDDHHMMSSt}`` token to a UTC datetime.

        GLM filenames stamp the scan start as year + day-of-year + time
        with a trailing tenths-of-second digit, e.g. ``s20261590600000``.
        """
        try:
            tok = filename.split("_s", 1)[1].split("_", 1)[0]
        except IndexError:
            return None
        if len(tok) < 13:
            return None
        try:
            yr = int(tok[0:4])
            doy = int(tok[4:7])
            hh = int(tok[7:9])
            mm = int(tok[9:11])
            ss = int(tok[11:13])
        except ValueError:
            return None
        base = datetime(yr, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
        return base.replace(hour=hh, minute=mm, second=ss)

    def _download_and_decode(
        self, fs: fsspec.AbstractFileSystem, key: str,
    ) -> list[Flash]:
        """Pull one granule and return its flashes as ``Flash`` tuples.

        Flashes are stamped at the granule's scan-start time (parsed from
        the filename).  GLM's per-flash time-offset variable is a sub-20 s
        within-granule offset against a packed reference, not an absolute
        epoch — far finer than the cross-hair fade cares about — so the
        granule start is both correct enough and robust against the
        variable's scale/offset packing.
        """
        name = key.rsplit("/", 1)[-1]
        start = self._parse_start_time(name)
        if start is None:
            return []
        granule_ts = int(start.timestamp())
        try:
            with tempfile.NamedTemporaryFile(suffix=".nc") as tmp:
                fs.get(key, tmp.name)
                return self._decode_netcdf(tmp.name, granule_ts)
        except Exception:
            logger.exception("%s: decode failed for %s", self.friendly_name, key)
            return []

    def _decode_netcdf(self, path: str, granule_ts: int) -> list[Flash]:
        # LCFA granules are HDF5 under the hood, and every GLM decode runs
        # in an asyncio.to_thread worker — so this has to take HDF5_LOCK
        # like OPERA / WRF-SMN / GMGSI do.  Without it a granule decode can
        # land inside libhdf5 at the same instant as a radar parse and lose
        # the race ("NetCDF: HDF error" at best, a segfault at worst).  The
        # lock must span the lazy .values reads too, not just the open.
        with HDF5_LOCK:
            ds = xr.open_dataset(path, engine="netcdf4", decode_times=False)
            try:
                if "flash_lat" not in ds.variables or "flash_lon" not in ds.variables:
                    return []
                lat = np.asarray(ds["flash_lat"].values, dtype=np.float64)
                lon = np.asarray(ds["flash_lon"].values, dtype=np.float64)
                if lat.size == 0:
                    return []
                energy = (
                    np.asarray(ds["flash_energy"].values, dtype=np.float64)
                    if "flash_energy" in ds.variables
                    else np.zeros(lat.shape, dtype=np.float64)
                )
            finally:
                ds.close()

        flashes: list[Flash] = []
        for i in range(lat.size):
            la = float(lat[i])
            lo = float(lon[i])
            # Reject fill / out-of-range (GLM uses ±999 fills).
            if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
                continue
            en = float(energy[i]) if i < energy.size else 0.0
            if not np.isfinite(en):
                en = 0.0
            flashes.append((granule_ts, la, lo, en))
        return flashes
