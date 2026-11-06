"""Consume finalized-image notifications; never walk or poll the image share."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from catalog import Catalog, CatalogUnavailable, _record_file, _safe_output_path, reconcile_jobs, utcnow

logger = logging.getLogger(__name__)


def index_generated_image(catalog: Catalog, root: Path, event: dict) -> dict:
    """Index exactly one finalized image, with replay-safe catalog semantics."""
    if (not isinstance(event, dict) or event.get("eventType") != "CoreView.OmeGenerated"
            or event.get("dataVersion") != "1" or not isinstance(event.get("id"), str)
            or not event["id"]):
        raise ValueError("Expected a CoreView.OmeGenerated version 1 event")
    data = event.get("data")
    if not isinstance(data, dict) or data.get("shareName") != "ome-tiff-share":
        raise ValueError("Expected the ome-tiff-share output share")
    expected_account = os.environ.get("COREVIEW_STORAGE_ACCOUNT", "").strip()
    if not expected_account:
        raise ValueError("COREVIEW_STORAGE_ACCOUNT must be configured explicitly")
    if data.get("storageAccount") != expected_account:
        raise ValueError("Unexpected storage account")
    relative = _safe_output_path(data.get("outputPath"))
    if not relative or any(part.startswith(".") for part in relative.split("/")):
        raise ValueError("Expected a share-relative output path")
    root = root.resolve()
    path = root / relative
    if path.suffix.lower() not in {".tif", ".tiff"}:
        raise ValueError("Output must be a TIFF")
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root):
        raise ValueError("Output paths must not contain symlinks")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Output escapes the image root")
    if not path.is_file():
        raise FileNotFoundError("Finalized output is not available on the mounted share")
    expected_size = data.get("sizeBytes")
    if type(expected_size) is not int or expected_size <= 0:
        raise ValueError("Expected a positive finalized output length")
    if path.stat().st_size != expected_size:
        raise OSError("Mounted output size does not yet match the completion event")
    catalog.check_schema()
    job = data.get("job")
    if isinstance(job, dict):
        if job.get("output_path") != relative or job.get("status") != "completed":
            raise ValueError("Job provenance must describe the completed output")
        reconcile_jobs(catalog, [job])
    with catalog.sessions.begin() as db:
        if catalog.engine.dialect.name == "postgresql":
            # Coordinate with explicit backfills and other receivers. This is
            # also what makes concurrent redelivery safe without new tables.
            from sqlalchemy import text
            db.execute(text("SELECT pg_advisory_xact_lock(82561534)"))
        changed, error = _record_file(db, root, path, None, utcnow(), stable_seconds=0)
        if error:
            raise CatalogUnavailable("Output metadata could not be indexed; notification will be retried")
    return {"path": relative, "changed": changed}


def process_message(receiver, message, catalog: Catalog, root: Path, *, wait=time.sleep) -> None:
    """Retry temporary outages under the same lock before consuming a delivery."""
    for attempt in range(6):
        try:
            event = json.loads(b"".join(message.body))
            result = index_generated_image(catalog, root, event)
        except (ValueError, TypeError, UnicodeError):
            logger.exception("Invalid completion event; dead-lettering")
            receiver.dead_letter_message(message, reason="InvalidOmeGeneratedEvent",
                                         error_description="Invalid version, share, path, or payload")
            return
        except Exception:
            if attempt == 5:
                logger.exception("Catalog update still unavailable; returning notification for redelivery")
                receiver.abandon_message(message)
                return
            delay = min(5 * 2 ** attempt, 60)
            logger.exception("Catalog update unavailable; retrying the locked notification in %s seconds", delay)
            wait(delay)
        else:
            # Settlement failures propagate so a committed record is replayed
            # on a fresh connection rather than mistaken for an indexing error.
            receiver.complete_message(message)
            logger.info("Indexed completed output: %s", result)
            return


def run(root: Path) -> None:
    if not os.environ.get("COREVIEW_STORAGE_ACCOUNT", "").strip():
        raise ValueError("COREVIEW_STORAGE_ACCOUNT must be configured explicitly")
    from azure.servicebus import AutoLockRenewer, ServiceBusClient
    from catalog import get_catalog

    connection = os.environ.get("CATALOG_SERVICE_BUS_CONNECTION_STRING")
    queue = os.environ["CATALOG_SERVICE_BUS_QUEUE"]
    if connection:
        client = ServiceBusClient.from_connection_string(connection)
        credential = None
    else:
        from azure.identity import DefaultAzureCredential
        credential = DefaultAzureCredential()
        client = ServiceBusClient(os.environ["SERVICE_BUS_NAMESPACE"], credential)
    catalog = get_catalog()
    catalog.check_schema()
    try:
        with client, AutoLockRenewer(max_lock_renewal_duration=600) as renewer:
            with client.get_queue_receiver(queue, prefetch_count=0, auto_lock_renewer=renewer) as receiver:
                logger.info("Waiting for finalized-image events on %s", queue)
                while True:
                    for message in receiver.receive_messages(max_message_count=1, max_wait_time=30):
                        process_message(receiver, message, catalog, root)
    finally:
        if credential:
            credential.close()
