# Lightning Layer — Implementation Write-up

A detailed account of the work adding a **lightning point-strike layer** to
LibreWXR, described as a delta against the latest master commit
`c8ec397` ("Use Viper HD as the default scheme in example clients").

---

## 1. Summary

LibreWXR previously served three raster data families — radar reflectivity,
NWP precipitation, and satellite imagery — all of which are grids encoded as
tiles. This change adds a **fourth, fundamentally different data type:
lightning**, which is *point* data (individual flashes with lat/lon/energy/time)
rather than a grid. It is served as JSON and rendered on the frontend as one
cross-hair per flash, faded by age.

Two upstream networks feed the layer, merged at query time:

| Network | Coverage | License | Access | Default |
|---------|----------|---------|--------|---------|
| **NOAA GOES-GLM** | Americas | CC0-1.0 public domain (NOAA NODD) | Anonymous AWS S3 | Always on |
| **EUMETSAT MTG-LI** | Europe / Africa / S. America | Free, attribution required | EUMDAC OAuth + Data Store | Dormant until credentials set |

Both licenses permit use in a commercial app's free tier with attribution.
**Blitzortung was evaluated and rejected** — its terms prohibit commercial use
even via a free tier, and restrict raw data to project participants.

### Why these two

GLM is the cleanest possible choice — public-domain, anonymous, and it reuses
the exact same anonymous-S3 access pattern the project already uses for CWA
radar and GMGSI satellite. Its one limitation is coverage (Americas only).
MTG-LI fills the rest of the populated world (Europe/Africa/South America) and
is free with attribution; it only needs a free EUMETSAT account, so it stays
dormant until credentials are configured.

---

## 2. Change statistics

```
 CLAUDE.md                        |   2 +
 examples/leaflet.html            | 110 +++++++++++++++++
 pyproject.toml                   |   4 ++
 src/librewxr/api/routes.py       | 108 ++++++++++++++++-
 src/librewxr/config.py           |  47 +++++++++
 src/librewxr/data/fetcher.py     |  57 +++++++++-
 src/librewxr/data_pipeline.py    |  15 ++
 src/librewxr/main.py             |  27 +++++
 src/librewxr/sources/__init__.py |  57 +++++++--
 src/librewxr/sources/_base.py    |  58 +++++++++
 10 files changed, 479 insertions(+), 6 deletions(-)
```

Plus 7 new files (923 lines):

```
 src/librewxr/sources/lightning/__init__.py        15
 src/librewxr/sources/lightning/_common.py        189
 src/librewxr/sources/lightning/glm/__init__.py    35
 src/librewxr/sources/lightning/glm/source.py     220
 src/librewxr/sources/lightning/mtg_li/__init__.py 46
 src/librewxr/sources/lightning/mtg_li/source.py  224
 tests/test_lightning.py                          194
```

---

## 3. New files

### `sources/lightning/__init__.py`
Namespace package + docstring describing the two networks. Exists so the
discovery walker (`pkgutil.walk_packages`) treats `lightning` as a package
subtree to recurse into.

### `sources/lightning/_common.py` — the shared `FlashStore`
The heart of the layer. Lightning has no grid and no memmap, so this is a
plain in-memory rolling-window store of `Flash` tuples
`(unix_ts, lat, lon, energy)`. Both networks subclass it and implement only
`_fetch_flashes()` (the network + decode half); everything else lives here once:

- **Retention/ingest** (`_ingest`): appends new flashes, trims anything older
  than `lightning_retention_minutes`, and dedups via a `_newest_ts`
  high-water mark so overlapping fetch windows don't double-count.
- **Query** (`flashes_since`): returns flashes from the last *N* seconds,
  optionally clipped to a `(min_lon, min_lat, max_lon, max_lat)` bbox. Linear
  scan — fine for the low-thousands of points a 30-min window holds.
- **Protocol surface**: `fetch()` (runs `_fetch_flashes` in a thread, returns
  `True` on new data — same ergonomics as `SatelliteSource`), `timestamps`
  (distinct fetch-interval slots, for `/health`), `flash_count`, `data_bytes`,
  `close()`.
- **Thread safety**: a `threading.Lock` guards the flash list because `fetch`
  runs in a worker thread while request handlers call `flashes_since` on the
  event loop.
- **Cross-process pickle** (`__getstate__`/`__setstate__`): so the multi-worker
  snapshot machinery (`data_pipeline.py` → render workers) can carry the flash
  window across processes, mirroring how the other source families serialize.

### `sources/lightning/glm/source.py` — NOAA GOES-GLM
- Fans out across every bucket in `glm_s3_buckets` (`noaa-goes19` +
  `noaa-goes18` by default) via anonymous `fsspec` S3, so the API sees one
  "GLM" network spanning the full Americas disk.
- Lists LCFA granule keys under `GLM-L2-LCFA/{YYYY}/{DDD}/{HH}/`, walking the
  hour buckets the fetch window touches, filtering by the filename start token.
- Per-key dedup (`_seen_keys`) avoids re-downloading granules already ingested;
  the set is pruned to a bounded size each fetch.
- Decodes flash vectors (`flash_lat` / `flash_lon` / `flash_energy`) from each
  ~20-second NetCDF granule with xarray, rejecting `±999` fills.
- **Time handling (notable bug found & fixed during testing):** GLM's
  `flash_time_offset_of_first_event` is a sub-20-second *within-granule* offset
  against a packed reference, **not** absolute seconds — decoding it naively
  produced timestamps at the J2000 epoch (year 2000). Flashes are instead
  stamped at the **granule scan-start time parsed from the filename**
  (`_s{YYYYDDDHHMMSSt}`), which is correct to ~20 s — far finer than the
  age-fade cares about and robust against the variable's packing.

### `sources/lightning/mtg_li/source.py` — EUMETSAT MTG-LI
- **EUMDAC access flow** (verified live against the real Data Store):
  1. OAuth2 client-credentials token from `api.eumetsat.int/token` (consumer
     key + secret as HTTP Basic auth), cached until ~60 s before expiry.
  2. OpenSearch product query over collection `EO:EUM:DAT:0691` (the **LFL**
     = Lightning Flashes product; `0690` is LEF events) filtered to the recent
     window.
  3. Download each product — which is a **ZIP** bundling quicklooks, manifests,
     and the flash data split across `…BODY….nc` and `…TRAIL….nc` chunks.
     Only the BODY members carry flash vectors, so the decoder extracts and
     reads those.
- Decodes `latitude` / `longitude` / `radiance` / `flash_time`, where
  `flash_time` is **seconds since 2000-01-01T00:00:00Z** (a fixed epoch, not
  J2000-noon) — converted to unix by adding that epoch offset.
- Per-product dedup via `_seen_ids`.
- Stays entirely inert without credentials (the provider returns `None`), so
  none of this code path runs in a GLM-only deployment.

### `sources/lightning/{glm,mtg_li}/__init__.py` — providers
Each exposes a `lightning_provider(settings, cache_dir)` returning a
`LightningContribution` (or `None`). GLM gates only on `glm_enabled`; MTG-LI
gates on `mtg_li_enabled` **and** the presence of both EUMETSAT credentials
(logging once that it's dormant otherwise). `cache_dir` is accepted for
signature symmetry but unused — lightning is in-memory.

### `tests/test_lightning.py` — 10 offline tests
Covers the `FlashStore` (ingest/retention, since-window, bbox filter,
high-water-mark dedup, pickle round-trip), the GLM filename parser, provider
gating (GLM on/off, MTG-LI dormant-without-creds vs active-with-creds), the
master toggle, and the `/v2/lightning` endpoint (503 when disabled, response
shape, bbox parse → 400 on malformed input). The endpoint tests use a
router-only app (no lifespan) so they don't trigger a live fetch. The network
+ decode halves are verified manually against live buckets, not in CI.

---

## 4. Modified files

### `sources/_base.py` (+58)
- New `LightningSource` Protocol (`name`, `timestamps`, `fetch()`,
  `flashes_since()`, `close()`).
- New `LightningContribution` dataclass (`instance`, `priority`, `name`,
  `slug`) — the return type of every `lightning_provider`.

### `sources/__init__.py` (+57/−6)
- `_collect_providers()` extended to also collect `lightning_provider`
  callables into a new `LIGHTNING_PROVIDERS` list.
- New `lightning_source_slug()` (mirrors `satellite_source_slug`).
- New `collect_lightning_contributions(settings, cache_dir)` — walks providers,
  short-circuits to `[]` when `lightning_enabled` is False, normalizes
  single/list/`None` returns, sorts by priority.

### `config.py` (+47)
New settings: `lightning_enabled`, `glm_enabled`, `mtg_li_enabled`,
`glm_s3_buckets`, `glm_s3_region`, `lightning_retention_minutes` (30),
`glm_fetch_window_minutes` (12), `eumetsat_key`, `eumetsat_secret`,
`eumetsat_base_url`, `mtg_li_collection` (`EO:EUM:DAT:0691`). All standard
`LIBREWXR_*` env vars; credentials default empty (MTG-LI dormant).

### `main.py` (+27)
- Imports `collect_lightning_contributions` + `lightning_source_slug`.
- **Regular lifespan**: collects lightning contributions, logs the
  "Lightning chain: [...]" line, wires `routes.lightning_grids`, passes
  `lightning_contributions=` to `RadarFetcher`.
- **Render-only lifespan** (multi-worker): collects + snapshot-restores
  lightning grids the same way satellite is handled, drops grids absent from
  the snapshot, wires the routes.
- Adds log tags for the new modules (`glm`, `mtg-li`, `lightning`).

### `data/fetcher.py` (+57)
- `RadarFetcher.__init__` accepts `lightning_contributions`.
- New `_lightning_tasks` dict + `_fetch_lightning_background()` — each network
  fetches as its own detached background task (same pattern as satellite), so
  neither gates the radar cycle. Dispatched from `_fetch_auxiliary_grids`,
  fires the cycle-complete hook on new data, and is closed in `stop()`.

### `api/routes.py` (+108)
- New module-level `lightning_grids` dict.
- `/health`: a `lightning` block (per-network loaded/flashes/slots/latest) and
  a `lightning_mb` line in the memory breakdown.
- **New endpoint** `GET /v2/lightning?since=<sec>&bbox=<min_lon,min_lat,max_lon,max_lat>`:
  merges every network's flashes, returns
  `{generated, since, count, attribution, flashes:[{t,lat,lon,energy,age}]}`,
  builds the attribution string per network (NOAA for GLM, © EUMETSAT for
  MTG-LI), 503 when disabled, 400 on malformed bbox.

### `data_pipeline.py` (+15)
Collects lightning contributions, logs the chain, folds the lightning grids
into the cross-worker `stores` snapshot, and passes them to the pipeline's
`RadarFetcher` — so the multi-worker deployment has full parity with single
mode.

### `examples/leaflet.html` (+110)
- New toolbar **"Lightning: On/Off"** dropdown (independent of the radar
  scrubber).
- A dedicated `lightning-pane` (z-index above radar, pointer-events off).
- `pollLightning()` queries `/v2/lightning?since=600&bbox=<viewport>` every
  20 s and on `moveend`; `drawFlashes()` renders one SVG cross-hair `divIcon`
  per flash with amber→cooling color and age-based opacity, capped at 4000
  markers (freshest kept) for responsiveness on continental views.

### `pyproject.toml` (+4)
Adds `requests>=2.31` (used by the MTG-LI EUMDAC path; GLM uses `s3fs`).

### `CLAUDE.md` (+2)
A "Lightning" architecture bullet documenting the endpoint, both networks,
their licenses/access, the shared `FlashStore`, and the time-stamping quirks.

---

## 5. Architecture notes & decisions

- **Zero per-source plumbing.** The layer plugs into the existing
  auto-discovery registry exactly like radar/NWP/satellite: drop a package with
  a `lightning_provider` under `sources/`, and the walker + collectors pick it
  up. `main.py`, `fetcher.py`, `data_pipeline.py`, and `routes.py` iterate the
  contribution list, so adding a third network needs no edits to those files.
- **Point data, not tiles.** Rendering individual cross-hairs (rather than a
  density heatmap tile) was the chosen visual. This keeps the layer off the
  tile pipeline entirely — it's a lightweight JSON endpoint plus a frontend
  marker layer, independent of the radar/satellite scrubber.
- **Detached fetch.** Both networks fetch as background tasks so a slow S3 list
  or EUMDAC download never stalls the 10-minute radar cycle.
- **Credentials.** The EUMETSAT key/secret live in the gitignored `.env`
  (`LIBREWXR_EUMETSAT_KEY` / `_SECRET`) and are never committed.

---

## 6. Verification

Validated **live** during development:

- GLM: listing + decode against `noaa-goes19`/`noaa-goes18` returned thousands
  of real flashes with valid coordinates; the J2000 timestamp bug was caught
  and fixed here.
- MTG-LI: full EUMDAC flow against the real Data Store with the provided
  credentials — token → OpenSearch → ZIP download → BODY decode — returned
  ~11k flashes spanning Europe/Africa/South America; collection ID and the
  seconds-since-2000 epoch were confirmed from the live product.
- End-to-end on the running server: `/health` shows both networks loaded;
  `/v2/lightning` returns thousands of flashes globally with correct
  attribution; bbox filters return the expected regional subsets; the Leaflet
  viewer draws cross-hairs.

Automated:

- `tests/test_lightning.py`: **10 passed**.
- Full `sources` + `api` markers: **263 passed, 0 failed** (253 baseline + 10
  new).

### Known caveats
- **GLM time precision** is granule-level (~20 s), not per-flash — deliberate,
  invisible for age-fade.
- Running pytest over this layer can emit a **cosmetic** netCDF4/h5py crash
  dump at interpreter *exit* — after tests pass (exit 0); it does not affect
  the running server.
- MTG-LI's network path is exercised only with credentials present and was
  validated by hand, not in CI.
