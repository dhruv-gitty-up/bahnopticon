export interface NetworkPerformance {
  product: string;
  label: string;
  onTimeProbability: number;
  averageDelayMinutes: number;
}

export interface RegionalPerformance {
  bundesland: string;
  regionalOnTimePercentage: number;
  suburbanOnTimePercentage: number;
}

export interface SevenDayAnalytics {
  periodDays: number;
  isMock: boolean;
  networkPerformance: NetworkPerformance[];
  regionalPerformance: RegionalPerformance[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function parseSevenDayAnalytics(value: unknown): SevenDayAnalytics {
  if (!isRecord(value) || !Array.isArray(value.network_performance)
    || !Array.isArray(value.regional_performance)) {
    throw new Error('Invalid analytics response');
  }
  const networkPerformance = value.network_performance.flatMap(row => {
    if (!isRecord(row) || typeof row.product !== 'string' || typeof row.label !== 'string'
      || typeof row.on_time_probability !== 'number' || !Number.isFinite(row.on_time_probability)
      || typeof row.average_delay_minutes !== 'number' || !Number.isFinite(row.average_delay_minutes)) return [];
    return [{ product: row.product, label: row.label,
      onTimeProbability: Math.max(0, Math.min(1, row.on_time_probability)),
      averageDelayMinutes: Math.max(0, row.average_delay_minutes) }];
  });
  const regionalPerformance = value.regional_performance.flatMap(row => {
    if (!isRecord(row) || typeof row.bundesland !== 'string'
      || typeof row.regional_on_time_percentage !== 'number'
      || !Number.isFinite(row.regional_on_time_percentage)
      || typeof row.suburban_on_time_percentage !== 'number'
      || !Number.isFinite(row.suburban_on_time_percentage)) return [];
    return [{ bundesland: row.bundesland,
      regionalOnTimePercentage: Math.max(0, Math.min(100, row.regional_on_time_percentage)),
      suburbanOnTimePercentage: Math.max(0, Math.min(100, row.suburban_on_time_percentage)) }];
  });
  if (!networkPerformance.length || !regionalPerformance.length) {
    throw new Error('Analytics response contains no usable rows');
  }
  return {
    periodDays: typeof value.period_days === 'number' ? value.period_days : 7,
    isMock: value.is_mock === true,
    networkPerformance,
    regionalPerformance,
  };
}
