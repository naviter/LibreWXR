# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
"""Lightning source family — geostationary optical lightning mappers.

Each subpackage exposes a ``lightning_provider(settings, cache_dir)``
returning a ``LightningContribution`` (or ``None`` when disabled / not
credentialed).  The discovery walker in ``librewxr.sources.__init__``
collects them; ``main.py`` / ``fetcher.py`` drive the fetch cycle and
``api/routes.py`` serves the merged flashes as JSON at ``/v2/lightning``.

Networks:
  - ``glm``    NOAA GOES-GLM, Americas, CC0 public domain (always on).
  - ``mtg_li`` EUMETSAT MTG-LI, Europe / Africa / S. America, free with
               attribution + a EUMETSAT account (dormant without one).
"""
