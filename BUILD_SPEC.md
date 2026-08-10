# BUILD SPEC — Covenant Processing System

This is the build brief for Claude Code. Pair with `CLAUDE.md` (always-loaded rules).
It captures decisions already settled with the stakeholder. Build in the order below.

---

> **What this document is, and is not.** This is the ORIGINAL design spec, written
> 2026-07-25 before the app existed. It still describes the intended architecture,
> pipeline order and cost controls accurately, and it is the right thing to read for
> *why the system is shaped this way*.
>
> It is NOT the current state of the code, and it is not the authority on any rule.
> Where this document and the sources below disagree, **the sources below win**:
>
> | For | Read |
> |---|---|
> | Standing rules, non-negotiables, model routing, tiered policies | `CLAUDE.md` (always current) |
> | The schema, and WHY each part of it exists | `app/db/migrations/versions/*.py` docstrings — they cannot drift from the schema they create |
> | What the code actually does, and the decisions behind it | module docstrings, and `scripts/test_*.py` — decisions there are executable and fail if violated |
> | How a specific covenant was resolved | that covenant's own `review_reason` notes in the database |
>
> A short list of the material changes since this was written is at the end, under
> **Decisions since the initial spec**. That list is a pointer, not a substitute for
> the sources.

## 1. Objective & scope

Process recorded private-transfer-fee covenants. For each covenant:
- identify the encumbered land from its legal description,
- enumerate every current lot/parcel with owner, legal description, acreage,
- build the chain of title from the original grant down to each lot (with an **estimated**
  sale price in non-disclosure states),
- maintain the covenant→lot lineage end to end,
- monitor remaining undeveloped acreage and attach newly filed plats to the right covenant.

**Current scope (do not exceed without approval):** build the core app, then run a **cost
probe** on Montgomery County + a small multi-county sample. Full portfolio (~1,037 covenants,
~45,000 parcels) is GATED on the probe's measured deed fetch/read cost and recorder fees.

## 2. Inputs (this folder)

- `<covid>/<covid>_D####.pdf` — covenant documents, keyed by covid (leading number).
  ~1,037 covenants; 19 have 2 PDFs (original + amendment); 4 covids have no PDF
  (2506, 3504, 3516, 7642) → flag.
- `_textcache_final/<covid>_..._pdf.json` — pre-extracted OCR text (`text`, `covid`, `pages`,
  `ocr`, `vocab_score`). Reuse this; only re-OCR the ~60 flagged bad scans.
- `Covenant_Matrix/covenant_matrix.json` — ~18 template clusters (V01–V18) + review buckets
  (R##/U## = bad/unreadable) + `covid_map`. Use templates to extract only the doc-specific
  fields (county, declarant, legal description, dates, non-standard fee) rather than re-parsing
  each document whole.
- `_pilot/covid_index.csv` — covid → state/county for all covenants.
- `_pilot/{7029,5340,5835,3428}_parcels.csv`, `targets.md` — the 4-covenant TX pilot results.

## 3. Architecture

- **System of record + job queue:** PostgreSQL + PostGIS (everything is spatial).
- **Object storage:** local disk for PDFs, deed images, raw API responses (DB stores pointers).
- **LLM layer:** provider-agnostic interface; config selects model/provider. Build it so a
  second provider can drop in later via config, but run **Claude only for now** — the
  dual-engine cross-check is deferred. Do NOT prompt for a second provider's credentials
  during the build; the stakeholder will supply it later.
- **Model routing (Claude):** default **Sonnet at high / "max smart" effort** for coding, bulk
  work, and field extraction (Haiku for trivial structuring); escalate to **Opus 5** for hard
  reasoning and **Fable 5** for the hardest reasoning and for bad-scan vision OCR (Opus 4.8 as a
  cheaper vision fallback). *(Superseded the original "Opus 4.8 for hard reasoning".)*
  **CLAUDE.md states the policy; the EFFECTIVE model is whatever `app/config.py` resolves at
  run time, and `.env` beats the source default there — `load_dotenv()` populates the
  environment, and `os.environ.get(name, default)` then never sees the default. That is not a
  hypothetical: `.env` pinned `LLM_MODEL_HARD=claude-opus-4-8` for weeks after the source
  default was corrected to `claude-opus-5`, so every hard-reasoning escalation quietly ran a
  model behind the documented policy. `scripts/test_config.py` now asserts the resolved values,
  which is the only place the two can be compared.*
- **Workers:** stateless; pull jobs from a queue table with `SELECT … FOR UPDATE SKIP LOCKED`;
  idempotent upserts keyed by covid / parcel id / instrument number; shard by county across
  the 4 Mac minis; per-portal politeness (1–2 workers per recorder).

## 4. Data model (lineage is the point)

`covenant` → `tract` (encumbered-land geometry + residual raw-acreage polygon) → `parcel`
(+ `parcel_history`) → `transfer` (chain of title, parcel→parcel linked). Plus `contact`,
`price_estimate`, `monitor_run`, `event`, and `source` (provenance). Append-only / temporal
snapshots so the monitor can show what changed and when. Any lot must be traceable back to its
covenant and forward through its full conveyance history.

*That sketch is the shape, not the inventory — the schema is 33 tables now. `\dt covenant.*`, or
the migrations in order, is the accurate list; this is only what a reader of the sketch would be
surprised to be missing. The covenant↔parcel edge is its own table, `parcel_covenant`, with
`parcel_covenant_exclusion` holding reviewed exclusions durably so a re-run cannot silently
re-admit a parcel the deed does not convey. Then `parcel_lineage` and `plat` (subdivision
lineage), `covenant_document` and `recorder_document_image` (document storage),
`fee_collection`, `fee_payoff_statement`, `estoppel_certificate`, `exemption_category`,
`covenant_template` (+ `_exemption`), `covenant_trustee`, `covenant_beneficiary`, `job_queue`,
the two county registries, `captcha_session`, and `covenant_release` (+ `covenant_release_parcel`)
for terminations and buyouts — see §"Decisions since the initial spec".*

## 5. Pipeline (build in this order)

1. **Ingestion** — walk `<covid>/` folders; covid = leading number; reuse `_textcache_final`;
   match to template; extract fields (county, declarant, fee %, term, exemptions, legal
   description). Idempotent + incremental. Also support a watched drop-in folder and single-file
   ingest for new covenants. Route 4 missing-PDF + ~60 bad-scan covids to review.
2. **Legal-description parsers** — three modules: Texas abstract/metes-and-bounds, PLSS
   (township/range/section), colonial metes-and-bounds. Robust to OCR noise.
3. **GIS adapter framework + per-county adapters** — discover each county's public parcel
   service (Esri REST); when the appraisal-district hostname fails, search the ArcGIS Online
   catalog (`/sharing/rest/search`). Cache endpoint + field mapping + quirks in a **county
   registry** so discovery happens once per county. Known-good:
   - Montgomery: `services1.arcgis.com/PRoAPGnMSUqvTrzq/.../Tax_Parcel_view/FeatureServer/0`
   - Tarrant: Fort Worth ArcGIS Online org `services5.arcgis.com/3ddLCBXe1bRt7mzj/.../Parcels_Public_Vview/FeatureServer/0`
   - (Dallas, Nueces discovered in pilot — see `_pilot/targets.md`.)
   Note query constraints learned: keep result sets paged; large responses can truncate; use
   `returnGeometry=false` for attribute pulls; extent queries with `outSR=4326` for classification.
4. **Spatial classifier + reconciliation QA — build the covenant polygon RIGOROUSLY (no
   bounding-box shortcut).** Parse the full Exhibit A metes-and-bounds: the Point of Beginning
   (POB) plus every call (bearing, distance, curve data). Georeference the POB by tying to
   authoritative, already-surveyed geometry — the recorded plat's coordinates, the parent
   tract's corner in the county parcel fabric, or the monument/ROW intersection it names (e.g.,
   N ROW of SH 242 ∩ the tract line) — NOT a rectangle. Construct the polygon via a COGO
   traverse from the POB and/or by tying to the authoritative parcel/plat boundary, and check
   closure error. Then do a TRUE geometric intersection (point-in-polygon) with the parcel
   layer to enumerate every encumbered parcel (spatial-first; names/lineage are cross-checks
   only, never the primary search). Reconcile: polygon closure, polygon area vs deeded acreage,
   and classified-parcel area vs polygon; flag unaccounted area and boundary parcels. **If the
   POB cannot be georeferenced with confidence, route to human review — do NOT fall back to a
   loose bounding box or guess.** This gate must pass before a covenant is "done."
   ~~NOTE: the 4440 output in `_pilot/` was produced with an APPROXIMATE bounding-box +
   tract-lineage method~~ **DONE** — covid 4440 was re-derived with the rigorous method, and its
   boundary parcels reviewed (22 deed-confirmed adjoiner/sliver exclusions across both tracts,
   6 wrongly-excluded parcels restored). The `_pilot/` bounding-box output is superseded.
5. **Deed / chain of title** — per-county recorder adapter, tiered by access:
   (a) API/downloadable index, (b) public portal via **headless browser (Playwright)** that
   waits on specific elements/network responses, (c) CAPTCHA/login/paywall → human-assist queue,
   (d) offline → manual. Search by parcel/owner/legal, pull the instrument index, walk
   grantor→grantee back to the parent tract. **Montgomery deed source (confirmed):**
   `montgomery.tx.publicsearch.us` — a **Kofile/GovOS PublicSearch** portal (standard product),
   free, index + full-text OCR. Its results client is fragile: in automated/edge browsers it
   throws a client-side error (reports to bugsnag) and falls back to a blank / "update your
   browser" page WITHOUT making the search API call — so do NOT depend on the rendered results
   page. API is **same-origin**; client routes are `/results`, `/doc/:id` (document detail),
   `/search/advanced`. **CONFIRMED (2026-07): search is PUBLIC — no login needed (the `authToken`
   cookie is issued to everyone and makes no difference).** The `/results?department=RP&
   searchType=quickSearch&searchValue=<NAME>&recordedDateRange=<FROM>,<TO>&...` GET returns the
   app SHELL + search-form config, NOT the result rows (raw HTML has 0 `/doc/` links). The
   results are assembled CLIENT-SIDE — interactive probing found no replayable JSON endpoint
   (the only JSON response is bugsnag error tracking; each search is a full navigation that
   defeats in-page network logging). **CONCLUSION: retrieve results by RENDERING, not by a raw
   HTTP call.** A normal up-to-date browser renders the results fine, so the app uses **headless
   Playwright (current Chromium)** to load
   `/results?department=RP&searchType=quickSearch&searchValue=<NAME>&recordedDateRange=<FROM>,<TO>`
   (public — no login), wait for the result rows to render, and read them from the DOM; then
   follow `/doc/:id` for the document image. (This could not be done from the interactive Cowork
   session because that sandbox browser couldn't render the SPA — a dedicated Playwright process
   can, exactly as the user's own Chrome does. The probe validates it.) Login and per-document
   fees apply only to the `/doc/:id` image. Dedup shared upstream chains
   once per subdivision. Index-first: only fetch/OCR document images (`/doc/:id`) when the index
   lacks needed detail.
   (Note: this could not be fully tested from the interactive Cowork session — web_fetch can't
   render JS, the extension bridge idle-timed-out/froze, the network tool returns no bodies, and
   in-page API probes were blocked by a cookie/query-data safety guard. None of these apply to a
   Playwright-driven app; the probe validates it in the clean environment.)
6. **OCR** — tiered per `CLAUDE.md`: Tesseract free → confidence gate → Fable 5 vision on bad
   scans → human queue.
7. **Non-disclosure price estimate** — TX, UT, and other non-disclosure states have no recorded
   price; estimate from the recited mortgage/deed-of-trust amount and/or assessor market value,
   tagged estimated + confidence.
8. **Dual-engine cross-check (DEFERRED — build the plumbing, run single-engine now)** —
   scrape/compute once; store extracted field values per engine with an `engine` tag and build
   the reconciliation step, but run **Claude only** until a second provider is supplied. Design
   so a second (different-family) engine drops in via config with no rework. Do NOT prompt for a
   second provider's key during the build.
9. **CAPTCHA human-in-the-loop** — on detection, pause that county, notify with a remote-viewable
   session for the human to solve, reuse the solved session for a batch, timeout (configurable,
   ~5 min default) → "CAPTCHA pending" queue + retry when the human is available; never block the
   fleet.
10. **Monitor** — maintain each covenant's residual raw-acreage polygon; periodic spatial diff
    for new parcels/plats; on a hit, pull the new lots + owners, reclassify, update lineage,
    alert. Deterministic diff (near-zero tokens); LLM only on a detected change.

## 6. Cost controls (deed reading — the gating cost)

Dedup shared chains; index-first; tiered OCR (free → smart model only on bad scans);
Haiku for routine structuring; Batch API (~half price) for bulk reads; prompt caching on the
extraction schema; selective dual-engine (not every field). These are what keep the probe cost
representative of an efficient full run.

## 7. The cost probe (deliverable that gates the full run)

Run the finished pipeline on Montgomery + a small multi-county sample and report:
- per-document deed read cost under the tiered OCR policy,
- recorder per-document fees encountered (which counties, how much),
- average documents per parcel chain,
- throughput per worker/portal.
Then stop and present the numbers for the full-run go/no-go decision.

## 8. Known hard limits (state honestly, don't paper over)
CAPTCHAs can't be auto-solved (human handoff); paywalled documents cost per copy; offline
recorders need manual retrieval; non-disclosure states yield estimated (not recorded) prices;
commercial/entity-owned parcels may transfer via entity sale with no recorded deed (mark the
recorded chain "deed-of-record complete; economic transfers may exist via entity sale").

## 9b. Extensibility & future integration
- Use a **migration framework** (e.g., Alembic) from the start; the schema evolves only via
  versioned migrations so features/tables add cleanly.
- **Stable, meaningful primary keys** (covid, parcel id, instrument number); clean, documented
  naming; **standard PostgreSQL/PostGIS only** (avoid non-portable features).
- Design the DB as an **independent, self-contained system of record** for covenant/parcel/title
  data with a clean domain boundary. A separate database — a future replacement for an existing
  FileMaker system (hundreds of tables, large relationship graph) — should be able to connect
  later via separate schemas / `postgres_fdw` / an API **with no rework**. Do NOT build that
  integration now; only keep the schema integration-ready.
- Keep business logic in the app layer, not the database, so it stays portable.
- (Note: replacing FileMaker also means replacing its UI/logic layer, not just the data — that
  is a separate future project; Postgres is only the data target.)

## 9. Phasing (full program, for reference)
P1 Core app (~1 wk) → P2 Texas-metro validation (~2–3 d) → P3 multi-state GIS coverage →
P4 chain of title (rolling) → P5 monitor. **Right now: P1 + the Montgomery cost probe only.**

## Decisions since the initial spec

A pointer list, newest first. Each entry names where the reasoning actually lives — the
migration docstrings and tests are the authority, not this summary.

**Releases: terminations and buyouts** (migrations 0037–0041, `app/title/release.py`,
`scripts/test_release.py`). Nothing in the original spec could say a covenant had STOPPED
applying to land. It now can, and the semantics were corrected five times against real
recorded instruments (kept in `_termination_examples/`):
- Every ingested covenant is **valid as of today**. A termination found in the public records
  is a downloaded document at `validity_status='pending_review'` and asserts nothing until
  adjudicated. An invalid one is answered by a rescission (generation deferred).
- A **termination** may be prospective or **void ab initio** ("null and void as if it had never
  been recorded"), the latter licensed by the instrument's sworn no-intervening-conveyance
  statement.
- A **buyout** is prospective only — it is negotiated to stop future collection.
- History is never erased: a released parcel stays in the census, prior `fee_collection` rows
  keep their amounts, and a buyout covering an outstanding balance *links* to it.
- A release adjudicated valid and covenant-wide makes the covenant **historic — recorded, not
  researched**, so the paid anchor tiers do not spend on it.

**Per-tract stated acreage** (migration 0036). `covenant.stated_acreage` covers all tracts
together; reconciling one tract against it misreported a 33.5-ac tract as having a 285-ac gap.

**Anchor resolution is tiered and automated** (`app/gis/anchor_resolver.py`, CLAUDE.md).
The spec's "georeference the POB by tying to authoritative geometry" is now a real orchestrator:
stated State Plane coordinate → **published NGS monument tie** (`app/gis/ngs.py`) → deferred
judgment tiers → Opus 5 → Fable 5 → the existing approximate placement. Every tier verifies
independently before committing; a released covenant is skipped before any paid tier.

**OCR escalation merges, never substitutes** (`app/ingestion/ocr_escalation.py`). Vision-OCR'd
pages are spliced into the existing transcription. Replacing a document with a 4-page fragment
cost covid 5839 its declarant, recording instrument and stated acreage.

**Reading the legal description is the whole game** (CLAUDE.md's own longest section). Nearly
every hard problem here has been a misreading, not a GIS or survey problem — and the closure
error tells you what you failed to read. Also: before treating a tract as a geometry problem,
check whether the deed already names an existing parcel (covid 5839 named its CAD account).

**Boundary parcels are never trusted on a raw match count** (CLAUDE.md non-negotiable,
`app/gis/classifier.py`). Confirmed real: 254 matched parcels were actually 214.
