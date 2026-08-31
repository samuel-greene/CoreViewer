from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import pyvips

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
        # OpenSeadragon/Deep Zoom level max_level is the full-resolution image.
        return math.ceil(math.log2(max(self.width, self.height))) if max(self.width, self.height) > 1 else 0


def _metadata_int(image: pyvips.Image, name: str, default: int = 0) -> int:
    if image.get_typeof(name) == 0:
        return default
    try:
        return int(image.get(name))
    except (TypeError, ValueError, pyvips.Error):
        return default


def _load_level(path: Path, level: NativeLevel) -> pyvips.Image:
    options: dict[str, object] = {"access": "random", "page": 0}
    if level.source == "subifd":
        options["subifd"] = level.index
    else:
        options["page"] = level.index
    return pyvips.Image.tiffload(str(path), **options)


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


def _try_subifd_levels(path: Path, base_image: pyvips.Image, base: NativeLevel) -> list[NativeLevel]:
    """Read reduced images referenced by the first page's TIFF SUBIFD tag."""
    levels: list[NativeLevel] = []
    count = _metadata_int(base_image, "n-subifds")

    # Modern libvips exposes n-subifds. Older builds may not, so probe until the
    # loader rejects an index. A candidate is accepted only if dimensions form
    # a decreasing pyramid with the same aspect ratio as the main image.
    indexes = range(min(count, MAX_DISCOVERED_LEVELS)) if count else range(MAX_DISCOVERED_LEVELS)
    previous = base
    for index in indexes:
        try:
            image = pyvips.Image.tiffload(str(path), page=0, subifd=index, access="random")
        except pyvips.Error:
            break
        candidate = NativeLevel(image.width, image.height, "subifd", index)
        if not _is_next_reduced_level(previous, candidate, base):
            break
        levels.append(candidate)
        previous = candidate
    return levels


def _try_page_levels(path: Path, page_count: int, base: NativeLevel) -> list[NativeLevel]:
    """Conservatively detect a classic TIFF pyramid stored as top-level pages."""
    levels: list[NativeLevel] = []
    previous = base
    for page in range(1, min(page_count, MAX_DISCOVERED_LEVELS)):
        image = pyvips.Image.tiffload(str(path), page=page, access="random")
        candidate = NativeLevel(image.width, image.height, "page", page)
        if not _is_next_reduced_level(previous, candidate, base):
            # Equal-sized OME pages are usually Z/C/T planes, not pyramid levels.
            # Stopping here avoids treating an arbitrary page as a zoom level.
            break
        levels.append(candidate)
        previous = candidate
    return levels


@lru_cache(maxsize=16)
def _discover_pyramid_cached(path_text: str, modified_ns: int) -> Pyramid:
    del modified_ns  # Included in the cache key so replacing a scan invalidates metadata.
    path = Path(path_text)
    try:
        base_image = pyvips.Image.tiffload(str(path), page=0, access="random")
        base = NativeLevel(base_image.width, base_image.height, "page", 0)

        # Bio-Formats-style OME pyramids normally use SUBIFDs. Classic pyramidal
        # TIFFs may instead put reduced images in the top-level page chain. We
        # verify dimensions in either case; page N is never assumed to be level N.
        reduced = _try_subifd_levels(path, base_image, base)
        if not reduced:
            reduced = _try_page_levels(path, base_image.get_n_pages(), base)

        return Pyramid(base.width, base.height, tuple([base, *reduced]))
    except pyvips.Error as exc:
        logger.exception("pyvips failed while inspecting %s", path)
        raise ScanReadError(f"libvips could not read this TIFF: {exc}") from exc


def discover_pyramid(path: Path) -> Pyramid:
    try:
        stat = path.stat()
    except OSError as exc:
        raise ScanReadError(f"Could not access scan: {exc}") from exc
    return _discover_pyramid_cached(str(path.resolve()), stat.st_mtime_ns)


def get_scan_info(path: Path) -> dict[str, object]:
    pyramid = discover_pyramid(path)
    return {
        "id": path.name,
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
    # Choose the smallest stored level that is still at least as detailed as the
    # requested Deep Zoom level. This minimizes decoding while avoiding upscaling.
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
        # shift=True maps integer bit depths down to 8-bit without calculating a
        # whole-image min/max (which would defeat region-based tile reads).
        image = image.cast("uchar", shift=True)
    return image


def get_tile(path: Path, osd_level: int, tile_x: int, tile_y: int) -> bytes:
    pyramid = discover_pyramid(path)
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
        image = _load_level(path, native)

        # Map the Deep Zoom tile rectangle into the chosen stored TIFF level.
        # Rounding outward ensures no source pixels are missed at level edges.
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
            # libvips rounding can very occasionally add one pixel.
            region = region.crop(0, 0, min(tile_width, region.width), min(tile_height, region.height))
            if region.width != tile_width or region.height != tile_height:
                region = region.embed(0, 0, tile_width, tile_height, extend="copy")

        return _jpeg_ready(region).write_to_buffer(".jpg", Q=JPEG_QUALITY)
    except pyvips.Error as exc:
        logger.exception(
            "pyvips failed: scan=%s level=%s tile=(%s,%s)",
            path.name,
            osd_level,
            tile_x,
            tile_y,
        )
        raise ScanReadError(f"libvips could not create this tile: {exc}") from exc
