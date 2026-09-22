"""Offline Overpass query and station-node conversion tests."""

import asyncio
import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile
from urllib.parse import parse_qs

import httpx

from osm_client import (
    OsmClient, TRACK_FALLBACK_BBOX, is_important_station, load_feature_cache,
    save_json_cache, station_features_from_osm,
)


class OsmClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_track_and_station_queries_use_the_fixed_bbox_and_expected_tags(self):
        queries = []
        timeouts = []

        def respond(request):
            queries.append(parse_qs(request.content.decode())["data"][0])
            timeouts.append(request.extensions["timeout"]["read"])
            return httpx.Response(200, json={"elements": []})

        client = OsmClient("https://overpass.example.test/interpreter")
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(client.close)
        await client.get_track_geometries(55.0, 5.8, 47.2, 15.0)
        await client.get_station_nodes(55.0, 5.8, 47.2, 15.0)
        self.assertEqual(len(queries), 2)  # An empty first quadrant aborts the merge.
        self.assertIn('[out:json][timeout:60]', queries[0])
        self.assertIn('way["railway"="rail"]["highspeed"="yes"](51.1,5.8,55.0,10.4)', queries[0])
        self.assertIn('way["railway"="rail"]["usage"="main"](51.1,5.8,55.0,10.4)', queries[0])
        self.assertNotIn('"subway"', queries[0])
        self.assertNotIn('"light_rail"', queries[0])
        self.assertIn("out body geom;", queries[0])
        self.assertIn('[out:json][timeout:120]', queries[1])
        self.assertIn('node["railway"~"^(station|halt)$"](47.2,5.8,55.0,15.0)', queries[1])
        self.assertIn("out body;", queries[1])
        self.assertEqual(timeouts, [60.0, 120.0])

    async def test_track_quadrants_run_sequentially_and_merge_duplicate_ways(self):
        queries = []
        def respond(request):
            query = parse_qs(request.content.decode())["data"][0]
            queries.append(query)
            index = len(queries)
            return httpx.Response(200, json={"elements": [
                {"type": "way", "id": 1, "geometry": [{"lat": 52, "lon": 10},
                    {"lat": 52.1, "lon": 10.1}]},
                {"type": "way", "id": index + 1, "geometry": [{"lat": 52, "lon": 10},
                    {"lat": 52.1, "lon": 10.1}]},
            ]})
        client = OsmClient("https://overpass.example.test/interpreter")
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(client.close)
        merged = await client.get_track_geometries(55.0, 5.8, 47.2, 15.0)
        self.assertEqual(len(queries), 4)
        self.assertIn("(51.1,5.8,55.0,10.4)", queries[0])
        self.assertIn("(51.1,10.4,55.0,15.0)", queries[1])
        self.assertIn("(47.2,5.8,51.1,10.4)", queries[2])
        self.assertIn("(47.2,10.4,51.1,15.0)", queries[3])
        self.assertEqual([way["id"] for way in merged["elements"]], [1, 2, 3, 4, 5])

    async def test_track_quadrant_requests_never_overlap(self):
        active = 0
        maximum_active = 0
        request_count = 0
        async def respond(_request):
            nonlocal active, maximum_active, request_count
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.001)
            active -= 1
            request_count += 1
            return httpx.Response(200, json={"elements": [{
                "type": "way", "id": request_count,
            }]})
        client = OsmClient("https://overpass.example.test/interpreter")
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(client.close)
        await client.get_track_geometries(55, 5.8, 47.2, 15)
        self.assertEqual(request_count, 4)
        self.assertEqual(maximum_active, 1)

    async def test_successful_quadrants_are_reused_after_later_failure(self):
        requests = []
        def respond(request):
            requests.append(request)
            if len(requests) == 4:
                return httpx.Response(504)
            return httpx.Response(200, json={"elements": [{
                "type": "way", "id": len(requests), "geometry": [
                    {"lat": 52, "lon": 10}, {"lat": 52.1, "lon": 10.1},
                ],
            }]})
        client = OsmClient("https://overpass.example.test/interpreter")
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(client.close)
        with tempfile.TemporaryDirectory() as directory, \
             patch("osm_client.TRACK_QUADRANT_CACHE_DIR", Path(directory)):
            self.assertEqual(await client.get_track_geometries(
                55, 5.8, 47.2, 15, cache_quadrants=True,
            ), {})
            merged = await client.get_track_geometries(
                55, 5.8, 47.2, 15, cache_quadrants=True,
            )
        self.assertEqual(len(requests), 5)  # Only the failed SE quadrant was retried.
        self.assertEqual([way["id"] for way in merged["elements"]], [1, 2, 3, 5])

    async def test_http_timeout_allows_nationwide_query_to_run_for_120_seconds(self):
        client = OsmClient("https://overpass.example.test/interpreter")
        self.addAsyncCleanup(client.close)
        self.assertEqual(client.client.timeout.read, 120.0)
        self.assertEqual(client.client.timeout.connect, 120.0)

    def test_track_fallback_bbox_is_bounded_to_the_berlin_leipzig_corridor(self):
        self.assertEqual(
            TRACK_FALLBACK_BBOX,
            {"north": 53.0, "south": 51.8, "west": 12.0, "east": 14.7},
        )

    async def test_bad_overpass_status_yields_retryable_empty_response(self):
        client = OsmClient("https://overpass.example.test/interpreter")
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(503)))
        self.addAsyncCleanup(client.close)
        self.assertEqual(await client.get_station_nodes(52.6, 13.1, 52.3, 13.7), {})


class StationFeatureTests(unittest.TestCase):
    def test_station_nodes_keep_name_id_and_railway_tags(self):
        collection = station_features_from_osm({"elements": [
            {"type": "node", "id": 42, "lat": 52.52, "lon": 13.405,
             "tags": {"railway": "station", "station": "subway", "name": "U Test", "ref:IFOPT": "de:11000:900100001"}},
            {"type": "node", "id": 43, "lat": 52.53, "lon": 13.41,
             "tags": {"railway": "halt", "name": "S Test"}},
            {"type": "node", "id": 44, "lat": 52.9, "lon": 13.405,
             "tags": {"railway": "station", "name": "Outside"}},
            {"type": "node", "id": 45, "lat": 52.52, "lon": 13.405,
             "tags": {"railway": "tram_stop", "name": "Not requested"}},
        ]}, north=52.6, west=13.1, south=52.3, east=13.7)
        self.assertEqual(len(collection["features"]), 2)
        station, halt = collection["features"]
        self.assertEqual(station["id"], "osm:node:42")
        self.assertEqual(station["properties"]["id"], 42)
        self.assertEqual(station["properties"]["name"], "U Test")
        self.assertEqual(station["properties"]["station"], "subway")
        self.assertEqual(station["properties"]["railway"], "station")
        self.assertEqual(station["properties"]["tags"]["ref:IFOPT"], "de:11000:900100001")
        self.assertEqual(halt["properties"]["railway"], "halt")
        self.assertEqual(halt["geometry"]["coordinates"], [13.41, 52.53])

    def test_station_importance_recognizes_main_station_names(self):
        for name in ("Berlin Hbf", "Hamburg Hauptbahnhof", "Amsterdam Centraal"):
            self.assertTrue(is_important_station(name))
        self.assertFalse(is_important_station("Berlin Südkreuz"))
        collection = station_features_from_osm({"elements": [
            {"type": "node", "id": 1, "lat": 52.52, "lon": 13.4,
             "tags": {"railway": "station", "name": "Berlin Hbf"}},
        ]}, north=53, south=52, west=13, east=14)
        self.assertIs(collection["features"][0]["properties"]["is_important"], True)

    def test_feature_collection_cache_round_trips_on_disk(self):
        collection = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [13.4, 52.5]},
             "properties": {"name": "Berlin Hbf"}},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "stations_cache.geojson"
            self.assertIsNone(load_feature_cache(path))
            save_json_cache(path, collection)
            self.assertEqual(load_feature_cache(path), collection)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
