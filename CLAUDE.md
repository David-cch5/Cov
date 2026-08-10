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

## READ THE FULL LEGAL TEXT FIRST (default, not a fallback)
Before any GIS trial-and-error, name-based parcel search, or LLM escalation on a new covenant,
read the deed's **entire** legal description — Exhibit A included — end to end. It routinely
states the answer outright, and skipping it has been the single most expensive recurring mistake
in this project:
- **covid 8534**: two LLM escalations (~$45–50) and two recorder-portal chain-of-title searches
  all failed to anchor the tract. The POB call named the answer plainly — "on the Westerly
  right-of-way line of F.M Highway 428 (Sherman Drive) and in or near the centerline of Hercules
  Drive" — a street intersection anyone can find. Note the deed's own internal inconsistency
  there ("Hercules Drive" once, "Hercules Lane" everywhere after): expect typos and treat a
  near-miss name as the same feature until GIS proves otherwise.
- The same text also names the **adjoining** subdivisions that must NOT be counted as encumbered
  land (see `app/parsing/legal_description/adjoiners.py`, now automated) — and, critically,
  the ones the tract was platted INTO, which must be.

Practical order for a new covenant: full text → named features (roads, plat corners, adjoiners,
monuments) → deterministic anchoring against those → only then LLM escalation. Escalating first
is both slower and far more expensive.

**Read the legal description from the DOCUMENT ITSELF, not a summary — and fix it when it's
wrong.** `covenant.legal_description_raw` is an ingestion-time summary and routinely omits the
field notes (covid 4781's literally contains the placeholder "[metes and bounds courses follow]").
Use `app/ingestion/walk.py`'s `get_deed_text`, and when the parse looks thin, go back to the
document and correct the parser rather than working around it. Nearly every hard problem in this
project has turned out to be a bad reading of the legal description, not a hard GIS problem:
- **covid 5838**: a 17.2-acre "missing land" residual and a 14.9-acre acreage shortfall were both
  reading failures, not survey or GIS errors. The tract's own traverse reproduces the deed's stated
  318.779 ac to 0.001 ac once every call is read — including one curve the deed states with **no
  bearing at all** ("THENCE in a northwesterly direction, an arc distance of 31.95 feet"), whose
  direction is recoverable from the radius bearing recited in the *previous* sentence.
- **covid 4780**: "TRACT Ii" is OCR for TRACT III, not tract II — a third encumbered tract whose
  land was being double-counted into tract 1.
- **covid 8534**: the POB names a street intersection outright, after ~$45–50 of LLM escalation
  failed to find it.

Corollary: **the closure error tells you what you failed to read.** A traverse that closes at
1:674 instead of 1:800,000 is not "a rough survey" — on covid 5838 the 31.95 ft closure error was
exactly the length of the one course the parser had dropped. Chase the discrepancy to its cause
before accepting or approximating anything.


## Terminated / released covenants are HISTORIC — record, don't research
A covenant fully released (terminated or bought out, `scope='covenant'`) is worth keeping on
the record — that record is how anyone later shows the land WAS encumbered and no longer is —
but it is **not worth researching**. No chain-of-title walk, no GIS anchoring, no LLM
escalation: each costs real money to establish facts about an obligation that no longer exists.
`app/title/release.py`'s `is_fully_released` is the check, and
`resolve_metes_and_bounds_anchor` skips on it by default; `research_released=True` is the
deliberate override. Do the work only on specific instruction.
- A **partial** release does not make a covenant historic — land is still encumbered.
- A release that `needs_review` (unexecuted acknowledgement, retroactive with no sworn
  no-conveyance statement) does NOT make it historic either: that is precisely the case where
  the covenant may still be live, so skipping research there would assume the answer.

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
- **Always test the boundary parcels — never report a raw match count.** A spatial hit is not
  proof of encumbrance. Every parcel classified `boundary` (not fully interior) must be checked
  against the deed's own text before it counts: a neighbouring subdivision's platted lots clip
  the tract polygon at ordinary digitization tolerance and will silently inflate the parcel
  count. `classify_metes_and_bounds_tract` now auto-flags the signature (a whole subdivision
  whose members are ALL low-overlap) as `possible_non_tract_subdivisions`, with the deed's own
  text as corroboration or veto — but the exclusion itself stays a human call via
  `exclude_non_tract_parcels`. Confirmed real on covid 8534 tract 1: 254 matched parcels were
  actually 214, with 40 belonging to two subdivisions the deed never conveys.
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

## Metes-and-bounds anchor resolution (tiered)
`app/gis/anchor_resolver.py`'s `resolve_metes_and_bounds_anchor` is the single entrypoint —
never anchor a tract by hand-writing a one-off script per covenant again.
1. Deterministic techniques first, free: a stated State Plane coordinate in the deed itself,
   **a published NGS control-monument tie**, a shared corner with an already-anchored sibling
   tract, a tie to a named adjoining platted parcel (`app/gis/state_plane_anchor.py`) — each
   with its own sanity check (recomputed closure/area; a parcel tie's implied `length_ratio`
   must be within ~5% of 1.0) before ever being trusted.
   - **Check for an NGS tie before anything cleverer.** When a deed says "a National Geodetic
     Survey monument stamped X bears <bearing> <distance>", the answer is published, free and
     survey-grade: `extract_ngs_monument_ties` → `app/gis/ngs.py`'s `find_monuments` →
     `anchor_by_ngs_monument_tie`, wired as Tier 0b in `anchor_resolver.py`. It needs no rotation
     solve, because a deed reciting ties this way is working on the State Plane grid, so its
     bearings ARE grid azimuths.
   - Only ties attached to the **Point of Beginning** are used deterministically. A tie names the
     corner it runs from, and mapping a named corner onto a traverse vertex is the same judgment
     call tiers 0c/0d defer to the LLM. "At the POB" means before the first real COURSE, not the
     first "thence" — covid 5838's 3.103 ac tract reaches its tie through a two-leg offset whose
     own text contains "thence".
   - **Keep the NGS search box small.** `/api/nde/bounds` silently caps at 500 marks with no error
     and no truncation flag. Nueces' own parcel extent returns 415 and finds both SF 010 and KNOLL;
     buffered by 0.25° it returns exactly 500 and finds NEITHER. `find_monuments` raises on that
     rather than reporting a published monument as missing — a tie runs thousands of feet, so
     0.05° is already generous.
   - Three independent checks make it trustworthy, and all three are cheap: the zone mapping is
     confirmed by reprojecting the monument's own lat/lon back onto its datasheet's own grid
     coordinates (0.006 ft on covid 5838); two ties to two monuments reconstruct the
     monument-to-monument vector with the unknown corner cancelling out, checked against
     published truth (0.42 ft in 5,590 ft, 0.007°); and a pair that disagrees is tested for the
     single-quadrant-letter hypothesis before being rejected.
   - **This deed corpus reverses East/West.** covid 5838 does it twice — once in a closing
     course, once in a KNOLL tie — each time with the distance right to a hundredth of a foot
     and only the letter wrong. `repair_quadrant_by_closure` (courses) and
     `cross_check_monument_ties` (ties) both recover it, and both refuse unless exactly one
     flip resolves the discrepancy. Never accept a flip that isn't unique.
2. None pass → escalate to an agentic LLM search (`app/llm/anchor_agent.py`, real tools: live
   GIS parcel queries, NGS survey-monument lookups, traverse walking, similarity solving) at
   **Opus 5**, budget-capped (~80 tool-call turns / 90 min per attempt).
3. Opus's own result doesn't independently verify (recomputed closure/acreage, a real
   live-parcel spatial dry-run — never the model's own self-reported confidence alone) →
   retry once at **Fable 5**.
4. Still nothing confident → `app/gis/geocode_anchor.py`'s existing rough approximate-placement
   fallback (shape validated, position unconfirmed, `needs_review`) — never a forced guess.
A confident, independently-verified result at any tier commits automatically and the pipeline
proceeds — this is the one place in the pipeline an LLM's output is trusted without a human
checkpoint, and only because every claim is re-derived and checked in plain code first.

## Model use (routing)
- **Default: Sonnet at high ("max smart") effort** for coding, bulk work, and field
  extraction — the efficient default that preserves Team seat allowance. Use **Haiku** for
  trivial subagent tasks (structuring clean OCR text).
- **Hard reasoning / stubborn edge cases (incl. smart/edge-case classification) → Opus 5**; the very hardest → **Fable 5**.
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
