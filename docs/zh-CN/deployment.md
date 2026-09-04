# 部署与硬件

[English](../deployment.md) | [中文](deployment.md)

本指南覆盖最小快速开始之外的所有内容：硬件选型、模型后端选项、
推荐 llama-server 参数，以及 Docker 细节。

---

## 硬件探测

使用内置探测工具检测 GPU 显存 / 系统内存，并获得推荐配置：

```bash
python -m core.services.system_probe
```

### 快速配置查表

| GPU 显存 | 模型 | 权重量化 | KV cache | LLM 上下文 | 预期能力 |
|---|---|---|---|---|---|
| ≥ 24 GB | 9B | Q5_K_M | Q4 | 262144 | 完整能力 |
| 16 GB（**推荐**） | 9B | Q5_K_M | Q4 | 131072 | 完整能力 |
| 12 GB | 9B | Q5_K_M | Q4 | 65536 | 可用；大文档上下文受限 |
| 8 GB | 4B | Q4_K_M | Q4 | 32768 | 能力受限（见下方说明） |
| 仅 CPU | 9B / 4B | Q4_K_M | Q8 | 16384 – 65536 | 可用但慢；仅适合试用 |

**LLM 上下文下限：16384 tokens**——低于此值整章内容无法放入上下文，
检索质量会崩塌。将推荐值配置到启动脚本，例如 `LLM_CTX_SIZE=131072`，
并给 llama-server 加 `--cache-type-k q4_0 --cache-type-v q4_0`。

> **关于 8GB 显存的说明**：理论上可用 4B 模型运行，但小模型的能力限制
> 会在实际使用中显现——长文档处理可能不稳定、复杂问题的理解可靠性
> 下降。**强烈建议使用 16GB 显存 + 9B 模型**，以获得完整、稳定的体验。

---

## 模型后端

OpenLAD 通过 OpenAI 兼容 HTTP API 与模型服务通信。服务本身纯 CPU；
推理引擎由你自带。

### llama.cpp（推荐）

编译安装：

```bash
git clone https://github.com/ggerganov/llama.cpp.git /tmp/llama.cpp
cd /tmp/llama.cpp && cmake -B build && cmake --build build -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/
```

下载参考模型（HuggingFace）：

```bash
mkdir -p ~/models
huggingface-cli download Qwen/Qwen3.5-9B-GGUF qwen3.5-9b-q5_k_m.gguf \
    --local-dir ~/models
huggingface-cli download Qwen/Qwen3-Embedding-0.6B-GGUF \
    qwen3-embedding-0.6b-q8_0.gguf --local-dir ~/models
```

### 推荐 llama-server 参数

**LLM — Qwen3.5-9B Q5_K_M（推荐 16 GB VRAM）：**

| 参数             | 值       | 说明                                  |
| ---------------- | -------- | ------------------------------------- |
| `--n-gpu-layers` | `999`    | 全部层卸载到 GPU。8 GB VRAM 请减至 `35`。 |
| `--ctx-size`     | `262144` | 256K 上下文窗口。OOM 时减至 `65536`。     |
| `--parallel`     | `2`      | 并发请求槽位数                            |
| `--batch-size`   | `2048`   | Prompt 处理批大小                        |
| `--reasoning`    | `off`    | **关键** — Qwen3.5 的思考模式必须禁用      |
| `--cache-type-k` | `q4_0`   | Q4 KV 缓存量化（256K 时约 2.3 GB）        |
| `--cache-type-v` | `q4_0`   | Q4 值缓存量化                            |
| `-n`             | `-1`     | 生成 token 数无限制                       |

**Embedding — Qwen3-Embedding-0.6B Q8_0：**

| 参数             | 值     | 说明                          |
| ---------------- | ------ | ----------------------------- |
| `--n-gpu-layers` | `999`  | 全部层到 GPU（总计约 0.6 GB）     |
| `--ctx-size`     | `8192` | 8K 上下文（页级 Embedding 足够）  |
| `--embeddings`   | —      | 启用 Embedding 模式              |
| `--pooling`      | `mean` | Embedding 向量的均值池化           |
| `--batch-size`   | `2048` | 限制单条输入 token 上限——见下方配对规则 |

> **Batch-size 配对。** embedding 服务的 `--batch-size` 限制单条输入的
> token 上限，超限的 chunk 会被拒收并在入库时静默跳过。OpenLAD 依据
> `OPENLAD_EMB_MAX_INPUT_TOKENS`（默认 `2048`，与 llama.cpp 默认一致）
> 推导 chunk 尺寸与截断上限。若 embedding 服务使用非默认 batch（如小显存
> 上的 `512`），请同步设置：`OPENLAD_EMB_MAX_INPUT_TOKENS=512`。
> 失配不会报错——受影响的 chunk 被静默跳过，文档会"空心"入库。详见
> [配置参考](../configuration.md#embedding-batch-size-配对)。

### 使用 Ollama 替代

```bash
ollama pull qwen3.5:9b
ollama pull qwen3-embedding:0.6b

export OPENLAD_LLM_URL="http://127.0.0.1:11434/v1"
export OPENLAD_LLM_MODEL="qwen3.5:9b"
export OPENLAD_EMB_URL="http://127.0.0.1:11434/v1"
export OPENLAD_EMB_MODEL="qwen3-embedding:0.6b"
```

vLLM 或任何其他 OpenAI 兼容服务栈用法相同——设置对应的
`*_URL` / `*_MODEL` 变量即可。

### 可选：专用 OCR 视觉模型

扫描件或纯图像页面由专用 OCR 端点转写 —— 任意 OpenAI 兼容视觉模型均可，
通过 `OPENLAD_OCR_URL` / `OPENLAD_OCR_MODEL` 接入。紧凑型整页 OCR VLM
显存占用小；例如 OvisOCR2（Apache-2.0，0.8B）在文档解析上表现优异，可与
LLM 和 Embedding 模型共存于 16 GB 显卡：

```bash
# 先从 GGUF 镜像下载 OvisOCR2 及其 mmproj，然后：
llama-server \
    --model ~/models/ovisocr2-q8_0.gguf \
    --mmproj ~/models/mmproj-f16.gguf \
    --host 127.0.0.1 --port 8082 --alias ovisocr2 \
    --n-gpu-layers 999 --ctx-size 32768

export OPENLAD_OCR_URL=http://127.0.0.1:8082/v1
export OPENLAD_OCR_MODEL=ovisocr2
```

仍无法转写的页面会记录入库警告，文档标记为 `degraded`，不会静默缺失内容。
未配置 OCR 端点时，纯图片文件回退到 Tesseract（如已安装）。

---

## Docker

最快的方式：API 单容器运行。容器纯 CPU 是刻意设计——模型服务保持在
**容器外**：在宿主机运行 llama-server / vLLM / Ollama，或直连任意
OpenAI 兼容端点，本地或云端均可。

```bash
# 1. 安装 Docker（Ubuntu）
sudo apt install -y docker.io
sudo usermod -aG docker $USER   # 之后重新登录

# 2. 配置
cp docker/.env.example .env
# 编辑 .env：管理员密码（必填）、模型地址、模型名称

# 3. 构建并运行
docker compose up -d --build
# → http://<主机>:11296

# 4. 验证
curl http://127.0.0.1:11296/api/v1/health
# {"status":"ok", ...} — 模型端点不可达时 status 为 "degraded"
```

说明：

- compose 使用 `network_mode: host`（Linux）：容器与宿主机共享网络，
  `127.0.0.1:8080` 可直接访问本机模型服务。
- 云端端点：将 `OPENLAD_LLM_URL` / `OPENLAD_EMB_URL` 设为公网地址。
- 数据持久化在 `./data`（卷 `./data:/app/data`）。重建/重启不丢文档。
- 首次启动时容器会用 `OPENLAD_ADMIN_PASSWORD` 创建管理员（仅在不存在
  任何管理员时生效；之后修改该变量不会重置已有密码）。
