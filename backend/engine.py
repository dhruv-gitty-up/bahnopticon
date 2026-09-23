"""Snap vehicle observations to OSM-derived PostGIS ways and route between them."""

from dataclasses import dataclass, field
from functools import lru_cache
import logging
import math
import time
from typing import Dict, Optional

import networkx as nx
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree
from osm_client import track_product


logger = logging.getLogger(__name__)
EARTH_RADIUS_METERS = 6_371_008.8
MAX_SNAP_DISTANCE_METERS = 250
VEHICLE_STATE_TTL_SECONDS = 900
PRODUCT_RAILWAY = {
    "subway": "subway",
    "suburban": "light_rail",
    "regional": "rail",
    "regionalExpress": "rail",
    "express": "rail",
    "national": "rail",
    "nationalExpress": "rail",
}


@dataclass(frozen=True)
class Segment:
    start: object
    end: object
    line: LineString  # local metre coordinates, used for nearest-segment lookup
    length: float
    railway: Optional[str]


@dataclass(frozen=True)
class Snap:
    coordinates: tuple[float, float]
    segment: int
    offset: float  # distance in metres from the segment's first OSM node


@dataclass
class TrackGraph:
    graph: nx.Graph
    segments: list[Segment]
    tree: STRtree
    mode_trees: dict[str, STRtree]
    mode_segments: dict[str, list[int]]
    components: dict[object, int]
    cosine_latitude: float
    shortest: object = field(init=False)

    def __post_init__(self):
        # Nearby vehicles often traverse the same node pairs. Bound memory while
        # retaining the expensive searches that repeat during successive polls.
        @lru_cache(maxsize=4096)
        def shortest(start, end, railway=None):
            if self.components[start] != self.components[end]:
                return None
            try:
                # A mode-specific weight hides edges belonging to another rail
                # system. This prevents an S-Bahn from routing via U-Bahn rails
                # when ways share an OSM node.
                weight = "weight" if railway is None else (
                    lambda _start, _end, edge: edge["weights"].get(railway)
                )
                nodes = nx.shortest_path(self.graph, start, end, weight=weight)
                distance = sum(
                    self.graph[a][b]["weight"] if railway is None
                    else self.graph[a][b]["weights"][railway]
                    for a, b in zip(nodes, nodes[1:])
                )
                return distance, tuple(nodes)
            except nx.NetworkXNoPath:
                return None

        self.shortest = shortest

    def project(self, longitude, latitude):
        scale = math.pi * EARTH_RADIUS_METERS / 180
        return Point(longitude * scale * self.cosine_latitude, latitude * scale)

    def unproject(self, point):
        scale = math.pi * EARTH_RADIUS_METERS / 180
        return (point.x / (scale * self.cosine_latitude), point.y / scale)

    def snap(self, longitude, latitude, railway=None) -> Optional[Snap]:
        point = self.project(longitude, latitude)
        if railway is None:
            index = int(self.tree.nearest(point))
        else:
            tree = self.mode_trees.get(railway)
            if tree is None:
                return None
            index = self.mode_segments[railway][int(tree.nearest(point))]
        segment = self.segments[index]
        offset = segment.line.project(point)
        nearest = segment.line.interpolate(offset)
        if point.distance(nearest) > MAX_SNAP_DISTANCE_METERS:
            return None
        return Snap(self.unproject(nearest), index, offset)

    def route(self, previous: Snap, current: Snap, railway=None) -> Optional[list[list[float]]]:
        """Find the shortest continuous railway route between two edge positions."""
        source = self.segments[previous.segment]
        target = self.segments[current.segment]
        best_cost = math.inf
        best_nodes = None
        if previous.segment == current.segment:
            best_cost = abs(previous.offset - current.offset)
            best_nodes = ()

        source_ends = ((source.start, previous.offset),
                       (source.end, source.length - previous.offset))
        target_ends = ((target.start, current.offset),
                       (target.end, target.length - current.offset))
        for source_node, source_distance in source_ends:
            for target_node, target_distance in target_ends:
                if source_distance + target_distance >= best_cost:
                    continue
                result = self.shortest(source_node, target_node, railway)
                if result is None:
                    continue
                graph_distance, nodes = result
                cost = source_distance + graph_distance + target_distance
                if cost < best_cost:
                    best_cost, best_nodes = cost, nodes

        if best_nodes is None:
            return None
        points = [previous.coordinates]
        points.extend(self.graph.nodes[node]["coordinates"] for node in best_nodes)
        points.append(current.coordinates)
        # Adjacent duplicate vertices arise when an observation lies on an OSM node.
        result = []
        for coordinates in points:
            value = [coordinates[0], coordinates[1]]
            if not result or value != result[-1]:
                result.append(value)
        return result if len(result) >= 2 else None


class InterpolationEngine:
    def __init__(self):
        self._graph: Optional[TrackGraph] = None
        self.vehicle_states = {}  # trip ID -> last coordinate, observation time and last-seen time
        self._mock_graph = None
        self._mock_vehicles = []
        self._mock_last_timestamp = None

    def load_osm_data(self, osm_json: Dict) -> bool:
        """Build a metric graph and spatial index, then publish them atomically."""
        valid_ways = []
        for element in osm_json.get("elements", []):
            if not isinstance(element, dict) or element.get("type") != "way":
                continue
            geometry = element.get("geometry")
            if not isinstance(geometry, list):
                continue
            coordinates = []
            for vertex in geometry:
                if not isinstance(vertex, dict):
                    break
                lon, lat = vertex.get("lon"), vertex.get("lat")
                if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                           and math.isfinite(value) for value in (lon, lat)):
                    break
                if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                    break
                coordinates.append((float(lon), float(lat)))
            else:
                if len(coordinates) >= 2:
                    ids = element.get("nodes")
                    if (not isinstance(ids, list) or len(ids) != len(coordinates)
                            or any(not isinstance(node_id, (int, str)) or isinstance(node_id, bool)
                                   for node_id in ids)):
                        ids = None
                    tags = element.get("tags")
                    tags = tags if isinstance(tags, dict) else {}
                    railway = tags.get("railway") if tags.get("railway") in {
                        "rail", "subway", "light_rail"
                    } else None
                    product = track_product(tags)
                    valid_ways.append((coordinates, ids, railway, product))

        if not valid_ways:
            return False

        mean_latitude = sum(point[1] for way, _, _, _ in valid_ways for point in way) / sum(
            len(way) for way, _, _, _ in valid_ways
        )
        cosine_latitude = math.cos(math.radians(mean_latitude))
        scale = math.pi * EARTH_RADIUS_METERS / 180
        graph = nx.Graph()
        segments = []
        shapes = []
        mode_shapes = {"rail": [], "subway": [], "light_rail": []}
        mode_segments = {"rail": [], "subway": [], "light_rail": []}
        for coordinates, ids, railway, product in valid_ways:
            nodes = []
            for index, point in enumerate(coordinates):
                # Raw OSM fixtures may include aligned node IDs. The current
                # PostGIS schema stores LineStrings, so imported rows reconnect
                # through identical coordinate keys instead.
                node = ("osm", ids[index]) if ids is not None else ("coord", point)
                nodes.append(node)
                graph.add_node(node, coordinates=point)
            for index in range(len(nodes) - 1):
                first, second = nodes[index:index + 2]
                a, b = coordinates[index:index + 2]
                line = LineString(((a[0] * scale * cosine_latitude, a[1] * scale),
                                   (b[0] * scale * cosine_latitude, b[1] * scale)))
                length = line.length
                if first == second or length == 0:
                    continue
                if not graph.has_edge(first, second):
                    graph.add_edge(first, second, weight=length, weights={}, products=set())
                edge = graph[first][second]
                edge["weight"] = min(edge["weight"], length)
                edge["products"].add(product)
                if railway is not None:
                    edge["weights"][railway] = min(edge["weights"].get(railway, math.inf), length)
                segment_index = len(segments)
                segments.append(Segment(first, second, line, length, railway))
                shapes.append(line)
                if railway is not None:
                    mode_shapes[railway].append(line)
                    mode_segments[railway].append(segment_index)

        if not segments:
            return False
        components = {
            node: component_id
            for component_id, component in enumerate(nx.connected_components(graph))
            for node in component
        }
        mode_trees = {mode: STRtree(lines) for mode, lines in mode_shapes.items() if lines}
        network = TrackGraph(graph, segments, STRtree(shapes), mode_trees,
                             mode_segments, components, cosine_latitude)
        self._graph = network
        return True

    def generate_mock_traffic(self, timestamp: Optional[float] = None) -> Dict:
        """Advance 20 simulated vehicles by one connected rail node.

        Call this from a worker thread. Each segment is an actual edge in the
        PostGIS-derived graph, so emitted coordinates stay on physical rail.
        """
        graph = self._graph
        if graph is None:
            raise RuntimeError("PostGIS track geometry is not loaded yet")
        timestamp = time.time() if timestamp is None else timestamp
        modes = (("nationalExpress", "rail", "ICE"),
                 ("national", "rail", "IC"),
                 ("regional", "rail", "RE"),
                 ("suburban", "light_rail", "S"))
        if self._mock_graph is not graph:
            candidates = {
                product: [node for node in graph.graph.nodes
                          if any(product in edge["products"]
                                 for edge in graph.graph[node].values())]
                for product, railway, _prefix in modes
            }
            available = [(product, railway, prefix) for product, railway, prefix in modes
                         if candidates[product]]
            if not available:
                raise RuntimeError("PostGIS graph has no usable long-distance or regional rail edges")
            counts = {product: 0 for product, _, _ in available}
            self._mock_vehicles = []
            for index in range(20):
                mode_index = index % len(available)
                product, railway, prefix = available[mode_index]
                ordinal = counts[product]
                counts[product] += 1
                slots = len(range(mode_index, 20, len(available)))
                nodes = candidates[product]
                node = nodes[min(len(nodes) - 1,
                                 int((ordinal + 0.5) * len(nodes) / slots))]
                self._mock_vehicles.append({
                    "id": f"mock:{product}:{index}", "index": index,
                    "product": product, "railway": railway,
                    "line_name": f"{prefix}{index % 9 + 1}",
                    "node": node, "previous": None, "ticks": 0,
                })
            self._mock_graph = graph
            self._mock_last_timestamp = None

        duration_ms = 0 if self._mock_last_timestamp is None else max(
            0, min(30_000, (timestamp - self._mock_last_timestamp) * 1000))
        features = []
        for vehicle in self._mock_vehicles:
            old_node = vehicle["node"]
            start = list(graph.graph.nodes[old_node]["coordinates"])
            if self._mock_last_timestamp is not None:
                neighbors = [node for node, edge in graph.graph[old_node].items()
                             if vehicle["product"] in edge["products"]]
                forward = [node for node in neighbors if node != vehicle["previous"]]
                options = forward or neighbors
                next_node = options[(vehicle["ticks"] + vehicle["index"]) % len(options)]
                vehicle["previous"] = old_node
                vehicle["node"] = next_node
                vehicle["ticks"] += 1
            end = list(graph.graph.nodes[vehicle["node"]]["coordinates"])
            moving = start != end
            features.append({
                "type": "Feature", "id": vehicle["id"],
                "properties": {
                    "trip_id": vehicle["id"], "product": vehicle["product"],
                    "line_name": vehicle["line_name"], "route": vehicle["line_name"],
                    "destination": "Simulated route", "delay_minutes": 0,
                    "next_station": None,
                    "scheduled_time": None, "expected_time": None,
                    "next_station_scheduled_time": None,
                    "next_station_expected_time": None,
                    "next_station_delay_minutes": 0,
                    "duration_ms": duration_ms, "start": start, "end": end,
                    "route_status": "routed" if moving else "initial",
                    "is_mock": True,
                },
                "geometry": ({"type": "LineString", "coordinates": [start, end]}
                             if moving else {"type": "Point", "coordinates": end}),
            })
        self._mock_last_timestamp = timestamp
        return {"type": "FeatureCollection", "generated_at": timestamp,
                "is_mock": True, "features": features}

    def purge_stale_states(self, current_time: float) -> int:
        """Remove vehicle state that has not been refreshed within the TTL."""
        stale_trip_ids = [
            trip_id for trip_id, state in self.vehicle_states.items()
            if current_time - state["last_seen"] > VEHICLE_STATE_TTL_SECONDS
        ]
        for trip_id in stale_trip_ids:
            del self.vehicle_states[trip_id]
        if stale_trip_ids:
            logger.info("Evicted %d stale vehicles", len(stale_trip_ids))
        return len(stale_trip_ids)

    def snap_to_track(self, lon: float, lat: float) -> tuple[float, float]:
        graph = self._graph
        snap = graph.snap(lon, lat) if graph is not None else None
        return snap.coordinates if snap else (lon, lat)

    def compute_trajectory(self, trip_id: str, new_lon: float, new_lat: float,
                           timestamp: float, product: Optional[str] = None) -> Dict:
        """Compute a route; callers run this off the FastAPI event loop."""
        railway = PRODUCT_RAILWAY.get(product) if product is not None else None
        # Untracked road vehicles remain at their reported positions. Legacy
        # callers without a product may use the full rail graph.
        graph = self._graph if product is None or railway is not None else None
        snap = graph.snap(new_lon, new_lat, railway) if graph is not None else None
        end = snap.coordinates if snap else (new_lon, new_lat)
        previous = self.vehicle_states.get(trip_id)
        duration_ms = max(0, min(60_000, (timestamp - previous["timestamp"]) * 1000)) \
            if previous else 0
        start = previous["coordinates"] if previous else end
        status = "initial" if previous is None else "unavailable"
        route = None
        if previous is not None:
            old_snap = previous["snap"]
            if start == end:
                status = "stationary"
            elif graph is not None and graph is previous["graph"] and snap and old_snap \
                    and railway == previous["railway"]:
                try:
                    route = graph.route(old_snap, snap, railway)
                except (nx.NetworkXException, KeyError, ValueError):
                    route = None
                if route:
                    status = "routed"
            if start != end and route is None:
                # Sparse or disconnected nationwide track data must not stop
                # an individual train from animating between observations.
                route = [list(start), list(end)]
                status = "straight"

        self.vehicle_states[trip_id] = {
            "coordinates": end, "timestamp": timestamp, "snap": snap,
            "graph": graph, "railway": railway, "last_seen": timestamp,
        }
        return {
            "trip_id": trip_id,
            "start_point": list(start),
            "end_point": list(end),
            "duration_ms": duration_ms,
            "route_status": status,
            "route": route,
        }
