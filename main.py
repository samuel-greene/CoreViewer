from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

# --- SECURITY & AUTHENTICATION CONFIG ---
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
SESSION_COOKIE_NAME = "ome_session"
SESSION_EXPIRE_SECONDS = 86400 * 7  # 7 days

def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Hashes a password using PBKDF2 with SHA-256."""
    if salt is None:
        salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    return key.hex(), salt.hex()

def verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    salt = bytes.fromhex(stored_salt)
    key, _ = hash_password(password, salt)
    return hmac.compare_digest(key, stored_hash)

def create_session_token(email: str) -> str:
    timestamp = str(int(time.time()))
    data = f"{email}:{timestamp}"
    signature = hmac.new(SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}:{signature}"

def verify_session_token(token: str) -> Optional[str]:
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        email, timestamp_str, signature = parts
        
        # Check expiry
        if int(time.time()) - int(timestamp_str) > SESSION_EXPIRE_SECONDS:
            return None
            
        data = f"{email}:{timestamp_str}"
        expected_sig = hmac.new(SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(signature, expected_sig):
            return email
    except Exception:
        return None
    return None

# Initial seed credentials (Change or replace with DB in future iterations)
default_hash, default_salt = hash_password("admin123")

USERS_DB = {
    "admin@example.com": {
        "email": "admin@example.com",
        "hash": default_hash,
        "salt": default_salt,
        "role": "admin",
        "allowed_paths": ["*"],  # Prepared for future per-file access control
    }
}

def get_current_user(ome_session: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME)) -> dict:
    if not ome_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    email = verify_session_token(ome_session)
    if not email or email not in USERS_DB:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    return USERS_DB[email]


def _resolve_mount_dir() -> Path:
    override = os.environ.get("IMAGES_DIR")
    if override:
        return Path(override).resolve()

    azure_mount = Path("/mnt/images")
    if azure_mount.exists():
        return azure_mount.resolve()

    local_scans = BASE_DIR / "scans"
    local_scans.mkdir(parents=True, exist_ok=True)
    return local_scans.resolve()

MOUNT_DIR = _resolve_mount_dir()

app = FastAPI(title="OME-TIFF Viewer")

class LoginRequest(BaseModel):
    email: str
    password: str

# --- AUTH ENDPOINTS ---

@app.post("/api/auth/login")
def login(credentials: LoginRequest, response: Response):
    user = USERS_DB.get(credentials.email.lower().strip())
    if not user or not verify_password(credentials.password, user["hash"], user["salt"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    token = create_session_token(user["email"])
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_EXPIRE_SECONDS,
        secure=False,  # Set to True if serving over HTTPS
    )
    return {"email": user["email"], "role": user["role"]}

@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"message": "Logged out"}

@app.get("/api/auth/me")
def get_me(user: dict = Depends(get_current_user)):
    return {"email": user["email"], "role": user["role"]}


# --- PROTECTED SCAN ENDPOINTS ---

def get_local_scan_path(scan_id: str, user: dict) -> Path:
    file_path = (MOUNT_DIR / scan_id).resolve()
    if not str(file_path).startswith(str(MOUNT_DIR)):
        raise HTTPException(status_code=403, detail="Invalid path request")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Scan not found")
    
    # Future permission check logic:
    # if "*" not in user["allowed_paths"] and not any(scan_id.startswith(p) for p in user["allowed_paths"]):
    #     raise HTTPException(status_code=403, detail="Access denied to this file")
        
    return file_path


@app.get("/api/scans")
def list_scans(prefix: str = "", user: dict = Depends(get_current_user)) -> dict[str, object]:
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    directories, files = [], []
    target_dir = (MOUNT_DIR / prefix).resolve()
    
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
        logger.error("Failed to list files: %s", e)
        raise HTTPException(status_code=500, detail="Could not retrieve scans")

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
def render_tile_cached(scan_id: str, level: int, x: int, y: int, local_path_str: str) -> bytes:
    return get_tile(local_path_str, level, x, y)


@app.get("/api/scans/{scan_id:path}/info")
def scan_info(scan_id: str, user: dict = Depends(get_current_user)) -> dict[str, object]:
    local_path = get_local_scan_path(scan_id, user)
    try:
        return get_scan_info(str(local_path))
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scans/{scan_id:path}/tiles/{level}/{x}/{y}.jpg")
def tile(scan_id: str, level: int, x: int, y: int, user: dict = Depends(get_current_user)) -> Response:
    local_path = get_local_scan_path(scan_id, user)
    try:
        jpeg = render_tile_cached(scan_id, level, x, y, str(local_path))
        return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400, immutable"})
    except InvalidTileRequest as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scans/{scan_id:path}/download")
def download_scan(scan_id: str, user: dict = Depends(get_current_user)) -> FileResponse:
    local_path = get_local_scan_path(scan_id, user)
    return FileResponse(path=local_path, filename=local_path.name, media_type="image/tiff")


class MarkingStroke(BaseModel):
    points: list[tuple[float, float]]
    width: float | None = None

class CropRequest(BaseModel):
    x: int
    y: int
    w: int
    h: int
    format: str = "jpg"
    markings: list[MarkingStroke] = []


@app.post("/api/scans/{scan_id:path}/crop")
def download_crop(scan_id: str, body: CropRequest, user: dict = Depends(get_current_user)) -> Response:
    local_path = get_local_scan_path(scan_id, user)
    x, y, w, h, format = body.x, body.y, body.w, body.h, body.format
    try:
        markings = [{"points": stroke.points, "width": stroke.width} for stroke in body.markings]
        cropped_bytes = crop_scan_region(str(local_path), x, y, w, h, format, markings)

        fmt_normalized = format.lower().lstrip(".")
        media_types = {"png": "image/png", "tif": "image/tiff", "tiff": "image/tiff"}
        media_type = media_types.get(fmt_normalized, "image/jpeg")
        extension = "tiff" if fmt_normalized in ("tif", "tiff") else fmt_normalized

        filename = f"{local_path.stem}_crop_{x}_{y}_{w}_{h}.{extension}"
        return Response(
            content=cropped_bytes,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")