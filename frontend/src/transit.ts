import type { Feature, FeatureCollection, Geometry, LineString } from 'geojson';

export type Coordinates = [number, number];
export type RouteStatus = 'routed' | 'straight' | 'initial' | 'stationary' | 'unavailable' | 'disconnected';

type VehicleGeometry =
  | { type: 'Point'; coordinates: Coordinates }
  | { type: 'LineString'; coordinates: Coordinates[] };

export interface VehicleFeature {
  type: 'Feature';
  id: string;
  geometry: VehicleGeometry;
  properties: {
    id: string;
    line: string;
    type: string;
    delay: number | null;
    destination?: string;
    route: string | null;
    nextStation: string | null;
    scheduledTime: string | null;
    expectedTime: string | null;
    nextStationScheduledTime: string | null;
    nextStationExpectedTime: string | null;
    nextStationDelay: number | null;
    durationMs: number;
    start: Coordinates;
    routeStatus: RouteStatus;
    animationStartedAt: number;
  };
}

export interface StationFeature {
  type: 'Feature';
  id: string;
  geometry: { type: 'Point'; coordinates: Coordinates };
  properties: {
    station_id: string;
    name: string;
    products: Record<string, boolean>;
    is_important: boolean;
  };
}

/** Resolve selection identity from the current snapshot instead of retaining stale feature objects. */
export function findVehicleById(vehicles: readonly VehicleFeature[], vehicleId: string | null): VehicleFeature | null {
  if (vehicleId === null) return null;
  return vehicles.find(vehicle => vehicle.id === vehicleId) ?? null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isCoordinates(value: unknown): value is Coordinates {
  return Array.isArray(value) && value.length === 2
    && typeof value[0] === 'number' && Number.isFinite(value[0]) && Math.abs(value[0]) <= 180
    && typeof value[1] === 'number' && Number.isFinite(value[1]) && Math.abs(value[1]) <= 90;
}

function isGeometry(value: Record<string, unknown>): value is Record<string, unknown> & VehicleGeometry {
  return (value.type === 'Point' && isCoordinates(value.coordinates))
    || (value.type === 'LineString' && Array.isArray(value.coordinates)
      && value.coordinates.length >= 2 && value.coordinates.every(isCoordinates));
}

export function getVehiclePosition(vehicle: VehicleFeature): Coordinates {
  const { geometry } = vehicle;
  return geometry.type === 'Point' ? geometry.coordinates : geometry.coordinates[geometry.coordinates.length - 1];
}

export interface MeasuredRoute {
  coordinates: Coordinates[];
  cumulativeMeters: number[];
  totalMeters: number;
}

export interface TimedTrip {
  vehicle: VehicleFeature;
  route: MeasuredRoute;
  path: Coordinates[];
  timestamps: number[];
  durationMs: number;
}

/** Precompute distance once per snapshot; frame sampling then costs O(log route vertices). */
export function measureRoute(coordinates: Coordinates[]): MeasuredRoute {
  const cumulativeMeters = [0];
  for (let i = 1; i < coordinates.length; i++) {
    const [lon1, lat1] = coordinates[i - 1];
    const [lon2, lat2] = coordinates[i];
    const latitude = (lat1 + lat2) * Math.PI / 360;
    const dx = (lon2 - lon1) * Math.cos(latitude);
    const dy = lat2 - lat1;
    cumulativeMeters.push(cumulativeMeters[i - 1] + Math.hypot(dx, dy) * 111_195);
  }
  return { coordinates, cumulativeMeters, totalMeters: cumulativeMeters[cumulativeMeters.length - 1] };
}

/** TripsLayer uses small, relative millisecond timestamps to retain GPU precision. */
export function createTimedTrip(vehicle: VehicleFeature): TimedTrip | null {
  if (vehicle.geometry.type !== 'LineString' || vehicle.properties.durationMs <= 0) return null;
  const route = measureRoute(vehicle.geometry.coordinates);
  if (route.totalMeters <= 0) return null;
  const durationMs = vehicle.properties.durationMs;
  return {
    vehicle,
    route,
    path: route.coordinates,
    timestamps: route.cumulativeMeters.map(meters => meters / route.totalMeters * durationMs),
    durationMs,
  };
}

export function sampleRoute(route: MeasuredRoute, progress: number, target: Coordinates = [0, 0]): Coordinates {
  const { coordinates, cumulativeMeters, totalMeters } = route;
  if (progress <= 0 || totalMeters <= 0) {
    target[0] = coordinates[0][0]; target[1] = coordinates[0][1];
    return target;
  }
  if (progress >= 1) {
    target[0] = coordinates[coordinates.length - 1][0];
    target[1] = coordinates[coordinates.length - 1][1];
    return target;
  }
  const distance = progress * totalMeters;
  let low = 1;
  let high = cumulativeMeters.length - 1;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (cumulativeMeters[middle] < distance) low = middle + 1;
    else high = middle;
  }
  const length = cumulativeMeters[low] - cumulativeMeters[low - 1];
  const fraction = length > 0 ? (distance - cumulativeMeters[low - 1]) / length : 1;
  target[0] = coordinates[low - 1][0] + (coordinates[low][0] - coordinates[low - 1][0]) * fraction;
  target[1] = coordinates[low - 1][1] + (coordinates[low][1] - coordinates[low - 1][1]) * fraction;
  return target;
}

/** Use the poll time, not SSE receipt time: a reconnect can replay an old cached snapshot. */
export function getSnapshotTimestamp(value: unknown, receivedAt: number): number {
  if (isRecord(value) && typeof value.generated_at === 'number'
    && Number.isFinite(value.generated_at) && value.generated_at > 0) {
    return value.generated_at * 1_000;
  }
  return receivedAt;
}

/** The stream sends complete GeoJSON snapshots, never incremental feature updates. */
export function parseVehicleSnapshot(value: unknown, animationStartedAt = Date.now()): VehicleFeature[] {
  if (!isRecord(value) || value.type !== 'FeatureCollection' || !Array.isArray(value.features)) {
    throw new Error('Expected a GeoJSON FeatureCollection');
  }

  const vehicles = new Map<string, VehicleFeature>();
  for (const item of value.features) {
    if (!isRecord(item) || item.type !== 'Feature' || !isRecord(item.geometry)
      || !isGeometry(item.geometry) || !isRecord(item.properties)) continue;

    const properties = item.properties;
    const rawId = properties.trip_id ?? item.id;
    if (!(typeof rawId === 'string' && rawId.length > 0)
      && !(typeof rawId === 'number' && Number.isFinite(rawId))) continue;
    const id = String(rawId);
    const geometry: VehicleGeometry = item.geometry.type === 'Point'
      ? { type: 'Point', coordinates: [...item.geometry.coordinates] }
      : { type: 'LineString', coordinates: item.geometry.coordinates.map(point => [...point]) };
    const coordinates = geometry.type === 'Point'
      ? geometry.coordinates : geometry.coordinates[geometry.coordinates.length - 1];
    const duration = properties.duration_ms;
    const delay = properties.delay_minutes;
    const nextStationDelay = properties.next_station_delay_minutes;

    vehicles.set(id, {
      type: 'Feature', id,
      geometry,
      properties: {
        id,
        line: typeof properties.line_name === 'string' ? properties.line_name : 'Unknown',
        type: typeof properties.product === 'string' ? properties.product : 'unknown',
        delay: typeof delay === 'number' && Number.isFinite(delay) ? delay : null,
        destination: typeof properties.destination === 'string' ? properties.destination : undefined,
        route: typeof properties.route === 'string' ? properties.route : null,
        nextStation: typeof properties.next_station === 'string' ? properties.next_station : null,
        scheduledTime: typeof properties.scheduled_time === 'string' ? properties.scheduled_time : null,
        expectedTime: typeof properties.expected_time === 'string' ? properties.expected_time : null,
        nextStationScheduledTime: typeof properties.next_station_scheduled_time === 'string'
          ? properties.next_station_scheduled_time : null,
        nextStationExpectedTime: typeof properties.next_station_expected_time === 'string'
          ? properties.next_station_expected_time : null,
        nextStationDelay: typeof nextStationDelay === 'number' && Number.isFinite(nextStationDelay)
          ? nextStationDelay : null,
        durationMs: typeof duration === 'number' && Number.isFinite(duration)
          ? Math.max(0, Math.min(30_000, duration)) : 15_000,
        start: isCoordinates(properties.start)
          ? [properties.start[0], properties.start[1]] : coordinates,
        routeStatus: geometry.type === 'LineString'
          ? properties.route_status === 'straight' ? 'straight' : 'routed'
          : properties.route_status === 'initial' || properties.route_status === 'stationary'
            || properties.route_status === 'unavailable' || properties.route_status === 'disconnected'
            ? properties.route_status : 'unavailable',
        animationStartedAt,
      },
    });
  }

  // Preserve the last good snapshot if an upstream schema change invalidates every feature.
  if (value.features.length > 0 && vehicles.size === 0) {
    throw new Error('The snapshot contains no valid vehicle geometry');
  }
  return [...vehicles.values()];
}

/** Keep the station feature intact so click handlers can pass its ID and metadata onward. */
export function parseStationCollection(value: unknown): StationFeature[] {
  if (!isRecord(value) || value.type !== 'FeatureCollection' || !Array.isArray(value.features)) {
    throw new Error('Expected a station FeatureCollection');
  }
  const stations: StationFeature[] = [];
  for (const item of value.features) {
    if (!isRecord(item) || item.type !== 'Feature' || !isRecord(item.geometry)
      || item.geometry.type !== 'Point' || !isCoordinates(item.geometry.coordinates)
      || !isRecord(item.properties)) continue;
    const id = item.properties.station_id;
    if (typeof id !== 'string' || !id) continue;
    const products = isRecord(item.properties.products)
      ? Object.fromEntries(Object.entries(item.properties.products)
        .filter((entry): entry is [string, boolean] => typeof entry[1] === 'boolean')) : {};
    stations.push({
      type: 'Feature', id,
      geometry: { type: 'Point', coordinates: [...item.geometry.coordinates] },
      properties: {
        station_id: id,
        name: typeof item.properties.name === 'string' ? item.properties.name : 'Unknown station',
        products,
        is_important: item.properties.is_important === true,
      },
    });
  }
  return stations;
}

/** Accept the single journey feature returned by /trip and reject unusable polylines. */
export function parseJourneyFeature(value: unknown): Feature<LineString> {
  const candidate = isRecord(value) && value.type === 'FeatureCollection' && Array.isArray(value.features)
    ? value.features.find((feature: unknown) => isRecord(feature) && isRecord(feature.geometry)
      && feature.geometry.type === 'LineString') : value;
  if (!isRecord(candidate) || candidate.type !== 'Feature' || !isRecord(candidate.geometry)
    || candidate.geometry.type !== 'LineString' || !Array.isArray(candidate.geometry.coordinates)
    || candidate.geometry.coordinates.length < 2
    || !candidate.geometry.coordinates.every(isCoordinates)) {
    throw new Error('Expected a journey LineString Feature');
  }
  return candidate as unknown as Feature<LineString>;
}

export type BorderProperties = { admin_level: '2' | '4'; name: string };

/** Keep only line or polygon boundaries that Deck.gl can stroke. */
export function parseBorderCollection(value: unknown): FeatureCollection<Geometry, BorderProperties> {
  if (!isRecord(value) || value.type !== 'FeatureCollection' || !Array.isArray(value.features)) {
    throw new Error('Expected a border FeatureCollection');
  }
  const features = value.features.flatMap((feature: unknown): Feature<Geometry, BorderProperties>[] => {
    if (!isRecord(feature) || feature.type !== 'Feature' || !isRecord(feature.properties)
      || (feature.properties.admin_level !== '2' && feature.properties.admin_level !== '4')
      || !isRecord(feature.geometry)
      || !['LineString', 'MultiLineString', 'Polygon', 'MultiPolygon'].includes(String(feature.geometry.type))
      || !Array.isArray(feature.geometry.coordinates)) return [];
    let geometry = feature.geometry as unknown as Geometry;
    // The backend intentionally stores border exteriors as lines. Closed rings
    // can be reconstructed here so Deck.gl can fill a selected Bundesland.
    if (geometry.type === 'MultiLineString') {
      const polygons = geometry.coordinates
        .filter(ring => ring.length >= 4 && ring.every(isCoordinates))
        .map(ring => [ring]);
      if (polygons.length) geometry = { type: 'MultiPolygon', coordinates: polygons };
    }
    return [{
      type: 'Feature',
      id: typeof feature.id === 'string' || typeof feature.id === 'number' ? feature.id : undefined,
      properties: {
        admin_level: feature.properties.admin_level,
        name: typeof feature.properties.name === 'string' ? feature.properties.name : 'Unknown region',
      },
      geometry,
    }];
  });
  return { type: 'FeatureCollection', features };
}

export interface VehicleSlots {
  snapshot: VehicleFeature[];
  slots: (VehicleFeature | null)[];
  generation: number;
}

/** Keep instance indexes stable across polls and bound retired slots in long sessions. */
export function reconcileVehicleSlots(previous: VehicleSlots, snapshot: VehicleFeature[]): VehicleSlots {
  const remaining = new Map(snapshot.map(vehicle => [vehicle.id, vehicle]));
  const slots = previous.slots.map(vehicle => {
    if (!vehicle) return null;
    const next = remaining.get(vehicle.id) ?? null;
    remaining.delete(vehicle.id);
    return next;
  });
  // Do not assign a retired index to a different trip within the same layer generation.
  slots.push(...remaining.values());
  if (slots.length > snapshot.length * 2) {
    // Bound retained CPU/GPU storage; the next layer generation starts with compact slots.
    return { snapshot, slots: [...snapshot], generation: previous.generation + 1 };
  }
  return { snapshot, slots, generation: previous.generation };
}
