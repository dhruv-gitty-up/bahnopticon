import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BBOX, CENTER, QUADRANTS, filterRailMovements, mergeRadarResults, PRODUCT_FILTER, queryRadar,
} from './hafas_radar.js';

test('uses the nationwide Germany bounding box', () => {
  assert.deepEqual(BBOX, {north: 55, west: 5.8, south: 47.2, east: 15});
  assert.deepEqual(CENTER, {latitude: 51.1, longitude: 10.4});
  assert.deepEqual(QUADRANTS, [
    {north: 55, west: 5.8, south: 51.1, east: 10.4},
    {north: 55, west: 10.4, south: 51.1, east: 15},
    {north: 51.1, west: 5.8, south: 47.2, east: 10.4},
    {north: 51.1, west: 10.4, south: 47.2, east: 15},
  ]);
});

test('enables only ICE, IC, regional and S-Bahn products', () => {
  assert.deepEqual(
    Object.entries(PRODUCT_FILTER).filter(([, enabled]) => enabled).map(([product]) => product),
    ['nationalExpress', 'national', 'regional', 'suburban'],
  );
});

test('defensively removes local and malformed movements', () => {
  const payload = filterRailMovements({
    realtimeDataUpdatedAt: 123,
    movements: [
      {line: {product: 'nationalExpress'}},
      {line: {product: 'national'}},
      {line: {product: 'regionalExpress'}},
      {line: {product: 'regional'}},
      {line: {product: 'suburban'}},
      {line: {product: 'subway'}},
      {line: {product: 'bus'}},
      null,
    ],
  });
  assert.equal(payload.realtimeDataUpdatedAt, 123);
  assert.deepEqual(payload.movements.map(({line}) => line.product), [
    'nationalExpress', 'national', 'regional', 'suburban',
  ]);
});

test('merges quadrant movements and deduplicates by tripId', () => {
  const movement = (tripId, product) => ({
    tripId, line: {product}, location: {latitude: 52.5, longitude: 13.4},
  });
  const result = mergeRadarResults([
    {movements: [movement('ice-1', 'nationalExpress'), movement('re-2', 'regional')]},
    {movements: [movement('ice-1', 'nationalExpress'), movement('bus-3', 'bus')]},
    {movements: [movement('s-4', 'suburban'), movement(null, 'national')]},
    {movements: [movement('ic-5', 'national')]},
  ]);
  assert.deepEqual(result.movements.map(({tripId}) => tripId), [
    'ice-1', 're-2', 's-4', 'ic-5',
  ]);
  assert.equal(result.movements[1].line.product, 'regional');
});

test('keeps local trains only within the specified bounds, and international ICE/IC', () => {
  const movement = (tripId, product, latitude, longitude) => ({
    tripId, line: {product}, location: {latitude, longitude},
  });
  const result = mergeRadarResults([{movements: [
    movement('berlin-s', 'suburban', 52.5, 13.4),
    movement('south-edge-re', 'regional', 47.27, 10),
    movement('north-edge-s', 'suburban', 55, 10),
    movement('austrian-s', 'suburban', 47.26, 11.4),
    movement('east-re', 'regional', 48.2, 16.3),
    movement('bad-re', 'regional', '52.5', 13.4),
    movement('international-ice', 'nationalExpress', 46.9, 9.5),
    movement('international-ic', 'national', 48.2, 16.3),
  ]}]);
  assert.deepEqual(result.movements.map(({tripId}) => tripId), [
    'berlin-s', 'south-edge-re', 'north-edge-s',
    'international-ice', 'international-ic',
  ]);
});

test('caps the merged response at 4000 movements', () => {
  const payloads = Array.from({length: 4}, (_, quadrant) => ({
    movements: Array.from({length: 1001}, (_, index) => ({
      tripId: `${quadrant}-${index}`,
      line: {product: 'nationalExpress'},
    })),
  }));
  const result = mergeRadarResults(payloads);
  assert.equal(result.movements.length, 4000);
});

test('starts all four 1000-result ÖBB radar queries concurrently', async () => {
  const calls = [];
  const pending = [];
  const client = {radar(bounds, options) {
    calls.push({bounds, options});
    return new Promise(resolve => pending.push(resolve));
  }};
  const resultPromise = queryRadar(client);
  assert.equal(calls.length, 4);
  assert.deepEqual(calls.map(({bounds}) => bounds), QUADRANTS);
  assert.ok(calls.every(({options}) => options.results === 1000
    && options.products === PRODUCT_FILTER));
  pending.forEach((resolve, index) => resolve({
    movements: [{tripId: `trip-${index}`, line: {product: 'nationalExpress'}}],
  }));
  const result = await resultPromise;
  assert.deepEqual(result.movements.map(({tripId}) => tripId), [
    'trip-0', 'trip-1', 'trip-2', 'trip-3',
  ]);
});
