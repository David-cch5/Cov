#!/usr/bin/env python3
"""Resumable page-level OCR extraction.

Run repeatedly (each run does ~BUDGET seconds of work, safe to kill):
    timeout 43 python3 extract_parallel.py
Prints 'EXTRACTION COMPLETE' when every PDF has a final JSON in _textcache/.
Page-level intermediate cache lives in /tmp/pagecache (VM-local).
"""
import fitz, os, sys, re, json, glob, subprocess, tempfile, time, traceback
from multiprocessing import Pool

SRC = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(SRC, "_textcache")          # final per-PDF json (persists)
PAGES = "/tmp/pagecache"                          # per-page txt (fast, local)
MANIFEST = "/tmp/manifest.json"
BUDGET = 36        # seconds of new work per run
OCR_MIN = 50       # chars/page: below this the page is OCR'd

os.makedirs(CACHE, exist_ok=True)
os.makedirs(PAGES, exist_ok=True)
os.environ["OMP_THREAD_LIMIT"] = "1"

def key_of(rel):
    return re.sub(r"[^A-Za-z0-9._-]", "_", rel)

def atomic_write(path, data, mode="w"):
    tmp = path + ".tmp"
    with open(tmp, mode, encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)

def build_manifest():
    if os.path.exists(MANIFEST):
        return json.load(open(MANIFEST))
    man = {}
    for p in sorted(glob.glob(os.path.join(SRC, "[0-9][0-9][0-9][0-9]*", "*.pdf"))):
        rel = os.path.relpath(p, SRC)
        try:
            d = fitz.open(p); n = len(d); d.close()
        except Exception as e:
            n = -1
        m = re.search(r"\d{4}", os.path.basename(p))
        man[rel] = {"pages": n, "covid": m.group(0) if m else ""}
    atomic_write(MANIFEST, json.dumps(man))
    return man

def do_page(task):
    rel, idx = task
    out = os.path.join(PAGES, f"{key_of(rel)}__{idx:04d}.txt")
    try:
        d = fitz.open(os.path.join(SRC, rel))
        page = d[idx]
        txt = page.get_text("text")
        ocr = 0
        if len(txt.strip()) < OCR_MIN:
            ocr = 1
            with tempfile.TemporaryDirectory() as td:
                png = os.path.join(td, "p.png")
                page.get_pixmap(dpi=150, colorspace=fitz.csGRAY).save(png)
                r = subprocess.run(["tesseract", png, "stdout", "--psm", "6",
                                    "-c", "tessedit_do_invert=0"],
                                   capture_output=True, text=True)
                txt = r.stdout
        d.close()
        atomic_write(out, f"OCR={ocr}\n{txt}")
    except Exception:
        atomic_write(out, f"OCR=0\n[PAGE ERROR]\n{traceback.format_exc()}")
    return rel

def assemble(rel, meta):
    """If all pages cached, write final json and clean page files."""
    k = key_of(rel)
    final = os.path.join(CACHE, k + ".json")
    if os.path.exists(final):
        return True
    if meta["pages"] < 0:
        atomic_write(final, json.dumps({"relpath": rel, "filename": os.path.basename(rel),
                                        "covid": meta["covid"], "pages": 0, "ocr": False,
                                        "text": "", "error": "could not open PDF"}))
        return True
    files = [os.path.join(PAGES, f"{k}__{i:04d}.txt") for i in range(meta["pages"])]
    if not all(os.path.exists(f) for f in files):
        return False
    parts, ocr_any = [], False
    for f in files:
        raw = open(f, encoding="utf-8").read()
        head, _, body = raw.partition("\n")
        if head.strip() == "OCR=1":
            ocr_any = True
        parts.append(body)
    atomic_write(final, json.dumps({"relpath": rel, "filename": os.path.basename(rel),
                                    "covid": meta["covid"], "pages": meta["pages"],
                                    "ocr": ocr_any, "text": "\n".join(parts)}))
    for f in files:
        try: os.remove(f)
        except OSError: pass
    return True

if __name__ == "__main__":
    t0 = time.time()
    man = build_manifest()
    todo = []          # (rel, pageidx) not yet cached
    done_files = 0
    for rel, meta in man.items():
        if assemble(rel, meta):
            done_files += 1
            continue
        k = key_of(rel)
        for i in range(meta["pages"]):
            if not os.path.exists(os.path.join(PAGES, f"{k}__{i:04d}.txt")):
                todo.append((rel, i))
    total = len(man)
    print(f"files {done_files}/{total} done; {len(todo)} pages to OCR", flush=True)
    if done_files == total:
        print("EXTRACTION COMPLETE", flush=True)
        sys.exit(0)

    touched = set()
    with Pool(4) as pool:
        it = pool.imap_unordered(do_page, todo, chunksize=1)
        for rel in it:
            touched.add(rel)
            if time.time() - t0 > BUDGET:
                pool.terminate()
                break
    for rel in touched:
        assemble(rel, man[rel])
    done = len(glob.glob(os.path.join(CACHE, "*.json")))
    print(f"run end: files {done}/{total}", flush=True)
    if done == total:
        print("EXTRACTION COMPLETE", flush=True)
