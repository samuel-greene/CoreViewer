import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import numpy as np
import tifffile

from catalog import Catalog, CatalogUnavailable, curate, get_asset, search, verify
from scripts.catalog_events import index_generated_image, process_message


class CatalogEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "images"
        self.root.mkdir()
        self.image = self.root / "scan.ome.tif"
        tifffile.imwrite(self.image, np.zeros((12, 16), dtype=np.uint8), metadata=None)
        self.catalog = Catalog(f"sqlite:///{self.temp.name}/catalog.db")
        self.addCleanup(self.catalog.engine.dispose)
        self.catalog.init_schema()
        self.event = {"id": "upload-1", "eventType": "CoreView.OmeGenerated", "dataVersion": "1",
                      "data": {"storageAccount": "examplestorage", "shareName": "ome-tiff-share",
                               "outputPath": self.image.name, "sizeBytes": self.image.stat().st_size}}
        self.env = patch.dict(os.environ, {"COREVIEW_STORAGE_ACCOUNT": "examplestorage"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def index(self, event=None):
        return index_generated_image(self.catalog, self.root, event or self.event)

    def test_immediate_index_and_duplicate_preserve_curated_verified_record(self):
        with patch("catalog._walk_images", side_effect=AssertionError("must not scan")):
            self.assertTrue(self.index()["changed"])
            asset = search(self.catalog)["items"][0]
            admin = self.catalog.account("admin@example.com", "admin")
            curate(self.catalog, asset["id"], admin, {"specimen_id": "corrected"}, ["reviewed"])
            verify(self.catalog, asset["id"], admin)
            self.assertFalse(self.index()["changed"])
        self.assertEqual(search(self.catalog)["total"], 1)
        current = get_asset(self.catalog, asset["id"])
        self.assertEqual(current["status"], "verified")
        self.assertEqual(current["specimen_id"], "corrected")

    def test_only_named_output_is_indexed(self):
        tifffile.imwrite(self.root / "other.tif", np.zeros((8, 8), dtype=np.uint8))
        self.index()
        self.assertEqual(search(self.catalog)["total"], 1)

    def test_invalid_paths_storage_and_versions_are_rejected(self):
        for key, value in [("outputPath", "../escape.tif"), ("outputPath", "/absolute.tif"),
                           ("outputPath", ".coreview-ome-work/file.tif"), ("shareName", "incoming-scans"),
                           ("storageAccount", "untrusted"), ("sizeBytes", True)]:
            with self.subTest(key=key, value=value):
                event = copy.deepcopy(self.event)
                event["data"][key] = value
                with self.assertRaises(ValueError):
                    self.index(event)
        event = dict(self.event, dataVersion="2")
        with self.assertRaises(ValueError):
            self.index(event)
        self.image.rename(self.root / "real.tif")
        self.image.symlink_to(self.root / "real.tif")
        with self.assertRaises(ValueError):
            self.index()

    def test_storage_account_must_be_explicitly_configured(self):
        for value in ("", "   "):
            with self.subTest(value=value), patch.dict(os.environ, {"COREVIEW_STORAGE_ACCOUNT": value}):
                with self.assertRaisesRegex(ValueError, "configured explicitly"):
                    self.index()

    def test_unavailable_output_or_bad_metadata_retries_without_partial_record(self):
        with patch("catalog._extract_metadata", return_value=({}, None, None, "unreadable")):
            with self.assertRaises(CatalogUnavailable):
                self.index()
        self.assertEqual(search(self.catalog)["total"], 0)
        with self.image.open("ab") as stream:
            stream.write(b"x")
        with self.assertRaises(OSError):
            self.index()
        self.image.unlink()
        with self.assertRaises(FileNotFoundError):
            self.index()

    def test_sql_outage_recovers_before_acknowledging_same_delivery(self):
        receiver, wait = Mock(), Mock()
        message = SimpleNamespace(body=[json.dumps(self.event).encode()])
        attempts = 0

        def index(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise CatalogUnavailable("temporary outage")
            return index_generated_image(*args)

        with patch("scripts.catalog_events.index_generated_image", side_effect=index):
            process_message(receiver, message, self.catalog, self.root, wait=wait)
        self.assertEqual(search(self.catalog)["total"], 1)
        wait.assert_called_once_with(5)
        receiver.complete_message.assert_called_once_with(message)
        receiver.abandon_message.assert_not_called()

    def test_prolonged_outage_backs_off_before_returning_delivery(self):
        receiver, wait = Mock(), Mock()
        message = SimpleNamespace(body=[json.dumps(self.event).encode()])
        with patch("scripts.catalog_events.index_generated_image", side_effect=CatalogUnavailable("offline")):
            process_message(receiver, message, self.catalog, self.root, wait=wait)
        self.assertEqual(wait.call_args_list, [call(5), call(10), call(20), call(40), call(60)])
        receiver.abandon_message.assert_called_once_with(message)
        receiver.complete_message.assert_not_called()

    def test_malformed_notification_is_dead_lettered_without_retry(self):
        receiver, wait = Mock(), Mock()
        message = SimpleNamespace(body=[b"invalid json"])
        process_message(receiver, message, self.catalog, self.root, wait=wait)
        receiver.dead_letter_message.assert_called_once()
        receiver.complete_message.assert_not_called()
        wait.assert_not_called()
