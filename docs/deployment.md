# Deployment & Hardware

[English](deployment.md) | [中文](zh-CN/deployment.md)

This guide covers everything beyond the minimal quickstart: hardware sizing,
model backend options, recommended llama-server flags, and Docker details.

---

## Hardware Probe

Run the built-in probe to detect your GPU VRAM / system memory and get a
recommended configuration:

```bash
python -m core.services.system_probe
```

### Quick configuration lookup

| GPU VRAM | Model | Weight quant | KV cache | LLM context | Expected capability |
|---|---|---|---|---|---|
| ≥ 24 GB | 9B | Q5_K_M | Q4 | 262144 | Full capability |
| 16 GB **(recommended)** | 9B | Q5_K_M | Q4 | 131072 | Full capability |
| 12 GB | 9B | Q5_K_M | Q4 | 65536 | Usable; large documents context-limited |
| 8 GB | 4B | Q4_K_M | Q4 | 32768 | Limited (see note below) |
| CPU-only | 9B / 4B | Q4_K_M | Q8 | 16384 – 65536 | Functional but slow; evaluation only |

**Minimum usable LLM context: 16384 tokens** — below that, whole chapters
cannot fit in the context and retrieval quality collapses. Set the
recommended values in your start script, e.g. `LLM_CTX_SIZE=131072` with
`--cache-type-k q4_0 --cache-type-v q4_0` on llama-server.

> **Note on 8 GB VRAM**: theoretically usable with the 4B model, but the
> small model's capability limits surface in practice — long-document
> handling can be unstable and complex questions are understood less
> reliably. **16 GB VRAM with the 9B model is strongly recommended** for a
> complete and stable experience.

---

## Model Backends

OpenLAD talks to model services over the OpenAI-compatible HTTP API. The
service itself is CPU-only; you bring the inference engines.

### llama.cpp (recommended)

Build and install:

```bash
git clone https://github.com/ggerganov/llama.cpp.git /tmp/llama.cpp
cd /tmp/llama.cpp && cmake -B build && cmake --build build -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/
```

Download the reference models (HuggingFace):

```bash
mkdir -p ~/models
huggingface-cli download Qwen/Qwen3.5-9B-GGUF qwen3.5-9b-q5_k_m.gguf \
    --local-dir ~/models
huggingface-cli download Qwen/Qwen3-Embedding-0.6B-GGUF \
    qwen3-embedding-0.6b-q8_0.gguf --local-dir ~/models
```

### Recommended llama-server flags

**LLM — Qwen3.5-9B Q5_K_M (16 GB VRAM recommended):**

| Flag             | Value    | Notes                                                    |
| ---------------- | -------- | -------------------------------------------------------- |
| `--n-gpu-layers` | `999`    | Offload all layers to GPU. Reduce to `35` for 8 GB VRAM. |
| `--ctx-size`     | `262144` | 256K context window. Reduce to `65536` if OOM.           |
| `--parallel`     | `2`      | Concurrent request slots                                 |
| `--batch-size`   | `2048`   | Prompt processing batch size                             |
| `--reasoning`    | `off`    | **CRITICAL** — Qwen3.5 thinking mode must be disabled    |
| `--cache-type-k` | `q4_0`   | Q4 KV cache quantization (~2.3 GB at 256K)               |
| `--cache-type-v` | `q4_0`   | Q4 value cache quantization                              |
| `-n`             | `-1`     | No limit on generated tokens                             |

**Embedding — Qwen3-Embedding-0.6B Q8_0:**

| Flag             | Value  | Notes                                             |
| ---------------- | ------ | ------------------------------------------------- |
| `--n-gpu-layers` | `999`  | All layers to GPU (~0.6 GB total)                 |
| `--ctx-size`     | `8192` | 8K context (sufficient for page-level embeddings) |
| `--embeddings`   | —      | Enable embedding mode                             |
| `--pooling`      | `mean` | Mean pooling for embedding vectors                |
| `--batch-size`   | `2048` | Caps tokens per input — see the pairing rule below |

> **Batch-size pairing.** The embedding server's `--batch-size` caps the
> tokens of any *single* input — a chunk larger than that is rejected
> outright and skipped during ingestion. OpenLAD derives its chunk/truncation
> limits from `OPENLAD_EMB_MAX_INPUT_TOKENS` (default `2048`, matching
> llama.cpp's own default). If you run the embedding server with a
> non-default `--batch-size` (e.g. `512` on a small GPU), set the same value:
> `OPENLAD_EMB_MAX_INPUT_TOKENS=512`. A mismatch does not crash — affected
> chunks are silently skipped and the document ingests "hollow". See
> [configuration reference](configuration.md#embedding-batch-size-pairing).

### Using Ollama instead

```bash
ollama pull qwen3.5:9b
ollama pull qwen3-embedding:0.6b

export OPENLAD_LLM_URL="http://127.0.0.1:11434/v1"
export OPENLAD_LLM_MODEL="qwen3.5:9b"
export OPENLAD_EMB_URL="http://127.0.0.1:11434/v1"
export OPENLAD_EMB_MODEL="qwen3-embedding:0.6b"
```

vLLM or any other OpenAI-compatible serving stack works the same way — set
the `*_URL` / `*_MODEL` variables to match.

### Optional: dedicated OCR vision model

Scanned or image-only pages are transcribed by a dedicated OCR endpoint —
any OpenAI-compatible vision model advertised via `OPENLAD_OCR_URL` /
`OPENLAD_OCR_MODEL`. A compact page-OCR VLM keeps the GPU footprint small;
e.g. OvisOCR2 (Apache-2.0, 0.8B) scores well on document parsing while
fitting alongside the LLM and embedding models on a 16 GB GPU:

```bash
# Download OvisOCR2 + its mmproj from a GGUF mirror, then:
llama-server \
    --model ~/models/ovisocr2-q8_0.gguf \
    --mmproj ~/models/mmproj-f16.gguf \
    --host 127.0.0.1 --port 8082 --alias ovisocr2 \
    --n-gpu-layers 999 --ctx-size 32768

export OPENLAD_OCR_URL=http://127.0.0.1:8082/v1
export OPENLAD_OCR_MODEL=ovisocr2
```

Pages that still cannot be transcribed are flagged with ingest warnings and
the document is marked `degraded` instead of silently missing content. When
no OCR endpoint is configured, image-only files fall back to Tesseract (if
installed).

---

## Docker

The fastest way to run OpenLAD: a single container for the API. The container
is CPU-only by design — model services stay **outside**: run llama-server /
vLLM / Ollama on the host, or point at any OpenAI-compatible endpoint, local
or cloud.

```bash
# 1. Install Docker (Ubuntu)
sudo apt install -y docker.io
sudo usermod -aG docker $USER   # re-login afterwards

# 2. Configure
cp docker/.env.example .env
# edit .env: admin password (required), model URLs, model names

# 3. Build & run
docker compose up -d --build
# → http://<host>:11296

# 4. Verify
curl http://127.0.0.1:11296/api/v1/health
# {"status":"ok", ...} — status is "degraded" if a model endpoint is down
```

Notes:

- The compose file uses `network_mode: host` (Linux): the container shares
  the host network, so `127.0.0.1:8080` URLs reach the model service on your
  machine directly.
- Cloud endpoints: set `OPENLAD_LLM_URL` / `OPENLAD_EMB_URL` to the public
  URLs.
- Data is persisted in `./data` (volume `./data:/app/data`). Rebuilds and
  restarts keep your documents.
- On first startup the container creates the admin user with
  `OPENLAD_ADMIN_PASSWORD` (only when no admin exists; changing the variable
  later does not reset the password).
