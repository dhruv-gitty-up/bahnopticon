# BahnOpticon frontend

React, TypeScript, MapLibre and DeckGL frontend. See the [project README](../README.md) for setup, the SSE schema, configuration, current coverage and limitations.

```sh
npm ci
cp .env.example .env.local
npm run dev -- --host 127.0.0.1
```

The example uses `VITE_API_URL=/api` for all stream and JSON requests. Vite proxies that prefix to `API_PROXY_TARGET` and strips `/api` before forwarding. Set the target in `.env.local` to your local backend; its example value uses port 8000. For a separately hosted backend, set `VITE_API_URL` to its full HTTPS base URL at build time and allow the frontend origin in backend CORS. A same-origin production reverse proxy can also serve the chosen path prefix. The local `.env.local` is ignored by Git.

For Vercel, select `frontend` as the project root and set `VITE_API_URL` in the Vercel project environment to the deployed backend's HTTPS origin (for example, `https://api.example.com`). The local `API_PROXY_TARGET` is only for Vite development and preview; Vercel does not use it. `vercel.json` serves `index.html` for SPA deep links while deployed static files retain filesystem precedence. Allow the Vercel frontend origin in backend `CORS_ORIGINS`, including the `/stream` SSE request.

```sh
npm test
npm run lint
npm run build
```

The tests run with Node's built-in TypeScript support (Node 22.18+ or 24). Vehicle rendering lives in `src/TransitMap.tsx`, stream lifecycle in `src/useVehicleStream.ts`, schema/identity handling in `src/transit.ts`, and controls in `src/App.tsx`.
