import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import os
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from hafas_client import (
    HafasClient, RadarSubprocessError, RadarSubprocessTimeout,
    TripSubprocessError, TripSubprocessTimeout,
)
from borders import get_cached_borders
from osm_client import (
    OsmClient, DEFAULT_OVERPASS_ENDPOINT, TRACK_FALLBACK_BBOX, TRACKS_CACHE_FILE,
    TRACKS_OVERPASS_CACHE_FILE,
    TRACKS_NATIONWIDE_CACHE_FILE, STATIONS_CACHE_FILE, load_feature_cache, load_json_cache,
    mark_important_stations,
    save_json_cache, station_features_from_osm, track_features_to_osm,
)
from station_client import StationClient
from engine import InterpolationEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client_queues = set()
hafas = None
osm = None
station_client = None
engine = InterpolationEngine()
latest_payload = None
station_payload = None
border_payload = None
station_by_id = {}
osm_stop_ids = {}
departure_cache = {}
trip_cache = {}
last_success = None
upstream_status = "starting"
POLL_INTERVAL = 30
SIMULATION_INTERVAL = 15
BBOX = {"north": 55.0, "south": 47.2, "west": 5.8, "east": 15.0}
VALID_PRODUCTS = {"nationalExpress", "national", "regional", "suburban"}
DEPARTURE_CACHE_SECONDS = 15
TRIP_CACHE_SECONDS = 300
TRACKS_UNAVAILABLE_DETAIL = (
    "Track geometry unavailable: the Overpass-backed in-memory cache is empty"
)
# Rectangular sanity boundary covering Germany, Austria, and Switzerland.
DACH_BOUNDS = {"north": 55.1, "south": 45.5, "west": 5.8, "east": 17.2}


async def load_tracks():
    """Load the complete disk cache first; query Overpass only if it is absent."""
    nationwide_features = await asyncio.to_thread(load_feature_cache, TRACKS_NATIONWIDE_CACHE_FILE)
    if nationwide_features:
        try:
            # Make /tracks available before the routing graph finishes rebuilding.
            engine.geometry_payload = await asyncio.to_thread(
                TRACKS_NATIONWIDE_CACHE_FILE.read_bytes,
            )
            nationwide_osm = await asyncio.to_thread(track_features_to_osm, nationwide_features)
            if await asyncio.to_thread(engine.load_osm_data, nationwide_osm):
                logger.info("Restored %d nationwide railway tracks from disk cache",
                            len(nationwide_features["features"]))
                return
        except Exception:
            logger.exception("Could not restore nationwide railway track cache")
    cached_osm = await asyncio.to_thread(load_json_cache, TRACKS_OVERPASS_CACHE_FILE)
    restored = False
    if cached_osm:
        try:
            restored = await asyncio.to_thread(engine.load_osm_data, cached_osm)
        except Exception:
            logger.exception("Could not restore cached Overpass track graph")
    if not restored:
        cached_features = await asyncio.to_thread(load_feature_cache, TRACKS_CACHE_FILE)
        if cached_features:
            try:
                cached_osm = await asyncio.to_thread(track_features_to_osm, cached_features)
                restored = await asyncio.to_thread(engine.load_osm_data, cached_osm)
            except Exception:
                logger.exception("Could not restore cached GeoJSON track graph")
    if restored:
        logger.info("Restored railway track geometry from disk cache")
    while True:
        for label, bounds in (("nationwide", BBOX), ("fallback", TRACK_FALLBACK_BBOX)):
            # Do not replace a working disk-restored graph with a smaller
            # corridor merely because the nationwide refresh timed out.
            if label == "fallback" and engine.geometry_payload:
                break
            try:
                osm_data = await osm.get_track_geometries(
                    **bounds, chunked=(label == "nationwide"),
                    cache_quadrants=(label == "nationwide"),
                )
                if osm_data.get("elements") and await asyncio.to_thread(engine.load_osm_data, osm_data):
                    feature_count = await asyncio.to_thread(
                        lambda: len(json.loads(engine.geometry_payload)["features"])
                    )
                    logger.info(
                        "Loaded %d %s railway track features from Overpass",
                        feature_count,
                        label,
                    )
                    try:
                        if label == "nationwide":
                            await asyncio.to_thread(
                                save_json_cache, TRACKS_NATIONWIDE_CACHE_FILE,
                                engine.geometry_payload,
                            )
                        await asyncio.to_thread(save_json_cache, TRACKS_OVERPASS_CACHE_FILE, osm_data)
                        await asyncio.to_thread(save_json_cache, TRACKS_CACHE_FILE, engine.geometry_payload)
                    except (OSError, ValueError):
                        logger.exception("Could not save railway track disk cache")
                    return
                logger.warning(
                    "Overpass returned no usable %s track geometry for bounds %s",
                    label,
                    bounds,
                )
            except Exception:
                logger.exception("Could not load %s track geometries", label)
        await asyncio.sleep(60)


async def load_stations():
    """Cache Overpass railway nodes without delaying startup or live polling."""
    global station_payload, station_by_id
    cached = await asyncio.to_thread(load_feature_cache, STATIONS_CACHE_FILE)
    if cached:
        try:
            collection = await asyncio.to_thread(mark_important_stations, cached)
            station_by_id = await asyncio.to_thread(
                lambda: {feature["id"]: feature for feature in collection["features"]}
            )
            station_payload = await asyncio.to_thread(
                lambda: json.dumps(collection, allow_nan=False, separators=(",", ":")).encode("utf-8")
            )
            logger.info("Restored %d railway stations from disk cache", len(collection["features"]))
        except (KeyError, TypeError, ValueError):
            logger.exception("Could not restore cached stations")
    while True:
        try:
            osm_data = await osm.get_station_nodes(**BBOX)
            collection = await asyncio.to_thread(station_features_from_osm, osm_data, **BBOX)
            if collection.get("features"):
                payload = await asyncio.to_thread(
                    lambda: json.dumps(collection, allow_nan=False, separators=(",", ":")).encode("utf-8")
                )
                station_by_id = await asyncio.to_thread(
                    lambda: {feature["id"]: feature for feature in collection["features"]}
                )
                station_payload = payload
                logger.info("Loaded %d railway station nodes from Overpass", len(collection["features"]))
                try:
                    await asyncio.to_thread(save_json_cache, STATIONS_CACHE_FILE, payload)
                except (OSError, ValueError):
                    logger.exception("Could not save station disk cache")
                return
        except Exception:
            logger.exception("Could not load stations")
        await asyncio.sleep(60)


async def load_map_data():
    """Restore both disk caches while serialized Overpass requests refresh them."""
    await asyncio.gather(load_tracks(), load_stations(), load_borders())


async def load_borders():
    """Keep a static national/state outline available independently of Overpass."""
    global border_payload
    while True:
        try:
            collection = await get_cached_borders()
            border_payload = await asyncio.to_thread(
                lambda: json.dumps(collection, allow_nan=False, separators=(",", ":")).encode("utf-8")
            )
            logger.info("Loaded %d cached German border features", len(collection["features"]))
            return
        except Exception:
            logger.exception("Could not load German border geometry")
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    global hafas, osm, station_client, latest_payload, station_payload, border_payload, station_by_id
    global last_success, upstream_status, engine
    hafas = HafasClient(
        os.getenv("HAFAS_BASE_URL", "https://v6.vbb.transport.rest"),
        os.getenv("HAFAS_RADAR_SCRIPT", str(
            os.path.join(os.path.dirname(__file__), "..", "microservice", "hafas_radar.js")
        )),
        os.getenv("NODE_BINARY", "node"),
    )
    osm = OsmClient(os.getenv("OVERPASS_URL", DEFAULT_OVERPASS_ENDPOINT))
    station_client = StationClient(os.getenv("HAFAS_BASE_URL", "https://v6.vbb.transport.rest"))
    engine = InterpolationEngine()
    latest_payload = None
    station_payload = None
    border_payload = None
    station_by_id = {}
    osm_stop_ids.clear()
    departure_cache.clear()
    trip_cache.clear()
    last_success = None
    upstream_status = "starting"
    tasks = [asyncio.create_task(load_map_data()), asyncio.create_task(polling_loop())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(hafas.close(), osm.close(), station_client.close())
        client_queues.clear()


app = FastAPI(title="BahnOpticon Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://localhost:5174"
    ).split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def nonempty_string(value):
    return value.strip() if isinstance(value, str) and value.strip() else "Unknown"


def iso_time(value):
    """Preserve a provider ISO-8601 timestamp only when it includes a UTC offset."""
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    return candidate if parsed.utcoffset() is not None else None


def stopover_details(movement, current_time):
    """Extract the next arrival from the provider's chronologically ordered stopovers."""
    rows = movement.get("nextStopovers")
    rows = rows if isinstance(rows, list) else []

    def station_name(stop):
        name = stop.get("name") if isinstance(stop, dict) else None
        return name.strip() if isinstance(name, str) and name.strip() else None

    valid = [row for row in rows if isinstance(row, dict) and station_name(row.get("stop"))]
    # The ÖBB radar bridge can include the journey's origin and already passed
    # stops. Prefer the first future arrival, then a future departure when no
    # arrival is available (for example, a one-stop origin record).
    def future_event(row, keys):
        value = iso_time(row.get(keys[0])) or iso_time(row.get(keys[1]))
        return bool(value and datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).timestamp() >= current_time)

    next_row = next((row for row in valid if future_event(row, ("arrival", "plannedArrival"))), None)
    if next_row is None:
        next_row = next((row for row in valid if future_event(row, ("departure", "plannedDeparture"))), None)
    if next_row is None:
        next_row = next((row for row in valid if not any(
            iso_time(row.get(key)) for key in ("arrival", "plannedArrival", "departure", "plannedDeparture")
        )), None)
    destination = station_name(valid[-1]["stop"]) if valid else None

    # At the next stop, arrival is the useful event when present; the first
    # stopover often only has departure data, so fall back to departure.
    planned = None
    expected = None
    if next_row:
        planned = iso_time(next_row.get("plannedArrival")) or iso_time(next_row.get("plannedDeparture"))
        expected = iso_time(next_row.get("arrival")) or iso_time(next_row.get("departure"))
    delay_row = next_row or (rows[0] if not valid and rows and isinstance(rows[0], dict) else None)
    raw_delay = delay_row.get("arrivalDelay") if delay_row else None
    if raw_delay is None and delay_row:
        raw_delay = delay_row.get("departureDelay")
    return {
        "next_station": station_name(next_row["stop"]) if next_row else None,
        "destination": destination or nonempty_string(movement.get("direction")),
        "scheduled_time": planned,
        "expected_time": expected,
        "delay_minutes": raw_delay / 60 if isinstance(raw_delay, (int, float))
        and not isinstance(raw_delay, bool) and math.isfinite(raw_delay) else 0,
    }


def route_name(movement, line):
    """Prefer an upstream full route label, with the public line name as fallback."""
    for value in (movement.get("route"), movement.get("routeName"), line.get("route"), line.get("name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)):
            labels = [item.strip() for item in value if isinstance(item, str) and item.strip()]
            if labels:
                return " · ".join(labels)
        if isinstance(value, dict):
            label = value.get("name") or value.get("label")
            if isinstance(label, str) and label.strip():
                return label.strip()
    return "Unknown"


def valid_dach_coordinate(longitude, latitude):
    """Accept only finite numeric coordinates inside the DACH sanity bounds."""
    return (
        isinstance(longitude, (int, float))
        and not isinstance(longitude, bool)
        and math.isfinite(longitude)
        and isinstance(latitude, (int, float))
        and not isinstance(latitude, bool)
        and math.isfinite(latitude)
        and DACH_BOUNDS["west"] <= longitude <= DACH_BOUNDS["east"]
        and DACH_BOUNDS["south"] <= latitude <= DACH_BOUNDS["north"]
    )


def numeric_delay(value):
    """Return a finite numeric delay, defaulting malformed telemetry to zero."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return 0


def valid_stream_geometry(geometry):
    """Validate Point/LineString coordinates before exposing them over SSE."""
    if not isinstance(geometry, dict):
        return False
    coordinates = geometry.get("coordinates")
    if geometry.get("type") == "Point":
        return (
            isinstance(coordinates, (list, tuple))
            and len(coordinates) >= 2
            and valid_dach_coordinate(coordinates[0], coordinates[1])
        )
    if geometry.get("type") == "LineString":
        return (
            isinstance(coordinates, (list, tuple))
            and len(coordinates) >= 2
            and all(
                isinstance(point, (list, tuple))
                and len(point) >= 2
                and valid_dach_coordinate(point[0], point[1])
                for point in coordinates
            )
        )
    return False


def validate_stream_snapshot(message):
    """Apply the final telemetry checks before an SSE snapshot is serialized."""
    if not isinstance(message, dict) or message.get("type") != "FeatureCollection":
        return message

    validated_features = []
    for feature in message.get("features", []):
        if not isinstance(feature, dict) or not valid_stream_geometry(feature.get("geometry")):
            continue
        properties = feature.get("properties")
        properties = dict(properties) if isinstance(properties, dict) else {}
        properties["delay_minutes"] = numeric_delay(properties.get("delay_minutes"))
        validated_feature = dict(feature)
        validated_feature["properties"] = properties
        validated_features.append(validated_feature)

    validated = dict(message)
    validated["features"] = validated_features
    is_mock = validated.get("is_mock") is True or any(
        feature["properties"].get("is_mock") is True for feature in validated_features
    )
    if is_mock:
        logger.warning("MOCK TRAFFIC ACTIVE: streaming simulated vehicle telemetry")

    for index, feature in enumerate(validated_features[:3], start=1):
        properties = feature["properties"]
        current_coordinates = properties.get("end")
        if not isinstance(current_coordinates, (list, tuple)):
            geometry_coordinates = feature["geometry"].get("coordinates")
            current_coordinates = (
                geometry_coordinates[-1]
                if feature["geometry"].get("type") == "LineString"
                else geometry_coordinates
            )
        logger.info(
            "Telemetry sample %d: trip_id=%s product=%s generated_at=%s "
            "scheduled_time=%s expected_time=%s delay_minutes=%s coordinates=%s is_mock=%s",
            index,
            properties.get("trip_id", feature.get("id")),
            properties.get("product"),
            validated.get("generated_at"),
            properties.get("scheduled_time"),
            properties.get("expected_time"),
            properties["delay_minutes"],
            current_coordinates,
            properties.get("is_mock") is True,
        )
    return validated


def normalize_movements(radar_data, current_time):
    """Build a complete GeoJSON snapshot off the ASGI event loop."""
    if not isinstance(radar_data, dict) or not isinstance(radar_data.get("movements"), list):
        raise ValueError("Radar response must contain a movements array")
    engine.purge_stale_states(current_time)
    features = {}
    for movement in radar_data["movements"]:
        if not isinstance(movement, dict):
            continue
        line = movement.get("line") or {}
        loc = movement.get("location") or {}
        if not isinstance(line, dict) or not isinstance(loc, dict):
            continue
        product = line.get("product")
        trip_id = movement.get("tripId")
        if not isinstance(product, str) or product not in VALID_PRODUCTS or not isinstance(trip_id, str) or not trip_id:
            continue
        if trip_id in features:
            continue
        lon, lat = loc.get("longitude"), loc.get("latitude")
        if not valid_dach_coordinate(lon, lat):
            continue
        stopovers = stopover_details(movement, current_time)
        trajectory = engine.compute_trajectory(trip_id, lon, lat, current_time, product)
        full_route = route_name(movement, line)
        features[trip_id] = {
            "type": "Feature",
            "id": trip_id,
            "properties": {
                "trip_id": trip_id,
                "duration_ms": trajectory["duration_ms"],
                "start": trajectory["start_point"],
                "end": trajectory["end_point"],
                "route_status": trajectory["route_status"],
                "line_name": nonempty_string(line.get("name")),
                "route": full_route,
                "product": product,
                **stopovers,
            },
            "geometry": (
                {"type": "LineString", "coordinates": trajectory["route"]}
                if trajectory["route"] else
                {"type": "Point", "coordinates": trajectory["end_point"]}
            ),
        }
    return {"type": "FeatureCollection", "generated_at": current_time, "features": list(features.values())}


def retry_delay(error, backoff):
    if isinstance(error, httpx.HTTPStatusError):
        value = error.response.headers.get("Retry-After")
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = backoff
            if math.isfinite(delay):
                return max(backoff, delay)
    return backoff


async def polling_loop():
    global last_success, upstream_status
    backoff = 5
    simulator_task = None
    try:
        while True:
            try:
                radar_data = await hafas.get_radar()
                current_time = time.time()
                feature_collection = await asyncio.to_thread(normalize_movements, radar_data, current_time)
                if simulator_task is not None:
                    simulator_task.cancel()
                    await asyncio.gather(simulator_task, return_exceptions=True)
                    simulator_task = None
                await broadcast(feature_collection)
                last_success = current_time
                upstream_status = "live"
                backoff = 5
                logger.info("Poll successful: %s vehicles", len(feature_collection["features"]))
                await asyncio.sleep(POLL_INTERVAL)
            except Exception as error:
                upstream_status = "unavailable"
                if isinstance(error, RadarSubprocessTimeout):
                    logger.warning("Node HAFAS radar timeout: %s", error)
                elif isinstance(error, RadarSubprocessError):
                    logger.warning("Node HAFAS radar failed; retaining last snapshot: %s", error)
                elif isinstance(error, httpx.TimeoutException):
                    logger.warning("HAFAS Radar Timeout")
                elif isinstance(error, httpx.HTTPStatusError):
                    logger.warning("Polling failed; retaining last snapshot: HTTP %s; response body: %s",
                                   error.response.status_code, error.response.text)
                else:
                    logger.warning("Polling failed; retaining last snapshot: %s: %s",
                                   type(error).__name__, error)
                should_simulate = isinstance(error, (RadarSubprocessError, httpx.RequestError)) or (
                    isinstance(error, httpx.HTTPStatusError)
                    and 500 <= error.response.status_code < 600
                )
                if should_simulate and (simulator_task is None or simulator_task.done()):
                    logger.warning("Upstream API failed. Yielding simulated traffic along physical tracks.")
                    simulator_task = asyncio.create_task(simulation_loop())
                await asyncio.sleep(retry_delay(error, backoff))
                backoff = min(backoff * 2, 60)
    finally:
        if simulator_task is not None:
            simulator_task.cancel()
            await asyncio.gather(simulator_task, return_exceptions=True)


async def simulation_loop():
    """Publish graph-based demo traffic every 15 seconds while radar is down."""
    while True:
        try:
            snapshot = await asyncio.to_thread(engine.generate_mock_traffic)
            await broadcast(snapshot)
            logger.info("Published %d simulated vehicles", len(snapshot["features"]))
        except RuntimeError as error:
            logger.warning("Simulation waiting for track geometry: %s", error)
        except Exception:
            logger.exception("Could not generate simulated traffic")
        await asyncio.sleep(SIMULATION_INTERVAL)


async def broadcast(message):
    global latest_payload
    # Validation and JSON serialization can be expensive for large snapshots.
    validated = await asyncio.to_thread(validate_stream_snapshot, message)
    latest_payload = "data: " + await asyncio.to_thread(
        json.dumps, validated, allow_nan=False
    ) + "\n\n"
    for queue in tuple(client_queues):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(latest_payload)


@app.get("/health")
async def health():
    return {"status": "ok", "upstream": upstream_status, "last_success": last_success}


@app.get("/geometry")
async def geometry():
    if not engine.geometry_payload:
        logger.error(TRACKS_UNAVAILABLE_DETAIL)
        raise HTTPException(status_code=503, detail=TRACKS_UNAVAILABLE_DETAIL,
                            headers={"Retry-After": "60"})
    return Response(content=engine.geometry_payload, media_type="application/geo+json")


@app.get("/tracks")
async def tracks():
    """Compatibility alias for clients that call the physical rail data tracks."""
    return await geometry()


@app.get("/stations")
async def stations():
    if station_payload is None:
        raise HTTPException(status_code=503, detail="Stations are still loading",
                            headers={"Retry-After": "60"})
    return Response(content=station_payload, media_type="application/geo+json")


@app.get("/borders")
async def borders():
    if border_payload is None:
        raise HTTPException(status_code=503, detail="Border geometry is still loading",
                            headers={"Retry-After": "60"})
    return Response(content=border_payload, media_type="application/geo+json")


@app.get("/trip/{trip_id:path}")
async def trip(trip_id: str):
    """Return one whole HAFAS journey as a GeoJSON LineString Feature."""
    if not trip_id.strip() or len(trip_id) > 4096:
        raise HTTPException(status_code=400, detail="Invalid trip ID")
    now = time.monotonic()
    cached = trip_cache.get(trip_id)
    if cached and now - cached[0] < TRIP_CACHE_SECONDS:
        return Response(content=cached[1], media_type="application/geo+json")
    try:
        feature = await hafas.get_trip(trip_id)
    except TripSubprocessTimeout as error:
        raise HTTPException(status_code=504, detail="Trip provider timed out") from error
    except TripSubprocessError as error:
        status = 404 if "notfound" in str(error).casefold() or "not found" in str(error).casefold() else 502
        raise HTTPException(status_code=status, detail="Trip geometry unavailable") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid trip ID") from error
    payload = await asyncio.to_thread(
        lambda: json.dumps(feature, allow_nan=False, separators=(",", ":")).encode("utf-8")
    )
    if len(trip_cache) >= 128:
        trip_cache.clear()
    trip_cache[trip_id] = (time.monotonic(), payload)
    return Response(content=payload, media_type="application/geo+json")


@app.get("/station/{station_id}/departures")
async def station_departures(station_id: str):
    if station_id.startswith("osm:node:"):
        node_id = station_id.removeprefix("osm:node:")
        if not (node_id.isascii() and node_id.isdigit()):
            raise HTTPException(status_code=400, detail="Invalid OSM station ID")
        station = station_by_id.get(station_id)
        if station is None:
            raise HTTPException(status_code=404, detail="OSM station not found")
        resolved = osm_stop_ids.get(station_id)
        if resolved is None:
            longitude, latitude = station["geometry"]["coordinates"]
            try:
                resolved = await station_client.resolve_stop_id(
                    latitude, longitude, station["properties"]["name"]
                )
            except (httpx.HTTPStatusError, httpx.TimeoutException,
                    httpx.RequestError, ValueError) as error:
                raise _departure_provider_error(error) from error
            if resolved is None:
                raise HTTPException(status_code=404, detail="No VBB stop found near this OSM station")
            osm_stop_ids[station_id] = resolved
        station_id = resolved
    elif not (station_id.isascii() and station_id.isdigit() and 5 <= len(station_id) <= 15):
        raise HTTPException(status_code=400, detail="Invalid VBB station ID")
    cached = departure_cache.get(station_id)
    now = time.monotonic()
    if cached and now - cached[0] < DEPARTURE_CACHE_SECONDS:
        return Response(content=cached[1], media_type="application/json")
    try:
        departures = await hafas.get_departures(station_id, limit=10)
    except (httpx.HTTPStatusError, httpx.TimeoutException,
            httpx.RequestError, ValueError) as error:
        raise _departure_provider_error(error) from error
    payload = await asyncio.to_thread(
        lambda: json.dumps(departures, allow_nan=False, separators=(",", ":")).encode("utf-8")
    )
    if len(departure_cache) >= 128:
        departure_cache.clear()
    departure_cache[station_id] = (time.monotonic(), payload)
    return Response(content=payload, media_type="application/json")


def _departure_provider_error(error):
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 404:
            return HTTPException(status_code=404, detail="Station not found")
        if status == 429:
            return HTTPException(status_code=503, detail="Departure provider is rate limited",
                                 headers={"Retry-After": error.response.headers.get("Retry-After", "15")})
        return HTTPException(status_code=502, detail="Departure provider failed")
    if isinstance(error, httpx.TimeoutException):
        return HTTPException(status_code=504, detail="Departure provider timed out")
    return HTTPException(status_code=502, detail="Departure provider failed")


@app.get("/departures/{station_id}")
async def departures(station_id: str):
    """Compatibility alias for the concise departures endpoint."""
    return await station_departures(station_id)


@app.get("/stream")
async def sse_endpoint(request: Request):
    async def event_generator():
        queue = asyncio.Queue(maxsize=1)
        client_queues.add(queue)
        if latest_payload is not None:
            queue.put_nowait(latest_payload)
        try:
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            client_queues.discard(queue)

    return StreamingResponse(
        event_generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
