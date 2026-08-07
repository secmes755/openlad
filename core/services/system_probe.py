"""
System configuration probe: detect hardware (GPU VRAM, system memory) and
recommend a context-window configuration for the LLM / embedding servers.

Purpose: a deployment wizard for users on different hardware — instead of
guessing the context size, they get a sane starting configuration. The
recommendations are calibrated against a local measurement (2026-08):

  Qwen3.5-9B Q5_K_M weights ≈ 6.2 GB + mmproj-F16 ≈ 0.9 GB → ≈ 7.1 GB on GPU.
  f16 KV cache measured ≈ 0.4 GB per 10K tokens of context
  (llama-server -c 65536 + mmproj ≈ 9.7 GB total on a 16 GB GPU).
  Q4-quantized KV cache ≈ halves that: ~0.2 GB per 10K tokens.

Usage:
  python -m core.services.system_probe
"""

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Minimum LLM context window for *usable* retrieval. OpenLAD delivers whole
# chapters into the context and keeps a synthesis budget (≈60% of the window);
# below this minimum even a single chapter cannot fit, so 8192-style "it boots"
# numbers have no practical meaning here.
MIN_LLM_CONTEXT = 16384

# Recommended configuration by GPU VRAM (MiB): model, LLM context and
# quantizations. First row whose threshold is met wins.
# 15360 MiB ≈ 15 GiB catches "16 GB" cards (the recommended platform).
GPU_CTX_TABLE = [
    # (vram_mib, model, llm_ctx, kv_cache_type)
    (24576, "9B", 262144, "q4_0"),   # >= 24 GiB: full capability
    (15360, "9B", 131072, "q4_0"),   # >= 15 GiB: recommended platform
    (12288, "9B", 65536, "q4_0"),    # >= 12 GiB: usable, context-limited
    (8192, "4B", 32768, "q4_0"),     # >= 8 GiB: small model, capability-limited
]

# Recommendation shown for the small-model tier.
SMALL_MODEL_NOTE = (
    "4B model: usable for small/medium documents, but capability and "
    "long-context stability are limited. 16 GB VRAM with the 9B model is "
    "strongly recommended for a complete, stable experience."
)

# Recommended LLM context by system memory (MiB) when no NVIDIA GPU is present
# (CPU inference, much slower but functional).
CPU_CTX_TABLE = [
    (65536, 65536),
    (32768, 32768),
    (16384, 16384),
]

# Message returned when the hardware cannot run the system at all.
UNSUPPORTED_REASON = (
    "GPU VRAM below 8 GiB: insufficient for the smallest supported "
    "configuration. 16 GB VRAM with the 9B model is strongly recommended."
)


def detect_gpu() -> dict | None:
    """Detect NVIDIA GPU via nvidia-smi.

    Returns {name, vram_total_mb, vram_free_mb} or None if unavailable.
    """
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        name, total, free = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
        return {"name": name, "vram_total_mb": int(total), "vram_free_mb": int(free)}
    except (subprocess.SubprocessError, ValueError):
        return None


def detect_memory() -> dict:
    """Detect system memory via psutil (cross-platform)."""
    import psutil
    mem = psutil.virtual_memory()
    return {"total_mb": mem.total // (1024 * 1024),
            "available_mb": mem.available // (1024 * 1024)}


def recommend_context(gpu: dict | None = None, memory: dict | None = None) -> dict:
    """Return context recommendations based on detected hardware.

    gpu=None means CPU-only mode. memory defaults to a live detection.
    Returns {llm_ctx, kv_cache_type, min_llm_ctx, source}.
    """
    if memory is None:
        memory = detect_memory()

    if gpu:
        vram_mib = gpu["vram_total_mb"]
        for min_mib, model, ctx, kv in GPU_CTX_TABLE:
            if vram_mib >= min_mib:
                note = SMALL_MODEL_NOTE if model == "4B" else ""
                return {"supported": True, "model": model, "llm_ctx": ctx,
                        "kv_cache_type": kv, "min_llm_ctx": MIN_LLM_CONTEXT,
                        "source": f"GPU ({gpu['vram_total_mb']} MB VRAM)",
                        "note": note}
        return {"supported": False, "model": None, "llm_ctx": None,
                "kv_cache_type": None, "min_llm_ctx": MIN_LLM_CONTEXT,
                "source": f"GPU ({gpu['vram_total_mb']} MB VRAM)",
                "reason": UNSUPPORTED_REASON}
    mem_mib = memory["total_mb"]
    for min_mib, ctx in CPU_CTX_TABLE:
        if mem_mib >= min_mib:
            return {"supported": True, "model": "9B/4B", "llm_ctx": ctx,
                    "kv_cache_type": "q8_0", "min_llm_ctx": MIN_LLM_CONTEXT,
                    "source": f"CPU-only ({memory['total_mb']} MB system RAM)",
                    "note": "CPU inference is functional but slow; a GPU is "
                            "strongly recommended."}
    return {"supported": False, "model": None, "llm_ctx": None,
            "kv_cache_type": None, "min_llm_ctx": MIN_LLM_CONTEXT,
            "source": f"CPU-only ({memory['total_mb']} MB system RAM)",
            "reason": "System RAM below 16 GiB: too little memory for CPU "
                      "inference of the bundled models."}


def validate_context(llm_ctx: int) -> dict:
    """Check a configured LLM context against the minimum.

    Returns {ok, min_llm_ctx, warning}.
    """
    if llm_ctx < MIN_LLM_CONTEXT:
        return {"ok": False, "min_llm_ctx": MIN_LLM_CONTEXT,
                "warning": f"Configured LLM context ({llm_ctx}) is below the "
                           f"minimum ({MIN_LLM_CONTEXT}); retrieval quality will "
                           "be severely degraded (contexts get truncated)."}
    return {"ok": True, "min_llm_ctx": MIN_LLM_CONTEXT, "warning": ""}


def probe() -> dict:
    """Full hardware probe: detection + recommendations + validation."""
    gpu = detect_gpu()
    memory = detect_memory()
    rec = recommend_context(gpu, memory)
    return {"gpu": gpu, "memory": memory, "recommendation": rec}

def main():
    """CLI entry point: print a human-readable probe report."""
    import json

    result = probe()
    print("OpenLAD hardware probe")
    print("======================")
    gpu = result["gpu"]
    if gpu:
        print(f"GPU     : {gpu['name']} ({gpu['vram_total_mb']} MB total, "
              f"{gpu['vram_free_mb']} MB free)")
    else:
        print("GPU     : none detected (CPU mode)")
    mem = result["memory"]
    print(f"Memory  : {mem['total_mb']} MB total, {mem['available_mb']} MB available")
    rec = result["recommendation"]
    if not rec.get("supported"):
        print("Result  : NOT SUPPORTED on this hardware")
        print(f"  Reason: {rec.get('reason', '')}")
        print(f"  Minimum usable LLM context: {rec['min_llm_ctx']} tokens")
        return
    print(f"Recommendation ({rec['source']}):")
    print(f"  Model              : {rec.get('model', '')}")
    print(f"  LLM context        : {rec['llm_ctx']} tokens")
    print(f"  KV cache type      : {rec['kv_cache_type']}")
    print(f"  Minimum LLM context: {rec['min_llm_ctx']} tokens (below this the "
          "system cannot deliver usable retrieval)")
    note = rec.get("note", "")
    if note:
        print(f"  Note               : {note}")
    print()
    print("Set these in your start script (e.g. LLM_CTX_SIZE=131072 and")
    print("add --cache-type-k q4_0 --cache-type-v q4_0 to llama-server).")
    print()
    print("Full JSON:", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
