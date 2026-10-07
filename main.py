from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

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

# Gap 1 fix: SECRET_KEY must be set as an environment variable in production.
# The fallback generates a fresh random key on every process start, which
# invalidates all active sessions after every container restart.
_secret_key_from_env = os.environ.get("SECRET_KEY", "")
if not _secret_key_from_env:
    _secret_key_from_env = secrets.token_hex(32)
    logger.warning(
        "SECRET_KEY env var is not set — using an ephemeral key. "
        "All sessions will be invalidated on every restart. "
        "Set SECRET_KEY in your Azure Container App environment variables."
    )
SECRET_KEY = _secret_key_from_env

SESSION_COOKIE_NAME = "ome_session"
SESSION_EXPIRE_SECONDS = 86400 * 7  # 7 days
GUEST_SESSION_SUBJECT = "guest"

# Gap 3 fix: read at startup so the same binary works locally (HTTP) and in
# Azure (HTTPS). Set HTTPS_ONLY=true in the Container App env vars.
HTTPS_ONLY = os.environ.get("HTTPS_ONLY", "false").lower() == "true"


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


# --- Gap 2 fix: environment-variable user store ---
#
# Two loading modes, checked in priority order:
#
# Mode A — USERS_JSON (preferred for multiple users)
#   Set to a JSON array where each element is a pre-hashed user object:
#   '[{"email":"viewer@example.com","hash":"<hex>","salt":"<hex>","role":"viewer","allowed_paths":["*"]}]'
#   Generate hash+salt pairs with:  python3 scripts/make_user.py
#
# Mode B — ADMIN_EMAIL + ADMIN_PASSWORD (simple single-user fallback)
#   The plaintext password is hashed on startup. Suitable for a single admin
#   account stored in Azure Key Vault as a secret.
#
# Neither mode stores credentials in source code.

def _load_users_from_env() -> dict:
    raw = os.environ.get("USERS_JSON", "").strip()
    if raw:
        try:
            users: list[dict] = json.loads(raw)
            if not isinstance(users, list):
                raise ValueError("USERS_JSON must be a JSON array")
            db = {u["email"].lower().strip(): u for u in users}
            logger.info("Loaded %d user(s) from USERS_JSON", len(db))
            return db
        except Exception as exc:
            # Fail hard — a silently empty user store is worse than a crash.
            raise RuntimeError(f"USERS_JSON env var is malformed: {exc}") from exc
    else:
        logger.info("Did not find raw USERS_JSON")

    # Mode B fallback
    email = os.environ.get("ADMIN_EMAIL", "admin@example.com")
    password = os.environ.get("ADMIN_PASSWORD", "change-me!")
    if password == "change-me!":
        logger.warning(
            "ADMIN_PASSWORD is using the insecure default value. "
            "Set ADMIN_PASSWORD (or USERS_JSON) in your environment variables."
        )
    h, s = hash_password(password)
    logger.info("Loaded 1 user from ADMIN_EMAIL/ADMIN_PASSWORD env vars (%s)", email)
    return {
        email.lower().strip(): {
            "email": email,
            "hash": h,
            "salt": s,
            "role": "admin",
            "allowed_paths": ["*"],
        }
    }


USERS_DB = _load_users_from_env()

def get_current_user(ome_session: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME)) -> dict:
    if not ome_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    email = verify_session_token(ome_session)
    if email == GUEST_SESSION_SUBJECT:
        return {"email": "Guest", "role": "guest", "allowed_paths": ["*"]}
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
INCOMING_SCANS_DIR = Path(
    os.environ.get("INCOMING_SCANS_DIR") or "/mnt/incoming-scans"
).resolve()

app = FastAPI(title="OME-TIFF Viewer")

class LoginRequest(BaseModel):
    email: str
    password: str

# --- AUTH ENDPOINTS ---

def _set_session_cookie(response: Response, subject: str) -> None:
    token = create_session_token(subject)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_EXPIRE_SECONDS,
        # Azure Container Apps terminates TLS at its ingress layer and proxies
        # plain HTTP internally, so secure=True is correct even though uvicorn
        # itself doesn't see HTTPS.
        secure=HTTPS_ONLY,
    )


@app.post("/api/auth/login")
def login(credentials: LoginRequest, response: Response):
    user = USERS_DB.get(credentials.email.lower().strip())
    if (
        not user
        or user["email"].lower().strip() == GUEST_SESSION_SUBJECT
        or not verify_password(credentials.password, user["hash"], user["salt"])
    ):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    _set_session_cookie(response, user["email"])
    return {"email": user["email"], "role": user["role"]}


@app.post("/api/auth/guest")
def guest_login(response: Response):
    _set_session_cookie(response, GUEST_SESSION_SUBJECT)
    return {"email": "Guest", "role": "guest"}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"message": "Logged out"}

@app.get("/api/auth/me")
def get_me(user: dict = Depends(get_current_user)):
    return {"email": user["email"], "role": user["role"]}


# --- PROTECTED SCAN ENDPOINTS ---

def _scan_root(source: Literal["scans", "incoming"]) -> Path:
    if source == "incoming":
        if not INCOMING_SCANS_DIR.is_dir():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Incoming scans share is not mounted. Mount the Azure Files "
                    "share at INCOMING_SCANS_DIR."
                ),
            )
        return INCOMING_SCANS_DIR
    return MOUNT_DIR


def _resolve_scan_path(root: Path, scan_id: str, invalid_detail: str) -> Path:
    file_path = (root / scan_id).resolve()
    try:
        file_path.relative_to(root.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail=invalid_detail) from None
    return file_path


def get_local_scan_path(
    scan_id: str,
    user: dict,
    source: Literal["scans", "incoming"] = "scans",
) -> Path:
    root = _scan_root(source)
    file_path = _resolve_scan_path(root, scan_id, "Invalid path request")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Scan not found")

    # Future permission check logic:
    # if "*" not in user["allowed_paths"] and not any(scan_id.startswith(p) for p in user["allowed_paths"]):
    #     raise HTTPException(status_code=403, detail="Access denied to this file")

    return file_path


def _list_scans_in_directory(root: Path, prefix: str) -> dict[str, object]:
    root = root.resolve()
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    directories, files = [], []
    target_dir = _resolve_scan_path(root, prefix, "Invalid directory request")
    if not target_dir.exists() or not target_dir.is_dir():
        return {
            "current_path": f"/{prefix}",
            "parent_prefix": None,
            "directories": [],
            "files": [],
        }

    try:
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
    except OSError as exc:
        logger.error("Failed to list files in %s: %s", target_dir, exc)
        raise HTTPException(status_code=500, detail="Could not retrieve scans") from exc

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


@app.get("/api/scans")
def list_scans(
    prefix: str = "",
    source: Literal["scans", "incoming"] = "scans",
    user: dict = Depends(get_current_user),
) -> dict[str, object]:
    return _list_scans_in_directory(_scan_root(source), prefix)


@lru_cache(maxsize=512)
def render_tile_cached(scan_id: str, level: int, x: int, y: int, local_path_str: str) -> bytes:
    return get_tile(local_path_str, level, x, y)


def _format_companion_metadata_value(key: str, value: object) -> str:
    labels = {
        "planId": "Plan ID",
        "specimenId": "Specimen ID",
        "pixelFormat": "Pixel format",
        "pixelSizeMicronsX": "Pixel size X",
        "pixelSizeMicronsY": "Pixel size Y",
        "zStepMicrons": "Z step",
        "zLayerCount": "Z layers",
        "exposureMicroseconds": "Exposure",
        "gainDb": "Gain",
        "acquiredUtc": "Acquired",
    }
    label = labels.get(key, re.sub(r"(?<!^)(?=[A-Z])", " ", key).title())
    if isinstance(value, float):
        formatted = f"{value:g}"
    elif isinstance(value, bool):
        formatted = "Yes" if value else "No"
    else:
        formatted = str(value)

    units = {
        "pixelSizeMicronsX": " µm",
        "pixelSizeMicronsY": " µm",
        "zStepMicrons": " µm",
        "exposureMicroseconds": " µs",
        "gainDb": " dB",
    }
    return f"{label}: {formatted}{units.get(key, '')}"


def _companion_metadata_summary(image_path: Path) -> str:
    sections = []
    for filename in ("plan.json", "ome-manifest.json"):
        metadata_path = image_path.parent / filename
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read companion metadata %s: %s", metadata_path, exc)
            sections.append(f"{filename}: metadata could not be read")
            continue

        if not isinstance(metadata, dict):
            logger.warning("Companion metadata %s must contain a JSON object", metadata_path)
            sections.append(f"{filename}: metadata must be a JSON object")
            continue

        fields = []
        for key, value in metadata.items():
            if key == "tiles":
                if isinstance(value, list):
                    fields.append(f"Tiles: {len(value)}")
            elif key == "cameraSettings" and isinstance(value, dict):
                fields.extend(
                    _format_companion_metadata_value(nested_key, nested_value)
                    for nested_key, nested_value in value.items()
                    if isinstance(nested_value, (str, int, float, bool))
                )
            elif isinstance(value, (str, int, float, bool)):
                fields.append(_format_companion_metadata_value(key, value))

        if fields:
            title = "Plan" if filename == "plan.json" else "OME manifest"
            sections.append(f"{title}:\n" + "\n".join(fields))

    return "\n".join(sections)


@app.get("/api/scans/{scan_id:path}/info")
def scan_info(
    scan_id: str,
    source: Literal["scans", "incoming"] = "scans",
    user: dict = Depends(get_current_user),
) -> dict[str, object]:
    local_path = get_local_scan_path(scan_id, user, source)
    try:
        info = get_scan_info(str(local_path))
        info["companion_metadata_summary"] = _companion_metadata_summary(local_path)
        return info
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scans/{scan_id:path}/tiles/{level}/{x}/{y}.jpg")
def tile(
    scan_id: str,
    level: int,
    x: int,
    y: int,
    source: Literal["scans", "incoming"] = "scans",
    user: dict = Depends(get_current_user),
) -> Response:
    local_path = get_local_scan_path(scan_id, user, source)
    try:
        jpeg = render_tile_cached(scan_id, level, x, y, str(local_path))
        return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400, immutable"})
    except InvalidTileRequest as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScanReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scans/{scan_id:path}/download")
def download_scan(
    scan_id: str,
    source: Literal["scans", "incoming"] = "scans",
    user: dict = Depends(get_current_user),
) -> FileResponse:
    local_path = get_local_scan_path(scan_id, user, source)
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
def download_crop(
    scan_id: str,
    body: CropRequest,
    source: Literal["scans", "incoming"] = "scans",
    user: dict = Depends(get_current_user),
) -> Response:
    local_path = get_local_scan_path(scan_id, user, source)
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