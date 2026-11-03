# CoreView Online Viewer

CoreView Online Viewer is a web workspace for browsing large microscopy scans and managing their conversion to OME-TIFF. It is built for high-resolution TIFF and OME-TIFF data that is impractical to load in full in a browser: the application reads only the requested image regions and serves them as viewable tiles.

The viewer combines a FastAPI service, libvips image processing, and OpenSeadragon navigation with an operator workflow for submitting completed acquisition folders, tracking conversion jobs, and opening results from one place.

## Product capabilities

- Browse TIFF and OME-TIFF files in a mounted scan library, including nested folders.
- Search a PostgreSQL-backed image catalog by filename, specimen ID, plan ID, administrator tags, and TIFF/companion metadata keywords (for example, Brightfield, 20x, or a model name); filter by UTC date, uploader, and verification status.
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
        │
        ▼
CoreView Online Viewer ──► PostgreSQL catalog
        │                       ▲
        │ on-demand tiles       │ indexed metadata
        ▼                       │
mounted output scan library ─► catalog worker
        ▲
        │ generated images
OME-TIFF generator ◄── Viewer job requests ◄── mounted incoming scan library
```

The browser does not upload image bytes for conversion. The viewer validates each selected folder against the incoming mount and sends only its path and the requesting identity to the private generator service.

The catalog stores paths, extracted metadata, administrator corrections, tags, provenance, and review history. Image bytes stay on Azure Files. Search results are text rows; no previews or thumbnails are stored or displayed. The existing viewer still renders tiles on demand after an image is opened.

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
| `DATABASE_URL` | SQLAlchemy URL for the catalog. Azure PostgreSQL connections require verified TLS and a readable CA bundle. | Not configured; folder browser remains available |
| `CATALOG_DEFAULT` | Make catalog search the default library interface after backfill validation. | `false` |
| `CATALOG_LEGACY_BROWSER` | Keep an admin-only Folders tab during catalog rollout; set `false` to retire it. | `true` |
| `CATALOG_SCAN_INTERVAL` | Seconds between completed-image share scans in the separate worker. | `180` |

Keep the generator on private Container Apps ingress. The generator must expose the matching manual job API. See the [generator deployment guide](https://github.com/upstream/Azure-OME-tiff-generator/blob/main/docs/azure-deployment.md) for coordinated deployment details.

## Catalog setup and rollout

The catalog is optional until it is initialized. For local development, set `DATABASE_URL=sqlite:///./catalog-local.db`. Production uses Azure Database for PostgreSQL Flexible Server; keep the database URL in a secret and use a private connection or your organization's approved network controls. Give the worker and viewer the same output-share mount, but run **one** worker process independently of the web replicas.

Run these commands in the application image with `DATABASE_URL` and `IMAGES_DIR` set:

```bash
python -m scripts.catalog_worker init
python -m scripts.catalog_worker once
python -m scripts.catalog_worker validate
python -m scripts.catalog_worker status
```

`validate` compares currently present TIFF paths with non-missing catalog records and reports samples of discrepancies and metadata errors. Resolve discrepancies and inspect representative image records before setting `CATALOG_DEFAULT=true`. While it is false, administrators can inspect the Library tab and all users retain the folder browser. After the switch, ordinary users enter Library; administrators retain Folders as a rollout fallback. Set `CATALOG_LEGACY_BROWSER=false` after the transition to retire folder navigation. A database outage produces an explicit catalog error, not an empty result set.

After initial validation, run one long-lived worker using the same container image with the command `python -m scripts.catalog_worker watch`. It rescans the completed-image share every three minutes by default. New files settle for 60 seconds before indexing. Failed directory walks do not mark files missing; a file is marked missing only after two complete scans at least 24 hours apart. An empty share with existing catalog records is treated as a scan failure. A confidently recognized rename retains the record; uncertain replacements receive a new unverified record. Incoming acquisition folders are not indexed.

The search index includes bounded text from OME-TIFF headers and scalar fields in `plan.json` and `ome-manifest.json`, including nested fields such as `model_name`; large per-tile arrays are skipped. Space-separated search terms can match across fields. After deploying an updated catalog worker, its next scan refreshes older records without changing their review status. A model name is searchable only if it is present in the TIFF or companion metadata; the generator's job-status response alone is not indexed.

Every indexed image starts unverified. Administrators can correct specimen ID, plan ID, acquisition time, and free-form tags without modifying the TIFF or companion files. Original extracted fields and edit history remain in the record. Material source changes or corrections move a verified record to needs review. Authenticated generator submissions can identify an uploader only when a recorded job ID maps to a completed output path; guest or ambiguous jobs remain unknown. Ownership is nullable and not enforced in this release. All existing accounts, including guest, can search the same catalog. Before enabling private ownership in a future release, enforce it across search, info, tiles, downloads, crops, and legacy browsing routes.

## Conversion workflow

### Event-driven acquisition pipeline

The coordinated migration adds `CoreView.FileUpload` notifications from Acquisition client and
`CoreView.OmeGenerated` notifications from the generator. Event Grid routes each
event into a durable Service Bus queue. The generator automatically submits completed
uploads for OME generation, and the separate catalog worker indexes each finalized
output immediately using `python -m scripts.catalog_worker events`. The viewer
continues to read the output share and PostgreSQL catalog.

This path removes incoming-folder discovery, the upload-settle delay for completed
event submissions, and recurring catalog share scans. The existing on-demand
processing job retains its startup time. Acquisition client can eventually generate OME locally
and publish the same finalized-output event directly to the catalog worker.

See the [event contract and coordinated cutover](docs/event-pipeline.md). The new
code is opt-in until its broker settings are configured and the worker command is
changed; this repository update alone does not switch the deployed pipeline.

### Manual submissions

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
