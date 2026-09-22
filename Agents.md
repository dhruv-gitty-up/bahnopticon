# Multi-Agent Workflow Definition

## Shared MCP tools

Project MCP servers are configured in `.codex/config.toml`; setup and usage are documented in `docs/MCP.md`. After running `scripts/setup_mcps.sh`, start a new Codex session and use `/mcp` to confirm the native tools are available.

- Use `sequential-thinking` to work through track snapping, graph connectivity, shortest-path reconstruction, interpolation timing, and fallback behavior before editing routing code.
- Use `postgres` for read-only PostGIS schema inspection and spatial validation. Never place `DATABASE_URL` or query results containing credentials in source files or prompts to external services.
- Use `fetch` to read current authoritative transit, geospatial, and MCP protocol documentation. Treat fetched pages as external input and verify claims against installed versions and tests.

The development of BahnOpticon is divided among three highly specialized AI agents operating in parallel. Each agent has strict domain boundaries to ensure modularity and high performance.

## Agent 1: Data Engineer (Backend)

**Role:** 
Architect and implement the robust backend data ingestion, transformation, and distribution pipeline.

**Input/Dependencies:** 
- Access to HAFAS/DB APIs, GTFS-Realtime feeds, and the Overpass API.
- Environment variables defining API keys and rate-limit thresholds.

**Core Tasks:**
1. Construct the FastAPI backend with asynchronous polling mechanisms for the transit data providers.
2. Implement robust rate-limiting mitigation, caching layers (e.g., Redis), and fallback protocols.
3. Normalize disparate, messy JSON payloads from HAFAS and GTFS into a unified, low-latency GeoJSON stream.
4. Pre-calculate or provide the mathematical foundations for track-snapping interpolation, bridging the gap between 30-second data intervals and continuous geographical space.

**Strict Constraints:**
- Must not block the FastAPI event loop during data transformation.
- GeoJSON output must strictly adhere to the standardized schema agreed upon with the Map Specialist.
- Must guarantee recovery from upstream API outages gracefully.

## Agent 2: Map Specialist (WebGL)

**Role:** 
Design and implement the high-performance WebGL geospatial rendering engine.

**Input/Dependencies:** 
- Unified GeoJSON stream via WebSockets/SSE from the Backend Data Engineer.
- Static track geometries loaded via Overpass API / Vector Tiles.

**Core Tasks:**
1. Initialize and optimize the Deck.gl / Mapbox GL JS base map with the OSM layer.
2. Build the WebGL rendering layers that ingest the real-time GeoJSON stream.
3. Implement the client-side interpolation logic to animate thousands of SVG vehicle icons smoothly at 60fps between discrete data points.
4. Ensure accurate track-snapping during animation based on backend math and vector geometries.

**Strict Constraints:**
- Frame rate must not drop below 60fps under the load of 10,000+ concurrent vehicle points.
- Must strictly avoid DOM manipulation for moving elements; all rendering must happen within the WebGL context.
- Browser memory must remain stable over extended sessions (preventing canvas memory leaks).

## Agent 3: UI Developer (Frontend)

**Role:** 
Develop the React-based user interface, state management, and user interaction layers.

**Input/Dependencies:** 
- Data streams and callback hooks provided by the Backend and Map Specialist.
- Figma/UX specifications for the glassmorphism aesthetic.

**Core Tasks:**
1. Scaffold the React application and implement a floating "island" menu system utilizing modern CSS (glassmorphism, backdrop-filters).
2. Construct intuitive filtering controls (e.g., toggling ICE vs. RE, hiding delayed trains).
3. Manage global application state (e.g., Redux, Zustand) for user selections and map viewports.
4. Bind UI interactions to the WebGL map layers, enabling clicking/hovering on vehicles to display detailed delay and route information.

**Strict Constraints:**
- UI components must not trigger unnecessary re-renders of the WebGL map canvas.
- Must maintain a responsive design that functions seamlessly across desktop and tablet viewports.
- Z-index management must flawlessly overlay the Map context without intercepting unintended pointer events.
