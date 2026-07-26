# CLAUDE.md — Covenant Processing System

Always-loaded rules for this project. Read `BUILD_SPEC.md` for the full design.

## What this is
An application that processes recorded private-transfer-fee ("Freehold"-style) covenants:
for each covenant, identify the encumbered land, enumerate every current lot with ownership,
build the chain of title from the original grant down to each lot, keep the covenant→lot
lineage intact, and monitor remaining raw acreage for new plats.

## SCOPE GUARDRAIL (critical)
- Build the **core app** and run **only a limited cost probe** (Montgomery County + a small
  multi-county sample).
- **DO NOT run the full ~1,037-covenant / ~45,000-parcel portfolio** without explicit
  written go-ahead. The full run is gated on the measured deed fetch/read cost and recorder
  per-document fees from the probe.

## Non-negotiables
- **Accuracy over completeness.** Every covenant passes a reconciliation check before it is
  considered done: classified acreage must reconcile with the covenant's stated acreage, and
  any unaccounted area inside the footprint is flagged. The parcel census is **spatial-first**
  (enumerate every parcel whose geometry falls in the covenant polygon); subdivision names are
  labels applied afterward, never the primary search. (A name-first pass previously missed
  2,500+ lots — do not repeat that.) Build that covenant polygon RIGOROUSLY from the deed's
  metes-and-bounds — georeferenced POB + COGO traverse, tied to authoritative plat/parcel
  geometry — NEVER a bounding-box approximation. If the POB can't be georeferenced with
  confidence, send it to human review rather than approximating.
- **Never fabricate title data.** Low-confidence OCR or a broken grantor→grantee link goes to
  the human-review queue, never a guessed value.
- **Provenance on every datum:** source, retrieval timestamp, read-vs-estimated flag, confidence.
- **Deterministic code for exact work.** GIS queries, spatial joins, dedup, reconciliation,
  lineage, and recorder navigation are plain code — not LLM calls. Use the model only for
  document parsing, vision OCR of bad scans, and edge-case classification.

## OCR policy (tiered)
1. Free OCR (Tesseract) first on every document — handles the clean majority at ~zero cost.
2. Confidence gate on the OCR output.
3. Bad scan / low confidence → escalate to the **smartest Claude vision model (Fable 5)**
   reading directly from the image. (Opus 4.8 is a ~half-price alternative if cost matters.)
4. Still illegible → human-review queue.

## Model use (routing)
- **Default: Sonnet at high ("max smart") effort** for coding, bulk work, and field
  extraction — the efficient default that preserves Team seat allowance. Use **Haiku** for
  trivial subagent tasks (structuring clean OCR text).
- **Hard reasoning / stubborn edge cases → Opus 4.8**; the very hardest → **Fable 5**.
- **Bad-scan vision OCR → Fable 5** (smartest), with **Opus 4.8** as a cheaper fallback.
- Keep per-covenant LLM usage minimal — most of the pipeline is deterministic code.

## Cost discipline (deed reading)
Index-first (use the free recorder index for chain structure; only OCR images when needed),
deduplicate shared upstream chains once per subdivision, Batch API for bulk reads, prompt
caching on the extraction schema. Run **single-engine (Claude) for now**; apply the
dual-engine cross-check selectively once a second provider is supplied.

## Data locations (this folder)
- `<covid>/` — covenant PDFs, keyed by covid (leading number).
- `_textcache_final/*.json` — pre-extracted OCR text per covenant (reuse; do not re-OCR unless bad).
- `Covenant_Matrix/` — ~18 template clusters + covid map.
- `_pilot/` — covid→county index, the 4-covenant pilot outputs, targets.

## Tech stack
PostgreSQL + PostGIS (system of record + job queue), local disk for document/object storage,
programmatic headless browser (Playwright) for recorder portals (NOT the interactive Chrome
extension), Tesseract for free OCR, provider-agnostic LLM layer (**Claude API only for now**;
a second provider for the dual-engine cross-check is deferred and will be supplied later — do
NOT prompt for a second provider's credentials during the build).

## Extensibility & future integration
- Schema evolves only via a **migration framework** (e.g., Alembic); never hand-edit live schema.
- **Standard PostgreSQL/PostGIS only** (no exotic/non-portable features); stable, meaningful
  primary keys; clean, documented naming.
- Build this as an **independent system of record** with a clean domain boundary, so a separate
  database (a future FileMaker replacement) can later connect via separate schemas / postgres_fdw
  / an API with no rework. Do NOT build that integration now — just keep it integration-ready.
- Keep business logic in the app layer, not the database, so it stays portable.

## Security
Never commit API keys or credentials. Read source files in place; do not modify the covenant
data folders.
