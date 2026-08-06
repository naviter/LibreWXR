# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
"""NOAA GOES-GLM — self-contained lightning source package.

Total lightning over the Americas from the Geostationary Lightning
Mapper, anonymous NOAA Open Data S3, CC0 public domain.  See
``source.py`` for the LCFA granule layout and decode.
"""
from __future__ import annotations

from librewxr.sources._base import LightningContribution

from .source import GLMLightningSource

__all__ = ["GLMLightningSource", "lightning_provider"]


def lightning_provider(settings, cache_dir) -> LightningContribution | None:
    """Return the GLM contribution, or ``None`` when disabled.

    GLM needs no credentials — the buckets are anonymous — so the only
    gate is ``glm_enabled``.  ``cache_dir`` is accepted for signature
    symmetry with the satellite provider but unused: lightning is point
    data held in memory, not memmapped frames.
    """
    if not getattr(settings, "glm_enabled", True):
        return None
    return LightningContribution(
        instance=GLMLightningSource(
            retention_minutes=getattr(settings, "lightning_retention_minutes", 30),
        ),
        priority=10,
        name="GOES-GLM",
        slug="glm_grid",
    )
