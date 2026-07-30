"""create plat table + parcel.recited_legal_description/plat_id -- real
platting-event dates, not detection dates

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-29

The gap this closes: covid 4440 (Montgomery, a raw ~1928-ac metes-and-
bounds assemblage with no subdivision name in its own 2009 legal
description) went from 0 to 4091 real matched parcels once genuinely
platted, but nothing in this schema records WHEN each subdivision/section
was actually subdivided -- parcel_lineage.effective_date is populated with
date.today() (the date OUR software happened to notice), not the real
recording date of the plat itself. That makes "how much was raw as of
mid-2015" unanswerable except by luck of check cadence.

Confirmed live (2026-07-29) that Montgomery's own recorder portal
(montgomery.tx.publicsearch.us) carries this as real, dated, filed
data: switching its Department selector from "Public Records" to "Plats"
and searching "THE CANOPIES" (the real subdivision covering one of covid
4440's own matched lots, APN 815074) returns FILE NUMBER/RECORDED DATE/
SECTION/ABSTRACT rows -- e.g. Section 3 recorded 2024-08-20, File#
2024082483, Abstract 494 (the SAME Walker County School Land Survey
abstract as covid 4440's own deed) -- i.e. this exact land was still raw
as late as 2024, 15 years after the covenant's own 2009 recording.
Searching by the BASE subdivision name (not a specific section) returns
every section's own plat in one call, so one lookup per subdivision name
resolves every section/phase of it at once -- matches this project's
existing cost-discipline convention (dedupe once per subdivision, not per
lot, per CLAUDE.md's Cost Discipline section).

Also confirmed real (same live check): not every parcel within a tract is
part of a plat at all -- some (e.g. covid 4440's own MUD-district and
Forestar-owned tracts, "A0494 - Walker Co Sch L, TRACT 1C-1, ACRES
27.2696") are raw abstract-survey tracts with their own county-assigned
APN but no lot/block/reserve designation -- i.e. still-unplatted land
visible as a parcel record. app/gis/plat_parser.py's job is telling these
apart from real plat references, never guessing one way when a legal
description doesn't clearly say either.

subdivision_name/section are stored separately (not one combined string)
because a single subdivision has multiple sections, each an independently-
dated plat filing (confirmed: THE CANOPIES sections 1/2/3/4/18 alone
carry two different recording dates, 2024-06-07 and 2024-08-20) -- the
unique key is per-section, not per-subdivision-name.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {SCHEMA}.plat (
            plat_id              BIGSERIAL PRIMARY KEY,
            county_fips          CHAR(5) NOT NULL REFERENCES {SCHEMA}.county(county_fips),
            subdivision_name     TEXT NOT NULL,
            section              TEXT NOT NULL DEFAULT '',
            lookup_status        TEXT NOT NULL DEFAULT 'found' CHECK (lookup_status IN ('found', 'not_found')),
            recording_instrument TEXT,
            recording_date       DATE,
            book_volume_page     TEXT,
            abstract_name        TEXT,
            source_id            BIGINT REFERENCES {SCHEMA}.source(source_id),
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (county_fips, subdivision_name, section)
        )
    """)
    op.execute(f"ALTER TABLE {SCHEMA}.parcel ADD COLUMN recited_legal_description TEXT")
    op.execute(f"ALTER TABLE {SCHEMA}.parcel ADD COLUMN plat_id BIGINT REFERENCES {SCHEMA}.plat(plat_id)")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.parcel DROP COLUMN plat_id")
    op.execute(f"ALTER TABLE {SCHEMA}.parcel DROP COLUMN recited_legal_description")
    op.execute(f"DROP TABLE {SCHEMA}.plat")
