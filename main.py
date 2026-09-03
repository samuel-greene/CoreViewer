from __future__ import annotations

import logging
import os
from pathlib import Path

from azure.storage.blob import BlobServiceClient
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from tile_service import (
    InvalidTileRequest,
    ScanReadError,
    get_scan_info,
    get_tile,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("pyvips").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Azure Blob Storage Configuration ---
AZURE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
AZURE_CONTAINER_NAME = os.environ.get("AZURE_CONTAINER_NAME", "scans")

if not AZURE_CONNECTION_STRING:
    raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING environment variable must be set.")

blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)

app = FastAPI(title="OME-TIFF Viewer (Azure Blob Backend)")


def find_scan(scan_id: str):
    """Resolve a scan in Azure Blob Storage."""
    if not scan_id.lower().endswith((".tif", ".tiff")):
        raise HTTPException(status_code=404, detail="Scan not found")

    blob_client = container_client.get_blob_client(scan_id)
    if not blob_client.exists():
        raise HTTPException(status_code=404, detail="Scan not found")
    return blob_client


@app.get("/api/scans")
def list_scans(prefix: str = "") -> dict[str, object]:
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    directories = []
    files = []

    try:
        # walk_blobs splits virtual Azure directories on '/'
        for item in container_client.walk_blobs(name_starts_with=prefix, delimiter="/"):
            if getattr(item, "prefix", None):
                dir_name = item.prefix[len(prefix):].rstrip("/")
                directories.append({"name": dir_name, "path": item.prefix})
            else:
                file_name = item.name[len(prefix):]
                if file_name and file_name.lower().endswith((".tif", ".tiff")):
                    files.append({"id": item.name, "name": file_name})
    except Exception as e:
        logger.error("Failed to list blobs: %s", e)
        raise HTTPException(status_code=500, detail="Could not retrieve scans from storage")

    parent_prefix = None
    if prefix:
        parts = prefix.rstrip("/").split("/")
        parent_prefix = "/".join(parts[:-1]) + "/" if len(parts) > 1 else ""

    return {
        "current_path": f"/{prefix}",
        "parent_prefix": parent_prefix,
        "directories": sorted(directories, key=lambda d: d["name"].lower()),
        "files": sorted(files, key=lambda f: f["name"].lower()),
    }


@app.get("/api/scans/{scan_id}/info")
def scan_info(scan_id: str) -> dict[str, object]:
    blob_client = find_scan(scan_id)
    try:
        info = get_scan_info(blob_client)
        logger.info(
            "Scan opened: %s (%sx%s, %s native pyramid levels)",
            scan_id,
            info["width"],
            info["height"],
            info["pyramid_levels"],
        )
        return info
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scans/{scan_id}/tiles/{level}/{x}/{y}.jpg")
def tile(scan_id: str, level: int, x: int, y: int) -> Response:
    blob_client = find_scan(scan_id)
    logger.info("Tile request: scan=%s level=%s x=%s y=%s", scan_id, level, x, y)
    try:
        jpeg = get_tile(blob_client, level, x, y)
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except InvalidTileRequest as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")