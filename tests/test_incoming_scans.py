import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import main


class IncomingScansTests(unittest.TestCase):
    def test_incoming_share_missing_returns_service_unavailable(self) -> None:
        with patch.object(main, "INCOMING_SCANS_DIR", Path("/not-mounted/incoming-scans")):
            with self.assertRaises(HTTPException) as raised:
                main._scan_root("incoming")

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("not mounted", raised.exception.detail)

    def test_incoming_listing_finds_nested_tiffs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            folder = root / "batch-1"
            folder.mkdir()
            (folder / "scan.tif").touch()
            (folder / "notes.json").touch()

            with patch.object(main, "INCOMING_SCANS_DIR", root):
                listing = main.list_scans(prefix="batch-1", source="incoming", user={})

        self.assertEqual([item["name"] for item in listing["files"]], ["scan.tif"])
        self.assertEqual(listing["parent_prefix"], "")

    def test_incoming_listing_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(HTTPException) as raised:
                main._list_scans_in_directory(Path(temp_dir), "../outside")

        self.assertEqual(raised.exception.status_code, 403)

    def test_incoming_image_path_is_resolved_under_incoming_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scan_path = root / "scan.tif"
            scan_path.touch()

            with patch.object(main, "INCOMING_SCANS_DIR", root):
                resolved = main.get_local_scan_path("scan.tif", {}, "incoming")
                with self.assertRaises(HTTPException) as raised:
                    main.get_local_scan_path("../outside.tif", {}, "incoming")

        self.assertEqual(resolved, scan_path.resolve())
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
