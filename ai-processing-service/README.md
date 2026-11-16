# AI Processing Service

StudyHub's asynchronous document processing worker consumes `document.uploaded`
events, downloads a specific document version from Content, extracts or transcribes
text, and generates a single-document summary with key facts and chunk embeddings.
It reports the terminal processing status to Content and publishes derived results
for Search. The worker has no HTTP API; the native transcription bridge has its own API.

## Service boundaries

- Content owns original files, document metadata, versions, access and visible status.
- AI Processing owns extraction, transcription, summaries, embeddings and job recovery.
- Search owns vector storage, indexing, retrieval and live permission checks.
- Synthesis owns cross-document summaries. Its model settings remain independent.

Search integration is still pending. The proposed result event below is not a claim
that the full four-service workflow is implemented. Synthesis currently supports
explicit mock fixtures; its real Search document-provider contract is not connected.

## Configuration

Use the repository's setup script to initialize the root `.env` and service tokens.
For an existing `.env`, copy missing AI Processing settings from `.env.example`.
Do not overwrite existing credentials or replace Synthesis settings with Processing settings.

| Setting | Purpose |
| --- | --- |
| `STUDYHUB_DATABASE_URL` | Host PostgreSQL URL; default development port is 5433 |
| `STUDYHUB_AMQP_URL` | Host RabbitMQ URL; must match `RABBITMQ_HOST_PORT` |
| `STUDYHUB_PROCESSING_TOKEN` | Same Processing bearer token configured in Content |
| `AI_CONTENT_URL` | Host Content URL, normally `http://127.0.0.1:8000` |
| `AI_MODEL_PROVIDER` | `ollama` for native endpoints, or `openai` for compatible API formats |
| `AI_SUMMARY_URL`, `AI_SUMMARY_MODEL`, `AI_SUMMARY_KEY` | Summary endpoint, model and optional key |
| `AI_EMBEDDING_URL`, `AI_EMBEDDING_MODEL`, `AI_EMBEDDING_KEY` | Embedding endpoint, model and optional key |
| `AI_TRANSCRIPTION_URL`, `AI_TRANSCRIPTION_MODEL`, `AI_TRANSCRIPTION_KEY` | Multipart transcription endpoint, allowed model and optional key |
| `AI_HTTP_TIMEOUT` | HTTP operation timeout in seconds; default 300 |
| `AI_CHUNK_SIZE`, `AI_CHUNK_OVERLAP` | Character counts, not token counts; defaults 2000 and 200 |
| `AI_MAX_ATTEMPTS` | Processing attempts per document version; default 3 |
| `AI_OLLAMA_CONTEXT` | Local summary context size; default 4096 tokens |
| `AI_SUMMARY_MAX_TOKENS` | Summary output limit; default 512 tokens |

The default models are `qwen3:4b`, `bge-m3` and
`mlx-community/whisper-small-mlx`. Summary and embedding tasks share the provider
setting. Transcription always sends multipart form data and expects a `text` field.
For compatible third-party APIs, change the provider, full endpoint URLs, models and
keys together. Synthesis uses its separate `AI_MODEL` and `OLLAMA_BASE_URL` settings.

## Run on the host

Reuse your existing Conda environment if the dependencies are already installed:

```bash
conda activate comp41720-groupAS
cd ai-processing-service
pip install -r requirements.txt
python main.py --check-config
python main.py
```

Alternatively, create a Python virtual environment and install the same requirements.
`--check-config` validates settings without connecting to external services.
The worker exits before consuming messages when required settings are missing.
Start Content, PostgreSQL, RabbitMQ and the required model services before the worker.

## Run the worker with Docker Compose

The root Compose file includes `ai-processing-worker` behind the optional
`processing` profile. Its image contains the Python worker and migrations;
Ollama and MLX Whisper run natively on the Mac.

From the repository root, start the native model services, then run:

```bash
docker compose --profile processing up --build -d ai-processing-worker
docker compose logs -f ai-processing-worker
```

Compose starts the worker's Content, database and broker dependencies. To start
all services, including Synthesis, use `docker compose --profile processing up --build -d`.
Default `docker compose up` leaves the optional Processing worker disabled.
Do not run a host worker and a container worker unintentionally at the same time.

Compose injects environment variables explicitly and supplies container addresses:

| Destination | Container address |
| --- | --- |
| PostgreSQL | `db:5432` |
| RabbitMQ | `rabbitmq:5672` |
| Content | `http://content-api:8000` |
| Summary | `http://host.docker.internal:11434/api/chat` |
| Embeddings | `http://host.docker.internal:11434/api/embed` |
| Transcription | `http://host.docker.internal:8001/v1/audio/transcriptions` |

Override model URLs for the container using `AI_DOCKER_SUMMARY_URL`,
`AI_DOCKER_EMBEDDING_URL` and `AI_DOCKER_TRANSCRIPTION_URL`. Host URLs remain
separate, so switching between host and container execution does not require editing them.
`host.docker.internal` assumes Docker Desktop on macOS; other hosts may need networking changes.

If a local RabbitMQ already occupies port 5672, set `RABBITMQ_HOST_PORT=5673` and
`RABBITMQ_MANAGEMENT_PORT=15673` in `.env`. Ensure the host AMQP URL uses that port.
Container-to-container connections continue to use `rabbitmq:5672`.

## Native Mac model services

Open Ollama with `qwen3:4b` and `bge-m3` downloaded. Native Ollama APIs disable
thinking and streaming for summaries, reject output-limit truncation, and disable
silent embedding input truncation. Each processing phase requests model unloading
in a `finally` block. A failed unload falls back to Ollama's keep-alive expiry.
Long summaries are recursively reduced within a conservative character budget;
this is not an exact tokenizer-based context check.

Run the transcription bridge in a native Apple Silicon Python environment:

```bash
# From ai-processing-service/, reuse an existing compatible environment.
pip install -r native/requirements.txt
# Install ffmpeg with Homebrew if it is not already available.
python native/transcription_server.py
```

The bridge defaults to `127.0.0.1:8001`. `GET /health` checks HTTP liveness without
loading the model. `POST /v1/audio/transcriptions` accepts `file` and `model` fields.
Only the configured model is allowed. Each transcription runs in a child process,
which releases model memory on exit. Requests are serialized; a busy bridge returns
429. Audio is limited to 5 MiB and temporary files are removed after processing.
The model is downloaded on first use if it is not cached.

For Docker access, if loopback binding is unreachable, set
`AI_TRANSCRIPTION_BIND=0.0.0.0` and a nonempty `AI_TRANSCRIPTION_KEY` in the root `.env`.
Restart the bridge and recreate the worker so both use the same key. The bridge
refuses non-loopback binding without a key. Restrict access to the host as appropriate.
Ollama may also need its host listener configured for Docker access.

## Supported inputs and processing limits

Supported documents are UTF-8 TXT, MD and CSV, text-based PDF, and DOCX, including
DOCX table text. Supported audio extensions are MP3, WAV, M4A, MP4, OGG, WEBM and FLAC.
Scanned PDFs require OCR, which is not implemented. Legacy DOC is unsupported.
Original files are limited to 5 MiB and extracted text to 200000 characters.
Summaries and key facts currently share one text field. Embeddings are generated
from original text chunks, with character offsets retained in each chunk.

## Persistence and recovery

The worker creates its own `ai_processing.jobs` table at startup. Development reuses
the Content PostgreSQL instance and database through a separate schema; the worker
does not access Content business tables. Separate database credentials and ownership
should be configured for an independently deployed service. The current migration
is idempotent initialization SQL; future schema changes need a migration mechanism.

Jobs are deduplicated by `(documentId, documentVersion)`. PostgreSQL session advisory
locks prevent concurrent processing of the same version. Results are persisted
before the Content callback and confirmed RabbitMQ publication, then the upload
message is acknowledged. Callback or publication failures retain staged results,
so redelivery does not repeat successful model work.

A crash after publication but before saving completion can publish the result again.
The result `eventId` remains stable and Search must consume it idempotently.
Processing failures consume the configured attempt budget and eventually report
`failed`. Callback and publication failures retry after at least three seconds,
without consuming additional model-processing attempts. Callback 409 responses are
retried for operator investigation rather than overwriting a terminal status.
Content 404 responses skip the job and clear staged results. Invalid upload events
are moved to `processing.invalid-events`. Broker heartbeats continue during model calls.

There are no per-chunk checkpoints: a crash during processing may repeat model calls.
Completed jobs clear staged output; Search is expected to retain delivered results.
Reprocessing APIs, deletion-event consumption, monitoring and OCR are not implemented.

## Proposed Search result contract

Results use durable topic exchange `processing.events`, routing key
`document.processed` and durable queue `search.processing-results`.
The team must agree this contract before integrating Search.

```json
{
  "schemaVersion": 1,
  "eventId": "1fc17bd4-9a02-4d10-a168-562bd3fd1b24",
  "eventType": "document.processed",
  "documentId": "04a1dc78-a5d7-428a-8773-3ba9ec503c22",
  "documentVersion": 1,
  "summary": "Summary and key facts",
  "embeddingModel": "bge-m3",
  "summaryModel": "qwen3:4b",
  "chunks": [
    {"chunkIndex": 0, "startOffset": 0, "endOffset": 4,
     "text": "Text", "embedding": [0.1, 0.2]}
  ]
}
```

The vector is shortened for illustration; BGE-M3 currently returns 1024 dimensions.
Search owns vector storage, stale-version handling and deletion ordering, and must
check live Content permissions before returning results. Content `succeeded` means
AI output has been durably staged, not that Search indexing is complete. Search must
prevent late processing results from reactivating deleted documents. Large payloads
may later be replaced with references to stored processing artifacts.

## Verification

From `ai-processing-service/`:

```bash
python -m unittest discover -s tests -v
```

Tests use fake models, Content responses and an in-memory job store. They cover
redelivery, delivery recovery, failure limits, deletion, chunk offsets, provider
formats and pipeline behavior. Native HTTP bridge tests require its Web dependencies
and do not load MLX. Unit tests do not establish real model, database or broker connectivity.
