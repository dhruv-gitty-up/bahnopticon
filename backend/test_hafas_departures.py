"""Offline HAFAS API contract tests: python -m unittest -v test_hafas_departures."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from hafas_client import (
    HafasClient, RadarSubprocessError, RadarSubprocessTimeout,
    TripSubprocessError, TripSubprocessTimeout,
)


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, block=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = None if block else returncode
        self.final_returncode = returncode
        self.block = block
        self.killed = False

    async def communicate(self):
        if self.block:
            await asyncio.Event().wait()
        self.returncode = self.final_returncode
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class HafasClientTests(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, handler):
        provider = HafasClient("https://vbb.example.test/")
        await provider.close()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(provider.close)
        return provider

    async def test_radar_runs_node_subprocess_and_filters_to_four_rail_products(self):
        process = FakeProcess(stdout=json.dumps({"movements": [
                {"line": {"product": "nationalExpress"}},
                {"line": {"product": "national"}},
                {"line": {"product": "regional"}},
                {"line": {"product": "suburban"}},
                {"line": {"product": "subway"}},
                {"line": {"product": "bus"}},
                {"line": {"product": "tram"}},
                {"line": {"product": "ferry"}},
                {"line": {"product": "express", "name": "ICE 651"}},
                {"line": {"product": ["subway"]}},
                {"line": None},
                None,
            ]}).encode())
        provider = HafasClient(node_binary="node-test")
        self.addAsyncCleanup(provider.close)
        with patch("hafas_client.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=process) as create_process:
            data = await provider.get_radar()
        self.assertEqual([item["line"]["product"] for item in data["movements"]],
                         ["nationalExpress", "national", "regional", "suburban"])
        command = create_process.await_args.args
        self.assertEqual(command[0], "node-test")
        self.assertEqual(command[1], str(provider.radar_script))

    async def test_trip_bridge_converts_ordered_hafas_points_to_linestring(self):
        points = [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": coordinates}}
            for coordinates in ([7.0, 51.0], [7.0, 51.0], [8.0, 52.0])
        ]
        process = FakeProcess(stdout=json.dumps({"trip": {
            "polyline": {"type": "FeatureCollection", "features": points},
            "line": {"name": "ICE 74"}, "destination": {"name": "Berlin Hbf"},
        }}).encode())
        provider = HafasClient(node_binary="node-test")
        self.addAsyncCleanup(provider.close)
        with patch("hafas_client.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=process) as create_process:
            feature = await provider.get_trip("trip/#123")
        self.assertEqual(feature["geometry"], {
            "type": "LineString", "coordinates": [[7.0, 51.0], [8.0, 52.0]],
        })
        self.assertEqual(feature["properties"]["destination"], "Berlin Hbf")
        self.assertEqual(create_process.await_args.args[2], "trip/#123")

    async def test_trip_bridge_rejects_missing_polyline_and_times_out(self):
        provider = HafasClient(trip_timeout=0.001)
        self.addAsyncCleanup(provider.close)
        with patch("hafas_client.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=FakeProcess(stdout=b'{"trip": {}}')):
            with self.assertRaises(TripSubprocessError):
                await provider.get_trip("missing-geometry")
        blocked = FakeProcess(block=True)
        with patch("hafas_client.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=blocked):
            with self.assertRaises(TripSubprocessTimeout):
                await provider.get_trip("slow-trip")
        self.assertTrue(blocked.killed)

    async def test_radar_subprocess_timeout_kills_child_and_propagates(self):
        process = FakeProcess(block=True)
        provider = HafasClient(radar_timeout=0.001)
        self.addAsyncCleanup(provider.close)
        with patch("hafas_client.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=process):
            with self.assertRaises(RadarSubprocessTimeout):
                await provider.get_radar()
        self.assertTrue(process.killed)

    async def test_radar_nonzero_exit_and_malformed_output_are_rejected(self):
        provider = HafasClient()
        self.assertEqual(provider.radar_script.name, "hafas_radar.js")
        self.assertEqual(provider.radar_timeout, 75.0)
        self.assertEqual(provider.client.timeout.read, 25.0)
        self.addAsyncCleanup(provider.close)
        failed = FakeProcess(stderr=b"provider down", returncode=1)
        malformed = FakeProcess(stdout=b"not-json")
        with patch("hafas_client.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   side_effect=[failed, malformed]):
            with self.assertRaisesRegex(RadarSubprocessError, "provider down"):
                await provider.get_radar()
            with self.assertRaisesRegex(RadarSubprocessError, "invalid JSON"):
                await provider.get_radar()

    async def test_departures_v6_board_preserves_delay_and_cancellation(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"departures": [
                {"tripId": "bus-1", "line": {"name": "M41", "product": "bus"},
                 "direction": "Hauptbahnhof", "when": "2026-09-17T10:03:30+02:00",
                 "plannedWhen": "2026-09-17T10:01:00+02:00", "delay": 150,
                 "platform": "A", "plannedPlatform": "B"},
                {"tripId": "tram-2", "line": {"name": "M2", "product": "tram"},
                 "direction": "Alexanderplatz", "when": None,
                 "plannedWhen": "2026-09-17T10:05:00+02:00", "delay": None,
                 "cancelled": True},
                {"tripId": "s-3", "line": {"name": "S3", "product": "suburban"},
                 "when": "2026-09-17T10:07:00+02:00", "delay": -60},
                {"when": None, "plannedWhen": None},
                None,
            ]})

        provider = await self.make_client(respond)
        board = await provider.get_departures("900013102")
        self.assertEqual(requests[0].url.path, "/stops/900013102/departures")
        self.assertEqual(requests[0].url.params["results"], "10")
        self.assertGreater(int(requests[0].url.params["duration"]), 10)
        self.assertEqual(len(board), 3)
        self.assertEqual(board[0], {
            "trip_id": "bus-1", "line_name": "M41", "product": "bus",
            "direction": "Hauptbahnhof", "when": "2026-09-17T10:03:30+02:00",
            "planned_when": "2026-09-17T10:01:00+02:00",
            "expected_time": "2026-09-17T10:03:30+02:00",
            "scheduled_time": "2026-09-17T10:01:00+02:00",
            "delay_minutes": 2.5, "platform": "A", "planned_platform": "B",
            "cancelled": False,
        })
        self.assertEqual(board[1]["product"], "tram")
        self.assertTrue(board[1]["cancelled"])
        self.assertIsNone(board[1]["when"])
        self.assertIsNone(board[1]["delay_minutes"])
        self.assertEqual(board[2]["delay_minutes"], -1)

    async def test_legacy_array_is_accepted_and_result_is_capped(self):
        def respond(_):
            rows = [{"when": f"2026-09-17T10:{minute:02d}:00+02:00"}
                    for minute in range(12)]
            return httpx.Response(200, json=rows)

        provider = await self.make_client(respond)
        board = await provider.get_departures("900013102", limit=10)
        self.assertEqual(len(board), 10)
        self.assertEqual(board[-1]["when"], "2026-09-17T10:09:00+02:00")

    async def test_bad_payload_and_upstream_http_error_are_not_treated_as_empty_board(self):
        bad = await self.make_client(lambda _: httpx.Response(200, json={"error": "bad"}))
        with self.assertRaises(ValueError):
            await bad.get_departures("900013102")

        unavailable = await self.make_client(lambda _: httpx.Response(503))
        with self.assertRaises(httpx.HTTPStatusError) as caught:
            await unavailable.get_departures("900013102")
        self.assertEqual(caught.exception.response.status_code, 503)

    async def test_invalid_station_or_limit_is_rejected_before_request(self):
        provider = await self.make_client(lambda _: self.fail("Unexpected HTTP request"))
        with self.assertRaises(ValueError):
            await provider.get_departures(" ")
        with self.assertRaises(ValueError):
            await provider.get_departures("900013102", limit=11)


if __name__ == "__main__":
    unittest.main()
