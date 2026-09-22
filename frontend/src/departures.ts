export interface Departure {
  tripId: string | null;
  line: string;
  product: string;
  destination: string;
  expectedTime: string | null;
  scheduledTime: string | null;
  delayMinutes: number | null;
  cancelled: boolean;
  platform: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function optionalText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

/** Validate the compact response instead of assuming every upstream row is complete. */
export function parseDepartures(value: unknown): Departure[] {
  if (!Array.isArray(value)) throw new Error('Expected a departures array');
  return value.slice(0, 10).filter(isRecord).map(row => ({
    tripId: optionalText(row.trip_id),
    line: optionalText(row.line_name) ?? 'Unknown',
    product: optionalText(row.product) ?? 'unknown',
    destination: optionalText(row.direction) ?? 'Destination unavailable',
    expectedTime: optionalText(row.expected_time),
    scheduledTime: optionalText(row.scheduled_time),
    delayMinutes: typeof row.delay_minutes === 'number' && Number.isFinite(row.delay_minutes)
      ? row.delay_minutes : null,
    cancelled: row.cancelled === true,
    platform: optionalText(row.platform),
  }));
}
