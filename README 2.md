# BahnOpticon

## Objective
BahnOpticon is a high-performance, real-time geospatial dashboard designed to track the entire Deutsche Bahn (DB) rail network and local public transit across Germany. It renders moving SVG icons of transit vehicles (ICE, RE, S-Bahn, buses) overlaid on an OpenStreetMap (OSM) base layer, augmented with live delay data and a React-based floating "island" UI for intuitive searching and filtering.

This describes the long-term goal. The current implementation is a **Berlin-area prototype** using VBB HAFAS radar: up to 150 upstream results per poll, filtered to S-Bahn, U-Bahn, and regional trains. Nationwide coverage, GTFS-Realtime, distributed caching, route-following interpolation, and the 10,000-vehicle/60fps target are not yet implemented or benchmarked.

## Run locally

Requires Python 3.10+ and Node.js 22.18+ (Node 24 recommended). Start each service in its own terminal from the project directory:

```sh
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 5
```

```sh
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open the local URL printed by Vite. Vite proxies `/stream` and `/health` to port 8000, so the browser does not need a hard-coded backend origin. The backend starts even when Overpass or HAFAS is unavailable; the UI shows connection/freshness status instead of inventing vehicle data. Public transit and map tile providers require network access.

The floating controls support line/destination search, service-type toggles, hiding delayed vehicles, and reset. Click an icon or a search result to inspect its destination and delay. Missing delays are displayed as unavailable. Data older than 60 seconds is marked stale, including cached snapshots replayed to a new connection.

## Configuration

| Variable | Service | Default / purpose |
| --- | --- | --- |
| `HAFAS_BASE_URL` | Backend | `https://v6.vbb.transport.rest`; HAFAS-compatible `/radar` provider |
| `OVERPASS_URL` | Backend | `https://overpass-api.de/api/interpreter`; static railway geometry |
| `CORS_ORIGINS` | Backend | Comma-separated origins; defaults to `http://localhost:5173,http://localhost:5174` |
| `VITE_STREAM_URL` | Frontend | `/stream`; optional full SSE URL when hosting the backend separately |

Backend variables must be exported in the process environment. Frontend variables may be placed in `frontend/.env.local` and are read at build time. For a separate backend origin, configure its CORS origins and use HTTPS when the frontend is served over HTTPS. Production hosting must proxy `/stream` without response buffering and allow long-lived connections; the Vite proxy is only for development/preview. Use a single backend worker: its shared snapshot cache is in-process, so multiple workers would independently poll providers.

## Stream contract and recovery

`GET /stream` returns Server-Sent Events (SSE), not WebSocket frames. Each `data:` event is a complete GeoJSON `FeatureCollection`, with `generated_at` in Unix seconds. Each Point feature has a stable trip `id` and properties `trip_id`, `line_name`, `product`, `destination`, `delay_minutes` (number or null), `start`, `end` (longitude/latitude pairs), and `duration_ms`. An empty collection clears vehicles; omitted trips are removed. Delay values preserve fractions of a minute.

The backend polls every 15 seconds after a successful request, retains the last successful snapshot during failures, backs off on errors and respects `Retry-After`. New clients receive the cached snapshot immediately. Slow clients keep only the newest pending snapshot, with SSE heartbeats every 15 seconds. Geometry processing and JSON serialization run off the async event loop. `GET /health` reports process status, upstream status, and the last successful poll time.

The map and icons share one camera. Icons use GPU attribute transitions with stable trip slots so reordered snapshots do not animate one train into another. Static tracks currently snap only observed endpoints to geometry; animation between points follows straight segments and does not yet route along tracks. Overpass failure temporarily leaves positions unsnapped. Rendering and browser memory at 10,000 vehicles still need a dedicated benchmark.

## Checks

```sh
cd backend
.venv/bin/python -m unittest -v test_backend
```

```sh
cd frontend
npm test
npm run lint
npm run build
```

If using the existing `backend/venv` environment, substitute `venv/bin/python`. `backend/test_ws.py` is a manual SSE smoke check despite its legacy name; with the backend running, it reads five snapshots. The automated tests use controlled data and do not depend on public provider availability.

## Architecture & Tech Stack

### Backend
- **Core Framework:** Python (FastAPI) for asynchronous API endpoints and SSE streaming.
- **Data Integration:** 
  - VBB HAFAS radar for current vehicle locations and delays.
  - GTFS-Realtime and nationwide DB providers are planned integrations.
  - Overpass API for querying static track geometry and routing pathways.

### Frontend
- **Core Framework:** React for component-based UI and state management.
- **Geospatial Visualization:** Deck.gl for GPU vehicle icons and MapLibre GL JS for OpenStreetMap tiles.

## Key Technical Challenges

1. **API Rate Limiting & Aggregation:** Orchestrating high-frequency polling against restrictive HAFAS/DB endpoints without encountering HTTP 429 Too Many Requests errors. Requires robust caching, request debouncing, and distributed polling strategies.
2. **Geospatial Interpolation & Track Snapping:** Vehicles are polled roughly every 15 seconds, plus request time. Smooth interpolation along complete railway routes remains a goal beyond the current endpoint transitions.
3. **Browser Memory & Rendering Optimization:** Tracking thousands of concurrent transit vehicles necessitates efficient garbage collection and rendering pipelines. Moving DOM/canvas elements must be strictly managed to prevent memory leaks and main-thread blocking, leveraging Web Workers and GPU acceleration wherever possible.
