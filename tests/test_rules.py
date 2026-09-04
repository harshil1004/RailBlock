import unittest
from datetime import date, datetime, timezone

import httpx

from backend.main import Block, MaintenanceRequest, build_candidate_windows, calculate_ai_priority_score, generate_ai_plan_for_request, hmin, ranges_overlap, time_string, times_overlap
from backend.rail_traffic_models import Corridor, TrainPosition, TrainSchedule, normalize_train_position
from backend.rail_traffic_provider import RailTrafficNotConfigured, RailTrafficProvider
from backend.live_rail_traffic_service import LiveRailTrafficService
from backend.rail_corridor_matching import PrototypeCorridorMapping, RailwayCorridorMatchingService
from backend.block_conflict_engine import BlockConflictAnalysisEngine, CandidateBlock, ExistingBlock, FreightForecast
from backend.ml_prediction_service import predictBlockConflictRisk, predictExpectedMaintenanceDuration, predictMaintenanceRisk


class PossessionRuleTests(unittest.TestCase):
    def test_km_ranges_overlap_only_when_intervals_intersect(self):
        self.assertTrue(ranges_overlap(121.4, 123.1, 122.0, 124.0))
        self.assertFalse(ranges_overlap(121.4, 123.1, 123.1, 124.0))

    def test_time_ranges_overlap_only_when_intervals_intersect(self):
        self.assertTrue(times_overlap(15, 135, 120, 180))
        self.assertFalse(times_overlap(15, 135, 135, 180))

    def test_hmin_converts_time_to_minutes(self):
        self.assertEqual(hmin("01:15"), 75)
        self.assertEqual(hmin("23:59"), 1439)

    def test_hmin_rejects_non_clock_times(self):
        with self.assertRaises(ValueError):
            hmin("24:00")
        with self.assertRaises(ValueError):
            hmin("9:00")

    def test_time_string_rejects_midnight_rollover(self):
        self.assertEqual(time_string(1439), "23:59")
        with self.assertRaises(ValueError):
            time_string(1440)

    def test_maintenance_request_canonical_model(self):
        row = {
            "request_id": "BR-1045",
            "dept": "TMS",
            "work": "Rail geometry correction",
            "from_station": "Penukonda",
            "to_station": "Dharmavaram",
            "request_day": "2026-09-02",
            "must_complete_by": "2026-09-05",
            "km_from": "121.4",
            "km_to": "123.1",
            "duration": "90",
            "priority": "High",
            "risk": "88",
            "status": "Approved",
            "hold_until": "2026-09-03T18:00:00",
            "asset_id": "TRK-145",
            "safety_clearance_required": "True",
            "preferred_start": "01:00",
            "preferred_end": "04:00",
            "reason": "Track defect detection",
            "requested_by": "TMS",
        }
        request = MaintenanceRequest.from_csv_row(row)
        self.assertEqual(request.id, "BR-1045")
        self.assertEqual(request.department, "TMS")
        self.assertEqual(request.activity, "Rail geometry correction")
        self.assertEqual(request.fromStation, "Penukonda")
        self.assertEqual(request.toStation, "Dharmavaram")
        self.assertEqual(request.aiPriorityScore, 88)
        self.assertEqual(request.aiPriorityLevel, "High")
        self.assertEqual(request.status, "Approved")

    def test_block_canonical_model(self):
        row = {
            "block_id": "BP-220",
            "start_time": "00:15",
            "end_time": "02:15",
            "block_day": "2026-09-03",
            "km_from": "121.4",
            "km_to": "123.1",
            "status": "Ongoing",
            "reason": "Lowest traffic window - 3 departments bundled",
            "affected": "1",
        }
        block = Block.from_csv_row(row, ["BR-1042", "BR-1043", "BR-1044"])
        self.assertEqual(block.id, "BP-220")
        self.assertEqual(block.corridor, "Bengaluru – Dharmavaram")
        self.assertEqual(block.fromKm, 121.4)
        self.assertEqual(block.toKm, 123.1)
        self.assertEqual(block.durationMinutes, 120)
        self.assertEqual(block.status, "Ongoing")
        self.assertEqual(block.maintenanceRequestIds, ["BR-1042", "BR-1043", "BR-1044"])

    def test_create_request_generates_ai_decision_support_data(self):
        request = {"request_id": "BR-TEST", "dept": "TMS", "work": "Track geometry correction", "from_station": "Penukonda", "to_station": "Dharmavaram", "request_day": "2026-09-02", "must_complete_by": "2026-09-05", "km_from": "121.4", "km_to": "123.1", "duration": "90", "priority": "High", "asset_type": "Track", "safety_clearance_required": "True", "preferred_start": "01:00", "preferred_end": "04:00"}
        blocks = [{"block_id": "BP-220", "start_time": "00:15", "end_time": "02:15", "block_day": "2026-09-02", "km_from": "121.4", "km_to": "123.1", "status": "Planned"}]
        plan = generate_ai_plan_for_request(request, [], blocks, [], None, None)

        self.assertGreater(calculate_ai_priority_score(request), 0)
        self.assertIn("AI decision support / optimization", plan["aiRecommendation"])
        self.assertEqual(plan["recommendedBlockId"], "BP-220")

    def test_candidate_windows_come_from_block_records(self):
        blocks = [
            {"block_id": "BP-220", "start_time": "00:15", "end_time": "02:15", "block_day": "2026-09-03", "km_from": "121.4", "km_to": "123.1", "status": "Approved"},
            {"block_id": "BP-221", "start_time": "04:10", "end_time": "05:10", "block_day": "2026-09-03", "km_from": "86.1", "km_to": "86.7", "status": "Planned"},
        ]
        candidates = build_candidate_windows(date(2026, 9, 3), blocks, [], [], [], [])

        self.assertEqual({candidate["blockId"] for candidate in candidates}, {"BP-220", "BP-221"})
        self.assertNotIn("00:15", {candidate["start"] for candidate in candidates if candidate["blockId"] != "BP-220"})

    def test_rail_traffic_provider_normalizes_feed_and_marks_stale_data(self):
        def handler(request):
            return httpx.Response(200, json={"data": [{"trainNumber": 22683, "trainName": "Yesvantpur-Lucknow SF", "scheduledTime": "00:03", "trainType": "Express"}], "updatedAt": "2020-01-01T00:00:00+00:00"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = RailTrafficProvider(base_url="https://provider.test", api_key="test-only", stale_after_seconds=60, client=client)
        result = provider.getLiveTrains()

        self.assertIsInstance(result["trains"][0], TrainPosition)
        self.assertEqual(result["trains"][0].trainNumber, "22683")
        self.assertEqual(result["trains"][0].trainName, "Yesvantpur-Lucknow SF")
        self.assertTrue(result["stale"])
        self.assertEqual(result["status"], "stale")

    def test_rail_traffic_provider_requires_server_configuration(self):
        provider = RailTrafficProvider(base_url="", api_key="")

        with self.assertRaises(RailTrafficNotConfigured):
            provider.getLiveTrains()

    def test_missing_live_traffic_fields_use_safe_defaults(self):
        train = normalize_train_position({"trainNumber": "22683"})

        self.assertIsNotNone(train)
        self.assertEqual(train.trainId, "22683")
        self.assertEqual(train.trainName, "Unknown train")
        self.assertIsNone(train.latitude)
        self.assertEqual(train.delayMinutes, 0)
        self.assertFalse(train.isLive)

    def test_schedule_and_corridor_models_are_normalized(self):
        def handler(request):
            if request.url.path.endswith("/schedules"):
                return httpx.Response(200, json={"data": [{"trainNo": "22683", "stationName": "Penukonda", "scheduledArrival": "00:03"}]})
            return httpx.Response(200, json={"data": [{"id": "C-1", "name": "Bengaluru-Dharmavaram", "fromStation": "Bengaluru", "toStation": "Dharmavaram", "available": True}]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = RailTrafficProvider(base_url="https://provider.test", api_key="test-only", client=client)

        schedules = provider.getSchedules()
        corridors = provider.getCorridors()

        self.assertIsInstance(schedules[0], TrainSchedule)
        self.assertEqual(schedules[0].trainNumber, "22683")
        self.assertIsInstance(corridors[0], Corridor)
        self.assertEqual(corridors[0].fromStation, "Bengaluru")

    def test_live_service_deduplicates_and_tracks_success_metrics(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        train = normalize_train_position({"trainId": "t-1", "trainNumber": "22683", "timestamp": timestamp})

        class Provider:
            def getLiveTrains(self):
                return {"trains": [train, train], "lastUpdated": timestamp, "status": "live"}

        snapshot = LiveRailTrafficService(Provider(), refresh_interval_seconds=60, freshness_threshold_seconds=3600).refresh()

        self.assertEqual(snapshot.numberOfTrainsReceived, 1)
        self.assertEqual(snapshot.trains[0].trainId, "t-1")
        self.assertIsNotNone(snapshot.lastSuccessfulUpdate)
        self.assertIsNotNone(snapshot.apiLatencyMs)
        self.assertIsNone(snapshot.apiError)

    def test_live_service_keeps_last_valid_cache_on_provider_error(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        train = normalize_train_position({"trainId": "t-1", "trainNumber": "22683", "timestamp": timestamp})

        class Provider:
            calls = 0

            def getLiveTrains(self):
                self.calls += 1
                if self.calls > 1:
                    raise RailTrafficNotConfigured("missing test configuration")
                return {"trains": [train], "lastUpdated": timestamp, "status": "live"}

        service = LiveRailTrafficService(Provider(), refresh_interval_seconds=60, freshness_threshold_seconds=3600)
        service.refresh()
        snapshot = service.refresh()

        self.assertEqual(snapshot.numberOfTrainsReceived, 1)
        self.assertEqual(snapshot.apiError, "missing test configuration")
        self.assertEqual(snapshot.status, "live")

    def test_corridor_matching_uses_section_mapping_and_relative_direction(self):
        corridor = Corridor("C-121", "Penukonda-Dharmavaram", "Penukonda", "Dharmavaram", 121.4, 123.1, "LINE-1", "Penukonda -> Dharmavaram", None, True)
        train = TrainPosition("t-1", "22683", "Test train", "Express", None, None, 60, "Penukonda -> Dharmavaram", "", "", None, None, 0, "LINE-1", "SEC-121", datetime.now(timezone.utc).isoformat(), "live-api", True, 0)
        service = RailwayCorridorMatchingService(PrototypeCorridorMapping({"SEC-121": (122.0, 122.4)}))

        result = service.match_train(train, corridor)

        self.assertEqual(result.state, "inside")
        self.assertEqual(result.direction, "forward")
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.distanceKm, 0.0)
        self.assertIsNotNone(result.estimatedExitTime)

    def test_corridor_matching_uses_station_evidence_without_fake_distance(self):
        corridor = Corridor("C-121", "Penukonda-Dharmavaram", "Penukonda", "Dharmavaram", 121.4, 123.1, "LINE-1", "", None, True)
        train = TrainPosition("t-2", "18064", "Test train", "Express", None, None, None, "", "Bengaluru", "Penukonda", None, None, 0, "LINE-1", "", None, "live-api", False, None)

        result = RailwayCorridorMatchingService().match_train(train, corridor)

        self.assertEqual(result.state, "approaching")
        self.assertEqual(result.confidence, "medium")
        self.assertIsNone(result.distanceKm)
        self.assertIsNone(result.estimatedEntryTime)

    def test_conflict_engine_classifies_passenger_as_hard_and_freight_as_warning(self):
        corridor = Corridor("C-121", "Penukonda-Dharmavaram", "Penukonda", "Dharmavaram", 121.4, 123.1, "LINE-1", "Penukonda -> Dharmavaram", None, True)
        candidate = CandidateBlock("BP-TEST", "2026-09-04", "01:00", "02:00", "C-121", 121.4, 123.1)
        passenger = TrainSchedule("t-passenger", "22683", "Express", "2026-09-04", "Penukonda", "01:10", "01:10", None, None, 4, "LINE-1", "", "Penukonda -> Dharmavaram")
        freight = FreightForecast("f-401", "F-401", "01:20", "01:50", "Penukonda -> Dharmavaram", "LINE-1", "", 0)

        result = BlockConflictAnalysisEngine().analyze(candidate, [], [passenger], [freight], corridor, [])

        self.assertEqual(result.numberOfPassengerConflicts, 1)
        self.assertEqual(result.passengerConflicts[0].severity, "HARD CONFLICT")
        self.assertEqual(result.numberOfFreightConflicts, 1)
        self.assertEqual(result.freightConflicts[0].severity, "WARNING")
        self.assertFalse(result.isFeasible)
        self.assertEqual(result.passengerConflictProbability, 1.0)
        self.assertEqual(result.freightConflictProbability, 1.0)
        self.assertIn("HARD CONFLICT", result.explanation)

    def test_conflict_engine_returns_no_conflict_and_does_not_fake_distance(self):
        corridor = Corridor("C-121", "Penukonda-Dharmavaram", "Penukonda", "Dharmavaram", 121.4, 123.1, "LINE-1", "", None, True)
        candidate = CandidateBlock("BP-TEST", "2026-09-04", "03:00", "04:00", "C-121")
        passenger = TrainSchedule("t-passenger", "22683", "Express", "2026-09-04", "Bengaluru", "01:10", "01:10", None, None, 0, "LINE-1", "", "")

        result = BlockConflictAnalysisEngine().analyze(candidate, [], [passenger], [], corridor, [])

        self.assertTrue(result.isFeasible)
        self.assertEqual(result.operationalRiskScore, 0)
        self.assertEqual(result.explanation, "NO CONFLICT: no meaningful passenger, freight, or possession overlap detected.")

    def test_ml_predictions_return_provenance_without_overriding_safety(self):
        corridor = Corridor("C-121", "Penukonda-Dharmavaram", "Penukonda", "Dharmavaram", 121.4, 123.1, "LINE-1", "", None, True)
        candidate = CandidateBlock("BP-TEST", "2026-09-04", "01:00", "02:00", "C-121")
        analysis = BlockConflictAnalysisEngine().analyze(candidate, [], [], [], corridor, [])

        risk = predictMaintenanceRisk({"priority": "High", "daysOverdue": 2, "assetCriticality": "High", "historicalFailureCount": 1})
        duration = predictExpectedMaintenanceDuration({"department": "TMS", "assetType": "Track", "historicalDuration": 90, "complexity": 2})
        conflict = predictBlockConflictRisk(candidate, corridor, [], [], [], [], analysis)

        for result in (risk, duration, conflict):
            self.assertIsNotNone(result.prediction)
            self.assertIsNotNone(result.probability)
            self.assertTrue(result.modelVersion.startswith("synthetic-prototype"))
            self.assertEqual(result.trainingData, "SYNTHETIC PROTOTYPE DATA")
            self.assertTrue(result.featuresUsed)
        self.assertTrue(analysis.isFeasible)


if __name__ == "__main__":
    unittest.main()
