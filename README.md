# CoreView Online Viewer

CoreView Online Viewer is a web workspace for browsing large microscopy scans and managing their conversion to OME-TIFF. It is built for high-resolution TIFF and OME-TIFF data that is impractical to load in full in a browser: the application reads only the requested image regions and serves them as viewable tiles.

The viewer combines a FastAPI service, libvips image processing, and OpenSeadragon navigation with an operator workflow for submitting completed acquisition folders, tracking conversion jobs, and opening results from one place.

## Product capabilities

- Browse TIFF and OME-TIFF files in a mounted scan library, including nested folders.
- Pan and zoom large pyramidal scans with on-demand JPEG tiles; no full-resolution browser download is required.
- Inspect embedded OME metadata and companion `plan.json` / `ome-manifest.json` summaries alongside each scan.
- Use temporary laser-pointer annotations, select a rectangular region, and export it as JPEG or TIFF.
- Download the original TIFF when needed.
- Submit completed TIFF or JP2 acquisition folders for OME-TIFF generation.
- Track queued, running, completed, and failed conversion jobs; retry failures and open completed results.
- Optionally generate a CycleGAN virtual H&E OME-TIFF when the generator reports a configured GPU worker and model.
- Provide controlled access to scan viewing and conversion workflows.
- Run locally or in Azure Container Apps with separate incoming and output Azure Files mounts.

## Architecture

```text
Browser (OpenSeadragon)
        │ application requests
        ▼
CoreView Online Viewer ──► mounted output scan library
        │                         │
        │ private job API          └─ on-demand TIFF region reads via libvips
        ▼
OME-TIFF generator ──────► mounted output scan library
        ▲
        │ selected folder names only
mounted incoming scan library
```

The browser does not upload image bytes for conversion. The viewer validates each selected folder against the incoming mount and sends only its path and the requesting identity to the private generator service.

## Quick start

### Prerequisites

- Python 3.10 or newer
- Native [libvips](https://www.libvips.org/) runtime

Install libvips for your platform:

- macOS: `brew install vips`
- Ubuntu/Debian: `sudo apt install libvips42`
- Windows: install the full `vips-dev-x64-all-*.zip` package from the [libvips Windows releases](https://github.com/libvips/build-win64-mxe/releases), then add its `bin` directory to `PATH`. The full package is needed for libvips-native JPEG 2000 TIFF variants.

From the repository root, create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux
source .venv/bin/activate

python -m pip install -r requirements.txt
```

Place pyramidal `.tif` or `.tiff` files in [`scans/`](scans/README.md), then start the service:

```bash
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>.

## Deployment configuration

The viewer discovers its output scan library in this order: `IMAGES_DIR`, `/mnt/images`, then the local `scans/` directory. In Azure, the viewer and generator must mount the same output share, although their mount paths may differ.

| Variable | Purpose | Local default |
| --- | --- | --- |
| `IMAGES_DIR` | Directory containing viewable TIFF/OME-TIFF output. | `scans/` when `/mnt/images` is absent |
| `INCOMING_SCANS_DIR` | Read-only directory containing acquisition folders for conversion. | `/mnt/incoming-scans` |
| `OME_GENERATOR_URL` | Internal base URL of the compatible OME-TIFF generator job API. | Not configured |

Keep the generator on private Container Apps ingress. The generator must expose the matching manual job API. See the [generator deployment guide](https://github.com/upstream/Azure-OME-tiff-generator/blob/main/docs/azure-deployment.md) for coordinated deployment details.

## Conversion workflow

1. Mount the incoming scan share at `INCOMING_SCANS_DIR`.
2. Upload a completed acquisition folder. It is eligible when it contains exactly one TIFF or JP2 file plus `plan.json` and `ome-manifest.json`.
3. In **Incoming**, select one or more eligible folders and choose either **Generate OME-TIFF** or, when available, **Generate virtual H&E**.
4. Follow the job in **Processing**. Jobs continue after the browser closes, and the page refreshes the job state while it is in use.
5. Use **Open result** after successful completion to open the generated scan from the output library.

The Incoming tab intentionally does not open or download source images. Submit folders only after uploads finish.

### Optional virtual H&E

The CycleGAN virtual H&E option is shown only when the generator reports an available GPU worker and model. It creates a separate OME-TIFF and preserves the requested operation and input identity when a job is retried. Conventional OME-TIFF export remains available without GPU inference.

Deploy the matching generator revision and complete its [GPU and model setup](https://github.com/upstream/Azure-OME-tiff-generator/blob/main/docs/cyclegan-inference.md) before enabling this workflow.

## TIFF handling and performance

CoreView maps each Deep Zoom request to the most appropriate native TIFF pyramid level, crops only the needed source region, resizes it to the requested tile, and returns JPEG to the viewer. It first detects libvips-exposed SubIFD pyramid layers, then falls back to a compatible top-level TIFF page chain. This avoids mistaking equal-sized OME Z/C/T pages for zoom levels.

Standard JPEG 2000 TIFF compression (tag 34712) is decoded region-by-region with `tifffile` and `imagecodecs`. Bio-Formats/Aperio-style JPEG 2000 TIFF tags supported by the installed libvips build are handled through libvips. Reads are strict: unsupported layouts or damaged segments return explicit errors rather than silent blank imagery.

Tiled pyramidal TIFFs provide the best experience. Non-pyramidal images are still viewable but may be slower at low zoom because they must be downsampled from the full-resolution page.

## Deployment notes

Keep the incoming share read-only to the viewer and the generator ingress private. Apply your organization’s standard access controls and secret-management practices to the deployed service and its storage mounts.

## Supported formats and current boundaries

- TIFF and OME-TIFF files with 8- or 16-bit grayscale or RGB data and top-left orientation are supported by the JPEG 2000 region decoder.
- The viewer displays the first TIFF/OME image plane. Series and OME Z/C/T selection are not yet available.
- Some vendor files mix scenes, planes, associated images, and pyramid levels in ways that cannot be identified reliably. Pyramid discovery stops on a dimension mismatch rather than guessing.
- 16-bit data is converted to 8-bit for JPEG tiles without whole-image intensity analysis. Fluorescence workflows may need future window/level controls.
- JPEG exports preserve grayscale or the first three visible bands; alpha and more than three channels are not preserved.
- Standalone `.jp2` files are not viewed directly; they may be submitted in completed incoming folders for conversion. Other vendor-specific compression layouts require representative-file validation.

## Verification and container image

Run the test suite with:

```bash
python3 -m unittest discover -s tests
```

The test suite covers incoming-folder validation, JP2 intake, conversion-job proxying, inference submission and retry identity, TIFF pyramid discovery, JPEG 2000 and DEFLATE decoding, region-only reads, 16-bit tiles, metadata summaries, and decoder error propagation. The `Dockerfile` installs libvips and runs these tests during image build, so a codec-incompatible build fails before deployment.

Build and run the container locally:

```bash
docker build -t coreview-online-viewer .
docker run --rm -p 8000:8000 -v "$(pwd)/scans:/mnt/images:ro" coreview-online-viewer
```

## Repository guide

| Path | Responsibility |
| --- | --- |
| [`main.py`](main.py) | FastAPI application, scan and processing APIs, metadata summaries, and export endpoints. |
| [`tile_service.py`](tile_service.py) | TIFF pyramid discovery, Deep Zoom mapping, region reads, rendering, annotations, and crop encoding. |
| [`static/`](static/) | Browser interface, scan browser, processing UI, and OpenSeadragon integration. |
| [`scans/`](scans/README.md) | Local development scan library; large scan files should not be committed. |
| [`tests/`](tests/) | Processing and codec regression coverage. |
| [`scripts/`](scripts/) | Imaging-support utilities. |
