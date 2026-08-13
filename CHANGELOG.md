# Changelog

All notable changes to OpenLAD will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-08-13

### Added

- Auto-derived document titles with a priority chain: explicit title on the
  upload API > structured LLM extraction (subject/year/doc_type) from the L1
  summary with anti-hallucination validation (each field must appear in the
  source text) > filename-derived fallback.
- Spec-fact injection widened: facts grouped by entity and injected on every
  retrieval path, with the extractor vocabulary living in industry packs.
- Local CI gate (`scripts/ci_gate.sh` wired to a pre-push hook): reproduces
  the CI minimal-dependency environment (lint with pinned ruff + unit tests)
  before any push reaches main, so the branch never turns red.

### Fixed

- Admin page shell is public again: `/static/admin.html` no longer requires
  Authorization (browser top-level navigation never sends it), while every
  API call the page makes stays authenticated.
- TOPS unit strictness in the spec extractor; frequency extraction is neutral
  and no longer conflated with other units.
- Title derivation decoupled from the builder into a stdlib-only module
  (`core/ingestion/title_deriver.py`) so unit checks import cleanly under the
  CI minimal-dependency environment.

### Changed

- Extractor vocabulary (spec headers / compute units / frequency terms) moved
  from core to industry packs; core stays generic.

## [0.3.0] - 2026-08-12

### Added

- Login sessions: each login issues its own API key; logout revokes only the
  current session, so other devices logged in with the same username stay
  online. Account-level revocation is done by an admin rotating the key.
- Hardware probe (`python -m core.services.system_probe`): detects GPU VRAM /
  system memory and recommends a model + context configuration. The quick
  lookup covers 8 GB (4B model, limited capability) up to 24 GB+ (9B model,
  full context); 16 GB with the 9B model is the recommended configuration.
  Machines below 8 GiB VRAM / 16 GiB RAM are reported as unsupported; the
  minimum usable context is 16384 tokens.

### Fixed

- Removed the last admin-tenant fallback references in the planner and
  executor (document listing for the admin tenant no longer merges the
  "default" tenant's documents).
- Chapter selection for very large documents (800+ chapters, e.g. annual
  reports): sending every chapter with its full summary could exceed the
  model context window, truncating the list and hiding the exact chapter the
  query needed. All chapter titles are now sent, with full previews only for
  semantically pre-selected chapters.
- Chapter merge no longer unions every pre-selected chapter into the final
  set (dozens of chapters blew the synthesis context budget and could drop
  the exact page with the answer); LLM picks take priority with a capped
  semantic supplement.
- Removed the query-cache half-implementation (disabled LRU/TTL cache with a
  stale comment) instead of shipping it as dead code.
- CI ruff baseline is now machine-independent (repo-relative paths) and the
  ruff version is pinned.
- Spec-fact assertion layer is wired into every retrieval path (traditional
  + agentic/decomposed): comparison queries can no longer answer with
  page-level asymmetries or authoritative-sounding denials, and rewrite
  collapse is guarded.
- Spec-fact injection is scoped by the assertion index's own entity
  vocabulary — unrelated entities' facts no longer leak in when the
  query-named entity has no facts of its own.
- Spec-fact extractor fixes: lookaround-based chip-model regex (clean
  entities even from UUID-prefixed filenames), versioned protocol support
  declarations (e.g. "Support PCIe3.1(8Gbps) ... backward compatible"), and
  "controllers?" as a countable unit.
- Small documents keep all pre-selected chapters instead of trimming to a
  fixed budget.

### Changed

- `logout` no longer revokes the whole account — it revokes only the session
  key used by the current request.
- Industry vocabulary and answer rules moved out of `core/` into industry
  packs (`rules.yaml` / `prompts.yaml` via `RetrievalPlugin` hooks): core
  keeps only domain-neutral mechanisms, and pack resolution is scoped per
  query with content-grounded pack detection.
- Answer-path LLM temperature pinned to 0 (final synthesis + agentic
  retrieval steps; ingestion temperatures untouched) for stable factual
  answers. Verified by a repeat-3 A/B on the single-fact suite: stable
  failures dropped from 1 to 0 and two flaky cases turned fully green.
- README deployment section documents the hardware lookup table and notes
  that 8 GB VRAM is theoretically usable but 16 GB with the 9B model is
  strongly recommended.

## [0.2.0] - 2026-08-07

### Added

- Global unique usernames — cross-tenant duplicate names are rejected (login ambiguity eliminated)
- Synthetic-data checks for retrieval and ingestion logic (public unit suite now 35 cases)
- Ruff baseline mechanism for `core/`: existing violations tolerated, new ones fail CI
- Quality cleanup tracker (`docs/quality-cleanup.md`)

### Fixed

- Security hardening batch:
  - `logout` now revokes the current API key (old key invalid immediately)
  - admin page (`/admin` and `/static/admin.html`) requires authentication
  - `/api/v1/industries` requires authentication
  - tenant deletion cascades user cleanup
  - usernames are globally unique (409 on duplicates)
  - removed dead `check_permission` code
- `create_user` no longer reports success when the DB unique index rejects the row
- Duplicate `map_reduce_chunk_size` key in config (dead 12000 value removed)
- Ruff cleanup of `core/`: 1410 → 17 violations (remaining are intentional sys.path imports)
- Removed the admin-tenant cross-tenant read fallback — tenants are strictly isolated;
  the admin tenant can only query its own data (cross-tenant access requires that
  tenant's own API key)
- Removed the disabled query-cache half-implementation

### Notes / Trade-offs

- **Usernames are globally unique** (login never needs a tenant identifier, and
  same-name users cannot exist across tenants). A side effect: creating a user
  with a name that exists elsewhere returns 409, which reveals that the name is
  taken (but not where/who). Acceptable for admin-managed user creation.
- **Logout revokes the account's API key**: with one key per user, logging out on
  one device invalidates sessions on all devices. The next login issues a fresh key.

## [0.1.0] - 2026-08-07

First open-source release.

### Added

- Fully offline document Q&A system with local LLM inference
- Multi-format document ingestion (PDF, Word, Excel, PowerPoint, images, Markdown, HTML, TXT)
- Hybrid retrieval: FTS5 (trigram) + sqlite-vec vector search + LLM-driven planning
- Agentic search pipeline: Plan → Retrieve → Rerank → Synthesize
- Document intelligence: metadata extraction, VLM chart analysis, structure parsing
- Multi-tenant architecture with isolated databases and vector spaces per tenant
- Web-based admin panel and user Q&A interface
- Industry pack plugin system with 1 complete sample pack (Semiconductor) and 3 empty templates
- BYO-LLM architecture: support for llama.cpp, Ollama, vLLM, or any OpenAI-compatible API
- API key lifecycle management (TTL, rotation, rate limiting)
- GitHub Actions CI (lint + unit checks) and multi-document local verification script
- MIT License

### Security

- bcrypt password hashing
- API Key authentication with expiry and rotation
- Login rate limiting (username + IP, no account lockout)
- Unique username constraint
- Role-based access control (admin/user)
- Tenant data isolation
