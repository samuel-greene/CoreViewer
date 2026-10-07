import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx

import main


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.scan = self.root / "Scan001"
        self.scan.mkdir()
        (self.scan / "scan.tif").touch()
        (self.scan / "plan.json").write_text("{}")
        (self.scan / "ome-manifest.json").write_text("{}")
        (self.root / "Incomplete").mkdir()
        self.mount = patch.object(main, "INCOMING_SCANS_DIR", self.root)
        self.mount.start()
        main.app.dependency_overrides[main.get_current_user] = lambda: {
            "email": "operator@example.com", "role": "admin"}
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        self.mount.stop()
        self.temp.cleanup()

    def test_incoming_returns_folders_and_readiness_without_image_links(self):
        response = self.client.get("/api/incoming")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotIn("files", data)
        folders = {f["name"]: f["selectable"] for f in data["directories"]}
        self.assertEqual(folders, {"Incomplete": False, "Scan001": True})
        self.assertTrue(data["can_submit"])
        current = self.client.get("/api/incoming?prefix=Scan001").json()["current_folder"]
        self.assertTrue(current["selectable"])
        self.assertEqual(current["path"], "Scan001")

    def test_authenticated_submission_forwards_only_folder_names_and_identity(self):
        proxy = AsyncMock(return_value={"jobs": [{"folder": "Scan001", "status": "queued"}]})
        with patch.object(main, "_generator_request", proxy):
            response = self.client.post("/api/processing", json={"folders": ["Scan001", "Scan001"]})
        self.assertEqual(response.status_code, 202)
        proxy.assert_awaited_once_with("POST", {"folders": ["Scan001"],
                                              "requested_by": "operator@example.com"})

    def test_guests_and_viewers_cannot_submit_or_read_job_records(self):
        for role in ("guest", "viewer"):
            main.app.dependency_overrides[main.get_current_user] = lambda role=role: {
                "email": "guest", "role": role}
            with patch.object(main, "_generator_request", AsyncMock()) as proxy:
                self.assertEqual(self.client.get("/api/processing").status_code, 403)
                self.assertEqual(self.client.post("/api/processing",
                                 json={"folders": ["Scan001"]}).status_code, 403)
                proxy.assert_not_called()
            self.assertFalse(self.client.get("/api/incoming").json()["can_submit"])

    def test_invalid_incomplete_or_oversized_batches_never_reach_generator(self):
        for folders in ([], ["../outside"], ["/absolute"], ["Incomplete"], [""],
                        ["Scan001/../Scan001"], ["Scan001"] * 101):
            with patch.object(main, "_generator_request", AsyncMock()) as proxy:
                self.assertIn(self.client.post("/api/processing",
                              json={"folders": folders}).status_code, (403, 422))
                proxy.assert_not_called()

    def test_job_status_is_forwarded_and_unconfigured_service_is_explicit(self):
        with patch.object(main, "_generator_request", AsyncMock(return_value={"jobs": []})):
            self.assertEqual(self.client.get("/api/processing").json(), {"jobs": []})
        with patch.dict(os.environ, {"OME_GENERATOR_URL": "", "OME_GENERATOR_API_KEY": ""}):
            result = self.client.get("/api/processing")
        self.assertEqual(result.status_code, 503)
        self.assertIn("not configured", result.json()["detail"])

    def test_missing_mount_and_path_traversal_are_reported(self):
        self.assertEqual(self.client.get("/api/incoming?prefix=../outside").status_code, 403)
        with patch.object(main, "INCOMING_SCANS_DIR", self.root / "missing"):
            self.assertEqual(self.client.get("/api/incoming").status_code, 503)

    def test_internal_jobs_and_staging_are_hidden_from_files(self):
        (self.root / ".coreview-ome-jobs").mkdir()
        (self.root / ".coreview-ome-work").mkdir()
        listing = main._list_scans_in_directory(self.root, "")
        self.assertEqual([item["name"] for item in listing["directories"]],
                         ["Incomplete", "Scan001"])

    def test_generator_proxy_uses_server_secret_and_maps_unavailable_errors(self):
        requests = []
        real_client = httpx.AsyncClient

        def handler(request):
            requests.append(request)
            return httpx.Response(202, json={"jobs": []})

        with patch.dict(os.environ, {"OME_GENERATOR_URL": "http://generator",
                                   "OME_GENERATOR_API_KEY": "k" * 32}), patch.object(
                main.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(
                    transport=httpx.MockTransport(handler), **kwargs)):
            result = self.client.post("/api/processing", json={"folders": ["Scan001"]})
        self.assertEqual(result.status_code, 202)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer " + "k" * 32)
        self.assertEqual(json.loads(requests[0].content)["folders"], ["Scan001"])
        self.assertNotIn("k" * 32, result.text)

        for code, expected in [(401, 503), (500, 503), (422, 422)]:
            with self.subTest(code=code), patch.dict(os.environ, {
                    "OME_GENERATOR_URL": "http://generator", "OME_GENERATOR_API_KEY": "k" * 32
                }), patch.object(main.httpx, "AsyncClient",
                    side_effect=lambda **kwargs: real_client(
                        transport=httpx.MockTransport(lambda request: httpx.Response(
                            code, json={"detail": "test failure"})), **kwargs)):
                self.assertEqual(self.client.get("/api/processing").status_code, expected)


if __name__ == "__main__":
    unittest.main()
