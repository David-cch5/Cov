#!/usr/bin/env python3
"""Build _textcache_final/: for each document choose the higher-quality text
between the original OCR (_textcache) and the high-quality re-OCR (_textcache_v2),
scored by corpus-vocabulary hit rate. Writes a comparison report."""
import os, re, json, glob, csv
from collections import Counter

SRC = os.path.dirname(os.path.abspath(__file__))
OLD, NEW = os.path.join(SRC, "_textcache"), os.path.join(SRC, "_textcache_v2")
OUT = os.path.join(SRC, "_textcache_final")
os.makedirs(OUT, exist_ok=True)

VOCAB = set(json.load(open(os.path.join(SRC, "_ocr2_work", "vocab.json"))))
def score(t):
    ws = re.findall(r"[a-z]{4,}", t.lower())
    if len(ws) < 8: return 0.0
    return sum(1 for w in ws if w in VOCAB) / len(ws)

rows, better, worse, tie = [], 0, 0, 0
for f in sorted(glob.glob(os.path.join(OLD, "*.json"))):
    name = os.path.basename(f)
    old = json.load(open(f, encoding="utf-8"))
    nf = os.path.join(NEW, name)
    if os.path.exists(nf):
        new = json.load(open(nf, encoding="utf-8"))
        so, sn = score(old["text"]), score(new["text"])
        # prefer new unless clearly worse (new = higher dpi + fallback)
        pick = new if sn >= so - 0.01 else old
        which = "new" if pick is new else "old"
        if sn > so + 0.005: better += 1
        elif sn < so - 0.005: worse += 1
        else: tie += 1
    else:
        pick, which, so, sn = old, "old(only)", score(old["text"]), 0
    json.dump(pick, open(os.path.join(OUT, name), "w", encoding="utf-8"))
    rows.append([old["covid"], which, round(so, 4), round(sn, 4)])

with open(os.path.join(SRC, "_ocr2_work", "merge_report.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["CovID", "Chosen", "OldScore", "NewScore"])
    w.writerows(rows)
import statistics
olds = [r[2] for r in rows]; news = [r[3] for r in rows if r[3]]
print(f"docs: {len(rows)} | new better: {better}, old better: {worse}, tie: {tie}")
print(f"mean vocab score old {statistics.mean(olds):.4f} -> new {statistics.mean(news):.4f}")
print("MERGE COMPLETE")
