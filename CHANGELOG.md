# Changelog

All notable changes to OpenLAD will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Unified local/cloud model backends (OpenAI-compatible), configurable at
  runtime.** The admin panel gains a "Model Services" tab: set URL, API
  key, and model name for the LLM and for embeddings, test the endpoint
  before saving (lists available model ids from `/models` to copy exact
  names), and apply — hot reload, no process restart. Local backends
  ignore keys, so an unset key resolves to a well-formed placeholder
  ("123") and one code path serves both. Resolution order per field:
  admin-saved value > environment variable > built-in default; an API key
  is never returned by the config API (masked to `set`/`hint`), and
  saved cloud keys live in the local system DB by design of this
  LAN-deployed tool.
- New endpoints (admin-gated): `GET/PUT /api/v1/admin/models/config`,
  `POST /api/v1/admin/models/test`. `Authorization: Bearer <key>` is now
  sent on every model call (`core/models/client.py`); `/health` and the
  status bar follow the runtime-configured endpoints, not startup env.
- **Windows deployment support.** `start.ps1` / `stop.ps1` mirror the bash
  entry points (same environment defaults, venv detection at
  `.venv\Scripts\python.exe`, port-based stop). All requirements ship
  Windows wheels, sqlite-vec loads via the standard
  `enable_load_extension` sequence, and `python main.py` runs unchanged.
  Optional native components (poppler for page rendering, tesseract for
  OCR) degrade gracefully when absent; text-extractable PDFs are
  unaffected.

### Fixed

- Chat rendering is resilient to a missing sanitizer deployment: without
  the vendored sanitizer the answer degrades to plain text instead of
  raw HTML (fail closed). Page-citation badges no longer carry inline
  script attributes — they use data attributes with one delegated click
  listener.
- Corrected two garbled checkbox labels on the model-services tab.
- **Auto-detection language gap.** The LLM classifier emits categories in
  the language of the pack's `taxonomy.yaml` (Chinese for the sample
  semiconductor pack), while `manifest.category_mapping` is English.
  Plugin resolution (`get_plugin_by_category`,
  `resolve_plugin_for_categories`) matched mapping keys only, so
  auto-ingested documents classified in Chinese silently lost their
  industry pack — spec-fact extraction ran vocabulary-less and query-time
  pack routing missed. Matching now covers each pack's full key set
  (mapping + taxonomy names, exact before fuzzy).
- Documents auto-ingested before the language-gap fix may lack pack spec
  facts; delete and re-upload them to rebuild (re-ingestion is
  idempotent).
- Admin upload form: "Industry Classification Mode" label and the
  "Manual Select" option were missing i18n wiring (identical text in both
  languages); the Embedding "Test" button now translates too. i18n assets
  bumped to v4.
- Data-dir configuration: an env-provided path is now compared as a
  parsed path instead of a raw string, so a POSIX-style custom
  `OPENLAD_DATA_DIR` resolves identically across platforms.

## [0.4.6] - 2026-08-27

### Added

- **Authenticated tenant-scoped image serving.** Ingestion images
  (page renders, chart crops) are served via `GET /images/{filename}`,
  which requires a valid session and resolves files only under the
  caller's own tenant directory. The web UI loads them as
  authenticated blob URLs, so citation badges, chart thumbnails, and
  page-image links work for the first time.

### Security

- **Tenant images no longer bypass authentication.** The static-asset
  auth exemption now excludes `/images/*`; a filename whitelist blocks
  path traversal, and cross-tenant requests return 404.
- **Rendered answer markdown is sanitized** with vendored DOMPurify,
  and HTML escaping now also covers quotes (attribute-safe).

### Fixed

- **Web UI wiring reconnections.** The Logs modal reads the real
  `/services/events` endpoint (admin-gated, hidden for non-admin
  users); chat sessions are created lazily on first message instead of
  eagerly; non-OK chat responses surface as errors instead of silent
  empties; logout/401 handling preserves the language preference;
  i18n language switches no longer wipe icon elements; assorted
  malformed i18n markup corrected; removed an unused legacy auth
  script.

- **Bookmarkless-document structure index.** Text-rule structure building
  for documents without embedded bookmarks/TOC now filters junk headings
  (table rows, year runs, page-header stitch lines such as
  "Chapter 5 Chapter 5", body-text fragments) and guarantees the coverage
  invariant: every page belongs to at least one `[start_page, end_page]`
  interval, so sections truncated by junk headings no longer leave page
  ranges (e.g. consolidated financial statements) structurally
  unreachable by chapter-scoped retrieval. The same section path may now
  span multiple discontinuous page ranges (composite identity key), and
  section expansion in the retriever sees every segment instead of only
  the last one. **Re-ingest your bookmarkless documents to benefit** —
  existing databases keep their previously extracted (junk-tainted)
  structure indexes until the document is deleted and uploaded again.

## [0.4.5] - 2026-08-24

### Added

- **Explicit industry-pack selection.** The query API accepts an optional
  `industry` field carried end-to-end (API → engine → synthesizer).
  Explicit selection wins over category routing and text detection; when
  omitted, the existing detection fallback chain is unchanged.
- **Generic base pack with runtime composition.** A new built-in
  `industries/generic` pack carries universal, cross-industry document
  knowledge (bilingual numbering and magnitude words, physical and
  financial units, structural reference terms, conservative answer
  discipline). Every resolved pack — explicit, routed, or detected — is
  layered over this base at runtime: list hooks merge as unions and dict
  conflicts resolve in favor of the industry pack. The generic pack
  deliberately leaves retrieval-shaping hooks (query expansion, low-value
  sections, spec sections, entity patterns) empty, so default retrieval
  behaviour is unchanged.
- Industry packs can declare evidence-anchor patterns through the
  `get_evidence_anchor_patterns()` plugin hook, and the self-check
  evidence sampler caps anchors via the new `context_extract_max_keywords`
  config knob — no domain word lists remain in core.

### Fixed

- Retrieval: exact-match chapter pages are pinned above the
  context-budget cut, so a page containing the queried term can no longer
  be truncated away by higher-scoring but less specific pages.
- Page text storage: `save_page` no longer omits the `raw_text` column on
  empty extraction (previously persisted NULL, e.g. from VLM-degraded
  chart pages), and `SearchResult.content` is coerced to a string at
  construction — the content-is-always-str invariant now holds at both
  boundaries, so the chapter-retrieve context quota loop cannot crash on
  `len(None)`.
- Section entity harvesting: `harvest_section_entities` had no function
  body (the implementation was misplaced into the acronym helper), so
  per-section entity lists silently never reached the chapter index. The
  body is restored and the helper slimmed to its own responsibility.
- Document metadata upsert: saving an existing document used
  `INSERT OR REPLACE`, which deleted and re-inserted the row — resetting
  `created_at` and wiping columns not present in the update (skill tags,
  permissions, content flags). Updates now merge only the supplied columns.
- Retrieval (comparison path): spec-fact hits now run before the
  empty-retrieval guard and count toward its context total, so an
  authoritative fact can still answer when page retrieval returns nothing
  — matching the traditional path's first-class assertion layer.
- Synthesis: the comparison and cross-reference answer branches (pure
  passthroughs) now forward `original_query` and the explicit industry
  pack id, so table-detection and language instructions keep using the
  original user query instead of the planner-rewritten one.
- Audit: the Agent skill query endpoint now records query-log entries
  with user id and intent, matching the chat endpoint — Agent-channel
  queries previously left no audit trail.
- Plugin registry: the `taxonomy` field is now exposed via
  `list_plugins()` (base class default `{}`, YAML plugins read it from
  their shared config), so classifier consumers no longer read a dead key.
- Context-budget fallbacks aligned with the configured defaults
  (60000 / 35500), so deployments without explicit config get the
  intended budgets instead of stale values.

## [0.4.1] - 2026-08-18

> **Upgrading from 0.4.0 or earlier: re-ingest your documents.** Existing
> databases remain fully compatible (schema migrations are additive), and
> the query-side fixes apply to old data immediately. But several fixes in
> this release change what is stored at ingestion time — spec-fact
> extraction now runs after classification, AI-scaffold junk is stripped
> before it enters the fact table, wrapped source sentences are joined,
> and bookmarkless documents get a usable chapter index. Documents
> ingested by older versions keep their old extraction results — including
> junk spec facts that would continue to be injected into answers as
> authoritative — until they are deleted and uploaded again.

### Added

- `OPENLAD_INDUSTRIES_DIRS` environment variable appends external industry
  pack scan directories, so closed-source packs can live outside this repo
  and load without code changes (colon-separated).
- Retrieval: FTS queries are expanded with synonyms declared by the active
  industry pack (`spec_query_terms` in pack rules), so pages that phrase a
  fact differently can be recalled. Pack-declared terms only; deployments
  without industry packs see zero behavioural change.
- Answers backed by authoritative spec facts now append a deterministic
  "Source excerpts" section: the verbatim source line of every injected
  fact, with page number and document title, assembled mechanically after
  generation (never re-worded by the model). Controlled by
  `spec_facts_appendix` (default on, env override
  `OPENLAD_SPEC_FACTS_APPENDIX`).
- Spec-fact injection blocks can present the verbatim source sentence first
  (`spec_facts_presentation: "source_first"`, new default; env override
  `OPENLAD_SPEC_FACTS_PRESENTATION`). The previous flattened
  attribute/value enum remains available as `"value_first"`.

### Fixed

- Synthesis context budget (39000 -> 35500) now stays under the model
  client's safe prompt limit together with the real template overhead.
  Previously every query silently truncated the tail of the retrieved
  context, which could drop exactly the pages holding the answer and made
  answers flip between runs.
- Text-rules structure extraction: numbered headings with ideographic
  commas ("40、...") are recognised, and the structure index save no longer
  silently drops every section when only `path` (no `short_path`) exists —
  bookmarkless Chinese annual reports now get a usable chapter index.
- Spec-fact assertion layer no longer extracts structural noise from
  documents when no industry pack provides spec vocabulary (annual reports
  accumulated thousands of junk "facts" that were later injected into
  answers as authoritative).
- OCR resource release tolerates torch import/runtime failures instead of
  aborting document ingestion.
- PDF parsing fallback (MuPDF, for files pdfplumber/pypdf reject) now
  preserves page boundaries instead of merging the whole document into a
  single page, keeping page-level retrieval and structure indexing working
  for corrupted PDFs.
- Query planner entity coverage filters generic Chinese query-noise words
  (公司/报告/营业收入/多少/...), so they no longer force-merge unrelated
  documents into the retrieval filter (a cross-document contamination
  variant).
- Spec-fact extraction now runs after document classification, resolving
  the industry plugin from the classified category (the same category→pack
  matching used by query-time routing). Since extraction vocabulary moved
  into industry packs, documents that no upload hint or detect hook claims
  (e.g. datasheets) were ingested with zero spec facts, silently disabling
  the authoritative-fact channel for them.
- Spec-fact source excerpts no longer end mid-sentence at PDF line wraps:
  an extracted line ending in a conjunction or comma is joined with the
  following line (300-char cap), so queries can match the wrapped tail of a
  sentence (e.g. "...backward compatible with the PCIe2.1 and | PCIe1.1
  protocol").
- Spec-fact matching now measures each query keyword's selectivity against
  the document's own fact table: a keyword whose hits span more distinct
  attributes than `spec_facts_selectivity_max_attrs` (default 3) carries no
  discriminating power — generic verbs appear in the source line of nearly
  every "Support X" fact — and is dropped from scoring. Entity-vocabulary
  tokens are exempt. Controlled by `spec_facts_selectivity_guard` (default
  on, env override `OPENLAD_SPEC_FACTS_SELECTIVITY_GUARD`).
- Fact extraction now strips both AI-generated block formats appended to
  page text (the page-level visual-analysis block and the chart-analysis
  block). The chart block's scaffold labels previously matched the
  key-value extraction pattern and entered the assertion table as junk
  facts.

## [0.4.0] - 2026-08-13

### Added

- Docker deployment: single-container API image (`Dockerfile` +
  `docker-compose.yml`). Model services stay external (llama-server / vLLM /
  Ollama on the host, or any cloud OpenAI-compatible endpoint); the container
  is CPU-only and persists data via a volume.

### Removed

- PaddleOCR dependency and engine path removed; scanned-document OCR now uses
  the multimodal VLM path (with optional Tesseract). Smaller footprint, one
  less heavy runtime dependency.

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
