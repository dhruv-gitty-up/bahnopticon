"""Supabase PostGIS access and conversion for nationwide rail geometry."""

import json
import logging
import os
from pathlib import Path
from typing import Dict

import asyncpg


logger = logging.getLogger(__name__)
DATA_DIR = Path(os.getenv("BAHNOPTICON_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))

TRACKS_GEOJSON_QUERY = """
SELECT json_build_object(
    'type', 'FeatureCollection',
    'features', COALESCE(
        json_agg(
            json_build_object(
                'type', 'Feature',
                'id', id,
                'properties', json_build_object('product', product),
                'geometry', extensions.ST_AsGeoJSON(geom)::json
            )
        ),
        '[]'::json
    )
)
FROM public.tracks
"""

STATIONS_GEOJSON_QUERY = """
SELECT json_build_object(
    'type', 'FeatureCollection',
    'features', COALESCE(
        json_agg(
            json_build_object(
                'type', 'Feature',
                'id', id,
                'properties', json_build_object(
                    'station_id', id,
                    'name', name,
                    'is_important', is_important
                ),
                'geometry', extensions.ST_AsGeoJSON(geom)::json
            )
        ),
        '[]'::json
    )
)
FROM public.stations
"""


def load_json_cache(path: Path) -> Dict | None:
    """Return a valid cached JSON object for the independent border layer."""
    try:
        payload = json.loads(path.read_bytes())
        return payload if isinstance(payload, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("Could not load JSON cache %s: %s", path, error)
        return None


def save_json_cache(path: Path, payload: Dict | bytes) -> None:
    """Write a complete JSON cache atomically."""
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


def track_product(tags: Dict) -> str:
    """Normalize either database products or legacy OSM tags for the engine."""
    product = tags.get("product")
    if product in {"suburban", "subway", "regional", "national", "nationalExpress"}:
        return product
    railway = tags.get("railway")
    if railway == "subway":
        return "subway"
    if railway == "light_rail":
        return "suburban"
    if railway == "rail" and (tags.get("highspeed") == "yes" or tags.get("usage") == "main"):
        return "nationalExpress"
    return "regional"


def track_features_to_osm(collection: Dict) -> Dict:
    """Convert database GeoJSON into the graph builder's way representation."""
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
        product = track_product(properties)
        railway = {"subway": "subway", "suburban": "light_rail"}.get(product, "rail")
        tags = {
            key: value for key, value in properties.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        tags.update({"product": product, "railway": railway})
        elements.append({
            "type": "way",
            "id": way_id,
            "geometry": vertices,
            "tags": tags,
        })
    return {"elements": elements}


class OsmClient:
    """Authenticated async connection pool for static PostGIS map data."""

    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or os.getenv("DATABASE_URL")
        self.pool = None

    async def connect(self) -> None:
        if self.pool is not None:
            return
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required for PostGIS map geometry")
        self.pool = await asyncpg.create_pool(
            dsn=self.database_url,
            min_size=1,
            max_size=4,
            command_timeout=120,
            statement_cache_size=0,
            server_settings={"application_name": "bahnopticon-backend"},
        )
        async with self.pool.acquire() as connection:
            database_role = await connection.fetchval("SELECT current_user")
        if database_role in {"anon", "authenticated"}:
            await self.pool.close()
            self.pool = None
            raise RuntimeError(
                "DATABASE_URL must use an authenticated Postgres role that can read RLS-protected map tables"
            )
        logger.info("Connected PostGIS geometry pool as database role %s", database_role)

    async def _feature_collection(self, query: str, label: str) -> Dict:
        if self.pool is None:
            await self.connect()
        async with self.pool.acquire() as connection:
            payload = await connection.fetchval(query)
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" \
                or not isinstance(payload.get("features"), list):
            raise ValueError(f"PostGIS returned invalid {label} GeoJSON")
        return payload

    async def get_track_feature_collection(self) -> Dict:
        return await self._feature_collection(TRACKS_GEOJSON_QUERY, "track")

    async def get_station_feature_collection(self) -> Dict:
        return await self._feature_collection(STATIONS_GEOJSON_QUERY, "station")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
