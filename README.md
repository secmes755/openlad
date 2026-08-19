<p align="center">
  <h1>OpenLAD</h1>
  <em>Local Document AI Knowledge Base — Fully Offline, Fully Open</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="README_zh-CN.md"><img src="https://img.shields.io/badge/中文-简体-red?style=for-the-badge" alt="中文"></a>
</p>

---

**OpenLAD** is an offline-first, local document intelligent Q&A system. Upload
your PDFs, Word documents, spreadsheets, and presentations — then ask questions
in natural language. Everything runs on your own hardware. No cloud. No
external API keys. No data leaves your premises.

Built for organizations that need a private document knowledge base on modest
hardware: a 16 GB consumer GPU and 32 GB of RAM is the recommended baseline.

<table>
<tr><td><b>🔒 Fully Offline</b></td><td>All processing — LLM inference, embeddings, OCR, document parsing — happens locally. Works on air-gapped networks.</td></tr>
<tr><td><b>📄 Multi-Format Ingestion</b></td><td>PDF, Word, Excel, PowerPoint, images, Markdown, HTML, TXT. Scanned documents handled via OCR (multimodal VLM / Tesseract).</td></tr>
<tr><td><b>🧠 Hybrid Retrieval</b></td><td>Full-text search (FTS5) + vector search (sqlite-vec) + LLM-driven planning. Three-phase pipeline: Plan → Retrieve → Synthesize.</td></tr>
<tr><td><b>🏭 Industry Plugins</b></td><td>Extensible plugin system. 1 complete sample pack (Semiconductor) + 3 empty templates (Legal, Financial, Generic) for customization. Custom packs can be built for any domain.</td></tr>
<tr><td><b>👥 Multi-Tenant</b></td><td>Isolated databases and vector spaces per tenant. Admin panel for user and document management.</td></tr>
<tr><td><b>🔐 Security</b></td><td>Login rate limiting (per-username + per-IP, no account lockout), expiring API keys (default 90 days, rotatable via admin panel), per-tenant data isolation, globally unique usernames.</td></tr>
<tr><td><b>🌐 Web UI</b></td><td>Built-in web interface. Admin panel at <code>/admin</code>, user Q&A at <code>/</code>. LAN-accessible.</td></tr>
<tr><td><b>🧩 BYO-LLM Architecture</b></td><td>Choose your own LLM and embedding backends — llama.cpp, Ollama, vLLM, or any OpenAI-compatible API.</td></tr>
</table>

---

## Docker Deployment

The fastest way to run OpenLAD: a single container for the API. The container
is CPU-only by design — model services stay **outside**: run llama-server /
vLLM / Ollama on the host, or point at any OpenAI-compatible endpoint, local
or cloud.

### 1. Install Docker

```bash
# Ubuntu
sudo apt install -y docker.io
sudo usermod -aG docker $USER   # re-login afterwards
```

### 2. Configure

```bash
cp docker/.env.example .env
# edit .env: admin password (required), model URLs, model names
```

### 3. Build & Run

```bash
docker compose up -d --build
# → http://<host>:11296
```

- The compose file uses `network_mode: host` (Linux): the container shares
  the host network, so `127.0.0.1:8080` URLs reach the model service on your
  machine directly.
- Cloud endpoints: set `OPENLAD_LLM_URL` / `OPENLAD_EMB_URL` to the public
  URLs.
- Data is persisted in `./data` (volume `./data:/app/data`). Rebuilds and
  restarts keep your documents.

### 4. Verify

```bash
curl http://127.0.0.1:11296/api/v1/health
# {"status":"ok", ...} — status is "degraded" if a model endpoint is down
```

On first startup the container creates the admin user with
`OPENLAD_ADMIN_PASSWORD` (only when no admin exists; changing the variable
later does not reset the password).

## Quick Start

### Prerequisites

- **Ubuntu 22.04/24.04** (x86_64) or macOS
- **Python 3.10+**
- **16+ GB VRAM GPU** (NVIDIA recommended; 8 GB works with reduced context — see below)
- **32+ GB RAM** (16 GB minimum for CPU-only inference)

### 1. Clone & Install

```bash
git clone https://github.com/secmes755/openlad.git
cd openlad

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
curl http://127.0.0.1:11296/api/v1/health
# → {"status":"ok","version":"...","name":"OpenLAD","services":{"database":...,"llm":...,"embedding":...}}
#   status is "degraded" if a model endpoint is unreachable
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

## ⚙ Configuration Reference

All settings are environment variables. Create a `.env` file or export directly.

### Model Backends

| Variable            | Default                    | Description                              |
| ------------------- | -------------------------- | ---------------------------------------- |
| `OPENLAD_LLM_URL`   | `http://localhost:8080/v1` | LLM API endpoint                         |
| `OPENLAD_LLM_MODEL` | *(required — no default)*  | Model name registered in the LLM backend |
| `OPENLAD_EMB_URL`   | `http://localhost:8081/v1` | Embedding API endpoint                   |
| `OPENLAD_EMB_MODEL` | *(required — no default)*  | Embedding model name                     |

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
- Or lower the retrieval context quota, e.g. `OPENLAD_MAX_CHARS=40000`
  (overrides the default phase-2 budget)

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

---

<p align="center">
  Built for organizations that value data sovereignty.
</p>
