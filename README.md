<p align="center">
  <h1>OpenLAD ☤</h1>
  <em>Local Document AI Knowledge Base — Fully Offline, Fully Open</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/Quick_Start-5_minutes-blue?style=for-the-badge" alt="Quick Start"></a>
  <a href="#-configuration-reference"><img src="https://img.shields.io/badge/GPU-16_GB_rec-orange?style=for-the-badge" alt="GPU: 16GB rec"></a>
  <a href="README_zh-CN.md"><img src="https://img.shields.io/badge/中文-简体-red?style=for-the-badge" alt="中文"></a>
</p>

---

**OpenLAD** is an offline-first, local document intelligent Q&A system. Upload
your PDFs, Word documents, spreadsheets, and presentations — then ask questions
in natural language. Everything runs on your own hardware. No cloud. No API
keys. No data leaves your premises.

Built for organizations that need a private document knowledge base on modest
hardware: a 16 GB consumer GPU and 32 GB of RAM is the recommended baseline.

<table>
<tr><td><b>🔒 Fully Offline</b></td><td>All processing — LLM inference, embeddings, OCR, document parsing — happens locally. Works on air-gapped networks.</td></tr>
<tr><td><b>📄 Multi-Format Ingestion</b></td><td>PDF, Word, Excel, PowerPoint, images, Markdown, HTML, TXT. OCR with PaddleOCR for scanned documents.</td></tr>
<tr><td><b>🧠 Hybrid Retrieval</b></td><td>Full-text search (FTS5) + vector search (sqlite-vec) + LLM-driven planning. Three-phase pipeline: Plan → Retrieve → Synthesize.</td></tr>
<tr><td><b>🏭 Industry Plugins</b></td><td>Extensible plugin system. 1 complete sample pack (Semiconductor) + 3 empty templates (Legal, Financial, Generic) for customization. Custom packs can be built for any domain.</td></tr>
<tr><td><b>👥 Multi-Tenant</b></td><td>Isolated databases and vector spaces per tenant. Admin panel for user and document management.</td></tr>
<tr><td><b>🔐 Security</b></td><td>Login rate limiting (per-username + per-IP, no account lockout), expiring API keys (default 90 days, rotatable via admin panel), per-tenant data isolation, unique usernames per tenant.</td></tr>
<tr><td><b>🌐 Web UI</b></td><td>Built-in web interface. Admin panel at <code>/admin</code>, user Q&A at <code>/</code>. LAN-accessible.</td></tr>
<tr><td><b>🧩 BYO-LLM Architecture</b></td><td>Choose your own LLM and embedding backends — llama.cpp, Ollama, vLLM, or any OpenAI-compatible API.</td></tr>
</table>

---

## Quick Start

### Prerequisites

- **Ubuntu 22.04/24.04** (x86_64) or macOS
- **Python 3.10+**
- **16+ GB VRAM GPU** (NVIDIA recommended; 8 GB works with reduced context — see below)
- **32+ GB RAM** (16 GB minimum for CPU-only inference)

### 1. Clone & Install

```bash
git clone https://github.com/your-org/OpenLAD.git
cd OpenLAD

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Start Model Services

OpenLAD requires two model backends. Use **llama.cpp** (recommended) or Ollama.

**Install llama.cpp:**

```bash
git clone https://github.com/ggerganov/llama.cpp.git /tmp/llama.cpp
cd /tmp/llama.cpp && cmake -B build && cmake --build build -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/
```

**Download models** (from HuggingFace):

```bash
mkdir -p ~/models

# LLM: Qwen3.5-9B Q5_K_M (~5.4 GB)
huggingface-cli download Qwen/Qwen3.5-9B-GGUF qwen3.5-9b-q5_k_m.gguf \
    --local-dir ~/models

# Embedding: Qwen3-Embedding-0.6B Q8_0 (~0.6 GB)
huggingface-cli download Qwen/Qwen3-Embedding-0.6B-GGUF \
    qwen3-embedding-0.6b-q8_0.gguf --local-dir ~/models
```

**Start the services** (in two terminals):

```bash
# Terminal 1: LLM (port 8080)
llama-server \
    --model ~/models/qwen3.5-9b-q5_k_m.gguf \
    --host 127.0.0.1 --port 8080 --alias qwen3.5-9b \
    --n-gpu-layers 999 --ctx-size 262144 --parallel 2 \
    --batch-size 2048 --reasoning off \
    --cache-type-k q4_0 --cache-type-v q4_0 -n -1

# Terminal 2: Embedding (port 8081)
llama-server \
    --model ~/models/qwen3-embedding-0.6b-q8_0.gguf \
    --host 127.0.0.1 --port 8081 --alias qwen3-embedding \
    --n-gpu-layers 999 --ctx-size 8192 \
    --embeddings --pooling mean --batch-size 2048
```

### 3. Start OpenLAD

```bash
# In the OpenLAD directory, with .venv activated:
./start.sh
```

Verify:

```bash
curl http://127.0.0.1:11296/health
# → {"status":"ok","llm":true,"embedding":true}
```

Open your browser: **`http://localhost:11296/`**

---

## Demo: Two Quick Use Cases

### Case 1: Upload a Datasheet and Ask Questions

> Note: the credentials below are placeholders for the request format only.
> The real admin password is set via the `OPENLAD_ADMIN_PASSWORD` environment
> variable at startup — there is no default password.

```bash
# 1. Login as admin
curl -X POST http://127.0.0.1:11296/api/v1/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"admin123"}'
# → {"api_key": "ak_...", "tenant_id": "..."}

# 2. Upload a PDF (use the returned api_key)
curl -X POST http://127.0.0.1:11296/api/v1/documents/upload \
    -H "Authorization: Bearer <your-api-key>" \
    -F "file=@/path/to/chip-datasheet.pdf"

# 3. Wait for ingestion to complete (check status)
curl http://127.0.0.1:11296/api/v1/documents \
    -H "Authorization: Bearer <your-api-key>"
# → Look for "status": "completed"

# 4. Ask a question
curl -X POST http://127.0.0.1:11296/api/v1/query \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer <your-api-key>" \
    -d '{"query":"What is the maximum CPU frequency of this chip?"}'
# → {"answer":"Based on the document, the maximum CPU frequency is 1.8 GHz...", ...}
```

### Case 2: Multi-Document Comparison

```bash
# Upload two competitor datasheets, then ask a comparison question:
curl -X POST http://127.0.0.1:11296/api/v1/query \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer <your-api-key>" \
    -d '{"query":"Compare the NPU performance between ProductA and ProductB"}'
# → Returns a side-by-side Markdown comparison table with specs from both documents
```

---

## 🔍 Deployment: Hardware Probe

Run the built-in probe to detect your GPU VRAM / system memory and get a
recommended LLM context window (calibrated for the bundled 9B model with a
Q4-quantized KV cache):

```bash
python -m core.services.system_probe
```

Example output on the baseline platform (RTX 5060 Ti 16GB):

```
GPU     : NVIDIA GeForce RTX 5060 Ti (16311 MB total, 3252 MB free)
Recommendation (GPU NVIDIA GeForce RTX 5060 Ti (16311 MB VRAM)):
  LLM context        : 131072 tokens
  KV cache type      : q4_0
  Minimum LLM context: 8192 tokens
```

Recommendation table (9B Q5_K_M model):

| GPU VRAM | Recommended LLM context | KV cache |
|---|---|---|
| ≥ 24 GiB | 262144 | q4_0 |
| ≥ 15 GiB (16 GB cards) | 131072 | q4_0 |
| ≥ 12 GiB | 65536 | q4_0 (tight) |
| CPU-only (≥ 16 GiB RAM) | 16384 – 65536 | q8_0 |
| < 12 GiB VRAM | **not supported** | — |

**Minimum usable LLM context: 16384 tokens** — the probe reports a machine
as unsupported below 12 GiB VRAM / 16 GiB RAM, because the bundled 9B LLM
(~7.1 GB weights incl. mmproj) plus the embedding model cannot both run, and
below 16K tokens even a single chapter cannot fit in the context. Set the
recommended values in your start script, e.g. `LLM_CTX_SIZE=131072` and
`--cache-type-k q4_0 --cache-type-v q4_0` on llama-server.

---

## ⚙ Configuration Reference

All settings are environment variables. Create a `.env` file or export directly.

### Model Backends

| Variable            | Default                    | Description                      |
| ------------------- | -------------------------- | -------------------------------- |
| `OPENLAD_LLM_URL`   | `http://127.0.0.1:8080/v1` | LLM API endpoint                 |
| `OPENLAD_LLM_MODEL` | `qwen3.5-9b`               | Model name registered in backend |
| `OPENLAD_EMB_URL`   | `http://127.0.0.1:8081/v1` | Embedding API endpoint           |
| `OPENLAD_EMB_MODEL` | `qwen3-embedding`          | Embedding model name             |

### Security & Authentication

| Variable                     | Default | Description                                    |
| ---------------------------- | ------- | ---------------------------------------------- |
| `OPENLAD_LOGIN_USER_PER_MIN` | `5`     | Login attempts per username per minute (→ 429) |
| `OPENLAD_LOGIN_IP_PER_MIN`   | `20`    | Login attempts per client IP per minute (→ 429)|
| `OPENLAD_API_KEY_TTL_DAYS`   | `90`    | Default API key validity in days (`0` = never) |

Login is rate-limited on both the username and IP axes without locking accounts.
API keys expire after the TTL and can be rotated anytime from the admin
user-management panel, or via `POST /api/v1/admin/users/{id}/regenerate-key`.

### Recommended llama-server Flags

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
| `--batch-size`   | `2048` | Batch size                                        |

### 8 GB VRAM Configuration (Reduced Context)

If you have an 8 GB GPU, you must significantly reduce the context window.
The trade-off: shorter documents and simpler queries will work, but multi-page
comparisons and long technical manuals may hit context limits.

**Math check** (Qwen3.5-9B Q5_K_M, ~6.5 GB weights):

- 8 GB VRAM − 6.5 GB model − 0.7 GB CUDA overhead − 0.7 GB embedding = **0.1 GB left for KV cache**
- At q4_0 KV quantization: **~4,000 tokens max context**
- A typical OpenLAD query needs **~13,000 tokens** (6 retrieved pages × 1,500 tokens + prompt + output)

**Realistic 8 GB setup** (single model, no embedding on same GPU):

```bash
# Run embedding on CPU or a second GPU. LLM gets the full 8 GB.
llama-server \
    --model ~/models/qwen3.5-9b-q5_k_m.gguf \
    --n-gpu-layers 35 --ctx-size 16384 \
    --parallel 1 --batch-size 1024 --reasoning off \
    --cache-type-k q4_0 --cache-type-v q4_0 -n -1 \
    --host 127.0.0.1 --port 8080 --alias qwen3.5-9b
```

With `--ctx-size 16384`, you can process **~10 retrieved pages** — adequate for
single-document Q&A, marginal for cross-document synthesis. For production use,
16 GB VRAM is strongly recommended.

### Using Ollama Instead

```bash
ollama pull qwen3.5:9b
ollama pull qwen3-embedding:0.6b

export OPENLAD_LLM_URL="http://127.0.0.1:11434/v1"
export OPENLAD_LLM_MODEL="qwen3.5:9b"
export OPENLAD_EMB_URL="http://127.0.0.1:11434/v1"
export OPENLAD_EMB_MODEL="qwen3-embedding:0.6b"
```

---

## Architecture

```
Browser (LAN) ─── http://<host>:11296/ ───┐
                                          │
┌─────────────────────────────────────────▼──────────────────────────┐
│                        OpenLAD API (FastAPI)                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  Query   │  │ Document │  │  Admin   │  │  Tenant Manager  │  │
│  │  Router  │  │ Ingestion│  │  Panel   │  │  (SQLite, per-   │  │
│  │          │  │          │  │          │  │   tenant DB)      │  │
│  └────┬─────┘  └──────────┘  └──────────┘  └──────────────────┘  │
│       │                                                            │
│  ┌────▼──────────────────────────────────────────────────────┐    │
│  │                 Retrieval Pipeline                         │    │
│  │  Planner → Executor → Retriever → Merger → Synthesizer    │    │
│  │  (FTS5 + sqlite-vec hybrid)  (LLM answer generation)      │    │
│  └───────────────────────────────────────────────────────────┘    │
└──────────────────────────┬──────────────────┬────────────────────┘
                           │                  │
              ┌────────────▼──┐    ┌──────────▼─────────┐
              │  LLM Service  │    │  Embedding Service  │
              │  llama-server │    │  llama-server       │
              │  :8080        │    │  :8081              │
              │  Qwen3.5-9B   │    │  Qwen3-Emb-0.6B     │
              └───────────────┘    └─────────────────────┘
```

---

## Troubleshooting

### LLM server exits immediately with "CUDA error"

Reduce GPU layers and context:

```bash
llama-server --model ~/models/qwen3.5-9b-q5_k_m.gguf \
    --n-gpu-layers 25 --ctx-size 32768 ...
```

### "Context size exceeded" in query logs

The retrieved context exceeds the model's context window.

- Increase `--ctx-size` (if VRAM allows)
- Or set `OPENLAD_MAX_CHARS=80000` to truncate retrieval context

### "No module named 'xxx'"

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### "reasoning off" flag not recognized

Your llama.cpp is too old. Rebuild from source:

```bash
cd /tmp/llama.cpp && git pull && cmake --build build -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/
```

### Port already in use

```bash
lsof -ti :11296 | xargs kill -9
```

---

## License

OpenLAD is licensed under the **MIT License**.

All core dependencies use permissive licenses compatible with MIT:

| Dependency                 | License          | Role                         |
| -------------------------- | ---------------- | ---------------------------- |
| pypdf                      | BSD-3-Clause     | PDF text/metadata extraction |
| pdfplumber                 | MIT              | PDF table extraction         |
| pdf2image                  | MIT              | PDF page rendering           |
| PaddleOCR                  | Apache 2.0       | OCR engine                   |
| FastAPI, Pydantic, uvicorn | MIT/BSD          | Web framework                |
| NumPy, Pandas, OpenCV      | BSD/Apache 2.0   | Data & image processing      |
| sqlite-vec                 | MIT / Apache 2.0 | Vector database              |

All dependencies are permissively licensed — no AGPL, no GPL, no copyleft
restrictions. You are free to use, modify, and distribute OpenLAD under the
MIT terms.

**Industry packs** under `industries/sample_*/` are provided under MIT as
reference implementations. The repository ships with 1 complete sample pack
(Semiconductor) and 3 empty templates (Legal, Financial, Generic) — the
templates are starting points for customization, not production-grade industry
solutions. Proprietary/commercial industry packs are available under separate
licensing.

See [LICENSE](LICENSE) for the full text.

### Why MIT?

OpenLAD was originally licensed under AGPLv3 due to its dependency on PyMuPDF
(AGPLv3). In v1.0, PyMuPDF was replaced with a combination of pypdf (BSD),
pdfplumber (MIT), and pdf2image (MIT) — all permissively licensed — allowing
OpenLAD to adopt the MIT license. The migration is fully transparent: the
AGPLv3 version is preserved at git tag `v0.9-agpl-pymupdf`.

---

<p align="center">
  Built for organizations that value data sovereignty.
</p>
