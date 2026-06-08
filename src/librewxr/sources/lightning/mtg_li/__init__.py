# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
"""EUMETSAT MTG-LI — self-contained lightning source package.

Lightning over Europe / Africa / South America from the MTG Lightning
Imager, via the EUMETSAT Data Store.  Free with attribution but requires
a EUMETSAT account: the provider returns ``None`` (dormant) until
``eumetsat_key`` / ``eumetsat_secret`` are set.  See ``source.py``.
"""
from __future__ import annotations

import logging

from librewxr.sources._base import LightningContribution

from .source import MTGLILightningSource

__all__ = ["MTGLILightningSource", "lightning_provider"]

logger = logging.getLogger(__name__)


def lightning_provider(settings, cache_dir) -> LightningContribution | None:
    """Return the MTG-LI contribution, or ``None`` when disabled/uncredentialed.

    Two gates: the ``mtg_li_enabled`` toggle, and the presence of both
    EUMDAC credentials.  Missing credentials is the common case (GLM-only
    deployments) and is silent-but-logged once at startup rather than an
    error — the layer simply doesn't appear.
    """
    if not getattr(settings, "mtg_li_enabled", True):
        return None
    if not (getattr(settings, "eumetsat_key", "") and getattr(settings, "eumetsat_secret", "")):
        logger.info(
            "MTG-LI: dormant (set LIBREWXR_EUMETSAT_KEY + _SECRET to enable "
            "European/African lightning coverage)",
        )
        return None
    return LightningContribution(
        instance=MTGLILightningSource(
            retention_minutes=getattr(settings, "lightning_retention_minutes", 30),
        ),
        priority=20,
        name="MTG-LI",
        slug="mtg_li_grid",
    )
