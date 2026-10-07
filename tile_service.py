from __future__ import annotations

import logging
import math
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal

import pyvips
import imagecodecs
import numpy as np
import tifffile

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
    raw_metadata: str = ""
    metadata_summary: str = ""

    @property
    def osd_max_level(self) -> int:
        return math.ceil(math.log2(max(self.width, self.height))) if max(self.width, self.height) > 1 else 0


def _metadata_int(image: pyvips.Image, name: str, default: int = 0) -> int:
    if image.get_typeof(name) == 0:
        return default
    try:
        return int(image.get(name))
    except (TypeError, ValueError, pyvips.Error):
        return default


def _load_tiff(file_path: str, *, page: int = 0, subifd: int | None = None) -> pyvips.Image:
    # Without strict decoding, unsupported compression can silently produce black tiles.
    options: dict[str, object] = {"access": "random", "page": page, "fail_on": "warning"}
    if subifd is not None:
        options["subifd"] = subifd
    return pyvips.Image.tiffload(file_path, **options)


def _load_level(file_path: str, level: NativeLevel) -> pyvips.Image:
    if level.source == "subifd":
        return _load_tiff(file_path, subifd=level.index)
    return _load_tiff(file_path, page=level.index)


def _load_region(
    file_path: str,
    level: NativeLevel,
    left: int,
    top: int,
    width: int,
    height: int,
) -> pyvips.Image:
    try:
        with tifffile.TiffFile(file_path) as tiff:
            if level.source == "subifd":
                subpages = tiff.pages[0].pages
                if subpages is None:
                    raise ScanReadError("The TIFF pyramid's SubIFDs are missing")
                page = subpages[level.index]
            else:
                page = tiff.pages[level.index]
            if isinstance(page, tifffile.TiffFrame):
                page = page.aspage()

            if int(page.compression) != 34712:
                return _load_level(file_path, level).crop(left, top, width, height)

            if page.planarconfig != 1 or page.imagedepth != 1:
                raise ScanReadError("JPEG 2000 TIFF decoding requires a 2D contiguous sample layout")
            if page.photometric not in (0, 1, 2) or page.dtype.name not in ("uint8", "uint16"):
                raise ScanReadError("JPEG 2000 TIFF decoding supports 8/16-bit grayscale and RGB pixels")
            orientation = page.tags.get("Orientation")
            if orientation is not None and orientation.value != 1:
                raise ScanReadError("JPEG 2000 TIFF decoding requires top-left image orientation")

            segment_width = page.tilewidth if page.is_tiled else page.imagewidth
            segment_height = page.tilelength if page.is_tiled else page.rowsperstrip
            columns = math.ceil(page.imagewidth / segment_width)
            samples = page.samplesperpixel
            pixels = np.empty((height, width, samples), dtype=page.dtype)

            for row in range(top // segment_height, (top + height - 1) // segment_height + 1):
                for column in range(left // segment_width, (left + width - 1) // segment_width + 1):
                    index = row * columns + column
                    byte_count = page.databytecounts[index]
                    if not byte_count:
                        raise ScanReadError(f"JPEG 2000 TIFF segment {index} is missing")
                    tiff.filehandle.seek(page.dataoffsets[index])
                    encoded = tiff.filehandle.read(byte_count)
                    if len(encoded) != byte_count:
                        raise ScanReadError(f"JPEG 2000 TIFF segment {index} is truncated")
                    decoded, _, _ = page.decode(encoded, index)
                    if decoded is None:
                        raise ScanReadError(f"JPEG 2000 TIFF segment {index} could not be decoded")

                    origin_x, origin_y = column * segment_width, row * segment_height
                    x0, y0 = max(left, origin_x), max(top, origin_y)
                    x1 = min(left + width, origin_x + segment_width)
                    y1 = min(top + height, origin_y + segment_height)
                    pixels[y0 - top : y1 - top, x0 - left : x1 - left] = decoded[
                        0, y0 - origin_y : y1 - origin_y, x0 - origin_x : x1 - origin_x
                    ]

            if page.photometric == 0:
                pixels[:, :, 0] = np.iinfo(page.dtype).max - pixels[:, :, 0]
            pixel_format = "uchar" if page.dtype.name == "uint8" else "ushort"
            interpretation = (
                ("b-w" if pixel_format == "uchar" else "grey16")
                if samples < 3
                else ("srgb" if pixel_format == "uchar" else "rgb16")
            )
            return pyvips.Image.new_from_memory(
                pixels.tobytes(), width, height, samples, pixel_format
            ).copy(interpretation=interpretation)
    except (OSError, ValueError, IndexError, NotImplementedError, imagecodecs.Jpeg2kError) as exc:
        logger.exception("Failed to decode TIFF region: scan=%s level=%s", file_path, level)
        raise ScanReadError(f"Could not decode TIFF region: {exc}") from exc


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


def _try_subifd_levels(file_path: str, base_image: pyvips.Image, base: NativeLevel) -> list[NativeLevel]:
    levels: list[NativeLevel] = []
    count = _metadata_int(base_image, "n-subifds")

    indexes = range(min(count, MAX_DISCOVERED_LEVELS)) if count else range(MAX_DISCOVERED_LEVELS)
    previous = base
    for index in indexes:
        try:
            image = _load_tiff(file_path, subifd=index)
        except pyvips.Error:
            if count:
                raise
            break
        candidate = NativeLevel(image.width, image.height, "subifd", index)
        if not _is_next_reduced_level(previous, candidate, base):
            break
        levels.append(candidate)
        previous = candidate
    return levels


def _try_page_levels(file_path: str, page_count: int, base: NativeLevel) -> list[NativeLevel]:
    levels: list[NativeLevel] = []
    previous = base
    for page in range(1, min(page_count, MAX_DISCOVERED_LEVELS)):
        image = _load_tiff(file_path, page=page)
        candidate = NativeLevel(image.width, image.height, "page", page)
        if not _is_next_reduced_level(previous, candidate, base):
            break
        levels.append(candidate)
        previous = candidate
    return levels


def _extract_ome_metadata(description: str) -> str:
    """Parses concise file specs from OME-XML strings."""
    if not description:
        return ""
    if "<OME" not in description:
        return description[:100]

    try:
        root = ET.fromstring(description)
    except ET.ParseError:
        logger.warning("Could not parse embedded OME-XML metadata")
        return "OME-TIFF Metadata"

    def find_child(parent: ET.Element, name: str) -> ET.Element | None:
        return next(
            (child for child in parent if child.tag.rsplit("}", 1)[-1] == name),
            None,
        )

    image = next(
        (element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "Image"),
        None,
    )
    pixels = next(
        (element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "Pixels"),
        None,
    )
    meta_lines = []

    if image is not None:
        image_name = image.get("Name")
        if image_name:
            meta_lines.append(f"Image: {image_name}")

        acquisition_date = find_child(image, "AcquisitionDate")
        if acquisition_date is not None and acquisition_date.text:
            meta_lines.append(f"Acquired: {acquisition_date.text.strip()}")

        image_description = find_child(image, "Description")
        if image_description is not None and image_description.text:
            description_parts = image_description.text.split(";")
            for part in description_parts:
                key, separator, value = part.partition(":")
                if separator and value.strip():
                    label = "Specimen" if key.strip().lower().endswith("specimen") else key.strip()
                    meta_lines.append(f"{label}: {value.strip()}")
                elif part.strip():
                    meta_lines.append(f"Description: {part.strip()}")

    if pixels is not None:
        size_x, size_y = pixels.get("SizeX"), pixels.get("SizeY")
        if size_x and size_y:
            meta_lines.append(f"Dimensions: {size_x} × {size_y}")

        for attribute, label in (
            ("Type", "Pixel type"),
            ("DimensionOrder", "Dimension order"),
            ("SizeC", "Channels"),
            ("SizeZ", "Z-slices"),
            ("SizeT", "Time points"),
        ):
            value = pixels.get(attribute)
            if value:
                meta_lines.append(f"{label}: {value}")

        for axis in ("X", "Y", "Z"):
            value = pixels.get(f"PhysicalSize{axis}")
            if value:
                unit = pixels.get(f"PhysicalSize{axis}Unit", "µm")
                meta_lines.append(f"Pixel size {axis}: {value} {unit}")

        channel_names = []
        for channel in pixels:
            if channel.tag.rsplit("}", 1)[-1] != "Channel":
                continue
            name = channel.get("Name") or channel.get("Fluor")
            if name:
                channel_names.append(name)
        if channel_names:
            meta_lines.append(f"Channel names: {', '.join(channel_names)}")

    return "\n".join(meta_lines) if meta_lines else "OME-TIFF Metadata"


_pyramid_cache: dict[tuple[str, float], Pyramid] = {}

def discover_pyramid(file_path: str) -> Pyramid:
    try:
        # Use local file modification time instead of Azure Blob properties
        modified_ts = os.path.getmtime(file_path)
        
        key = (file_path, modified_ts)
        if key in _pyramid_cache:
            return _pyramid_cache[key]
            
        base_image = _load_tiff(file_path)
        base = NativeLevel(base_image.width, base_image.height, "page", 0)

        reduced = _try_subifd_levels(file_path, base_image, base)
        if not reduced:
            reduced = _try_page_levels(file_path, base_image.get_n_pages(), base)

        # Extract OME metadata
        raw_metadata = ""
        if base_image.get_typeof("image-description") != 0:
            raw_metadata = base_image.get("image-description")
            
        metadata_summary = _extract_ome_metadata(raw_metadata)

        pyramid = Pyramid(base.width, base.height, tuple([base, *reduced]), raw_metadata, metadata_summary)
        
        if len(_pyramid_cache) >= 16:
            _pyramid_cache.pop(next(iter(_pyramid_cache)))
        _pyramid_cache[key] = pyramid
        return pyramid
        
    except pyvips.Error as exc:
        logger.exception("pyvips failed while inspecting %s", file_path)
        raise ScanReadError(f"libvips could not read this TIFF: {exc}") from exc
    except Exception as exc:
        raise ScanReadError(f"Could not access local file: {exc}") from exc


def get_scan_info(file_path: str) -> dict[str, object]:
    pyramid = discover_pyramid(file_path)
    return {
        "id": os.path.basename(file_path),
        "width": pyramid.width,
        "height": pyramid.height,
        "pyramid_levels": len(pyramid.levels),
        "tile_size": TILE_SIZE,
        "osd_max_level": pyramid.osd_max_level,
        "metadata_summary": pyramid.metadata_summary,
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


def get_tile(file_path: str, osd_level: int, tile_x: int, tile_y: int) -> bytes:
    pyramid = discover_pyramid(file_path)
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

    try:
        source_left = math.floor(tile_left * native.width / target_width)
        source_top = math.floor(tile_top * native.height / target_height)
        source_right = math.ceil((tile_left + tile_width) * native.width / target_width)
        source_bottom = math.ceil((tile_top + tile_height) * native.height / target_height)
        source_right = min(source_right, native.width)
        source_bottom = min(source_bottom, native.height)

        region = _load_region(
            file_path,
            native,
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
            file_path,
            osd_level,
            tile_x,
            tile_y,
        )
        raise ScanReadError(f"libvips could not create this tile: {exc}") from exc


MARKING_SVG_COLOR = "rgb(239,68,68)"  # tailwind red-500, matches the viewer's on-screen laser color


def _default_marking_width(region: pyvips.Image) -> float:
    """A visible stroke width that scales with the exported region size,
    for strokes that didn't come with an explicit width."""
    return max(2, round(max(region.width, region.height) / 400))


def _ensure_rgb_capable(region: pyvips.Image) -> pyvips.Image:
    """Promotes a region with fewer than 3 bands to RGB (or RGBA) so a red
    marking can actually be represented as red rather than as a plain
    brightness value. Regions that already carry color (bands >= 3) are
    returned unchanged.

    This only runs when the crop actually has a marking on it -- an
    unmarked grayscale/native crop is untouched and keeps its original
    band count and bit depth.
    """
    if region.bands >= 3:
        return region
    if region.bands == 1:
        return region.bandjoin([region, region])
    # bands == 2: treat as grayscale + alpha, replicate the gray channel
    # into RGB and keep the alpha channel on top.
    gray = region[0]
    alpha = region[1]
    return gray.bandjoin([gray, gray, alpha])


def _marking_max_value(region: pyvips.Image) -> float:
    if region.format in ("ushort", "short"):
        return 65535
    if region.format in ("float", "double"):
        return 1.0
    return 255


def _svg_stroke_elements(markings: list[dict], default_width: float) -> list[str]:
    """Builds one SVG shape per stroke: a filled dot for a plain click, or a
    round-capped/joined polyline for a drag, matching the on-screen look."""
    elements = []
    for stroke in markings:
        points = stroke.get("points") or []
        if not points:
            continue
        width = stroke.get("width") or default_width
        if len(points) == 1:
            x, y = points[0]
            elements.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{width / 2:.2f}" fill="{MARKING_SVG_COLOR}"/>')
        else:
            points_attr = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
            elements.append(
                f'<polyline points="{points_attr}" fill="none" stroke="{MARKING_SVG_COLOR}" '
                f'stroke-width="{width:.2f}" stroke-linecap="round" stroke-linejoin="round"/>'
            )
    return elements


def _draw_markings(
    region: pyvips.Image, markings: list[dict], origin_x: int, origin_y: int
) -> pyvips.Image:
    """Burns laser-pointer strokes into an already-cropped region."""
    if not markings:
        return region

    default_width = _default_marking_width(region)
    local_markings = [
        {
            "points": [(float(px) - origin_x, float(py) - origin_y) for px, py in (m.get("points") or [])],
            "width": m.get("width"),
        }
        for m in markings
    ]

    elements = _svg_stroke_elements(local_markings, default_width)
    if not elements:
        return region

    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{region.width}" height="{region.height}">' + "".join(
        elements
    ) + "</svg>"
    mask = pyvips.Image.svgload_buffer(svg.encode("utf-8"))  # uchar RGBA, same size as region

    color_bands = min(region.bands, 3)
    max_value = _marking_max_value(region)
    mask_rgb = (mask[0:color_bands]) * (max_value / 255.0)
    mask_alpha = mask[3] / 255.0  # float, 0..1

    base_rgb = region[0:color_bands]
    blended = (mask_rgb * mask_alpha + base_rgb * (1 - mask_alpha)).cast(region.format)

    if region.bands > color_bands:
        blended = blended.bandjoin(region[color_bands : region.bands])

    return blended


def _draw_metadata(region: pyvips.Image, text: str) -> pyvips.Image:
    """Burns text metadata as an SVG mask into the bottom left of the image."""
    if not text:
        return region

    escaped_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    font_size = max(12, min(36, round(region.width / 40)))
    x_pos = max(10, round(region.width * 0.02))
    y_pos = region.height - max(10, round(region.height * 0.02))

    # Match UI styling (gray, slightly transparent, subtle shadow)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{region.width}" height="{region.height}">
        <text x="{x_pos}" y="{y_pos}" font-family="monospace" font-size="{font_size}px" fill="rgba(156,163,175,0.7)" stroke="rgba(0,0,0,0.5)" stroke-width="0.5">{escaped_text}</text>
    </svg>'''
    
    try:
        mask = pyvips.Image.svgload_buffer(svg.encode("utf-8"))
        if mask.bands < 4:
            return region

        color_bands = min(region.bands, 3)
        max_value = _marking_max_value(region)
        mask_rgb = (mask[0:color_bands]) * (max_value / 255.0)
        mask_alpha = mask[3] / 255.0

        base_rgb = region[0:color_bands]
        blended = (mask_rgb * mask_alpha + base_rgb * (1 - mask_alpha)).cast(region.format)

        if region.bands > color_bands:
            blended = blended.bandjoin(region[color_bands : region.bands])

        return blended
    except pyvips.Error:
        return region


def crop_scan_region(
    file_path: str,
    x: int,
    y: int,
    width: int,
    height: int,
    fmt: str = "jpg",
    markings: list[dict] | None = None,
) -> bytes:
    try:
        pyramid = discover_pyramid(file_path)
        base = pyramid.levels[0]

        # Clamp region coordinates within image bounds
        x_val = max(0, min(int(x), pyramid.width - 1))
        y_val = max(0, min(int(y), pyramid.height - 1))
        w_val = max(1, min(int(width), pyramid.width - x_val))
        h_val = max(1, min(int(height), pyramid.height - y_val))

        region = _load_region(file_path, base, x_val, y_val, w_val, h_val)

        if markings:
            region = _ensure_rgb_capable(region)
            region = _draw_markings(region, markings, x_val, y_val)

        # Apply metadata overlay prior to final formatting
        if pyramid.metadata_summary:
            region = _ensure_rgb_capable(region)
            region = _draw_metadata(region, pyramid.metadata_summary)

        fmt_normalized = fmt.lower().lstrip(".")

        if fmt_normalized in ("tif", "tiff"):
            # Keep the region at its native bit depth/band count instead of
            # flattening to 8-bit RGB -- TIFF can hold the original data,
            # which is the whole point of offering it as an export option.
            return region.write_to_buffer(".tif", compression="deflate", tile=True)

        # JPEG/PNG can't represent every source format (alpha, >8bit, etc.),
        # so those still go through the flatten/8-bit prep step.
        prepared = _jpeg_ready(region)
        if fmt_normalized == "png":
            return prepared.write_to_buffer(".png")
        return prepared.write_to_buffer(".jpg", Q=JPEG_QUALITY)
    except pyvips.Error as exc:
        raise ScanReadError(f"libvips failed to crop region: {exc}") from exc