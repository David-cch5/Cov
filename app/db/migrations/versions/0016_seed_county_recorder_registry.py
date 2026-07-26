"""seed county_recorder_registry for the counties whose clerk/recorder portal
was reverse-engineered this segment

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-24

county_recorder_registry has existed since 0001_initial_schema but was never
populated -- every prior "missing Exhibit A" / "lots don't exist in current
GIS" escalation this session was done ad hoc, by hand, re-discovering each
portal's login/search/navigation flow from scratch. This migration records
what was actually found, and app/recorder/adapters/*.py turns three of these
into real, reusable Playwright automation (mirroring how county_gis_registry
backs app/gis/adapters/*.py):

  - Ellis (Acclaim, Harris Recording Solutions) -- resolved covid 8386's
    missing Exhibit A: the recorded instrument was actually 36 scanned
    images (18 stamped pages), our local PDF only had 11.
  - Kerr (AVA, Fidlar) -- resolved covid 7768 the same way.
  - Denton, Nueces, Collin (GovOS "PublicSearch") -- used for chain-of-title
    tracing on covid 7938 (unresolved), 5963, and 4955 respectively.
  - Bexar -- also GovOS PublicSearch, but hosted at a different subdomain
    (bexar.tx.ds.search.govos.com rather than <county>.tx.publicsearch.us)
    -- confirmed the same adapter works against it unmodified (identical
    DOM: #basicSearchInputBox, #withOcr, same results-table shape).

Harris (proprietary ASP.NET WebSearch app, cclerk.hctx.net) and Webb (an
older-generation Kofile/GovOS "CountyFusion" product, distinct from the
PublicSearch line despite the shared corporate parent) were both navigated
successfully by hand this project but have no adapter module yet -- seeded
with status='needs_review' so the gap is visible rather than silently
absent.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    table = sa.table(
        "county_recorder_registry",
        sa.column("county_fips", sa.Text), sa.column("access_tier", sa.Text),
        sa.column("base_url", sa.Text), sa.column("auth_notes", sa.Text),
        sa.column("workers_allowed", sa.SmallInteger), sa.column("quirks", sa.Text),
        sa.column("status", sa.Text),
        sa.column("discovered_at", sa.DateTime), sa.column("last_verified_at", sa.DateTime),
        schema=SCHEMA,
    )

    rows = [
        dict(
            county_fips="48139", access_tier="portal_playwright",
            base_url="https://ellisccktxpublicsearch.us",
            auth_notes="Guest access, no login required. Guest is limited to the "
                       "'Property (By Name)' search mode.",
            status="active",
            quirks={
                "vendor": "acclaim_harris_recording_solutions",
                "adapter": "app.recorder.adapters.acclaim",
                "search_path": "/AcclaimWeb/Search/SearchTypeName",
                "search_results_are_json": "POST .../Search/GetSearchResults returns "
                    "{'Data': [...]} directly -- captured via a Playwright response "
                    "listener rather than DOM-scraped.",
                "document_detail_opens_in_new_tab": "clicking a result row does NOT "
                    "navigate the current page -- confirmed a Playwright locator "
                    "click, a raw mouse.click() at the row's coordinates, and a "
                    "dispatched MouseEvent all fail to navigate in place; it opens a "
                    "popup/new tab instead (context.expect_page() catches it).",
                "page_duplication": "the document image viewer (WebDocViewerHandler.ashx) "
                    "shows every physical page at two adjacent ataladocpage indices, "
                    "confirmed while resolving covid 8386's Exhibit A.",
                "resolved_covenants": ["8386"],
            },
        ),
        dict(
            county_fips="48265", access_tier="portal_playwright",
            base_url="https://ava.fidlar.com/TXKerr",
            auth_notes="Fully open, no login.",
            status="active",
            quirks={
                "vendor": "fidlar_ava",
                "adapter": "app.recorder.adapters.ava_fidlar",
                "angular_material_no_stable_ids": "form fields are mat-input-0/1/2... "
                    "which renumber depending on page state -- targeted by visible "
                    "placeholder text instead.",
                "exact_name_format_required": "Kerr's own help text requires the name "
                    "'spelled out' with a middle initial (e.g. 'DOE JOHN A.', not "
                    "'DOE JOHN') -- a bare common surname (tested: 'SMITH') returned "
                    "zero results rather than a results list.",
                "single_result_includes_full_detail_inline": "when a search matches "
                    "exactly one document, Page Count/Parties/Legals/Notes are already "
                    "in the same response -- no extra click needed. Multi-result "
                    "per-row detail (clicking a specific row to see its own detail "
                    "panel) was NOT confirmed working -- a real gap, not assumed.",
                "invisible_recaptcha_present": "a google.com/recaptcha/api2/reload "
                    "request fires on search but did not block automated Playwright "
                    "queries in testing; unverified under heavier/rapid querying.",
                "resolved_covenants": ["7768"],
            },
        ),
        dict(
            county_fips="48121", access_tier="portal_playwright",
            base_url="https://denton.tx.publicsearch.us",
            auth_notes="Guest access to search; full-text OCR search available via "
                       "the 'Search Index & Full Text (OCR)' radio.",
            status="active",
            quirks={
                "vendor": "govos_publicsearch",
                "adapter": "app.recorder.adapters.publicsearch",
                "no_interceptable_api": "reverse-engineering attempt confirmed there "
                    "is no stable JSON API for this vendor -- monkey-patching both "
                    "window.fetch and XMLHttpRequest.prototype.open before "
                    "triggering a client-side-routed search caught nothing but a "
                    "Bugsnag telemetry call, even though the results DOM populates "
                    "with real data. A rendered Playwright page is required.",
                "used_for": "chain-of-title research on covid 7938 (Torres Elaine) "
                    "-- ultimately still unresolved after exhaustive checking.",
            },
        ),
        dict(
            county_fips="48355", access_tier="portal_playwright",
            base_url="https://nueces.tx.publicsearch.us",
            auth_notes="Guest access to search; same GovOS PublicSearch product as Denton/Collin.",
            status="active",
            quirks={
                "vendor": "govos_publicsearch",
                "adapter": "app.recorder.adapters.publicsearch",
                "resolved_covenants": ["5963"],
                "note": "found the 2008 Passco Corpus Christi LLC replat ('MAP' doc "
                    "2008014951) that consolidated the deed's Lots 1-5 Block 2 into "
                    "the modern Lot 1A Block 2.",
            },
        ),
        dict(
            county_fips="48085", access_tier="portal_playwright",
            base_url="https://collin.tx.publicsearch.us",
            auth_notes="Guest access to search; same GovOS PublicSearch product as Denton/Nueces.",
            status="active",
            quirks={
                "vendor": "govos_publicsearch",
                "adapter": "app.recorder.adapters.publicsearch",
                "resolved_covenants": ["4955"],
                "note": "found a 2023 TxDOT right-of-way deed accounting for part of "
                    "the tract's acreage shortfall, and confirmed the covenant's own "
                    "trustee (Covenant Clearinghouse LLC) still lists the full "
                    "deed-stated acreage in a 2024 filing.",
            },
        ),
        dict(
            county_fips="48029", access_tier="portal_playwright",
            base_url="https://bexar.tx.ds.search.govos.com",
            auth_notes="Guest access to search. Same GovOS PublicSearch product as "
                       "Denton/Nueces/Collin but hosted at a *.ds.search.govos.com "
                       "domain rather than <county>.tx.publicsearch.us -- confirmed "
                       "app.recorder.adapters.publicsearch works against it unmodified "
                       "(identical #basicSearchInputBox / #withOcr / results-table DOM).",
            status="active",
            quirks={
                "vendor": "govos_publicsearch",
                "adapter": "app.recorder.adapters.publicsearch",
                "book_volume_page_advanced_search_field_does_not_reach_pre2000s_plat_refs":
                    "searching Volume/Page in Advanced Search for a plat reference "
                    "cited in a deed's exhibit (e.g. '2575'/'284') returned zero "
                    "results -- that field indexes this system's own recording "
                    "volume/page, not older Deed-and-Plat-Records book references.",
                "resolved_covenants": ["2497 (confidence upgrade, not a new match)"],
            },
        ),
        dict(
            county_fips="48201", access_tier="portal_playwright",
            base_url="https://www.cclerk.hctx.net/Applications/WebSearch/RP.aspx",
            auth_notes="Guest access to search (File Number/Grantor/Grantee/"
                       "Subdivision/Lot/Block/Abstract/Tract fields, ASP.NET WebForms "
                       "postback). No adapter module built yet -- navigated by hand "
                       "via ad hoc DOM manipulation this session, not a reusable "
                       "Playwright script.",
            status="needs_review",
            quirks={
                "vendor": "proprietary_aspnet_webforms",
                "confirmed_field_ids": ["txtFileNo", "txtDesc", "txtInstrument",
                    "txtVolNo", "txtPageNo", "txtLot", "txtBlock", "txtAbstract",
                    "txtTract", "btnSearch"],
                "no_document_image_viewer_found_for_guests": "the search results grid "
                    "gives full chain-of-title text (resolved covid 7991 this way), "
                    "but no page-image/page-count viewer was found reachable without "
                    "a login -- unlike Acclaim/AVA, so Harris can't yet support the "
                    "missing-Exhibit-A page-count diagnostic in app/recorder/diagnose.py.",
            },
        ),
        dict(
            county_fips="48479", access_tier="portal_playwright",
            base_url="https://countyfusion13.govos.com",
            auth_notes="Guest login available after dismissing a stale 'IE "
                       "Compatibility Mode' warning dialog and accepting a standard "
                       "liability disclaimer. No adapter module built yet.",
            status="needs_review",
            quirks={
                "vendor": "kofile_countyfusion",
                "note": "an older-generation Kofile/GovOS product, distinct from the "
                    "newer PublicSearch line despite the shared corporate parent -- "
                    "do not assume app.recorder.adapters.publicsearch works here "
                    "without re-verifying the DOM.",
            },
        ),
    ]

    for row in rows:
        op.execute(
            table.insert().values(
                county_fips=row["county_fips"], access_tier=row["access_tier"],
                base_url=row["base_url"], auth_notes=row["auth_notes"],
                workers_allowed=1,
                quirks=sa.text("(:q)::jsonb").bindparams(q=json.dumps(row["quirks"])),
                status=row["status"], discovered_at=sa.func.now(), last_verified_at=sa.func.now(),
            )
        )


def downgrade() -> None:
    op.execute(
        f"DELETE FROM {SCHEMA}.county_recorder_registry WHERE county_fips IN "
        "('48139','48265','48121','48355','48085','48029','48201','48479')"
    )
