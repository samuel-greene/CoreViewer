# Minimal OME-TIFF viewer

A small FastAPI and OpenSeadragon proof of concept for browsing large pyramidal TIFF/OME-TIFF scans. Tiles are cropped lazily by libvips and returned as JPEG; the complete full-resolution image is not loaded into a Python array.

## Install

Python 3.10 or newer is recommended. `pyvips` is a Python binding and also needs the native **libvips** runtime:

- **Windows:** download a current `vips-dev-w64-all-*.zip` release from the [libvips releases](https://github.com/libvips/libvips/releases), extract it, and add its `bin` directory to `PATH`. Restart VS Code/your terminal afterward.
- **Ubuntu/Debian:** `sudo apt install libvips42` (use `libvips-dev` too only when a source build needs headers).
- **macOS:** `brew install vips`.

From this directory, create/activate a virtual environment and install the Python packages:

    python -m venv .venv
    .venv\Scripts\activate        # Windows PowerShell
    python -m pip install -r requirements.txt

On Linux/macOS, activation is `source .venv/bin/activate`.

## Run

1. Copy a pyramidal `.tif` or `.tiff` file into `scans/`.
2. From the `viewer/` directory run `uvicorn main:app --reload`.
3. Open <http://127.0.0.1:8000>.

The workspace launch configuration **OME-TIFF Viewer (Uvicorn)** starts the same command under the Python debugger, so breakpoints in `main.py` and `tile_service.py` work normally.

## Files

- `main.py` defines the API, validates scan names, returns HTTP errors, and serves `static/`.
- `tile_service.py` contains TIFF pyramid discovery, Deep Zoom coordinate mapping, region cropping, resizing, and JPEG encoding.
- `static/index.html`, `viewer.js`, and `styles.css` provide the dependency-free OpenSeadragon interface.
- `scans/` is the local scan directory.
- `requirements.txt` contains only FastAPI, Uvicorn, and pyvips.

## Pyramid and OpenSeadragon level mapping

OpenSeadragon uses Deep Zoom level numbers. Its maximum level is the full-resolution image; every lower level is approximately half the width and height. These level numbers are API coordinates, not TIFF page numbers.

For every tile request, the backend computes that Deep Zoom level's dimensions, chooses the smallest stored TIFF level that is still at least that detailed, maps the tile rectangle into that stored level, crops only that region, and downsamples it to the requested tile size. Right and bottom edge tiles retain their smaller dimensions.

libvips exposes Bio-Formats-style pyramid layers through the TIFF loader's `subifd` option and commonly reports their count as `n-subifds`. The service checks those first. If none are found, it checks the top-level page chain used by classic pyramidal TIFFs. A page is accepted only when its dimensions decrease and its aspect ratio matches the base image, so equal-sized OME Z/C/T pages are not mistaken for zoom levels.

## Current limitations

- This first version displays the first TIFF/OME image plane only; it does not yet select OME series, Z, C, or T dimensions.
- Some vendor TIFF layouts mix scenes, planes, associated images, and pyramid levels in ways libvips cannot identify unambiguously. Discovery therefore stops at the first dimension mismatch rather than guessing.
- Non-pyramidal TIFFs work, but low-zoom tiles must be downsampled from the full-resolution page and can be slower. Tiled pyramidal TIFFs give the intended region-read performance.
- 16-bit integer pixels are shifted to 8-bit for JPEG without whole-image intensity analysis. Fluorescence scans may therefore need explicit window/level controls in a later version.
- JPEG cannot preserve more than three channels or alpha; this proof of concept uses grayscale or the first three visible bands.
