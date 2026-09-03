import unittest
from datetime import date

import httpx

from backend.main import Block, MaintenanceRequest, build_candidate_windows, calculate_ai_priority_score, generate_ai_plan_for_request, hmin, ranges_overlap, time_string, times_overlap
from backend.rail_traffic_provider import RailTrafficNotConfigured, RailTrafficProvider


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

        self.assertEqual(result["trains"][0]["no"], "22683")
        self.assertEqual(result["trains"][0]["name"], "Yesvantpur-Lucknow SF")
        self.assertTrue(result["stale"])
        self.assertEqual(result["status"], "stale")

    def test_rail_traffic_provider_requires_server_configuration(self):
        provider = RailTrafficProvider(base_url="", api_key="")

        with self.assertRaises(RailTrafficNotConfigured):
            provider.getLiveTrains()


if __name__ == "__main__":
    unittest.main()
