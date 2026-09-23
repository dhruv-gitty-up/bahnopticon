"""Offline regressions: python -m unittest -v test_backend."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock, patch
import httpx
import main
from engine import InterpolationEngine


def movement(trip_id="trip-1", **updates):
    result = {"tripId": trip_id, "line": {"product": "suburban", "name": "S1"},
              "location": {"longitude": 13.4, "latitude": 52.5},
              "nextStopovers": [{"arrivalDelay": None, "departureDelay": 120}]}
    result.update(updates)
    return result


class CorsTests(unittest.IsolatedAsyncioTestCase):
    async def preflight(self, origin):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            return await client.options("/stream", headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Last-Event-ID",
            })

    async def test_vercel_and_cloudflare_tunnel_origins_pass_preflight(self):
        for origin in (
            "https://nachhause.vercel.app",
            "https://bahnopticon.vercel.app",
            "https://bahnopticon-git-preview-team.vercel.app",
            "https://random-words.trycloudflare.com",
        ):
            with self.subTest(origin=origin):
                response = await self.preflight(origin)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["access-control-allow-origin"], origin)
                self.assertEqual(response.headers["access-control-allow-credentials"], "true")
                self.assertIn("GET", response.headers["access-control-allow-methods"])

    async def test_untrusted_https_origin_is_not_allowed(self):
        response = await self.preflight("https://example.com")
        self.assertNotIn("access-control-allow-origin", response.headers)


class AnalyticsTests(unittest.IsolatedAsyncioTestCase):
    ALL_STATES = {
        "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen",
        "Hamburg", "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen",
        "Nordrhein-Westfalen", "Rheinland-Pfalz", "Saarland", "Sachsen",
        "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen",
    }

    async def test_seven_day_mock_contract(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            response = await client.get("/analytics/7day")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["period_days"], 7)
        self.assertIs(payload["is_mock"], True)
        self.assertEqual(
            {row["label"] for row in payload["network_performance"]},
            {"ICE", "Regional", "S-Bahn"},
        )
        self.assertTrue(all(
            0 <= row["on_time_probability"] <= 1
            and row["average_delay_minutes"] >= 0
            for row in payload["network_performance"]
        ))
        self.assertEqual(
            {row["bundesland"] for row in payload["regional_performance"]},
            self.ALL_STATES,
        )
        self.assertEqual(len(payload["regional_performance"]), 16)
        self.assertTrue(all(
            0 <= row["regional_on_time_percentage"] <= 100
            and 0 <= row["suburban_on_time_percentage"] <= 100
            for row in payload["regional_performance"]
        ))

    async def test_vehicle_mock_contract_accepts_line_name(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            first = await client.get("/analytics/vehicle/ICE%20592")
            repeated = await client.get("/analytics/vehicle/ICE%20592")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), repeated.json())
        payload = first.json()
        self.assertEqual(payload["line_id"], "ICE 592")
        self.assertEqual(payload["period_days"], 7)
        self.assertIs(payload["is_mock"], True)
        self.assertTrue(0 <= payload["on_time_probability"] <= 1)
        self.assertGreaterEqual(payload["average_delay_minutes"], 0)


class DeploymentTests(unittest.TestCase):
    def test_server_binds_publicly_to_render_port(self):
        with patch.dict(main.os.environ, {"PORT": "4321"}), \
             patch.object(main.uvicorn, "run") as run:
            main.run_server()
        run.assert_called_once_with(
            main.app,
            host="0.0.0.0",
            port=4321,
            timeout_graceful_shutdown=10,
        )

    def test_server_defaults_to_port_8000(self):
        with patch.dict(main.os.environ, {}, clear=True), \
             patch.object(main.uvicorn, "run") as run:
            main.run_server()
        self.assertEqual(run.call_args.kwargs["port"], 8000)


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        main.engine = InterpolationEngine()

    def test_bad_rows_do_not_discard_valid_vehicles(self):
        rows = [None, {}, movement("bad", location=None),
                movement("nan", location={"longitude": float("nan"), "latitude": 52}),
                movement("range", location={"longitude": 200, "latitude": 52}),
                movement("line", line=[]), movement("product", line={"product": []}), movement()]
        result = main.normalize_movements({"movements": rows}, 10)
        self.assertEqual(len(result["features"]), 1)
        feature = result["features"][0]
        self.assertEqual(feature["id"], "trip-1")
        self.assertEqual(feature["properties"]["delay_minutes"], 2)
        self.assertEqual(feature["geometry"]["coordinates"], [13.4, 52.5])
        json.dumps(result, allow_nan=False)

    def test_unknown_delay_defaults_to_zero(self):
        result = main.normalize_movements({"movements": [movement(nextStopovers=None)]}, 10)
        self.assertEqual(result["features"][0]["properties"]["delay_minutes"], 0)

    def test_non_lod_products_are_excluded_from_stream_snapshot(self):
        result = main.normalize_movements({"movements": [
            movement("bus", line={"product": "bus", "name": "M41"}),
            movement("tram", line={"product": "tram", "name": "M10"}),
            movement("ferry", line={"product": "ferry", "name": "F10"}),
            movement("ice", line={"product": "express", "name": "ICE 651"}),
            movement("subway", line={"product": "subway", "name": "U8"}),
            movement("regional", line={"product": "regional", "name": "RE1"}),
        ]}, 10)
        self.assertEqual([feature["properties"]["product"] for feature in result["features"]],
                         ["regional"])

    def test_only_requested_rail_products_reach_engine(self):
        result = main.normalize_movements({"movements": [
            movement("national-express", line={"product": "nationalExpress", "name": "ICE 123"}),
            movement("national", line={"product": "national", "name": "IC 456"}),
            movement("regional", line={"product": "regional", "name": "RE1"}),
            movement("suburban", line={"product": "suburban", "name": "S1"}),
            movement("subway", line={"product": "subway", "name": "U8"}),
            movement("regional-express", line={"product": "regionalExpress", "name": "RE2"}),
            movement("ice", line={"product": "express", "name": "ICE 651"}),
        ]}, 10)
        self.assertEqual({f["id"]: f["properties"]["product"] for f in result["features"]},
                         {"national-express": "nationalExpress", "national": "national",
                          "regional": "regional", "suburban": "suburban"})

    def test_stopover_timing_and_route_metadata_are_added(self):
        data = movement("timed", line={"product": "suburban", "name": "S1"}, route="S1 Oranienburg",
                         frames=[{"origin": {"name": "Hermannplatz"}}], nextStopovers=[
                             {"stop": {"name": "Sonnenallee"},
                              "plannedArrival": "2026-09-17T23:30:00+02:00",
                              "arrival": "2026-09-17T23:31:00+02:00"},
                             {"stop": {"name": "Oranienburg"}},
                         ])
        properties = main.normalize_movements({"movements": [data]}, 10)["features"][0]["properties"]
        self.assertNotIn("previous_station", properties)
        self.assertEqual(properties["next_station"], "Sonnenallee")
        self.assertEqual(properties["destination"], "Oranienburg")
        self.assertEqual(properties["scheduled_time"], "2026-09-17T23:30:00+02:00")
        self.assertEqual(properties["expected_time"], "2026-09-17T23:31:00+02:00")
        self.assertEqual(properties["next_station_scheduled_time"],
                         "2026-09-17T23:30:00+02:00")
        self.assertEqual(properties["next_station_expected_time"],
                         "2026-09-17T23:31:00+02:00")
        self.assertEqual(properties["next_station_delay_minutes"], 1)
        self.assertEqual(properties["route"], "S1 Oranienburg")

    def test_past_origin_is_not_misreported_as_next_stop(self):
        # The live ÖBB radar bridge can return the entire journey, including
        # the morning origin, even while the train is moving after noon.
        now = datetime.fromisoformat("2026-09-22T12:10:00+02:00").timestamp()
        data = movement("ice-529", direction="München Hbf", nextStopovers=[
            {"stop": {"name": "Dortmund Hbf"},
             "plannedDeparture": "2026-09-22T07:19:00+02:00",
             "departure": "2026-09-22T07:20:00+02:00", "departureDelay": 60},
            {"stop": {"name": "Würzburg Hbf"},
             "plannedArrival": "2026-09-22T11:03:00+02:00",
             "arrival": "2026-09-22T11:24:00+02:00", "arrivalDelay": 1260},
            {"stop": {"name": "Nürnberg Hbf"},
             "plannedArrival": "2026-09-22T11:58:00+02:00",
             "arrival": "2026-09-22T12:17:00+02:00", "arrivalDelay": 1140},
            {"stop": {"name": "München Hbf"},
             "plannedArrival": "2026-09-22T13:12:00+02:00",
             "arrival": "2026-09-22T13:27:00+02:00"},
        ])
        properties = main.normalize_movements({"movements": [data]}, now)["features"][0]["properties"]
        self.assertEqual(properties["next_station"], "Nürnberg Hbf")
        self.assertEqual(properties["destination"], "München Hbf")
        self.assertEqual(properties["scheduled_time"], "2026-09-22T11:58:00+02:00")
        self.assertEqual(properties["expected_time"], "2026-09-22T12:17:00+02:00")
        self.assertEqual(properties["delay_minutes"], 19)

    def test_actual_arrival_overrides_future_planned_arrival(self):
        now = datetime.fromisoformat("2026-09-22T12:10:00+02:00").timestamp()
        data = movement("early", nextStopovers=[
            {"stop": {"name": "Already reached"},
             "plannedArrival": "2026-09-22T12:15:00+02:00",
             "arrival": "2026-09-22T12:05:00+02:00"},
            {"stop": {"name": "Next stop"},
             "plannedArrival": "2026-09-22T12:30:00+02:00",
             "arrival": "2026-09-22T12:30:00+02:00"},
        ])
        properties = main.normalize_movements({"movements": [data]}, now)["features"][0]["properties"]
        self.assertEqual(properties["next_station"], "Next stop")

    def test_destination_falls_back_to_direction_and_naive_times_are_rejected(self):
        data = movement("fallback", direction="München Hbf", nextStopovers=[
            {"stop": {}, "plannedArrival": "2026-09-17T22:00:00+02:00"},
            {"stop": {"name": "Augsburg Hbf"},
             "plannedArrival": "2026-09-17T22:30:00",
             "arrival": "2026-09-17T20:31:00Z"},
        ])
        properties = main.normalize_movements({"movements": [data]}, 10)["features"][0]["properties"]
        self.assertEqual(properties["next_station"], "Augsburg Hbf")
        self.assertEqual(properties["destination"], "Augsburg Hbf")
        self.assertIsNone(properties["scheduled_time"])
        self.assertEqual(properties["expected_time"], "2026-09-17T20:31:00Z")

        data["nextStopovers"] = []
        properties = main.normalize_movements({"movements": [data]}, 20)["features"][0]["properties"]
        self.assertIsNone(properties["next_station"])
        self.assertEqual(properties["destination"], "München Hbf")

    def test_stopover_missing_times_are_null_and_line_is_route_fallback(self):
        data = movement("untimed", line={"product": "national", "name": "IC 8"}, nextStopovers=[
            {"stop": {"name": "Invalid"}, "plannedArrival": "not-a-time", "arrival": None},
        ])
        properties = main.normalize_movements({"movements": [data]}, 10)["features"][0]["properties"]
        self.assertEqual(properties["next_station"], "Invalid")
        self.assertIsNone(properties["scheduled_time"])
        self.assertIsNone(properties["expected_time"])
        self.assertEqual(properties["route"], "IC 8")

    def test_partial_minute_delays_and_text_fields_are_preserved_safely(self):
        result = main.normalize_movements({"movements": [movement(
            line={"name": {}, "product": "suburban"}, direction=[],
            nextStopovers=[{"arrivalDelay": 30}])]}, 10)
        properties = result["features"][0]["properties"]
        self.assertEqual(properties["delay_minutes"], 0.5)
        self.assertEqual(properties["line_name"], "Unknown")
        self.assertEqual(properties["destination"], "Unknown")
        self.assertEqual(result["generated_at"], 10)
        result = main.normalize_movements({"movements": [movement(
            nextStopovers=[{"arrivalDelay": True}])]}, 20)
        self.assertEqual(result["features"][0]["properties"]["delay_minutes"], 0)

    def test_duplicate_trips_are_collapsed_and_recent_state_is_retained(self):
        main.normalize_movements({"movements": [movement("recent")]}, 1)
        result = main.normalize_movements({"movements": [movement(), movement()]}, 10)
        self.assertEqual(len(result["features"]), 1)
        self.assertEqual(set(main.engine.vehicle_states), {"recent", "trip-1"})
        main.normalize_movements({"movements": []}, 20)
        self.assertEqual(set(main.engine.vehicle_states), {"recent", "trip-1"})

    def test_vehicle_state_updated_sixteen_minutes_ago_is_purged(self):
        main.engine.compute_trajectory("stale-trip", 13.4, 52.5, 100)
        main.engine.compute_trajectory("fresh-trip", 13.4, 52.5, 1_000)

        with self.assertLogs("engine", level="INFO") as logs:
            purged = main.engine.purge_stale_states(1_060)

        self.assertEqual(purged, 1)
        self.assertNotIn("stale-trip", main.engine.vehicle_states)
        self.assertIn("fresh-trip", main.engine.vehicle_states)
        self.assertIn("Evicted 1 stale vehicles", logs.output[0])

    def test_dach_coordinate_outliers_are_dropped(self):
        result = main.normalize_movements({"movements": [
            movement("berlin"),
            movement("new-york", location={"longitude": -74.006, "latitude": 40.7128}),
            movement("east-outlier", location={"longitude": 17.3, "latitude": 48.2}),
        ]}, 10)
        self.assertEqual([feature["id"] for feature in result["features"]], ["berlin"])

    def test_malformed_delays_default_to_numeric_zero(self):
        cases = [None, True, "120", float("nan"), float("inf")]
        for index, raw_delay in enumerate(cases):
            with self.subTest(raw_delay=raw_delay):
                result = main.normalize_movements({"movements": [movement(
                    f"delay-{index}", nextStopovers=[{"arrivalDelay": raw_delay}]
                )]}, index + 1)
                delay = result["features"][0]["properties"]["delay_minutes"]
                self.assertEqual(delay, 0)
                self.assertIsInstance(delay, (int, float))

    def test_malformed_snapshot_preserves_previous_state(self):
        main.normalize_movements({"movements": [movement()]}, 1)
        with self.assertRaises(ValueError):
            main.normalize_movements({"error": "unavailable"}, 10)
        self.assertIn("trip-1", main.engine.vehicle_states)

    def test_sparse_nationwide_track_emits_straight_linestring(self):
        main.normalize_movements({"movements": [movement()]}, 10)
        moved = movement(location={"longitude": 13.41, "latitude": 52.51})
        feature = main.normalize_movements({"movements": [moved]}, 25)["features"][0]
        self.assertEqual(feature["properties"]["route_status"], "straight")
        self.assertEqual(feature["geometry"], {"type": "LineString", "coordinates": [
            [13.4, 52.5], [13.41, 52.51],
        ]})

    def test_duration_handles_clock_changes_and_outages(self):
        main.engine.compute_trajectory("trip", 13, 52, 100)
        self.assertEqual(main.engine.compute_trajectory("trip", 13, 52, 99)["duration_ms"], 0)
        self.assertEqual(main.engine.compute_trajectory("trip", 13, 52, 1000)["duration_ms"], 60000)

    def test_malformed_tracks_are_skipped(self):
        main.engine.load_osm_data({"elements": [None, {}, {"type": "way", "geometry": [{}]},
            {"type": "way", "geometry": [{"lon": 13, "lat": 52}, {"lon": 14, "lat": 52}]}]})
        lon, lat = main.engine.snap_to_track(13.5, 52.001)
        self.assertAlmostEqual(lon, 13.5)
        self.assertAlmostEqual(lat, 52)
        # A distant railway is not a trustworthy snap target.
        self.assertEqual(main.engine.snap_to_track(13.5, 52.1), (13.5, 52.1))

    def test_retry_after_is_respected(self):
        request = httpx.Request("GET", "https://example.test/radar")
        response = httpx.Response(429, headers={"Retry-After": "120"}, request=request)
        error = httpx.HTTPStatusError("rate limited", request=request, response=response)
        self.assertEqual(main.retry_delay(error, 5), 120)
        response.headers["Retry-After"] = "invalid"
        self.assertEqual(main.retry_delay(error, 5), 5)


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.client_queues.clear()
        main.latest_payload = None
        main.track_payload = None
        main.station_payload = None
        main.border_payload = None
        main.station_by_id = {}
        main.osm_stop_ids.clear()
        main.departure_cache.clear()
        main.trip_cache.clear()
        main.engine = InterpolationEngine()

    async def test_slow_client_keeps_only_latest_snapshot(self):
        queue = asyncio.Queue(maxsize=1)
        main.client_queues.add(queue)
        await main.broadcast({"features": [1]})
        await asyncio.wait_for(main.broadcast({"features": [2]}), timeout=1)
        self.assertEqual(queue.qsize(), 1)
        self.assertEqual(json.loads(queue.get_nowait()[6:])["features"], [2])

    async def test_final_stream_gate_logs_telemetry_and_mock_warning(self):
        snapshot = {
            "type": "FeatureCollection", "generated_at": 1_234, "is_mock": True,
            "features": [{
                "type": "Feature", "id": "mock:1",
                "properties": {
                    "trip_id": "mock:1", "product": "regional", "delay_minutes": None,
                    "scheduled_time": "2026-09-20T10:00:00+02:00",
                    "expected_time": "2026-09-20T10:01:00+02:00", "is_mock": True,
                },
                "geometry": {"type": "Point", "coordinates": [13.4, 52.5]},
            }],
        }

        with self.assertLogs("main", level="INFO") as logs:
            await main.broadcast(snapshot)

        payload = json.loads(main.latest_payload[6:])
        self.assertEqual(payload["features"][0]["properties"]["delay_minutes"], 0)
        self.assertTrue(any("MOCK TRAFFIC ACTIVE" in line for line in logs.output))
        self.assertTrue(any(
            "Telemetry sample 1" in line
            and "trip_id=mock:1" in line
            and "generated_at=1234" in line
            and "delay_minutes=0" in line
            for line in logs.output
        ))

    async def test_final_stream_gate_drops_outlier_geometry(self):
        snapshot = {
            "type": "FeatureCollection", "features": [{
                "type": "Feature", "id": "outlier", "properties": {"delay_minutes": 1},
                "geometry": {"type": "Point", "coordinates": [-74.006, 40.7128]},
            }],
        }

        await main.broadcast(snapshot)

        self.assertEqual(json.loads(main.latest_payload[6:])["features"], [])

    async def test_stream_replays_cached_snapshot_and_cleans_up(self):
        await main.broadcast({"type": "FeatureCollection", "features": []})
        request = AsyncMock()
        request.is_disconnected.return_value = False
        response = await main.sse_endpoint(request)
        stream = response.body_iterator
        self.assertEqual(await anext(stream), main.latest_payload)
        self.assertEqual(len(main.client_queues), 1)
        await stream.aclose()
        self.assertFalse(main.client_queues)

    async def test_lifespan_cancels_workers_and_closes_clients(self):
        cancelled = []
        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        hafas, osm, stations = AsyncMock(), AsyncMock(), AsyncMock()
        with patch.object(main, "HafasClient", return_value=hafas), \
             patch.object(main, "OsmClient", return_value=osm), \
             patch.object(main, "StationClient", return_value=stations), \
             patch.object(main, "polling_loop", worker), \
             patch.object(main, "load_map_data", worker), \
             patch.dict(main.os.environ, {"DATABASE_URL": "postgresql://test"}):
            async with main.lifespan(main.app):
                await asyncio.sleep(0)
            self.assertEqual(len(cancelled), 2)
            osm.connect.assert_awaited_once()
            hafas.close.assert_awaited_once()
            osm.close.assert_awaited_once()
            stations.close.assert_awaited_once()

    async def test_map_data_loads_tracks_before_stations(self):
        order = []

        async def tracks():
            order.append("tracks")

        async def stations():
            order.append("stations")

        with patch.object(main, "load_tracks", tracks), patch.object(main, "load_stations", stations):
            await main.load_map_data()
        self.assertEqual(order, ["tracks", "stations"])

    async def test_polling_recovers_and_preserves_cache_during_outage(self):
        await main.broadcast({"type": "FeatureCollection", "features": ["cached"]})
        cached = main.latest_payload
        provider = AsyncMock()
        provider.get_radar.side_effect = [httpx.ConnectError("offline"), {"movements": [movement()]}]
        simulator_started = asyncio.Event()
        async def idle_simulator():
            simulator_started.set()
            await asyncio.Event().wait()
        waits = []
        async def sleep(delay):
            waits.append(delay)
            if len(waits) == 1:
                await simulator_started.wait()
                self.assertEqual(main.latest_payload, cached)
                self.assertEqual(main.upstream_status, "unavailable")
            else:
                raise asyncio.CancelledError
        with patch.object(main, "hafas", provider), \
             patch.object(main, "simulation_loop", idle_simulator), \
             patch.object(main.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await main.polling_loop()
        self.assertEqual(main.upstream_status, "live")
        self.assertTrue(simulator_started.is_set())
        self.assertEqual(waits, [5, main.POLL_INTERVAL])
        self.assertEqual(json.loads(main.latest_payload[6:])["features"][0]["id"], "trip-1")

    async def test_polling_logs_http_status_and_response_body(self):
        request = httpx.Request("GET", "https://vbb.example.test/radar")
        response = httpx.Response(429, text="quota exceeded", request=request)
        provider = AsyncMock()
        provider.get_radar.side_effect = httpx.HTTPStatusError(
            "rate limited", request=request, response=response)
        async def stop_after_failure(_delay):
            raise asyncio.CancelledError

        with patch.object(main, "hafas", provider), \
             patch.object(main.asyncio, "sleep", stop_after_failure), \
             self.assertLogs("main", level="WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await main.polling_loop()
        self.assertIn("HTTP 429; response body: quota exceeded", logs.output[0])
        self.assertEqual(main.upstream_status, "unavailable")

    async def test_node_radar_snapshot_publishes_only_rail_products(self):
        provider = AsyncMock()
        provider.get_radar.return_value = {"movements": [
            movement("local-bus", line={"product": "bus", "name": "M41"}),
            movement("rail-ice", line={"product": "nationalExpress", "name": "ICE 1"}),
        ]}
        async def stop_after_publish(_delay):
            raise asyncio.CancelledError

        with patch.object(main, "hafas", provider), \
             patch.object(main.asyncio, "sleep", stop_after_publish):
            with self.assertRaises(asyncio.CancelledError):
                await main.polling_loop()
        self.assertEqual(main.upstream_status, "live")
        self.assertEqual(json.loads(main.latest_payload[6:])["features"][0]["id"], "rail-ice")

    async def test_node_radar_timeout_starts_simulation_and_replays_mock_snapshot(self):
        mock_way = way([1, 2, 3], [(13.36, 52.51), (13.37, 52.51), (13.38, 52.51)])
        mock_way["tags"] = {"railway": "rail", "highspeed": "yes"}
        main.engine.load_osm_data({"elements": [mock_way]})
        provider = AsyncMock()
        provider.get_radar.side_effect = main.RadarSubprocessTimeout("deadline exceeded")
        simulated = asyncio.Event()
        async def simulation_once():
            await main.broadcast(await asyncio.to_thread(main.engine.generate_mock_traffic, 10))
            simulated.set()
            await asyncio.Event().wait()
        async def stop_after_failure(_delay):
            await simulated.wait()
            raise asyncio.CancelledError

        with patch.object(main, "hafas", provider), \
             patch.object(main, "simulation_loop", simulation_once), \
             patch.object(main.asyncio, "sleep", stop_after_failure), \
             self.assertLogs("main", level="WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await main.polling_loop()
        self.assertIn("Node HAFAS radar timeout", logs.output[0])
        self.assertTrue(any("Upstream API failed. Yielding simulated traffic along physical tracks."
                            in line for line in logs.output))
        self.assertEqual(main.upstream_status, "unavailable")
        payload = json.loads(main.latest_payload[6:])
        self.assertTrue(payload["is_mock"])
        self.assertEqual(len(payload["features"]), 20)
        request = AsyncMock()
        request.is_disconnected.return_value = False
        replay = await main.sse_endpoint(request)
        stream = replay.body_iterator
        self.assertEqual(await anext(stream), main.latest_payload)
        await stream.aclose()

    async def test_node_radar_failure_starts_simulation(self):
        provider = AsyncMock()
        simulated = asyncio.Event()
        async def simulation_once():
            simulated.set()
            await asyncio.Event().wait()
        async def stop_after_failure(_delay):
            await simulated.wait()
            raise asyncio.CancelledError
        provider.get_radar.side_effect = main.RadarSubprocessError("provider down")
        with patch.object(main, "hafas", provider), \
             patch.object(main, "simulation_loop", simulation_once), \
             patch.object(main.asyncio, "sleep", stop_after_failure), \
             self.assertLogs("main", level="WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await main.polling_loop()
        self.assertTrue(simulated.is_set())
        self.assertTrue(any("Node HAFAS radar failed" in line and "provider down" in line
                            for line in logs.output))

    async def test_simulated_snapshots_keep_fifteen_second_cadence(self):
        rail = way([1, 2, 3], [(13.36, 52.51), (13.37, 52.51), (13.38, 52.51)])
        rail["tags"] = {"railway": "rail", "highspeed": "yes"}
        main.engine.load_osm_data({"elements": [rail]})
        snapshots = []
        async def capture(snapshot):
            snapshots.append(snapshot)
        async def tick(delay):
            self.assertEqual(delay, main.SIMULATION_INTERVAL)
            if len(snapshots) == 2:
                raise asyncio.CancelledError

        with patch.object(main, "broadcast", capture), patch.object(main.asyncio, "sleep", tick):
            with self.assertRaises(asyncio.CancelledError):
                await main.simulation_loop()
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0]["features"][0]["geometry"]["type"], "Point")
        self.assertEqual(snapshots[1]["features"][0]["geometry"]["type"], "LineString")

    async def test_live_node_recovery_stops_simulator_before_broadcast(self):
        provider = AsyncMock()
        provider.get_radar.side_effect = [
            main.RadarSubprocessError("outage"),
            {"movements": [movement("recovered")]},
        ]
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def simulator():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        async def sleep(delay):
            if delay == 5:
                await started.wait()
            else:
                raise asyncio.CancelledError

        with patch.object(main, "hafas", provider), \
             patch.object(main, "simulation_loop", simulator), \
             patch.object(main.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await main.polling_loop()
        self.assertTrue(stopped.is_set())
        self.assertEqual(main.upstream_status, "live")
        payload = json.loads(main.latest_payload[6:])
        self.assertNotIn("is_mock", payload)
        self.assertEqual(payload["features"][0]["id"], "recovered")

    async def test_track_loader_uses_postgis_collection_and_builds_graph(self):
        collection = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "id": "osm:way:42",
            "properties": {"product": "nationalExpress"},
            "geometry": {"type": "LineString", "coordinates": [[8.0, 50.0], [8.1, 50.1]]},
        }]}
        provider = AsyncMock()
        provider.get_track_feature_collection.return_value = collection
        with patch.object(main, "osm", provider), self.assertLogs("main", level="INFO") as logs:
            await asyncio.wait_for(main.load_tracks(), timeout=1)
        provider.get_track_feature_collection.assert_awaited_once_with()
        self.assertIsNotNone(main.engine._graph)
        response = await main.tracks()
        self.assertEqual(json.loads(response.body)["features"][0]["id"], "osm:way:42")
        self.assertTrue(any("Loaded 1 railway tracks from PostGIS" in line for line in logs.output))

    async def test_track_loader_retries_after_postgis_failure(self):
        collection = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "id": "osm:way:42",
            "properties": {"product": "nationalExpress"},
            "geometry": {"type": "LineString", "coordinates": [[8.0, 50.0], [8.1, 50.1]]},
        }]}
        provider = AsyncMock()
        provider.get_track_feature_collection.side_effect = [RuntimeError("database unavailable"), collection]
        with patch.object(main, "osm", provider), \
             patch.object(main.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            await main.load_tracks()
        self.assertEqual(provider.get_track_feature_collection.await_count, 2)
        sleep.assert_awaited_once_with(60)

    async def test_tracks_are_served_while_graph_rebuilds(self):
        collection = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "id": "osm:way:42",
            "properties": {"product": "nationalExpress"},
            "geometry": {"type": "LineString", "coordinates": [[8.0, 50.0], [8.1, 50.1]]},
        }]}
        rebuilding = threading.Event()
        release = threading.Event()
        actual_load = main.engine.load_osm_data
        def delayed_load(osm_data):
            rebuilding.set()
            release.wait(2)
            return actual_load(osm_data)
        provider = AsyncMock()
        provider.get_track_feature_collection.return_value = collection
        with patch.object(main, "osm", provider), \
             patch.object(main.engine, "load_osm_data", side_effect=delayed_load):
            task = asyncio.create_task(main.load_tracks())
            try:
                self.assertTrue(await asyncio.to_thread(rebuilding.wait, 1))
                response = await main.tracks()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(json.loads(response.body)["features"]), 1)
            finally:
                release.set()
                await task
        provider.get_track_feature_collection.assert_awaited_once_with()

    async def test_geometry_endpoint_serves_cached_linestrings(self):
        for endpoint in (main.tracks, main.geometry):
            with self.subTest(endpoint=endpoint.__name__), \
                 self.assertLogs("main", level="ERROR") as logs, \
                 self.assertRaises(main.HTTPException) as error:
                await endpoint()
            self.assertEqual(error.exception.status_code, 503)
            self.assertEqual(error.exception.detail, main.TRACKS_UNAVAILABLE_DETAIL)
            self.assertEqual(error.exception.headers, {"Retry-After": "60"})
            self.assertIn("PostGIS-backed in-memory cache is empty", logs.output[0])
        rail = way([1, 2, 3], [(13.4, 52.5), (13.401, 52.5), (13.401, 52.501)])
        rail.update({"id": 42, "tags": {"railway": "rail", "name": "Berlin test track"}})
        self.assertTrue(main.engine.load_osm_data({"elements": [rail]}))
        main.track_payload = main.engine.geometry_payload
        response = await main.geometry()
        self.assertEqual(response.media_type, "application/geo+json")
        collection = json.loads(response.body)
        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual(collection["features"][0]["id"], "osm:way:42")
        self.assertEqual(collection["features"][0]["geometry"]["type"], "LineString")
        self.assertEqual(collection["features"][0]["geometry"]["coordinates"],
                         [[13.4, 52.5], [13.401, 52.5], [13.401, 52.501]])
        self.assertEqual(collection["features"][0]["properties"]["product"], "regional")
        self.assertNotIn("category", collection["features"][0]["properties"])
        alias = await main.tracks()
        self.assertEqual(alias.body, response.body)

    async def test_tracks_http_response_explains_empty_postgis_cache(self):
        transport = httpx.ASGITransport(app=main.app)
        with self.assertLogs("main", level="ERROR"):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/tracks")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": main.TRACKS_UNAVAILABLE_DETAIL})
        self.assertEqual(response.headers["retry-after"], "60")

    async def test_border_endpoint_serves_cached_national_and_state_lines(self):
        await main.load_borders()
        response = await main.borders()
        self.assertEqual(response.media_type, "application/geo+json")
        collection = json.loads(response.body)
        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual({f["properties"]["admin_level"] for f in collection["features"]}, {"2", "4"})
        self.assertTrue(all(f["geometry"]["type"] == "MultiLineString" for f in collection["features"]))

    async def test_trip_endpoint_returns_and_caches_full_linestring(self):
        feature = {"type": "Feature", "id": "trip/one", "properties": {"trip_id": "trip/one"},
                   "geometry": {"type": "LineString", "coordinates": [[7.0, 51.0], [8.0, 52.0]]}}
        provider = AsyncMock()
        provider.get_trip.return_value = feature
        with patch.object(main, "hafas", provider):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.get("/trip/trip%2Fone")
                second = await client.get("/trip/trip%2Fone")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), feature)
        self.assertEqual(second.content, first.content)
        provider.get_trip.assert_awaited_once_with("trip/one")

    async def test_station_loader_retries_and_endpoint_serves_cached_points(self):
        with self.assertRaises(main.HTTPException) as error:
            await main.stations()
        self.assertEqual(error.exception.status_code, 503)
        provider = AsyncMock()
        collection = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "id": "osm:node:123",
            "properties": {"station_id": "osm:node:123", "name": "S Hackescher Markt",
                           "is_important": False},
            "geometry": {"type": "Point", "coordinates": [13.402359, 52.522605]},
        }]}
        provider.get_station_feature_collection.side_effect = [ValueError("outage"), collection]
        with patch.object(main, "osm", provider), \
             patch.object(main.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            await main.load_stations()
        self.assertEqual(provider.get_station_feature_collection.await_count, 2)
        sleep.assert_awaited_once_with(60)
        response = await main.stations()
        collection = json.loads(response.body)
        self.assertEqual(collection["features"][0]["id"], "osm:node:123")
        self.assertEqual(collection["features"][0]["properties"]["station_id"], "osm:node:123")
        self.assertEqual((await main.stations()).body, response.body)
        self.assertEqual(provider.get_station_feature_collection.await_count, 2)

    async def test_departures_endpoint_validates_id_and_caches_ten_rows(self):
        provider = AsyncMock()
        provider.get_departures.return_value = [{"line_name": "M41", "product": "bus",
                                                  "delay_minutes": 2}]
        with patch.object(main, "hafas", provider):
            with self.assertRaises(main.HTTPException) as error:
                await main.station_departures("osm:node:bad")
            self.assertEqual(error.exception.status_code, 400)
            first = await main.station_departures("900100002")
            second = await main.station_departures("900100002")
        provider.get_departures.assert_awaited_once_with("900100002", limit=10)
        self.assertEqual(first.body, second.body)
        self.assertEqual(json.loads(first.body)[0]["delay_minutes"], 2)
        alias = await main.departures("900100002")
        self.assertEqual(alias.body, first.body)

    async def test_osm_station_click_resolves_vbb_id_once_and_reuses_departure_cache(self):
        station = {"type": "Feature", "id": "osm:node:123",
                   "properties": {"name": "S Hackescher Markt"},
                   "geometry": {"type": "Point", "coordinates": [13.402359, 52.522605]}}
        main.station_by_id = {station["id"]: station}
        resolver = AsyncMock()
        resolver.resolve_stop_id.return_value = "900100002"
        provider = AsyncMock()
        provider.get_departures.return_value = [{"line_name": "S9", "product": "suburban"}]
        with patch.object(main, "station_client", resolver), patch.object(main, "hafas", provider):
            first = await main.station_departures("osm:node:123")
            second = await main.station_departures("osm:node:123")
        self.assertEqual(first.body, second.body)
        resolver.resolve_stop_id.assert_awaited_once_with(52.522605, 13.402359, "S Hackescher Markt")
        provider.get_departures.assert_awaited_once_with("900100002", limit=10)

    async def test_routing_work_does_not_block_event_loop(self):
        started = threading.Event()
        release = threading.Event()
        broadcasted = asyncio.Event()
        provider = AsyncMock()
        provider.get_radar.return_value = {"movements": [movement()]}

        def slow_normalization(*args):
            started.set()
            release.wait(timeout=2)
            return {"type": "FeatureCollection", "features": []}

        async def capture_broadcast(_):
            broadcasted.set()

        with patch.object(main, "hafas", provider), \
             patch.object(main, "normalize_movements", slow_normalization), \
             patch.object(main, "broadcast", capture_broadcast):
            task = asyncio.create_task(main.polling_loop())
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(started.wait), 1))
                # This timer must run while normalization is waiting in its worker.
                await asyncio.wait_for(asyncio.sleep(0.01), 0.2)
                self.assertFalse(broadcasted.is_set())
                release.set()
                await asyncio.wait_for(broadcasted.wait(), 1)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def way(nodes, coordinates):
    return {"type": "way", "nodes": nodes,
            "geometry": [{"lon": lon, "lat": lat} for lon, lat in coordinates]}


class TrackRoutingTests(unittest.TestCase):
    def setUp(self):
        self.engine = InterpolationEngine()

    def assert_points_close(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for position, target in zip(actual, expected):
            self.assertAlmostEqual(position[0], target[0], places=7)
            self.assertAlmostEqual(position[1], target[1], places=7)

    def test_mock_traffic_uses_connected_nodes_of_matching_track_products(self):
        ways = []
        for index, tags in enumerate((
            {"railway": "subway"},
            {"railway": "light_rail"},
            {"railway": "rail", "usage": "branch"},
            {"railway": "rail", "usage": "main"},
        )):
            track = way([index * 3 + 1, index * 3 + 2, index * 3 + 3], [
                (13.35, 52.50 + index * 0.01),
                (13.36, 52.50 + index * 0.01),
                (13.37, 52.50 + index * 0.01),
            ])
            track["tags"] = tags
            ways.append(track)
        self.engine.load_osm_data({"elements": ways})
        graph = self.engine._graph.graph
        nodes_by_position = {tuple(details["coordinates"]): node
                             for node, details in graph.nodes(data=True)}
        first = self.engine.generate_mock_traffic(10)
        second = self.engine.generate_mock_traffic(25)
        third = self.engine.generate_mock_traffic(40)
        self.assertTrue(first["is_mock"])
        self.assertEqual(len(first["features"]), 20)
        self.assertEqual({feature["properties"]["product"] for feature in first["features"]},
                         {"nationalExpress", "suburban", "regional"})
        for initial, moved, continued in zip(first["features"], second["features"], third["features"]):
            self.assertEqual(initial["id"], moved["id"])
            self.assertTrue(initial["properties"]["is_mock"])
            self.assertEqual(initial["geometry"]["type"], "Point")
            self.assertIn(tuple(initial["geometry"]["coordinates"]), nodes_by_position)
            self.assertEqual(moved["geometry"]["type"], "LineString")
            start, end = moved["geometry"]["coordinates"]
            self.assertEqual(start, initial["geometry"]["coordinates"])
            self.assertEqual(continued["properties"]["start"], end)
            self.assertEqual(moved["properties"]["duration_ms"], 15_000)
            edge = graph[nodes_by_position[tuple(start)]][nodes_by_position[tuple(end)]]
            self.assertIn(moved["properties"]["product"], edge["products"])

    def test_mock_traffic_waits_for_overpass_graph(self):
        with self.assertRaises(RuntimeError):
            self.engine.generate_mock_traffic(10)

    def test_mock_traffic_runs_on_nationwide_mainline_only_graph(self):
        mainline = way([1, 2, 3], [(10, 50), (10.01, 50), (10.02, 50)])
        mainline["tags"] = {"railway": "rail", "usage": "main"}
        self.engine.load_osm_data({"elements": [mainline]})
        first = self.engine.generate_mock_traffic(10)
        second = self.engine.generate_mock_traffic(25)
        self.assertEqual(len(second["features"]), 20)
        self.assertEqual({f["properties"]["product"] for f in second["features"]},
                         {"nationalExpress"})
        self.assertTrue(all(f["geometry"]["type"] == "LineString" for f in second["features"]))

    def test_track_products_follow_overpass_tags(self):
        tags = [
            {"railway": "subway"},
            {"railway": "light_rail"},
            {"railway": "rail", "highspeed": "yes"},
            {"railway": "rail", "usage": "main"},
            {"railway": "rail", "usage": "branch"},
        ]
        elements = []
        for index, item in enumerate(tags):
            element = way([index * 2 + 1, index * 2 + 2],
                          [(13 + index * 0.01, 52), (13 + index * 0.01 + 0.001, 52)])
            element.update({"id": index + 1, "tags": item})
            elements.append(element)
        self.assertTrue(self.engine.load_osm_data({"elements": elements}))
        features = json.loads(self.engine.geometry_payload)["features"]
        self.assertEqual([feature["properties"]["product"] for feature in features],
                         ["subway", "suburban", "nationalExpress", "nationalExpress", "regional"])
        self.assertTrue(all("category" not in feature["properties"] for feature in features))
        self.assertEqual(features[2]["properties"]["highspeed"], "yes")
        self.assertEqual(features[3]["properties"]["usage"], "main")

    def test_subway_line_name_prefers_current_u_line_then_ref_or_name(self):
        referenced = way([1, 2], [(13, 52), (13.001, 52)])
        referenced["tags"] = {"railway": "subway", "ref": "U5", "name": "Alexanderplatz tunnel"}
        named = way([3, 4], [(13.002, 52), (13.003, 52)])
        named["tags"] = {"railway": "subway", "name": "U1"}
        historic = way([5, 6], [(13.004, 52), (13.005, 52)])
        historic["tags"] = {"railway": "subway", "ref": "C", "name": "U6"}
        self.engine.load_osm_data({"elements": [referenced, named, historic]})
        features = json.loads(self.engine.geometry_payload)["features"]
        self.assertEqual([f["properties"]["line_name"] for f in features], ["U5", "U1", "U6"])
        self.assertEqual(features[0]["properties"]["ref"], "U5")

    def test_vehicle_snaps_only_to_its_rail_mode(self):
        subway = way([1, 2], [(13, 52), (13.002, 52)])
        subway["tags"] = {"railway": "subway"}
        suburban = way([3, 4, 5], [(13, 52.0001), (13.001, 52.0001), (13.002, 52.0001)])
        suburban["tags"] = {"railway": "light_rail"}
        self.engine.load_osm_data({"elements": [subway, suburban]})
        self.engine.compute_trajectory("s", 13.0002, 52.00001, 10, "suburban")
        route = self.engine.compute_trajectory("s", 13.0018, 52.00001, 25, "suburban")
        self.assertEqual(route["route_status"], "routed")
        self.assertTrue(all(abs(point[1] - 52.0001) < 1e-7 for point in route["route"]))
        underground = self.engine.compute_trajectory("u", 13.0002, 52.00009, 10, "subway")
        self.assertAlmostEqual(underground["end_point"][1], 52)
        bus = self.engine.compute_trajectory("bus", 13.0002, 52.00001, 10, "bus")
        self.assertEqual(bus["end_point"], [13.0002, 52.00001])

    def test_mode_specific_shortest_path_avoids_other_railways(self):
        detour = way([1, 2, 3, 4], [(13, 52), (13, 52.001),
                                     (13.002, 52.001), (13.002, 52)])
        detour["tags"] = {"railway": "light_rail"}
        shortcut = way([1, 4], [(13, 52), (13.002, 52)])
        shortcut["tags"] = {"railway": "subway"}
        self.engine.load_osm_data({"elements": [detour, shortcut]})
        distance, nodes = self.engine._graph.shortest(("osm", 1), ("osm", 4), "light_rail")
        self.assertGreater(distance, 0)
        self.assertEqual(nodes, (("osm", 1), ("osm", 2), ("osm", 3), ("osm", 4)))

    def test_curved_way_includes_intermediate_nodes_and_projected_endpoints(self):
        self.engine.load_osm_data({"elements": [way(
            [1, 2, 3, 4],
            [(13, 52), (13.001, 52), (13.001, 52.001), (13.002, 52.001)]
        )]})
        first = self.engine.compute_trajectory("s1", 13.00025, 52.00002, 10)
        self.assertEqual(first["route_status"], "initial")
        self.assertIsNone(first["route"])
        second = self.engine.compute_trajectory("s1", 13.00175, 52.00098, 25)
        self.assertEqual(second["route_status"], "routed")
        self.assertEqual(second["duration_ms"], 15000)
        self.assert_points_close(second["route"], [
            (13.00025, 52), (13.001, 52), (13.001, 52.001),
            (13.00175, 52.001)
        ])
        self.assert_points_close([second["start_point"], second["end_point"]],
                                 [second["route"][0], second["route"][-1]])

    def test_shortest_subsegment_does_not_detour_via_endpoint(self):
        self.engine.load_osm_data({"elements": [way(
            [1, 2], [(13, 52), (13.002, 52)]
        )]})
        self.engine.compute_trajectory("s1", 13.0002, 52, 10)
        trajectory = self.engine.compute_trajectory("s1", 13.0008, 52, 25)
        self.assertEqual(trajectory["route_status"], "routed")
        self.assert_points_close(trajectory["route"],
                                 [(13.0002, 52), (13.0008, 52)])

    def test_shorter_connected_branch_wins_over_long_detour(self):
        self.engine.load_osm_data({"elements": [
            way([1, 2, 3], [(13, 52), (13.001, 52), (13.002, 52)]),
            way([2, 4, 5, 3], [(13.001, 52), (13.001, 52.003),
                               (13.002, 52.001), (13.002, 52)]),
        ]})
        self.engine.compute_trajectory("s1", 13.0005, 52, 10)
        trajectory = self.engine.compute_trajectory("s1", 13.002, 52.0008, 25)
        self.assertEqual(trajectory["route_status"], "routed")
        self.assert_points_close(trajectory["route"], [
            (13.0005, 52), (13.001, 52), (13.002, 52),
            (13.002, 52.0008)
        ])

    def test_shared_node_joins_ways_but_crossing_without_shared_id_does_not(self):
        self.engine.load_osm_data({"elements": [
            way([1, 2], [(13, 52), (13.001, 52)]),
            way([2, 3], [(13.001, 52), (13.001, 52.001)]),
        ]})
        self.engine.compute_trajectory("joined", 13.0002, 52, 10)
        joined = self.engine.compute_trajectory("joined", 13.001, 52.0008, 25)
        self.assert_points_close(joined["route"], [
            (13.0002, 52), (13.001, 52), (13.001, 52.0008)
        ])

        self.engine = InterpolationEngine()
        self.engine.load_osm_data({"elements": [
            way([1, 2, 3], [(13, 52.001), (13.001, 52.001), (13.002, 52.001)]),
            way([4, 5, 6], [(13.001, 52), (13.001, 52.001), (13.001, 52.002)]),
        ]})
        self.engine.compute_trajectory("crossing", 13.0002, 52.001, 10)
        crossing = self.engine.compute_trajectory("crossing", 13.001, 52.0002, 25)
        self.assertEqual(crossing["route_status"], "straight")
        self.assert_points_close(crossing["route"],
                                 [crossing["start_point"], crossing["end_point"]])

    def test_sparse_track_falls_back_to_straight_segment_and_stationary_point(self):
        first = self.engine.compute_trajectory("s1", 13, 52, 10)
        self.assertEqual(first["route_status"], "initial")
        second = self.engine.compute_trajectory("s1", 13.001, 52, 25)
        self.assertEqual(second["route_status"], "straight")
        self.assertEqual(second["route"], [[13, 52], [13.001, 52]])
        third = self.engine.compute_trajectory("s1", 13.001, 52, 40)
        self.assertEqual(third["route_status"], "stationary")

    def test_stream_feature_geometry_is_a_route_or_current_point(self):
        main.engine = self.engine
        suburban_way = way([1, 2, 3], [(13, 52), (13.001, 52), (13.001, 52.001)])
        suburban_way["tags"] = {"railway": "light_rail"}
        self.engine.load_osm_data({"elements": [suburban_way]})
        first = main.normalize_movements({"movements": [movement(
            location={"longitude": 13.0002, "latitude": 52})]}, 10)["features"][0]
        self.assertEqual(first["geometry"]["type"], "Point")
        self.assertEqual(first["properties"]["route_status"], "initial")
        second = main.normalize_movements({"movements": [movement(
            location={"longitude": 13.001, "latitude": 52.0008})]}, 25)["features"][0]
        self.assertEqual(second["geometry"]["type"], "LineString")
        self.assertEqual(second["properties"]["route_status"], "routed")
        self.assert_points_close(second["geometry"]["coordinates"], [
            (13.0002, 52), (13.001, 52), (13.001, 52.0008)
        ])
        self.assert_points_close([second["properties"]["start"], second["properties"]["end"]],
                                 [second["geometry"]["coordinates"][0],
                                  second["geometry"]["coordinates"][-1]])
        json.dumps(second, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
