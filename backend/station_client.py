"""Discover VBB stops with IDs accepted by the departures API."""

import asyncio
import logging
import math
import re

import httpx


logger = logging.getLogger(__name__)


def _station_name_key(value):
    """Ignore common mode/city affixes when comparing OSM and VBB names."""
    if not isinstance(value, str):
        return ""
    words = re.findall(r"[\w]+", value.lower())
    return " ".join(word for word in words if word not in {"s", "u", "berlin", "bhf", "bahnhof"})


class StationClient:
    # A 5 km grid needs 63 queries for the fixed Berlin viewport. A 0.8 s
    # launch interval stays below the public API's 100 requests/minute limit
    # and leaves capacity for the radar poll and on-demand departures.
    GRID_SPACING_METERS = 5_000
    SEARCH_RADIUS_METERS = 4_500
    RESULTS_PER_QUERY = 1_000
    MAX_GRID_POINTS = 80

    def __init__(
        self,
        base_url: str = "https://v6.vbb.transport.rest",
        *,
        request_interval: float = 0.8,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.request_interval = request_interval
        self.client = httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": "BahnOpticon/1.0"},
            transport=transport,
        )

    @classmethod
    def _grid_centers(cls, north: float, west: float, south: float, east: float):
        bounds = (north, west, south, east)
        if not all(math.isfinite(value) for value in bounds) or not (
            -90 <= south < north <= 90 and -180 <= west < east <= 180
        ):
            raise ValueError("Invalid station bounding box")

        middle_latitude = (north + south) / 2
        height = (north - south) * 111_320
        width = (east - west) * 111_320 * math.cos(math.radians(middle_latitude))
        rows = max(1, math.ceil(height / cls.GRID_SPACING_METERS))
        columns = max(1, math.ceil(width / cls.GRID_SPACING_METERS))
        if rows * columns > cls.MAX_GRID_POINTS:
            raise ValueError("Station bounding box requires too many queries")

        return [
            (
                south + (row + 0.5) * (north - south) / rows,
                west + (column + 0.5) * (east - west) / columns,
            )
            for row in range(rows)
            for column in range(columns)
        ]

    async def get_stations(self, north: float, west: float, south: float, east: float) -> dict:
        """Return nearby stops clipped to the viewport as GeoJSON Points.

        Responses are deduplicated by native VBB stop ID, so every returned
        feature can be passed directly to ``/stops/{id}/departures``.
        """
        centers = self._grid_centers(north, west, south, east)
        semaphore = asyncio.Semaphore(3)

        async def fetch(index: int, latitude: float, longitude: float):
            await asyncio.sleep(index * self.request_interval)
            async with semaphore:
                response = await self.client.get(
                    f"{self.base_url}/locations/nearby",
                    params={
                        "latitude": latitude,
                        "longitude": longitude,
                        "results": self.RESULTS_PER_QUERY,
                        "distance": self.SEARCH_RADIUS_METERS,
                        "stops": "true",
                        "poi": "false",
                    },
                )
                response.raise_for_status()
                return await asyncio.to_thread(response.json)

        responses = await asyncio.gather(
            *(fetch(index, *center) for index, center in enumerate(centers)),
            return_exceptions=True,
        )
        features = {}
        successful_queries = 0
        for response in responses:
            if isinstance(response, Exception):
                logger.warning("VBB station discovery query failed: %s", response)
                continue
            if not isinstance(response, list):
                logger.warning("VBB station discovery returned an unexpected payload")
                continue
            successful_queries += 1
            for stop in response:
                if not isinstance(stop, dict) or stop.get("type") not in {"stop", "station"}:
                    continue
                stop_id = stop.get("id")
                if not isinstance(stop_id, str) or not stop_id.isascii() or not stop_id.isdigit():
                    continue
                location = stop.get("location")
                if not isinstance(location, dict):
                    continue
                latitude = location.get("latitude")
                longitude = location.get("longitude")
                if not all(
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value)
                    for value in (latitude, longitude)
                ):
                    continue
                if not (south <= latitude <= north and west <= longitude <= east):
                    continue
                products = stop.get("products")
                if stop_id in features:
                    continue
                features[stop_id] = {
                    "type": "Feature",
                    "id": stop_id,
                    "properties": {
                        "station_id": stop_id,
                        "name": stop.get("name") if isinstance(stop.get("name"), str) and stop["name"].strip() else "Unknown station",
                        "products": products if isinstance(products, dict) else {},
                    },
                    "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
                }

        if not successful_queries:
            raise RuntimeError("All VBB station discovery queries failed")
        return {"type": "FeatureCollection", "features": list(features.values())}

    async def resolve_stop_id(self, latitude: float, longitude: float, name: str) -> str | None:
        """Match one clicked OSM station to a departures-ready VBB stop ID."""
        response = await self.client.get(
            f"{self.base_url}/locations/nearby",
            params={"latitude": latitude, "longitude": longitude, "results": 30,
                    "distance": 750, "stops": "true", "poi": "false"},
        )
        response.raise_for_status()
        rows = await asyncio.to_thread(response.json)
        if not isinstance(rows, list):
            raise ValueError("Nearby VBB response must be an array")
        wanted = _station_name_key(name)
        best = None
        best_score = -math.inf
        for stop in rows:
            if not isinstance(stop, dict) or stop.get("type") not in {"stop", "station"}:
                continue
            stop_id = stop.get("id")
            location = stop.get("location")
            if not isinstance(stop_id, str) or not stop_id.isascii() or not stop_id.isdigit():
                continue
            if not isinstance(location, dict):
                continue
            other_lat, other_lon = location.get("latitude"), location.get("longitude")
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                       and math.isfinite(value) for value in (other_lat, other_lon)):
                continue
            north_m = (other_lat - latitude) * 111_320
            east_m = (other_lon - longitude) * 111_320 * math.cos(math.radians(latitude))
            distance = math.hypot(north_m, east_m)
            if distance > 750:
                continue
            candidate = _station_name_key(stop.get("name"))
            name_bonus = 500 if wanted and candidate == wanted else (
                250 if wanted and candidate and (wanted in candidate or candidate in wanted) else 0
            )
            products = stop.get("products")
            rail_bonus = 50 if isinstance(products, dict) and any(
                products.get(product) is True for product in
                ("suburban", "subway", "regional", "regionalExpress", "national", "nationalExpress")
            ) else 0
            score = name_bonus + rail_bonus - distance
            if score > best_score:
                best, best_score = stop_id, score
        return best

    async def close(self):
        await self.client.aclose()
