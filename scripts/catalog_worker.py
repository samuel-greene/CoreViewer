"""Explicit catalog setup, reconciliation, and rollout validation commands.

Run as ``python -m scripts.catalog_worker init|once|watch|validate`` with
DATABASE_URL and the same IMAGES_DIR mount used by the viewer.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from catalog import (Asset, CatalogUnavailable, _walk_images, create_postgres_search_index,
                     get_catalog, reconcile_jobs, scan_once, status_summary)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def image_root() -> Path:
    configured = os.environ.get("IMAGES_DIR")
    if configured:
        return Path(configured).resolve()
    mounted = Path("/mnt/images")
    return mounted.resolve() if mounted.exists() else (Path(__file__).resolve().parents[1] / "scans").resolve()


def validate(root: Path) -> dict:
    catalog = get_catalog()
    catalog.check_schema()
    if not root.is_dir():
        raise RuntimeError(f"Image share is not mounted: {root}")
    filesystem = {path.relative_to(root).as_posix() for path in _walk_images(root)}
    with catalog.sessions() as db:
        records = {asset.path: asset for asset in db.scalars(
            select(Asset).where(Asset.status != "missing")).all()}
    absent = sorted(path for path in records if path not in filesystem)
    unindexed = sorted(path for path in filesystem if path not in records)
    unreadable = sorted(asset.path for asset in records.values() if asset.index_error)
    return {
        "filesystem_images": len(filesystem), "catalog_records": len(records),
        "unindexed_count": len(unindexed), "absent_count": len(absent),
        "index_error_count": len(unreadable),
        "unindexed_sample": unindexed[:20], "absent_sample": absent[:20],
        "index_error_sample": unreadable[:20],
    }


def sync_generator_provenance() -> None:
    """Use the generator's existing status API; indexing never depends on it."""
    url = os.environ.get("OME_GENERATOR_URL", "").rstrip("/")
    if not url:
        return
    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.get(f"{url}/api/jobs")
            response.raise_for_status()
            jobs = response.json().get("jobs")
        if isinstance(jobs, list):
            reconcile_jobs(get_catalog(), jobs)
    except (httpx.HTTPError, ValueError, AttributeError, TypeError,
            CatalogUnavailable, SQLAlchemyError):
        logger.warning("Could not refresh generator provenance; catalog scan remains available")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "optimize", "once", "watch", "events", "validate", "status"))
    parser.add_argument("--interval", type=int, default=int(os.environ.get("CATALOG_SCAN_INTERVAL", "180")))
    args = parser.parse_args()
    catalog = get_catalog()
    root = image_root()
    if args.command == "init":
        catalog.init_schema()
        print("Catalog schema initialized")
    elif args.command == "optimize":
        create_postgres_search_index(catalog)
        print("Search index ready")
    elif args.command == "status":
        print(json.dumps(status_summary(catalog), indent=2, default=str))
    elif args.command == "validate":
        print(json.dumps(validate(root), indent=2))
    elif args.command == "once":
        result = scan_once(catalog, root)
        sync_generator_provenance()
        print(json.dumps(result, indent=2))
    elif args.command == "events":
        from scripts.catalog_events import run
        run(root)
    else:
        if args.interval < 60:
            parser.error("--interval must be at least 60 seconds")
        while True:
            try:
                logger.info("Catalog scan: %s", scan_once(catalog, root))
                sync_generator_provenance()
            except Exception:
                logger.exception("Catalog scan failed; retrying")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
