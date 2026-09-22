import math
from typing import Tuple, Dict, Optional
from shapely.geometry import Point, LineString, MultiLineString
from shapely.ops import nearest_points

class InterpolationEngine:
    def __init__(self):
        self.track_network: Optional[MultiLineString] = None
        self.vehicle_states = {} # tripId -> dict with last lon, lat, timestamp
        
    def load_osm_data(self, osm_json: Dict):
        """
        Parse OSM Overpass JSON into a Shapely MultiLineString representing the track network.
        """
        lines = []
        for element in osm_json.get("elements", []):
            if not isinstance(element, dict) or element.get("type") != "way":
                continue
            geometry = element.get("geometry")
            if not isinstance(geometry, list):
                continue
            coords = []
            for node in geometry:
                if not isinstance(node, dict):
                    break
                lon, lat = node.get("lon"), node.get("lat")
                if not all(isinstance(value, (int, float)) and math.isfinite(value)
                           for value in (lon, lat)):
                    break
                coords.append((lon, lat))
            else:
                if len(coords) >= 2:
                    lines.append(LineString(coords))
        
        self.track_network = MultiLineString(lines) if lines else None

    def prune_states(self, active_trip_ids):
        """A radar response is a snapshot; retain only currently visible trips."""
        self.vehicle_states = {
            trip_id: state for trip_id, state in self.vehicle_states.items()
            if trip_id in active_trip_ids
        }
            
    def snap_to_track(self, lon: float, lat: float) -> Tuple[float, float]:
        """
        Snap a raw GPS coordinate to the nearest static track geometry.
        """
        if not self.track_network:
            return lon, lat
            
        point = Point(lon, lat)
        # Find the closest point on the track network
        nearest_geom, _ = nearest_points(self.track_network, point)
        return nearest_geom.x, nearest_geom.y

    def compute_trajectory(self, trip_id: str, new_lon: float, new_lat: float, timestamp: float) -> Dict:
        """
        Takes a new API ping, snaps it to the track, and calculates the segment between
        the last known position and the current one for 60fps client-side interpolation.
        Returns a payload suitable for streaming to the client.
        """
        snapped_lon, snapped_lat = self.snap_to_track(new_lon, new_lat)
        
        if trip_id not in self.vehicle_states:
            self.vehicle_states[trip_id] = {
                "lon": snapped_lon,
                "lat": snapped_lat,
                "timestamp": timestamp
            }
            return {
                "trip_id": trip_id,
                "start_point": [snapped_lon, snapped_lat],
                "end_point": [snapped_lon, snapped_lat],
                "duration_ms": 0
            }
            
        last_state = self.vehicle_states[trip_id]
        
        # Calculate time difference in ms for client-side tweening
        duration_ms = max(0, min(60000, (timestamp - last_state["timestamp"]) * 1000))
        
        payload = {
            "trip_id": trip_id,
            "start_point": [last_state["lon"], last_state["lat"]],
            "end_point": [snapped_lon, snapped_lat],
            "duration_ms": duration_ms
        }
        
        # Update state
        self.vehicle_states[trip_id] = {
            "lon": snapped_lon,
            "lat": snapped_lat,
            "timestamp": timestamp
        }
        
        return payload
