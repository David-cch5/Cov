"""Pipeline orchestration: the stages a covenant passes through, and the runner
that walks it through them off the job queue.

Before this, every covenant was driven by a human plus a hand-written script.
scripts/ still holds the evidence -- commit_covid3346_tract1.py,
manual_commit_covid4780_tract1.py, reanchor_covid5838_tract1.py,
resolve_covid5839_tract2.py -- one file per covenant, per stage, per problem.
That is the pattern CLAUDE.md's anchor rule was written to end ("never anchor a
tract by hand-writing a one-off script per covenant again"), and
app/gis/anchor_resolver.py ended it for the anchoring step only. This ends it
for the rest.
"""
