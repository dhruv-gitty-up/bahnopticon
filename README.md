# BahnOpticon

BahnOpticon is a nationwide German rail transit map. A FastAPI service launches a local Node.js HAFAS adapter every 30 seconds, filters to long-distance and regional train products, and streams complete GeoJSON snapshots to a React application. When radar fails, a graph-based simulator can keep the stream moving after Overpass geometry loads. MapLibre displays OpenStreetMap tiles; Deck.gl draws and animates vehicle icons in WebGL from a Germany-centered macro view.

This is a **nationwide rail visualization experiment**, not a verified nationwide Deutsche Bahn feed. The project plan in [AGENTS.md](AGENTS.md) describes a broader target; the sections below describe the code that exists today.

## Current scope

| Area | Implemented behavior |
| --- | --- |
| Coverage | Radar, tracks, and station queries use the Germany box: 47.2–55.0° N, 5.8–15.0° E. The frontend opens at Germany's center (51.16° N, 10.45° E, zoom 5.5); moving the map does not change backend queries. |
| Live provider | A local `microservice/hafas_radar.js` process uses `hafas-client` with its ÖBB profile. It polls four Germany quadrants concurrently at up to 1,000 movements each, deduplicates trips, and emits JSON over stdout. Only `nationalExpress`, `national`, `regional`, and `suburban` products are retained; regional and S-Bahn positions must pass the configured geographic bounds. |
| Outage simulator | After the Node adapter cannot start, times out, exits unsuccessfully, or emits invalid JSON, 20 stable mock vehicles step across connected nodes of cached Overpass tracks every 15 seconds. Simulated collections and features carry `is_mock: true`. Normal live snapshots resume after radar recovers. |
| Track geometry | Four sequential Overpass quadrants fetch `railway=rail` ways with `highspeed=yes` or `usage=main`, each with a 60-second limit. Successful quadrants are checkpointed and the complete 129,531-feature network is cached in `data/tracks_cache_nationwide.geojson`; that file is loaded before any new Overpass request. Observations within 250 m of a rail segment snap to it. Connected updates use the shortest path; missing or disconnected paths use a straight two-point `LineString`. |
| Static map data | `GET /tracks` (also `/geometry`) serves cached rail `LineString`s. `GET /stations` serves station/halt `Point`s with an `is_important` flag for major station names. Successful Overpass responses update both the in-memory data and atomic disk caches. `GET /borders` serves a bundled, simplified German national and state boundary cache. |
| Updates | One backend poll loop launches the Node adapter every 30 seconds after a successful request. Each SSE message replaces the whole vehicle snapshot. |
| Map | OpenStreetMap raster tiles, one shared MapLibre/Deck.gl camera, Germany and state outlines, subdued product-filtered rail lines, station icons from zoom 11 (major stations 3× larger), product-colored rail markers, GPU track trails, and distance-based movement along routed track vertices. Selecting a train fetches and highlights its entire HAFAS journey in yellow. ICE, IC, regional, and S-Bahn services use white, light grey, red, and green respectively. |
| Controls | Nationwide line/destination/trip search; consolidated ICE/IC, Regional, and S-Bahn toggles; hide-delayed toggle; reset; counts; cursor-following vehicle tooltip; full vehicle timing panel; live station departure board; and a matching floating color legend. |
| State | React state and a stream hook; no Redux, Zustand, database, Redis, or persistent history. |

The [ÖBB HAFAS profile](https://github.com/public-transport/hafas-client/blob/main/p/oebb/index.js) supports radar and defines the [four requested product IDs](https://raw.githubusercontent.com/public-transport/hafas-client/main/p/oebb/products.js). The backend no longer calls the hosted `v6.oebb.transport.rest` radar wrapper. Instead, Python starts the local Node adapter with `asyncio.create_subprocess_exec`, reads its stdout asynchronously, and parses JSON in a worker thread. The adapter passes an explicit products mask to `radar()` and defensively filters the returned array again. Radar omits provider polylines because frequent vehicle updates use cached OSM routing; a separate on-demand Node trip request retrieves the complete provider polyline for the selected journey. VBB remains the HTTP provider for station matching and departure boards, which are therefore not verified nationwide. There is no GTFS-Realtime ingestion.

## How it works

```text
ÖBB HAFAS ──hafas-client/Node stdout──> FastAPI ──GeoJSON snapshots over SSE──> React
Overpass railway ways ──────────> FastAPI                         │              ├──> Deck.gl tracks, trails + vehicle icons
Overpass station/halt nodes ─────> FastAPI /stations ─────────────┼──────────────┼──> Deck.gl station icons
                                                                 │              └──> MapLibre + OSM tiles
```

At startup, the backend starts the Node-backed radar poller and loads tracks, stations, and borders independently. Track and station GeoJSON is restored from `data/` before Overpass refreshes are attempted; successful refreshes atomically replace the disk files. A successful fresh track fetch also saves the raw Overpass response so OSM node IDs can rebuild the routing graph on restart. The current Berlin fallback was reconstructed from older GeoJSON and uses coordinate-based junctions instead; if only GeoJSON is present, startup can reconstruct that form again. Overpass track and station requests share one lock to avoid competing heavy queries. The nationwide query runs first, with a Berlin–Leipzig fallback only when no track cache is usable. `/tracks`, `/stations`, and `/borders` return the in-memory data immediately once loaded and never make an upstream request per page view. Every successful radar cycle launches one Node process, requests `results=1000`, emits the four supported rail products, and waits 30 seconds. Node execution has a 75-second deadline; failures start a 15-second simulator while polling retries with exponential backoff. Graph construction, JSON processing, and routing run off the FastAPI event loop. The normalizer skips malformed movements, removes duplicate IDs, selects the first future stopover arrival (rather than a past journey origin), and derives that stop's delay; missing or invalid delays default to zero. Trip positions and trajectory state are keyed by trip ID, with entries evicted after 15 minutes without an update.

For a moved rail vehicle whose previous and current snapped segments connect, the backend projects both observations onto matching track and finds the weighted NetworkX shortest path between those positions. National express, national, and regional trains use the cached `railway=rail` graph. Suburban trains seek `light_rail` ways, which the optimized nationwide query omits, so their updates usually take the straight-line fallback outside the Berlin seed. A connected `LineString` includes projected endpoints and intervening OSM vertices. A fresh Overpass graph uses actual OSM node IDs; the Berlin seed uses coordinate junctions and can be less precise at crossings. If a vehicle has no usable nearby segment or its two segments are disconnected, the backend sends a straight `[start, end]` `LineString` with `route_status: "straight"`. A first observation or stationary vehicle has Point geometry.

The simulator chooses 20 stable IDs across the `nationalExpress`, `national`, `regional`, and `suburban` track products present in the graph. The bundled Berlin fallback cache has regional and suburban ways; a fresh nationwide mainline result is generally classified as `nationalExpress`, so its simulation may consist entirely of mock ICE-type vehicles. Each first position is an exact graph node. Later snapshots advance by one adjacent edge carrying the same product, reversing at dead ends. Generated positions, delays, and labels are demonstration data marked with `is_mock: true`. If no usable graph exists, simulation waits for a later cache load.

The frontend treats the stream as complete snapshots, validates GeoJSON, and keeps the last valid data if an update is malformed. Stable vehicle slots prevent reordered snapshots from animating one train into another. For a routed update, it measures the `LineString` once and assigns each track vertex a timestamp proportional to distance traveled. A Deck.gl `TripsLayer` draws a short trail, while a `ScatterplotLayer` and rail-glyph `IconLayer` place vehicles at the same sampled position. A React `requestAnimationFrame` clock drives movement and stops when a transition finishes. Static GeoJSON layers place national and state borders below product-filtered tracks (dashed product colors at alpha 40). A selected vehicle triggers `/trip/{trip_id}` and draws its complete provider journey as a bright yellow line with a soft halo above the tracks. Station icons appear from zoom 11 onward; `is_important` stations are three times larger. All layers share the MapLibre camera, and the map keeps the existing OSM raster tiles.

The map reports hovered and clicked vehicle IDs plus the selected station feature to the UI. The `activeFilters` passed to `TransitMap` use the backend identifiers `nationalExpress`, `national`, `regional`, and `suburban`. ICE/IC toggles two products together; Regional and S-Bahn each toggle one. Filtering immediately affects vehicles and tracks without a backend refresh. Search selections and map clicks both select a whole journey; closing the panel clears its highlight. The tooltip and panel resolve each vehicle ID against the latest SSE snapshot, showing the next station, destination, route, scheduled/expected times, and delay. Clicking a station opens its departure board, which refreshes every 15 seconds. Times use a 24-hour Europe/Berlin clock, with positive delays highlighted in red.

## Run locally

Use Python 3.10+ and Node.js 22.18+ (Node 24 also works). The existing lockfile is for npm. From the project root, run the services in separate terminals:

```sh
cd microservice
npm ci
```

```sh
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 5
```

```sh
cd frontend
cp .env.example .env.local
npm ci
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173/>. The example frontend environment sets `VITE_API_URL=/api` and a server-side `API_PROXY_TARGET` for the local backend. Vite removes `/api` before forwarding stream and JSON requests, so local browser calls remain same-origin. Change `API_PROXY_TARGET` in `frontend/.env.local` if the backend runs elsewhere. Git ignores downloaded caches in `data/`, so a fresh clone must populate them from the public providers before the full network is available. The backend can start while either public provider is down. Live vehicles, newly requested full journeys, and map tiles require access to their external services. No API key is required by the current default clients.

The backend also exposes interactive API documentation at <http://127.0.0.1:8000/docs>. The frontend has `npm run preview` for a local preview of a built bundle; it uses the same configured API proxy.

## Configuration

| Environment variable | Used by | Default | Meaning |
| --- | --- | --- | --- |
| `HAFAS_RADAR_SCRIPT` | Backend | `microservice/hafas_radar.js` | Absolute or relative path to the local Node radar adapter. Relative paths resolve from the backend process working directory. |
| `NODE_BINARY` | Backend | `node` | Node.js executable used by `asyncio.create_subprocess_exec`. |
| `HAFAS_USER_AGENT` | Node adapter | `BahnOpticon/1.0 (local HAFAS radar subprocess)` | HAFAS client user agent. Deployments should set a project URL or contact address. |
| `HAFAS_BASE_URL` | Backend | `https://v6.vbb.transport.rest` | VBB-compatible base URL used for departure boards and stop matching. |
| `OVERPASS_URL` | Backend | `https://maps.mail.ru/osm/tools/overpass/api/interpreter` | Full Overpass interpreter endpoint for railway ways and station/halt nodes. |
| `BAHNOPTICON_DATA_DIR` | Backend | Project `data/` directory | Disk cache location for tracks, stations, and borders. Set it to a writable persistent directory in deployments. |
| `CORS_ORIGINS` | Backend | `http://localhost:5173,http://localhost:5174` | Comma-separated browser origins allowed to call the backend directly. The normal Vite proxy is same-origin and does not need this. |
| `VITE_API_URL` | Frontend | Empty (same origin); local example `/api` | Build-time base URL or path prefix for every stream and JSON request. Set an HTTPS URL for a separately hosted backend, or a reverse-proxy prefix. |
| `API_PROXY_TARGET` | Vite dev/preview | Unset; local example `http://127.0.0.1:8000` | Server-side target used when `VITE_API_URL` is a path prefix. Never included in the browser bundle. |

Export backend variables before launching Uvicorn. Vite reads frontend variables from its environment or `frontend/.env.local`; restart Vite after changing them. `VITE_API_URL` is embedded in the browser bundle and must not contain credentials. A separately hosted frontend needs an allowed CORS origin and an HTTPS `VITE_API_URL` when served over HTTPS. In deployment, the reverse proxy must allow long-lived, unbuffered SSE responses. The in-memory poller and snapshot are process-local, so run one backend worker unless that state is redesigned for multiple workers.

## API and data contract

### `GET /stream`

Returns `text/event-stream`. Each `data:` event contains an entire GeoJSON `FeatureCollection`, not a patch and not a WebSocket message. A new client receives the latest live or simulated snapshot immediately if one exists. An empty collection clears all vehicles. The server emits a comment heartbeat about every 15 seconds when there is no data event. Simulated collections add top-level `"is_mock": true` and `"properties": {"is_mock": true}` on each vehicle feature; live collections omit those flags.

Example payload (illustrative coordinates and timestamp):

```json
{
  "type": "FeatureCollection",
  "generated_at": 1770000000.0,
  "features": [
    {
      "type": "Feature",
      "id": "trip-123",
      "properties": {
        "trip_id": "trip-123",
        "duration_ms": 15000,
        "start": [13.4, 52.5],
        "end": [13.41, 52.5],
        "route_status": "routed",
        "route": "S7 Potsdam Hbf",
        "next_station": "Charlottenburg",
        "scheduled_time": "2026-02-02T12:03:00+01:00",
        "expected_time": "2026-02-02T12:03:30+01:00",
        "line_name": "S7",
        "product": "suburban",
        "destination": "Potsdam Hbf",
        "delay_minutes": 0.5
      },
      "geometry": {
        "type": "LineString",
        "coordinates": [
          [13.4, 52.5],
          [13.4, 52.52],
          [13.41, 52.52],
          [13.41, 52.5]
        ]
      }
    }
  ]
}
```

Coordinates are `[longitude, latitude]` in decimal degrees. `generated_at` is the snapshot generation time in Unix seconds, not the time a client receives or replays the event. `start` and `end` remain available as the previous and current observed/snapped positions. For `route_status: "routed"`, geometry is a `LineString` from `start` through track vertices to `end`; for `route_status: "straight"`, it is a two-point `LineString` directly connecting observations when no reliable track route exists. The vehicle's latest position is the final coordinate. An initial or stationary trip has Point geometry. A newly seen trip has identical start/end positions and `duration_ms: 0`. `route` is the provider's full route label when present, falling back to the line name. `next_station` is the first named stop with a future arrival in `nextStopovers`, or a future departure when no arrival is available. The ÖBB radar bridge can include already passed stops, so the backend skips those before selecting the next stop and its delay. `destination` is the last named stopover, falling back to the provider's direction. The stream does not infer a previous station from stopovers. `scheduled_time` is the planned next-stop arrival/departure, and `expected_time` is its realtime arrival/departure. Valid times remain full ISO-8601 strings with their original UTC offsets; missing or timezone-naive times are `null`. The UI displays these times in Berlin local time on a 24-hour clock. The backend caps later segment durations at 60,000 ms; the frontend caps the displayed transition at 30,000 ms. `delay_minutes` is a signed number (including fractions), defaulting to zero for missing or invalid delay telemetry. Positive means late, negative means early, and zero means on time.

### `GET /health`

Returns a JSON object such as `{"status":"ok","upstream":"live","last_success":1770000000.0}`. `status` indicates that the API process is responding. `upstream` is `starting`, `live`, or `unavailable`; it remains `unavailable` while simulated snapshots are streaming because the upstream radar is still down. `last_success` is the last real radar poll time as a Unix timestamp or `null`. This endpoint does not assert that Overpass geometry or map tiles are available.

### `GET /geometry`

Returns railway ways as a GeoJSON `FeatureCollection`. On startup, the backend reads `data/tracks_cache_nationwide.geojson` first and serves it while its routing graph rebuilds; it does not query Overpass when that cache is valid. The current cache contains 129,531 features. On a cold start without that file, four Germany quadrants are queried sequentially for `railway=rail` ways tagged `highspeed=yes` or `usage=main`, with 60-second query and HTTP limits per quadrant. Successful quadrants are checkpointed in `data/track_quadrants/`; duplicate OSM way IDs are merged before the full GeoJSON is atomically saved. The older Berlin-area cache remains a partial fallback if a quadrant fails. Each feature is a `LineString` in `[longitude, latitude]` order with `properties.product` matching the map filters. Fresh mainline ways are classified as `nationalExpress`; this describes the track, not the only service that may use it. If no valid geometry is available, the endpoint returns `503` with `Retry-After: 60`. `GET /tracks` is an equivalent alias.

### `GET /stations`

Returns a GeoJSON `FeatureCollection` of Overpass nodes tagged `railway=station` or `railway=halt`. Each `Point` has an `osm:node:<id>` feature ID and `properties.station_id`, a name, railway tags, and `is_important: true` when its name contains `Hbf`, `Hauptbahnhof`, or `Centraal` (case-insensitive). The startup loader first restores `data/stations_cache.geojson`, then attempts an Overpass refresh; successful responses atomically update the file. The current cache has been refreshed with nationwide station nodes. The endpoint returns `503` with `Retry-After: 60` only when neither disk nor upstream supplies usable data. Departure lookup remains VBB-based and is not guaranteed for stations outside its network.

### `GET /trip/{trip_id}`

Runs `microservice/hafas_trip.js` on demand. The Node adapter calls the ÖBB HAFAS client's `trip(tripId, {polyline: true})`; FastAPI converts its ordered Point polyline into one GeoJSON `Feature` with `LineString` geometry, `trip_id`, `line_name`, and `destination`. URLs must percent-encode opaque trip IDs. Successful responses are cached in memory for five minutes, and the subprocess has a 45-second deadline. Invalid IDs return `400`; unavailable geometry or provider failures return `404` or `502`, and timeouts return `504`.

### `GET /borders`

Returns a cached GeoJSON `FeatureCollection` of simplified German outlines as `MultiLineString` features. `properties.admin_level` is `"2"` for the national boundary and `"4"` for the 16 Bundesländer. The 241 KB cache in `data/borders_cache.geojson` was built from pinned [geoBoundaries Germany ADM0 and ADM1 files](https://www.geoboundaries.org/api.html), sourced from Germany's Federal Agency for Cartography and Geodesy (BKG), under Data license Germany – Attribution – Version 2.0. If the file is absent, startup downloads those pinned public files and saves a new cache. The map credits geoBoundaries/BKG alongside OSM.

### `GET /departures/{station_id}` and `GET /station/{station_id}/departures`

Fetches and returns up to the next 10 departures from VBB's HAFAS `/stops/{id}/departures` endpoint. Pass a numeric VBB stop ID directly, or pass a station feature's `osm:node:<id>` route key. On the first click for an OSM node, the backend matches a nearby VBB stop by name, rail products, and distance, then caches that mapping in memory. OSM and VBB IDs belong to different providers, so some OSM nodes may have no nearby matching VBB stop and return `404`. The compact JSON list includes `trip_id`, `line_name`, `product`, `direction`, `expected_time`, `scheduled_time`, legacy `when`/`planned_when` aliases, `delay_minutes` (or `null`), platforms, and `cancelled`. Both paths are equivalent. Results are cached for 15 seconds per VBB stop. Invalid IDs return `400`; upstream not-found, rate-limit, timeout, and other failures are mapped to `404`, `503`, `504`, and `502` respectively.

## Outages and freshness

Node startup failures, execution timeouts, unsuccessful exits, empty output, and invalid radar JSON start the track-based simulator. Polling retries with exponential backoff from 5 seconds up to 60 seconds; successful polling stops simulation and resets the backoff. Track and station loaders restore disk caches before attempting refreshes and retry failed Overpass calls after 60 seconds. A 504 cannot erase already loaded geometry. Borders do not depend on Overpass. SSE uses one pending-message slot per client, so slow clients get the newest snapshot rather than an unbounded queue. Async tasks, child processes, and HTTP clients are closed at shutdown; graph construction, mock movement, route calculations, and JSON processing run off the FastAPI event loop.

The browser reconnects a dropped SSE connection automatically. It marks a snapshot older than 60 seconds as stale, using `generated_at` so a replayed cached message does not look fresh. Until the first successful poll there is no snapshot to display, even if the SSE connection itself is open. The UI shows connection, invalid-update, reconnecting, and stale states, plus the source update time where available.

## Tests and useful commands

```sh
cd microservice
npm test
```

```sh
cd backend
.venv/bin/python -m unittest discover -v
```

```sh
cd frontend
npm test
npm run lint
npm run build
```

## Development MCP tools

Project-scoped Codex MCP servers are defined in [`.codex/config.toml`](.codex/config.toml), with a portable JSON mirror at [`.codex/mcp.json`](.codex/mcp.json). They provide structured reasoning, read-only PostgreSQL/PostGIS queries, and live documentation fetching. Copy [`.env.example`](.env.example) to `.env`, configure `DATABASE_URL`, install `uv`/`uvx`, and run:

```sh
scripts/setup_mcps.sh
```

The checker validates the configuration, safely reads `DATABASE_URL` without sourcing arbitrary `.env` shell code, performs MCP handshakes, and runs `PostGIS_Version()` through the PostgreSQL MCP tool. Detailed agent workflows and troubleshooting are in [`docs/MCP.md`](docs/MCP.md). Start a new Codex session or restart the IDE extension after changing the configuration, then use `/mcp` to inspect the loaded tools.

The backend tests cover normalization, malformed data, route selection and disconnection, track classification, Overpass railway/station queries, station GeoJSON conversion, static cache responses, OSM-to-VBB departure resolution, trajectory state, event-loop isolation, retry timing, cached SSE replay, bounded client queues, outage recovery, and task cleanup. Frontend tests cover route parsing, distance-based interpolation, `TripsLayer` path/timestamp preparation, source timestamps, stable/bounded icon slots, and departure response parsing. These tests use controlled data and do not require public API access. With the backend running, `backend/test_ws.py` is a **manual SSE smoke script** despite its legacy filename; it waits for five snapshots.

## Code map

| Path | Responsibility |
| --- | --- |
| `backend/main.py` | FastAPI lifespan, normalization, polling, broadcast, static-data caches, and HTTP endpoints. |
| `backend/hafas_client.py` | Async Node radar/trip subprocess management, trip polyline conversion, and VBB station-departure requests. |
| `microservice/hafas_radar.js`, `microservice/hafas_trip.js` | Nationwide ÖBB-profile radar query and on-demand full journey polyline adapters. |
| `backend/osm_client.py` | Async Overpass railway/station queries, station-node conversion, and atomic geometry disk caches. |
| `backend/borders.py`, `data/borders_cache.geojson` | Pinned geoBoundaries source, simplified national/state line conversion, and static border cache. |
| `data/tracks_cache_nationwide.geojson`, `data/track_quadrants/`, `data/stations_cache.geojson` | Complete nationwide track GeoJSON, retryable raw Overpass quadrant checkpoints, and nationwide station GeoJSON. The older `data/tracks_cache.geojson` remains a small fallback; `data/tracks_cache.overpass.json` preserves original track node IDs after a successful fetch. |
| `backend/station_client.py` | On-demand OSM-to-VBB stop matching for departure boards; also contains a legacy VBB grid-discovery helper that is no longer used by `/stations`. |
| `backend/engine.py` | Indexed track graph, snapping, shortest-path routing, trip trajectory state, and connected-node mock traffic. |
| `.codex/config.toml` | Project-scoped Codex MCP server configuration. |
| `scripts/setup_mcps.sh` | MCP prerequisites, configuration, handshake, and PostGIS connectivity checks. |
| `docs/MCP.md` | MCP setup, agent workflows, spatial SQL examples, and troubleshooting. |
| `frontend/src/transit.ts` | GeoJSON validation, stopover metadata, station parsing, and stable icon-slot reconciliation. |
| `frontend/src/useVehicleStream.ts` | EventSource lifecycle, freshness, and connection state. |
| `frontend/src/TransitMap.tsx` | MapLibre/Deck.gl map, border/track/station layers, selected journey highlight, timed route trails, product-colored vehicle markers/icons, and picking callbacks. |
| `frontend/src/TransitPanels.tsx` | Cursor tooltip, vehicle timing panel, station departure board, and legend. |
| `frontend/src/departures.ts` | Validation and normalization of departure board rows. |
| `frontend/src/transitPresentation.ts` | Product labels and Berlin-time/delay formatting. |
| `frontend/public/vehicle-icons.svg` | Vehicle glyph atlas; the nationwide map currently uses only its white rail icon. |
| `frontend/src/App.tsx` | Filters, search, feed status, map interaction state, and panel selection. |
| `frontend/vite.config.ts` | Development/preview API proxy and MapLibre worker handling. |

## Known limits and next work

- The frontend initializes over Germany and hides station icons below zoom 11, but nationwide clustering and production-scale rendering benchmarks remain. The strict Node radar product mask excludes buses, trams, ferries, U-Bahn, `regionalExpress`, and provider-specific product names outside the four configured strings. Simulation requires a usable track graph, which may come from disk or Overpass.
- The path is the shortest connection in the loaded OSM graph, not a confirmed itinerary from HAFAS. Parallel tracks, service direction, missing junction data, and branches can make the estimated path differ from the train's actual trip. The graph covers only ways returned for the fixed query area.
- Snapping uses a 250 m cutoff and matches the broad rail mode, but does not yet match a train's specific line to a way. The optimized track query omits light-rail ways, so suburban services generally take the straight-line fallback. Disconnected or unsnapped updates also use straight segments; these are visual interpolation, not verified physical itineraries. The nationwide graph takes several seconds to rebuild from disk, although `/tracks` is served before that rebuild finishes; no production-scale routing benchmark has been completed.
- Static geometry has a disk cache, but vehicle states, trip lookup cache, and per-client queues remain in one backend process. There is no Redis cache, database, persistent vehicle history, or multi-worker coordination. Refreshing the nationwide track cache still depends on public Overpass availability; existing cache files remain available during outages.
- The UI uses local React state and has no saved filters, full stop list, or journey search. The highlighted trip polyline is fetched only for one selected vehicle and requires its HAFAS trip request to succeed. The board shows departures for one selected station at a time and depends on the live VBB endpoint. There is no committed automated end-to-end browser suite in the repository.
- The 10,000-vehicle, stable-memory, and 60 fps goals in [AGENTS.md](AGENTS.md) have not been benchmarked or guaranteed. MapLibre/Deck.gl bundles are also large enough for Vite to warn during production builds.
- Public HAFAS, Overpass, and OSM tile availability varies. When providers fail, use `/health` and the UI's feed status to distinguish an unavailable upstream from a frontend startup problem.
