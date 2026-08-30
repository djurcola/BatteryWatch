"""Public HTTP contract tests for the fixture API."""

import unittest

from fastapi.testclient import TestClient

from batterywatch_api.main import app


class BatteryWatchApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health_and_generator_contract(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        response = self.client.get("/api/generators")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["generators"][0]["duid"], "BWTEST1")

    def test_series_calculates_signs_and_labels_estimates(self):
        response = self.client.get("/api/series", params={"generator": "BWTEST1"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["points"]), 12)
        self.assertAlmostEqual(body["points"][0]["energy_mwh"], 0.125)
        self.assertAlmostEqual(body["points"][1]["energy_mwh"], -5 / 60)
        self.assertEqual(body["points"][3]["price_status"], "negative")
        self.assertIsNone(body["points"][4]["price_aud_per_mwh"])
        self.assertEqual(body["points"][4]["price_status"], "missing")
        self.assertTrue(body["estimate"]["is_estimate"])
        self.assertIn("Estimate only", body["estimate"]["disclaimer"])

    def test_unknown_generator_and_invalid_ranges_fail_closed(self):
        self.assertEqual(self.client.get("/api/series?generator=NOPE").status_code, 404)
        self.assertEqual(
            self.client.get("/api/series", params={"generator": "BWTEST1", "start": "2026-01-01T01:00:00Z", "end": "2026-01-01T00:00:00Z"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/api/series", params={"generator": "BWTEST1", "start": "2026-01-01T00:00:00Z", "end": "2026-01-09T00:00:00Z"}).status_code,
            400,
        )


if __name__ == "__main__":
    unittest.main()
