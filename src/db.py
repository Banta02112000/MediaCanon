import os
import sqlite3
import threading
import time

from util import norm, parse_year, tmdb_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, source TEXT, kind TEXT, list_name TEXT DEFAULT '',
  imdb TEXT, tmdb TEXT, trakt TEXT, title TEXT, norm TEXT, year INTEGER DEFAULT 0,
  medium TEXT, rating INTEGER, key TEXT,
  UNIQUE(source, kind, list_name, key));
CREATE INDEX IF NOT EXISTS ix_items_imdb ON items(imdb);
CREATE TABLE IF NOT EXISTS exclusions(
  key TEXT PRIMARY KEY, imdb TEXT, title TEXT, norm TEXT, year INTEGER DEFAULT 0, medium TEXT);
CREATE TABLE IF NOT EXISTS canon(
  id INTEGER PRIMARY KEY, franchise TEXT, title TEXT, norm TEXT, year INTEGER DEFAULT 0,
  medium TEXT, imdb TEXT, tmdb TEXT, origin TEXT,
  UNIQUE(franchise, norm, year, origin));
CREATE TABLE IF NOT EXISTS specials(
  imdb TEXT PRIMARY KEY, title TEXT, status TEXT, note TEXT, checked_at REAL);
CREATE TABLE IF NOT EXISTS franchise_map(
  id INTEGER PRIMARY KEY, franchise TEXT, title TEXT, norm TEXT, imdb TEXT, year INTEGER DEFAULT 0,
  UNIQUE(franchise, norm, year));
CREATE TABLE IF NOT EXISTS tmdb_cache(
  k TEXT PRIMARY KEY, tmdb_id TEXT, collection_id TEXT, collection_name TEXT);
"""


def _row(r):
    d = dict(r)
    if "year" in d:
        d["year"] = d["year"] or None
    return d


class DB:
    def __init__(self, path):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        with self.lock:
            self.con.executescript(SCHEMA)
            self.con.commit()

    # settings
    def get(self, k, default=""):
        with self.lock:
            r = self.con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set(self, k, v):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, str(v)))
            self.con.commit()

    # items
    def add_items(self, items, source, kind, list_name=""):
        rows = []
        for it in items:
            title = it.get("title") or ""
            n = norm(title)
            y = parse_year(it.get("year")) or 0
            med = it.get("medium") or "Movie"
            imdb = it.get("imdb") or None
            tmdb = str(it["tmdb"]) if it.get("tmdb") else None
            if not n and not imdb:
                continue
            key = imdb or (tmdb_key(med, tmdb) if tmdb else f"{n}|{y or ''}")
            trakt = str(it["trakt"]) if it.get("trakt") else None
            rows.append((source, kind, list_name, imdb, tmdb, trakt, title, n, y, med, it.get("rating"), key))
        with self.lock:
            self.con.executemany(
                "INSERT OR REPLACE INTO items(source,kind,list_name,imdb,tmdb,trakt,title,norm,year,medium,rating,key)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.con.commit()
        return len(rows)

    def items(self, source=None, kinds=None):
        sql, args, where = "SELECT * FROM items", [], []
        if source:
            where.append("source=?"); args.append(source)
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds))); args.extend(kinds)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.lock:
            return [_row(r) for r in self.con.execute(sql, args)]

    # exclusions
    def add_exclusion(self, title, year, medium, imdb=None):
        n = norm(title)
        key = imdb or f"{n}|{year or ''}"
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO exclusions VALUES(?,?,?,?,?,?)",
                             (key, imdb, title, n, parse_year(year) or 0, medium))
            self.con.commit()

    def exclusions(self):
        with self.lock:
            return [_row(r) for r in self.con.execute("SELECT * FROM exclusions")]

    # canon
    def replace_canon(self, origin, rows):
        data = []
        for r in rows:
            t = r.get("title") or ""
            if not t.strip():
                continue
            data.append((r.get("franchise") or "Unsorted", t, norm(t), parse_year(r.get("year")) or 0,
                         r.get("medium") or "Movie", r.get("imdb") or None,
                         str(r["tmdb"]) if r.get("tmdb") else None, origin))
        with self.lock:
            self.con.execute("DELETE FROM canon WHERE origin=?", (origin,))
            self.con.executemany(
                "INSERT OR IGNORE INTO canon(franchise,title,norm,year,medium,imdb,tmdb,origin) VALUES(?,?,?,?,?,?,?,?)", data)
            self.con.commit()
        return len(data)

    def canon(self):
        with self.lock:
            return [_row(r) for r in self.con.execute("SELECT * FROM canon")]

    # specials
    def upsert_special(self, imdb, title, status, note):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO specials VALUES(?,?,?,?,?)",
                             (imdb, title, status, note, time.time()))
            self.con.commit()

    def specials(self):
        with self.lock:
            return [dict(r) for r in self.con.execute("SELECT * FROM specials ORDER BY status, title")]

    # franchise mapping files
    def add_franchise_rows(self, rows):
        data = [(r["franchise"], r["title"], norm(r["title"]), r.get("imdb"), parse_year(r.get("year")) or 0)
                for r in rows if r.get("title") and r.get("franchise")]
        with self.lock:
            self.con.executemany(
                "INSERT OR IGNORE INTO franchise_map(franchise,title,norm,imdb,year) VALUES(?,?,?,?,?)", data)
            self.con.commit()
        return len(data)

    def franchise_hints(self):
        with self.lock:
            return {r["norm"]: r["franchise"] for r in self.con.execute("SELECT norm, franchise FROM franchise_map")}

    # tmdb cache
    def cache_get(self, k):
        with self.lock:
            r = self.con.execute("SELECT * FROM tmdb_cache WHERE k=?", (k,)).fetchone()
        return dict(r) if r else None

    def cache_put(self, k, tmdb_id, cid, cname):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO tmdb_cache VALUES(?,?,?,?)", (k, tmdb_id, cid, cname))
            self.con.commit()

    # stats / maintenance
    def counters(self):
        with self.lock:
            c = self.con
            watched = c.execute("SELECT COUNT(DISTINCT key) FROM items WHERE kind IN ('watched','rated')").fetchone()[0]
            excl = c.execute("SELECT COUNT(DISTINCT key) FROM items").fetchone()[0] + \
                c.execute("SELECT COUNT(*) FROM exclusions").fetchone()[0]
            by = {m: n for m, n in c.execute(
                "SELECT medium, COUNT(DISTINCT key) FROM items WHERE kind IN ('watched','rated') GROUP BY medium")}
        return {"watched": watched, "excluded": excl, "movies": by.get("Movie", 0),
                "series": by.get("TV Series", 0), "games": by.get("Video Game", 0)}

    def clear_cache(self):
        """Removes imported/fetched data. Keeps settings and manual exclusions."""
        with self.lock:
            for t in ("items", "canon", "specials", "franchise_map", "tmdb_cache"):
                self.con.execute(f"DELETE FROM {t}")
            self.con.commit()
