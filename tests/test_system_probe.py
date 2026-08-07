"""Hardware probe recommendation checks (pure logic, no GPU needed)."""
from core.services.system_probe import (
    MIN_LLM_CONTEXT,
    recommend_context,
    validate_context,
)


def test_gpu_16gb_recommends_9b_128k():
    gpu = {"name": "GPU", "vram_total_mb": 16311, "vram_free_mb": 8000}
    mem = {"total_mb": 31899, "available_mb": 10000}
    rec = recommend_context(gpu, mem)
    assert rec["supported"] is True
    assert rec["model"] == "9B"
    assert rec["llm_ctx"] == 131072
    assert rec["kv_cache_type"] == "q4_0"
    assert rec["note"] == ""


def test_gpu_24gb_recommends_max():
    gpu = {"name": "GPU", "vram_total_mb": 32768, "vram_free_mb": 30000}
    rec = recommend_context(gpu, {"total_mb": 65536, "available_mb": 40000})
    assert rec["llm_ctx"] == 262144
    assert rec["model"] == "9B"


def test_gpu_8gb_small_model_with_warning():
    gpu = {"name": "GPU", "vram_total_mb": 8192, "vram_free_mb": 4000}
    rec = recommend_context(gpu, {"total_mb": 16384, "available_mb": 8000})
    assert rec["supported"] is True
    assert rec["model"] == "4B"
    assert rec["llm_ctx"] == 32768
    assert "strongly recommended" in rec["note"]


def test_gpu_below_8gb_unsupported():
    gpu = {"name": "GPU", "vram_total_mb": 6144, "vram_free_mb": 3000}
    rec = recommend_context(gpu, {"total_mb": 16384, "available_mb": 8000})
    assert rec["supported"] is False
    assert "below 8 GiB" in rec["reason"]


def test_cpu_mode_by_memory():
    rec = recommend_context(None, {"total_mb": 32768, "available_mb": 20000})
    assert rec["supported"] is True
    assert rec["llm_ctx"] == 32768
    assert rec["source"].startswith("CPU-only")
    rec2 = recommend_context(None, {"total_mb": 16384, "available_mb": 8000})
    assert rec2["supported"] is True
    assert rec2["llm_ctx"] == 16384


def test_cpu_below_minimum_unsupported():
    rec = recommend_context(None, {"total_mb": 8192, "available_mb": 4000})
    assert rec["supported"] is False


def test_minimum_context_floor():
    gpu = {"name": "GPU", "vram_total_mb": 6144, "vram_free_mb": 4096}
    rec = recommend_context(gpu, {"total_mb": 16384, "available_mb": 8000})
    assert rec["supported"] is False
    assert rec["min_llm_ctx"] == MIN_LLM_CONTEXT


def test_validate_below_minimum_fails():
    v = validate_context(4096)
    assert v["ok"] is False
    assert "below the minimum" in v["warning"]


def test_validate_at_or_above_minimum_passes():
    assert validate_context(MIN_LLM_CONTEXT)["ok"] is True
    assert validate_context(65536)["ok"] is True
