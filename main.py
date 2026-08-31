from __future__ import annotations

import logging
from pathlib import Path

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
SCANS_DIR = BASE_DIR / "scans"
STATIC_DIR = BASE_DIR / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("pyvips").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = FastAPI(title="OME-TIFF Viewer")


def find_scan(scan_id: str) -> Path:
    """Resolve a scan by exact filename without allowing path traversal."""
    if Path(scan_id).name != scan_id or Path(scan_id).suffix.lower() not in {".tif", ".tiff"}:
        raise HTTPException(status_code=404, detail="Scan not found")

    scan_path = SCANS_DIR / scan_id
    if not scan_path.is_file():
        raise HTTPException(status_code=404, detail="Scan not found")
    return scan_path


@app.get("/api/scans")
def list_scans() -> list[dict[str, str]]:
    scans = sorted(
        (
            path
            for path in SCANS_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}
        ),
        key=lambda path: path.name.lower(),
    )
    return [{"id": path.name, "name": path.name} for path in scans]


@app.get("/api/scans/{scan_id}/info")
def scan_info(scan_id: str) -> dict[str, object]:
    scan_path = find_scan(scan_id)
    try:
        info = get_scan_info(scan_path)
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
    scan_path = find_scan(scan_id)
    logger.info("Tile request: scan=%s level=%s x=%s y=%s", scan_id, level, x, y)
    try:
        jpeg = get_tile(scan_path, level, x, y)
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except InvalidTileRequest as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# Mount last so /api routes take precedence. html=True serves static/index.html at /.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
