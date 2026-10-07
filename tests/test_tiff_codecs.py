import base64
import math
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import pyvips
import imagecodecs
import numpy as np
import tifffile

import tile_service


# Generated with tifffile/imagecodecs, not the decoder under test: lossless
# JPEG 2000 (TIFF tag 34712), RGB, OME-XML, and two reduced SubIFDs.
JPEG2000_OME_TIFF = (
    "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"
)


def reference_image() -> pyvips.Image:
    pixels = bytearray()
    for y in range(36):
        for x in range(52):
            red = 40 + (40 if x >= 26 else 0) + (80 if y >= 18 else 0)
            pixels.extend((red, red + 40, red + 80))
    return pyvips.Image.new_from_memory(bytes(pixels), 52, 36, 3, "uchar")


class TiffCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "jpeg2000.ome.tif"
        self.path.write_bytes(zlib.decompress(base64.b64decode(JPEG2000_OME_TIFF)))
        tile_service._pyramid_cache.clear()

    def tearDown(self) -> None:
        tile_service._pyramid_cache.clear()
        pyvips.cache_set_max(0)
        self.directory.cleanup()

    def assert_tiles(self, path: Path) -> None:
        info = tile_service.get_scan_info(str(path))
        reference = reference_image()
        with patch.object(tile_service, "TILE_SIZE", 16):
            for level in range(info["osd_max_level"] + 1):
                scale = 2 ** (level - info["osd_max_level"])
                width, height = math.ceil(52 * scale), math.ceil(36 * scale)
                points = {(0, 0), ((width - 1) // 16, (height - 1) // 16)}
                for x, y in points:
                    with self.subTest(path=path.name, level=level, x=x, y=y):
                        jpeg = tile_service.get_tile(str(path), level, x, y)
                        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
                        decoded = pyvips.Image.new_from_buffer(jpeg, "")
                        self.assertEqual(
                            (decoded.width, decoded.height),
                            (min(16, width - x * 16), min(16, height - y * 16)),
                        )
                        self.assertGreater(decoded.avg(), 10, "Decoded tile is blank")
                        if level == 0:
                            for channel, expected in enumerate((100, 140, 180)):
                                self.assertAlmostEqual(decoded[channel].avg(), expected, delta=8)
                        if level == info["osd_max_level"]:
                            expected = reference.crop(x * 16, y * 16, decoded.width, decoded.height)
                            for channel in range(3):
                                self.assertAlmostEqual(
                                    decoded[channel].avg(), expected[channel].avg(), delta=4
                                )

    def test_jpeg2000_ome_pyramid_pixels(self) -> None:
        info = tile_service.get_scan_info(str(self.path))
        self.assertEqual((info["width"], info["height"]), (52, 36))
        self.assertEqual(info["pyramid_levels"], 3)
        self.assertIn("<OME", tile_service.discover_pyramid(str(self.path)).raw_metadata)
        self.assertEqual(
            [(level["width"], level["height"]) for level in info["levels"]],
            [(52, 36), (26, 18), (13, 9)],
        )
        self.assert_tiles(self.path)

    def test_deflate_pixels(self) -> None:
        path = Path(self.directory.name) / "deflate.tif"
        reference_image().tiffsave(str(path), compression="deflate", tile=True)
        self.assert_tiles(path)

    def test_bioformats_jpeg2000_pixels(self) -> None:
        path = Path(self.directory.name) / "native-jpeg2000.tif"
        reference_image().tiffsave(
            str(path), compression="jp2k", Q=100, tile=True,
            tile_width=128, tile_height=128,
        )
        with tifffile.TiffFile(path) as tiff:
            self.assertEqual(int(tiff.pages[0].compression), 33004)
        self.assert_tiles(path)

    def test_region_decodes_only_intersecting_tiles(self) -> None:
        with tifffile.TiffFile(self.path) as tiff:
            page = tiff.pages[0]
            with patch.object(page, "decode", wraps=page.decode) as decode:
                with patch.object(tifffile, "TiffFile", return_value=tiff):
                    region = tile_service._load_region(
                        str(self.path), tile_service.NativeLevel(52, 36, "page", 0), 15, 15, 2, 2
                    )
            self.assertEqual([call.args[1] for call in decode.call_args_list], [0, 1, 4, 5])
            expected = reference_image().crop(15, 15, 2, 2)
            self.assertEqual(region.write_to_memory(), expected.write_to_memory())

    def test_jpeg2000_16bit_tiles_and_strips(self) -> None:
        for tiled in (True, False):
            with self.subTest(tiled=tiled):
                path = Path(self.directory.name) / f"16bit-{tiled}.tif"
                pixels = np.empty((18, 20, 3), dtype=np.uint16)
                pixels[:] = (5120, 10240, 15360)
                options = {"tile": (16, 16)} if tiled else {"rowsperstrip": 16}
                tifffile.imwrite(path, pixels, photometric="rgb", compression="jpeg2000", **options)
                base = tile_service.NativeLevel(20, 18, "page", 0)
                region = tile_service._load_region(str(path), base, 15, 15, 5, 3)
                self.assertEqual(region.format, "ushort")
                for channel, expected in enumerate((5120, 10240, 15360)):
                    self.assertEqual(region[channel].avg(), expected)
                jpeg = tile_service.get_tile(str(path), 5, 0, 0)
                decoded = pyvips.Image.new_from_buffer(jpeg, "")
                for channel, expected in enumerate((20, 40, 60)):
                    self.assertAlmostEqual(decoded[channel].avg(), expected, delta=2)

    def test_jpeg2000_segment_errors_are_reported(self) -> None:
        with tifffile.TiffFile(self.path) as tiff:
            page = tiff.pages[0]
            with patch.object(page, "decode", side_effect=imagecodecs.Jpeg2kError("damaged segment")):
                with patch.object(tifffile, "TiffFile", return_value=tiff):
                    with self.assertLogs("tile_service", level="ERROR"):
                        with self.assertRaisesRegex(tile_service.ScanReadError, "damaged segment"):
                            tile_service._load_region(
                                str(self.path), tile_service.NativeLevel(52, 36, "page", 0),
                                0, 0, 16, 16,
                            )

    def test_jpeg2000_grayscale_photometric(self) -> None:
        for photometric in ("minisblack", "miniswhite"):
            with self.subTest(photometric=photometric):
                path = Path(self.directory.name) / f"{photometric}.tif"
                tifffile.imwrite(
                    path, np.full((16, 16), 40, dtype=np.uint8),
                    photometric=photometric, tile=(16, 16), compression="jpeg2000",
                )
                region = tile_service._load_region(
                    str(path), tile_service.NativeLevel(16, 16, "page", 0), 0, 0, 16, 16
                )
                self.assertEqual(region.bands, 1)
                self.assertEqual(region.avg(), 40 if photometric == "minisblack" else 215)

    def test_all_tiff_reads_are_strict(self) -> None:
        with patch.object(pyvips.Image, "tiffload") as load:
            tile_service._load_tiff("scan.tif")
            tile_service._load_level("scan.tif", tile_service.NativeLevel(49, 35, "page", 2))
            tile_service._load_level("scan.tif", tile_service.NativeLevel(25, 18, "subifd", 1))
            for call in load.call_args_list:
                self.assertEqual(call.kwargs["fail_on"], "warning")
                self.assertEqual(call.kwargs["access"], "random")
            self.assertEqual(load.call_args_list[1].kwargs["page"], 2)
            self.assertEqual(load.call_args_list[2].kwargs["subifd"], 1)

    def test_tile_decoder_error_is_not_replaced_with_black_pixels(self) -> None:
        tile_service.discover_pyramid(str(self.path))
        with patch.object(tile_service, "_load_region", side_effect=pyvips.Error("unsupported codec")):
            with self.assertLogs("tile_service", level="ERROR"):
                with self.assertRaisesRegex(tile_service.ScanReadError, "unsupported codec"):
                    tile_service.get_tile(str(self.path), 6, 0, 0)

    def test_crop_decoder_error_is_propagated(self) -> None:
        tile_service.discover_pyramid(str(self.path))
        with patch.object(tile_service, "_load_region", side_effect=pyvips.Error("unsupported codec")):
            with self.assertRaisesRegex(tile_service.ScanReadError, "unsupported codec"):
                tile_service.crop_scan_region(str(self.path), 0, 0, 16, 16)

    def test_declared_subifd_error_is_not_silently_ignored(self) -> None:
        base = tile_service.NativeLevel(49, 35, "page", 0)
        with patch.object(tile_service, "_metadata_int", return_value=2):
            with patch.object(tile_service, "_load_tiff", side_effect=pyvips.Error("broken SubIFD")):
                with self.assertRaisesRegex(pyvips.Error, "broken SubIFD"):
                    tile_service._try_subifd_levels(str(self.path), reference_image(), base)

    def test_declared_page_error_is_not_silently_ignored(self) -> None:
        base = tile_service.NativeLevel(49, 35, "page", 0)
        with patch.object(tile_service, "_load_tiff", side_effect=pyvips.Error("broken page")):
            with self.assertRaisesRegex(pyvips.Error, "broken page"):
                tile_service._try_page_levels(str(self.path), 2, base)


if __name__ == "__main__":
    unittest.main()
