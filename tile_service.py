from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Literal

import pyvips
from azure.storage.blob import BlobClient

TILE_SIZE = 256
JPEG_QUALITY = 85
MAX_DISCOVERED_LEVELS = 64
logger = logging.getLogger(__name__)


class ScanReadError(Exception):
    """The TIFF could not be inspected or decoded."""


class InvalidTileRequest(Exception):
    """The requested level or tile coordinate is outside the image."""


@dataclass(frozen=True)
class NativeLevel:
    width: int
    height: int
    source: Literal["page", "subifd"]
    index: int


@dataclass(frozen=True)
class Pyramid:
    width: int
    height: int
    levels: tuple[NativeLevel, ...]

    @property
    def osd_max_level(self) -> int:
        return math.ceil(math.log2(max(self.width, self.height))) if max(self.width, self.height) > 1 else 0


class AzureBlobSource(pyvips.SourceCustom):
    """A pyvips Custom Source to stream TIFFs directly from Azure Blob Storage."""
    def __init__(self, blob_client: BlobClient, chunk_size: int = 1024 * 1024):
        super().__init__()
        self.blob_client = blob_client
        props = self.blob_client.get_blob_properties()
        self.length = props.size
        self.offset = 0
        
        # Simple chunk caching to drastically reduce HTTP overhead 
        # for frequent tiny reads (like TIFF headers/IFDs).
        self.chunk_size = chunk_size
        self.buffer_start = 0
        self.buffer = b""

        self.on_read(self._read)
        self.on_seek(self._seek)

    def _read(self, length: int) -> bytes:
        if self.offset >= self.length:
            return b""

        # If the requested range is fully inside our current buffer:
        if self.buffer and self.buffer_start <= self.offset and (self.offset + length) <= (self.buffer_start + len(self.buffer)):
            start_idx = self.offset - self.buffer_start
            data = self.buffer[start_idx : start_idx + length]
            self.offset += len(data)
            return data

        # Otherwise, fetch a larger chunk to amortize HTTP latency
        fetch_len = max(length, self.chunk_size)
        fetch_len = min(fetch_len, self.length - self.offset)

        stream = self.blob_client.download_blob(offset=self.offset, length=fetch_len)
        self.buffer = stream.readall()
        self.buffer_start = self.offset

        data = self.buffer[:length]
        self.offset += len(data)
        return data

    def _seek(self, offset: int, whence: int) -> int:
        if whence == os.SEEK_SET:
            self.offset = offset
        elif whence == os.SEEK_CUR:
            self.offset += offset
        elif whence == os.SEEK_END:
            self.offset = self.length + offset
        return self.offset


def _metadata_int(image: pyvips.Image, name: str, default: int = 0) -> int:
    if image.get_typeof(name) == 0:
        return default
    try:
        return int(image.get(name))
    except (TypeError, ValueError, pyvips.Error):
        return default


def _load_level(blob_client: BlobClient, level: NativeLevel) -> tuple[pyvips.Image, AzureBlobSource]:
    source = AzureBlobSource(blob_client)
    options: dict[str, object] = {"access": "random", "page": 0}
    if level.source == "subifd":
        options["subifd"] = level.index
    else:
        options["page"] = level.index
        
    image = pyvips.Image.tiffload_source(source, **options)
    # Return the source too so the caller can keep it alive
    return image, source


def _same_image_shape(base_width: int, base_height: int, width: int, height: int) -> bool:
    base_ratio = base_width / base_height
    ratio = width / height
    return abs(ratio / base_ratio - 1.0) <= 0.03


def _is_next_reduced_level(previous: NativeLevel, candidate: NativeLevel, base: NativeLevel) -> bool:
    return (
        candidate.width < previous.width
        and candidate.height < previous.height
        and _same_image_shape(base.width, base.height, candidate.width, candidate.height)
    )


def _try_subifd_levels(blob_client: BlobClient, base_image: pyvips.Image, base: NativeLevel) -> list[NativeLevel]:
    levels: list[NativeLevel] = []
    count = _metadata_int(base_image, "n-subifds")

    indexes = range(min(count, MAX_DISCOVERED_LEVELS)) if count else range(MAX_DISCOVERED_LEVELS)
    previous = base
    for index in indexes:
        try:
            source = AzureBlobSource(blob_client)
            image = pyvips.Image.tiffload_source(source, page=0, subifd=index, access="random")
        except pyvips.Error:
            break
        candidate = NativeLevel(image.width, image.height, "subifd", index)
        if not _is_next_reduced_level(previous, candidate, base):
            break
        levels.append(candidate)
        previous = candidate
    return levels


def _try_page_levels(blob_client: BlobClient, page_count: int, base: NativeLevel) -> list[NativeLevel]:
    levels: list[NativeLevel] = []
    previous = base
    for page in range(1, min(page_count, MAX_DISCOVERED_LEVELS)):
        try:
            source = AzureBlobSource(blob_client)
            image = pyvips.Image.tiffload_source(source, page=page, access="random")
        except pyvips.Error:
            break
        candidate = NativeLevel(image.width, image.height, "page", page)
        if not _is_next_reduced_level(previous, candidate, base):
            break
        levels.append(candidate)
        previous = candidate
    return levels


# Custom dictionary cache to replace lru_cache since Azure's BlobClient object 
# changes memory addresses and breaks normal LRU hashing.
_pyramid_cache: dict[tuple[str, float], Pyramid] = {}

def discover_pyramid(blob_client: BlobClient) -> Pyramid:
    try:
        props = blob_client.get_blob_properties()
        modified_ts = props.last_modified.timestamp()
        
        key = (blob_client.blob_name, modified_ts)
        if key in _pyramid_cache:
            return _pyramid_cache[key]
            
        source = AzureBlobSource(blob_client)
        base_image = pyvips.Image.tiffload_source(source, page=0, access="random")
        base = NativeLevel(base_image.width, base_image.height, "page", 0)

        reduced = _try_subifd_levels(blob_client, base_image, base)
        if not reduced:
            reduced = _try_page_levels(blob_client, base_image.get_n_pages(), base)

        pyramid = Pyramid(base.width, base.height, tuple([base, *reduced]))
        
        # Keep cache from growing infinitely
        if len(_pyramid_cache) >= 16:
            _pyramid_cache.pop(next(iter(_pyramid_cache)))
        _pyramid_cache[key] = pyramid
        return pyramid
        
    except pyvips.Error as exc:
        logger.exception("pyvips failed while inspecting %s", blob_client.blob_name)
        raise ScanReadError(f"libvips could not read this TIFF: {exc}") from exc
    except Exception as exc:
        raise ScanReadError(f"Could not access blob: {exc}") from exc


def get_scan_info(blob_client: BlobClient) -> dict[str, object]:
    pyramid = discover_pyramid(blob_client)
    return {
        "id": blob_client.blob_name,
        "width": pyramid.width,
        "height": pyramid.height,
        "pyramid_levels": len(pyramid.levels),
        "tile_size": TILE_SIZE,
        "osd_max_level": pyramid.osd_max_level,
        "levels": [
            {
                "width": level.width,
                "height": level.height,
                "downsample": pyramid.width / level.width,
                "source": level.source,
                "source_index": level.index,
            }
            for level in pyramid.levels
        ],
    }


def _deep_zoom_dimensions(pyramid: Pyramid, osd_level: int) -> tuple[int, int]:
    scale = 2 ** (osd_level - pyramid.osd_max_level)
    return max(1, math.ceil(pyramid.width * scale)), max(1, math.ceil(pyramid.height * scale))


def _select_native_level(pyramid: Pyramid, target_width: int, target_height: int) -> NativeLevel:
    suitable = [
        level
        for level in pyramid.levels
        if level.width >= target_width and level.height >= target_height
    ]
    return suitable[-1] if suitable else pyramid.levels[-1]


def _jpeg_ready(image: pyvips.Image) -> pyvips.Image:
    if image.hasalpha():
        image = image.flatten(background=[0, 0, 0])
    if image.bands > 3:
        image = image[:3]
    if image.bands == 2:
        image = image[0]
    if image.format != "uchar":
        image = image.cast("uchar", shift=True)
    return image

from functools import lru_cache

@lru_cache(maxsize=2048)
def get_cached_tile(scan_id: str, level: int, x: int, y: int) -> bytes:
    get_tile()

def get_tile(blob_client: BlobClient, osd_level: int, tile_x: int, tile_y: int) -> bytes:
    pyramid = discover_pyramid(blob_client)
    if osd_level < 0 or osd_level > pyramid.osd_max_level:
        raise InvalidTileRequest(f"Level must be between 0 and {pyramid.osd_max_level}")
    if tile_x < 0 or tile_y < 0:
        raise InvalidTileRequest("Tile coordinates must be non-negative")

    target_width, target_height = _deep_zoom_dimensions(pyramid, osd_level)
    tile_left = tile_x * TILE_SIZE
    tile_top = tile_y * TILE_SIZE
    if tile_left >= target_width or tile_top >= target_height:
        raise InvalidTileRequest("Tile coordinates are outside this level")

    tile_width = min(TILE_SIZE, target_width - tile_left)
    tile_height = min(TILE_SIZE, target_height - tile_top)
    native = _select_native_level(pyramid, target_width, target_height)
    logger.info(
        "Using TIFF %s=%s (%sx%s) for requested level=%s tile=(%s,%s)",
        native.source,
        native.index,
        native.width,
        native.height,
        osd_level,
        tile_x,
        tile_y,
    )

    try:
        image, _keep_alive_source = _load_level(blob_client, native)

        source_left = math.floor(tile_left * native.width / target_width)
        source_top = math.floor(tile_top * native.height / target_height)
        source_right = math.ceil((tile_left + tile_width) * native.width / target_width)
        source_bottom = math.ceil((tile_top + tile_height) * native.height / target_height)
        source_right = min(source_right, native.width)
        source_bottom = min(source_bottom, native.height)

        region = image.crop(
            source_left,
            source_top,
            source_right - source_left,
            source_bottom - source_top,
        )
        if region.width != tile_width or region.height != tile_height:
            region = region.resize(
                tile_width / region.width,
                vscale=tile_height / region.height,
                kernel="lanczos3",
            )
            region = region.crop(0, 0, min(tile_width, region.width), min(tile_height, region.height))
            if region.width != tile_width or region.height != tile_height:
                region = region.embed(0, 0, tile_width, tile_height, extend="copy")

        return _jpeg_ready(region).write_to_buffer(".jpg", Q=JPEG_QUALITY)
    except pyvips.Error as exc:
        logger.exception(
            "pyvips failed: scan=%s level=%s tile=(%s,%s)",
            blob_client.blob_name,
            osd_level,
            tile_x,
            tile_y,
        )
        raise ScanReadError(f"libvips could not create this tile: {exc}") from exc