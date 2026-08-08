"""allow source_type 'deed_traverse' -- geometry read from the document itself

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-08

source_source_type_check enumerates where a datum came from, and every existing
member names an external system this project READ from: a recorder portal, a
county GIS or assessor API, an OCR engine, a PDF. A metes-and-bounds boundary
has no such origin. It is computed from the deed's own field notes by pure
trigonometry -- app/parsing/legal_description/metes_bounds.py's COGO traverse --
and no external service holds that shape at all.

Before this, such a geometry had to borrow someone else's provenance. covid 5838
tract 1 is the case that forced it: its geom was written with the source row of
the Nueces CAD spatial query that happened to run alongside it, which then read
as though the county had supplied the boundary. It had not; the shape came from
the document, and the mislabelling is what let a parcel union sit unnoticed under
boundary_resolution_method='metes_and_bounds_traverse'.

'deed_traverse' is not the anchoring METHOD -- that lives in
tract.boundary_resolution_method, and an anchor's own technique (a stated State
Plane coordinate, an NGS monument tie, a sibling-tract corner) belongs in the
source's `reference` text. This value answers only "where did this shape come
from", and the honest answer is the deed.

Widening a CHECK constraint is backward-compatible: every row that satisfied the
old constraint still satisfies the new one, so the downgrade is only safe if no
row has since used the new value -- hence the guard below.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

_ALLOWED = (
    "pdf_document", "textcache_ocr", "vision_ocr_fable5", "vision_ocr_opus",
    "gis_api", "recorder_portal", "recorder_api", "assessor_api",
    "estimate_derivation", "manual_entry",
)
_NEW = "deed_traverse"


def _values(names) -> str:
    return ", ".join(f"'{n}'" for n in names)


def upgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.source DROP CONSTRAINT source_source_type_check")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.source ADD CONSTRAINT source_source_type_check
        CHECK (source_type IN ({_values((*_ALLOWED, _NEW))}))
    """)


def downgrade() -> None:
    # Refuse rather than silently orphan a real provenance record.
    op.execute(f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM {SCHEMA}.source WHERE source_type = '{_NEW}') THEN
            RAISE EXCEPTION
              'cannot downgrade: % source row(s) use source_type ''{_NEW}''',
              (SELECT count(*) FROM {SCHEMA}.source WHERE source_type = '{_NEW}');
          END IF;
        END $$;
    """)
    op.execute(f"ALTER TABLE {SCHEMA}.source DROP CONSTRAINT source_source_type_check")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.source ADD CONSTRAINT source_source_type_check
        CHECK (source_type IN ({_values(_ALLOWED)}))
    """)
