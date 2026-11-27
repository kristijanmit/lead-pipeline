"""Named search areas (`maps.areas` in config.yaml) and bounding boxes.

Shared by the Maps collector (grid mode) and the OSM collector (a bbox query
instead of an area-name lookup, which is ambiguous in markets such as the UAE).
Boxes use gosom's order: "minLat,minLon,maxLat,maxLon" — the same order as
Overpass's (south,west,north,east).
"""

from __future__ import annotations

import re
import unicodedata


def parse_bbox(raw: str) -> tuple[float, float, float, float]:
    """"minLat,minLon,maxLat,maxLon" -> floats, validated. Raises ValueError."""
    try:
        min_lat, min_lon, max_lat, max_lon = (float(x) for x in raw.split(","))
    except ValueError:
        raise ValueError(
            f"bbox {raw!r} must be 4 comma-separated numbers: minLat,minLon,maxLat,maxLon"
        ) from None
    if not (-90 <= min_lat < max_lat <= 90 and -180 <= min_lon < max_lon <= 180):
        raise ValueError(f"bbox {raw!r}: min must be below max (lat -90..90, lon -180..180)")
    return min_lat, min_lon, max_lat, max_lon


def _ascii_key(text: str) -> str:
    text = text.strip().lower().replace("đ", "dj")
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()).strip()


def resolve_bbox(params: dict, areas: dict) -> str | None:
    """The bounding box for this run: an explicit params["bbox"] wins, then the
    configured area for the location; params["grid"] is False -> no grid."""
    if params.get("grid") is False:
        return None
    if params.get("bbox"):
        return params["bbox"]
    wanted = _ascii_key(params.get("location") or "")
    for name, bbox in (areas or {}).items():
        if _ascii_key(str(name)) == wanted:
            return bbox
    return None
