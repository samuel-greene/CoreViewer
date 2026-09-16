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
    crop_scan_region,
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


def _resolve_mount_dir() -> Path:
    """Picks the directory to serve scans from.

    - IMAGES_DIR env var always wins if set (explicit override).
    - Otherwise, use /mnt/images if it exists (Azure Container App mount).
    - Otherwise, fall back to a local "scans" folder next to this file,
      so the app can be run and tested locally without the Azure mount.
    """
    override = os.environ.get("IMAGES_DIR")
    if override:
        return Path(override).resolve()

    azure_mount = Path("/mnt/images")
    if azure_mount.exists():
        return azure_mount.resolve()

    local_scans = BASE_DIR / "scans"
    local_scans.mkdir(parents=True, exist_ok=True)
    logger.info(
        "/mnt/images not found; falling back to local scans directory at %s",
        local_scans,
    )
    return local_scans.resolve()


# Where scans are read from. See _resolve_mount_dir for override rules.
MOUNT_DIR = _resolve_mount_dir()

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
                    files.append({
                        "id": f"{prefix}{item_name}",
                        "name": item_name,
                        "size": item_path.stat().st_size,
                    })
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

@app.get("/api/scans/{scan_id:path}/crop")
def download_crop(
    scan_id: str,
    x: int,
    y: int,
    w: int,
    h: int,
    format: str = "jpg",
) -> Response:
    local_path = get_local_scan_path(scan_id)
    try:
        cropped_bytes = crop_scan_region(str(local_path), x, y, w, h, format)

        fmt_normalized = format.lower().lstrip(".")
        media_types = {
            "png": "image/png",
            "tif": "image/tiff",
            "tiff": "image/tiff",
        }
        media_type = media_types.get(fmt_normalized, "image/jpeg")
        extension = "tiff" if fmt_normalized in ("tif", "tiff") else fmt_normalized

        filename = f"{local_path.stem}_crop_{x}_{y}_{w}_{h}.{extension}"
        return Response(
            content=cropped_bytes,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to crop region for %s", scan_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# IMPORTANT: this mount must be the LAST route registered. Starlette matches
# routes in registration order, and a mount at "/" will swallow every path
# beneath it (including /api/... routes) if it's added before they are.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")