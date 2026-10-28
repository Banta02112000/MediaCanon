import json
import re
import time

import requests

import engine
from util import MEDIA, medium_from_text, norm

PROMPT = """You are a transmedia canon expert. Below are titles a user has consumed, as "Title (Year) [Medium]".
Group them into overarching connected universes / franchises (for example The Matrix, Star Wars, The Walking Dead,
Doctor Who / Whoniverse, Arrowverse). For EVERY universe represented, return ALL of its canonical entries:
Movies, TV Series, Webisodes/Specials, and Video Games, including entries the user did not list.
Use official titles as they appear on IMDb. Respond ONLY with a JSON array, no commentary, in this exact shape:
[{"franchise": "...", "title": "...", "medium": "Movie|TV Series|Special|Video Game", "year": "YYYY"}]

TITLES:
"""


def parse_json_array(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        data = json.loads(text)
    except ValueError:
        cut = text.rfind("},")
        if cut == -1:
            raise RuntimeError("Gemini returned unparseable JSON.")
        data = json.loads(text[:cut + 1] + "]")
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return [d for d in data if isinstance(d, dict)]


def _call(key, model, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                 "maxOutputTokens": 32000}}
    for attempt in range(4):
        r = requests.post(url, params={"key": key}, json=body, timeout=240)
        if r.status_code in (429, 503):
            time.sleep(8 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:200]}")
        try:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise RuntimeError("Gemini returned no content (blocked or empty).")
    raise RuntimeError("Gemini rate limit: try again later.")


def multiverse_scan(db, key, model, progress, batch_size=120, max_batches=10):
    if not key.strip():
        raise RuntimeError("Enter a Gemini API key in Settings first.")
    hints = db.franchise_hints()
    pool = [i for i in engine.consumed(db) if i["medium"] in MEDIA and i["title"]]
    pool.sort(key=lambda i: (-(i["rating"] or 0), i["title"]))
    batches = [pool[i:i + batch_size] for i in range(0, len(pool), batch_size)][:max_batches]
    if not batches:
        raise RuntimeError("No consumed titles yet. Import IMDb/Trakt data first.")
    merged, seen = [], set()
    for b, chunk in enumerate(batches):
        progress(b / len(batches), f"Gemini batch {b + 1}/{len(batches)}")
        lines = []
        for i in chunk:
            hint = hints.get(i["norm"])
            lines.append(f"{i['title']} ({i['year'] or '?'}) [{i['medium']}]" + (f" {{{hint}}}" if hint else ""))
        for d in parse_json_array(_call(key, model, PROMPT + "\n".join(lines))):
            title = str(d.get("title") or "").strip()
            fr = str(d.get("franchise") or "Unsorted").strip()
            if not title:
                continue
            med = medium_from_text(str(d.get("medium") or ""))
            k = (norm(fr), norm(title), str(d.get("year") or ""))
            if k in seen:
                continue
            seen.add(k)
            merged.append({"franchise": fr, "title": title, "year": d.get("year"), "medium": med})
        db.replace_canon("gemini", merged)
        if b < len(batches) - 1:
            time.sleep(3)
    progress(1.0, f"Gemini scan done: {len(merged)} canon entries")
    return len(merged)
