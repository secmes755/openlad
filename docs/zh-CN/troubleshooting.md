# 故障排除

[English](../troubleshooting.md) | [中文](troubleshooting.md)

症状 → 原因 → 修复。如果你的问题不在列表里，先查查询日志和
`curl http://127.0.0.1:11296/api/v1/health` —— `status: "degraded"`
会指出哪个模型端点不可达。

---

## 安装阶段

### LLM 服务器启动后立即退出并报 "CUDA error"

显存不足以支撑所请求的配置。降低 GPU 层数和上下文：

```bash
llama-server --model ~/models/qwen3.5-9b-q5_k_m.gguf \
    --n-gpu-layers 25 --ctx-size 32768 ...
```

各显存档位的安全取值见[硬件配置查表](deployment.md#快速配置查表)。

### "--reasoning off" 参数无法识别

你的 llama.cpp 版本太旧。从源码重新编译：

```bash
cd /tmp/llama.cpp && git pull && cmake --build build -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/
```

### "No module named 'xxx'"

虚拟环境未激活或依赖未安装：

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### 端口被占用

```bash
lsof -ti :11296 | xargs kill -9
```

---

## 运行阶段

### 健康检查报 "degraded"

`services` 段中某个模型端点不可达。直接验证各后端：

```bash
curl http://127.0.0.1:8080/v1/models   # LLM
curl http://127.0.0.1:8081/v1/models   # embedding
```

如果端点能应答但 OpenLAD 仍报 degraded，检查 `OPENLAD_LLM_URL` /
`OPENLAD_EMB_URL` 及模型名称是否与后端注册的 alias 一致。

### 查询日志中出现 "Context size exceeded"

检索到的上下文超过了模型的上下文窗口。

- 增加 LLM 服务的 `--ctx-size`（显存允许时），或
- 调低检索上下文配额，例如 `OPENLAD_MAX_CHARS=40000`（覆盖默认的
  phase-2 预算）

### llama-server 静默劣化（卡死，或输出变慢/乱码）

在长期运行的部署中观察到：llama-server 进程活着，但开始超时或输出退化。
两个实测到的诱因：

1. **系统包更新替换了用户态 NVIDIA 库**（如 apt 升级）而服务未重启——
   已加载的驱动与磁盘上的库发生分叉。
2. **超长 uptime**（多天级）持续批处理。

处置前先探针确认：

```bash
curl http://127.0.0.1:8080/health        # 进程是否响应？
# 发一条短补全并计时——与你正常的 token/s 对比
curl http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" \
    -d '{"model":"qwen3.5-9b","messages":[{"role":"user","content":"ping"}],"max_tokens":16}'
```

修复：重启 llama-server（任何 NVIDIA 驱动/库更新后，重启**所有** GPU
进程）。如果驱动是通过 apt 更新的，重启整机最稳妥。

### 文档已入库但答案漏掉大部分内容（"空心"入库）

embedding 服务的 `--batch-size` 小于 `OPENLAD_EMB_MAX_INPUT_TOKENS`
（默认 `2048`）：超限 chunk 被服务拒收并静默跳过。对齐两个值后重新入库
该文档——见[配对规则](../configuration.md#embedding-batch-size-配对)。
