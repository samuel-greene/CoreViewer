"""Searchable catalog for images in the published Azure Files share.

The share remains the source of image bytes. This module owns only indexed
metadata, curation, provenance, and reconciliation state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import tifffile
from sqlalchemy import (
    JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String,
    Text, create_engine, func, or_, select, text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
IMAGE_SUFFIXES = {".tif", ".tiff"}
EDITABLE_FIELDS = {"specimen_id", "plan_id", "acquired_at"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return as_utc(parsed)
    except ValueError:
        return None


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "catalog_schema_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class Account(Base):
    __tablename__ = "catalog_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Asset(Base):
    __tablename__ = "catalog_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    device: Mapped[int | None] = mapped_column(BigInteger)
    inode: Mapped[int | None] = mapped_column(BigInteger)
    sidecar_signature: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    extracted: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    overrides: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    specimen_id: Mapped[str | None] = mapped_column(Text)
    plan_id: Mapped[str | None] = mapped_column(Text)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    date_key: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    date_source: Mapped[str] = mapped_column(String(16), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    uploader_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("catalog_accounts.id"))
    owner_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("catalog_accounts.id"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="unverified")
    status_before_missing: Mapped[str | None] = mapped_column(String(24))
    verified_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("catalog_accounts.id"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seen_run_id: Mapped[str | None] = mapped_column(String(36))
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    index_error: Mapped[str | None] = mapped_column(Text)


Index("ix_catalog_assets_date", Asset.date_key.desc(), Asset.id)
Index("ix_catalog_assets_path", Asset.path)
Index("ix_catalog_assets_status", Asset.status)
Index("ix_catalog_assets_uploader", Asset.uploader_id)
Index("ix_catalog_assets_inode", Asset.device, Asset.inode)


class AuditEvent(Base):
    __tablename__ = "catalog_audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("catalog_assets.id"), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("catalog_accounts.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    before: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    after: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IndexRun(Base):
    __tablename__ = "catalog_index_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    changed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text)


class JobProvenance(Base):
    __tablename__ = "catalog_job_provenance"
    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    submitter_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("catalog_accounts.id"))
    folder: Mapped[str] = mapped_column(Text, nullable=False)
    output_path: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def database_url() -> str | None:
    value = os.environ.get("DATABASE_URL", "").strip()
    if value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value[len("postgresql://"):]
    if not value:
        return None
    parsed = make_url(value)
    if (parsed.host or "").lower().endswith(".postgres.database.azure.com"):
        if parsed.query.get("sslmode") != "verify-full":
            raise CatalogUnavailable("Azure PostgreSQL requires sslmode=verify-full")
        root = parsed.query.get("sslrootcert")
        if not isinstance(root, str) or not Path(root).is_file() or not os.access(root, os.R_OK):
            raise CatalogUnavailable("Azure PostgreSQL requires a readable sslrootcert CA bundle")
    return value


class CatalogUnavailable(RuntimeError):
    pass


class Catalog:
    def __init__(self, url: str):
        self.engine = create_engine(url, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.sessions.begin() as db:
            marker = db.get(SchemaVersion, 1)
            if marker is None:
                db.add(SchemaVersion(id=1, version=SCHEMA_VERSION))
            elif marker.version != SCHEMA_VERSION:
                raise CatalogUnavailable("Catalog schema version does not match this application")

    def check_schema(self) -> None:
        with self.sessions() as db:
            marker = db.get(SchemaVersion, 1)
            if marker is None or marker.version != SCHEMA_VERSION:
                raise CatalogUnavailable("Catalog database has not been initialized for this version")

    def account(self, email: str, role: str) -> str | None:
        if role == "guest":
            return None
        normalized = email.strip().lower()
        with self.sessions.begin() as db:
            record = db.scalar(select(Account).where(Account.email == normalized))
            if record is None:
                record = Account(id=str(uuid.uuid4()), email=normalized, role=role,
                                 created_at=utcnow(), last_seen_at=utcnow())
                db.add(record)
            else:
                if record.role != role:
                    record.role = role
                if utcnow() - as_utc(record.last_seen_at) >= timedelta(hours=1):
                    record.last_seen_at = utcnow()
            return record.id


_catalog: Catalog | None = None
_catalog_url: str | None = None


def get_catalog() -> Catalog:
    global _catalog, _catalog_url
    url = database_url()
    if not url:
        raise CatalogUnavailable("Catalog database is not configured")
    if _catalog is None or url != _catalog_url:
        _catalog = Catalog(url)
        _catalog_url = url
    return _catalog


def normalize_tags(values: Iterable[str]) -> list[str]:
    tags: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Tags must be text")
        tag = re.sub(r"\s+", " ", value.strip())
        if len(tag) > 80:
            raise ValueError("Tags must be at most 80 characters")
        if tag:
            tags[tag.casefold()] = tag
    return [tags[key] for key in sorted(tags)]


def _search_text(asset: Asset) -> str:
    metadata = (asset.extracted or {}).get("search_metadata", [])
    return " ".join(str(value) for value in
                    [asset.filename, asset.specimen_id or "", asset.plan_id or "",
                     *asset.tags, *metadata]).casefold()


def _date_and_source(asset: Asset) -> None:
    asset.date_key = as_utc(asset.acquired_at) or as_utc(asset.modified_at)
    asset.date_source = "acquired" if asset.acquired_at else "modified"


def _audit(db, asset: Asset, action: str, actor_id: str | None,
           before: dict | None = None, after: dict | None = None) -> None:
    db.add(AuditEvent(id=str(uuid.uuid4()), asset_id=asset.id, actor_id=actor_id,
                      action=action, before=before or {}, after=after or {}, created_at=utcnow()))


def _sidecar_signature(path: Path) -> str:
    parts = []
    for name in ("plan.json", "ome-manifest.json"):
        sidecar = path.parent / name
        try:
            stat = sidecar.stat()
            parts.append(f"{name}:{stat.st_size}:{stat.st_mtime_ns}")
        except FileNotFoundError:
            parts.append(f"{name}:missing")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _metadata_search_terms(ome_root: ET.Element | None, description: str,
                           sidecars: list[dict]) -> list[str]:
    """Keep human-readable metadata searchable without indexing tile arrays."""
    terms: list[str] = []
    seen: set[str] = set()
    size = 0

    def add(value: Any) -> None:
        nonlocal size
        if not isinstance(value, str):
            return
        term = re.sub(r"\s+", " ", value.strip())
        key = term.casefold()
        if (not term or len(term) > 2048 or key in seen or
                size + len(term) > 8192 or len(terms) >= 128):
            return
        seen.add(key)
        terms.append(term)
        size += len(term)

    if ome_root is None:
        add(description)
    else:
        for element in ome_root.iter():
            if _local_name(element.tag) in {"OME", "Pixels", "Plane", "TiffData", "BinData"}:
                continue
            for key, value in element.attrib.items():
                key = _local_name(key)
                if key in {"ID", "UUID"}:
                    continue
                add(key)
                add(value)
                if key == "NominalMagnification":
                    try:
                        add(f"{float(value):g}x")
                    except ValueError:
                        pass
            add(element.text)

    def add_json_fields(node: dict, depth: int = 0) -> None:
        if depth > 4:
            return
        for key, value in node.items():
            if key.casefold() in {"tiles", "planes", "images"}:
                continue
            if isinstance(value, str):
                add(key)
                add(value)
            elif isinstance(value, dict):
                add_json_fields(value, depth + 1)
            # Arrays (especially per-tile metadata) are deliberately skipped.

    for sidecar in sidecars:
        add_json_fields(sidecar)
    return terms


def _extract_metadata(path: Path) -> tuple[dict, int | None, int | None, str | None]:
    """Read TIFF headers and small companion fields without decoding pixels."""
    data: dict[str, Any] = {}
    width = height = None
    errors = []
    ome_root = None
    description = ""
    sidecars: list[dict] = []
    try:
        with tifffile.TiffFile(path) as tif:
            page = tif.pages[0]
            width, height = int(page.imagewidth), int(page.imagelength)
            description = page.description or ""
        if "<OME" in description:
            ome_root = ET.fromstring(description)
            image = next((el for el in ome_root.iter() if _local_name(el.tag) == "Image"), None)
            if image is not None:
                data["image_name"] = image.get("Name")
                for child in image:
                    name = _local_name(child.tag)
                    if name == "AcquisitionDate":
                        data["acquired_at"] = child.text
                    elif name == "Description" and child.text:
                        match = re.search(r"(?:^|;)\s*[^;:]*specimen\s*:\s*([^;]+)", child.text, re.I)
                        if match:
                            data["specimen_id"] = match.group(1).strip()
    except (OSError, ValueError, IndexError, ET.ParseError, tifffile.TiffFileError) as exc:
        errors.append(f"TIFF metadata: {type(exc).__name__}")

    for name in ("plan.json", "ome-manifest.json"):
        try:
            raw = json.loads((path.parent / name).read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            sidecars.append(raw)
            for source, target in (("specimenId", "specimen_id"), ("planId", "plan_id"),
                                   ("acquiredUtc", "acquired_at")):
                if isinstance(raw.get(source), str) and raw[source].strip():
                    data[target] = raw[source].strip()
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {type(exc).__name__}")

    data["search_metadata"] = _metadata_search_terms(ome_root, description, sidecars)
    return data, width, height, "; ".join(errors) or None


def _asset_dict(asset: Asset) -> dict:
    return {
        "id": asset.id, "path": asset.path, "filename": asset.filename,
        "size_bytes": asset.size_bytes, "width": asset.width, "height": asset.height,
        "specimen_id": asset.specimen_id, "plan_id": asset.plan_id,
        "acquired_at": as_utc(asset.acquired_at), "modified_at": as_utc(asset.modified_at),
        "imported_at": as_utc(asset.imported_at), "date_key": as_utc(asset.date_key),
        "date_source": asset.date_source, "uploader_id": asset.uploader_id,
        "owner_id": asset.owner_id, "status": asset.status, "tags": asset.tags,
        "index_error": asset.index_error,
    }


def _effective(asset: Asset) -> None:
    source = asset.extracted or {}
    overrides = asset.overrides or {}
    asset.specimen_id = overrides.get("specimen_id", source.get("specimen_id"))
    asset.plan_id = overrides.get("plan_id", source.get("plan_id"))
    raw_date = overrides.get("acquired_at", source.get("acquired_at"))
    asset.acquired_at = parse_time(raw_date)
    _date_and_source(asset)
    asset.search_text = _search_text(asset)


def _signed_file_id(value: int) -> int | None:
    """Represent an unsigned filesystem ID in a signed SQL BIGINT losslessly."""
    if not 0 < value < (1 << 64):
        return None
    return value if value < (1 << 63) else value - (1 << 64)


def _record_file(db, root: Path, path: Path, run_id: str | None,
                 now: datetime, stable_seconds: int, force: bool = False) -> tuple[bool, bool]:
    relative = path.relative_to(root).as_posix()
    stat = path.stat()
    device = _signed_file_id(stat.st_dev)
    inode = _signed_file_id(stat.st_ino)
    modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    sidecars = _sidecar_signature(path)
    existing = db.scalars(select(Asset).where(Asset.path == relative)
                          .order_by(Asset.imported_at.desc())).all()
    asset = next((item for item in existing if item.status != "missing"), None)
    if asset is None:
        asset = next((item for item in existing if item.status == "missing" and
                      item.device == device and
                      item.inode == inode and
                      item.size_bytes == stat.st_size and
                      item.mtime_ns == stat.st_mtime_ns), None)
    fresh = (now - modified).total_seconds() < stable_seconds and not force
    if fresh:
        if asset is not None:
            asset.seen_run_id = run_id
        return False, False

    renamed = False
    if asset is None and inode is not None and device is not None:
        matches = db.scalars(select(Asset).where(
            Asset.device == device, Asset.inode == inode,
            Asset.size_bytes == stat.st_size, Asset.mtime_ns == stat.st_mtime_ns,
        )).all()
        candidates = [item for item in matches if not (root / item.path).exists()]
        if len(candidates) == 1:
            asset = candidates[0]
            renamed = True

    new_asset = asset is None
    changed = new_asset or renamed
    if new_asset:
        asset = Asset(
            id=str(uuid.uuid4()), path=relative, filename=path.name,
            size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
            device=device, inode=inode,
            sidecar_signature=sidecars, modified_at=modified, imported_at=now,
            indexed_at=now, date_key=modified, date_source="modified",
            status="unverified", extracted={}, overrides={}, tags=[], search_text="",
            missing_observations=0,
        )
        db.add(asset)
        db.flush()
        _audit(db, asset, "discovered", None, after={"path": relative})
    elif renamed:
        old_path = asset.path
        asset.path = relative
        asset.filename = path.name
        _audit(db, asset, "moved", None, before={"path": old_path}, after={"path": relative})

    source_changed = (
        asset.size_bytes != stat.st_size or asset.mtime_ns != stat.st_mtime_ns
        or asset.sidecar_signature != sidecars
    )
    if new_asset or source_changed or force or renamed or "search_metadata" not in (asset.extracted or {}):
        before = dict(asset.extracted or {})
        extracted, width, height, error = _extract_metadata(path)
        asset.extracted = extracted
        asset.width = width
        asset.height = height
        asset.index_error = error
        asset.size_bytes = stat.st_size
        asset.mtime_ns = stat.st_mtime_ns
        asset.device = device
        asset.inode = inode
        asset.sidecar_signature = sidecars
        asset.modified_at = modified
        asset.indexed_at = now
        _effective(asset)
        changed = True
        if source_changed and asset.status in {"verified", "missing"}:
            prior = asset.status_before_missing if asset.status == "missing" else asset.status
            if prior == "verified":
                asset.status = "needs_review"
                asset.status_before_missing = None
                _audit(db, asset, "source_changed", None, before=before, after=extracted)

    if asset.status == "missing":
        asset.status = asset.status_before_missing or "unverified"
        asset.status_before_missing = None
        _audit(db, asset, "restored", None, after={"path": relative})
    asset.missing_since = None
    asset.missing_observations = 0
    asset.seen_run_id = run_id
    jobs = db.scalars(select(JobProvenance).where(JobProvenance.output_path == relative)).all()
    if len(jobs) == 1 and jobs[0].submitter_id and asset.uploader_id != jobs[0].submitter_id:
        asset.uploader_id = jobs[0].submitter_id
        _audit(db, asset, "uploader_linked", None,
               after={"job_id": jobs[0].job_id, "submitter_id": jobs[0].submitter_id})
    elif len(jobs) > 1 and asset.uploader_id is not None:
        prior = asset.uploader_id
        asset.uploader_id = None
        _audit(db, asset, "uploader_ambiguous", None, before={"uploader_id": prior})
    return changed, bool(asset.index_error)


def _safe_output_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        return None
    parts = value.split("/")
    return value if all(part and part not in {".", ".."} for part in parts) else None


def record_submissions(catalog: Catalog, jobs: list[dict], submitter_id: str | None,
                       submitted_folders: list[str]) -> None:
    catalog.check_schema()
    with catalog.sessions.begin() as db:
        for job in jobs:
            job_id, folder = job.get("id"), job.get("folder")
            if (not isinstance(job_id, str) or len(job_id) > 128 or
                    not isinstance(folder, str) or folder not in submitted_folders):
                continue
            record = db.get(JobProvenance, job_id)
            if record is None:
                db.add(JobProvenance(job_id=job_id, submitter_id=submitter_id,
                                     folder=folder, output_path=_safe_output_path(job.get("output_path")),
                                     submitted_at=utcnow()))


def reconcile_jobs(catalog: Catalog, jobs: list[dict]) -> None:
    catalog.check_schema()
    with catalog.sessions.begin() as db:
        for job in jobs:
            job_id = job.get("id")
            output_path = _safe_output_path(job.get("output_path"))
            if not isinstance(job_id, str) or not output_path or job.get("status") != "completed":
                continue
            record = db.get(JobProvenance, job_id)
            if record is not None and record.output_path != output_path:
                record.output_path = output_path
                asset = db.scalar(select(Asset).where(Asset.path == output_path,
                                                     Asset.status != "missing"))
                if asset is not None:
                    peers = db.scalars(select(JobProvenance).where(
                        JobProvenance.output_path == output_path)).all()
                    if len(peers) == 1 and record.submitter_id:
                        asset.uploader_id = record.submitter_id
                        _audit(db, asset, "uploader_linked", None,
                               after={"job_id": job_id, "submitter_id": record.submitter_id})
                    elif len(peers) > 1 and asset.uploader_id is not None:
                        prior = asset.uploader_id
                        asset.uploader_id = None
                        _audit(db, asset, "uploader_ambiguous", None,
                               before={"uploader_id": prior})


def _walk_images(root: Path):
    def onerror(exc: OSError) -> None:
        raise exc

    for directory, subdirs, filenames in os.walk(root, followlinks=False, onerror=onerror):
        subdirs[:] = [name for name in subdirs if not name.startswith(".")
                      and not (Path(directory) / name).is_symlink()]
        for name in filenames:
            if name.startswith(".") or Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            path = Path(directory) / name
            if path.is_symlink():
                continue
            yield path


def _run_scan(catalog: Catalog, root: Path, *, now: datetime | None = None,
              stable_seconds: int = 60, grace_hours: int = 24) -> dict:
    root = root.resolve()
    if not root.is_dir():
        raise CatalogUnavailable(f"Published image share is not mounted: {root}")
    catalog.check_schema()
    started = as_utc(now) or utcnow()
    run_id = str(uuid.uuid4())
    with catalog.sessions.begin() as db:
        db.add(IndexRun(id=run_id, started_at=started, status="running",
                        seen_count=0, changed_count=0, error_count=0))
    seen = changed = errors = 0
    try:
        batch: list[Path] = []

        def commit_batch(paths: list[Path]) -> tuple[int, int, int]:
            local_seen = local_changed = local_errors = 0
            with catalog.sessions.begin() as db:
                for path in paths:
                    try:
                        was_changed, had_error = _record_file(db, root, path, run_id,
                                                              started, stable_seconds)
                    except FileNotFoundError:
                        # A writer or operator moved the file during the walk.
                        continue
                    local_seen += 1
                    local_changed += int(was_changed)
                    local_errors += int(had_error)
            return local_seen, local_changed, local_errors

        for path in _walk_images(root):
            batch.append(path)
            if len(batch) >= 100:
                batch_seen, batch_changed, batch_errors = commit_batch(batch)
                seen += batch_seen
                changed += batch_changed
                errors += batch_errors
                batch.clear()
        if batch:
            batch_seen, batch_changed, batch_errors = commit_batch(batch)
            seen += batch_seen
            changed += batch_changed
            errors += batch_errors
        if seen == 0:
            with catalog.sessions() as db:
                prior = db.scalar(select(func.count()).select_from(Asset)) or 0
            if prior:
                raise CatalogUnavailable("Published share appears empty; missing reconciliation skipped")
        # Missing detection runs only after a complete walk. No partial share
        # listing can turn available records into missing records.
        with catalog.sessions.begin() as db:
            unseen = db.scalars(select(Asset).where(or_(Asset.seen_run_id != run_id,
                                                       Asset.seen_run_id.is_(None)))).all()
            for asset in unseen:
                if asset.missing_since is None:
                    asset.missing_since = started
                    asset.missing_observations = 1
                else:
                    asset.missing_observations += 1
                    if (asset.missing_observations >= 2 and
                            started - as_utc(asset.missing_since) >= timedelta(hours=grace_hours)
                            and asset.status != "missing"):
                        asset.status_before_missing = asset.status
                        asset.status = "missing"
                        _audit(db, asset, "missing", None, after={"path": asset.path})
            run = db.get(IndexRun, run_id)
            run.finished_at = utcnow()
            run.status = "completed"
            run.seen_count, run.changed_count, run.error_count = seen, changed, errors
    except Exception as exc:
        with catalog.sessions.begin() as db:
            run = db.get(IndexRun, run_id)
            run.finished_at = utcnow()
            run.status = "failed"
            run.seen_count, run.changed_count, run.error_count = seen, changed, errors
            run.message = f"{type(exc).__name__}: {exc}"[:1000]
        raise
    return {"run_id": run_id, "seen": seen, "changed": changed, "errors": errors}


def scan_once(catalog: Catalog, root: Path, *, now: datetime | None = None,
              stable_seconds: int = 60, grace_hours: int = 24) -> dict:
    """Reconcile a full library walk. PostgreSQL prevents overlapping workers."""
    if catalog.engine.dialect.name == "postgresql":
        with catalog.engine.connect() as lock:
            acquired = lock.scalar(text("SELECT pg_try_advisory_lock(82561534)"))
            if not acquired:
                raise CatalogUnavailable("Another catalog scan is already running")
            try:
                return _run_scan(catalog, root, now=now,
                                 stable_seconds=stable_seconds, grace_hours=grace_hours)
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(82561534)"))
    return _run_scan(catalog, root, now=now,
                     stable_seconds=stable_seconds, grace_hours=grace_hours)


def search(catalog: Catalog, *, query: str = "", date_from: datetime | None = None,
           date_to: datetime | None = None, uploader: str | None = None,
           verification: str | None = None, page: int = 1, page_size: int = 50) -> dict:
    catalog.check_schema()
    conditions = [Asset.status != "missing"]
    for term in query.split():
        escaped = (term.casefold().replace("\\", "\\\\")
                   .replace("%", "\\%").replace("_", "\\_"))
        conditions.append(Asset.search_text.like(f"%{escaped}%", escape="\\"))
    if date_from:
        conditions.append(Asset.date_key >= as_utc(date_from))
    if date_to:
        conditions.append(Asset.date_key < as_utc(date_to))
    if uploader == "unknown":
        conditions.append(Asset.uploader_id.is_(None))
    elif uploader:
        conditions.append(Asset.uploader_id == uploader)
    if verification:
        conditions.append(Asset.status == verification)
    with catalog.sessions() as db:
        total = db.scalar(select(func.count()).select_from(Asset).where(*conditions)) or 0
        rows = db.scalars(select(Asset).where(*conditions)
                          .order_by(Asset.date_key.desc(), Asset.id)
                          .limit(page_size).offset((page - 1) * page_size)).all()
        uploader_ids = {row.uploader_id for row in rows if row.uploader_id}
        accounts = {row.id: row.email for row in db.scalars(
            select(Account).where(Account.id.in_(uploader_ids))).all()} if uploader_ids else {}
        items = [_asset_dict(row) | {"uploader": accounts.get(row.uploader_id)} for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_asset(catalog: Catalog, asset_id: str) -> dict | None:
    catalog.check_schema()
    with catalog.sessions() as db:
        asset = db.get(Asset, asset_id)
        if asset is None:
            return None
        events = db.scalars(select(AuditEvent).where(AuditEvent.asset_id == asset_id)
                            .order_by(AuditEvent.created_at.desc()).limit(50)).all()
        detail = _asset_dict(asset)
        detail["extracted"] = asset.extracted
        detail["overrides"] = asset.overrides
        detail["audit"] = [{"action": e.action, "actor_id": e.actor_id,
                            "before": e.before, "after": e.after,
                            "created_at": as_utc(e.created_at)} for e in events]
        return detail


def curate(catalog: Catalog, asset_id: str, actor_id: str,
           changes: dict[str, Any], tags: list[str] | None = None,
           reset_fields: list[str] | None = None) -> dict | None:
    catalog.check_schema()
    with catalog.sessions.begin() as db:
        asset = db.get(Asset, asset_id)
        if asset is None:
            return None
        fields = set(changes) | set(reset_fields or [])
        if not fields.issubset(EDITABLE_FIELDS):
            raise ValueError("One or more fields cannot be corrected")
        before = {key: getattr(asset, key) for key in fields}
        if tags is not None:
            before["tags"] = asset.tags
            asset.tags = normalize_tags(tags)
        overrides = dict(asset.overrides or {})
        for key in reset_fields or []:
            overrides.pop(key, None)
        for key, value in changes.items():
            overrides[key] = as_utc(value).isoformat() if isinstance(value, datetime) else value
        asset.overrides = overrides
        _effective(asset)
        after = {key: getattr(asset, key) for key in fields}
        if tags is not None:
            after["tags"] = asset.tags
        if before != after:
            if asset.status == "verified":
                asset.status = "needs_review"
            _audit(db, asset, "curated", actor_id,
                   before={k: v.isoformat() if isinstance(v, datetime) else v for k, v in before.items()},
                   after={k: v.isoformat() if isinstance(v, datetime) else v for k, v in after.items()})
        return _asset_dict(asset)


def verify(catalog: Catalog, asset_id: str, actor_id: str) -> dict | None:
    catalog.check_schema()
    with catalog.sessions.begin() as db:
        asset = db.get(Asset, asset_id)
        if asset is None:
            return None
        if asset.status == "missing" or asset.index_error:
            raise ValueError("Missing or unreadable images cannot be verified")
        before = asset.status
        asset.status = "verified"
        asset.verified_by = actor_id
        asset.verified_at = utcnow()
        _audit(db, asset, "verified", actor_id,
               before={"status": before}, after={"status": "verified"})
        return _asset_dict(asset)


def reindex_asset(catalog: Catalog, root: Path, asset_id: str) -> dict | None:
    catalog.check_schema()
    with catalog.sessions.begin() as db:
        asset = db.get(Asset, asset_id)
        if asset is None:
            return None
        if asset.status == "missing":
            raise FileNotFoundError(asset.path)
        path = (root.resolve() / asset.path).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise FileNotFoundError(asset.path)
        _record_file(db, root.resolve(), path, asset.seen_run_id, utcnow(), 0, force=True)
        return _asset_dict(asset)


def status_summary(catalog: Catalog) -> dict:
    catalog.check_schema()
    with catalog.sessions() as db:
        counts = dict(db.execute(select(Asset.status, func.count()).group_by(Asset.status)).all())
        errors = db.scalar(select(func.count()).select_from(Asset)
                           .where(Asset.index_error.is_not(None))) or 0
        run = db.scalar(select(IndexRun).order_by(IndexRun.started_at.desc()).limit(1))
        successful = db.scalar(select(IndexRun).where(IndexRun.status == "completed")
                               .order_by(IndexRun.started_at.desc()).limit(1))
        return {"counts": counts, "index_errors": errors,
                "last_successful_scan": as_utc(successful.finished_at) if successful else None,
                "last_run": None if run is None else {
                    "id": run.id, "started_at": as_utc(run.started_at),
                    "finished_at": as_utc(run.finished_at), "status": run.status,
                    "seen": run.seen_count, "changed": run.changed_count,
                    "errors": run.error_count, "message": run.message,
                }}


def create_postgres_search_index(catalog: Catalog) -> None:
    """Optional trigram index for fast substring search on PostgreSQL.

    Azure Flexible Server must have pg_trgm on its extension allowlist first.
    """
    if catalog.engine.dialect.name != "postgresql":
        return
    catalog.check_schema()
    with catalog.engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_catalog_assets_search_trgm "
            "ON catalog_assets USING gin (search_text gin_trgm_ops)"
        ))
