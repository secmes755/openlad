# OpenLAD Model Benchmark

This document reports evaluation results of open-source LLMs used as the
reasoning backend of OpenLAD, a document-grounded retrieval-augmented
generation (RAG) system. All results are produced by OpenLAD's built-in
evaluation harness, which runs a fixed set of factual-extraction tasks
(entity identification and numeric retrieval) against a heterogeneous
document corpus.

The page is intended to be updated incrementally: each new benchmark run
appends to the history table below, reflecting both OpenLAD's own evolution
and the changing landscape of open-source models.

---

## Latest Results (2026-08)

Reference version: **OpenLAD `bb8a5af`** · built-in evaluation harness v1
(per-case grading: deterministic checks as primary verdict, LLM semantic
review as fallback where string matching is insufficient).

| Model | Params | Quant | Accuracy | End-to-end runtime | Notes |
|---|---|---|---|---|---|
| DeepSeek-V4-Pro-Qwen3.5-9B (MTP) | 9B | Q5_K_M | **98.0%** | ~19 min | Best accuracy/throughput trade-off; MTP speculative decoding |
| Qwen3.5-9B | 9B | Q5_K_M | 96.1% | ~25 min | Strong generalist on factual extraction |
| Qwen3.5-4B | 4B | Q4_K_M | 92.2% | ~16 min | Fastest; suited for latency/cost-constrained deployments |
| Qwen3.8-27B | 27B | Q4_K_M | 90.2% | ~53 min | Largest model; no accuracy gain on this task set |

*Hardware note: single consumer GPU, 24 GB VRAM (AMD Radeon RX 7900 XTX),
64 GB system RAM, ROCm software stack. Context 114688 tokens, 4-bit KV cache,
flash attention. All models served locally via llama.cpp.*

## Analysis

- **9B-class models deliver the best accuracy/throughput ratio.** The
  difference between the top two 9B entries is ~2 points, within the typical
  variance of local quantized inference; the MTP variant adds a measurable
  throughput advantage at identical accuracy.
- **Larger scale does not automatically improve accuracy.** The 27B model
  trails the 9B-class models on this task set while costing ~3x the runtime,
  suggesting that factual-extraction accuracy is dominated by retrieval
  quality and answer organization rather than raw parameter count.
- **Quantization level matters less than model architecture.** Q5_K_M vs
  Q4_K_M at the same size shows a small but consistent edge; both are viable
  for production.
- **Failure modes are task-shaped, not size-shaped.** Residual errors cluster
  around numeric/scale confusions and incomplete entity answers, and occur
  across all model sizes — an important input for downstream prompt and
  retrieval tuning.

## Model Selection Recommendations

| Use case | Recommended model |
|---|---|
| Default / best accuracy | **DeepSeek-V4-Pro-Qwen3.5-9B (MTP)** |
| Low latency / constrained hardware | Qwen3.5-4B |
| Not recommended on current task set | Qwen3.8-27B (higher cost, no accuracy gain) |

## Revision History

| Date | OpenLAD version | Evaluation harness | Scope |
|---|---|---|---|
| 2026-08-22 | `bb8a5af` | v1 (per-case grading) | 4 models, full evaluation set |

---

*For reproduction details of the harness and task definitions, see the
OpenLAD repository documentation.*
