"""Resolve activity start provinces offline with the dashboard's own boundaries."""

import ast
import json
import math
from functools import lru_cache
from pathlib import Path

import polyline

BOUNDARIES = (
    Path(__file__).resolve().parents[1]
    / "src/components/Dashboard/assets/china-provinces.json"
)


@lru_cache(maxsize=1)
def province_boundaries():
    regions = []
    data = json.loads(BOUNDARIES.read_text(encoding="utf-8"))
    for feature in data["features"]:
        name = feature["properties"].get("name")
        if not name:
            continue  # The unnamed dashed maritime outline is not a province.
        geometry = feature["geometry"]
        polygons = (
            [geometry["coordinates"]]
            if geometry["type"] == "Polygon"
            else geometry["coordinates"]
        )
        for polygon in polygons:
            outer = polygon[0]
            bbox = (
                min(p[0] for p in outer),
                min(p[1] for p in outer),
                max(p[0] for p in outer),
                max(p[1] for p in outer),
            )
            regions.append((name, bbox, polygon))
    return regions


def in_ring(lon, lat, ring):
    inside = False
    for index, (x1, y1, *_rest) in enumerate(ring):
        x2, y2 = ring[index - 1][:2]
        cross = (lon - x1) * (y2 - y1) - (lat - y1) * (x2 - x1)
        if (
            abs(cross) < 1e-10
            and min(x1, x2) <= lon <= max(x1, x2)
            and min(y1, y2) <= lat <= max(y1, y2)
        ):
            return True
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


@lru_cache(maxsize=8192)
def province_at(lat, lon):
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    for name, (west, south, east, north), polygon in province_boundaries():
        if not (west <= lon <= east and south <= lat <= north):
            continue
        if in_ring(lon, lat, polygon[0]) and not any(
            in_ring(lon, lat, hole) for hole in polygon[1:]
        ):
            return name
    return None


def province_from_location(location):
    if not location or location == "None":
        return None
    if location.lstrip().startswith("{"):
        try:
            data = json.loads(location)
        except (ValueError, TypeError):
            try:
                data = ast.literal_eval(location)
            except (SyntaxError, ValueError):
                return None
        if not isinstance(data, dict) or data.get("country") not in {
            "中国",
            "China",
            "中华人民共和国",
        }:
            return None
        location = str(data.get("province") or "")
    for name in dict.fromkeys(region[0] for region in province_boundaries()):
        if name in location:
            return name
    return None


def activity_province(location, summary_polyline):
    province = province_from_location(location)
    if province:
        return province
    if not summary_polyline:
        return None
    try:
        points = polyline.decode(summary_polyline)
    except (ValueError, TypeError, IndexError):
        return None
    if not points:
        return None
    lat, lon = points[0]
    return province_at(lat, lon)
