import time
from difflib import SequenceMatcher

import requests

import engine
from util import norm

BASE = "https://api.themoviedb.org/3"


class TMDB:
    def __init__(self, key):
        self.key = key.strip()
        if not self.key:
            raise RuntimeError("Enter a TMDB API key in Settings first.")
        self.n = 0
        self.s = requests.Session()

    def get(self, path, **params):
        self.n += 1
        if self.n % 40 == 0:
            time.sleep(1.0)  # polite pause to avoid HTTP 429
        headers = {"accept": "application/json"}
        if self.key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        for _ in range(4):
            r = self.s.get(BASE + path, params=params, headers=headers, timeout=25)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "3")) + 1)
                continue
            if r.status_code == 404:
                return {}
            if r.status_code == 401:
                raise RuntimeError("TMDB rejected the API key.")
            r.raise_for_status()
            return r.json()
        raise RuntimeError("TMDB kept returning 429 (rate limit). Try again in a minute.")


def collection_scan(db, key, progress):
    t = TMDB(key)
    movies = {}
    for it in engine.consumed(db):
        if it["medium"] == "Movie" and (it["imdb"] or it["tmdb"]):
            movies[it["imdb"] or "tmdb:" + it["tmdb"]] = it
    keys = list(movies)
    collections = {}
    for i, k in enumerate(keys):
        it = movies[k]
        cached = db.cache_get(k)
        if cached is None:
            tid = it["tmdb"]
            if not tid:
                res = t.get(f"/find/{it['imdb']}", external_source="imdb_id")
                mr = res.get("movie_results") or []
                tid = mr[0]["id"] if mr else None
            cid = cname = None
            if tid:
                c = t.get(f"/movie/{tid}").get("belongs_to_collection") or {}
                cid, cname = c.get("id"), c.get("name")
            db.cache_put(k, str(tid) if tid else None, str(cid) if cid else None, cname)
        else:
            cid, cname = cached["collection_id"], cached["collection_name"]
        if cid:
            collections[cid] = cname
        if i % 5 == 0:
            progress(0.5 * (i + 1) / max(len(keys), 1), f"Finding collections {i + 1}/{len(keys)}")
    rows = []
    for j, (cid, cname) in enumerate(collections.items()):
        col = t.get(f"/collection/{cid}")
        for p in col.get("parts") or []:
            m = t.get(f"/movie/{p['id']}", append_to_response="external_ids")
            imdb = (m.get("external_ids") or {}).get("imdb_id") or m.get("imdb_id") or None
            rows.append({"franchise": cname or col.get("name") or "Collection", "title": p.get("title"),
                         "year": (p.get("release_date") or "")[:4], "medium": "Movie", "imdb": imdb, "tmdb": p["id"]})
        progress(0.5 + 0.5 * (j + 1) / max(len(collections), 1), f"Collection {j + 1}/{len(collections)}: {cname}")
    n = db.replace_canon("tmdb", rows)
    progress(1.0, f"TMDB scan done: {len(collections)} collections, {n} entries")
    return n


def _resolve_special(t, it, th):
    imdb = it["imdb"]
    res = t.get(f"/find/{imdb}", external_source="imdb_id")
    eps = res.get("tv_episode_results") or []
    if eps:
        e = eps[0]
        sn, en = int(e.get("season_number") or 0), int(e.get("episode_number") or 0)
        if sn == 0:
            return ("Linked Season 0 special",
                    f"TMDB show {e.get('show_id')} S00E{en:02d} carries this IMDb ID, so Trakt files it under the "
                    f"parent series' Specials instead of as its own entry.")
        return ("Regular episode", f"Episode S{sn}E{en} of TMDB show {e.get('show_id')}; Trakt tracks it inside the show.")
    if res.get("movie_results"):
        return ("On TMDB as movie", "TMDB has a movie record. If Trakt still lacks it, wait for Trakt's ~24h TMDB sync.")
    if res.get("tv_results"):
        return ("On TMDB as series", "TMDB has a series record. If Trakt still lacks it, wait for Trakt's ~24h TMDB sync.")
    title = it["title"] or ""
    if ":" not in title:
        return ("Not on TMDB", f"No TMDB record is bound to {imdb}. Add the IMDb ID to the matching TMDB record "
                               f"(or create it) so Trakt can sync it within 24h.")
    parent, sub = [s.strip() for s in title.split(":", 1)]
    for show in (t.get("/search/tv", query=parent).get("results") or [])[:3]:
        s0 = t.get(f"/tv/{show['id']}/season/0")
        best, bs = None, 0.0
        for ep in s0.get("episodes") or []:
            s = SequenceMatcher(None, norm(sub), norm(ep.get("name", ""))).ratio()
            if s > bs:
                best, bs = ep, s
        if best and bs >= min(th, 0.8):
            en = int(best["episode_number"])
            ext = t.get(f"/tv/{show['id']}/season/0/episode/{en}/external_ids")
            if not ext.get("imdb_id"):
                return ("Nested special (TMDB lacks IMDb ID)",
                        f"Found as S00E{en:02d} of TMDB show {show['id']} ({show.get('name')}), but that episode has no "
                        f"imdb_id. Contribute {imdb} on TMDB so Trakt links it in its next 24h sync.")
            return ("Nested special (different IMDb ID)",
                    f"Found as S00E{en:02d} of TMDB show {show['id']}, bound to {ext['imdb_id']} instead of {imdb}.")
    return ("Unlinked", f"No Season 0 episode on a matching parent series; {imdb} has no TMDB link. "
                        f"Contribute the IMDb ID on TMDB if the entry exists there.")


def season0_scan(db, key, threshold, progress, check_all=False, limit=250):
    t = TMDB(key)
    d = engine.discrepancies(db)
    cands = [i for i in d["imdb_not_trakt"] if check_all or i["medium"] == "Special"][:limit]
    if not cands:
        progress(1.0, d["warning"] or "No IMDb-only specials to check.")
        return 0
    for n, it in enumerate(cands):
        status, note = _resolve_special(t, it, threshold)
        db.upsert_special(it["imdb"], it["title"], status, note)
        progress((n + 1) / len(cands), f"Specials {n + 1}/{len(cands)}: {it['title']}")
    progress(1.0, f"Checked {len(cands)} items")
    return len(cands)
