"""Overpass queries and conversion for the nationwide rail viewport."""

import asyncio
import json
import logging
import math
import os
from pathlib import Path
from typing import Dict

import httpx

logger = logging.getLogger(__name__)
DEFAULT_OVERPASS_ENDPOINT = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"

# Development fallback for the Berlin–Leipzig corridor. The nationwide query
# remains authoritative; this smaller box keeps the rendering pipeline usable
# when a public Overpass instance cannot finish the Germany-wide request.
TRACK_FALLBACK_BBOX = {"north": 53.0, "south": 51.8, "west": 12.0, "east": 14.7}
DATA_DIR = Path(os.getenv("BAHNOPTICON_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
TRACKS_CACHE_FILE = DATA_DIR / "tracks_cache.geojson"
TRACKS_NATIONWIDE_CACHE_FILE = DATA_DIR / "tracks_cache_nationwide.geojson"
TRACKS_OVERPASS_CACHE_FILE = DATA_DIR / "tracks_cache.overpass.json"
TRACK_QUADRANT_CACHE_DIR = DATA_DIR / "track_quadrants"
STATIONS_CACHE_FILE = DATA_DIR / "stations_cache.geojson"


def load_json_cache(path: Path) -> Dict | None:
    """Return a valid cached JSON object, ignoring absent or damaged files."""
    try:
        payload = json.loads(path.read_bytes())
        return payload if isinstance(payload, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("Could not load geometry cache %s: %s", path, error)
        return None


def save_json_cache(path: Path, payload: Dict | bytes) -> None:
    """Write a complete cache atomically so a crash cannot leave partial JSON."""
    data = payload if isinstance(payload, bytes) else json.dumps(
        payload, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def load_feature_cache(path: Path) -> Dict | None:
    collection = load_json_cache(path)
    if (collection and collection.get("type") == "FeatureCollection"
            and isinstance(collection.get("features"), list)
            and collection["features"]):
        return collection
    return None


def track_features_to_osm(collection: Dict) -> Dict:
    """Rebuild a usable graph from a GeoJSON-only cache made by an older run."""
    elements = []
    for feature in collection.get("features", []):
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        properties = feature.get("properties")
        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            continue
        if not isinstance(properties, dict):
            continue
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            vertices = [{"lon": float(point[0]), "lat": float(point[1])} for point in coordinates]
        except (TypeError, ValueError, IndexError):
            continue
        feature_id = feature.get("id")
        way_id = int(feature_id.removeprefix("osm:way:")) if (
            isinstance(feature_id, str) and feature_id.startswith("osm:way:")
            and feature_id.removeprefix("osm:way:").isdigit()
        ) else len(elements) + 1
        elements.append({
            "type": "way", "id": way_id, "geometry": vertices,
            "tags": {key: value for key, value in properties.items()
                     if isinstance(key, str) and isinstance(value, str)},
        })
    return {"elements": elements}


def is_important_station(name: str) -> bool:
    return any(word in name.casefold() for word in ("hbf", "hauptbahnhof", "centraal"))


def mark_important_stations(collection: Dict) -> Dict:
    """Upgrade older station caches that predate the importance property."""
    for feature in collection.get("features", []):
        if isinstance(feature, dict) and isinstance(feature.get("properties"), dict):
            properties = feature["properties"]
            name = properties.get("name")
            properties["is_important"] = is_important_station(name) if isinstance(name, str) else False
    return collection


def track_product(tags: Dict) -> str:
    """Express a railway way using the same product names as the vehicle stream.

    The Overpass track query selects railways only, so it does not yield bus
    geometry. Untagged fixture ways fall back to regional.
    """
    railway = tags.get("railway")
    if railway == "subway":
        return "subway"
    if railway == "light_rail":
        return "suburban"
    if railway == "rail" and (tags.get("highspeed") == "yes" or tags.get("usage") == "main"):
        return "nationalExpress"
    return "regional"


class OsmClient:
    def __init__(self, endpoint: str = DEFAULT_OVERPASS_ENDPOINT):
        self.endpoint = endpoint
        self.client = httpx.AsyncClient(timeout=120.0, headers={"User-Agent": "BahnOpticon/1.0"})
        # Avoid competing heavy queries against the same public Overpass instance.
        self._request_lock = asyncio.Lock()

    async def _query(self, query: str, *, timeout: float | None = None) -> Dict:
        try:
            async with self._request_lock:
                response = await self.client.post(
                    self.endpoint, data={"data": query}, timeout=timeout or 120.0,
                )
                response.raise_for_status()
                return await asyncio.to_thread(response.json)
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("Overpass API request failed: %s", error)
            return {}

    async def _get_track_quadrant(self, north: float, west: float,
                                  south: float, east: float) -> Dict:
        """Fetch one bounded set of high-speed and mainline rail ways."""
        # Overpass expects bounding box as (south, west, north, east)
        bbox = f"{south},{west},{north},{east}"
        query = f"""
        [out:json][timeout:60];
        (
          way["railway"="rail"]["highspeed"="yes"]({bbox});
          way["railway"="rail"]["usage"="main"]({bbox});
        );
        out body geom;
        """
        return await self._query(query, timeout=60.0)

    async def get_track_geometries(self, north: float, west: float, south: float,
                                   east: float, *, chunked: bool = True,
                                   cache_quadrants: bool = False) -> Dict:
        """Fetch four quadrants in order, merging duplicate ways at their edges."""
        if not chunked:
            return await self._get_track_quadrant(north, west, south, east)
        middle_latitude = (north + south) / 2
        middle_longitude = (west + east) / 2
        quadrants = (
            ("nw", (north, west, middle_latitude, middle_longitude)),
            ("ne", (north, middle_longitude, middle_latitude, east)),
            ("sw", (middle_latitude, west, south, middle_longitude)),
            ("se", (middle_latitude, middle_longitude, south, east)),
        )
        ways = {}
        for index, (label, bounds) in enumerate(quadrants, start=1):
            cache_file = TRACK_QUADRANT_CACHE_DIR / f"{label}.overpass.json"
            payload = None
            if cache_quadrants:
                cached = await asyncio.to_thread(load_json_cache, cache_file)
                if cached and cached.get("requested_bbox") == list(bounds) \
                        and isinstance(cached.get("elements"), list) \
                        and cached["elements"]:
                    payload = cached
                    logger.info("Restored Overpass track quadrant %d/4 from disk", index)
            if payload is None:
                payload = await self._get_track_quadrant(*bounds)
                if cache_quadrants and isinstance(payload.get("elements"), list) \
                        and payload["elements"]:
                    try:
                        await asyncio.to_thread(save_json_cache, cache_file, {
                            **payload, "requested_bbox": list(bounds),
                        })
                    except (OSError, ValueError):
                        logger.exception("Could not save track quadrant %s", label)
            if not isinstance(payload.get("elements"), list) or not payload["elements"]:
                logger.warning("Overpass track quadrant %d/4 returned no ways", index)
                return {}
            for element in payload["elements"]:
                if isinstance(element, dict) and element.get("type") == "way" \
                        and isinstance(element.get("id"), int):
                    ways[element["id"]] = element
            logger.info("Loaded Overpass track quadrant %d/4: %d ways", index,
                        len(payload["elements"]))
        return {"elements": list(ways.values())}

    async def get_station_nodes(self, north: float, west: float, south: float, east: float) -> Dict:
        """Fetch railway station and halt nodes, including their OSM tags."""
        bbox = f"{south},{west},{north},{east}"
        query = f"""
        [out:json][timeout:120];
        node["railway"~"^(station|halt)$"]({bbox});
        out body;
        """
        return await self._query(query)

    async def close(self):
        await self.client.aclose()


def station_features_from_osm(osm_json: Dict, *, north: float, west: float,
                              south: float, east: float) -> Dict:
    """Validate OSM nodes and retain their tags in GeoJSON properties."""
    if not isinstance(osm_json, dict) or not isinstance(osm_json.get("elements"), list):
        raise ValueError("Overpass station response must contain elements")
    features = {}
    for element in osm_json["elements"]:
        if not isinstance(element, dict) or element.get("type") != "node":
            continue
        node_id = element.get("id")
        latitude, longitude = element.get("lat"), element.get("lon")
        tags = element.get("tags")
        if not isinstance(node_id, int) or isinstance(node_id, bool) or node_id <= 0:
            continue
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) for value in (latitude, longitude)):
            continue
        if not (south <= latitude <= north and west <= longitude <= east):
            continue
        if not isinstance(tags, dict) or tags.get("railway") not in {"station", "halt"}:
            continue
        clean_tags = {key: value for key, value in tags.items()
                      if isinstance(key, str) and isinstance(value, str)}
        feature_id = f"osm:node:{node_id}"
        properties = {
            **clean_tags,
            "id": node_id,
            "name": clean_tags.get("name") or "Unnamed station",
            "railway": tags["railway"],
            "tags": clean_tags,
            # This is the public board route key, resolved to a VBB stop on click.
            "station_id": feature_id,
            "products": {},
            "is_important": is_important_station(clean_tags.get("name") or ""),
        }
        features[feature_id] = {
            "type": "Feature",
            "id": feature_id,
            "properties": properties,
            "geometry": {"type": "Point", "coordinates": [float(longitude), float(latitude)]},
        }
    return {"type": "FeatureCollection", "features": list(features.values())}
