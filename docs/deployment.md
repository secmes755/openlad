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
| 16 GB **(recommended)** | 9B | Q5_K_M | Q4 | 131072 | Default resident profile; optional staged 35B querying |
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
> reliably. **16 GB VRAM with the 9B model is the default recommendation** for
> a simple, stable, always-on setup. If query quality matters more and model
> switching by work phase is acceptable, use the optional 35B query profile below.

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
# 35B query profile (switched with the 9B ingestion profile by work phase)
huggingface-cli download Qwen/Qwen3.6-35B-A3B-GGUF qwen3.6-35b-a3b-q2_k_xl.gguf \
    --local-dir ~/models
# 9B resident / ingestion profile
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
| `--cache-ram`    | `2048`   | Host-side prompt cache cap, MiB — **pin this**, see below |
| `-n`             | `-1`     | No limit on generated tokens                             |

**Embedding — Qwen3-Embedding-0.6B Q8_0:**

| Flag             | Value  | Notes                                             |
| ---------------- | ------ | ------------------------------------------------- |
| `--n-gpu-layers` | `999`  | All layers to GPU (~0.6 GB total)                 |
| `--ctx-size`     | `8192` | 8K context (sufficient for page-level embeddings) |
| `--embeddings`   | —      | Enable embedding mode                             |
| `--pooling`      | `mean` | Mean pooling for embedding vectors                |
| `--batch-size`   | `2048` | Caps tokens per input — see the pairing rule below |
| `--cache-ram`    | `0`    | Prompt cache off — nothing to reuse for embeddings |

> **Batch-size pairing.** The embedding server's `--batch-size` caps the
> tokens of any *single* input — a chunk larger than that is rejected
> outright and skipped during ingestion. OpenLAD derives its chunk/truncation
> limits from `OPENLAD_EMB_MAX_INPUT_TOKENS` (default `2048`, matching
> llama.cpp's own default). If you run the embedding server with a
> non-default `--batch-size` (e.g. `512` on a small GPU), set the same value:
> `OPENLAD_EMB_MAX_INPUT_TOKENS=512`. A mismatch does not crash — affected
> chunks are silently skipped and the document ingests "hollow". See
> [configuration reference](configuration.md#embedding-batch-size-pairing).

> **The prompt cache lives in host RAM, and its default ceiling is 8192 MiB *per
> process*.** llama.cpp's server keeps the state of recent prompts on the host so
> that a later request can skip re-processing a shared prefix. Leave that default
> in place and three model services reserve up to 24 GiB of host RAM that is never
> returned while the servers run — on a 32 GB baseline machine the OOM killer
> eventually takes a model server, and every service sharing memory with it. Pin
> the cache explicitly: `--cache-ram 2048` for the main LLM (bounded, keeps prefix
> reuse), `--cache-ram 0` for the embedding and OCR servers (nothing to reuse).
> A cached prompt state costs roughly 28 KB of host RAM per prompt token, so the
> 8192 MiB default holds ~300k prompt tokens.

### Optional: staged two-model profile for 16 GB GPUs

The always-on 9B + Embedding + OCR combination remains the simplest deployment.
If query comprehension and calculation quality matter more, and ingestion and
querying can run in separate phases, use these mutually exclusive profiles on the
same 16 GB GPU:

| Phase | Models and services | Intended work |
|---|---|---|
| Query | 35B-A3B MoE (for example, a Qwen3.6-35B-A3B Q2_K_XL GGUF) + Embedding | Retrieval, calculation and answer synthesis |
| Ingest | 9B (Qwen3.5 Q5_K_M) + Embedding + optional OCR | Parsing, classification, entities and indexing |

Start the 35B query server with these conservative settings:

| Flag | Value | Reason |
|---|---|---|
| `--n-gpu-layers` | `999` | Put all layers on the GPU; verify in the startup log that no layers fall back to CPU |
| `--ctx-size` | `32768` | Leave VRAM for weights, KV cache and Embedding |
| `--parallel` | `1` | One slot; OpenLAD queues concurrent queries instead of synthesizing them in parallel |
| `--cache-type-k` / `--cache-type-v` | `q8_0` | Preserve KV precision within the limited memory budget |
| `--cache-ram` | `2048` | Bound the host-side prompt cache |
| `--reasoning` | `off` | Use non-thinking mode |

**Resource limits:** the 35B query profile nearly fills a 16 GB GPU. Stop OCR and
do not run OCR-dependent ingestion in this phase. Its 32K context is also shorter
than the default 9B profile, so long chapters may be truncated. This trades
long-context and concurrency headroom for a more capable query model. It is not an
accuracy guarantee: validate against your own documents and inspect `degraded`
ingest warnings.

**Switching profiles:** drain active requests, stop the old LLM (and OCR when no
longer needed), then start the other phase's llama-server configuration. Keep
`OPENLAD_LLM_MODEL` or the admin model alias consistent with the actual server;
restart the API when necessary to apply an environment override. Changing the
model alias alone does not load or unload GGUF weights, and queries are
unavailable during the switch.

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
fitting alongside the LLM and embedding models in the default 9B resident
profile on a 16 GB GPU. Stop OCR before entering the 35B query profile, and
start it again after switching back to the 9B ingestion profile:

```bash
# Download OvisOCR2 + its mmproj from a GGUF mirror, then:
llama-server \
    --model ~/models/ovisocr2-q8_0.gguf \
    --mmproj ~/models/mmproj-f16.gguf \
    --host 127.0.0.1 --port 8082 --alias ovisocr2 \
    --n-gpu-layers 999 --ctx-size 32768 --cache-ram 0

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
