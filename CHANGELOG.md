# Changelog

All notable changes to OpenLAD will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Only industry vocabulary opens the spec-fact table, and a fact is only
  attributed to a real entity.** Two ways the assertion index took on content it
  could not support:
  - *Units counted as vocabulary.* The guard that decides whether a document may
    be mined for facts accepted a unit list as industry vocabulary. The generic
    pack ships SI units (V, mV, A …) for every industry and nothing else, so any
    document it applied to qualified, and the pack-independent colon-header
    pattern then read the prose that was there — `Less: Corporate income tax
    143,692` became a fact with attribute `Less`, and on a PDF whose text layer is
    font glyph codes `(cid:6813)` became one with attribute `cid`. Units are what a
    value is measured *in*, not a vocabulary; only declared industry vocabulary
    (spec headers, frequency terms, support objects, codec literals, the compute
    attribute name) enables extraction. Measured on a datasheet corpus the
    units-only configuration produced 241 facts by itself; the pack-driven
    extraction over the same pages is unchanged (312 facts, before and after).
  - *Document labels posed as entities.* `infer_doc_entity` fell back to the
    cleaned title/filename when no pack pattern matched, so facts were labelled
    `<uuid>_2024` / `..._Roc` and those labels entered the entity vocabulary that
    scopes fact injection — a vocabulary of document labels can never match a
    query entity, so the scoping looked active while it could not scope anything
    (26% of one corpus's facts carried such labels). An unknown entity is now
    empty; the vocabulary query already excludes empty entities. Inference also
    sees the caller's title now, not only the filename, so a corpus with opaque
    filenames can still resolve an entity from its title.
- **Spec facts are no longer built out of a text layer that is font glyph codes.**
  Fonts without a ToUnicode map make a reader render glyph indices as `(cid:NNN)`
  — ordinary ASCII, so the garbled-character checks pass them (they look for
  replacement and control characters). Worse, `(cid:4303)` has the shape of an
  attribute/value pair, so the structural extractor took `cid` as the attribute
  and the number as the value, and its self-verification ("the value must appear
  in the source line") was satisfied by the glyph token itself — that is how a
  tenant's fact table ended up 29% glyph noise. A page whose text is mostly these
  glyphs is now skipped entirely, a fact whose own source line is mostly glyphs is
  dropped, and either case marks the document degraded (`ingest_warnings`) so
  retrieval reports the sources as incomplete. Measured on the corpus that
  prompted it: all 258 such facts are blocked (245 by the page gate, 13 by the
  fact gate) and no fact from a readable page is affected.
- **CI now installs the dependencies it actually imports, from a single source.**
  The unit job ran `pip install` with a hand-maintained list that was missing
  `numpy` and `Pillow` — both declared in `requirements.txt` and both imported at
  module level by five modules under `core/` (`agentic_retriever`,
  `layout/{chart_analyzer,layout_analyzer}`, `preprocessing/{__init__,image_corrector}`
  and `ingestion/builder`). On a runner image that did not happen to provide them,
  `pytest` could not even collect tests that import those modules and the job died
  with `ModuleNotFoundError`; on an image that did, the same commit passed. The
  dependency set now lives in `requirements-ci.txt`, installed by the workflow and
  read by the local gate, so the two cannot drift apart again.
  `tests/test_ci_dependency_completeness.py` parses `core/` and fails if a
  module-level import is missing from that file, so this cannot come back quietly.
- **CI no longer depends on which runner image it lands on.** Both jobs ran on
  `ubuntu-latest`, which rolled from image `20260831.293` (Ubuntu 24.04.4) to
  `20260907.300` (24.04.5) inside one afternoon; the two images carry different
  preinstalled packages, so identical commits passed or failed depending on the
  runner they landed on. Jobs are pinned to `ubuntu-24.04` and install their own
  dependencies.

### Changed

- **The query hot path no longer re-lists the document table or scans it.** Two
  costs, both work the request did not need to do:
  `RetrievalExecutor._resolve_doc_filter` loads up to 10k documents to map filter
  terms to document ids, and it was reached once per step in two passes over the
  same plan (the quota pass and the step pass), so a request with a repeated filter
  paid for that listing several times. It is now resolved once per request — the
  memo is scoped to a single `execute()` call on purpose, because the executor is
  cached per tenant and reused, so a longer-lived cache would go stale after an
  upload and hide the new document from filters. Separately, the planner's document
  query (`status IN ('verified','degraded') ORDER BY created_at DESC`) planned as
  `SCAN DOCUMENTS USE TEMP B-TREE FOR ORDER BY` on every query; `documents` now
  carries `idx_documents_status` on `(status, created_at)`.
- **Chunks are written to both databases in batches instead of one connection per
  chunk.** `save_chunk` and `store_l2_chunk` each open a connection and commit, and
  the builder always stores a whole embedded batch at once, so a long document paid
  two connections plus two fsyncs *per chunk* — the ingestion bottleneck on large
  PDFs. New `save_chunks` / `store_l2_chunks` write a batch in a single transaction
  (the metadata one keeps its FTS rows, so the chunks stay searchable), and the
  builder falls back to the per-chunk path when a batch fails — so one bad row still
  cannot cost its neighbours, and the per-chunk failure counters are unchanged. A
  failed batch is rolled back whole, so the fallback cannot duplicate what it
  rewrites.

### Added

- **Contract tests for the retrieval executor and the agentic retriever**, the two
  modules the audit found with no coverage at all (1214 and 651 lines). They pin
  what callers observe rather than internal detail: which strategy each plan shape
  dispatches to, the per-request tenant switch, the step-quota arithmetic, the
  result shape of both the standard and agentic paths, the agentic index load, the
  document-selection rules (a model named in the query is searched even when the
  model selects nothing) and the early exit once a document answers. The net was
  mutation-checked: six deliberate breaks of those behaviours (default strategy,
  tenant rebuild, quota cap, result accounting, force-include, early exit) each
  make a specific test fail.

### Removed

- **Dead retrieval and sample-pack code, plus the superseded schematic prompts.**
  Removed unreachable helpers in the retrieval stack
  (`RetrievalExecutor._extract_step_data`, the no-op `reload_overview` on both
  the executor and the planner, `HierarchicalRetriever._path_to_url`,
  `AgenticRetriever._verify_pages_have_answer`/`_extract_notes`,
  `QueryPlanner._load_taxonomy`) and in the semiconductor sample pack
  (`_llm_classify`, `_parse_power_page`, `_parse_pinmux_page`,
  `find_power_supply`/`find_pinmux`/`find_net`). The six per-page-type schematic
  prompts those functions used are removed as well: they belong to the
  specialised path that the generic `PARSE_GENERIC_SCHEMATIC_PROMPT` extraction
  superseded, and were never called. Also drops the `DiagnosticResponse` model
  (its field models were removed above) and the imports left unused.
- **Dead database, API and service code (~400 lines).** Removed the duplicated
  `upload_tasks` implementation in `TenantDB` (table, both indexes and five CRUD
  methods: the only callers go through the `SystemDB` copy, and the two had
  already drifted apart), seven unused `TenantDB` methods including the
  127-line superseded `search_fts` (chunk search runs through
  `search_fts_chunks`), four unused `SystemDB` methods,
  `restore_interrupted_tasks` on both sides — the "resume uploads after a
  restart" hook was never wired up — five unused Pydantic request/response
  models, the unused `TenantContext` path helpers, `AuthManager.revoke_all_sessions`,
  `ServiceManager.log_event`, `ResourceCapacity.get_snapshot`, and the two
  imports those deletions left unused.
- **Dead ingestion code (~600 lines across 9 files).** Unreachable code removed:
  the disabled LLM full-structure-analysis chain in `builder.py` (its call site
  logs "LLM full analysis disabled", so the path could never run), the legacy
  `_optimize_table_format` whose call site was already commented out,
  `_chunk_by_page_boundary`, the `title_deriver` delegating wrappers
  `_generate_identifiable_title`/`_subject_in_text`, `parser._classify_pdf_page`,
  plus unused helpers in the layout, formula, chart and preprocessing modules,
  and `ModelClient.generate_json_array` / `ModelClient.health_check` (the only
  caller of the former was the dead structure-analysis chain). Every symbol was
  confirmed unreachable by call-graph analysis and by an independent whole-repo
  search; the JSON-array parameter plumbing on the client is deliberately kept,
  since it is the provider capability rather than application logic.
- **Dead module: `core/ingestion/pdf_watermark_remover.py` (747 lines).** It was
  never imported by any code path, test, script, configuration or document —
  page-level watermark handling in ingestion is done by the text sanitizer in
  `builder.py`, which is unaffected. Located by call-graph reachability
  analysis and confirmed by an independent whole-repo name search (the module's
  symbols occur nowhere but inside the file itself).

### Fixed

- **The answer self-check is now a decision rather than an accident of attribute
  naming.** Its gate read `getattr(industry_pack, "name", "generic") != "generic"`,
  but no plugin class has ever had a `name` attribute — pack identity lives on
  `manifest.id` / `manifest.name` — so the default always won and the check could
  never run. The composed plugin's docstring even recorded that state as
  intentional ("must stay disabled until its own fix lands"). The gate now reads
  `manifest.id` together with an explicit `self_check_enabled` config knob that
  defaults to **off**: behaviour is unchanged and the check's known cost (one extra
  LLM round trip per answer) stays opt-in, but enabling it is now a configuration
  change instead of unreachable code. The generic base pack is never checked — it is
  composed under every pack and carries no domain rules to enforce.
- **The agentic retriever's per-request index is actually released.** `AgenticRetriever`
  is built per deep-research query and loads the tenant's entire vector index into
  memory, but `release()` only logged a line — the index stayed referenced until the
  garbage collector reached it — and the engine's failure path skipped the call
  altogether, so a query that failed part-way held that memory with nothing to drop
  it. The release now clears the index and catalog and runs from a `finally`, so it
  covers the success and failure paths alike. The per-request rebuild itself is
  unchanged: caching it per tenant would trade load cost for index staleness, which
  remains an open decision.
- **Failures in the retrieval layer's industry-pack hooks are no longer silent.**
  Six handlers caught an exception and continued with a degraded result — the
  pack's query vocabulary, its retrieval rules, its spec terms — behind a bare
  `pass` or a debug-level line, so a pack that could not be consulted was
  indistinguishable from a pack with nothing to contribute. Each now logs, at
  warning where answer quality is affected (including the rule loader, which lost
  every rule while reporting only at debug) and at debug where the path is already
  degraded or the call is best-effort by design.
- **A model reply that could not be parsed as JSON is now distinguishable from an
  empty one.** `generate_json` returns `{}` both when the model genuinely answered
  `{}` and when its reply was unreadable, and the ~13 call sites cannot tell the
  two apart: a planner whose reply failed to parse looked exactly like a planner
  that found no candidates, so retrieval quietly ran with less than it should have.
  The failure is now also recorded on `client.last_json_error` (None on success, a
  reason otherwise), mirroring the existing `last_finish_reason` convention. The
  return value is unchanged, so no caller had to be touched; reacting to the flag
  is a per-caller decision and is deliberately left for the callers whose fallback
  is a *wrong* answer rather than a degraded one.
- **A page lost during ingestion is now reported instead of silently missing.**
  When a page's analysis raised, `_build_l2` logged it and left the page out of the
  index, after which the document was saved as *verified* — retrieval then had no
  way to know content was missing, so answers could be confidently incomplete.
  Failed pages are collected and emitted through `_collect_ingest_warnings`, the
  channel that already marks a document *degraded* and is surfaced by retrieval, so
  the gap is visible to the user. The sequential path now records failures the same
  way the parallel path did (it previously aborted instead).
  Preprocessing failures were also mis-reported: the page list is index-aligned and
  `_analyze_single_page` indexes into it, so a page cannot simply be dropped without
  shifting every later page's text onto the wrong page number. That path now stops
  with the affected pages and causes named, rather than raising a bare `KeyError`.
- **Rate limiting is scoped to the caller instead of a context that does not exist
  yet.** `RateLimitMiddleware` runs outside `TenantMiddleware` — deliberately, so a
  flood is rejected before any authentication work — but its limit key read the
  tenant from the contextvar that `TenantMiddleware` sets. That context is empty at
  this layer, so the key was always `query:unknown` / `upload:unknown` and every
  caller shared one bucket: a single busy client could exhaust the quota of all the
  others. The key is now derived from the presented credential (hashed so the key
  itself is never stored or logged) and falls back to the client IP. Quotas are
  therefore per credential rather than per tenant, which is the trade-off chosen
  over moving the middleware behind authentication and paying a database lookup on
  every unauthenticated request.
- **The LLM client no longer retries requests that were rejected, and builds one
  session.** `_chat_completion` retried every failure three times, including 4xx
  rejections where the retry sends an identical request that cannot succeed —
  the failure was then reported as "LLM call failed (attempt 3/3)", hiding the
  real cause. Deterministic 4xx now returns immediately with the status and body
  logged, while 408/429/5xx still retry; this reuses the distinction the
  embedding path already made. Separately, the `session` property built its
  `requests.Session` without holding `self._lock` (which existed but was never
  used), so concurrent first use from the ingestion thread pool could create
  several sessions and leak every loser's connection pool.
- **A truncated context can no longer be reported as high confidence.** The
  confidence heuristic looked for an uppercase `[TRUNCATED` marker in the
  synthesized context, but the retrieval side wrote lowercase markers
  (`...[truncated]`, `...[content truncated]`, `... (context truncated)`) and
  three call sites — the standard and decomposed executors and the deep-research
  engine — cut the context with no marker at all, so a cut-short context was
  invisible either way and still scored `high`. Every producer now appends one
  canonical marker from `core/retrieval/truncation.py` and the detector reads it
  through the same module, which also still recognises the older marker shapes.
- **`/health`, `/` and the OpenAPI schema now report the real version.** The
  version was hardcoded as `"1.0.0"` in four places across the API layer while the
  project was releasing 0.4.x, so `GET /health` never named the running release
  and operators could not tell which build was deployed. It now comes from a
  single `core/version.py` constant, bumped by the release commit together with
  the CHANGELOG entry.
- **Upload task history is no longer wiped, and timestamps have a single shape.**
  `update_upload_task` stored `updated_at` as an epoch float while freshly created
  rows took the TEXT column default (`CURRENT_TIMESTAMP`). SQLite orders every REAL
  below every TEXT, so the cleanup's `updated_at < datetime('now', '-N hours')` was
  true for rows that had just been written — every upload reaped the previous
  batch's completed tasks, destroying the history the table exists to preserve.
  `updated_at` is now written by SQLite, rows left in the float shape are normalised
  when the database is opened, and the sqlite3 datetime adapter is registered
  explicitly at second precision to match the column default (Python 3.12 deprecated
  the implicit adapter and 3.13 removes it, so bound `datetime` objects emitted a
  DeprecationWarning and would have failed outright on an interpreter upgrade).
- **Upload audit rows now name the acting user.** The background document
  processor is dispatched on a worker thread via `run_in_executor`, which does
  not copy contextvars (only `asyncio.to_thread` does), so the `user_id` it read
  from the tenant context inside that worker was always empty — every
  `document_upload` audit row recorded an anonymous actor. The tenant was still
  attributed correctly because it is passed as an argument; the acting user now
  travels the same way.
- **Startup now fails fast when a core component cannot initialise.** The
  lifespan caught every initialisation error and only logged it, so the service
  started healthy in states it cannot serve: a failed `QueryEngine` or
  `DocumentIndexBuilder` left `app.state.query_engine` unset (every later
  `/query` returned an opaque 500 while `/health` stayed green), and a missing
  `OPENLAD_ADMIN_PASSWORD` produced an instance with no admin user. Both now
  abort startup — the same contract the environment self-check already
  followed — and only the non-core, network-dependent service reachability
  check remains a warning.
- **Concurrent queries no longer share each other's industry hint.**
  `RetrievalExecutor` is cached per tenant and reused by concurrent requests,
  but `industry_hint` (supplied by the client on every request) was stored on
  the executor instance and read back from it later in the same call chain, so
  two overlapping requests for one tenant could apply each other's industry
  pack rules — query expansion, chapter boost rules and spec-fact terms. The
  hint now travels through the retrieval call chain as an explicit argument
  and is no longer kept on the shared instance; `execute()`'s public signature
  is unchanged.
- **Source citations are now clickable and deduplicated.** The answer footer's
  source links were `href="#"` placeholders that did nothing when clicked and
  repeated the same document once per cited chunk. Sources are now merged per
  document (union of cited pages), and clicking a source opens the cited page
  renders in the in-page lightbox — all cited pages of that document are
  navigable via ‹ › buttons or ArrowLeft/ArrowRight, as are inline page
  citations. Image fetch failures surface a toast instead of failing silently.
- **Copy button works on non-secure origins.** `navigator.clipboard` requires
  a secure context, so over `http://<lan-ip>:port` the copy button silently
  failed. A legacy textarea fallback now handles that case.
- **Uploaded image files now get OCR degeneration cleanup.** The `auto` vision
  route resolves to the dedicated OCR endpoint when configured, but unlike PDF
  page transcription its output was not passed through the tail-repetition
  cleanup — degenerate loops from the OCR model could enter the index. Also
  fixed the cleanup chain itself: when degeneration switches shape mid-tail
  (numbered loop → exact digit run), the one-unit stub left by the exact-period
  pass no longer shields the numbered loop above it from trimming.
- **Page renders now land in the tenant-scoped images dir.** Ingestion wrote
  page renders (`{doc_id}_p{N}.png`) to the legacy global `data/images/`, while
  the authenticated `/images/{filename}` endpoint serves only from
  `data/tenants/<tenant>/images/` — so citation images always 404'd. Ingestion
  now writes to the tenant dir; `scripts/migrate_page_images_to_tenants.py`
  migrates existing files (idempotent, `--dry-run` supported, ownership
  resolved via each tenant's metadata.db).
- **The Tesseract OCR fallback now actually ships in deployments.**
  `pytesseract` was absent from `requirements.txt` and the `tesseract` binary
  was never installed in the Docker image, so the last-resort OCR fallback for
  uploaded image files and low-quality PDF pages was silently dead (placeholder
  text, not even an error log). `pytesseract` is now a declared dependency and
  the image installs `tesseract-ocr` + `tesseract-ocr-chi-sim`; the startup
  environment self-check enforces the package and warns when the system binary
  is missing on host deployments.

## [0.4.8] - 2026-09-07

### Fixed

- **`start.sh` now sources `.env`.** Previously the script exported its own
  defaults for endpoint/model variables, which silently shadowed `.env` —
  edits to `.env` alone had no effect. Values from `.env` now take effect
  directly; unset variables still fall back to built-in defaults.
- **Rewrote root `.env.example`** to match what the current code actually
  reads (dropped legacy managed-mode variables such as `LLM_MODEL_PATH`),
  with per-variable comments and a pointer to `docs/configuration.md`.

### Changed

- **Docs restructure.** Slimmed the bilingual READMEs down to the essentials
  (intro, demo GIF, feature table, canonical quickstart) and moved reference
  material into `docs/`: deployment & hardware, configuration reference,
  API quickstart, architecture, and troubleshooting — each in English and
  Chinese (`docs/zh-CN/`). Added UI demo GIFs under `docs/assets/`.
  Troubleshooting now also covers field-observed operational issues
  (silent llama-server degradation, hollow ingestion from batch-size
  mismatch).

### Added

- **Dedicated OCR endpoint for scanned / image-only pages.** Visual pages
  are transcribed page-by-page by any OpenAI-compatible vision model
  configured as an OCR backend (`OPENLAD_OCR_URL` / `OPENLAD_OCR_MODEL`,
  or admin panel → Model Services). This replaces the previous
  classify-then-describe split for full-page transcription and keeps the
  main LLM free of image work. Pages that still cannot be transcribed
  surface as page-level visual transcription warnings and mark the
  document `degraded` (extending the existing verified/degraded model to
  OCR failures). Image-only uploads keep Tesseract as an offline fallback
  when no OCR endpoint is configured.
- **OCR transcription tail-degeneration cleanup.** Small OCR models can
  loop on tail repetition once the real page content is exhausted. Two
  cleanup passes trim exact periodic repeats (digit/word runs) and
  numbered pseudo-repeats (the same sentence re-typed with an incrementing
  index), so degenerate tails never enter the index.
- **Fail-fast startup environment self-check.** On boot the API verifies
  every declared runtime dependency is importable and aborts startup
  instead of silently degrading ingestion (e.g. unparsed PDFs stored as
  verified). Escapable with `OPENLAD_ENV_CHECK=off` for operators who know
  what they are doing.
- **Chart description pinned to the main LLM; analysis master switch.**
  Chart-region semantic description is a main-LLM vision task and is no
  longer routed to a configured OCR endpoint (`endpoint="llm"`), keeping
  ingestion deterministic: the page-OCR model transcribes visual candidate
  pages only. `OPENLAD_CHART_ANALYSIS=off` disables chart description
  entirely for operators who prefer deterministic chunk counts over the
  semantic enrichment.
- **All semantic vision calls pinned to the main LLM.** Page-level VLM
  analysis, image description and formula recognition now pass
  `endpoint="llm"` explicitly, so a configured OCR endpoint only ever
  handles transcription work (OCR page transcription and image-file
  transcription), never semantic description/analysis.
- **`generate_with_image` endpoint is now required.** The parameter lost
  its `"auto"` default: every vision call site must declare its intent
  (`llm` / `ocr` / deliberately `auto` for image-file transcription), so a
  future call that forgets the endpoint fails loudly instead of silently
  routing to the OCR model.
- **`OPENLAD_CHART_ANALYSIS` accepts any of `0|off|false|no`.** The master
  switch now matches its documentation (previously only the literal `"0"`
  was honoured, so `=off` silently did nothing).
- **Semantic-vision enrichment is off by default.** Chart-region
  description, page-level VLM analysis, formula→LaTeX and PDF-page image
  description all run on the MAIN LLM and require a vision-capable model
  (mmproj). The reference deployment is text-only-main-LLM + dedicated OCR
  endpoint, so these features now default to OFF and no longer make
  no-op vision calls against a text-only LLM. Deployments whose main LLM
  carries vision enable them explicitly with `OPENLAD_CHART_ANALYSIS=1`
  and/or `OPENLAD_IMAGE_DESCRIPTION=1`. OCR page transcription is
  unaffected (it never used the main LLM).

## [0.4.7] - 2026-09-01

### Added

- **Ingest quality states: `verified` vs `degraded`.** A document that
  completes ingestion with zero anomalies is `verified`; one that lost
  chunks (e.g. embedding rejections) or hits any future non-fatal ingest
  issue is marked `degraded`, with human- and machine-readable
  `ingest_warnings` in its metadata. Degraded documents still participate
  in retrieval — informed, not excluded: the LLM context header warns
  that the document was incompletely ingested (with the loss detail),
  citations carry `degraded` + `ingest_warnings`, and the admin document
  list shows a "Degraded" badge whose tooltip lists the warnings.
  Re-ingesting a degraded document cleanly restores `verified` and clears
  the warnings. `GET /api/v1/documents?status=degraded` filters affected
  documents for analysis and targeted re-ingest.

### Changed

- **Domain vocabulary moved out of core into industry packs.** Two
  spec-fact extraction patterns previously carried hardcoded datasheet
  vocabulary in core: the "Support &lt;num&gt; &lt;feature&gt; &lt;object&gt;"
  pattern (interfaces/channels/ports/... word list) and the codec-resolution
  pattern (H.264/HEVC/VP-family enumeration). Both are now pack-gated like
  the compute/frequency patterns already were: packs supply
  `spec_extraction.support_objects` (verbatim word forms — the mechanism
  deliberately does not auto-pluralize, so plural-only "bits" keeps
  bit-width declarations like "16 to 31 bit" from being misread as counts)
  and `spec_extraction.resolution_codecs` (verbatim literals, matched
  longest-first). Without a pack vocabulary the patterns are inert, so a
  bare or generic-only deployment no longer runs datasheet-specific
  matchers. The planner's entity stopwords are split the same way: core
  keeps only domain-neutral question/meta words ("多少", "是什么"), while
  corporate-filing vocabulary ("公司", "营业收入", ...) moves to the new
  `entity_stopwords` list in the generic base pack, injected through the
  new `RetrievalPlugin.get_entity_stopwords()` hook (merged across all
  loaded packs, filter-side union). Extraction output on the RK3588
  datasheet fixture is byte-identical before/after (79 facts, zero lost,
  zero gained).
- **Internal housekeeping (no behavior change).** The planner's
  "retrievable documents" lookup (`verified`/`degraded`, with legacy
  `completed` fallback) is consolidated into a single
  `_list_retrievable_documents()` helper instead of being duplicated at
  four call sites — new retrievable states now have one place to extend.
  The spec-fact bypass path documents its degraded-document premise in
  code (safe today because spec facts index from page text, not
  embeddings). Retry backoff `import time` moved to module top.
- **Query stage progress over SSE.** New `POST /api/v1/query/stream`
  endpoint emits Server-Sent Events — coarse pipeline stages
  (`planning` / `retrieving` / `generating`) followed by a terminal
  `result` frame whose payload is identical to `POST /api/v1/query`.
  The chat UI shows the current stage under the thinking indicator and
  falls back to the plain `/query` endpoint automatically when the
  stream is unavailable, so functionality never depends on streaming.
- **Chat UX upgrades.** Example-question chips on the welcome screen; a
  per-conversation search box and relative timestamps in the session
  list; conversations can be renamed (new `PATCH
  /api/v1/chat/sessions/{id}`, owner-only, returns 404 for other users'
  sessions to avoid existence leaks); every message carries a timestamp
  and a hover copy button that copies the raw markdown.
- **In-page image lightbox.** Citation page images and related-chart
  thumbnails now open in an in-page viewer (caption with document and
  page context, Esc / backdrop click to close) instead of a bare blob
  in a new tab, keeping the conversation in view.
- **Shared UI primitives (`ui.js`).** Toast notifications, a
  Promise-based confirm dialog, and a credentials modal with per-item
  copy buttons replace all native `alert()`/`confirm()` calls on both
  pages. Dangerous actions (delete conversation/document, regenerate
  API key, reset database) now use a consistently styled danger
  confirm, and newly created user credentials show a save-them-now
  notice.
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
- **Dark mode and accessibility.** A theme toggle on both pages switches
  between light and dark palettes, persisted in `localStorage` and
  following the OS preference until the user chooses; the saved theme is
  applied before first paint to avoid a light flash. Both pages share a
  new `common.js` (auth headers, 401 handling, `apiFetch`, HTML
  escaping, visibility-aware polling) and the admin page's inline script
  moved to `admin.js`. Polling (service status, import progress) now
  pauses while the tab is hidden and resumes immediately on return.
  Toasts are announced to screen readers via a live region, dialogs
  carry `role="dialog"`/`aria-modal`, and icon-only buttons have
  localized `aria-label`s (`data-i18n-aria-label`).

### Fixed

- **Embedding failures are no longer silent.** A batch whose embedding
  call fails was previously dropped with a single `warning` ("skipping"),
  and when *every* chunk failed the document logged nothing at all — a
  hollow document looked like a healthy one. Now the per-document summary
  always prints: full success stays `info`; any loss escalates to `error`
  with exact counts (`stored X, LOST Y`) bucketed by cause
  (`rejected` / `timeout` / `other` / `store`). `EmbeddingError` carries
  the upstream HTTP status and a failure kind; deterministic rejections —
  HTTP 4xx, plus llama-server's physical-batch overflow (HTTP 500 with
  the stable "too large to process" message) — fail fast instead of
  burning three pointless retries.
- **Ingestion chunk limits now respect the embedding server's physical
  batch.** Chunk sizing previously derived only from the embedding model's
  context window (8192 tokens), with no notion of llama-server's
  `--batch-size` cap on a *single* input — on small-batch deployments a
  dense (e.g. Chinese) chunk could exceed that cap, get rejected with
  "input is too large to process", and be silently skipped, ingesting the
  document "hollow" while still reporting success. New
  `OPENLAD_EMB_MAX_INPUT_TOKENS` (default `2048`, matching llama.cpp's own
  default so undeclared deployments are unchanged) clamps both
  `max_chunk_chars` and `max_embed_chars`; with `512` declared, every limit
  collapses to a 537-char budget that keeps chunks embeddable. The
  `embed_batch` truncation that used to hide such overflows now logs a
  warning naming the offending chunk count and the configured limit.
- Chat rendering is resilient to a missing sanitizer deployment: without
  the vendored sanitizer the answer degrades to plain text instead of
  raw HTML (fail closed). Page-citation badges no longer carry inline
  script attributes — they use data attributes with one delegated click
  listener.
- Corrected two garbled checkbox labels on the model-services tab.
- Repaired remaining GBK-misdecode mojibake on the model-services tab
  (em-dash, "or", and the ✓/✗ test-result markers), stripped stray UTF-8
  BOMs from the static pages, and added a CI-gate scan
  (`scripts/check_frontend_assets.py`) that fails on BOM or mojibake in
  `frontend/web/dist` so the corruption class cannot regress. Login
  forms on both pages now submit on Enter, and the key-clear checkbox
  label is localized instead of hardcoded Chinese.
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
