# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
"""Shared in-memory flash store for lightning sources.

Lightning is point data — a flash is ``(unix_ts, lat, lon, energy)`` —
so unlike the radar / NWP / satellite families there is no grid and no
memmap.  ``FlashStore`` keeps a rolling window of recent flashes in a
plain list, trims by age on every ingest, and answers the two queries
the API needs: ``flashes_since`` (the live cross-hair feed) and
``timestamps`` (distinct fetch-interval slots, for ``/health``).

Concrete sources (GLM, MTG-LI) subclass this and implement only
``_fetch_flashes`` — the network + decode half — while retention,
querying, the SatelliteSource-shaped ``fetch``/``close`` surface, and
cross-process pickling live here once.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from librewxr.config import settings

logger = logging.getLogger(__name__)

# A single flash: (unix_seconds, latitude, longitude, energy_joules).
Flash = tuple[int, float, float, float]


class FlashStore:
    """Rolling-window store + fetch surface for one lightning network.

    Subclasses set ``friendly_name`` and implement ``_fetch_flashes`` to
    return a list of ``Flash`` tuples for the recent window.  Everything
    else — dedup-free append, age trim, thread-safe query, the async
    ``fetch``/``close`` protocol surface — is inherited.
    """

    friendly_name: str = "lightning"

    def __init__(self, retention_minutes: int | None = None) -> None:
        self.name = self.friendly_name
        self._flashes: list[Flash] = []
        # Guards _flashes: fetch runs in a worker thread (via to_thread)
        # while request handlers call flashes_since on the event loop.
        self._lock = threading.Lock()
        self._retention_seconds = (
            (retention_minutes if retention_minutes is not None
             else settings.lightning_retention_minutes) * 60
        )
        # High-water mark of the newest flash time we've ingested, so
        # overlapping fetch windows don't re-append already-seen granules'
        # flashes.  Subclasses that dedup by granule key may ignore this.
        self._newest_ts: int = 0

    # ── Public state (LightningSource protocol) ──

    @property
    def timestamps(self) -> list[int]:
        """Distinct fetch-interval slots that currently hold flashes.

        Purely for the ``/health`` payload and catalog — the live query
        path is ``flashes_since``.  Floors each flash time to the radar
        fetch_interval so the slot list aligns with the radar timeline.
        """
        interval = max(settings.fetch_interval, 1)
        with self._lock:
            slots = {(ts // interval) * interval for ts, _, _, _ in self._flashes}
        return sorted(slots)

    @property
    def flash_count(self) -> int:
        with self._lock:
            return len(self._flashes)

    @property
    def data_bytes(self) -> int:
        """Approximate resident bytes (for the /health breakdown).

        Each flash is a 4-tuple of Python scalars; ~120 bytes is a fair
        rule-of-thumb for the tuple + boxed float/int members on CPython.
        """
        return self.flash_count * 120

    # ── Query (renderer / API hook) ──

    def flashes_since(
        self,
        seconds: int,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> list[Flash]:
        """Return flashes from the last ``seconds``, optionally within bbox.

        ``bbox`` is ``(min_lon, min_lat, max_lon, max_lat)``.  Linear scan
        — fine for the low-thousands of points a 30-min window holds.
        Results are newest-last (ingest order is roughly chronological).
        """
        import time
        cutoff = int(time.time()) - max(seconds, 0)
        with self._lock:
            snapshot = list(self._flashes)
        out: list[Flash] = []
        if bbox is not None:
            min_lon, min_lat, max_lon, max_lat = bbox
            for f in snapshot:
                ts, lat, lon, _ = f
                if ts < cutoff:
                    continue
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                    out.append(f)
        else:
            out = [f for f in snapshot if f[0] >= cutoff]
        return out

    # ── Ingest + retention ──

    def _ingest(self, flashes: list[Flash]) -> int:
        """Append new flashes, trim by age, return count actually added.

        Drops flashes at or before ``_newest_ts`` so re-fetched granule
        overlap doesn't double-count.  Subclasses that can't rely on a
        monotonic time cursor (out-of-order publication) should dedup
        before calling and pass only genuinely-new flashes.
        """
        import time
        if not flashes:
            return 0
        cutoff = int(time.time()) - self._retention_seconds
        added = 0
        with self._lock:
            for f in flashes:
                ts = f[0]
                if ts <= self._newest_ts or ts < cutoff:
                    continue
                self._flashes.append(f)
                added += 1
            if added:
                # Recompute the high-water mark from the freshly added set.
                self._newest_ts = max(self._newest_ts, max(f[0] for f in flashes))
                # Age-trim in place.
                self._flashes = [f for f in self._flashes if f[0] >= cutoff]
                self._flashes.sort(key=lambda f: f[0])
        return added

    # ── Fetch / lifecycle (LightningSource protocol) ──

    async def fetch(self) -> bool:
        """Fetch the recent window in a thread; True if new flashes landed."""
        try:
            flashes = await asyncio.to_thread(self._fetch_flashes)
        except Exception:
            logger.exception("%s: fetch failed", self.friendly_name)
            return False
        added = self._ingest(flashes)
        if added:
            logger.info(
                "%s: ingested %d new flash(es); window holds %d",
                self.friendly_name, added, self.flash_count,
            )
        return added > 0

    def _fetch_flashes(self) -> list[Flash]:
        """Subclass hook: return recent flashes (network + decode). Blocking."""
        raise NotImplementedError

    async def close(self) -> None:
        return None

    # ── Cross-process snapshot (pickle for multi-worker) ──

    def __getstate__(self) -> dict:
        with self._lock:
            flashes = list(self._flashes)
            newest = self._newest_ts
        return {
            "flashes": flashes,
            "newest_ts": newest,
            "retention_seconds": self._retention_seconds,
        }

    def __setstate__(self, state: dict) -> None:
        self._lock = threading.Lock()
        self._flashes = list(state.get("flashes", []))
        self._newest_ts = state.get("newest_ts", 0)
        self._retention_seconds = state.get(
            "retention_seconds", settings.lightning_retention_minutes * 60,
        )
        self.name = self.friendly_name
