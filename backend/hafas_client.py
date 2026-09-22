import asyncio
import json
import math
from pathlib import Path
from urllib.parse import quote

import httpx


RADAR_PRODUCTS = frozenset({"nationalExpress", "national", "regional", "suburban"})
DEFAULT_RADAR_SCRIPT = Path(__file__).resolve().parent.parent / "microservice" / "hafas_radar.js"
DEFAULT_TRIP_SCRIPT = Path(__file__).resolve().parent.parent / "microservice" / "hafas_trip.js"
RADAR_SUBPROCESS_TIMEOUT = 75.0
TRIP_SUBPROCESS_TIMEOUT = 45.0


class RadarSubprocessError(RuntimeError):
    """The local Node HAFAS adapter failed or returned unusable output."""


class RadarSubprocessTimeout(RadarSubprocessError):
    """The local Node HAFAS adapter exceeded its execution deadline."""


class TripSubprocessError(RuntimeError):
    """The local Node HAFAS trip adapter failed or returned unusable geometry."""


class TripSubprocessTimeout(TripSubprocessError):
    """The local Node HAFAS trip adapter exceeded its execution deadline."""


def trip_linestring(payload, trip_id: str) -> dict:
    """Convert hafas-client's ordered Point polyline into a journey LineString."""
    trip = payload.get("trip") if isinstance(payload, dict) else None
    polyline = trip.get("polyline") if isinstance(trip, dict) else None
    features = polyline.get("features") if isinstance(polyline, dict) else None
    if not isinstance(features, list):
        raise ValueError("HAFAS trip response has no point polyline")
    coordinates = []
    for feature in features:
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        point = geometry.get("coordinates") if isinstance(geometry, dict) and geometry.get("type") == "Point" else None
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        longitude, latitude = point[:2]
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) for value in (longitude, latitude)):
            continue
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            continue
        vertex = [longitude, latitude]
        if not coordinates or vertex != coordinates[-1]:
            coordinates.append(vertex)
    if len(coordinates) < 2:
        raise ValueError("HAFAS trip polyline has fewer than two valid points")
    line = trip.get("line") if isinstance(trip.get("line"), dict) else {}
    destination = trip.get("destination")
    return {
        "type": "Feature", "id": trip_id,
        "properties": {
            "trip_id": trip_id,
            "line_name": _text(line.get("name")),
            "destination": _text(destination.get("name")) if isinstance(destination, dict) else None,
        },
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalize_departures(rows, limit):
    departures = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        when = _text(row.get("when"))
        planned_when = _text(row.get("plannedWhen"))
        if when is None and planned_when is None:
            continue
        line = row.get("line")
        if not isinstance(line, dict):
            line = {}
        delay = row.get("delay")
        delay_minutes = (
            delay / 60
            if isinstance(delay, (int, float)) and not isinstance(delay, bool) and math.isfinite(delay)
            else None
        )
        departures.append({
            "trip_id": _text(row.get("tripId")),
            "line_name": _text(line.get("name")) or "Unknown",
            "product": _text(line.get("product")) or "unknown",
            "direction": _text(row.get("direction")) or "Unknown",
            "when": when,
            "planned_when": planned_when,
            "expected_time": when,
            "scheduled_time": planned_when,
            "delay_minutes": delay_minutes,
            "platform": _text(row.get("platform")),
            "planned_platform": _text(row.get("plannedPlatform")),
            "cancelled": row.get("cancelled") is True,
        })
        if len(departures) == limit:
            break
    return departures


class HafasClient:
    def __init__(self, base_url: str = "https://v6.vbb.transport.rest",
                 radar_script: str | Path = DEFAULT_RADAR_SCRIPT,
                 node_binary: str = "node",
                 radar_timeout: float = RADAR_SUBPROCESS_TIMEOUT,
                 trip_script: str | Path = DEFAULT_TRIP_SCRIPT,
                 trip_timeout: float = TRIP_SUBPROCESS_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.radar_script = Path(radar_script).expanduser().resolve()
        self.node_binary = node_binary
        self.radar_timeout = radar_timeout
        self.trip_script = Path(trip_script).expanduser().resolve()
        self.trip_timeout = trip_timeout
        self.client = httpx.AsyncClient(timeout=25.0, headers={"User-Agent": "BahnOpticon/1.0"})

    async def get_radar(self):
        """Run the local Node HAFAS adapter without blocking the ASGI event loop."""
        if not self.radar_script.is_file():
            raise RadarSubprocessError(f"Radar script does not exist: {self.radar_script}")
        try:
            process = await asyncio.create_subprocess_exec(
                self.node_binary,
                str(self.radar_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise RadarSubprocessError(f"Could not start Node radar process: {error}") from error

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.radar_timeout,
            )
        except TimeoutError as error:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise RadarSubprocessTimeout(
                f"Node radar process exceeded {self.radar_timeout:g} seconds"
            ) from error
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = stderr_text[-4000:] or "no stderr output"
            raise RadarSubprocessError(
                f"Node radar process exited with status {process.returncode}: {detail}"
            )
        if not stdout.strip():
            raise RadarSubprocessError("Node radar process returned empty stdout")
        try:
            payload = await asyncio.to_thread(json.loads, stdout)
            return await asyncio.to_thread(self._filter_radar_payload, payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            detail = stderr_text[-1000:]
            suffix = f"; stderr: {detail}" if detail else ""
            raise RadarSubprocessError(f"Node radar process returned invalid JSON{suffix}") from error

    @staticmethod
    def _filter_radar_payload(payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("movements"), list):
            raise ValueError("Node radar response must contain a movements array")
        return {"movements": [movement for movement in payload["movements"]
                if isinstance(movement, dict)
                and isinstance(movement.get("line"), dict)
                and isinstance(movement["line"].get("product"), str)
                and movement["line"]["product"] in RADAR_PRODUCTS]}

    async def get_departures(self, station_id: str, limit: int = 10) -> list[dict]:
        """Return the next departures at a VBB stop as a compact board list."""
        if not isinstance(station_id, str) or not station_id.strip():
            raise ValueError("A VBB station ID is required")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ValueError("Departure limit must be between 1 and 10")

        response = await self.client.get(
            f"{self.base_url}/stops/{quote(station_id.strip(), safe='')}/departures",
            params={"results": limit, "duration": 120},
        )
        response.raise_for_status()
        payload = await asyncio.to_thread(response.json)
        rows = payload.get("departures") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("Departure response must contain a departures array")
        return await asyncio.to_thread(_normalize_departures, rows, limit)

    async def get_trip(self, trip_id: str) -> dict:
        """Fetch one full trip through the local Node bridge without blocking ASGI."""
        if not isinstance(trip_id, str) or not trip_id.strip() or len(trip_id) > 4096:
            raise ValueError("A valid trip ID is required")
        if not self.trip_script.is_file():
            raise TripSubprocessError(f"Trip script does not exist: {self.trip_script}")
        try:
            process = await asyncio.create_subprocess_exec(
                self.node_binary, str(self.trip_script), trip_id,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise TripSubprocessError(f"Could not start Node trip process: {error}") from error
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.trip_timeout)
        except TimeoutError as error:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise TripSubprocessTimeout(
                f"Node trip process exceeded {self.trip_timeout:g} seconds"
            ) from error
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
            raise TripSubprocessError(f"Node trip process exited with status {process.returncode}: {detail}")
        try:
            payload = await asyncio.to_thread(json.loads, stdout)
            return await asyncio.to_thread(trip_linestring, payload, trip_id)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise TripSubprocessError(f"Node trip process returned invalid polyline: {error}") from error

    async def close(self):
        await self.client.aclose()
