import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

import {createClient} from 'hafas-client';
import {profile as oebbProfile} from 'hafas-client/p/oebb/index.js';

export const BBOX = Object.freeze({
  north: 55.0,
  west: 5.8,
  south: 47.2,
  east: 15.0,
});
export const CENTER = Object.freeze({latitude: 51.1, longitude: 10.4});
export const QUADRANTS = Object.freeze([
  Object.freeze({north: BBOX.north, west: BBOX.west, south: CENTER.latitude, east: CENTER.longitude}), // NW
  Object.freeze({north: BBOX.north, west: CENTER.longitude, south: CENTER.latitude, east: BBOX.east}), // NE
  Object.freeze({north: CENTER.latitude, west: BBOX.west, south: BBOX.south, east: CENTER.longitude}), // SW
  Object.freeze({north: CENTER.latitude, west: CENTER.longitude, south: BBOX.south, east: BBOX.east}), // SE
]);

export const RAIL_PRODUCTS = new Set([
  'nationalExpress',
  'national',
  'regional',
  'suburban',
]);

// Explicitly disable every other product, since partial filters inherit defaults.
export const PRODUCT_FILTER = Object.freeze(Object.fromEntries(
  oebbProfile.products.map(({id}) => [id, RAIL_PRODUCTS.has(id)]),
));

export function filterRailMovements(payload) {
  if (!payload || typeof payload !== 'object' || !Array.isArray(payload.movements)) {
    throw new TypeError('HAFAS radar response must contain a movements array');
  }
  return {
    ...payload,
    movements: payload.movements.filter(movement => (
      movement
      && typeof movement === 'object'
      && movement.line
      && typeof movement.line === 'object'
      && RAIL_PRODUCTS.has(movement.line.product)
    )),
  };
}

function isInGermanyBounds(movement) {
  const latitude = movement.location?.latitude;
  const longitude = movement.location?.longitude;
  return Number.isFinite(latitude) && Number.isFinite(longitude)
    && latitude >= 47.27 && latitude <= BBOX.north
    && longitude >= BBOX.west && longitude <= BBOX.east;
}

export function mergeRadarResults(payloads) {
  const seenTripIds = new Set();
  const movements = [];
  for (const payload of payloads) {
    for (const movement of filterRailMovements(payload).movements.slice(0, 1000)) {
      const tripId = movement.tripId;
      if (typeof tripId !== 'string' || !tripId || seenTripIds.has(tripId)) continue;
      seenTripIds.add(tripId);
      movements.push(movement);
    }
  }
  return {movements: movements.filter(movement => (
    movement.line.product === 'nationalExpress'
    || movement.line.product === 'national'
    || isInGermanyBounds(movement)
  ))};
}

export async function queryRadar(client) {
  const userAgent = process.env.HAFAS_USER_AGENT
    || 'BahnOpticon/1.0 (local HAFAS radar subprocess)';
  const radarClient = client || createClient(oebbProfile, userAgent);
  const payloads = await Promise.all(QUADRANTS.map(bounds => radarClient.radar(bounds, {
    results: 1000,
    duration: 30,
    frames: 3,
    products: PRODUCT_FILTER,
    polylines: false,
  })));
  return mergeRadarResults(payloads);
}

async function main() {
  const payload = await queryRadar();
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

const isMain = process.argv[1]
  && resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (isMain) {
  main().catch(error => {
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    process.stderr.write(`HAFAS radar failed: ${message}\n`);
    process.exitCode = 1;
  });
}
