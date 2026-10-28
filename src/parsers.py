import csv
import json
import os
import re
import zipfile

from util import medium_from_imdb, parse_year


def _g(row, *names):
    for n in names:
        for k in row:
            if k and k.strip().lower() == n.lower():
                v = row[k]
                return v.strip() if isinstance(v, str) else v
    return ""


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def import_imdb_csv(db, path, kind="auto", list_name=""):
    """kind='auto': rows with 'Your Rating' -> rated, others -> watched. Otherwise the given kind
    (e.g. 'list' for exclusion lists / watchlists)."""
    groups = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            const = _g(row, "Const")
            title = _g(row, "Title", "Original Title")
            if not const and not title:
                continue
            rating = _to_int(_g(row, "Your Rating"))
            k = kind
            if kind == "auto":
                k = "rated" if rating else "watched"
            groups.setdefault(k, []).append({
                "imdb": const if const.startswith("tt") else None,
                "title": title,
                "year": parse_year(_g(row, "Year")),
                "medium": medium_from_imdb(_g(row, "Title Type")),
                "rating": rating,
            })
    total = 0
    ln = list_name or (os.path.splitext(os.path.basename(path))[0] if kind == "list" else "")
    for k, items in groups.items():
        total += db.add_items(items, "imdb", k, ln)
    return total


def import_franchise_file(db, path):
    rows = []
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            heads = rd.fieldnames or []
            low = {h: h.lower() for h in heads}
            fcol = next((h for h in heads if "universe" in low[h]), None) or \
                next((h for h in heads if "franchise" in low[h]), None) or (heads[0] if heads else None)
            tcol = next((h for h in heads if low[h] in ("title", "name")), None) or \
                next((h for h in heads if "title" in low[h]), None) or (heads[1] if len(heads) > 1 else None)
            icol = next((h for h in heads if low[h] in ("const", "imdb", "imdb_id", "imdb id")), None)
            ycol = next((h for h in heads if "year" in low[h]), None)
            for r in rd:
                if fcol and tcol and r.get(fcol) and r.get(tcol):
                    rows.append({"franchise": r[fcol].strip(), "title": r[tcol].strip(),
                                 "imdb": (r.get(icol) or "").strip() or None if icol else None,
                                 "year": r.get(ycol) if ycol else None})
    else:
        current = None
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                if re.match(r"^(#+|=+|-{3,})", s) or s.endswith(":"):
                    current = re.sub(r"^[#=\-\s]+|[=\-\s:]+$", "", s)
                    continue
                if ":" in s and ";" in s:  # "Franchise: a; b; c"
                    head, rest = s.split(":", 1)
                    for t in rest.split(";"):
                        if t.strip():
                            rows.append({"franchise": head.strip(), "title": t.strip()})
                    continue
                if current:
                    t = re.sub(r"^[\-\*\u2022\d\.\)\s]+", "", s)
                    m = re.search(r"\((\d{4})\)\s*$", t)
                    rows.append({"franchise": current, "title": re.sub(r"\s*\(\d{4}\)\s*$", "", t),
                                 "year": m.group(1) if m else None})
    return db.add_franchise_rows(rows)


# ---------- Trakt ----------

def trakt_entries(data):
    """Yield movie/show records from any Trakt JSON shape (history, collection, ratings, lists)."""
    stack = [data]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            stack.extend(n)
            continue
        if not isinstance(n, dict):
            continue
        found = False
        for k, medium in (("movie", "Movie"), ("show", "TV Series")):
            v = n.get(k)
            if isinstance(v, dict) and v.get("title"):
                ids = v.get("ids") or {}
                rating = n.get("rating") if n.get("type") in (None, "movie", "show") else None
                yield {"medium": medium, "title": v["title"], "year": v.get("year"),
                       "imdb": ids.get("imdb"), "tmdb": ids.get("tmdb"), "trakt": ids.get("trakt"),
                       "rating": rating if isinstance(rating, int) else None}
                found = True
        if not found:
            stack.extend(x for x in n.values() if isinstance(x, (dict, list)))


def route_trakt_file(base):
    b = base.lower()
    if b.startswith("watched"):
        return "watched", ""
    if b.startswith("ratings"):
        return "rated", ""
    if b.startswith("collection"):
        return "collection", ""
    if b.startswith("lists-watchlist") or b == "watchlist":
        return "watchlist", ""
    if b.startswith("lists-list-"):
        return "list", re.sub(r"^\d+-", "", b[len("lists-list-"):])
    if b.startswith("lists-"):
        return "list", b[len("lists-"):]
    return None, ""


def store_trakt(db, data, kind, list_name=""):
    items = list(trakt_entries(data))
    if kind == "rated":
        items = [i for i in items if i["rating"]]
    return db.add_items(items, "trakt", kind, list_name)


def import_trakt_zip(db, path, progress=None):
    total = 0
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        for i, name in enumerate(names):
            base = os.path.basename(name)[:-5]
            kind, ln = route_trakt_file(base)
            if progress:
                progress(i / max(len(names), 1), f"Reading {base}")
            if not kind:
                continue
            try:
                data = json.loads(zf.read(name).decode("utf-8-sig"))
            except ValueError:
                continue
            total += store_trakt(db, data, kind, ln)
    return total
