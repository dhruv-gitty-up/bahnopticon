import { apiUrl } from './api';
import { parseSevenDayAnalytics } from './analytics';
import type { SevenDayAnalytics } from './analytics';

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
