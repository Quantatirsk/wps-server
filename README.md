# WPS API

`wps-api` is a headless PDF conversion service built on top of:

- `WPS Office for Linux`
- `pywpsrpc`
- `FastAPI`

It is intentionally narrow in scope. The service accepts supported Office
documents, asks WPS to export them as PDF, and returns either a single PDF or a
ZIP archive for batch requests.

For Chinese documentation, see [中文文档](docs/README.zh-CN.md).

## Status

The current deployment model is a single container with a local warm worker
pool:

- one FastAPI service process
- one local `spreadsheet` warm worker
- one local `presentation` warm worker
- multiple local `writer` warm workers for `doc` and `docx`

Workers run inside the same container and communicate through local process IPC.
The project no longer uses the old multi-container HTTP dispatch design.

## Features

- Convert `doc` and `docx` to PDF
- Convert `ppt` and `pptx` to PDF
- Convert `xls` and `xlsx` to PDF
- Convert a single file and stream a PDF response
- Convert multiple files and stream a ZIP response
- Reuse warm WPS application sessions to reduce cold-start overhead
- Prewarm writer, spreadsheet, and presentation sessions on startup
- Expose liveness and readiness endpoints
- Clean expired job directories on startup

## API

The API prefix is `/api/v1`.

### `GET /api/v1/healthz`

Simple process liveness probe.

Example response:

```json
{"ok": true}
```

### `GET /api/v1/readyz`

Runtime readiness probe. The endpoint verifies:

- the jobs directory is writable
- the runtime directory is writable
- `DISPLAY` is configured
- `XDG_RUNTIME_DIR` is configured
- `pywpsrpc` can be imported

Example response:

```json
{
  "ok": true,
  "checks": {
    "jobsDirWritable": true,
    "runtimeDirWritable": true,
    "displayConfigured": true,
    "xdgRuntimeDirConfigured": true,
    "pywpsrpcInstalled": true
  }
}
```

### `POST /api/v1/convert-to-pdf`

Upload one file as multipart form data with the field name `file`.

Example:

```bash
curl -X POST \
  -F "file=@./example.docx" \
  http://127.0.0.1:18000/api/v1/convert-to-pdf \
  --output output.pdf
```

### `POST /api/v1/convert-to-pdf/batch`

Upload multiple files as multipart form data with the field name `files`.
Each uploaded file is converted to PDF. The response is a ZIP archive containing
all generated PDFs and a manifest file.

Example:

```bash
curl -X POST \
  -F "files=@./a.docx" \
  -F "files=@./b.pptx" \
  -F "files=@./c.xlsx" \
  http://127.0.0.1:18000/api/v1/convert-to-pdf/batch \
  --output outputs.zip
```

### Interactive API Docs

Swagger UI is available at `/docs`.

## Supported Formats

- Writer: `.doc`, `.docx`
- Presentation: `.ppt`, `.pptx`
- Spreadsheet: `.xls`, `.xlsx`

## Architecture

### Request Flow

The request path is:

1. FastAPI receives a multipart upload.
2. The file is persisted under the workspace jobs directory.
3. `ConversionService` routes the file by document family.
4. `WarmSessionManager` dispatches the request to a local family worker.
5. The worker reuses or starts a warm WPS application session.
6. WPS opens the document and exports it as PDF.
7. The API streams back the PDF or ZIP response.
8. Temporary job files are cleaned up by background tasks.

### Warm Worker Model

- `writer` requests are distributed across a local worker pool.
- `spreadsheet` requests use one local warm worker.
- `presentation` requests use one local warm worker.
- Each worker processes its own document family serially.
- Concurrency comes from multiple local processes, not from multiple requests
  sharing one WPS application at the same time.

### Startup Behavior

On application startup the service:

1. configures logging
2. ensures workspace directories exist
3. creates the warm session manager
4. optionally prewarms all local workers
5. deletes expired job directories

If prewarm is enabled, the service becomes externally useful only after the
prewarm sequence completes.

## Worker Count Rules

`WPS_WORKER_COUNT` controls the number of local `writer` workers.

### Auto Mode

If `WPS_WORKER_COUNT` is empty or set to `auto`, the service detects physical
CPU core count and applies this formula:

- fewer than `8` cores: use the core count
- `8..16` cores: use `core_count - 2`
- more than `16` cores: use `16`

### Manual Mode

If `WPS_WORKER_COUNT` is an explicit integer, the current implementation clamps
it into `1..32`.

That means:

- auto mode is capped by the formula above
- manual mode still allows values above `16`, up to the current hard clamp of
  `32`

## Configuration

### Runtime Environment Variables

- `WPS_WORKSPACE_ROOT`
  - default: `/workspace`
  - root directory for job files and runtime files
- `WPS_CONVERSION_TIMEOUT_SECONDS`
  - default: `120`
  - timeout for one conversion request inside a local worker
- `WPS_CLEANUP_MAX_AGE_SECONDS`
  - default: `86400`
  - startup cleanup threshold for stale job directories
- `WPS_MAX_UPLOAD_SIZE_BYTES`
  - default: `52428800`
  - per-file upload size limit
- `WPS_BATCH_MAX_FILES`
  - default: `12`
  - maximum number of files accepted by the batch endpoint
- `WPS_WORKER_COUNT`
  - default: `auto`
  - local writer worker count, resolved by the rules described above
- `WPS_WARM_SESSION_MAX_JOBS`
  - default: `100`
  - recycle a local warm session after this many completed jobs
- `WPS_WARM_SESSION_PREWARM_ENABLED`
  - default: `true`
  - prewarm local workers during application startup

### Compose and Startup Variables

- `WPS_IMAGE`
  - default: `quantatrisk/wps-api:latest`
  - image used by `scripts/compose_up.sh`
- `WPS_API_PORT`
  - default: `18000`
  - host port published by `docker/docker-compose.yml`

## Build and Run

### Preferred Startup Path

The supported startup entrypoint is:

```bash
./scripts/build_image.sh
./scripts/compose_up.sh
```

Do not run `docker compose up` directly. The wrapper script is the supported
entrypoint because it resolves worker count and validates image availability
before starting the service.

### Build the Image

Interactive build:

```bash
./scripts/build_image.sh
```

Non-interactive build:

```bash
docker build -f docker/Dockerfile -t quantatrisk/wps-api:local .
```

Optional build arguments:

```bash
docker build \
  -f docker/Dockerfile \
  --build-arg WPS_DEB_URL_BASE=https://your-mirror.example.com/wps-office.deb \
  --build-arg FONTS_ZIP_URL=https://your-cdn.example.com/Fonts.zip \
  -t quantatrisk/wps-api:local .
```

### Start with Docker Compose

```bash
./scripts/compose_up.sh
```

Override selected values:

```bash
WPS_IMAGE=quantatrisk/wps-api:local \
WPS_API_PORT=18000 \
WPS_WORKER_COUNT=auto \
./scripts/compose_up.sh
```

Stop and clean up:

```bash
docker compose -f docker/docker-compose.yml down --remove-orphans
```

### Run Locally

If the host already has a usable Linux WPS runtime, you can run the API process
without Docker:

```bash
./scripts/run_local_api.sh
```

The local launcher sets:

- `WPS_WORKSPACE_ROOT`
- `DISPLAY`
- `QT_QPA_PLATFORM`
- `XDG_RUNTIME_DIR`

## Deployment Notes

### Remote Host

The expected remote deployment flow is:

```bash
git clone https://github.com/Quantatirsk/wps-api.git
cd wps-api
./scripts/build_image.sh
./scripts/compose_up.sh
```

There is no separate in-repo remote deployment script anymore. The repository
expects the target host to have:

- Docker
- the repository checkout
- a locally built image

If the image does not exist, `scripts/compose_up.sh` fails fast.

## Operational Notes

- Prewarm happens during startup.
- Batch requests are concurrent but not partially successful; one failed item
  causes the whole batch request to fail.
- Temporary files are stored under the workspace jobs directory.
- Single worker processes handle one document family serially.
- The service does not include authentication, a persistent queue, or retry
  orchestration.

## Project Layout

```text
.
├── app/
│   ├── adapters/        # WPS-family-specific automation adapters
│   ├── api/             # FastAPI routes
│   ├── runtime/         # Warm worker pool and worker process management
│   ├── services/        # Conversion orchestration
│   └── utils/           # Filesystem, logging, CPU detection, and errors
├── docker/
│   ├── conf/            # WPS and X11 config files
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── entrypoint.sh
├── docs/
│   └── README.zh-CN.md
├── scripts/
├── tests/
└── README.md
```

## Development Notes

- The application code lives outside the vendored `pywpsrpc/` directory.
- The repository currently includes sample Office files under `tests/files/`.
- The service is optimized for batch `docx -> pdf` throughput, with smaller
  amounts of spreadsheet and presentation traffic.
