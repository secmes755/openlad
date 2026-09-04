# Architecture

[English](architecture.md) | [中文](zh-CN/architecture.md)

## System overview

```
Browser (LAN) ─── http://<host>:11296/ ───┐
                                          │
┌─────────────────────────────────────────▼──────────────────────────┐
│                        OpenLAD API (FastAPI)                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  Query   │  │ Document │  │  Admin   │  │  Tenant Manager  │  │
│  │  Router  │  │ Ingestion│  │  Panel   │  │  (SQLite, per-   │  │
│  │          │  │          │  │          │  │   tenant DB)      │  │
│  └────┬─────┘  └──────────┘  └──────────┘  └──────────────────┘  │
│       │                                                            │
│  ┌────▼──────────────────────────────────────────────────────┐    │
│  │                 Retrieval Pipeline                         │    │
│  │  Planner → Executor → Retriever → Merger → Synthesizer    │    │
│  │  (FTS5 + sqlite-vec hybrid)  (LLM answer generation)      │    │
│  └───────────────────────────────────────────────────────────┘    │
└──────────────────────────┬──────────────────┬────────────────────┘
                           │                  │
              ┌────────────▼──┐    ┌──────────▼─────────┐
              │  LLM Service  │    │  Embedding Service  │
              │  llama-server │    │  llama-server       │
              │  :8080        │    │  :8081              │
              │  Qwen3.5-9B   │    │  Qwen3-Emb-0.6B     │
              └───────────────┘    └─────────────────────┘
```

## Query pipeline

Every question goes through three phases:

1. **Plan** — the LLM analyzes the question and decomposes it into retrieval
   steps (which documents, which sections, what to compare).
2. **Retrieve** — hybrid search over the tenant's document store: FTS5
   full-text search for exact term matching plus sqlite-vec vector search for
   semantic recall, merged and ranked.
3. **Synthesize** — the LLM generates the final answer from the retrieved
   evidence, with source citations.

The pipeline is LLM-driven: the core contains no hardcoded domain rules.

## Industry packs

Domain knowledge lives outside the core. An *industry pack* is a directory of
YAML rules (vocabulary, entity aliases, query hints) loaded through the
`RetrievalPlugin` hook at query and ingestion time. Packs are discovered from
`industries/` plus any extra directories in `OPENLAD_INDUSTRIES_DIRS`.

The repository ships one complete sample pack (Semiconductor) and three empty
templates (Legal, Financial, Generic) — copy a template to build your own.
This layering keeps the core industry-neutral: generic mechanisms live in
`core/`, domain vocabulary lives in packs.

## Multi-tenancy

Each tenant gets isolated SQLite databases and vector spaces under the data
directory. Users belong to exactly one tenant; usernames are globally unique.
The admin panel (`/admin`) manages users, API keys, documents, and model
endpoints.
