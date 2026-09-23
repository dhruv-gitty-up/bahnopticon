export type TrackViewportBounds = readonly [
  minLongitude: number,
  minLatitude: number,
  maxLongitude: number,
  maxLatitude: number,
];

const formatCoordinate = (value: number) => Number(value.toFixed(6)).toString();

/** Build the backend path for the tracks intersecting the visible map extent. */
export function buildTrackRequestPath(
  bounds: TrackViewportBounds,
  products: readonly string[] = [],
): `/tracks?${string}` {
  const [minLongitude, minLatitude, maxLongitude, maxLatitude] = bounds;
  const params = new URLSearchParams({
    min_lon: formatCoordinate(minLongitude),
    min_lat: formatCoordinate(minLatitude),
    max_lon: formatCoordinate(maxLongitude),
    max_lat: formatCoordinate(maxLatitude),
  });
  if (products.length > 0) params.set('products', products.join(','));
  return `/tracks?${params.toString()}`;
}
