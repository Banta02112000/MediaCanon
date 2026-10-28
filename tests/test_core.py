import csv
import io
import json
import os
import sys
import tempfile
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import engine, gemini_api, parsers, tmdb_api  # noqa: E402
from db import DB  # noqa: E402
from util import norm  # noqa: E402

HDR = ["Position", "Const", "Created", "Modified", "Description", "Title", "Original Title", "URL", "Title Type",
       "IMDb Rating", "Runtime (mins)", "Year", "Genres", "Num Votes", "Release Date", "Directors", "Your Rating", "Date Rated"]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HDR)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HDR})


def resp(js, code=200, headers=None):
    m = mock.Mock(status_code=code, headers=headers or {})
    m.json.return_value = js
    m.text = json.dumps(js)
    return m


def main():
    tmp = tempfile.mkdtemp()
    db = DB(os.path.join(tmp, "t.db"))

    assert norm("Star Wars: Episode IV - A New Hope") == norm("Star Wars: A New Hope")

    # IMDb ratings CSV (auto) + exclusion list
    p1 = os.path.join(tmp, "ratings.csv")
    write_csv(p1, [
        {"Const": "tt0133093", "Title": "The Matrix", "Title Type": "movie", "Year": "1999", "Your Rating": "9"},
        {"Const": "tt0944947", "Title": "Game of Thrones", "Title Type": "tvSeries", "Year": "2011", "Your Rating": "8"},
        {"Const": "tt3235888", "Title": "Elden Ring", "Title Type": "videoGame", "Year": "2022", "Your Rating": "10"},
        {"Const": "tt9999991", "Title": "The Walking Dead: Red Machete", "Title Type": "short", "Year": "2012", "Your Rating": "7"},
        {"Const": "tt0000001", "Title": "Some Episode", "Title Type": "tvEpisode", "Year": "2012", "Your Rating": "7"},
    ])
    assert parsers.import_imdb_csv(db, p1, "auto") == 5
    p2 = os.path.join(tmp, "to-watch.csv")
    write_csv(p2, [{"Const": "tt0234215", "Title": "The Matrix Reloaded", "Title Type": "movie", "Year": "2003"}])
    assert parsers.import_imdb_csv(db, p2, "list") == 1

    # Trakt ZIP
    zp = os.path.join(tmp, "trakt.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("watched-history-26.json", json.dumps([
            {"type": "movie", "movie": {"title": "The Matrix", "year": 1999, "ids": {"imdb": "tt0133093", "tmdb": 603, "trakt": 1}}},
            {"type": "episode", "episode": {"season": 1, "number": 1, "ids": {}},
             "show": {"title": "The Walking Dead", "year": 2010, "ids": {"imdb": "tt1520211", "tmdb": 1402}}}]))
        z.writestr("lists-list-31109335-to-watch.json", json.dumps([
            {"type": "movie", "movie": {"title": "Alien", "year": 1979, "ids": {"imdb": "tt0078748", "tmdb": 348}}},
            {"type": "person", "person": {"name": "x"}}]))
        z.writestr("ratings-movies.json", json.dumps([
            {"rating": 8, "type": "movie", "movie": {"title": "Heat", "year": 1995, "ids": {"imdb": "tt0113277"}}}]))
    assert parsers.import_trakt_zip(db, zp) == 4
    assert any(i["list_name"] == "to-watch" for i in db.items("trakt", ["list"]))

    # Discrepancies
    d = engine.discrepancies(db)
    assert {i["imdb"] for i in d["imdb_not_trakt"]} == {"tt0944947", "tt9999991"}, d["imdb_not_trakt"]
    assert [i["title"] for i in d["games"]] == ["Elden Ring"]
    assert {i["title"] for i in d["trakt_not_imdb"]} == {"The Walking Dead", "Heat"}

    # Franchise files
    fp = os.path.join(tmp, "grouped_by_franchise.txt")
    open(fp, "w").write("# The Matrix\n- The Matrix (1999)\n- The Matrix Reloaded (2003)\nStar Wars: A New Hope; Rogue One\n")
    assert parsers.import_franchise_file(db, fp) >= 2
    fc = os.path.join(tmp, "franchises_grouped_by_connected_universe.csv")
    open(fc, "w").write("Connected Universe,Title,Const,Year\nWhoniverse,Doctor Who,tt0436992,2005\n")
    assert parsers.import_franchise_file(db, fc) == 1

    # Subtraction: exact id, fuzzy AI title, year guard, manual exclusion
    db.replace_canon("gemini", [
        {"franchise": "Matrix", "title": "The Matrix", "year": "1999", "medium": "Movie"},              # fuzzy/exact -> excluded
        {"franchise": "Matrix", "title": "The Matrix Resurrections", "year": "2021", "medium": "Movie"},  # missing
        {"franchise": "Matrix", "title": "Enter the Matrix", "year": "2003", "medium": "Video Game"},      # missing
        {"franchise": "Star Wars", "title": "Alien", "year": "1986", "medium": "Movie"},                  # year guard -> missing
        {"franchise": "TWD", "title": "Walking Dead: Red Machete", "year": "2012", "medium": "Special"},  # fuzzy -> excluded
    ])
    db.replace_canon("tmdb", [{"franchise": "Matrix", "title": "The Matrix Reloaded", "year": "2003", "medium": "Movie", "imdb": "tt0234215"}])
    miss = {c["title"] for c in engine.missing_canon(db, 0.8)}
    assert miss == {"The Matrix Resurrections", "Enter the Matrix", "Alien"}, miss
    db.add_exclusion("Enter the Matrix", 2003, "Video Game")
    assert "Enter the Matrix" not in {c["title"] for c in engine.missing_canon(db, 0.8)}
    c = db.counters()
    assert c["games"] == 1 and c["watched"] >= 6, c

    # TMDB collection scan with mocked HTTP
    def fake_get(url, params=None, headers=None, timeout=None):
        path = url.replace(tmdb_api.BASE, "")
        if path.startswith("/find/tt0133093"):
            return resp({"movie_results": [{"id": 603}]})
        if path == "/movie/603":
            return resp({"belongs_to_collection": {"id": 2344, "name": "The Matrix Collection"}})
        if path == "/collection/2344":
            return resp({"name": "The Matrix Collection", "parts": [
                {"id": 603, "title": "The Matrix", "release_date": "1999-03-30"},
                {"id": 604, "title": "The Matrix Reloaded", "release_date": "2003-05-15"},
                {"id": 624860, "title": "The Matrix Resurrections", "release_date": "2021-12-16"}]})
        if path.startswith("/movie/"):
            ids = {603: "tt0133093", 604: "tt0234215", 624860: "tt10838180"}
            return resp({"external_ids": {"imdb_id": ids[int(path.split("/")[2])]}})
        return resp({})
    with mock.patch("requests.Session.get", side_effect=lambda url, **kw: fake_get(url, **kw)):
        db2 = DB(os.path.join(tmp, "t2.db"))
        db2.add_items([{"imdb": "tt0133093", "title": "The Matrix", "year": 1999, "medium": "Movie", "rating": 9}], "imdb", "rated")
        n = tmdb_api.collection_scan(db2, "KEY", lambda f, t: None)
        assert n == 3
        assert [c["title"] for c in engine.missing_canon(db2)] == ["The Matrix Reloaded", "The Matrix Resurrections"] or \
            {c["title"] for c in engine.missing_canon(db2)} == {"The Matrix Reloaded", "The Matrix Resurrections"}

    # Season 0 resolution with mocked HTTP
    def fake_s0(url, **kw):
        path = url.replace(tmdb_api.BASE, "")
        if path.startswith("/find/tt9999991"):
            return resp({})
        if path == "/search/tv":
            return resp({"results": [{"id": 1402, "name": "The Walking Dead"}]})
        if path == "/tv/1402/season/0":
            return resp({"episodes": [{"episode_number": 4, "name": "Red Machete"}]})
        if path.endswith("/external_ids"):
            return resp({"imdb_id": None})
        return resp({})
    with mock.patch("requests.Session.get", side_effect=lambda url, **kw: fake_s0(url, **kw)):
        tmdb_api.season0_scan(db, "KEY", 0.9, lambda f, t: None)
    sp = db.specials()
    assert sp and sp[0]["status"] == "Nested special (TMDB lacks IMDb ID)", sp

    # Gemini parsing + scan with mocked HTTP
    assert len(gemini_api.parse_json_array('```json\n[{"a":1},{"b":2},{"c"')) == 2
    out = json.dumps([{"franchise": "Whoniverse", "title": "Doctor Who", "medium": "TV Series", "year": "2005"},
                      {"franchise": "Whoniverse", "title": "Torchwood", "medium": "TV Series", "year": "2006"}])
    gm = resp({"candidates": [{"content": {"parts": [{"text": out}]}}]})
    with mock.patch("requests.post", return_value=gm):
        assert gemini_api.multiverse_scan(db, "K", "gemini-2.5-flash", lambda f, t: None) == 2
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
