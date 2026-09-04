# API 快速上手

[English](../api-quickstart.md) | [中文](api-quickstart.md)

两个 curl 端到端示例。`http://localhost:11296/` 的 Web 界面可以交互式
完成同样流程。

> 下面命令中的凭据仅为请求格式占位符。真实管理员密码通过启动时的
> `OPENLAD_ADMIN_PASSWORD` 环境变量设置 —— 没有默认密码。

## 用例 1：上传数据手册并提问

```bash
# 1. 管理员登录
curl -X POST http://127.0.0.1:11296/api/v1/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"admin123"}'
# → {"api_key": "ak_...", "tenant_id": "..."}

# 2. 上传 PDF（使用返回的 api_key）
curl -X POST http://127.0.0.1:11296/api/v1/documents/upload \
    -H "Authorization: Bearer <api_key>" \
    -F "file=@/path/to/chip-datasheet.pdf"

# 3. 等待入库完成（检查状态）
curl http://127.0.0.1:11296/api/v1/documents \
    -H "Authorization: Bearer <api_key>"
# → 查找 "status": "completed"

# 4. 提问
curl -X POST http://127.0.0.1:11296/api/v1/query \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer <api_key>" \
    -d '{"query":"这颗芯片的最高 CPU 频率是多少？"}'
# → {"answer":"根据文档，最高 CPU 频率为 1.8 GHz...", ...}
```

## 用例 2：多文档对比

```bash
# 上传两份竞品数据手册，然后提问对比：
curl -X POST http://127.0.0.1:11296/api/v1/query \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer <api_key>" \
    -d '{"query":"对比 ProductA 和 ProductB 的 NPU 性能"}'
# → 返回并排 Markdown 对比表格，包含两份文档中的规格参数
```
