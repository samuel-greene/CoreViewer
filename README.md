# Minimal OME-TIFF viewer

A small FastAPI and OpenSeadragon proof of concept for browsing large pyramidal TIFF/OME-TIFF scans. Tiles are cropped lazily by libvips and returned as JPEG; the complete full-resolution image is not loaded into a Python array.

## Install

Python 3.10 or newer is recommended. `pyvips` is a Python binding and also needs the native **libvips** runtime:

- **Windows:** download the full `vips-dev-x64-all-*.zip` package (or the `all` package for your architecture) from the [Windows libvips releases](https://github.com/libvips/build-win64-mxe/releases), extract it, and add its `bin` directory to `PATH`. Restart VS Code/your terminal afterward. Use the full package, rather than `web` packages or `pyvips[binary]`, for libvips-native JPEG 2000 TIFF variants.
- **Ubuntu/Debian:** `sudo apt install libvips42` (use `libvips-dev` too only when a source build needs headers).
- **macOS:** `brew install vips`.

From this directory, create/activate a virtual environment and install the Python packages:

    python -m venv .venv
    .venv\Scripts\activate        # Windows PowerShell
    python -m pip install -r requirements.txt

On Linux/macOS, activation is `source .venv/bin/activate`.

## JPEG 2000-compressed OME-TIFF

Standard JPEG 2000 TIFF compression tag 34712 is decoded with `tifffile` and `imagecodecs`: only the tiles or strips intersecting a requested region are read. The decoder supports 8/16-bit grayscale and RGB, contiguous samples, and top-left orientation. Unsupported layouts and damaged segments return explicit errors.

Bio-Formats/Aperio JPEG 2000 TIFF tags handled natively by libvips still require a native build with OpenJPEG support. Installing OpenJPEG alongside a libvips build compiled without that support does not enable it. Standalone `.jp2` files are not supported by this viewer.

Verify the actual runtime before using it:

    python -m unittest discover -s tests

These tests use a small independently encoded JPEG 2000 OME-TIFF fixture, check its pyramid, and verify nonblank tiles at every zoom level and full-resolution pixel values. They cover both standard tag 34712 and native Bio-Formats tag 33004, 16-bit pixels, region-only segment reads, DEFLATE TIFFs, and decoder failures. No test-only dependencies are required. The Docker build runs the same tests and fails if decoding is incompatible.

TIFF reads use `fail_on="warning"` so unsupported compression raises an error instead of silently returning black pixels. The viewer displays metadata and tile-load failures. This stricter behavior also rejects damaged TIFFs that libvips would otherwise partially recover.

Other vendor compression variants and OME layouts still need representative-file testing.

## Run

1. Copy a pyramidal `.tif` or `.tiff` file into `scans/`.
2. From the `viewer/` directory run `uvicorn main:app --reload`.
3. Open <http://127.0.0.1:8000>.

The bottom-left metadata panel shows useful embedded OME-TIFF fields and, when present, scan, acquisition, specimen, and pixel-size summaries from `plan.json` and `ome-manifest.json` beside the image.

The sidebar's **Incoming** tab is a folder selector for manual OME-TIFF
conversion. It lists folders, checks for one TIFF plus `plan.json` and
`ome-manifest.json`, and allows selecting multiple completed scan folders.
It does not open incoming images or offer incoming image downloads.
Mount `incoming-scans` read-only at `/mnt/incoming-scans`, or set
`INCOMING_SCANS_DIR` to the actual mount path. An absent mount returns a clear
unavailable message. Only submit folders after the uploader finishes.

**Processing** now shows persisted OME conversion jobs, real stage names,
elapsed time, errors, manual retry, and an Open result button. It refreshes
status every three seconds while an authorized viewer page is visible.
Conversions continue when the browser tab closes. GPU inference is not part
of this change.

Configure backend environment variables:

- `OME_GENERATOR_URL=http://ome-generator` for the generator in the same
  Container Apps environment, using internal HTTP ingress on port 8000.
- `IMAGES_DIR`: a mount of the **same output share** the generator writes.
  Returned output paths are relative to that share; mount paths can differ.

The generator connection uses internal ingress without a shared API key. Keep
the generator ingress private to the Container Apps environment. The viewer
forwards only folder names and the authenticated operator identity; no image
bytes pass through the browser. Accounts with role `admin` or `processor` can
submit and track conversions. Guests can also submit, retry and view job
history for now: `ALLOW_GUEST_PROCESSING` defaults to `true`. Set it to `false`
in the viewer's Azure environment settings to disable guest processing without
editing code. The browser uses the server's permission response. Other viewer
accounts remain limited to image viewing, and guest viewing still works when
guest processing is disabled.

The generator must run the matching manual API version. For coordinated
cutover instructions, see [generator deployment](https://github.com/upstream/Azure-OME-tiff-generator/blob/main/docs/azure-deployment.md).
The old automatic watcher revisions must be deactivated during cutover.

The workspace launch configuration **OME-TIFF Viewer (Uvicorn)** starts the same command under the Python debugger, so breakpoints in `main.py` and `tile_service.py` work normally.

## Files

- `main.py` defines the API, validates scan names, returns HTTP errors, and serves `static/`.
- `tile_service.py` contains TIFF pyramid discovery, Deep Zoom coordinate mapping, region cropping, resizing, and JPEG encoding.
- `static/index.html`, `viewer.js`, and `styles.css` provide the dependency-free OpenSeadragon interface.
- `scans/` is the local scan directory.
- `requirements.txt` contains FastAPI, Uvicorn, pyvips, and the region-level JPEG 2000 TIFF decoder dependencies.

## Pyramid and OpenSeadragon level mapping

OpenSeadragon uses Deep Zoom level numbers. Its maximum level is the full-resolution image; every lower level is approximately half the width and height. These level numbers are API coordinates, not TIFF page numbers.

For every tile request, the backend computes that Deep Zoom level's dimensions, chooses the smallest stored TIFF level that is still at least that detailed, maps the tile rectangle into that stored level, crops only that region, and downsamples it to the requested tile size. Right and bottom edge tiles retain their smaller dimensions.

libvips exposes Bio-Formats-style pyramid layers through the TIFF loader's `subifd` option and commonly reports their count as `n-subifds`. The service checks those first. If none are found, it checks the top-level page chain used by classic pyramidal TIFFs. A page is accepted only when its dimensions decrease and its aspect ratio matches the base image, so equal-sized OME Z/C/T pages are not mistaken for zoom levels.

## Current limitations

- Guest Login currently bypasses credential checks and grants access to scans; do not expose it in production.
- This first version displays the first TIFF/OME image plane only; it does not yet select OME series, Z, C, or T dimensions.
- Some vendor TIFF layouts mix scenes, planes, associated images, and pyramid levels in ways libvips cannot identify unambiguously. Discovery therefore stops at the first dimension mismatch rather than guessing.
- Non-pyramidal TIFFs work, but low-zoom tiles must be downsampled from the full-resolution page and can be slower. Tiled pyramidal TIFFs give the intended region-read performance.
- 16-bit integer pixels are shifted to 8-bit for JPEG without whole-image intensity analysis. Fluorescence scans may therefore need explicit window/level controls in a later version.
- JPEG cannot preserve more than three channels or alpha; this proof of concept uses grayscale or the first three visible bands.

## Optional CycleGAN virtual H&E

Incoming offers conventional OME-TIFF export and, when the generator reports
the configured GPU worker and model available, **CycleGAN virtual H&E**. The
default checkpoint is shown with the processing choice. Inference creates a
separate OME-TIFF; Processing reports startup, model loading, block counts,
writing, validation and Open result. Retry keeps the inference operation and
model/input identity. Guest access uses the existing `ALLOW_GUEST_PROCESSING`
setting.

Deploy the corresponding generator change and complete its
[GPU/model setup](https://github.com/upstream/Azure-OME-tiff-generator/blob/main/docs/cyclegan-inference.md)
before using this option. Existing generator versions and unconfigured workers
continue to support conventional OME export.
