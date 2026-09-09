#!/usr/bin/env python3
"""Mirror The Joyce Project (joyceproject.com) content to local data/ files.

Fetches via the site's public JSON API (same endpoints the site's own
frontend uses):
  GET /api/chapters/          -> [{id, number, title}]
  GET /api/chapters/<id>      -> {id, number, title, html_source, ...}
  GET /api/notes/             -> [{id, title, media_doc_ids}]
  GET /api/notes/<id>         -> {id, title, html_source, media_doc_ids, ...}
  GET /api/media/             -> [{id, title, type, file_ext, ...}]
  GET /api/media/<id>         -> {id, title, type, html_source(caption), ...}
  GET /api/info/              -> info pages list (+ /api/info/<id>)

Images live at https://joyceproject.com/static/img/<media-id>/img.<ext>
(thumbs at .../thumb.<ext>). Downloaded by --images flag; the web app
falls back to the remote URL when a local file is missing, so images
are optional for a working local copy.

Usage:
  python3 scripts/rip.py [--base DIR] [--images] [--workers N]

Personal-use local mirror only. Text of Ulysses itself is public domain;
annotations/commentary are the work of the Joyce Project contributors.
Keep attribution in the UI and do not redistribute.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE_URL = "https://joyceproject.com"
UA = {"User-Agent": "Mozilla/5.0 (local-mirror; personal-use)"}


def get_json(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            wait = 2 ** attempt
            print(f"  retry {attempt+1}/{retries} {url}: {e} (sleep {wait}s)",
                  flush=True)
            time.sleep(wait)
    raise RuntimeError(f"FAILED: {url}")


def get_binary(url, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                ctype = r.headers.get("Content-Type", "")
                if "text/html" in ctype:  # wrong ext -> 404 page
                    return None
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 ** attempt)
        except Exception as e:
            print(f"  img retry {attempt+1}/{retries} {url}: {e}", flush=True)
            time.sleep(2 ** attempt)
    return None


def fetch_many(pairs, outdir, workers, label):
    """pairs: list of (doc_id, url). Skips ids already on disk (resume)."""
    os.makedirs(outdir, exist_ok=True)
    todo = [(i, u) for i, u in pairs
            if not os.path.exists(os.path.join(outdir, i + ".json"))]
    print(f"{label}: {len(pairs)} total, {len(todo)} to fetch")
    done = [0]

    def one(pair):
        doc_id, url = pair
        try:
            data = get_json(url)
            with open(os.path.join(outdir, doc_id + ".json"), "w") as f:
                json.dump(data, f)
        except Exception as e:
            print(f"ERROR {doc_id}: {e}", flush=True)
        done[0] += 1
        if done[0] % 100 == 0 or done[0] == len(todo):
            print(f"  {label}: {done[0]}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, todo))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".."))
    ap.add_argument("--images", action="store_true",
                    help="also download media image binaries")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()
    base = os.path.abspath(args.base)
    data = os.path.join(base, "data")
    os.makedirs(os.path.join(data, "chapters"), exist_ok=True)
    os.makedirs(os.path.join(data, "notes"), exist_ok=True)
    os.makedirs(os.path.join(data, "media"), exist_ok=True)
    os.makedirs(os.path.join(data, "info"), exist_ok=True)

    print("Fetching lists...")
    chapters = get_json(f"{BASE_URL}/api/chapters/")
    with open(os.path.join(data, "chapters.json"), "w") as f:
        json.dump(chapters, f)
    notes = get_json(f"{BASE_URL}/api/notes/")
    with open(os.path.join(data, "notes.json"), "w") as f:
        json.dump(notes, f)
    media = get_json(f"{BASE_URL}/api/media/")
    with open(os.path.join(data, "media.json"), "w") as f:
        json.dump(media, f)
    try:
        info = get_json(f"{BASE_URL}/api/info/")
        with open(os.path.join(data, "info.json"), "w") as f:
            json.dump(info, f)
    except Exception as e:
        print(f"info list failed (non-fatal): {e}")
        info = []
    print(f"chapters={len(chapters)} notes={len(notes)} "
          f"media={len(media)} info={len(info)}")

    fetch_many([(c["id"], f"{BASE_URL}/api/chapters/{c['id']}")
                for c in chapters],
               os.path.join(data, "chapters"), args.workers, "chapters")
    fetch_many([(n["id"], f"{BASE_URL}/api/notes/{n['id']}")
                for n in notes],
               os.path.join(data, "notes"), args.workers, "notes")
    fetch_many([(m["id"], f"{BASE_URL}/api/media/{m['id']}")
                for m in media],
               os.path.join(data, "media"), args.workers, "media")
    if info:
        fetch_many([(i["id"], f"{BASE_URL}/api/info/{i['id']}")
                    for i in info if isinstance(i, dict) and "id" in i],
                   os.path.join(data, "info"), args.workers, "info")

    if args.images:
        img_base = os.path.join(base, "static", "img")
        os.makedirs(img_base, exist_ok=True)
        # need file_ext per item: prefer detail file, else list
        details = {}
        for m in media:
            details[m["id"]] = m.get("file_ext")
        print(f"images: {len(media)} items")
        done = [0]

        def one(m):
            mid = m["id"]
            if m.get("type") == "yt":
                done[0] += 1
                return
            ext = details.get(mid) or m.get("file_ext")
            if not ext:
                # read detail json
                try:
                    d = json.load(open(
                        os.path.join(data, "media", mid + ".json")))
                    ext = d.get("file_ext")
                except Exception:
                    pass
            if not ext:
                done[0] += 1
                return
            dest = os.path.join(img_base, mid, f"img.{ext}")
            if not os.path.exists(dest):
                blob = get_binary(f"{BASE_URL}/static/img/{mid}/img.{ext}")
                if blob:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(blob)
            done[0] += 1
            if done[0] % 200 == 0 or done[0] == len(media):
                print(f"  images: {done[0]}/{len(media)}", flush=True)

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(one, media))

    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
