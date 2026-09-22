import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import test from 'node:test';

import {queryTrip} from './hafas_trip.js';

test('requests the full trip polyline and returns the response unchanged', async () => {
  const response = {
    trip: {
      id: 'trip-1',
      polyline: {type: 'FeatureCollection', features: [
        {type: 'Feature', geometry: {type: 'Point', coordinates: [13.4, 52.5]}, properties: {}},
      ]},
    },
  };
  const calls = [];
  const client = {trip: async (...args) => {
    calls.push(args);
    return response;
  }};

  assert.equal(await queryTrip('trip-1', client), response);
  assert.deepEqual(calls, [['trip-1', {polyline: true}]]);
});

test('requires a trip ID and fails without JSON on stdout', async () => {
  await assert.rejects(queryTrip(' ', {trip: () => { throw new Error('should not call'); }}), /tripId/);

  const result = spawnSync(process.execPath, ['hafas_trip.js'], {cwd: import.meta.dirname, encoding: 'utf8'});
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /HAFAS trip failed: TypeError: A non-empty tripId argument is required/);
});
