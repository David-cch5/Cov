# AGENTS.md

Entry point for any AI coding assistant working in this repository.

**The rules live in [`CLAUDE.md`](CLAUDE.md). Read that file first and follow it.** It is
always-current and applies regardless of which model or tool you are — nothing in it is
Claude-specific except the filename, which is a tool convention rather than a scope.

This file deliberately contains no rules of its own. Duplicating them here would create two
sources that drift apart, and the copy that drifts is the one that gets followed by whoever
found it first.

## Where the reasoning lives

Decisions in this project are recorded next to the thing they govern, not in a single log:

| For | Read |
|---|---|
| Standing rules, non-negotiables, tiered policies, model routing, cost discipline | `CLAUDE.md` |
| The original design intent — why the system is shaped this way | `BUILD_SPEC.md` (dated; it says plainly what it is not authoritative on) |
| The schema, and why each part exists | `app/db/migrations/versions/*.py` docstrings — these cannot drift from the schema they create |
| What the code does and the decisions behind it | module docstrings, and `scripts/test_*.py` |
| Why a specific covenant was resolved the way it was | that covenant's own `review_reason` notes in the database |
| Source documents behind a rule | `_termination_examples/` and the covenant PDFs under `<covid>/` |

The tests are the strongest form. A decision written only as prose can be missed; a decision
written as an assertion plus a CHECK constraint fails loudly when violated. "A buyout is always
prospective" is not a note — it is `scripts/test_release.py` and
`covenant_release_buyout_is_prospective`.

## Two things to know before changing anything

**Domain facts here are not derivable from the code.** The release semantics were corrected five
times against real recorded instruments — direction of time, retroactivity, what a buyout can
settle, validity as an adjudicated stage, and buyouts being prospective by nature. If you find
yourself inferring how private-transfer-fee covenants work, stop and ask. The documents in
`_termination_examples/` exist because reading them changed the answer.

**Verify, don't assume, and say what you actually found.** This project's own history is a list
of confident wrong readings caught by an independent check: a traverse closure error that was
exactly the length of a dropped course, 254 "matched" parcels that were 214, a parcel union
masquerading as a deed traverse, an OCR'd decimal comma turning 244.30 ft into 24,430 ft. Every
one surfaced because something re-derived the result rather than trusting it. Do that, and
report failures plainly — a wrong number stated confidently is the expensive outcome here, not
an unfinished task.
