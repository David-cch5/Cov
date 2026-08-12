"""the land spine: a tract, split forward by deeds, acquiring APNs at the leaves

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-12

parcel_lineage (0029) keys a split on the PARENT'S APN, and Texas counties do not
keep retired APNs -- the parent is precisely the row the county deletes. So that
table can only record a split this project OBSERVES happening (app/gis/monitor.py),
which is why it holds 0 rows after 66 monitor runs. It keeps that narrower job.

This is the spine it was mistaken for. It starts at the covenant's own acreage tract
and runs FORWARD on deeds:

    root        the encumbered tract as its legal description describes it
    split       a deed conveying part of it creates TWO children -- the piece
                CONVEYED and the piece RETAINED
    leaf        a node acquires (county_fips, apn) when its owner is identifiable,
                and plat_id when it is platted

THE RETAINED REMAINDER IS A FIRST-CLASS NODE. Nobody records a document for it, so a
purely document-driven walk never creates it -- and it is the only way to answer how
much of a covenant's land is still in the declarant's hands.

IDENTITY IS BOTH KINDS. node_id is the surrogate every parent, transfer and fee
points at, so a covenant can be re-tracted without rewriting references. node_label
is the readable path a payoff statement shows a person ("48339-4780-T1.2.1"). The
label can be regenerated; the surrogate never changes.

ACREAGE IS A LEDGER, in its own table, because one deed can convey TWO tracts -- one
encumbered by the covenant and one not, and likewise with lots. A deed's stated
acreage is a fact about the DEED, not about the covenant's land: subtracting it from
the parent corrupts the remainder, and a fee computed from it overstates what is
owed. So each measurement is stored with the basis it came from, and the one a fee
accrues on ('encumbered') is never confused with the one the instrument recites
('stated'). A disagreement between bases is a finding to chase to the document, which
is why nothing here stores a single reconciled number.
"""
from alembic import op

from app.config import DB_SCHEMA as SCHEMA

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {SCHEMA}.tract_node (
            node_id                 bigserial PRIMARY KEY,
            node_label              text NOT NULL,
            covid                   integer NOT NULL,
            tract_no                integer NOT NULL,
            parent_node_id          bigint REFERENCES {SCHEMA}.tract_node (node_id),
            disposition             text NOT NULL,
            -- the deed that created this node. Absent on a root, required otherwise:
            -- a node exists because an instrument split its parent.
            split_county_fips       char(5) REFERENCES {SCHEMA}.county (county_fips),
            split_instrument_number text,
            split_recording_date    date,
            -- acquired at the leaf, not the identity. Nullable for the whole life of
            -- a node that never becomes a separately-assessed parcel.
            county_fips             char(5),
            apn                     text,
            plat_id                 bigint REFERENCES {SCHEMA}.plat (plat_id),
            source_id               bigint REFERENCES {SCHEMA}.source (source_id),
            review_reason           text,
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT tract_node_disposition_check
                CHECK (disposition IN ('root', 'conveyed', 'retained')),
            -- A root has no parent and no splitting instrument; anything else has
            -- both. This is what stops a floating node with no provenance.
            CONSTRAINT tract_node_root_shape CHECK (
                (disposition = 'root'
                 AND parent_node_id IS NULL AND split_instrument_number IS NULL)
                OR (disposition <> 'root'
                    AND parent_node_id IS NOT NULL AND split_instrument_number IS NOT NULL
                    AND split_county_fips IS NOT NULL)
            ),
            -- An apn without its county cannot be joined to parcel, and a county
            -- without an apn identifies nothing.
            CONSTRAINT tract_node_apn_needs_county
                CHECK ((county_fips IS NULL) = (apn IS NULL)),
            CONSTRAINT tract_node_apn_is_a_real_parcel
                FOREIGN KEY (county_fips, apn) REFERENCES {SCHEMA}.parcel (county_fips, apn),
            CONSTRAINT tract_node_belongs_to_a_tract
                FOREIGN KEY (covid, tract_no) REFERENCES {SCHEMA}.tract (covid, tract_no),
            CONSTRAINT tract_node_label_unique UNIQUE (covid, tract_no, node_label)
        )
    """)
    # One root per covenant tract: the spine has a single starting point, or "the
    # land this covenant encumbers" has no answer.
    op.execute(f"""
        CREATE UNIQUE INDEX tract_node_one_root_per_tract
            ON {SCHEMA}.tract_node (covid, tract_no) WHERE disposition = 'root'
    """)
    # A deed splits a given parent once, into one conveyed piece and one retained
    # piece. Recording the same instrument against the same parent twice with the
    # same disposition is a double-count of the same event.
    op.execute(f"""
        CREATE UNIQUE INDEX tract_node_one_split_per_instrument
            ON {SCHEMA}.tract_node (parent_node_id, split_instrument_number, disposition)
         WHERE parent_node_id IS NOT NULL
    """)
    op.execute(f"CREATE INDEX tract_node_parent_idx ON {SCHEMA}.tract_node (parent_node_id)")
    op.execute(f"CREATE INDEX tract_node_covid_idx ON {SCHEMA}.tract_node (covid, tract_no)")
    op.execute(f"""CREATE INDEX tract_node_apn_idx ON {SCHEMA}.tract_node (county_fips, apn)
                    WHERE apn IS NOT NULL""")

    op.execute(f"""
        CREATE TABLE {SCHEMA}.tract_node_acreage (
            node_id     bigint NOT NULL REFERENCES {SCHEMA}.tract_node (node_id) ON DELETE CASCADE,
            basis       text NOT NULL,
            acreage     numeric(12,3) NOT NULL,
            source_id   bigint REFERENCES {SCHEMA}.source (source_id),
            note        text,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (node_id, basis),
            -- 'stated'     what the instrument recites -- about the DEED
            -- 'encumbered' how much lies inside THIS covenant's tract -- what a fee
            --              accrues on, and the only one safe to bill from
            -- 'derived'    parent minus siblings; meaningful only when the
            --              conveyance stayed inside the tract
            -- 'gis'        measured from geometry, once a parcel or plat exists
            CONSTRAINT tract_node_acreage_basis_check
                CHECK (basis IN ('stated', 'encumbered', 'derived', 'gis')),
            CONSTRAINT tract_node_acreage_nonneg CHECK (acreage >= 0)
        )
    """)
    op.execute(f"""
        COMMENT ON TABLE {SCHEMA}.tract_node_acreage IS
        'One row per (node, basis). Never collapsed into a single acreage: a deed can '
        'convey an encumbered and an unencumbered tract together, so its stated acreage '
        'is not the covenant''s acreage, and a disagreement between bases is a finding '
        'to chase to the document rather than a number to reconcile.'
    """)


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.tract_node_acreage")
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.tract_node")
