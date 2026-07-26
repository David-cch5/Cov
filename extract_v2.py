#!/usr/bin/env python3
"""High-quality re-OCR (v2): 300dpi, psm3, per-page psm1 fallback for pages that
score poorly (rotated pages / hard scans). Fully resumable; all state lives in
_ocr2_work/ (inside the mounted folder, survives VM restarts).

Run repeatedly:  timeout 42 python3 extract_v2.py
Prints 'EXTRACTION V2 COMPLETE' when every PDF has a json in _textcache_v2/.
"""
import fitz, os, sys, re, json, glob, subprocess, tempfile, time, traceback
from collections import Counter
from multiprocessing import Pool

SRC = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(SRC, "_ocr2_work")
PAGES = os.path.join(WORK, "pages")
CACHE2 = os.path.join(SRC, "_textcache_v2")
OLD = os.path.join(SRC, "_textcache")
BUDGET = 36
OCR_MIN = 50

os.makedirs(PAGES, exist_ok=True)
os.makedirs(CACHE2, exist_ok=True)
os.environ["OMP_THREAD_LIMIT"] = "1"

def key_of(rel): return re.sub(r"[^A-Za-z0-9._-]", "_", rel)

def atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)

# ---- corpus vocabulary for scoring OCR quality --------------------------------
VOCAB_F = os.path.join(WORK, "vocab.json")
if os.path.exists(VOCAB_F):
    VOCAB = set(json.load(open(VOCAB_F)))
else:
    df = Counter()
    for f in glob.glob(os.path.join(OLD, "*.json")):
        d = json.load(open(f, encoding="utf-8"))
        df.update(set(re.findall(r"[a-z]{4,}", d["text"].lower())))
    VOCAB = {w for w, c in df.items() if c >= 30}
    atomic(VOCAB_F, json.dumps(sorted(VOCAB)))
    print(f"vocab built: {len(VOCAB)} words", flush=True)

def score(txt):
    ws = re.findall(r"[a-z]{4,}", txt.lower())
    if len(ws) < 8: return 0.0
    return sum(1 for w in ws if w in VOCAB) / len(ws)

# ---- manifest ------------------------------------------------------------------
MAN_F = os.path.join(WORK, "manifest.json")
if os.path.exists(MAN_F):
    man = json.load(open(MAN_F))
else:
    man = {}
    for p in sorted(glob.glob(os.path.join(SRC, "[0-9][0-9][0-9][0-9]*", "*.pdf"))):
        rel = os.path.relpath(p, SRC)
        try:
            d = fitz.open(p); n = len(d); d.close()
        except Exception:
            n = -1
        m = re.search(r"\d{4}", os.path.basename(p))
        man[rel] = {"pages": n, "covid": m.group(0) if m else ""}
    atomic(MAN_F, json.dumps(man))

def ocr_page(page, psm, dpi, tmo):
    """returns text or None on timeout. Render capped at 4500px on the long side
    so oversized sheets (35x45in plats) can't hang the renderer."""
    with tempfile.TemporaryDirectory() as td:
        png = os.path.join(td, "p.png")
        zoom = dpi / 72.0
        longside = max(page.rect.width, page.rect.height)
        if longside * zoom > 4500:
            zoom = 4500 / longside
        page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY).save(png)
        try:
            r = subprocess.run(["tesseract", png, "stdout", "--psm", str(psm),
                                "-c", "tessedit_do_invert=0"],
                               capture_output=True, text=True, timeout=tmo)
            return r.stdout
        except subprocess.TimeoutExpired:
            return None

def do_page(task):
    rel, idx = task
    out = os.path.join(PAGES, f"{key_of(rel)}__{idx:04d}.txt")
    try:
        d = fitz.open(os.path.join(SRC, rel))
        page = d[idx]
        txt = page.get_text("text")
        used = "embedded"
        if len(txt.strip()) < OCR_MIN:
            txt = ocr_page(page, 3, 300, 25)
            used = "psm3"
            if txt is None:                  # very slow page: fast fallback
                txt = ocr_page(page, 6, 200, 15)
                used = "psm6-200"
            if txt is None:
                txt, used = "", "timeout"
            elif score(txt) < 0.5 and used == "psm3":
                alt = ocr_page(page, 1, 300, 20)   # OSD handles rotated pages
                if alt is not None and score(alt) > score(txt):
                    txt, used = alt, "psm1"
        d.close()
        atomic(out, f"SRC={used}\n{txt}")
    except Exception:
        atomic(out, f"SRC=error\n[PAGE ERROR]\n{traceback.format_exc()}")
    return rel

def assemble(rel, meta):
    final = os.path.join(CACHE2, key_of(rel) + ".json")
    if os.path.exists(final): return True
    if meta["pages"] < 0:
        atomic(final, json.dumps({"relpath": rel, "filename": os.path.basename(rel),
                                  "covid": meta["covid"], "pages": 0, "ocr": False,
                                  "text": "", "error": "could not open PDF"}))
        return True
    files = [os.path.join(PAGES, f"{key_of(rel)}__{i:04d}.txt") for i in range(meta["pages"])]
    if not all(os.path.exists(f) for f in files): return False
    parts, ocr_any = [], False
    for f in files:
        raw = open(f, encoding="utf-8").read()
        head, _, body = raw.partition("\n")
        if head.strip() != "SRC=embedded": ocr_any = True
        parts.append(body)
    text = "\n".join(parts)
    atomic(final, json.dumps({"relpath": rel, "filename": os.path.basename(rel),
                              "covid": meta["covid"], "pages": meta["pages"],
                              "ocr": ocr_any, "text": text,
                              "vocab_score": round(score(text), 4)}))
    for f in files:
        try: os.remove(f)
        except OSError: pass
    return True

if __name__ == "__main__":
    t0 = time.time()
    todo, done_files = [], 0
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
        print("EXTRACTION V2 COMPLETE", flush=True)
        sys.exit(0)
    touched = set()
    with Pool(4) as pool:
        for rel in pool.imap_unordered(do_page, todo, chunksize=1):
            touched.add(rel)
            if time.time() - t0 > BUDGET:
                pool.terminate()
                break
    for rel in touched:
        assemble(rel, man[rel])
    done = len(glob.glob(os.path.join(CACHE2, "*.json")))
    print(f"run end: files {done}/{total}", flush=True)
    if done == total:
        print("EXTRACTION V2 COMPLETE", flush=True)
