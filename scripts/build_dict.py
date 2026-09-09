#!/usr/bin/env python3
"""Build sharded offline dictionary (Webster's Unabridged 1913, public domain)
for the local Joyce mirror's word-lookup popup.

Input:  dictionary.json from matthewreagan/WebstersEnglishDictionary
        ({word: definition}, ~102k entries, ~23MB)
Output: data/dict/<shard>.json where shard is the word's first letter
        (a-z) or "0" for anything else. The app fetches one small shard
        on demand instead of loading the whole dictionary.

Usage:
  python3 scripts/build_dict.py [--src PATH_OR_URL] [--base DIR]
"""
import argparse
import json
import os
import sys
import urllib.request

DEFAULT_SRC = ("https://raw.githubusercontent.com/matthewreagan/"
               "WebstersEnglishDictionary/master/dictionary.json")


def load(src):
    if src.startswith("http"):
        print(f"downloading {src} ...", flush=True)
        req = urllib.request.Request(
            src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    with open(src) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--base", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".."))
    args = ap.parse_args()
    data = load(args.src)
    print(f"entries: {len(data)}")
    shards = {}
    for word, definition in data.items():
        w = (word or "").strip().lower()
        if not w or not isinstance(definition, str):
            continue
        ch = w[0]
        shard = ch if "a" <= ch <= "z" else "0"
        shards.setdefault(shard, {})[w] = definition.strip()
    outdir = os.path.join(os.path.abspath(args.base), "data", "dict")
    os.makedirs(outdir, exist_ok=True)
    manifest = {}
    for shard in sorted(shards):
        path = os.path.join(outdir, shard + ".json")
        with open(path, "w") as f:
            json.dump(shards[shard], f, separators=(",", ":"))
        manifest[shard] = len(shards[shard])
        print(f"  {shard}.json: {len(shards[shard])} words, "
              f"{os.path.getsize(path)//1024} KB")
    with open(os.path.join(outdir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
