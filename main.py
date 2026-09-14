from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles

from tile_service import (
    InvalidTileRequest,
    ScanReadError,
    get_scan_info,
    get_tile,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# This is where the Azure File Share is mounted in your Container App
MOUNT_DIR = Path("/mnt/images").resolve()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("pyvips").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = FastAPI(title="OME-TIFF Viewer (Local Mount Backend)")


def get_local_scan_path(scan_id: str) -> Path:
    """Validates the requested scan exists on the mounted volume."""
    # Resolve the absolute path to prevent directory traversal attacks
    file_path = (MOUNT_DIR / scan_id).resolve()
    
    # Ensure the requested path is actually inside our mount directory
    if not str(file_path).startswith(str(MOUNT_DIR)):
        raise HTTPException(status_code=403, detail="Invalid path request")
        
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Scan not found")
    
    return file_path


@app.get("/api/scans")
def list_scans(prefix: str = "") -> dict[str, object]:
    """Lists files and folders directly from the SMB mount."""
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    directories = []
    files = []
    
    target_dir = (MOUNT_DIR / prefix).resolve()
    
    # Ensure we aren't looking outside the mount directory
    if not str(target_dir).startswith(str(MOUNT_DIR)):
        raise HTTPException(status_code=403, detail="Invalid directory request")

    try:
        if target_dir.exists() and target_dir.is_dir():
            for item_name in os.listdir(target_dir):
                item_path = target_dir / item_name
                if item_path.is_dir():
                    directories.append({"name": item_name, "path": f"{prefix}{item_name}/"})
                elif item_path.is_file() and item_name.lower().endswith((".tif", ".tiff")):
                    files.append({"id": f"{prefix}{item_name}", "name": item_name})
    except Exception as e:
        logger.error("Failed to list files in mount: %s", e)
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


@lru_cache(maxsize=512)
def render_tile_cached(scan_id: str, level: int, x: int, y: int) -> bytes:
    """Passes the physical path directly to libvips for memory mapping."""
    local_path = get_local_scan_path(scan_id)
    return get_tile(str(local_path), level, x, y)


@app.get("/api/scans/{scan_id:path}/info")
def scan_info(scan_id: str) -> dict[str, object]:
    local_path = get_local_scan_path(scan_id)
    try:
        info = get_scan_info(str(local_path))
        return info
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scans/{scan_id:path}/tiles/{level}/{x}/{y}.jpg")
def tile(scan_id: str, level: int, x: int, y: int) -> Response:
    try:
        jpeg = render_tile_cached(scan_id, level, x, y)
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400, immutable"
            },
        )
    except InvalidTileRequest as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.get("/api/scans/{scan_id:path}/download")
def download_scan(scan_id: str) -> FileResponse:
    local_path = get_local_scan_path(scan_id)
    return FileResponse(
        path=local_path,
        filename=local_path.name,
        media_type="image/tiff"
    )

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")