<p align="center">
  <h1>OpenLAD ☤</h1>
  <em>本地文档 AI 知识库 — 完全离线，完全开放</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/快速开始-5分钟-blue?style=for-the-badge" alt="Quick Start"></a>
  <a href="#-配置参考"><img src="https://img.shields.io/badge/GPU-16GB推荐-orange?style=for-the-badge" alt="GPU: 16GB rec"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/English-Version-blue?style=for-the-badge" alt="English"></a>
</p>

---

**OpenLAD** 是一个离线优先的本地文档智能问答系统。上传你的 PDF、Word 文档、
表格和演示文稿，然后用自然语言提问。一切都在你自己的硬件上运行。无需云端。
无需 API 密钥。数据不出你的内网。

专为需要在普通硬件上搭建私有文档知识库的组织设计：推荐 16 GB 消费级 GPU
和 32 GB 内存作为基线配置。

<table>
<tr><td><b>🔒 完全离线</b></td><td>所有处理 — LLM 推理、Embedding、OCR、文档解析 — 均在本地完成。支持物理隔离网络。</td></tr>
<tr><td><b>📄 多格式入库</b></td><td>PDF、Word、Excel、PowerPoint、图片、Markdown、HTML、TXT。扫描件通过 PaddleOCR 识别。</td></tr>
<tr><td><b>🧠 混合检索</b></td><td>全文检索（FTS5）+ 向量检索（sqlite-vec）+ LLM 驱动规划。三阶段流水线：规划 → 检索 → 合成。</td></tr>
<tr><td><b>🏭 行业插件</b></td><td>可扩展的插件系统。1 个完整示例包（半导体）+ 3 个空模板（法律、金融、通用）供定制。可为任意领域定制行业包。</td></tr>
<tr><td><b>👥 多租户</b></td><td>每租户独立数据库和向量空间。管理面板支持用户和文档管理。</td></tr>
<tr><td><b>🔐 安全</b></td><td>登录限流（按用户名 + 按 IP，不锁定账号）、API 密钥过期（默认 90 天，管理面板可轮换）、每租户数据隔离、每租户用户名唯一。</td></tr>
<tr><td><b>🌐 Web 界面</b></td><td>内置 Web 界面。管理面板 <code>/admin</code>，用户问答 <code>/</code>。局域网可访问。</td></tr>
<tr><td><b>🧩 BYO-LLM 架构</b></td><td>自由选择 LLM 和 Embedding 后端 — llama.cpp、Ollama、vLLM，或任何 OpenAI 兼容 API。</td></tr>
</table>

---

## 快速开始

### 前置要求

- **Ubuntu 22.04/24.04**（x86_64）或 macOS
- **Python 3.10+**
- **16+ GB VRAM GPU**（推荐 NVIDIA；8 GB 可运行但需降低上下文 — 见下文）
- **32+ GB RAM**（纯 CPU 推理最低 16 GB）

### 1. 克隆与安装

```bash
git clone https://github.com/your-org/OpenLAD.git
cd OpenLAD

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 启动模型服务

OpenLAD 需要两个模型后端。使用 **llama.cpp**（推荐）或 Ollama。

**安装 llama.cpp：**

```bash
git clone https://github.com/ggerganov/llama.cpp.git /tmp/llama.cpp
cd /tmp/llama.cpp && cmake -B build && cmake --build build -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/
```

**下载模型**（从 HuggingFace）：

```bash
mkdir -p ~/models

# LLM：Qwen3.5-9B Q5_K_M（约 5.4 GB）
huggingface-cli download Qwen/Qwen3.5-9B-GGUF qwen3.5-9b-q5_k_m.gguf \
    --local-dir ~/models

# Embedding：Qwen3-Embedding-0.6B Q8_0（约 0.6 GB）
huggingface-cli download Qwen/Qwen3-Embedding-0.6B-GGUF \
    qwen3-embedding-0.6b-q8_0.gguf --local-dir ~/models
```

**启动服务**（两个终端）：

```bash
# 终端 1：LLM（端口 8080）
llama-server \
    --model ~/models/qwen3.5-9b-q5_k_m.gguf \
    --host 127.0.0.1 --port 8080 --alias qwen3.5-9b \
    --n-gpu-layers 999 --ctx-size 262144 --parallel 2 \
    --batch-size 2048 --reasoning off \
    --cache-type-k q4_0 --cache-type-v q4_0 -n -1

# 终端 2：Embedding（端口 8081）
llama-server \
    --model ~/models/qwen3-embedding-0.6b-q8_0.gguf \
    --host 127.0.0.1 --port 8081 --alias qwen3-embedding \
    --n-gpu-layers 999 --ctx-size 8192 \
    --embeddings --pooling mean --batch-size 2048
```

### 3. 启动 OpenLAD

```bash
# 在 OpenLAD 目录下，确保 .venv 已激活：
./start.sh
```

验证：

```bash
curl http://127.0.0.1:11296/health
# → {"status":"ok","llm":true,"embedding":true}
```

打开浏览器：**`http://localhost:11296/`**

---

## 演示：两个快速用例

### 用例 1：上传数据手册并提问

```bash
# 1. 管理员登录
curl -X POST http://127.0.0.1:11296/api/v1/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"admin123"}'
# → {"api_key": "ak_...", "tenant_id": "..."}

# 2. 上传 PDF（使用返回的 api_key）
curl -X POST http://127.0.0.1:11296/api/v1/documents/upload \
    -H "Authorization: Bearer *** \
    -F "file=@/path/to/chip-datasheet.pdf"

# 3. 等待入库完成（检查状态）
curl http://127.0.0.1:11296/api/v1/documents \
    -H "Authorization: Bearer ***
# → 查找 "status": "completed"

# 4. 提问
curl -X POST http://127.0.0.1:11296/api/v1/query \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer *** \
    -d '{"query":"这颗芯片的最高 CPU 频率是多少？"}'
# → {"answer":"根据文档，最高 CPU 频率为 1.8 GHz...", ...}
```

### 用例 2：多文档对比

```bash
# 上传两份竞品数据手册，然后提问对比：
curl -X POST http://127.0.0.1:11296/api/v1/query \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer *** \
    -d '{"query":"对比 ProductA 和 ProductB 的 NPU 性能"}'
# → 返回并排 Markdown 对比表格，包含两份文档中的规格参数
```

---

## ⚙ 配置参考

所有配置均为环境变量。创建 `.env` 文件或直接 export。

### 模型后端

| 变量                  | 默认值                        | 说明               |
| ------------------- | -------------------------- | ---------------- |
| `OPENLAD_LLM_URL`   | `http://127.0.0.1:8080/v1` | LLM API 端点       |
| `OPENLAD_LLM_MODEL` | `qwen3.5-9b`               | 后端注册的模型名称        |
| `OPENLAD_EMB_URL`   | `http://127.0.0.1:8081/v1` | Embedding API 端点 |
| `OPENLAD_EMB_MODEL` | `qwen3-embedding`          | Embedding 模型名称   |

### 安全与认证

| 变量                         | 默认值 | 说明                                     |
| ---------------------------- | ------ | ---------------------------------------- |
| `OPENLAD_LOGIN_USER_PER_MIN` | `5`    | 每用户名每分钟登录尝试次数（超限 → 429） |
| `OPENLAD_LOGIN_IP_PER_MIN`   | `20`   | 每客户端 IP 每分钟登录尝试次数（→ 429）  |
| `OPENLAD_API_KEY_TTL_DAYS`   | `90`   | API 密钥默认有效期（天，`0` = 永不过期） |

登录在用户名和 IP 两个维度限流，但不锁定账号。API 密钥到期后失效，可随时在
管理面板的用户管理中轮换，或调用 `POST /api/v1/admin/users/{id}/regenerate-key`。

### llama-server 参数（测试基线）

**LLM — Qwen3.5-9B Q5_K_M（推荐 16 GB VRAM）：**

| 参数               | 值        | 说明                             |
| ---------------- | -------- | ------------------------------ |
| `--n-gpu-layers` | `999`    | 全部层卸载到 GPU。8 GB VRAM 请减至 `35`。 |
| `--ctx-size`     | `262144` | 256K 上下文窗口。OOM 时减至 `65536`。    |
| `--parallel`     | `2`      | 并发请求槽位数                        |
| `--batch-size`   | `2048`   | Prompt 处理批大小                   |
| `--reasoning`    | `off`    | **关键** — Qwen3.5 的思考模式必须禁用     |
| `--cache-type-k` | `q4_0`   | Q4 KV 缓存量化（256K 时约 2.3 GB）     |
| `--cache-type-v` | `q4_0`   | Q4 值缓存量化                       |
| `-n`             | `-1`     | 生成 token 数无限制                  |

**Embedding — Qwen3-Embedding-0.6B Q8_0：**

| 参数               | 值      | 说明                      |
| ---------------- | ------ | ----------------------- |
| `--n-gpu-layers` | `999`  | 全部层到 GPU（总计约 0.6 GB）    |
| `--ctx-size`     | `8192` | 8K 上下文（页级 Embedding 足够） |
| `--embeddings`   | —      | 启用 Embedding 模式         |
| `--pooling`      | `mean` | Embedding 向量的均值池化       |
| `--batch-size`   | `2048` | 批大小                     |

### 8 GB VRAM 配置（降低上下文）

如果你使用 8 GB GPU，必须显著降低上下文窗口。代价是：较短文档和简单
查询可以工作，但多页对比和长技术手册可能会触及上下文上限。

**显存核算**（Qwen3.5-9B Q5_K_M，权重约 6.5 GB）：

- 8 GB VRAM − 6.5 GB 模型 − 0.7 GB CUDA 开销 − 0.7 GB Embedding = **仅剩 0.1 GB 给 KV cache**
- q4_0 KV 量化下：**最大上下文约 4,000 tokens**
- 一次典型 OpenLAD 查询需要 **约 13,000 tokens**（6 个检索页 × 1,500 tokens + prompt + 输出）

**现实的 8 GB 方案**（单模型，Embedding 不在同一 GPU）：

```bash
# Embedding 跑在 CPU 或第二块 GPU 上，LLM 独占 8 GB
llama-server \
    --model ~/models/qwen3.5-9b-q5_k_m.gguf \
    --n-gpu-layers 35 --ctx-size 16384 \
    --parallel 1 --batch-size 1024 --reasoning off \
    --cache-type-k q4_0 --cache-type-v q4_0 -n -1 \
    --host 127.0.0.1 --port 8080 --alias qwen3.5-9b
```

使用 `--ctx-size 16384` 可处理 **约 10 个检索页** — 单文档问答够用，
跨文档合成勉强。生产环境强烈建议 16 GB VRAM。

### 使用 Ollama 替代

```bash
ollama pull qwen3.5:9b
ollama pull qwen3-embedding:0.6b

export OPENLAD_LLM_URL="http://127.0.0.1:11434/v1"
export OPENLAD_LLM_MODEL="qwen3.5:9b"
export OPENLAD_EMB_URL="http://127.0.0.1:11434/v1"
export OPENLAD_EMB_MODEL="qwen3-embedding:0.6b"
```

---

## 架构

```
浏览器（局域网）─── http://<主机>:11296/ ───┐
                                          │
┌─────────────────────────────────────────▼──────────────────────────┐
│                        OpenLAD API (FastAPI)                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  查询    │  │ 文档入库 │  │  管理    │  │  租户管理器      │  │
│  │  路由    │  │          │  │  面板    │  │  (SQLite, 每租户 │  │
│  │          │  │          │  │          │  │   独立数据库)    │  │
│  └────┬─────┘  └──────────┘  └──────────┘  └──────────────────┘  │
│       │                                                            │
│  ┌────▼──────────────────────────────────────────────────────┐    │
│  │                     检索流水线                             │    │
│  │  规划器 → 执行器 → 检索器 → 合并器 → 合成器               │    │
│  │  （FTS5 + sqlite-vec 混合检索）（LLM 答案生成）           │    │
│  └───────────────────────────────────────────────────────────┘    │
└──────────────────────────┬──────────────────┬────────────────────┘
                           │                  │
              ┌────────────▼──┐    ┌──────────▼─────────┐
              │  LLM 服务     │    │  Embedding 服务     │
              │  llama-server │    │  llama-server       │
              │  :8080        │    │  :8081              │
              │  Qwen3.5-9B   │    │  Qwen3-Emb-0.6B     │
              └───────────────┘    └─────────────────────┘
```

---

## 故障排除

### LLM 服务器启动后立即退出并报 "CUDA error"

降低 GPU 层数和上下文：

```bash
llama-server --model ~/models/qwen3.5-9b-q5_k_m.gguf \
    --n-gpu-layers 25 --ctx-size 32768 ...
```

### 查询日志中出现 "Context size exceeded"

检索到的上下文超过了模型的上下文窗口。

- 增加 `--ctx-size`（如果 VRAM 允许）
- 或设置 `OPENLAD_MAX_CHARS=80000` 截断检索上下文

### "No module named 'xxx'"

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### "reasoning off" 参数无法识别

你的 llama.cpp 版本太旧。从源码重新编译：

```bash
cd /tmp/llama.cpp && git pull && cmake --build build -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/
```

### 端口被占用

```bash
lsof -ti :11296 | xargs kill -9
```

---

## 许可证

OpenLAD 基于 **MIT 许可证** 开源。

所有核心依赖均使用与 MIT 兼容的宽松许可证：

| 依赖                         | 许可证              | 作用           |
| -------------------------- | ---------------- | ------------ |
| pypdf                      | BSD-3-Clause     | PDF 文本/元数据提取 |
| pdfplumber                 | MIT              | PDF 表格提取     |
| pdf2image                  | MIT              | PDF 页面渲染     |
| PaddleOCR                  | Apache 2.0       | OCR 引擎       |
| FastAPI, Pydantic, uvicorn | MIT/BSD          | Web 框架       |
| NumPy, Pandas, OpenCV      | BSD/Apache 2.0   | 数据与图像处理      |
| sqlite-vec                 | MIT / Apache 2.0 | 向量数据库        |

所有依赖均为宽松许可 — 无 AGPL、无 GPL、无 copyleft 限制。你可以自由使用、
修改和分发 OpenLAD，遵循 MIT 条款。

**行业包** 位于 `industries/sample_*/`，以 MIT 许可提供，作为参考实现。
仓库附带 1 个完整示例包（半导体）和 3 个空模板（法律、金融、通用）—
模板为定制化起点，非生产级行业解决方案。专有/商业行业包需单独授权。

详见 [LICENSE](LICENSE) 全文。

### 为什么选择 MIT？

OpenLAD 最初因依赖 PyMuPDF（AGPLv3）而采用 AGPLv3 许可证。在 v1.0 中，
PyMuPDF 被 pypdf（BSD）、pdfplumber（MIT）和 pdf2image（MIT）的组合
替代 — 全部为宽松许可 — 使 OpenLAD 能够采用 MIT 许可证。迁移完全透明：
AGPLv3 版本保留在 git tag `v0.9-agpl-pymupdf`。

---

<p align="center">
  为重视数据主权的组织而建。
</p>
