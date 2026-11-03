import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import tifffile
from fastapi.testclient import TestClient

import main
from catalog import (Catalog, CatalogUnavailable, database_url, get_asset, scan_once, search, utcnow, verify,
                     curate, record_submissions, reconcile_jobs)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "images"
        self.root.mkdir()
        self.catalog = Catalog(f"sqlite:///{Path(self.temp.name) / 'catalog.db'}")
        self.addCleanup(self.catalog.engine.dispose)
        self.catalog.init_schema()
        self.folder = self.root / "Scan A"
        self.folder.mkdir()
        self.image = self.folder / "field 1.ome.tif"
        tifffile.imwrite(self.image, np.zeros((12, 16), dtype=np.uint8), metadata=None)
        self.plan = self.folder / "plan.json"
        self.plan.write_text(json.dumps({"specimenId": "Spec-1", "planId": "Plan-1",
                                         "acquiredUtc": "2024-01-02T03:04:00Z"}))

    def scan(self, now=None):
        return scan_once(self.catalog, self.root, now=now, stable_seconds=0)

    def test_azure_database_url_requires_verified_tls_and_ca_bundle(self):
        base = "postgresql+psycopg://catalog:secret@catalog.postgres.database.azure.com:5432/coreview"
        root = Path(self.temp.name) / "roots.pem"
        root.write_text("test CA placeholder")
        for suffix in ("", "?sslmode=require", "?sslmode=verify-full"):
            with self.subTest(suffix=suffix), patch.dict(os.environ, {"DATABASE_URL": base + suffix}):
                with self.assertRaises(CatalogUnavailable):
                    database_url()
        secure = f"{base}?sslmode=verify-full&sslrootcert={root}"
        with patch.dict(os.environ, {"DATABASE_URL": secure}):
            self.assertEqual(database_url(), secure)

    def test_index_search_curation_and_verification_invalidation(self):
        self.scan()
        result = search(self.catalog, query="spec-1")
        self.assertEqual(result["total"], 1)
        asset = result["items"][0]
        self.assertEqual(asset["width"], 16)
        self.assertEqual(asset["date_source"], "acquired")
        self.assertEqual(asset["status"], "unverified")
        self.assertEqual(search(
            self.catalog,
            date_from=datetime(2024, 1, 2, tzinfo=timezone.utc),
            date_to=datetime(2024, 1, 3, tzinfo=timezone.utc),
        )["total"], 1)
        self.assertEqual(search(self.catalog, query="%")["total"], 0)

        admin = self.catalog.account("Admin@Example.com", "admin")
        curated = curate(self.catalog, asset["id"], admin,
                         {"specimen_id": "Corrected"}, ["Cancer", "cancer", "  Tissue "])
        self.assertEqual(curated["tags"], ["cancer", "Tissue"])
        self.assertEqual(search(self.catalog, query="corrected")["total"], 1)
        verify(self.catalog, asset["id"], admin)
        self.assertEqual(get_asset(self.catalog, asset["id"])["status"], "verified")

        self.plan.write_text(json.dumps({"specimenId": "Spec-2", "planId": "Plan-2",
                                         "acquiredUtc": "2024-01-03T03:04:00Z"}))
        self.scan()
        detail = get_asset(self.catalog, asset["id"])
        self.assertEqual(detail["status"], "needs_review")
        self.assertEqual(detail["specimen_id"], "Corrected")
        self.assertEqual(detail["extracted"]["specimen_id"], "Spec-2")
        self.assertEqual(detail["plan_id"], "Plan-2")
        self.assertTrue(any(event["action"] == "source_changed" for event in detail["audit"]))
        restored = curate(self.catalog, asset["id"], admin, {}, reset_fields=["specimen_id"])
        self.assertEqual(restored["specimen_id"], "Spec-2")

    def test_searches_ome_and_companion_metadata_keywords(self):
        ome = ('<OME xmlns="urn:ome"><Image Name="Field A">'
               '<Description>Modality: Brightfield; Objective: 20x</Description>'
               '<Pixels><Channel Name="RGB"/></Pixels></Image>'
               '<StructuredAnnotations><MapAnnotation><Value>'
               '<M K="model_name">stain-net-v2</M>'
               '</Value></MapAnnotation></StructuredAnnotations></OME>')
        tifffile.imwrite(self.image, np.zeros((12, 16), dtype=np.uint8),
                         description=ome, metadata=None)
        self.plan.write_text(json.dumps({"specimenId": "Spec-1", "planId": "Plan-1",
                                         "inference": {"model_name": "companion-net-v4"},
                                         "tiles": [{"modality": "array-only-decoy"}]}))
        self.scan()

        for keyword in ("brightfield", "20X", "stain-net-v2", "model_name",
                        "companion-net-v4", "RGB"):
            with self.subTest(keyword=keyword):
                self.assertEqual(search(self.catalog, query=keyword)["total"], 1)
        self.assertEqual(search(self.catalog, query="brightfield stain-net-v2")["total"], 1)
        self.assertEqual(search(self.catalog, query="array-only-decoy")["total"], 0)

        self.plan.write_text(json.dumps({"specimenId": "Spec-1", "planId": "Plan-1",
                                         "modality": "Fluorescence", "objective": "40x"}))
        tifffile.imwrite(self.image, np.zeros((12, 16), dtype=np.uint8), metadata=None)
        self.scan()
        self.assertEqual(search(self.catalog, query="brightfield")["total"], 0)
        self.assertEqual(search(self.catalog, query="stain-net-v2")["total"], 0)
        self.assertEqual(search(self.catalog, query="companion-net-v4")["total"], 0)
        self.assertEqual(search(self.catalog, query="fluorescence 40x")["total"], 1)

    def test_existing_record_refreshes_metadata_search_without_file_change(self):
        self.plan.write_text(json.dumps({"specimenId": "Spec-1", "planId": "Plan-1",
                                         "modality": "Brightfield"}))
        self.scan()
        asset_id = search(self.catalog)["items"][0]["id"]
        verify(self.catalog, asset_id, self.catalog.account("admin@example.com", "admin"))
        with self.catalog.sessions.begin() as db:
            from catalog import Asset
            asset = db.query(Asset).one()
            asset.extracted = {key: value for key, value in asset.extracted.items()
                               if key != "search_metadata"}
            asset.search_text = "field 1.ome.tif spec-1 plan-1"
        self.assertEqual(search(self.catalog, query="brightfield")["total"], 0)
        self.scan()
        self.assertEqual(search(self.catalog, query="brightfield")["total"], 1)
        self.assertEqual(get_asset(self.catalog, asset_id)["status"], "verified")

    def test_searches_manifest_and_tiff_description(self):
        tifffile.imwrite(self.image, np.zeros((12, 16), dtype=np.uint8),
                         description="Modality: Brightfield", metadata=None)
        (self.folder / "ome-manifest.json").write_text(json.dumps({
            "objective": "20x", "inference": {"model_name": "model-from-manifest"},
            "tiles": [{"model_name": "tile-decoy"}],
        }))
        self.scan()
        for keyword in ("brightfield", "20x", "model-from-manifest"):
            with self.subTest(keyword=keyword):
                self.assertEqual(search(self.catalog, query=keyword)["total"], 1)
        self.assertEqual(search(self.catalog, query="tile-decoy")["total"], 0)

    def test_ome_nominal_magnification_matches_20x(self):
        ome = ('<OME xmlns="urn:ome"><Instrument>'
               '<Objective NominalMagnification="20.0"/></Instrument>'
               '<Image Name="Field A"/></OME>')
        tifffile.imwrite(self.image, np.zeros((12, 16), dtype=np.uint8),
                         description=ome, metadata=None)
        self.scan()
        self.assertEqual(search(self.catalog, query="20x")["total"], 1)

    def test_rename_missing_recovery_and_duplicate_copy(self):
        start = utcnow()
        self.scan(start)
        original = search(self.catalog)["items"][0]
        renamed = self.folder / "renamed.tif"
        self.image.rename(renamed)
        self.scan(start + timedelta(minutes=5))
        moved = search(self.catalog)["items"][0]
        self.assertEqual(moved["id"], original["id"])
        self.assertEqual(moved["path"], "Scan A/renamed.tif")

        duplicate = self.folder / "copy.tif"
        shutil.copyfile(renamed, duplicate)
        self.scan(start + timedelta(minutes=10))
        self.assertEqual(search(self.catalog)["total"], 2)

        renamed.unlink()
        self.scan(start + timedelta(minutes=15))
        self.assertEqual(search(self.catalog)["total"], 2)
        self.scan(start + timedelta(hours=25))
        self.assertEqual(search(self.catalog)["total"], 1)
        self.assertEqual(get_asset(self.catalog, original["id"])["status"], "missing")

        shutil.copyfile(duplicate, renamed)
        self.scan(start + timedelta(hours=26))
        self.assertEqual(search(self.catalog)["total"], 2)
        self.assertNotEqual(search(self.catalog, query="renamed")["items"][0]["id"], original["id"])

    def test_unsigned_azure_files_inode_can_be_indexed_and_renamed(self):
        original_stat = Path.stat
        large_inode = (1 << 63) + 123
        renamed = self.folder / "renamed.tif"
        resolved_image = self.image.resolve()
        resolved_renamed = resolved_image.with_name("renamed.tif")
        seen_image_stats = []

        def azure_files_stat(path, *args, **kwargs):
            stat = original_stat(path, *args, **kwargs)
            if kwargs.get("follow_symlinks") is False or path not in {resolved_image, resolved_renamed}:
                return stat
            seen_image_stats.append(path)
            return SimpleNamespace(
                st_dev=stat.st_dev, st_ino=large_inode,
                st_size=stat.st_size, st_mtime=stat.st_mtime,
                st_mtime_ns=stat.st_mtime_ns,
            )

        with patch.object(Path, "stat", azure_files_stat):
            self.scan()
            original = search(self.catalog)["items"][0]
            self.image.rename(renamed)
            self.scan()
            moved = search(self.catalog)["items"][0]

        self.assertEqual(moved["id"], original["id"])
        self.assertEqual(moved["path"], "Scan A/renamed.tif")
        self.assertTrue(seen_image_stats)

    def test_api_shared_search_admin_curation_and_guest_restriction(self):
        self.scan()
        url = str(self.catalog.engine.url)
        with patch.dict(os.environ, {"DATABASE_URL": url, "CATALOG_DEFAULT": "true"}), \
             patch.object(main, "MOUNT_DIR", self.root):
            client = TestClient(main.app)
            self.assertEqual(client.get("/api/catalog/search").status_code, 401)
            self.assertEqual(client.post("/api/auth/guest").status_code, 200)
            guest_search = client.get("/api/catalog/search?q=spec-1")
            self.assertEqual(guest_search.status_code, 200)
            asset_id = guest_search.json()["items"][0]["id"]
            self.assertEqual(client.patch(f"/api/catalog/admin/assets/{asset_id}", json={"tags": ["Lab"]}).status_code, 403)
            client.post("/api/auth/logout")
            login = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "change-me!"})
            self.assertEqual(login.status_code, 200)
            edited = client.patch(f"/api/catalog/admin/assets/{asset_id}", json={"tags": ["Lab"]})
            self.assertEqual(edited.status_code, 200)
            self.assertEqual(client.get("/api/catalog/search?q=lab").json()["total"], 1)
            self.assertEqual(client.post(f"/api/catalog/admin/assets/{asset_id}/verify").status_code, 200)

    def test_job_provenance_links_only_an_exact_completed_output(self):
        submitter = self.catalog.account("processor@example.com", "processor")
        record_submissions(self.catalog, [{"id": "job-one", "folder": "incoming-one"}],
                           submitter, ["incoming-one"])
        self.scan()
        self.assertIsNone(search(self.catalog)["items"][0]["uploader_id"])
        reconcile_jobs(self.catalog, [{"id": "job-one", "status": "completed",
                                       "output_path": "Scan A/field 1.ome.tif"}])
        self.assertEqual(search(self.catalog)["items"][0]["uploader_id"], submitter)
        self.assertEqual(search(self.catalog, uploader=submitter)["total"], 1)
        self.assertEqual(search(self.catalog, uploader="unknown")["total"], 0)

    def test_uninitialized_catalog_reports_unavailable_not_empty(self):
        url = f"sqlite:///{Path(self.temp.name) / 'not-initialized.db'}"
        with patch.dict(os.environ, {"DATABASE_URL": url}):
            client = TestClient(main.app)
            client.post("/api/auth/guest")
            response = client.get("/api/catalog/search")
            self.assertEqual(response.status_code, 503)
            self.assertIn("unavailable", response.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
