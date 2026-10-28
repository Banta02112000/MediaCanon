import base64
import hashlib
import json
import os
import time

import requests

import parsers

BASE = "https://api.trakt.tv"
UA = "CanonSync/1.0"

PROFILES = {
    "PKCE / Public Client (App 232427)": {
        "client_id": "kbyV7Ulf_br4lCQghVHTFifDuwGf5S23CIks-tGTFM0",
        "redirect": "https://trakt.tv", "pkce": True},
    "OAuth OOB (App 232151)": {
        "client_id": "XyELwkpc071SpSzuIQC9027o8OIr6RJmKgMuegUiDT8",
        "redirect": "urn:ietf:wg:oauth:2.0:oob", "pkce": False},
}


def pkce_pair():
    verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id, redirect, challenge=None):
    from urllib.parse import urlencode
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect}
    if challenge:
        q.update({"code_challenge": challenge, "code_challenge_method": "S256"})
    return "https://trakt.tv/oauth/authorize?" + urlencode(q)


def exchange_code(client_id, redirect, code, verifier=None, secret=None):
    body = {"code": code, "client_id": client_id, "redirect_uri": redirect, "grant_type": "authorization_code"}
    if verifier:
        body["code_verifier"] = verifier
    if secret:
        body["client_secret"] = secret
    r = requests.post(BASE + "/oauth/token", json=body, headers={"User-Agent": UA}, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({r.status_code}): {r.text[:200]}")
    return r.json()


class Trakt:
    def __init__(self, client_id, username, token=None):
        self.client_id, self.user, self.token = client_id.strip(), username.strip(), token

    def get(self, path, **params):
        headers = {"Content-Type": "application/json", "trakt-api-version": "2",
                   "trakt-api-key": self.client_id, "User-Agent": UA}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        out, page = [], 1
        while True:
            for _ in range(4):
                r = requests.get(BASE + path, params=dict(params, page=page, limit=1000), headers=headers, timeout=40)
                if r.status_code == 429:
                    time.sleep(float(r.headers.get("Retry-After", "3")) + 1)
                    continue
                break
            if r.status_code in (401, 403):
                raise RuntimeError("Trakt denied access: profile is private or the Client ID is wrong "
                                   "(authorize in Settings for private data).")
            if r.status_code == 404:
                raise RuntimeError(f"Trakt user or list not found: {path}")
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return data
            out.extend(data)
            if page >= int(r.headers.get("X-Pagination-Page-Count", "1")):
                return out
            page += 1


def sync(db, client_id, username, token, progress):
    t = Trakt(client_id, username, token)
    u = f"/users/{username}"
    steps = [("Watched movies", f"{u}/watched/movies", "watched", ""),
             ("Watched shows", f"{u}/watched/shows", "watched", ""),
             ("Ratings", f"{u}/ratings", "rated", ""),
             ("Watchlist", f"{u}/watchlist", "watchlist", "")]
    total = 0
    n_steps = len(steps) + 1
    for i, (label, path, kind, ln) in enumerate(steps):
        progress(i / n_steps, f"Trakt: {label}")
        total += parsers.store_trakt(db, t.get(path), kind, ln)
    progress(len(steps) / n_steps, "Trakt: custom lists")
    lists = t.get(f"{u}/lists")
    for j, lst in enumerate(lists):
        lid = (lst.get("ids") or {}).get("trakt")
        name = (lst.get("name") or str(lid)).lower().replace(" ", "-")
        progress((len(steps) + j / max(len(lists), 1)) / n_steps, f"Trakt list: {lst.get('name')}")
        total += parsers.store_trakt(db, t.get(f"{u}/lists/{lid}/items"), "list", name)
    progress(1.0, f"Trakt sync finished: {total} records")
    return total
