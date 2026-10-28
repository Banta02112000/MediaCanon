import os

import flet as ft

import engine
import gemini_api
import parsers
import tmdb_api
import trakt_api
from db import DB

MEDIUM_COLORS = {"Movie": ft.Colors.BLUE_700, "TV Series": ft.Colors.TEAL_700,
                 "Special": ft.Colors.ORANGE_800, "Video Game": ft.Colors.PURPLE_700, "Episode": ft.Colors.GREY_700}
DEFAULT_PROFILE = "PKCE / Public Client (App 232427)"


def data_dir():
    return os.environ.get("FLET_APP_STORAGE_DATA") or os.path.join(os.path.expanduser("~"), ".canonsync")


def main(page: ft.Page):
    page.title = "CanonSync"
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO, use_material3=True)
    page.padding = 0
    db = DB(os.path.join(data_dir(), "canonsync.db"))
    state = {"verifier": None}

    # ---------- shared helpers ----------
    status = ft.Text("Ready.", size=12)
    bar = ft.ProgressBar(value=0)

    def prog(frac, text=None):
        bar.value = max(0.0, min(1.0, frac))
        if text is not None:
            status.value = text
        page.update()

    def toast(msg):
        page.open(ft.SnackBar(ft.Text(msg)))

    def run_bg(fn):
        def wrapper():
            try:
                fn()
            except Exception as ex:  # surface every failure to the user
                status.value = f"Error: {ex}"
                toast(f"Error: {ex}")
            finally:
                page.update()
        page.run_thread(wrapper)

    def threshold():
        try:
            return max(0.7, min(1.0, float(db.get("threshold", "90")) / 100))
        except ValueError:
            return 0.9

    def badge(medium):
        return ft.Container(ft.Text(medium or "?", size=11), bgcolor=MEDIUM_COLORS.get(medium, ft.Colors.GREY_700),
                            padding=ft.padding.symmetric(horizontal=8, vertical=2), border_radius=8)

    # ---------- settings ----------
    def val(k, d=""):
        return db.get(k, d)

    username = ft.TextField(label="Trakt username", value=val("username", "Banta2000"))
    profile_dd = ft.Dropdown(label="Trakt API profile", value=val("profile", DEFAULT_PROFILE),
                             options=[ft.dropdown.Option(k) for k in trakt_api.PROFILES])
    client_id = ft.TextField(label="Trakt Client ID",
                             value=val("client_id", trakt_api.PROFILES[DEFAULT_PROFILE]["client_id"]))
    redirect = ft.TextField(label="Redirect URI", value=val("redirect", trakt_api.PROFILES[DEFAULT_PROFILE]["redirect"]))
    secret = ft.TextField(label="Client secret (OOB profile only, optional)", value=val("secret"),
                          password=True, can_reveal_password=True)
    auth_code = ft.TextField(label="Authorization code")
    token_lbl = ft.Text("Token: " + ("saved" if val("token") else "none (public profile reads only)"), size=12)
    tmdb_key = ft.TextField(label="TMDB API key (v3 key or v4 token)", value=val("tmdb_key"),
                            password=True, can_reveal_password=True)
    gem_key = ft.TextField(label="Gemini API key", value=val("gemini_key"), password=True, can_reveal_password=True)
    gem_model = ft.TextField(label="Gemini model", value=val("gemini_model", "gemini-2.5-flash"))
    thr_lbl = ft.Text(f"Fuzzy match threshold: {val('threshold', '90')}%")
    thr = ft.Slider(min=70, max=100, divisions=30, value=float(val("threshold", "90")), label="{value}%")

    def on_thr(e):
        thr_lbl.value = f"Fuzzy match threshold: {int(thr.value)}%"
        page.update()
    thr.on_change = on_thr

    def on_profile(e):
        p = trakt_api.PROFILES[profile_dd.value]
        client_id.value, redirect.value = p["client_id"], p["redirect"]
        page.update()
    profile_dd.on_change = on_profile

    def save_settings(e=None):
        for k, c in (("username", username), ("profile", profile_dd), ("client_id", client_id),
                     ("redirect", redirect), ("secret", secret), ("tmdb_key", tmdb_key),
                     ("gemini_key", gem_key), ("gemini_model", gem_model)):
            db.set(k, (c.value or "").strip())
        db.set("threshold", int(thr.value))
        if e is not None:
            toast("Settings saved")

    def authorize(e):
        save_settings()
        pkce = trakt_api.PROFILES.get(profile_dd.value, {}).get("pkce", False)
        verifier, challenge = trakt_api.pkce_pair()
        state["verifier"] = verifier if pkce else None
        page.launch_url(trakt_api.authorize_url(client_id.value.strip(), redirect.value.strip(),
                                                challenge if pkce else None))
        toast("Approve in the browser, then paste the code from the page/URL below.")

    def exchange(e):
        def job():
            tok = trakt_api.exchange_code(client_id.value.strip(), redirect.value.strip(), auth_code.value.strip(),
                                          state["verifier"], secret.value.strip() or None)
            db.set("token", tok["access_token"])
            token_lbl.value = "Token: saved"
            toast("Trakt authorized")
        run_bg(job)

    def clear_cache(e):
        def yes(_):
            page.close(dlg)
            db.clear_cache()
            refresh_all()
            toast("Cached data cleared (settings and manual exclusions kept)")
        dlg = ft.AlertDialog(title=ft.Text("Clear cached data?"),
                             content=ft.Text("Removes imported items, scan results and caches. Settings and manual "
                                             "exclusions are kept."),
                             actions=[ft.TextButton("Cancel", on_click=lambda _: page.close(dlg)),
                                      ft.TextButton("Clear", on_click=yes)])
        page.open(dlg)

    settings_view = ft.Column([
        username, profile_dd, client_id, redirect, secret,
        ft.Row([ft.ElevatedButton("Authorize Trakt", icon=ft.Icons.LOGIN, on_click=authorize)]),
        auth_code, ft.ElevatedButton("Submit code", on_click=exchange), token_lbl,
        ft.Divider(), tmdb_key, gem_key, gem_model, thr_lbl, thr,
        ft.Row([ft.FilledButton("Save settings", icon=ft.Icons.SAVE, on_click=save_settings),
                ft.OutlinedButton("Clear Cached Data", icon=ft.Icons.DELETE_OUTLINE, on_click=clear_cache)]),
        ft.Text("Keys are stored unencrypted in this app's private storage.", size=11),
    ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=10)

    # ---------- ingest tab ----------
    c_watched, c_excl, c_movies, c_series, c_games = [ft.Text("0", size=20, weight=ft.FontWeight.BOLD) for _ in range(5)]

    def counter_card(label, ctl):
        return ft.Container(ft.Column([ctl, ft.Text(label, size=11)], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                      spacing=0), padding=10, border_radius=10, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                            width=104)

    def refresh_counters():
        c = db.counters()
        c_watched.value, c_excl.value = str(c["watched"]), str(c["excluded"])
        c_movies.value, c_series.value, c_games.value = str(c["movies"]), str(c["series"]), str(c["games"])

    def pick_handler(importer, label):
        def handler(e):
            if not e.files:
                return
            files = list(e.files)

            def job():
                total = 0
                for i, f in enumerate(files):
                    if not f.path:
                        raise RuntimeError(f"No readable path for {f.name}.")
                    prog(i / len(files), f"{label}: {f.name}")
                    total += importer(f.path)
                refresh_counters()
                prog(1.0, f"{label}: {total} records imported")
            run_bg(job)
        return handler

    pk_imdb = ft.FilePicker(on_result=pick_handler(lambda p: parsers.import_imdb_csv(db, p, "auto"), "IMDb watched/rated"))
    pk_excl = ft.FilePicker(on_result=pick_handler(lambda p: parsers.import_imdb_csv(db, p, "list"), "IMDb exclusion list"))
    pk_fr = ft.FilePicker(on_result=pick_handler(lambda p: parsers.import_franchise_file(db, p), "Franchise mapping"))
    pk_zip = ft.FilePicker(on_result=pick_handler(
        lambda p: parsers.import_trakt_zip(db, p, lambda f, t: prog(f, t)), "Trakt export"))
    page.overlay.extend([pk_imdb, pk_excl, pk_fr, pk_zip])

    def sync_live(e):
        save_settings()

        def job():
            trakt_api.sync(db, client_id.value.strip(), username.value.strip(), val("token") or None, prog)
            refresh_counters()
        run_bg(job)

    ingest_view = ft.Column([
        ft.ElevatedButton("IMDb Watched/Rated CSV", icon=ft.Icons.UPLOAD_FILE, on_click=lambda e: pk_imdb.pick_files(
            allow_multiple=True, file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=["csv"])),
        ft.ElevatedButton("IMDb Exclusion CSVs (lists/watchlist)", icon=ft.Icons.UPLOAD_FILE,
                          on_click=lambda e: pk_excl.pick_files(allow_multiple=True, file_type=ft.FilePickerFileType.CUSTOM,
                                                                allowed_extensions=["csv"])),
        ft.ElevatedButton("Franchise mapping (.csv / .txt)", icon=ft.Icons.UPLOAD_FILE,
                          on_click=lambda e: pk_fr.pick_files(allow_multiple=True, file_type=ft.FilePickerFileType.CUSTOM,
                                                              allowed_extensions=["csv", "txt"])),
        ft.ElevatedButton("Trakt export ZIP", icon=ft.Icons.FOLDER_ZIP,
                          on_click=lambda e: pk_zip.pick_files(file_type=ft.FilePickerFileType.CUSTOM,
                                                               allowed_extensions=["zip"])),
        ft.FilledButton("Sync Live Trakt", icon=ft.Icons.SYNC, on_click=sync_live),
        bar, status,
        ft.Row([counter_card("Watched", c_watched), counter_card("Excluded", c_excl), counter_card("Movies", c_movies)],
               wrap=True),
        ft.Row([counter_card("Series", c_series), counter_card("Games", c_games)], wrap=True),
    ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=10)

    # ---------- missing canon tab ----------
    search = ft.TextField(label="Search", dense=True, expand=True, on_change=lambda e: render_missing())
    medium_dd = ft.Dropdown(value="All", width=130, dense=True,
                            options=[ft.dropdown.Option(m) for m in ("All", "Movie", "TV Series", "Special", "Video Game")],
                            on_change=lambda e: render_missing())
    missing_hdr = ft.Text("", size=12)
    missing_list = ft.ListView(expand=True, spacing=2)

    def exclude(c):
        db.add_exclusion(c["title"], c["year"], c["medium"], c["imdb"])
        render_missing()
        refresh_counters()
        page.update()

    def render_missing():
        rows = engine.missing_canon(db, threshold())
        q, mf = (search.value or "").lower(), medium_dd.value
        groups = {}
        for r in rows:
            if mf != "All" and r["medium"] != mf:
                continue
            if q and q not in r["title"].lower() and q not in r["franchise"].lower():
                continue
            groups.setdefault(r["franchise"], []).append(r)
        ctrls = []
        for f, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0].lower())):
            tiles = [ft.ListTile(
                title=ft.Text(f"{c['title']}" + (f" ({c['year']})" if c["year"] else "")),
                subtitle=ft.Row([badge(c["medium"]), ft.Text(c["origin"], size=11)]),
                trailing=ft.IconButton(ft.Icons.ADD_TASK, tooltip="Add to exclusions", on_click=lambda e, c=c: exclude(c)))
                for c in sorted(items, key=lambda c: (c["year"] or 9999, c["title"]))]
            ctrls.append(ft.ExpansionTile(title=ft.Text(f), subtitle=ft.Text(f"{len(items)} missing"), controls=tiles))
        missing_list.controls = ctrls
        missing_hdr.value = f"{sum(len(v) for v in groups.values())} missing across {len(groups)} franchises"

    def scan_tmdb(e):
        save_settings()
        def job():
            tmdb_api.collection_scan(db, tmdb_key.value, prog)
            render_missing()
        run_bg(job)

    def scan_gemini(e):
        save_settings()
        def job():
            gemini_api.multiverse_scan(db, gem_key.value, gem_model.value.strip() or "gemini-2.5-flash", prog)
            render_missing()
        run_bg(job)

    missing_view = ft.Column([
        ft.Row([ft.ElevatedButton("TMDB Collection Scan", icon=ft.Icons.MOVIE_FILTER, on_click=scan_tmdb),
                ft.ElevatedButton("Gemini Multiverse Scan", icon=ft.Icons.AUTO_AWESOME, on_click=scan_gemini)], wrap=True),
        ft.Row([search, medium_dd]), missing_hdr, missing_list,
    ], expand=True, spacing=8)

    # ---------- discrepancies tab ----------
    VIEWS = ["In IMDb, missing on Trakt", "In Trakt, missing on IMDb", "Video Games Ledger (IMDb-only)",
             "Season 0 / TMDB Unlinked Specials"]
    disc_dd = ft.Dropdown(value=VIEWS[0], options=[ft.dropdown.Option(v) for v in VIEWS],
                          on_change=lambda e: render_disc())
    disc_info = ft.Text("", size=12)
    disc_list = ft.ListView(expand=True, spacing=2)
    all_sw = ft.Switch(label="Check all IMDb-only items, not just specials (slower)", value=False)

    def item_tile(i):
        return ft.ListTile(title=ft.Text(f"{i['title']}" + (f" ({i['year']})" if i["year"] else "")),
                           subtitle=ft.Row([badge(i["medium"]), ft.Text(i["imdb"] or "no IMDb ID", size=11)]))

    def render_disc():
        d = engine.discrepancies(db)
        v = disc_dd.value
        disc_info.value = d["warning"] if v in VIEWS[:2] else ""
        if v == VIEWS[0]:
            rows = d["imdb_not_trakt"]
        elif v == VIEWS[1]:
            rows = d["trakt_not_imdb"]
        elif v == VIEWS[2]:
            rows = d["games"]
        else:
            sp = db.specials()
            disc_list.controls = [ft.ListTile(title=ft.Text(s["title"] or s["imdb"]),
                                              subtitle=ft.Text(f"{s['status']}: {s['note']}", size=11)) for s in sp]
            disc_info.value = f"{len(sp)} checked specials" if sp else "Run the Season 0 scan (needs a TMDB key)."
            return
        rows = sorted(rows, key=lambda i: (i["title"] or "").lower())
        disc_list.controls = [item_tile(i) for i in rows]
        if not disc_info.value:
            disc_info.value = f"{len(rows)} items"

    def scan_s0(e):
        save_settings()
        def job():
            tmdb_api.season0_scan(db, tmdb_key.value, threshold(), prog, all_sw.value)
            disc_dd.value = VIEWS[3]
            render_disc()
        run_bg(job)

    disc_view = ft.Column([
        disc_dd, ft.ElevatedButton("Run Season 0 scan", icon=ft.Icons.SEARCH, on_click=scan_s0), all_sw,
        disc_info, disc_list,
    ], expand=True, spacing=8)

    # ---------- navigation ----------
    views = [ingest_view, missing_view, disc_view, settings_view]
    body = ft.Container(content=ingest_view, expand=True, padding=12)

    def refresh_all():
        refresh_counters()
        render_missing()
        render_disc()
        page.update()

    def nav_change(e):
        i = e.control.selected_index
        body.content = views[i]
        if i == 1:
            render_missing()
        elif i == 2:
            render_disc()
        elif i == 0:
            refresh_counters()
        page.update()

    page.navigation_bar = ft.NavigationBar(selected_index=0, on_change=nav_change, destinations=[
        ft.NavigationBarDestination(icon=ft.Icons.SYNC, label="Ingest"),
        ft.NavigationBarDestination(icon=ft.Icons.TRAVEL_EXPLORE, label="Missing"),
        ft.NavigationBarDestination(icon=ft.Icons.COMPARE_ARROWS, label="Discrepancies"),
        ft.NavigationBarDestination(icon=ft.Icons.SETTINGS, label="Settings"),
    ])
    page.add(ft.SafeArea(body, expand=True))
    refresh_counters()
    page.update()


if __name__ == "__main__":
    ft.app(target=main)
