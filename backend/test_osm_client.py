"""Offline PostGIS client and graph-conversion tests."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from osm_client import (
    OsmClient,
    load_feature_cache,
    save_json_cache,
    track_features_to_osm,
)


class OsmClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_postgis_queries_return_frontend_compatible_geojson(self):
        tracks = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "id": "osm:way:42",
            "properties": {"product": "nationalExpress"},
            "geometry": {"type": "LineString", "coordinates": [[8, 50], [8.1, 50.1]]},
        }]}
        stations = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "id": "osm:node:7",
            "properties": {"station_id": "osm:node:7", "name": "Test Hbf",
                           "is_important": True},
            "geometry": {"type": "Point", "coordinates": [8, 50]},
        }]}
        connection = AsyncMock()
        connection.fetchval.side_effect = [json.dumps(tracks), stations]
        pool = self.fake_pool(connection)
        client = OsmClient(database_url="postgresql://authenticated")
        client.pool = pool
        self.addAsyncCleanup(client.close)

        self.assertEqual(await client.get_track_feature_collection(), tracks)
        self.assertEqual(await client.get_station_feature_collection(), stations)
        track_query, station_query = [call.args[0] for call in connection.fetchval.await_args_list]
        self.assertIn("extensions.ST_AsGeoJSON(geom)", track_query)
        self.assertIn("FROM public.tracks", track_query)
        self.assertIn("json_build_object('product', product)", track_query)
        self.assertIn("extensions.ST_AsGeoJSON(geom)", station_query)
        self.assertIn("FROM public.stations", station_query)
        self.assertIn("'station_id', id", station_query)

    async def test_postgis_pool_rejects_rls_api_roles(self):
        connection = AsyncMock()
        connection.fetchval.return_value = "anon"
        pool = self.fake_pool(connection)
        client = OsmClient(database_url="postgresql://authenticated")
        with patch("osm_client.asyncpg.create_pool", new=AsyncMock(return_value=pool)):
            with self.assertRaisesRegex(RuntimeError, "authenticated Postgres role"):
                await client.connect()
        pool.close.assert_awaited_once()
        self.assertIsNone(client.pool)

    async def test_postgis_pool_requires_database_url(self):
        with patch.dict("os.environ", {}, clear=True):
            client = OsmClient()
        with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
            await client.connect()

    @staticmethod
    def fake_pool(connection):
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        pool.close = AsyncMock()
        return pool


class GeometryConversionTests(unittest.TestCase):
    def test_database_products_map_to_engine_rail_modes(self):
        collection = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": "osm:way:1",
             "properties": {"product": "nationalExpress"},
             "geometry": {"type": "LineString", "coordinates": [[8, 50], [8.1, 50.1]]}},
            {"type": "Feature", "id": "osm:way:2",
             "properties": {"product": "suburban"},
             "geometry": {"type": "LineString", "coordinates": [[9, 51], [9.1, 51.1]]}},
        ]}
        elements = track_features_to_osm(collection)["elements"]
        self.assertEqual(elements[0]["tags"], {
            "product": "nationalExpress", "railway": "rail",
        })
        self.assertEqual(elements[1]["tags"], {
            "product": "suburban", "railway": "light_rail",
        })
        self.assertEqual(elements[0]["id"], 1)

    def test_feature_collection_cache_remains_for_static_borders(self):
        collection = {"type": "FeatureCollection", "features": [{
            "type": "Feature",
            "geometry": {"type": "MultiLineString", "coordinates": [[[8, 50], [9, 51]]]},
            "properties": {"admin_level": "2"},
        }]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "borders.geojson"
            self.assertIsNone(load_feature_cache(path))
            save_json_cache(path, collection)
            self.assertEqual(load_feature_cache(path), collection)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
