# Changelog

All notable changes to OpenLAD will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-07-17

First open-source release.

### Added

- Fully offline document Q&A system with local LLM inference
- Multi-format document ingestion (PDF, Word, Excel, PowerPoint, images, Markdown, HTML, TXT)
- Hybrid retrieval: FTS5 (trigram) + sqlite-vec vector search + LLM-driven planning
- Agentic search pipeline: Plan → Retrieve → Rerank → Synthesize
- Document intelligence: Grobid metadata extraction, VLM chart analysis, MinerU structure parsing
- Multi-tenant architecture with isolated databases and vector spaces per tenant
- Web-based admin panel and user Q&A interface
- Industry pack plugin system with 1 complete sample pack (Semiconductor) and 3 empty templates
- BYO-LLM architecture: support for llama.cpp, Ollama, vLLM, or any OpenAI-compatible API
- MIT License

### Security

- bcrypt password hashing
- API Key authentication
- Role-based access control (admin/user)
- Tenant data isolation
