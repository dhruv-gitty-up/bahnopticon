"""Small, disk-cached German national and state outlines for the map."""

import asyncio
import logging

import httpx
from shapely.geometry import shape

from osm_client import DATA_DIR, load_feature_cache, save_json_cache

logger = logging.getLogger(__name__)
BORDERS_CACHE_FILE = DATA_DIR / "borders_cache.geojson"
# Pinned geoBoundaries release, sourced from Germany's Federal Agency for
# Cartography and Geodesy. ADM0/ADM1 correspond to OSM admin levels 2/4.
SOURCE_URLS = {
    "2": "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/DEU/ADM0/geoBoundaries-DEU-ADM0_simplified.geojson",
    "4": "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/DEU/ADM1/geoBoundaries-DEU-ADM1_simplified.geojson",
}


def border_features(level: str, collection: dict) -> list[dict]:
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise ValueError(f"Invalid ADM{level} border source")
    rows = collection.get("features")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"Empty ADM{level} border source")
    features = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("geometry"), dict):
            continue
        properties = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        geometry = shape(row["geometry"]).simplify(0.01, preserve_topology=True)
        polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
        rings = [
            [[float(longitude), float(latitude)] for longitude, latitude in polygon.exterior.coords]
            for polygon in polygons if polygon.geom_type == "Polygon" and not polygon.is_empty
        ]
        if not rings:
            continue
        features.append({
            "type": "Feature",
            "id": properties.get("shapeID") or f"DEU-{level}-{index}",
            "properties": {
                "admin_level": level,
                "name": properties.get("shapeName") or "Germany",
                "source": "geoBoundaries / BKG",
            },
            "geometry": {"type": "MultiLineString", "coordinates": rings},
        })
    if not features:
        raise ValueError(f"ADM{level} border source has no usable outlines")
    return features


def combine_border_sources(country: dict, states: dict) -> dict:
    return {"type": "FeatureCollection", "features": (
        border_features("2", country) + border_features("4", states)
    )}


async def get_cached_borders() -> dict:
    """Use the checked-in disk cache, downloading pinned source files if absent."""
    cached = await asyncio.to_thread(load_feature_cache, BORDERS_CACHE_FILE)
    if cached:
        return cached
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        responses = await asyncio.gather(*(client.get(url) for url in SOURCE_URLS.values()))
        for response in responses:
            response.raise_for_status()
        sources = await asyncio.gather(*(asyncio.to_thread(response.json) for response in responses))
    collection = await asyncio.to_thread(combine_border_sources, *sources)
    await asyncio.to_thread(save_json_cache, BORDERS_CACHE_FILE, collection)
    return collection
