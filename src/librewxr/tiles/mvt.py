# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
"""Minimal hand-rolled Mapbox Vector Tile (MVT) encoder — points only.

No runtime dependencies beyond stdlib.  Produces a valid MVT 2.x tile with
one layer containing POINT features.  Each feature carries a single ``t``
attribute (unix timestamp as sint64 varint, field 4 of Value).

Wire-format reference: https://github.com/mapbox/vector-tile-spec/blob/master/2.1/vector_tile.proto
"""
import math

LIGHTNING_LAYER = "lightning"  # source-layer name shared with the client


def _varint(v: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
    out = []
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def _zigzag(v: int) -> int:
    """Zigzag-encode a signed integer for sint32/sint64."""
    return (v << 1) ^ (v >> 63)


def _string_field(field: int, s: str) -> bytes:
    b = s.encode()
    return _tag(field, 2) + _varint(len(b)) + b


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _len_delimited(field: int, data: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(data)) + data


def _packed_varints(field: int, values: list[int]) -> bytes:
    body = b"".join(_varint(v) for v in values)
    return _len_delimited(field, body)


def _encode_value(t: int) -> bytes:
    """Encode Value message with int64_value = field 4 varint.

    Field 4 is ``int64``, NOT ``sint64`` — plain varint encoding, no zigzag.
    (Zigzag is only for ``sint32``/``sint64`` field types, field 6 in Value.)
    """
    inner = _tag(4, 0) + _varint(t)
    return _len_delimited(4, inner)


def _encode_feature(px: int, py: int, key_idx: int, val_idx: int) -> bytes:
    """Encode a POINT Feature.

    Geometry: MoveTo(count=1), dx=px, dy=py (all zigzag sint32).
    The per-feature cursor starts at (0,0) so dx/dy equal px/py.
    """
    # tags: packed pair (key_idx, val_idx)
    tags = _packed_varints(2, [key_idx, val_idx])
    # type = 1 (POINT)
    feature_type = _tag(3, 0) + _varint(1)
    # geometry: MoveTo cmd (id=1, count=1) + zigzag(px) + zigzag(py)
    # cmd = (count << 3) | id = (1 << 3) | 1 = 9
    geom = _packed_varints(4, [9, _zigzag(px), _zigzag(py)])
    inner = tags + feature_type + geom
    return _len_delimited(2, inner)


def encode_point_tile(
    layer_name: str,
    points: list[tuple[int, int, int]],
    extent: int = 4096,
) -> bytes:
    """Encode a list of (px, py, t) points into a one-layer MVT tile.

    Returns ``b""`` for an empty point list — a valid zero-feature tile
    that nginx will cache correctly (unlike a 204).

    ``px``/``py`` must already be in ``[0, extent)`` (or slightly outside
    for buffer pixels — the spec allows out-of-extent coordinates).
    ``t`` is stored as the ``t`` attribute on every feature.
    """
    if not points:
        return b""

    # Build deduplicated values table (t values, deduped at 1 s resolution).
    unique_t: list[int] = []
    t_to_idx: dict[int, int] = {}
    for _, _, t in points:
        if t not in t_to_idx:
            t_to_idx[t] = len(unique_t)
            unique_t.append(t)

    # Encode keys: just ["t"]
    keys_bytes = _string_field(3, "t")

    # Encode values
    values_bytes = b"".join(_encode_value(t) for t in unique_t)

    # Encode features
    features_bytes = b"".join(
        _encode_feature(px, py, 0, t_to_idx[t]) for px, py, t in points
    )

    # Assemble layer
    layer = (
        _tag(15, 0) + _varint(2)            # version = 2
        + _string_field(1, layer_name)       # name
        + features_bytes                      # features (field 2, repeated)
        + keys_bytes                          # keys (field 3, repeated string)
        + values_bytes                        # values (field 4, repeated Value)
        + _tag(5, 0) + _varint(extent)        # extent
    )

    # Wrap in Tile (field 3 = layers)
    return _len_delimited(3, layer)


# ── Mercator helpers ──────────────────────────────────────────────────────────

def merc_y(lat_deg: float) -> float:
    """Web Mercator Y = atanh(sin(lat)) — non-linear in latitude."""
    lat_rad = math.radians(lat_deg)
    return math.atanh(math.sin(lat_rad))


def quantize_point(
    lat: float,
    lon: float,
    west: float,
    east: float,
    north_merc: float,
    south_merc: float,
    extent: int = 4096,
) -> tuple[int, int]:
    """Convert (lat, lon) to MVT pixel coordinates within a tile.

    Uses Mercator Y (not linear lat) to match MapLibre's rendering.
    Returns pixel coords; may be slightly outside [0, extent) for buffer
    points — the MVT spec permits this.
    """
    px = int((lon - west) / (east - west) * extent)
    my = merc_y(lat)
    py = int((north_merc - my) / (north_merc - south_merc) * extent)
    return px, py
