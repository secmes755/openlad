# Troubleshooting

[English](troubleshooting.md) | [中文](zh-CN/troubleshooting.md)

Symptom → cause → fix. If your issue isn't listed, check the query logs and
`curl http://127.0.0.1:11296/api/v1/health` first — `status: "degraded"`
tells you which model endpoint is unreachable.

---

## Setup failures

### LLM server exits immediately with "CUDA error"

Insufficient VRAM for the requested configuration. Reduce GPU layers and
context:

```bash
llama-server --model ~/models/qwen3.5-9b-q5_k_m.gguf \
    --n-gpu-layers 25 --ctx-size 32768 ...
```

See the [hardware lookup table](deployment.md#quick-configuration-lookup) for
safe values per GPU size.

### "--reasoning off" flag not recognized

Your llama.cpp is too old. Rebuild from source:

```bash
cd /tmp/llama.cpp && git pull && cmake --build build -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/
```

### "No module named 'xxx'"

The virtualenv isn't active or dependencies aren't installed:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Port already in use

```bash
lsof -ti :11296 | xargs kill -9
```

---

## Runtime issues

### Health check reports "degraded"

One of the model endpoints in the `services` section is unreachable. Verify
each backend directly:

```bash
curl http://127.0.0.1:8080/v1/models   # LLM
curl http://127.0.0.1:8081/v1/models   # embedding
```

If the endpoint answers but OpenLAD still reports degraded, check that
`OPENLAD_LLM_URL` / `OPENLAD_EMB_URL` and the model names match the backend's
registered aliases.

### "Context size exceeded" in query logs

The retrieved context exceeds the model's context window.

- Increase `--ctx-size` on the LLM server (if VRAM allows), or
- Lower the retrieval context quota, e.g. `OPENLAD_MAX_CHARS=40000`
  (overrides the default phase-2 budget)

### llama-server silently degrades (hangs, or slow/garbled output)

Observed in long-running deployments: the llama-server process stays alive
but starts producing timeouts or degenerate output. Two field-observed
triggers:

1. **A system package update replaced the user-space NVIDIA libraries** while
   the server was running (the loaded driver and on-disk libraries diverge).
2. **Extreme uptime** (many days) under continuous batching.

Probe before restarting anything:

```bash
curl http://127.0.0.1:8080/health        # process responsive?
# send a short completion and time it — compare against your normal token/s
curl http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" \
    -d '{"model":"qwen3.5-9b","messages":[{"role":"user","content":"ping"}],"max_tokens":16}'
```

Fix: restart llama-server (and after any NVIDIA driver/library update,
restart *all* GPU processes). If you installed driver updates via apt, a
reboot is the safest course.

### Document ingested but answers miss most of its content ("hollow" ingestion)

The embedding server's `--batch-size` is smaller than
`OPENLAD_EMB_MAX_INPUT_TOKENS` (default `2048`): oversized chunks are
rejected by the server and silently skipped. Align the two values and
re-ingest the document — see the
[pairing rule](configuration.md#embedding-batch-size-pairing).
