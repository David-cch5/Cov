"""Local navigation over the system of record: covenant -> tract -> parcel, and back.

BUILD_SPEC section 4 is titled "Data model (lineage is the point)" and requires
that "any lot must be traceable back to its covenant and forward through its full
conveyance history." Every piece of that was in the database and none of it was
reachable: you could answer it with SQL and nothing else.

This is deliberately a small local app rather than a generated page per covenant.
A per-covenant artifact has to be regenerated whenever anything changes and only
covers the covenants somebody remembered to run; the app navigates all of them
live. It also keeps the read model in one place for the separate database that
CLAUDE.md says will later connect -- an API boundary is easier to expose from a
web app than from a pile of static HTML.
"""
