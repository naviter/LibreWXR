# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
from typing import Any

from pydantic import BaseModel


class AlertProperties(BaseModel):
    title: str
    severity: str
    time: int | None
    expires: int | None
    description: str
    regions: list[str]
    uri: str


class GeoJSONFeature(BaseModel):
    type: str = "Feature"
    properties: AlertProperties
    geometry: dict[str, Any] | None


class AlertsResponse(BaseModel):
    type: str = "FeatureCollection"
    features: list[GeoJSONFeature]


class RadarTimestamp(BaseModel):
    time: int
    path: str


class ColorScheme(BaseModel):
    id: int
    name: str


class RadarData(BaseModel):
    past: list[RadarTimestamp]
    nowcast: list[RadarTimestamp]
    colorSchemes: list[ColorScheme]


class SatelliteData(BaseModel):
    infrared: list[RadarTimestamp]


class LightningData(BaseModel):
    time: int
    path: str
    attribution: str


class WeatherMapsResponse(BaseModel):
    version: str
    generated: int
    host: str
    radar: RadarData
    satellite: SatelliteData
    lightning: LightningData | None = None
