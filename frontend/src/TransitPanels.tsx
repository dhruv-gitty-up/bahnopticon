import { useEffect, useState } from 'react';
import type { Ref } from 'react';
import { apiUrl } from './api';
import { parseDepartures } from './departures';
import type { Departure } from './departures';
import type { StationFeature, VehicleFeature } from './transit';
import { delayLabel, formatDateTime, formatTime, PRODUCT_LABELS } from './transitPresentation';

export function VehicleHoverTooltip({ vehicle, tooltipRef }: {
  vehicle: VehicleFeature;
  tooltipRef: Ref<HTMLDivElement>;
}) {
  const { properties } = vehicle;
  return (
    <div className="vehicle-hover-tooltip" ref={tooltipRef} role="tooltip">
      <div className="tooltip-heading">
        <span className="eyebrow">{PRODUCT_LABELS[properties.type] ?? properties.type}</span>
        <strong className="route-number">{properties.line}</strong>
      </div>
      <p className="tooltip-route"><span>Route</span>{properties.route ?? properties.destination ?? 'Unavailable'}</p>
      <p className="tooltip-route"><span>Next stop</span>{properties.nextStation ?? 'Unavailable'}</p>
      <p className="tooltip-route"><span>Destination</span>{properties.destination ?? 'Unavailable'}</p>
      <div className="tooltip-timing">
        <span>Scheduled <strong>{formatDateTime(properties.scheduledTime)}</strong></span>
        <span>Expected <strong>{formatDateTime(properties.expectedTime)}</strong></span>
      </div>
      <p className={properties.delay != null && properties.delay > 0 ? 'delay-text delay-text--late' : 'delay-text'}>
        {delayLabel(properties.delay)}
      </p>
    </div>
  );
}

export function VehicleDetailsPanel({ vehicle, feedLive, onClose }: {
  vehicle: VehicleFeature;
  feedLive: boolean;
  onClose: () => void;
}) {
  const { properties } = vehicle;
  return (
    <section className="island detail-panel vehicle-details" aria-label="Selected vehicle details">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{PRODUCT_LABELS[properties.type] ?? properties.type}</p>
          <h2 className="route-number">{properties.line}</h2>
        </div>
        <button className="text-button" type="button" onClick={onClose} aria-label="Close vehicle details">Close</button>
      </div>
      <p className="destination">{properties.destination || 'Destination unavailable'}</p>
      <p className={properties.delay != null && properties.delay > 0 ? 'large-delay large-delay--late' : 'large-delay'}>
        {delayLabel(properties.delay)}
      </p>
      <dl className="detail-list">
        <div><dt>Full route</dt><dd>{properties.route ?? 'Unavailable'}</dd></div>
        <div><dt>Next station</dt><dd className="station-name">{properties.nextStation ?? 'Unavailable'}</dd></div>
        <div><dt>Destination</dt><dd className="station-name">{properties.destination ?? 'Unavailable'}</dd></div>
        <div><dt>Scheduled</dt><dd>{formatDateTime(properties.scheduledTime)}</dd></div>
        <div><dt>Expected</dt><dd>{formatDateTime(properties.expectedTime)}</dd></div>
      </dl>
      {!feedLive && <p className="empty-message">Showing the last received vehicle information.</p>}
    </section>
  );
}

export function StationDeparturePanel({ station, onClose }: {
  station: StationFeature;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<Departure[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const refresh = async () => {
      try {
        const response = await fetch(apiUrl(`/departures/${encodeURIComponent(station.id)}`), {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`Departures: ${response.status}`);
        const payload: unknown = await response.json();
        const departures = parseDepartures(payload);
        if (!controller.signal.aborted) {
          setRows(departures);
          setError(false);
          setLoading(false);
        }
      } catch {
        if (!controller.signal.aborted) {
          setError(true);
          setLoading(false);
        }
      }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 15_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [station.id]);

  return (
    <section className="island detail-panel station-details" aria-label="Station departure board">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Live departure board</p>
          <h2 className="station-name">{station.properties.name}</h2>
        </div>
        <button className="text-button" type="button" onClick={onClose} aria-label="Close station departures">Close</button>
      </div>
      {loading && <p className="board-message" role="status">Loading departures…</p>}
      {error && <p className="board-message board-message--error" role="status">Live departures are unavailable. Retrying…</p>}
      {!loading && rows.length === 0 && !error && <p className="board-message">No upcoming departures found.</p>}
      {rows.length > 0 && (
        <table className="departure-table">
          <thead><tr><th scope="col">Expected</th><th scope="col">Line</th><th scope="col">Destination</th></tr></thead>
          <tbody>
            {rows.map((departure, index) => (
              <tr key={`${departure.tripId ?? departure.line}-${index}`}>
                <td className="departure-time">
                  <time dateTime={departure.expectedTime ?? departure.scheduledTime ?? undefined}>
                    {formatTime(departure.expectedTime ?? departure.scheduledTime)}
                  </time>
                  {departure.scheduledTime && departure.expectedTime && departure.expectedTime !== departure.scheduledTime && (
                    <span className="scheduled-time">was {formatTime(departure.scheduledTime)}</span>
                  )}
                </td>
                <td><span className={`line-badge line-badge--${departure.product}`}>{departure.line}</span></td>
                <td className="departure-destination">
                  {departure.destination}
                  {departure.cancelled && <span className="departure-note">Cancelled</span>}
                  {!departure.cancelled && departure.delayMinutes != null && departure.delayMinutes > 0 && (
                    <span className="departure-note departure-note--late">{delayLabel(departure.delayMinutes)}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="board-footer">Updates every 15 seconds · Times in Berlin</p>
    </section>
  );
}

export function TransitLegend() {
  return (
    <aside className="transit-legend" aria-label="Vehicle color legend">
      <span className="eyebrow">Map key</span>
      <ul>
        <li><span className="legend-swatch legend-swatch--national" />ICE / IC</li>
        <li><span className="legend-swatch legend-swatch--regional" />Regional</li>
        <li><span className="legend-swatch legend-swatch--suburban" />S-Bahn</li>
      </ul>
    </aside>
  );
}
