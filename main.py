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


# from azure.core.exceptions import ResourceNotFoundError

def find_scan(scan_id: str):
    """Instantiate blob client instantly without making a blocking network call."""
    if not scan_id.lower().endswith((".tif", ".tiff")):
        raise HTTPException(status_code=404, detail="Scan not found")
    
    # Fast, local operation (0ms)
    return container_client.get_blob_client(scan_id)


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

import tempfile
from pathlib import Path

# Local disk directory for cached TIFF files
CACHE_DIR = Path(tempfile.gettempdir()) / "tiff_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def get_local_scan_path(scan_id: str) -> Path:
    """Retrieves TIFF from local SSD cache or downloads it if missing."""
    # Create a safe filename for flat storage
    safe_filename = scan_id.replace("/", "_")
    local_path = CACHE_DIR / safe_filename

    if not local_path.exists():
        logger.info("Downloading %s from Azure Blob Storage to local cache...", scan_id)
        blob_client = find_scan(scan_id)
        
        try:
            with open(local_path, "wb") as download_file:
                download_stream = blob_client.download_blob()
                download_file.write(download_stream.readall())
        except ResourceNotFoundError:
            if local_path.exists():
                local_path.unlink()
            raise HTTPException(status_code=404, detail="Scan not found")
        except Exception as e:
            if local_path.exists():
                local_path.unlink()
            logger.error("Failed to download blob %s: %s", scan_id, e)
            raise HTTPException(status_code=500, detail="Failed to retrieve file from storage")

    return local_path

from functools import lru_cache

# Reduce cache to hold roughly ~100-200MB of tiles instead of several GBs
@lru_cache(maxsize=512)
def render_tile_cached(scan_id: str, level: int, x: int, y: int) -> bytes:
    local_path = get_local_scan_path(scan_id)
    # Pass the local file path string to your tile service
    return get_tile(str(local_path), level, x, y)


@app.get("/api/scans/{scan_id}/info")
def scan_info(scan_id: str) -> dict[str, object]:
    local_path = get_local_scan_path(scan_id)
    try:
        info = get_scan_info(str(local_path))
        return info
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scans/{scan_id}/tiles/{level}/{x}/{y}.jpg")
def tile(scan_id: str, level: int, x: int, y: int) -> Response:
    try:
        jpeg = render_tile_cached(scan_id, level, x, y)
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={
                # Tell browsers to aggressively cache tiles locally for 24 hours
                "Cache-Control": "public, max-age=86400, immutable"
            },
        )
    except InvalidTileRequest as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")