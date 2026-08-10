"""Regenerate app/ingestion/deed_vocabulary.txt from the document corpus.

The supplement exists because /usr/share/dict/words does not know deed
vocabulary, Texas county and subdivision names, or the party names that recur
across a thousand recorded instruments -- and without them a legibility score
punishes perfectly clean text for containing the words these documents are
made of.

Self-calibrating rather than hand-curated: a token appearing in at least
MIN_DOCUMENT_FRACTION of independently-recorded instruments is real vocabulary,
because OCR noise does not repeat the same misreading across hundreds of
separate scans. That also means the list improves on its own as the corpus
grows, with no judgment calls to maintain.

Run after the corpus changes materially. Committed output, so a fresh checkout
does not need the corpus present to score text.

Usage: python3 scripts/regen_deed_vocabulary.py
"""
import collections
import glob
import json
import os
import re
import sys

sys.path.insert(0, ".")

from app.ingestion.text_extract import DEED_VOCABULARY, SYSTEM_WORDS

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXTCACHE_FINAL = os.path.join(PROJECT_ROOT, "_textcache_final")

MIN_DOCUMENT_FRACTION = 0.05
MAX_TOKEN_LENGTH = 20  # beyond this it is run-together text, not a word
_TOKEN = re.compile(r"[A-Za-z]{2,}")


def main() -> int:
    paths = sorted(glob.glob(os.path.join(TEXTCACHE_FINAL, "*.json")))
    if not paths:
        print(f"no corpus at {TEXTCACHE_FINAL} -- nothing to regenerate")
        return 1
    with open(SYSTEM_WORDS, encoding="utf-8", errors="replace") as f:
        system = {w.strip().lower() for w in f if w.strip()}

    document_frequency: collections.Counter = collections.Counter()
    for path in paths:
        with open(path, encoding="utf-8") as f:
            text = json.load(f).get("text") or ""
        document_frequency.update({t.lower() for t in _TOKEN.findall(text)})

    n = len(paths)
    floor = MIN_DOCUMENT_FRACTION * n
    supplement = sorted(
        w for w, c in document_frequency.items()
        if c >= floor and w not in system and len(w) <= MAX_TOKEN_LENGTH
    )

    with open(DEED_VOCABULARY, "w", encoding="utf-8") as f:
        f.write("# Deed/covenant vocabulary present in this corpus but absent from "
                "/usr/share/dict/words.\n")
        f.write(f"# Generated from all {n} documents in _textcache_final/: every alphabetic "
                f"token (>=2 chars,\n")
        f.write(f"# <={MAX_TOKEN_LENGTH} chars) appearing in at least "
                f"{MIN_DOCUMENT_FRACTION:.0%} of documents. A token that common across a\n")
        f.write("# thousand independently-recorded instruments is real vocabulary -- deed terms, "
                "Texas\n")
        f.write("# county and subdivision names, party names -- not OCR noise. Regenerate with\n")
        f.write("# scripts/regen_deed_vocabulary.py if the corpus changes.\n")
        for w in supplement:
            f.write(w + "\n")

    print(f"wrote {DEED_VOCABULARY}: {len(supplement)} tokens "
          f"from {n} documents (floor: appears in >={floor:.0f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
