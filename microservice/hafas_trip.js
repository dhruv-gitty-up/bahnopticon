import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

import {createClient} from 'hafas-client';
import {profile as oebbProfile} from 'hafas-client/p/oebb/index.js';

export async function queryTrip(tripId, client) {
  if (typeof tripId !== 'string' || !tripId.trim()) {
    throw new TypeError('A non-empty tripId argument is required');
  }

  const hafas = client || createClient(
    oebbProfile,
    process.env.HAFAS_USER_AGENT || 'BahnOpticon/1.0 (local HAFAS trip subprocess)',
  );
  return hafas.trip(tripId, {polyline: true});
}

async function main() {
  const payload = await queryTrip(process.argv[2]);
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

const isMain = process.argv[1]
  && resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (isMain) {
  main().catch(error => {
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    process.stderr.write(`HAFAS trip failed: ${message}\n`);
    process.exitCode = 1;
  });
}
