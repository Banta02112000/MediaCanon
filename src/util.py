import re
import unicodedata
from difflib import SequenceMatcher

MEDIA = ("Movie", "TV Series", "Special", "Video Game")
STOP = {"the", "and", "of", "for", "part", "season", "with"}

_IMDB_TYPES = {
    "movie": "Movie", "featurefilm": "Movie", "tvmovie": "Movie", "video": "Movie",
    "tvseries": "TV Series", "tvminiseries": "TV Series", "tvmini": "TV Series",
    "short": "Special", "tvshort": "Special", "tvspecial": "Special", "musicvideo": "Special",
    "videogame": "Video Game", "tvepisode": "Episode",
}


def norm(title):
    if not title:
        return ""
    s = unicodedata.normalize("NFKD", str(title))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\bepisode\s+([ivxlc]+|\d+)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^(the|a|an)\s+", "", s)
    return re.sub(r"\s+", " ", s)


def parse_year(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v or None
    m = re.search(r"(18|19|20)\d{2}", str(v))
    return int(m.group(0)) if m else None


def medium_from_imdb(t):
    key = re.sub(r"[^a-z]", "", (t or "").lower())
    return _IMDB_TYPES.get(key, "Movie")


def medium_from_text(t):
    s = (t or "").lower()
    if "game" in s:
        return "Video Game"
    if any(w in s for w in ("webisode", "special", "short", "minisode", "episode")):
        return "Special"
    if "movie" in s or "film" in s:
        return "Movie"
    if any(w in s for w in ("series", "show", "anime", "tv")):
        return "TV Series"
    return "Movie"


def tmdb_key(medium, tmdb):
    return ("t" if medium == "TV Series" else "m") + str(tmdb)


def year_ok(a, b):
    return not a or not b or abs(int(a) - int(b)) <= 1


def medium_ok(a, b):
    if not a or not b:
        return True
    return (a == "Video Game") == (b == "Video Game")


def tokens(n):
    return [t for t in n.split() if len(t) >= 3 and t not in STOP]


class TitleIndex:
    """Exact + fuzzy title lookup over rows with keys norm/year/medium."""

    def __init__(self, rows):
        self.rows = rows
        self.exact = {}
        self.inv = {}
        for i, r in enumerate(rows):
            self.exact.setdefault(r["norm"], []).append(r)
            for t in set(tokens(r["norm"]) or [r["norm"]]):
                self.inv.setdefault(t, []).append(i)

    def find(self, n, year, medium, threshold=0.9, fuzzy=True):
        for r in self.exact.get(n, ()):
            if year_ok(year, r["year"]) and medium_ok(medium, r["medium"]):
                return r
        if not fuzzy or not n:
            return None
        cand = set()
        for t in set(tokens(n) or [n]):
            cand.update(self.inv.get(t, ()))
        best, best_s = None, 0.0
        for i in cand:
            r = self.rows[i]
            if not (year_ok(year, r["year"]) and medium_ok(medium, r["medium"])):
                continue
            sm = SequenceMatcher(None, n, r["norm"])
            if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                continue
            s = sm.ratio()
            if s >= threshold and s > best_s:
                best, best_s = r, s
        return best
