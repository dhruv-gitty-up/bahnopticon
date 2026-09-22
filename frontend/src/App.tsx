import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { TransitMap } from './TransitMap';
import type { TrackProduct, TransitMapInteractions } from './TransitMap';
import { StationDeparturePanel, TransitLegend, VehicleDetailsPanel, VehicleHoverTooltip } from './TransitPanels';
import { findVehicleById } from './transit';
import type { StationFeature } from './transit';
import { formatTime } from './transitPresentation';
import { useVehicleStream } from './useVehicleStream';
import './App.css';

const TRACK_PRODUCTS: readonly TrackProduct[] = [
  'nationalExpress', 'national', 'regional', 'suburban',
];
const FILTER_GROUPS: readonly { label: string; products: readonly TrackProduct[] }[] = [
  { label: 'ICE / IC', products: ['nationalExpress', 'national'] },
  { label: 'Regional', products: ['regional'] },
  { label: 'S-Bahn', products: ['suburban'] },
];
const isTrackProduct = (value: string): value is TrackProduct =>
  TRACK_PRODUCTS.includes(value as TrackProduct);

function App() {
  const { vehicles, status, updatedAt } = useVehicleStream();
  const [query, setQuery] = useState('');
  const [activeFilters, setActiveFilters] = useState<TrackProduct[]>(() => [...TRACK_PRODUCTS]);
  const [hideDelayed, setHideDelayed] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedStation, setSelectedStation] = useState<StationFeature | null>(null);
  const [activePanel, setActivePanel] = useState<'vehicle' | 'station' | null>(null);
  const [mapInteractions, setMapInteractions] = useState<TransitMapInteractions>({
    hoveredVehicleId: null, clickedVehicleId: null, clickedStation: null,
  });
  // Resolve interaction identity from each SSE snapshot instead of retaining stale feature objects.
  const hoveredVehicle = findVehicleById(vehicles, mapInteractions.hoveredVehicleId);
  const [filtersOpen, setFiltersOpen] = useState(true);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  const tooltipFrameRef = useRef(0);

  const placeTooltip = useCallback(() => {
    const tooltip = tooltipRef.current;
    const point = pointerRef.current;
    if (!tooltip || !point) return;
    const width = tooltip.offsetWidth;
    const height = tooltip.offsetHeight;
    const x = Math.max(12, Math.min(point.x + 16, window.innerWidth - width - 12));
    const y = Math.max(12, Math.min(point.y + 16, window.innerHeight - height - 12));
    tooltip.style.transform = `translate3d(${Math.round(x)}px, ${Math.round(y)}px, 0)`;
    tooltip.style.visibility = 'visible';
  }, []);
  const scheduleTooltip = useCallback(() => {
    if (tooltipFrameRef.current) return;
    tooltipFrameRef.current = requestAnimationFrame(() => {
      tooltipFrameRef.current = 0;
      placeTooltip();
    });
  }, [placeTooltip]);
  const onVehiclePointerMove = useCallback((point: { x: number; y: number } | null) => {
    pointerRef.current = point;
    if (point) scheduleTooltip();
    else if (tooltipRef.current) tooltipRef.current.style.visibility = 'hidden';
  }, [scheduleTooltip]);
  useLayoutEffect(() => {
    if (hoveredVehicle) scheduleTooltip();
  }, [hoveredVehicle, scheduleTooltip]);
  useEffect(() => () => cancelAnimationFrame(tooltipFrameRef.current), []);

  const filteredVehicles = useMemo(() => {
    const search = query.trim().toLocaleLowerCase();
    return vehicles.filter(({ properties }) => {
      return isTrackProduct(properties.type) && activeFilters.includes(properties.type)
        && (!hideDelayed || !(properties.delay != null && properties.delay > 0))
        && (!search || `${properties.line} ${properties.destination ?? ''} ${properties.id}`.toLocaleLowerCase().includes(search));
    });
  }, [vehicles, query, activeFilters, hideDelayed]);
  const selectedVehicle = findVehicleById(vehicles, selectedId);
  const onSelect = useCallback((vehicleId: string | null) => {
    setSelectedId(vehicleId);
    setActivePanel(vehicleId ? 'vehicle' : null);
  }, []);
  const onStationSelect = useCallback((station: StationFeature) => {
    setSelectedStation(station);
    setSelectedId(null);
    setActivePanel('station');
  }, []);
  const onInteractionChange = useCallback((next: TransitMapInteractions) => setMapInteractions(next), []);
  const closePanel = useCallback(() => {
    setActivePanel(null);
    setSelectedId(null);
    setSelectedStation(null);
  }, []);

  const toggleTrackProducts = (products: readonly TrackProduct[]) => {
    setActiveFilters(previous => {
      const next = new Set(previous);
      const enabled = products.every(product => next.has(product));
      for (const product of products) {
        if (enabled) next.delete(product);
        else next.add(product);
      }
      return TRACK_PRODUCTS.filter(candidate => next.has(candidate));
    });
  };
  const resetFilters = () => {
    setQuery('');
    setActiveFilters([...TRACK_PRODUCTS]);
    setHideDelayed(false);
  };
  const hasFilters = Boolean(query || activeFilters.length !== TRACK_PRODUCTS.length || hideDelayed);
  const connectionLabel = status === 'connected'
    ? 'Stream connected'
    : status === 'connecting'
      ? 'Connecting to transit feed…'
      : status === 'stale'
        ? 'Feed delayed; showing older vehicle data'
        : status === 'invalid'
          ? 'Invalid feed update; showing last valid data'
          : 'Connection lost; reconnecting…';

  return (
    <main className="app" aria-label="BahnOpticon transit dashboard">
      <TransitMap
        vehicles={filteredVehicles}
        activeFilters={activeFilters}
        selectedVehicleId={selectedId}
        onSelect={onSelect}
        onStationSelect={onStationSelect}
        onInteractionChange={onInteractionChange}
        onVehiclePointerMove={onVehiclePointerMove}
      />
      {hoveredVehicle && (
        <VehicleHoverTooltip vehicle={hoveredVehicle} tooltipRef={tooltipRef} />
      )}
      <div className="dashboard-overlay">
        <section className="island controls" aria-label="Transit controls">
          <div className="panel-heading">
            <h1>BahnOpticon</h1>
            <button
              className="text-button"
              type="button"
              aria-expanded={filtersOpen}
              aria-controls="transit-filters"
              onClick={() => setFiltersOpen(open => !open)}
            >
              {filtersOpen ? 'Hide filters' : 'Show filters'}
            </button>
          </div>
          <p className={`connection connection--${status}`} role="status">
            <span className="status-dot" aria-hidden="true" />{connectionLabel}
          </p>
          <div id="transit-filters" hidden={!filtersOpen}>
            <label className="search-label" htmlFor="vehicle-search">Find a line or destination</label>
            <input
              id="vehicle-search"
              type="search"
              placeholder="e.g. ICE 74 or München Hbf"
              value={query}
              onChange={event => setQuery(event.target.value)}
            />
            <fieldset>
              <legend>Vehicle types</legend>
              <div className="product-filters">
                {FILTER_GROUPS.map(({ label, products }) => (
                  <button
                    type="button"
                    className="product-toggle"
                    key={label}
                    aria-pressed={products.every(product => activeFilters.includes(product))}
                    onClick={() => toggleTrackProducts(products)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </fieldset>
            <label className="checkbox-label">
              <input type="checkbox" checked={hideDelayed} onChange={event => setHideDelayed(event.target.checked)} />
              Hide delayed vehicles
            </label>
            <div className="filter-summary">
              <span>{filteredVehicles.length} of {vehicles.length} vehicles</span>
              {hasFilters && <button className="text-button" type="button" onClick={resetFilters}>Reset filters</button>}
            </div>
            {query.trim() && filteredVehicles.length > 0 && (
              <ul className="search-results" aria-label="Matching vehicles">
                {filteredVehicles.slice(0, 6).map(vehicle => (
                  <li key={vehicle.properties.id}>
                    <button type="button" onClick={() => onSelect(vehicle.id)} aria-pressed={selectedId === vehicle.properties.id}>
                      <strong className="route-number">{vehicle.properties.line}</strong>
                      <span>{vehicle.properties.destination || 'Destination unavailable'}</span>
                    </button>
                  </li>
                ))}
                {filteredVehicles.length > 6 && <li className="search-hint">Refine your search to see more specific matches.</li>}
              </ul>
            )}
          </div>
          {updatedAt != null && (
            <p className="updated-time">Feed updated {formatTime(updatedAt)} (Berlin)</p>
          )}
          {status === 'connected' && filteredVehicles.length === 0 && (
            <p className="empty-message">{vehicles.length ? 'No vehicles match these filters.' : 'Waiting for vehicles in the feed.'}</p>
          )}
        </section>
        {activePanel === 'vehicle' && selectedVehicle && (
          <VehicleDetailsPanel vehicle={selectedVehicle} feedLive={status === 'connected'} onClose={closePanel} />
        )}
        {activePanel === 'station' && selectedStation && (
          <StationDeparturePanel key={selectedStation.id} station={selectedStation} onClose={closePanel} />
        )}
      </div>
      <TransitLegend />
    </main>
  );
}

export default App;
