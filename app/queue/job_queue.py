"""Retry-with-backoff + durable failure logging, using covenant.job_queue --
a table that has existed since the initial schema (with status values
'error'/'needs_review'/'captcha_pending' clearly meant for exactly this) but
had never actually been written to by any app code until the recorder-portal
adapters started using it. Shared here (rather than living under
app/recorder/) because it isn't recorder-specific: the GIS classifier
(app/gis/classifier.py) hits the exact same class of problem -- a live
third-party network call (an ArcGIS REST query, in that case, vs a
Playwright-driven portal here) that can fail transiently or genuinely break --
and gets the exact same treatment.

This is deliberately NOT self-healing: a broken query/selector/flow still
fails loudly (see the "why not self-healing" discussion this was built
from) -- what this adds is a durable record of *that* failure, immune to bad
luck: a raw exception only exists in whoever's terminal happened to be
running it; a job_queue row is queryable by anyone, any time, and survives
the process exiting. Retries exist only to absorb transient slowness (a
dropped connection, a DNS blip, a rate-limited response) before concluding
the endpoint itself changed or is down -- not to paper over a genuine break.

The failure-log write uses its OWN short-lived session (app.db.session.
get_session()), never the caller's -- confirmed the hard way: writing it
through the caller's session and then raising JobFailed *inside* that same
`with get_session():` block silently rolled the insert back, since
get_session()'s own exception handler rolls back the whole transaction on
any exception leaving the block, including the one this function
deliberately raises. The one thing this module exists to guarantee -- that
the record survives -- doesn't hold if it shares a transaction with the
very failure it's reporting.
"""
import json
import time

from sqlalchemy import text

from app.db.session import get_session


class JobFailed(Exception):
    """Raised after all retries are exhausted. Wraps the last underlying
    exception and the job_queue row's id, so a caller processing many
    covenants can catch just this type, log job_id, and move on to the next
    one rather than letting one broken endpoint halt a whole batch."""

    def __init__(self, job_id: int | None, original_exception: Exception):
        self.job_id = job_id
        self.original_exception = original_exception
        super().__init__(
            f"job failed after retries (job_queue.job_id={job_id}): {original_exception}"
        )


def run_with_job_queue(fn, *, job_type: str, county_fips: str | None = None,
                        covid: int | None = None, payload: dict | None = None,
                        max_attempts: int = 5, backoff_seconds: tuple[int, ...] = (3, 8, 20, 45)):
    """Call the zero-arg callable `fn`, retrying on any exception up to
    max_attempts times (waiting backoff_seconds[i] before attempt i+1 -- the
    first attempt is immediate). Returns fn()'s result on success.

    Defaults give a ~76s total window (3+8+20+45s) across 5 attempts before
    giving up -- widened from an original 3-attempt/~7s window after that
    window turned out to be shorter than a real DNS blip hit while testing
    this against Kerr's AVA portal: it failed 3 times inside ~8 seconds and
    got logged as an "error" for a problem that had actually cleared within
    another few seconds (confirmed independently with a bare curl). Still
    bounded, not infinite -- a genuinely broken endpoint is reported within
    about a minute and a half, not left retrying forever.

    On final failure, writes one covenant.job_queue row (status='error',
    error_message=str(last exception), job_type/county_fips/covid/payload for
    context) in its own committed transaction, then raises JobFailed -- never
    lets the exception vanish silently, and never retries forever hoping a
    fundamentally broken endpoint will start working."""
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        if attempt > 0:
            delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
            print(f"  [job_queue] attempt {attempt} failed ({type(last_exc).__name__}: {last_exc}); "
                  f"retrying in {delay}s (attempt {attempt + 1}/{max_attempts})")
            time.sleep(delay)
        try:
            result = fn()
            if attempt > 0:
                print(f"  [job_queue] succeeded on attempt {attempt + 1}/{max_attempts}")
            return result
        except Exception as e:
            last_exc = e

    print(f"  [job_queue] all {max_attempts} attempts failed, writing job_queue row")
    job_id = _record_failure(
        job_type=job_type, county_fips=county_fips, covid=covid,
        payload=payload, attempts=max_attempts, error=last_exc,
    )
    raise JobFailed(job_id, last_exc) from last_exc


def _record_failure(*, job_type: str, county_fips: str | None, covid: int | None,
                     payload: dict | None, attempts: int, error: Exception) -> int:
    with get_session() as session:
        return session.execute(
            text("""
                INSERT INTO job_queue (job_type, covid, county_fips, payload, status, attempts, error_message)
                VALUES (:job_type, :covid, :county_fips, (:payload)::jsonb, 'error', :attempts, :error_message)
                RETURNING job_id
            """),
            {
                "job_type": job_type, "covid": covid, "county_fips": county_fips,
                "payload": json.dumps(payload or {}), "attempts": attempts,
                "error_message": f"{type(error).__name__}: {error}",
            },
        ).scalar_one()
