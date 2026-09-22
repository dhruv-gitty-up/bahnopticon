import { useEffect, useState } from 'react';
import { apiUrl } from './api';
import { getSnapshotTimestamp, parseVehicleSnapshot } from './transit';
import type { VehicleFeature } from './transit';

export type StreamStatus = 'connecting' | 'connected' | 'reconnecting' | 'invalid' | 'stale';

const STALE_AFTER_MS = 60_000;

export function useVehicleStream() {
  const [vehicles, setVehicles] = useState<VehicleFeature[]>([]);
  const [status, setStatus] = useState<StreamStatus>('connecting');
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);

  useEffect(() => {
    let snapshotTime: number | null = null;
    const freshness = (): StreamStatus => snapshotTime === null ? 'connecting'
      : Date.now() - snapshotTime > STALE_AFTER_MS ? 'stale' : 'connected';
    const source = new EventSource(apiUrl('/stream'));
    source.onopen = () => setStatus(freshness());
    source.onmessage = event => {
      try {
        const payload: unknown = JSON.parse(event.data);
        const receivedAt = Date.now();
        const sourceTime = getSnapshotTimestamp(payload, receivedAt);
        // Cached replay after an outage is already historical; place icons at its observed endpoint.
        const animationStartedAt = receivedAt - sourceTime > STALE_AFTER_MS
          ? receivedAt - 30_000 : receivedAt;
        const snapshot = parseVehicleSnapshot(payload, animationStartedAt);
        snapshotTime = sourceTime;
        setVehicles(snapshot);
        setUpdatedAt(snapshotTime);
        setStatus(freshness());
      } catch {
        // Keep the last valid snapshot visible, and surface the failure to the UI.
        setStatus('invalid');
      }
    };
    // EventSource handles reconnection; avoid creating competing connections or retry timers.
    source.onerror = () => setStatus('reconnecting');
    const timer = window.setInterval(() => {
      setStatus(current => current === 'connected' || current === 'stale' ? freshness() : current);
    }, 5_000);
    return () => {
      window.clearInterval(timer);
      source.close();
    };
  }, []);

  return { vehicles, status, updatedAt };
}
