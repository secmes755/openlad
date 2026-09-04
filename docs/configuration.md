# Configuration Reference

[English](configuration.md) | [中文](zh-CN/configuration.md)

All settings are environment variables with the `OPENLAD_*` prefix. There are
three ways to supply them, depending on how you run OpenLAD:

- **Source install (`./start.sh`)** — the API process reads `.env` from the
  repository root on startup (via python-dotenv). **Caveat:** `start.sh`
  exports its own defaults for the endpoint/model-name variables listed under
  "Model backends" below, which shadow `.env`. For those variables, either
  export them before launching or load `.env` into the shell first:
  `set -a; . ./.env; set +a`. Variables that `start.sh` does not touch (OCR,
  security, ingestion tuning, …) are picked up from `.env` normally.
- **Docker** — the compose file loads `.env` (next to `docker-compose.yml`)
  via `env_file`; see `docker/.env.example`.
- **Admin panel** — model endpoints can also be set at runtime under
  admin panel → Model Services.

---

## Model backends

| Variable            | Default                    | Description                              |
| ------------------- | -------------------------- | ---------------------------------------- |
| `OPENLAD_LLM_URL`   | `http://localhost:8080/v1` | LLM API endpoint                         |
| `OPENLAD_LLM_MODEL` | *(required — no default)*  | Model name registered in the LLM backend |
| `OPENLAD_EMB_URL`   | `http://localhost:8081/v1` | Embedding API endpoint                   |
| `OPENLAD_EMB_MODEL` | *(required — no default)*  | Embedding model name                     |

## OCR endpoint (optional)

| Variable            | Default | Description                                   |
| ------------------- | ------- | --------------------------------------------- |
| `OPENLAD_OCR_URL`   | *(unset)* | OpenAI-compatible vision endpoint for page OCR |
| `OPENLAD_OCR_MODEL` | *(unset)* | Vision model name (e.g. `ovisocr2`)            |

When unset, image-only pages fall back to Tesseract (if installed), and pages
that cannot be transcribed mark the document `degraded` rather than silently
dropping content.

## Security & authentication

| Variable                     | Default | Description                                    |
| ---------------------------- | ------- | ---------------------------------------------- |
| `OPENLAD_ADMIN_PASSWORD`     | *(unset)* | Required on first startup to create the admin user; changing it later does not reset an existing password |
| `OPENLAD_LOGIN_USER_PER_MIN` | `5`     | Login attempts per username per minute (→ 429) |
| `OPENLAD_LOGIN_IP_PER_MIN`   | `20`    | Login attempts per client IP per minute (→ 429)|
| `OPENLAD_API_KEY_TTL_DAYS`   | `90`    | Default API key validity in days (`0` = never) |

Login is rate-limited on both the username and IP axes without locking
accounts. API keys expire after the TTL and can be rotated anytime from the
admin user-management panel, or via
`POST /api/v1/admin/users/{id}/regenerate-key`.

## Embedding batch-size pairing

The embedding server's `--batch-size` caps the tokens of any *single* input —
a chunk larger than that is rejected outright and skipped during ingestion.
OpenLAD derives its chunk/truncation limits from
`OPENLAD_EMB_MAX_INPUT_TOKENS` (default `2048`, matching llama.cpp's own
default). If you run the embedding server with a non-default `--batch-size`
(e.g. `512` on a small GPU), set the same value:

```bash
OPENLAD_EMB_MAX_INPUT_TOKENS=512
```

A mismatch does not crash — affected chunks are silently skipped and the
document ingests "hollow" (present in the library, but large parts of its
text never got embedded). If you suspect this, re-ingest the affected
documents after aligning both values.

## Retrieval & ingestion tuning

| Variable                       | Default | Description                                |
| ------------------------------ | ------- | ------------------------------------------ |
| `OPENLAD_MAX_CHARS`            | *(unset — auto budget)* | Overrides the phase-2 retrieval context budget (chars). Lower it if queries hit "context size exceeded" |
| `OPENLAD_EMB_MAX_INPUT_TOKENS` | `2048`  | Must match the embedding server's `--batch-size` — see pairing rule above |
| `OPENLAD_INGEST_MAX_WORKERS`   | `1`     | Parallel ingestion workers — set to match your LLM server's `--parallel` (safe default `1`; e.g. `--parallel 2` → `2`) |
| `OPENLAD_CHART_VLM_MAX_WORKERS`| `1`     | Concurrent chart/VLM calls during ingestion — same pairing rule |
| `OPENLAD_DATA_DIR`             | `./data` | Document/database storage root             |
| `OPENLAD_INDUSTRIES_DIRS`      | *(unset)* | Extra directories to scan for industry packs (`;`-separated on Windows, `:`-separated on Linux) |

## Query concurrency

| Variable                        | Default  | Description                          |
| ------------------------------- | -------- | ------------------------------------ |
| `OPENLAD_QUERY_CONCURRENCY_MODE`| `auto`   | `auto` / `serial` (force one at a time) / `parallel`. `start.sh` pins `serial` |
| `OPENLAD_QUERY_MAX_CONCURRENT`  | `0`      | `0` = default policy; `>0` forces an explicit cap |
| `OPENLAD_LLM_NP`                | `1`      | Concurrent slots for LLM API calls — must match llama-server `--parallel` |

Keep these aligned with your backend's actual parallel capacity — a mismatch
either queues queries unnecessarily or overwhelms the LLM server.
