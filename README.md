# CoreViewer

A web viewer for large TIFF and OME-TIFF microscopy images, built with **FastAPI, libvips, OpenSeadragon, and SQLAlchemy**. The server reads requested image regions and returns Deep Zoom tiles, allowing the browser to navigate images without downloading the full-resolution scan.

This public portfolio edition includes the application, regression tests, optional event infrastructure, and sanitized development history. It contains no research scans, catalog databases, deployed-service credentials, or production runbooks. A procedural demo makes the viewer usable without private data.

## Features

- Pan and zoom tiled, pyramidal TIFF and OME-TIFF images.
- Decode JPEG 2000 and DEFLATE TIFF variants, including supported 16-bit images.
- Inspect embedded OME metadata and optional JSON sidecars.
- Select a region and export JPEG or TIFF; download the original image.
- Search an optional SQLite/PostgreSQL catalog, curate metadata, and track review status.
- Browse incoming acquisition folders and track conversion jobs through a separately configured generator API.
- Index completed images through an optional Azure Service Bus consumer with duplicate-safe updates and transient-failure retries.
- Use signed sessions, user roles, and a guest demo workflow.

## Architecture

```text
Browser / OpenSeadragon
        |
        v
FastAPI viewer -----------> SQLAlchemy catalog (optional)
        |                          ^
        v                          |
libvips / TIFF codecs       separate catalog worker
        |                          ^
        v                          |
mounted image library ------+------+

Optional integrations:
incoming folders -> external conversion API -> image library
finalized-image events -> Service Bus -> catalog worker
```

Image bytes stay on the filesystem or mounted share. Catalog records hold extracted metadata, curation, provenance, and review history. The viewer works locally without Azure, PostgreSQL, or a conversion service.

## Run the synthetic demo

Use Python 3.10+ and a native libvips runtime. On macOS, install libvips with `brew install vips`; on Ubuntu/Debian use `sudo apt install libvips-dev`. Windows users can install a full [libvips runtime](https://github.com/libvips/build-win64-mxe/releases) and add its `bin` directory to `PATH`.

```bash
python3 -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts/create_demo_scan.py
uvicorn main:app --reload --host 127.0.0.1
```

Open <http://127.0.0.1:8000> and choose **Continue as Guest**. The generated `synthetic-demo.tif` is a tiled pyramid made entirely from mathematical patterns; it contains no acquired image or specimen data. The generator refuses to overwrite an existing file. Generated files in `scans/` are ignored by Git.

For an administrator account, set `ADMIN_EMAIL` and `ADMIN_PASSWORD` in your shell before starting the server. Generate a session key with `python -c "import secrets; print(secrets.token_hex(32))"` and set `SECRET_KEY` to the result. Alternatively, generate multi-user entries with `python scripts/make_user.py` and supply `USERS_JSON`.

The unconfigured local application has a development-only administrator (`admin@example.com` / `change-me!`) and an ephemeral session key. Guest sessions can browse mounted scans. Use synthetic data for public demonstrations; configure authentication and access controls before attaching private datasets or exposing a service. Publishing this source repository does not deploy the application.

## Configuration

[`.env.example`](.env.example) lists supported settings using placeholders. The application reads process environment variables; it does **not** automatically load `.env`. If using an environment-file loader, keep the real file out of Git.

| Variable | Purpose | Default |
| --- | --- | --- |
| `IMAGES_DIR` | Viewable image library | `/mnt/images` if present, otherwise `scans/` |
| `INCOMING_SCANS_DIR` | Incoming acquisition folders | `/mnt/incoming-scans` |
| `SECRET_KEY` | Session signing key | Random per process |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Single administrator account | Local development values above |
| `USERS_JSON` | JSON array of users with password hashes and roles | Unset |
| `HTTPS_ONLY` | Require HTTPS for session cookies | `false` |
| `ALLOW_GUEST_PROCESSING` | Allow guest conversion submissions | `true`; sample configuration uses `false` |
| `OME_GENERATOR_URL` | Compatible external conversion job API | Unset |
| `DATABASE_URL` | Optional SQLAlchemy catalog connection | Unset |
| `CATALOG_DEFAULT` | Use catalog search as the default interface | `false` |
| `CATALOG_LEGACY_BROWSER` | Retain administrator folder browsing after catalog rollout | `true` |
| `CATALOG_SCAN_INTERVAL` | Polling worker interval in seconds | `180` |

Conversion and optional virtual H&E controls require a compatible external generator; that service and its models are not bundled here. TIFF viewing, metadata, navigation, and exports work independently.

## Optional catalog

Set `DATABASE_URL=sqlite:///./catalog-local.db` in your environment for local development, then initialize and import:

```bash
python -m scripts.catalog_worker init
python -m scripts.catalog_worker once
python -m scripts.catalog_worker validate
```

The polling importer waits for files to settle for 60 seconds. Run the import after that interval, or use `python -m scripts.catalog_worker watch`. Set `CATALOG_DEFAULT=true` before starting the viewer to make the catalog the default interface. The database is local runtime data and is ignored by Git.

PostgreSQL is supported through SQLAlchemy/psycopg. Azure PostgreSQL connections require `sslmode=verify-full` and a readable CA bundle. The worker must use the same image library and database as the viewer. See [event integration](docs/event-pipeline.md) for the event-driven importer.

## Tests and containers

```bash
python -m unittest discover -s tests -v
docker build -t coreviewer .
docker run --rm -p 127.0.0.1:8000:8000 -v "$(pwd)/scans:/mnt/images:ro" coreviewer
```

Tests generate temporary images and databases. Coverage includes authentication, catalog curation, event replay and retries, incoming-folder validation, conversion proxy behavior, TIFF pyramid selection, region reads, JPEG 2000/DEFLATE decoding, and exports. The container build runs the suite against its native codec runtime. GitHub Actions runs tests only; this edition has no automatic cloud deployment workflow.

## Repository guide

| Path | Responsibility |
| --- | --- |
| `main.py` | FastAPI routes, authentication, processing proxy, metadata, and exports |
| `tile_service.py` | Pyramid discovery, region decoding, tiles, and crops |
| `catalog.py` | Persistence, indexing, search, curation, and reconciliation |
| `static/` | Browser interface and OpenSeadragon integration |
| `scripts/catalog_worker.py` | Catalog setup, import, validation, and receiver commands |
| `scripts/create_demo_scan.py` | Procedural synthetic image generator |
| `infra/pipeline-events.bicep` | Parameterized optional broker infrastructure |
| `tests/` | Application and codec regression suite |

## Boundaries

The viewer displays the first TIFF/OME image plane; series and OME Z/C/T selection are not implemented. JPEG tiles convert 16-bit data to 8-bit without interactive window/level controls. JPEG exports use grayscale or the first three visible bands. Standalone JP2 files are conversion inputs rather than directly viewable scans. Unsupported or damaged TIFF layouts return errors.

## Development history

The commit graph preserves branch development, author names, dates, and PR merge relationships. Private content was removed throughout history, so commit hashes have changed and original commit signatures are not retained. Personal email addresses were replaced. [Development history](docs/HISTORY.md) records retained PR merge evidence and the limits of what Git can preserve; [public export notes](docs/PUBLIC_EXPORT.md) describe the sanitation scope.
