# API Quickstart

[English](api-quickstart.md) | [中文](zh-CN/api-quickstart.md)

Two end-to-end examples with curl. The web UI at `http://localhost:11296/`
covers the same flows interactively.

> The credentials below are placeholders for the request format only. The
> real admin password is set via the `OPENLAD_ADMIN_PASSWORD` environment
> variable at startup — there is no default password.

## Case 1: Upload a datasheet and ask questions

```bash
# 1. Login as admin
curl -X POST http://127.0.0.1:11296/api/v1/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"admin123"}'
# → {"api_key": "ak_...", "tenant_id": "..."}

# 2. Upload a PDF (use the returned api_key)
curl -X POST http://127.0.0.1:11296/api/v1/documents/upload \
    -H "Authorization: Bearer <api_key>" \
    -F "file=@/path/to/chip-datasheet.pdf"

# 3. Wait for ingestion to complete (check status)
curl http://127.0.0.1:11296/api/v1/documents \
    -H "Authorization: Bearer <api_key>"
# → Look for "status": "completed"

# 4. Ask a question
curl -X POST http://127.0.0.1:11296/api/v1/query \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer <api_key>" \
    -d '{"query":"What is the maximum CPU frequency of this chip?"}'
# → {"answer":"Based on the document, the maximum CPU frequency is 1.8 GHz...", ...}
```

## Case 2: Multi-document comparison

```bash
# Upload two competitor datasheets, then ask a comparison question:
curl -X POST http://127.0.0.1:11296/api/v1/query \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer <api_key>" \
    -d '{"query":"Compare the NPU performance between ProductA and ProductB"}'
# → Returns a side-by-side Markdown comparison table with specs from both documents
```
