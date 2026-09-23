import assert from 'node:assert/strict';
import test from 'node:test';
import { parseDepartures } from './departures.ts';
import { parseSevenDayAnalytics } from './analytics.ts';
import { createTimedTrip, findVehicleById, getSnapshotTimestamp, getVehiclePosition, measureRoute, parseBorderCollection, parseJourneyFeature, parseStationCollection, parseVehicleSnapshot, reconcileVehicleSlots, sampleRoute } from './transit.ts';
import type { VehicleSlots } from './transit.ts';
import { formatDateTime, formatTime } from './transitPresentation.ts';

const feature = (id: string, longitude = 13.4) => ({
  type: 'Feature',
  geometry: { type: 'Point', coordinates: [longitude, 52.5] },
  properties: { trip_id: id, line_name: 'S1', product: 'suburban', duration_ms: 15_000 },
});
const snapshot = (...features: unknown[]) => parseVehicleSnapshot({ type: 'FeatureCollection', features });

test('cached SSE replay preserves the source timestamp rather than appearing newly polled', () => {
  const original = { generated_at: 1_700_000_000 };
  assert.equal(getSnapshotTimestamp(original, 1_700_001_000_000), 1_700_000_000_000);
  assert.equal(getSnapshotTimestamp({}, 123), 123);
});

test('normalizes real backend fields and distinguishes missing delay from zero', () => {
  const [vehicle] = snapshot(feature('a'));
  assert.equal(vehicle.id, 'a');
  assert.equal(vehicle.properties.line, 'S1');
  assert.equal(vehicle.properties.delay, null);
  assert.deepEqual(vehicle.geometry.coordinates, [13.4, 52.5]);
  const raw = feature('b');
  Object.assign(raw.properties, { delay_minutes: 0, duration_ms: Infinity });
  assert.equal(snapshot(raw)[0].properties.delay, 0);
  assert.equal(snapshot(raw)[0].properties.durationMs, 15_000);
});

test('retains stopover timing and full route metadata for vehicle selection', () => {
  const raw = feature('bus-1');
  Object.assign(raw.properties, {
    route: 'M41 Hauptbahnhof via Sonnenallee',
    next_station: 'Sonnenallee',
    destination: 'Berlin Hauptbahnhof',
    scheduled_time: '2026-09-17T23:30:00+02:00',
    expected_time: '2026-09-17T23:31:00+02:00',
    next_station_scheduled_time: '2026-09-17T23:30:00+02:00',
    next_station_expected_time: '2026-09-17T23:31:00+02:00',
    next_station_delay_minutes: 1,
  });
  const { properties } = snapshot(raw)[0];
  assert.equal(properties.route, 'M41 Hauptbahnhof via Sonnenallee');
  assert.equal(properties.nextStation, 'Sonnenallee');
  assert.equal(properties.destination, 'Berlin Hauptbahnhof');
  assert.equal(properties.scheduledTime, '2026-09-17T23:30:00+02:00');
  assert.equal(properties.expectedTime, '2026-09-17T23:31:00+02:00');
  assert.equal(properties.nextStationScheduledTime, '2026-09-17T23:30:00+02:00');
  assert.equal(properties.nextStationExpectedTime, '2026-09-17T23:31:00+02:00');
  assert.equal(properties.nextStationDelay, 1);
});

test('renders offset-aware Berlin times on a 24-hour clock, including midnight', () => {
  assert.match(formatDateTime('2026-09-17T23:30:00+02:00'), /23:30/);
  assert.equal(formatTime('2026-09-17T23:30:00+02:00'), '23:30');
  assert.equal(formatTime('2026-09-17T22:30:00Z'), '00:30');
  assert.match(formatDateTime('2026-09-17T22:30:00Z'), /00:30/);
  assert.equal(formatTime(Date.parse('2026-09-17T23:30:00+02:00')), '23:30');
  assert.doesNotMatch(formatDateTime('2026-09-17T23:30:00+02:00'), /AM|PM/i);
});

test('resolves a selected vehicle ID from the latest SSE snapshot', () => {
  const firstRaw = feature('live-1');
  Object.assign(firstRaw.properties, { delay_minutes: 1, expected_time: '2026-09-17T23:31:00+02:00' });
  const nextRaw = feature('live-1', 13.5);
  Object.assign(nextRaw.properties, { delay_minutes: 6, expected_time: '2026-09-17T23:36:00+02:00' });
  const first = snapshot(firstRaw);
  const next = snapshot(nextRaw);

  assert.equal(findVehicleById(first, 'live-1')?.properties.delay, 1);
  assert.equal(findVehicleById(next, 'live-1')?.properties.delay, 6);
  assert.equal(findVehicleById(next, 'live-1')?.properties.expectedTime, '2026-09-17T23:36:00+02:00');
  assert.equal(findVehicleById(next, null), null);
  assert.equal(findVehicleById(next, 'gone'), null);
});

test('keeps station IDs and metadata available to station click handlers', () => {
  const stations = parseStationCollection({ type: 'FeatureCollection', features: [
    { type: 'Feature', id: '900100002', geometry: { type: 'Point', coordinates: [13.405, 52.52] },
      properties: { station_id: '900100002', name: 'Berlin Hbf', products: { suburban: true, bus: false }, is_important: true } },
    { type: 'Feature', geometry: { type: 'Point', coordinates: [181, 52.52] },
      properties: { station_id: 'invalid', name: 'Invalid' } },
  ] });
  assert.equal(stations.length, 1);
  assert.equal(stations[0].properties.station_id, '900100002');
  assert.deepEqual(stations[0].properties.products, { suburban: true, bus: false });
  assert.equal(stations[0].properties.is_important, true);
});

test('accepts selected journey geometry and rejects malformed routes', () => {
  const trip = { type: 'Feature', properties: {}, geometry: {
    type: 'LineString', coordinates: [[10.1, 51.1], [10.2, 51.2], [10.3, 51.3]],
  } };
  assert.deepEqual(parseJourneyFeature(trip).geometry.coordinates, trip.geometry.coordinates);
  assert.throws(() => parseJourneyFeature({ ...trip, geometry: {
    type: 'LineString', coordinates: [[10.1, 51.1], [181, 51.2]],
  } }));
});

test('keeps national and state border levels for map styling', () => {
  const border = (level: string) => ({ type: 'Feature', properties: { admin_level: level, name: 'Berlin' },
    geometry: { type: 'LineString', coordinates: [[8, 50], [9, 51]] } });
  const result = parseBorderCollection({ type: 'FeatureCollection', features: [
    border('2'), border('4'), border('6'), { ...border('2'), geometry: { type: 'Point', coordinates: [8, 50] } },
  ] });
  assert.deepEqual(result.features.map(feature => feature.properties.admin_level), ['2', '4']);
  assert.deepEqual(result.features.map(feature => feature.properties.name), ['Berlin', 'Berlin']);
});

test('reconstructs closed border lines as fillable polygons for region highlighting', () => {
  const result = parseBorderCollection({ type: 'FeatureCollection', features: [{
    type: 'Feature', properties: { admin_level: '4', name: 'Bayern' },
    geometry: { type: 'MultiLineString', coordinates: [
      [[10, 48], [11, 48], [11, 49], [10, 48]],
    ] },
  }] });
  assert.equal(result.features[0].geometry.type, 'MultiPolygon');
  assert.equal(result.features[0].properties.name, 'Bayern');
});

test('normalizes the seven-day analytics dashboard contract', () => {
  const analytics = parseSevenDayAnalytics({
    period_days: 7, is_mock: true,
    network_performance: [
      { product: 'regional', label: 'Regional', on_time_probability: 0.78,
        average_delay_minutes: 7.2 },
    ],
    regional_performance: [
      { bundesland: 'Berlin', regional_on_time_percentage: 74.2,
        suburban_on_time_percentage: 89.1 },
    ],
  });
  assert.equal(analytics.networkPerformance[0].onTimeProbability, 0.78);
  assert.equal(analytics.regionalPerformance[0].bundesland, 'Berlin');
  assert.equal(analytics.isMock, true);
});

test('accepts the live departure contract and keeps expected time and delay', () => {
  const board = parseDepartures([
    { trip_id: 'trip-1', line_name: 'S9', product: 'suburban', direction: 'Flughafen BER',
      scheduled_time: '2026-09-18T00:00:00+02:00', expected_time: '2026-09-18T00:03:00+02:00',
      delay_minutes: 3, cancelled: false, platform: '3' },
    { line_name: 'M41', product: 'bus', direction: 'Hauptbahnhof',
      scheduled_time: '2026-09-18T00:04:00+02:00', expected_time: null,
      delay_minutes: null, cancelled: true },
  ]);
  assert.equal(board.length, 2);
  assert.equal(board[0].expectedTime, '2026-09-18T00:03:00+02:00');
  assert.equal(board[0].delayMinutes, 3);
  assert.equal(board[0].destination, 'Flughafen BER');
  assert.equal(board[1].cancelled, true);
  assert.equal(board[1].expectedTime, null);
  assert.throws(() => parseDepartures({ departures: [] }));
});

test('rejects malformed snapshots and coordinates, tolerates a bad feature, deduplicates IDs', () => {
  assert.throws(() => parseVehicleSnapshot({ id: 'untyped' }));
  assert.throws(() => snapshot(feature('bad', 181)));
  assert.throws(() => snapshot(feature('bad', NaN)));
  assert.equal(snapshot(feature('a'), feature('bad', 181), feature('a', 13.5)).length, 1);
  assert.equal(snapshot(feature('a'), feature('a', 13.5))[0].geometry.coordinates[0], 13.5);
  assert.deepEqual(snapshot(), []);
});

test('accepts routed LineString geometry and locates the current vehicle at its final coordinate', () => {
  const raw = {
    ...feature('route'),
    geometry: { type: 'LineString', coordinates: [[13.4, 52.5], [13.4, 52.52], [13.41, 52.52], [13.41, 52.5]] },
    properties: { ...feature('route').properties, route_status: 'routed', start: [13.4, 52.5], end: [13.41, 52.5] },
  };
  const [vehicle] = parseVehicleSnapshot({ type: 'FeatureCollection', features: [raw] }, 1234);
  assert.equal(vehicle.geometry.type, 'LineString');
  assert.equal(vehicle.properties.routeStatus, 'routed');
  assert.equal(vehicle.properties.animationStartedAt, 1234);
  assert.deepEqual(getVehiclePosition(vehicle), [13.41, 52.5]);
  assert.throws(() => snapshot({ ...raw, geometry: { type: 'LineString', coordinates: [[13.4, 52.5], [181, 52.52]] } }));
  assert.throws(() => snapshot({ ...raw, geometry: { type: 'LineString', coordinates: [[13.4, 52.5]] } }));
});

test('preserves straight-line fallback status for sparse national tracks', () => {
  const raw = {
    ...feature('fallback'),
    geometry: { type: 'LineString', coordinates: [[10, 50], [10.1, 50.1]] },
    properties: { ...feature('fallback').properties, route_status: 'straight',
      start: [10, 50], end: [10.1, 50.1] },
  };
  const [vehicle] = parseVehicleSnapshot({ type: 'FeatureCollection', features: [raw] }, 1234);
  assert.equal(vehicle.properties.routeStatus, 'straight');
  assert.deepEqual(getVehiclePosition(vehicle), [10.1, 50.1]);
});

test('samples a curved route by distance, not vertex index or straight-line endpoint interpolation', () => {
  const route = measureRoute([[13.4, 52.5], [13.4, 52.52], [13.41, 52.52], [13.41, 52.5]]);
  assert.deepEqual(sampleRoute(route, 0), [13.4, 52.5]);
  assert.deepEqual(sampleRoute(route, 1), [13.41, 52.5]);
  const quarter = sampleRoute(route, 0.25);
  assert.ok(quarter[0] < 13.4001 && quarter[1] > 52.51 && quarter[1] < 52.52);
  const middle = sampleRoute(route, 0.5);
  assert.ok(middle[0] > 13.4 && middle[0] < 13.41 && Math.abs(middle[1] - 52.52) < 1e-9);
  const threeQuarters = sampleRoute(route, 0.75);
  assert.ok(threeQuarters[0] > 13.4099 && threeQuarters[1] > 52.51 && threeQuarters[1] < 52.52);
});

test('converts a routed LineString to distance-scaled TripsLayer timestamps in the 30-second window', () => {
  const raw = {
    ...feature('route'),
    geometry: { type: 'LineString', coordinates: [[13.4, 52.5], [13.4, 52.52], [13.41, 52.52], [13.41, 52.5]] },
    properties: { ...feature('route').properties, duration_ms: 60_000, route_status: 'routed' },
  };
  const [vehicle] = snapshot(raw);
  const trip = createTimedTrip(vehicle);
  assert.ok(trip);
  assert.equal(trip.durationMs, 30_000);
  assert.deepEqual(trip.path, raw.geometry.coordinates);
  assert.equal(trip.timestamps.length, trip.path.length);
  assert.equal(trip.timestamps[0], 0);
  assert.equal(trip.timestamps.at(-1), 30_000);
  assert.ok(trip.timestamps[1] > 0 && trip.timestamps[1] < trip.timestamps[2]);
  assert.ok(trip.timestamps[2] < 30_000);
  const iconAtTurn = sampleRoute(trip.route, trip.timestamps[2] / trip.durationMs);
  assert.deepEqual(iconAtTurn, trip.path[2]);
  assert.equal(createTimedTrip(snapshot(feature('point'))[0]), null);
});

test('preserves vehicle slots during reorder and removal to prevent cross-vehicle interpolation', () => {
  const initial = snapshot(feature('a'), feature('b'), feature('c'));
  const previous = { snapshot: initial, slots: initial, generation: 0 };
  const next = reconcileVehicleSlots(previous, snapshot(feature('c', 13.6), feature('a', 13.7)));
  assert.deepEqual(next.slots.map(vehicle => vehicle?.id ?? null), ['a', null, 'c']);
  assert.equal(next.slots[0]?.geometry.coordinates[0], 13.7);
  assert.equal(next.generation, 0);
});

test('does not reuse removed slots and bounds memory across replacement snapshots', () => {
  let state: VehicleSlots = { snapshot: [], slots: [], generation: 0 };
  state = reconcileVehicleSlots(state, snapshot(feature('a'), feature('b')));
  state = reconcileVehicleSlots(state, snapshot(feature('b'), feature('c')));
  assert.deepEqual(state.slots.map(vehicle => vehicle?.id ?? null), [null, 'b', 'c']);
  for (let i = 0; i < 100; i++) {
    state = reconcileVehicleSlots(state, snapshot(feature(`vehicle-${i}`)));
    assert.ok(state.slots.length <= state.snapshot.length * 2);
  }
  assert.ok(state.generation > 0);
  state = reconcileVehicleSlots(state, []);
  assert.deepEqual(state.slots, []);
});
