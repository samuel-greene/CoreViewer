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

    def test_inference_submission_and_retry_keep_operation_and_identity(self):
        payload = {"folders": ["Scan001"], "operation": "cyclegan", "retry_job_id": "a" * 64}
        with patch.object(main, "_generator_request", AsyncMock(return_value={"jobs": []})) as proxy:
            self.assertEqual(self.client.post("/api/processing", json=payload).status_code, 202)
            proxy.assert_awaited_once_with("POST", {**payload, "requested_by": "operator@example.com"})
        for invalid in ({"operation": "arbitrary-model"}, {"retry_job_id": "../model.pth"}):
            with patch.object(main, "_generator_request", AsyncMock()) as proxy:
                self.assertEqual(self.client.post("/api/processing", json={"folders": ["Scan001"], **invalid}).status_code, 422)
                proxy.assert_not_called()

    @patch.dict(os.environ, {"ALLOW_GUEST_PROCESSING": "false"})
    def test_guests_and_viewers_cannot_process_when_guest_access_disabled(self):
        for role in ("guest", "viewer"):
            main.app.dependency_overrides[main.get_current_user] = lambda role=role: {
                "email": "guest", "role": role}
            with patch.object(main, "_generator_request", AsyncMock()) as proxy:
                self.assertEqual(self.client.get("/api/processing").status_code, 403)
                self.assertEqual(self.client.post("/api/processing",
                                 json={"folders": ["Scan001"]}).status_code, 403)
                proxy.assert_not_called()
            self.assertFalse(self.client.get("/api/incoming").json()["can_submit"])

    def test_guest_session_can_submit_and_track_by_default(self):
        # Exercise a real guest cookie, including the capability used by the UI.
        main.app.dependency_overrides.clear()
        with patch.dict(os.environ, {}, clear=True), patch.object(
                main, "_generator_request", AsyncMock(return_value={"jobs": []})) as proxy:
            login = self.client.post("/api/auth/guest")
            self.assertTrue(login.json()["can_process"])
            self.assertTrue(self.client.get("/api/auth/me").json()["can_process"])
            self.assertTrue(self.client.get("/api/incoming").json()["can_submit"])
            self.assertEqual(self.client.get("/api/processing").status_code, 200)
            self.assertEqual(self.client.post("/api/processing",
                             json={"folders": ["Scan001"]}).status_code, 202)
            proxy.assert_awaited_with("POST", {"folders": ["Scan001"], "requested_by": "Guest"})
            with patch.dict(os.environ, {"ALLOW_GUEST_PROCESSING": "false"}):
                self.assertFalse(self.client.get("/api/auth/me").json()["can_process"])
                self.assertEqual(self.client.get("/api/processing").status_code, 403)
                self.assertEqual(self.client.post("/api/processing",
                                 json={"folders": ["Scan001"]}).status_code, 403)

    def test_guest_switch_does_not_grant_viewer_or_anonymous_access(self):
        with patch.dict(os.environ, {"ALLOW_GUEST_PROCESSING": "true"}):
            main.app.dependency_overrides[main.get_current_user] = lambda: {
                "email": "viewer", "role": "viewer"}
            self.assertFalse(self.client.get("/api/auth/me").json()["can_process"])
            self.assertEqual(self.client.get("/api/processing").status_code, 403)
            main.app.dependency_overrides.clear()
            self.assertEqual(self.client.get("/api/processing").status_code, 401)
            self.assertEqual(self.client.post("/api/processing",
                             json={"folders": ["Scan001"]}).status_code, 401)

    def test_guest_switch_keeps_admin_and_processor_access(self):
        with patch.dict(os.environ, {"ALLOW_GUEST_PROCESSING": "false"}):
            for role in ("admin", "processor"):
                self.assertTrue(main.can_process({"role": role}))

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
        with patch.dict(os.environ, {"OME_GENERATOR_URL": ""}):
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

    def test_generator_proxy_needs_only_url_and_maps_unavailable_errors(self):
        requests = []
        real_client = httpx.AsyncClient

        def handler(request):
            requests.append(request)
            return httpx.Response(202, json={"jobs": []})

        with patch.dict(os.environ, {"OME_GENERATOR_URL": "http://generator"}, clear=True), patch.object(
                main.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(
                    transport=httpx.MockTransport(handler), **kwargs)):
            result = self.client.post("/api/processing", json={"folders": ["Scan001"]})
        self.assertEqual(result.status_code, 202)
        self.assertNotIn("Authorization", requests[0].headers)
        self.assertEqual(json.loads(requests[0].content)["folders"], ["Scan001"])

        for code, expected in [(401, 503), (500, 503), (422, 422)]:
            with self.subTest(code=code), patch.dict(os.environ, {
                    "OME_GENERATOR_URL": "http://generator"
                }), patch.object(main.httpx, "AsyncClient",
                    side_effect=lambda **kwargs: real_client(
                        transport=httpx.MockTransport(lambda request: httpx.Response(
                            code, json={"detail": "test failure"})), **kwargs)):
                self.assertEqual(self.client.get("/api/processing").status_code, expected)


if __name__ == "__main__":
    unittest.main()
