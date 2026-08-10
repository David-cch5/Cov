"""enqueueing the same work twice must be impossible, not merely avoided

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-10

covenant.job_queue was designed as a work queue in the initial schema -- status
defaults to 'queued' and allows in_progress/done, and there are already
priority, attempts, locked_by, locked_at and available_at columns plus
job_queue_dequeue_idx on (status, county_fips, priority, available_at), which
leads with county precisely so a recorder portal can be worked by one or two
polite workers. None of it was ever used: the only code touching the table was
run_with_job_queue's retry wrapper, writing status='error' rows after a network
call had already exhausted its retries. Every row in the table is one of those.

Making it an actual queue needs one thing the schema cannot express yet:
enqueueing must be idempotent. The drop folder rescans, a stage re-runs, a
crashed worker's job is reclaimed, a covenant is re-dropped -- all of which
would otherwise pile up duplicate live jobs that two workers then do twice,
concurrently, to the same covenant. Application-side "check then insert" cannot
fix that; the check and the insert race. A partial unique index can, and pushes
the guarantee down to where concurrency actually resolves.

TWO indexes because a job has two kinds of subject:

  job_queue_live_covid_uniq   (job_type, covid) for work about a covenant that
                              already exists.

  job_queue_live_path_uniq    (job_type, payload->>'path') for INTAKE, which
                              has no covid yet. It cannot have one:
                              job_queue.covid is a FK to covenant, and intake's
                              whole job is to read a dropped file and create
                              that covenant row. So the file path is the
                              subject, and it lives in payload.

Both are predicated on status IN ('queued','in_progress') -- only LIVE work is
unique. A job that finished, errored or went to review must not block the same
work being queued again later; re-running a stage after fixing something is the
normal case, not an anomaly. The first index also requires covid IS NOT NULL:
Postgres treats NULLs as distinct in a unique index, so without it every intake
row would trivially satisfy uniqueness and the predicate would read as though
it covered them when it does not.

Nothing is backfilled. The 11 existing rows are all status='error' and fall
outside both predicates.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

LIVE = "status IN ('queued', 'in_progress')"


def upgrade() -> None:
    op.execute(f"""
        CREATE UNIQUE INDEX job_queue_live_covid_uniq
          ON {SCHEMA}.job_queue (job_type, covid)
          WHERE {LIVE} AND covid IS NOT NULL
    """)
    op.execute(f"""
        CREATE UNIQUE INDEX job_queue_live_path_uniq
          ON {SCHEMA}.job_queue (job_type, (payload->>'path'))
          WHERE {LIVE} AND covid IS NULL AND payload->>'path' IS NOT NULL
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.job_queue.status IS
        'queued: waiting to be claimed. in_progress: leased by locked_by since locked_at; '
        'reclaimed by app/queue/queue.py reclaim_stale if the worker died. done. error: '
        'retries exhausted. needs_review: a human has to answer something before this can '
        'proceed -- not a failure. captcha_pending: blocked on a portal challenge. Only '
        'queued and in_progress are unique per subject; see migration 0042.'
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.job_queue.available_at IS
        'Not claimable before this. Used for retry backoff (fail() pushes it forward) and '
        'for deliberately deferred work. Part of job_queue_dequeue_idx.'
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.job_queue_live_covid_uniq")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.job_queue_live_path_uniq")
