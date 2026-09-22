"""Offline station discovery tests: python -m unittest -v test_station_client."""

import unittest

import httpx

from station_client import StationClient


class StationClientTests(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, handler):
        provider = StationClient("https://vbb.example.test/", request_interval=0,
                                 transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(provider.close)
        return provider

    async def test_nearby_stops_are_clipped_deduplicated_and_departure_ready(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=[
                {"type": "stop", "id": "900100002", "name": "S Hackescher Markt",
                 "location": {"latitude": 52.522605, "longitude": 13.402359},
                 "products": {"suburban": True, "bus": True}},
                {"type": "stop", "id": "900100002", "name": "duplicate",
                 "location": {"latitude": 52.522605, "longitude": 13.402359}},
                {"type": "stop", "id": "outside", "name": "bad", "location":
                 {"latitude": 52.9, "longitude": 13.4}},
                {"type": "poi", "id": "900100003", "location":
                 {"latitude": 52.522, "longitude": 13.402}},
            ])

        provider = await self.make_client(respond)
        collection = await provider.get_stations(52.53, 13.39, 52.51, 13.42)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/locations/nearby")
        self.assertEqual(len(collection["features"]), 1)
        feature = collection["features"][0]
        self.assertEqual(feature["id"], "900100002")
        self.assertEqual(feature["properties"]["station_id"], "900100002")
        self.assertEqual(feature["properties"]["name"], "S Hackescher Markt")
        self.assertEqual(feature["geometry"]["type"], "Point")

    async def test_all_upstream_failures_are_reported(self):
        provider = await self.make_client(lambda _: httpx.Response(503))
        with self.assertRaises(RuntimeError):
            await provider.get_stations(52.53, 13.39, 52.51, 13.42)

    async def test_invalid_bbox_is_rejected(self):
        provider = await self.make_client(lambda _: self.fail("Unexpected HTTP request"))
        with self.assertRaises(ValueError):
            await provider.get_stations(52.5, 13.4, 52.6, 13.3)

    async def test_resolves_clicked_osm_node_to_matching_vbb_stop(self):
        def respond(request):
            self.assertEqual(request.url.path, "/locations/nearby")
            self.assertEqual(request.url.params["distance"], "750")
            return httpx.Response(200, json=[
                {"type": "stop", "id": "900100001", "name": "Nearby bus stop",
                 "location": {"latitude": 52.5226, "longitude": 13.4024},
                 "products": {"bus": True}},
                {"type": "stop", "id": "900100002", "name": "S Hackescher Markt (Berlin)",
                 "location": {"latitude": 52.5228, "longitude": 13.4026},
                 "products": {"suburban": True}},
            ])

        provider = await self.make_client(respond)
        self.assertEqual(await provider.resolve_stop_id(52.522605, 13.402359,
                                                       "Hackescher Markt"), "900100002")


if __name__ == "__main__":
    unittest.main()
