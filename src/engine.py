from util import TitleIndex, tmdb_key


def _dedupe(rows):
    seen, out = set(), []
    for r in rows:
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        out.append(r)
    return out


def consumed(db, source=None):
    return _dedupe(db.items(source, ("watched", "rated")))


class Exclusion:
    def __init__(self, db):
        rows = db.items() + [dict(r, key=r["key"], tmdb=None) for r in db.exclusions()]
        self.imdb = {r["imdb"] for r in rows if r.get("imdb")}
        self.tmdb = {tmdb_key(r["medium"], r["tmdb"]) for r in rows if r.get("tmdb")}
        seen, slim = set(), []
        for r in rows:
            k = (r["norm"], r["year"], r["medium"])
            if r["norm"] and k not in seen:
                seen.add(k)
                slim.append({"norm": r["norm"], "year": r["year"], "medium": r["medium"]})
        self.index = TitleIndex(slim)
        self.size = len({r["key"] for r in rows})

    def matches(self, c, threshold):
        if c.get("imdb") and c["imdb"] in self.imdb:
            return True
        if c.get("tmdb") and tmdb_key(c["medium"], c["tmdb"]) in self.tmdb:
            return True
        # Candidates with an IMDb ID only get exact-title fallback; AI titles without IDs get fuzzy matching.
        fuzzy = not c.get("imdb")
        return self.index.find(c["norm"], c["year"], c["medium"], threshold, fuzzy) is not None


def missing_canon(db, threshold=0.9):
    ex = Exclusion(db)
    return [c for c in db.canon() if not ex.matches(c, threshold)]


def discrepancies(db):
    imdb = consumed(db, "imdb")
    trakt = consumed(db, "trakt")
    t_ids = {i["imdb"] for i in trakt if i["imdb"]}
    i_ids = {i["imdb"] for i in imdb if i["imdb"]}
    skip = ("Video Game", "Episode")
    warning = ""
    if not imdb or not trakt:
        warning = "Load both IMDb and Trakt data before comparing."
    return {
        "warning": warning,
        "imdb_not_trakt": [] if warning else [i for i in imdb if i["medium"] not in skip and i["imdb"] and i["imdb"] not in t_ids],
        "trakt_not_imdb": [] if warning else [i for i in trakt if i["medium"] != "Episode" and (not i["imdb"] or i["imdb"] not in i_ids)],
        "games": [i for i in imdb if i["medium"] == "Video Game"],
    }
