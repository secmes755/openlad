# 配置参考

[English](../configuration.md) | [中文](configuration.md)

所有配置均为 `OPENLAD_*` 前缀的环境变量。按运行方式有三种提供途径：

- **源码安装（`./start.sh`）** — `start.sh` 启动时自动加载仓库根目录的
  `.env`；未设置的变量回落到下表的内置默认值。已在 shell 中 export 的
  环境变量优先于 `.env`。
- **Docker** — compose 通过 `env_file` 加载 `docker-compose.yml` 旁的
  `.env`；模板见 `docker/.env.example`。
- **管理面板** — 模型端点也可在运行时经 管理面板 → 模型服务 设置。

---

## 模型后端

| 变量                | 默认值                       | 说明               |
| ------------------- | -------------------------- | ---------------- |
| `OPENLAD_LLM_URL`   | `http://localhost:8080/v1` | LLM API 端点       |
| `OPENLAD_LLM_MODEL` | *（必填——无默认值）*            | LLM 后端注册的模型名称  |
| `OPENLAD_EMB_URL`   | `http://localhost:8081/v1` | Embedding API 端点 |
| `OPENLAD_EMB_MODEL` | *（必填——无默认值）*            | Embedding 模型名称   |

## OCR 端点（可选）

| 变量                | 默认值     | 说明                              |
| ------------------- | -------- | --------------------------------- |
| `OPENLAD_OCR_URL`   | *（未设置）* | 整页 OCR 的 OpenAI 兼容视觉端点        |
| `OPENLAD_OCR_MODEL` | *（未设置）* | 视觉模型名称（如 `ovisocr2`）          |

未配置时，纯图片页面回退到 Tesseract（如已安装）；仍无法转写的页面会把
文档标记为 `degraded`，不会静默丢内容。

## 安全与认证

| 变量                         | 默认值     | 说明                                     |
| ---------------------------- | -------- | ---------------------------------------- |
| `OPENLAD_ADMIN_PASSWORD`     | *（未设置）* | 首次启动创建 admin 用户时必填；之后修改不会重置已有密码 |
| `OPENLAD_LOGIN_USER_PER_MIN` | `5`      | 每用户名每分钟登录尝试次数（超限 → 429） |
| `OPENLAD_LOGIN_IP_PER_MIN`   | `20`     | 每客户端 IP 每分钟登录尝试次数（→ 429）  |
| `OPENLAD_API_KEY_TTL_DAYS`   | `90`     | API 密钥默认有效期（天，`0` = 永不过期） |

登录在用户名和 IP 两个维度限流，但不锁定账号。API 密钥到期后失效，可随时在
管理面板的用户管理中轮换，或调用 `POST /api/v1/admin/users/{id}/regenerate-key`。

## Embedding batch-size 配对

embedding 服务的 `--batch-size` 限制单条输入的 token 上限，超限的 chunk
会被拒收并在入库时静默跳过。OpenLAD 依据 `OPENLAD_EMB_MAX_INPUT_TOKENS`
（默认 `2048`，与 llama.cpp 默认一致）推导 chunk 尺寸与截断上限。若
embedding 服务使用非默认 batch（如小显存上的 `512`），请同步设置：

```bash
OPENLAD_EMB_MAX_INPUT_TOKENS=512
```

失配不会报错——受影响的 chunk 被静默跳过，文档会"空心"入库（库里有这篇
文档，但大部分正文从未生成向量）。如怀疑遇到此问题，对齐两个值后重新
入库受影响文档。

## 检索与入库调优

| 变量                           | 默认值         | 说明                             |
| ------------------------------ | ------------ | -------------------------------- |
| `OPENLAD_MAX_CHARS`            | *（未设置——自动预算）* | 覆盖 phase-2 检索上下文预算（字符数）。查询报 "context size exceeded" 时调低 |
| `OPENLAD_EMB_MAX_INPUT_TOKENS` | `2048`       | 必须与 embedding 服务 `--batch-size` 一致——见上方配对规则 |
| `OPENLAD_INGEST_MAX_WORKERS`   | `1`          | 入库并行 worker 数——与 LLM 服务 `--parallel` 对齐（安全默认 `1`；如 `--parallel 2` → 设 `2`） |
| `OPENLAD_CHART_VLM_MAX_WORKERS`| `1`          | 入库期间图表/VLM 并发数——同上配对规则 |
| `OPENLAD_DATA_DIR`             | `./data`     | 文档/数据库存储根目录                 |
| `OPENLAD_INDUSTRIES_DIRS`      | *（未设置）*    | 额外行业包扫描目录（Windows 用 `;` 分隔，Linux 用 `:`） |

## 查询并发

| 变量                            | 默认值   | 说明                                 |
| ------------------------------- | ------ | ------------------------------------ |
| `OPENLAD_QUERY_CONCURRENCY_MODE`| `auto` | `auto` / `serial`（强制串行）/ `parallel`。`start.sh` 固定为 `serial` |
| `OPENLAD_QUERY_MAX_CONCURRENT`  | `0`    | `0` = 默认策略；`>0` 强制指定上限       |
| `OPENLAD_LLM_NP`                | `1`    | LLM API 并发槽位——必须与 llama-server `--parallel` 一致 |

这些配置要与后端的实际并行能力对齐——失配要么让查询无谓排队，要么压垮
LLM 服务。
