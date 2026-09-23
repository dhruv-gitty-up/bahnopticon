import { memo, useCallback, useEffect, useMemo, useState } from 'react';
import MapLibre from 'react-map-gl/maplibre';
import DeckGL from '@deck.gl/react';
import { GeoJsonLayer, IconLayer, ScatterplotLayer } from '@deck.gl/layers';
import { TripsLayer } from '@deck.gl/geo-layers';
import { PathStyleExtension } from '@deck.gl/extensions';
import type { PathStyleExtensionProps } from '@deck.gl/extensions';
import { WebMercatorViewport } from '@deck.gl/core';
import type { MapViewState, PickingInfo } from '@deck.gl/core';
import type { Feature, FeatureCollection, Geometry, LineString } from 'geojson';
import { apiUrl } from './api';
import { createTimedTrip, getVehiclePosition, parseBorderCollection, parseJourneyFeature, parseStationCollection, reconcileVehicleSlots, sampleRoute } from './transit';
import type { BorderProperties, Coordinates, StationFeature, TimedTrip, VehicleFeature, VehicleSlots } from './transit';
import 'maplibre-gl/dist/maplibre-gl.css';

const INITIAL_VIEW_STATE = { longitude: 10.45, latitude: 51.16, zoom: 5.5, pitch: 0, bearing: 0 };
const MAP_STYLE = {
  version: 8 as const,
  sources: {
    osm: {
      type: 'raster' as const,
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> | <a href="https://www.geoboundaries.org/">geoBoundaries / BKG</a>',
    },
  },
  layers: [{ id: 'osm', type: 'raster' as const, source: 'osm' }],
};
const ICON_MAPPING = {
  train: { x: 0, y: 0, width: 48, height: 48, mask: true },
};
const STATION_ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48"><circle cx="24" cy="24" r="20" fill="#fff" fill-opacity=".94" stroke="#17212b" stroke-width="3"/><path d="M16 15h16v14H16z" fill="#17212b"/><path d="M19 18h10v6H19z" fill="#fff"/><circle cx="20" cy="27" r="1.5" fill="#fff"/><circle cx="28" cy="27" r="1.5" fill="#fff"/><path d="m18 34 3-5m9 5-3-5" stroke="#17212b" stroke-width="2" stroke-linecap="round"/></svg>';
const STATION_ICON_ATLAS = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(STATION_ICON_SVG)}`;
const STATION_ICON_MAPPING = { station: { x: 0, y: 0, width: 48, height: 48, mask: false } };
type RGB = [number, number, number];
export type TrackProduct = 'nationalExpress' | 'national' | 'regional' | 'suburban';
type TrackProperties = { product: TrackProduct; line_name?: string; highspeed?: string };
const PRODUCT_COLORS: Record<TrackProduct, RGB> = {
  nationalExpress: [248, 248, 248],
  national: [190, 198, 207],
  regional: [220, 45, 48],
  suburban: [0, 149, 70],
};
const DEFAULT_COLOR: RGB = [184, 206, 225];
const TRACK_EXTENSIONS = [new PathStyleExtension({ dash: true })];
const TRACK_DASH_STYLE = { getDashArray: [4, 3], dashUnits: 'pixels' } satisfies PathStyleExtensionProps;
const EMPTY_TRACKS: FeatureCollection<LineString, TrackProperties> = { type: 'FeatureCollection', features: [] };
const EMPTY_BORDERS: FeatureCollection<Geometry, BorderProperties> = { type: 'FeatureCollection', features: [] };
const EMPTY_STATIONS: StationFeature[] = [];
const EMPTY_VEHICLES: VehicleFeature[] = [];
const EMPTY_POSITION: Coordinates = [0, 0];
type IndexedTrack = {
  feature: Feature<LineString, TrackProperties>;
  west: number; south: number; east: number; north: number;
};
const TRAIL_LENGTH_MS = 2_500;
const isTrackProduct = (value: string): value is TrackProduct =>
  Object.prototype.hasOwnProperty.call(PRODUCT_COLORS, value);
const getProductColor = (product: string): RGB =>
  isTrackProduct(product) ? PRODUCT_COLORS[product] : DEFAULT_COLOR;
const getColor = (vehicle: VehicleFeature | null): RGB =>
  vehicle ? getProductColor(vehicle.properties.type) : DEFAULT_COLOR;
const isVisibleAtLevel = (product: string, level: number) =>
  product === 'nationalExpress' || product === 'national'
  || (level >= 1 && product === 'regional')
  || (level >= 2 && product === 'suburban');
const getIcon = () => 'train';
const getTripPath = (trip: TimedTrip) => trip.path;
const getTripTimestamps = (trip: TimedTrip) => trip.timestamps;
const getTripColor = (trip: TimedTrip) => getColor(trip.vehicle);
const getStationPosition = (station: StationFeature) => station.geometry.coordinates;
const getStationIcon = () => 'station';

function parseTrackCollection(value: unknown): FeatureCollection<LineString, TrackProperties> {
  if (!value || typeof value !== 'object' || !('type' in value) || value.type !== 'FeatureCollection'
    || !('features' in value) || !Array.isArray(value.features)) {
    throw new Error('Expected a track FeatureCollection');
  }
  return value as FeatureCollection<LineString, TrackProperties>;
}

/** Static API data loads once per mount and retries while the backend cache warms up. */
function useGeoJsonResource<T>(url: `/${string}`, parse: (value: unknown) => T, initial: T): T {
  const [data, setData] = useState(initial);
  useEffect(() => {
    let disposed = false;
    let retryTimer = 0;
    const controller = new AbortController();
    const load = async () => {
      let retryMs = 10_000;
      try {
        const response = await fetch(apiUrl(url), { signal: controller.signal });
        if (response.status === 503) {
          const seconds = Number(response.headers.get('Retry-After'));
          if (Number.isFinite(seconds) && seconds > 0) retryMs = seconds * 1_000;
        }
        if (!response.ok) {
          const detail = (await response.text()).trim().slice(0, 500);
          throw new Error(
            `Failed to load ${url}: HTTP ${response.status} ${response.statusText}${detail ? ` — ${detail}` : ''}`,
          );
        }
        const payload: unknown = await response.json();
        const collection = parse(payload);
        if (!disposed) setData(collection);
      } catch (error) {
        if (!disposed) {
          console.error(`[BahnOpticon] ${url} unavailable; retrying in ${retryMs / 1_000}s.`, error);
          retryTimer = window.setTimeout(load, retryMs);
        }
      }
    };
    void load();
    return () => {
      disposed = true;
      controller.abort();
      window.clearTimeout(retryTimer);
    };
  }, [url, parse]);
  return data;
}

export interface TransitMapInteractions {
  hoveredVehicleId: string | null;
  clickedVehicleId: string | null;
  clickedStation: StationFeature | null;
}

export interface TransitMapProps {
  vehicles: VehicleFeature[];
  activeFilters: readonly TrackProduct[];
  selectedRegion: string;
  selectedVehicleId?: string | null;
  onSelect: (vehicleId: string | null) => void;
  onStationSelect?: (station: StationFeature) => void;
  onVehiclePointerMove?: (position: { x: number; y: number } | null) => void;
  onInteractionChange?: (interactions: TransitMapInteractions) => void;
}

export const TransitMap = memo(function TransitMap({
  vehicles, activeFilters, selectedRegion, selectedVehicleId, onSelect, onStationSelect,
  onVehiclePointerMove, onInteractionChange,
}: TransitMapProps) {
  const trackData = useGeoJsonResource('/tracks', parseTrackCollection, EMPTY_TRACKS);
  const borders = useGeoJsonResource('/borders', parseBorderCollection, EMPTY_BORDERS);
  const stations = useGeoJsonResource('/stations', parseStationCollection, EMPTY_STATIONS);
  const [viewState, setViewState] = useState<MapViewState>(INITIAL_VIEW_STATE);
  const [viewportSize, setViewportSize] = useState(() => ({
    width: window.innerWidth, height: window.innerHeight,
  }));
  const [hoveredVehicleId, setHoveredVehicleId] = useState<string | null>(null);
  const [clickedVehicleId, setClickedVehicleId] = useState<string | null>(null);
  const [clickedStation, setClickedStation] = useState<StationFeature | null>(null);
  const [journeyResult, setJourneyResult] = useState<{ vehicleId: string; feature: Feature<LineString> } | null>(null);
  const [mapping, setMapping] = useState<VehicleSlots>(() => ({
    snapshot: vehicles, slots: [...vehicles], generation: 0,
  }));
  const [currentTime, setCurrentTime] = useState(0);
  if (mapping.snapshot !== vehicles) {
    setMapping(reconcileVehicleSlots(mapping, vehicles));
  }

  useEffect(() => {
    const onResize = () => setViewportSize({ width: window.innerWidth, height: window.innerHeight });
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  useEffect(() => {
    onInteractionChange?.({ hoveredVehicleId, clickedVehicleId, clickedStation });
  }, [hoveredVehicleId, clickedVehicleId, clickedStation, onInteractionChange]);

  useEffect(() => {
    console.log("Track features loaded:", trackData?.features?.length);
  }, [trackData]);

  const journeyVehicleId = selectedVehicleId === undefined ? clickedVehicleId : selectedVehicleId;
  const journey = journeyResult?.vehicleId === journeyVehicleId ? journeyResult.feature : undefined;
  useEffect(() => {
    if (!journeyVehicleId) return;
    const controller = new AbortController();
    const loadJourney = async () => {
      try {
        const response = await fetch(apiUrl(`/trip/${encodeURIComponent(journeyVehicleId)}`), { signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const feature = parseJourneyFeature(await response.json());
        if (!controller.signal.aborted) setJourneyResult({ vehicleId: journeyVehicleId, feature });
      } catch (error) {
        if (!controller.signal.aborted) console.error(`[BahnOpticon] Journey ${journeyVehicleId} unavailable.`, error);
      }
    };
    void loadJourney();
    return () => controller.abort();
  }, [journeyVehicleId]);

  const handleVehicleClick = useCallback(({ object }: PickingInfo<VehicleFeature | null>) => {
    const vehicleId = object?.id ?? null;
    setClickedVehicleId(vehicleId);
    setClickedStation(null);
    onSelect(vehicleId);
  }, [onSelect]);

  const handleHover = useCallback(({ object, layer, x, y }: PickingInfo<VehicleFeature | StationFeature>) => {
    const vehicle = layer?.id.startsWith('transit-vehicle') ? object as VehicleFeature | null : null;
    setHoveredVehicleId(vehicle?.id ?? null);
    onVehiclePointerMove?.(vehicle ? { x, y } : null);
  }, [onVehiclePointerMove]);

  const animation = useMemo(() => {
    const positions = new Map<string, Coordinates>();
    const trips: TimedTrip[] = [];
    for (const vehicle of mapping.snapshot) {
      const trip = createTimedTrip(vehicle);
      if (trip) trips.push(trip);
      positions.set(vehicle.id, trip ? [...trip.path[0]] : [...getVehiclePosition(vehicle)]);
    }
    return {
      positions,
      trips,
      startedAt: trips[0]?.vehicle.properties.animationStartedAt ?? 0,
      maxDurationMs: trips.reduce((maximum, trip) => Math.max(maximum, trip.durationMs), 0),
    };
  }, [mapping.snapshot]);

  useEffect(() => {
    let requestId = 0;
    const tick = () => {
      const now = Date.now();
      const elapsed = Math.min(animation.maxDurationMs, Math.max(0, now - animation.startedAt));
      let animating = false;
      for (const trip of animation.trips) {
        const progress = Math.min(1, Math.max(0,
          (now - trip.vehicle.properties.animationStartedAt) / trip.durationMs));
        sampleRoute(trip.route, progress, animation.positions.get(trip.vehicle.id));
        if (progress < 1) animating = true;
      }
      setCurrentTime(previous => previous === elapsed ? previous : elapsed);
      if (animating) requestId = requestAnimationFrame(tick);
    };
    tick();
    return () => cancelAnimationFrame(requestId);
  }, [animation]);

  const lodLevel = viewState.zoom < 6.5 ? 0 : viewState.zoom < 8.5 ? 1 : 2;
  const vehicleRadiusMin = viewState.zoom < 7 ? 2 : 4;
  const vehicleRadius = viewState.zoom < 7 ? 2
    : Math.min(12, Math.max(4, Math.round((viewState.zoom - 6) * 2)));
  const vehicleIconSize = Math.min(18, Math.max(6, vehicleRadius * 2 - 4));
  const trackBounds = useMemo<IndexedTrack[]>(() => trackData.features.map(feature => {
    let west = Infinity; let south = Infinity; let east = -Infinity; let north = -Infinity;
    for (const [longitude, latitude] of feature.geometry.coordinates) {
      west = Math.min(west, longitude); east = Math.max(east, longitude);
      south = Math.min(south, latitude); north = Math.max(north, latitude);
    }
    return { feature, west, south, east, north };
  }), [trackData]);
  const viewportBounds = useMemo(() => new WebMercatorViewport({
    ...viewState, width: viewportSize.width, height: viewportSize.height,
  }).getBounds(), [viewState, viewportSize]);
  const visibleTracks = useMemo(() => {
    const [west, south, east, north] = viewportBounds;
    const longitudePad = (east - west) * 0.1;
    const latitudePad = (north - south) * 0.1;
    return trackBounds.filter(({ feature, west: trackWest, south: trackSouth,
      east: trackEast, north: trackNorth }) => (
      activeFilters.includes(feature.properties.product)
      && (feature.properties.product !== 'suburban' || viewState.zoom >= 8.5)
      && trackEast >= west - longitudePad && trackWest <= east + longitudePad
      && trackNorth >= south - latitudePad && trackSouth <= north + latitudePad
    )).map(({ feature }) => feature);
  }, [activeFilters, trackBounds, viewState.zoom, viewportBounds]);
  const visibleVehicles = useMemo(() => mapping.slots.filter(
    (vehicle): vehicle is VehicleFeature => vehicle !== null
      && activeFilters.includes(vehicle.properties.type as TrackProduct)
      && isVisibleAtLevel(vehicle.properties.type, lodLevel),
  ), [activeFilters, lodLevel, mapping.slots]);
  const visibleTrips = useMemo(() => animation.trips.filter(
    trip => activeFilters.includes(trip.vehicle.properties.type as TrackProduct)
      && isVisibleAtLevel(trip.vehicle.properties.type, lodLevel),
  ), [activeFilters, animation.trips, lodLevel]);
  const stationsVisible = viewState.zoom >= 11;

  const borderLayer = useMemo(() => new GeoJsonLayer<BorderProperties>({
    id: 'transit-borders',
    data: borders,
    stroked: true,
    filled: true,
    getFillColor: feature => feature.properties.name === selectedRegion
      ? [255, 255, 255, 40] : [0, 0, 0, 0],
    getLineColor: feature => feature.properties.name === selectedRegion
      ? [255, 255, 255, 255]
      : feature.properties.admin_level === '2'
        ? [214, 224, 232, 210] : [174, 187, 198, 68],
    getLineWidth: feature => feature.properties.name === selectedRegion
      ? 3 : feature.properties.admin_level === '2' ? 2 : 0.5,
    lineWidthUnits: 'pixels',
    pickable: false,
    updateTriggers: {
      getFillColor: selectedRegion,
      getLineColor: selectedRegion,
      getLineWidth: selectedRegion,
    },
  }), [borders, selectedRegion]);

  const trackLayer = useMemo(() => new GeoJsonLayer<TrackProperties>({
    id: 'transit-tracks',
    data: visibleTracks,
    stroked: true,
    filled: false,
    getLineColor: feature => [...getProductColor(feature.properties.product), 40],
    getLineWidth: 2,
    lineWidthUnits: 'pixels',
    lineWidthMinPixels: 2,
    extensions: TRACK_EXTENSIONS,
    ...TRACK_DASH_STYLE,
    pickable: false,
    updateTriggers: {
      getLineColor: [activeFilters, lodLevel],
    },
  }), [activeFilters, lodLevel, visibleTracks]);

  const journeyGlowLayer = useMemo(() => new GeoJsonLayer({
    id: 'transit-journey-glow',
    data: journey,
    stroked: true,
    filled: false,
    getLineColor: [255, 204, 0, 75],
    getLineWidth: 12,
    lineWidthUnits: 'pixels',
    pickable: false,
  }), [journey]);

  const journeyLayer = useMemo(() => new GeoJsonLayer({
    id: 'transit-journey',
    data: journey,
    stroked: true,
    filled: false,
    getLineColor: [255, 204, 0, 255],
    getLineWidth: 4,
    lineWidthUnits: 'pixels',
    lineWidthMinPixels: 3,
    pickable: false,
  }), [journey]);

  const stationLayer = useMemo(() => new IconLayer<StationFeature>({
    id: 'transit-stations',
    data: stationsVisible ? stations : EMPTY_STATIONS,
    iconAtlas: STATION_ICON_ATLAS,
    iconMapping: STATION_ICON_MAPPING,
    getIcon: getStationIcon,
    getPosition: getStationPosition,
    getSize: station => station.properties.is_important ? 42 : 14,
    sizeUnits: 'pixels',
    billboard: true,
    visible: stationsVisible,
    pickable: true,
    updateTriggers: { getSize: viewState.zoom },
    onClick: ({ object }: PickingInfo<StationFeature>) => {
      if (object) {
        setClickedStation(object);
        setClickedVehicleId(null);
        onStationSelect?.(object);
      }
    },
  }), [onStationSelect, stations, stationsVisible, viewState.zoom]);

  const layers = useMemo(() => [
    borderLayer,
    trackLayer,
    journeyGlowLayer,
    journeyLayer,
    new TripsLayer<TimedTrip>({
      id: 'transit-track-trails',
      data: visibleTrips,
      getPath: getTripPath,
      getTimestamps: getTripTimestamps,
      getColor: getTripColor,
      currentTime,
      trailLength: TRAIL_LENGTH_MS,
      widthUnits: 'pixels',
      getWidth: 3,
      capRounded: true,
      jointRounded: true,
      pickable: false,
    }),
    stationLayer,
    new ScatterplotLayer<VehicleFeature | null>({
      id: `transit-vehicle-colors-${mapping.generation}`,
      data: visibleVehicles,
      getPosition: vehicle => vehicle
        ? animation.positions.get(vehicle.id) ?? EMPTY_POSITION : EMPTY_POSITION,
      getRadius: vehicle => vehicle
        ? vehicleRadius + (vehicle.id === hoveredVehicleId
          || vehicle.properties.delay != null && vehicle.properties.delay >= 5 ? 2 : 0)
        : 0,
      radiusUnits: 'pixels',
      radiusMinPixels: vehicleRadiusMin,
      radiusMaxPixels: 12,
      getFillColor: getColor,
      getLineColor: vehicle => vehicle?.id === hoveredVehicleId
        ? [255, 204, 0, 255]
        : vehicle?.properties.delay != null && vehicle.properties.delay >= 5
          ? [255, 50, 50, 255] : [0, 0, 0, 0],
      getLineWidth: 2,
      lineWidthUnits: 'pixels',
      stroked: true,
      pickable: true,
      updateTriggers: {
        getPosition: currentTime,
        getRadius: [vehicleRadius, hoveredVehicleId],
        getLineColor: hoveredVehicleId,
      },
      onClick: handleVehicleClick,
    }),
    new IconLayer<VehicleFeature | null>({
      id: `transit-vehicles-${mapping.generation}`,
      data: lodLevel === 2 ? visibleVehicles : EMPTY_VEHICLES,
      iconAtlas: `${import.meta.env.BASE_URL}vehicle-icons.svg`,
      iconMapping: ICON_MAPPING,
      getIcon,
      getPosition: vehicle => vehicle
        ? animation.positions.get(vehicle.id) ?? EMPTY_POSITION : EMPTY_POSITION,
      getColor: [255, 255, 255],
      getSize: vehicle => vehicle ? vehicleIconSize : 0,
      visible: lodLevel === 2,
      pickable: true,
      billboard: true,
      sizeUnits: 'pixels',
      // Route geometry stays cached; only the icon position attribute changes on each frame.
      updateTriggers: { getPosition: currentTime, getSize: vehicleIconSize },
      onClick: handleVehicleClick,
    }),
  ], [animation, borderLayer, currentTime, handleVehicleClick, hoveredVehicleId, journeyGlowLayer, journeyLayer, lodLevel, mapping.generation, stationLayer, trackLayer, vehicleIconSize, vehicleRadius, vehicleRadiusMin, visibleTrips, visibleVehicles]);

  return (
    <DeckGL
      viewState={viewState}
      onViewStateChange={({ viewState: next }) => setViewState(next as MapViewState)}
      controller
      layers={layers}
      onHover={handleHover}
      getCursor={({ isHovering, isDragging }) => isDragging ? 'grabbing' : isHovering ? 'pointer' : 'grab'}
      getTooltip={({ object, layer }: PickingInfo<VehicleFeature | StationFeature>) => {
        return object && layer?.id === 'transit-stations'
          ? { text: (object as StationFeature).properties.name } : null;
      }}
    >
      {/* Map consumes DeckGL's camera context, keeping tiles and vehicle picking aligned. */}
      <MapLibre mapStyle={MAP_STYLE} attributionControl={{ compact: false }} />
    </DeckGL>
  );
});

export default TransitMap;
