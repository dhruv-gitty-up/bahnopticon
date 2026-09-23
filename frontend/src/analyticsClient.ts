import { apiUrl } from './api';
import { parseSevenDayAnalytics, parseVehicleAnalytics } from './analytics';
import type { SevenDayAnalytics, VehicleAnalytics } from './analytics';

let analyticsRequest: Promise<SevenDayAnalytics> | null = null;

export function loadSevenDayAnalytics(): Promise<SevenDayAnalytics> {
  if (analyticsRequest) return analyticsRequest;
  analyticsRequest = fetch(apiUrl('/analytics/7day'))
    .then(async response => {
      if (!response.ok) throw new Error(`Analytics: ${response.status}`);
      return parseSevenDayAnalytics(await response.json());
    })
    .catch(error => {
      analyticsRequest = null;
      throw error;
    });
  return analyticsRequest;
}

export async function loadVehicleAnalytics(lineId: string, signal?: AbortSignal): Promise<VehicleAnalytics> {
  const response = await fetch(apiUrl(`/analytics/vehicle/${encodeURIComponent(lineId)}`), { signal });
  if (!response.ok) throw new Error(`Vehicle analytics: ${response.status}`);
  return parseVehicleAnalytics(await response.json());
}
