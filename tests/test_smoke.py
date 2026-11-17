"""Basic API behavior checks. These do not verify live video processing."""
import os
import unittest
from fastapi.testclient import TestClient

from backend.app import app

class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_response(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertIn("ffmpeg", body)

    def test_protected_endpoints_reject_missing_key(self):
        for endpoint in ("/api/uploads", "/api/streams", "/api/clips"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 401)

if __name__ == "__main__":
    unittest.main()
