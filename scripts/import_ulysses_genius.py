#!/usr/bin/env python3
"""Cache and normalize Genius annotations for James Joyce's *Ulysses*.

Genius pages contain the annotated text and a list of referent IDs.  The
annotation bodies are fetched through Genius's public referents endpoint and
stored locally, so the browser never needs a Genius connection.  Both page
HTML and API responses are cache files and can be reparsed with ``--offline``.

The importer intentionally writes only under ``data/works/ulysses``.  The
legacy Joyce Project mirror under ``data/chapters`` and ``data/notes`` is a
separate input and is never modified by this command.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import json
import re
import sys
import urllib.parse
import warnings
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = BASE_DIR / "sources" / "ulysses-genius.json"
ULYSSES_DIR = BASE_DIR / "data" / "works" / "ulysses"
RAW_DIR = ULYSSES_DIR / "sources" / "genius" / "raw"
API_DIR = RAW_DIR / "api"
NORMALIZED_DIR = ULYSSES_DIR / "normalized" / "annotations"
REPORT_DIR = ULYSSES_DIR / "manifests"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_portrait_genius as genius_helpers


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def decode_preloaded_state(source: str) -> dict:
    """Decode the JSON.parse string, including Genius's ``\\$`` variant."""
    try:
        return genius_helpers.decode_preloaded_state(source)
    except json.JSONDecodeError:
        match = re.search(
            r"window\.__PRELOADED_STATE__\s*=\s*JSON\.parse\('(.*?)'\);",
            source,
            flags=re.DOTALL,
        )
        if not match:
            raise
        payload = match.group(1).replace("\\/", "/")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            decoded = ast.literal_eval("'" + payload + "'")
        # A few Ulysses referents contain a dollar sign escaped for the
        # JavaScript string but not for JSON. Remove only backslashes that
        # cannot begin a JSON escape, preserving valid \n/\u sequences.
        decoded = re.sub(
            r"\\(?![\"\\/bfnrtu]|u[0-9a-fA-F]{4})", "", decoded
        )
        return json.loads(decoded)


def page_info(source: str, expected_chapter: int) -> dict:
    """Read Genius's structured state, with a small server-DOM fallback."""
    try:
        state = decode_preloaded_state(source)
        song_page = state.get("songPage") or {}
        title = str(song_page.get("title") or "")
        match = re.search(r"Chap(?:ter)?\.?\s*(\d+)\b", title, re.IGNORECASE)
        if not match or int(match.group(1)) != expected_chapter:
            raise ValueError(
                f"Genius page title does not identify Chapter {expected_chapter}: "
                f"{title!r}"
            )
        lyrics = song_page.get("lyricsData") or {}
        body = lyrics.get("body") or {}
        plain, anchors = genius_helpers.page_anchor_map(body)
        ids = [int(value) for value in lyrics.get("referents") or []]
        if not ids:
            raise ValueError(f"Genius Chapter {expected_chapter} contains no referent IDs")
        return {
            "state": state,
            "song_id": song_page.get("song"),
            "title": title,
            "plain": plain,
            "anchors": anchors,
            "referent_ids": list(dict.fromkeys(ids)),
        }
    except (ValueError, KeyError, TypeError, SyntaxError, json.JSONDecodeError):
        return dom_fallback_page_info(source, expected_chapter)


def dom_fallback_page_info(source: str, expected_chapter: int) -> dict:
    """Recover anchor fragments if Genius changes its preloaded state markup."""
    title_match = re.search(
        r"<(?:title|h1)\b[^>]*>(.*?)</(?:title|h1)>",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    title = (
        html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
        if title_match
        else ""
    )
    chapter_match = re.search(
        r"Chap(?:ter)?\.?\s*(\d+)\b", title or source, flags=re.IGNORECASE
    )
    if not chapter_match or int(chapter_match.group(1)) != expected_chapter:
        raise ValueError(
            f"Genius DOM fallback does not identify Chapter {expected_chapter}: "
            f"{title!r}"
        )

    pieces: list[str] = []
    anchors: dict[str, dict[str, str]] = {}
    anchor_pattern = re.compile(
        r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in anchor_pattern.finditer(source):
        attrs = match.group("attrs")
        id_match = re.search(
            r"\bdata-(?:id|referent-id)\s*=\s*['\"](\d+)['\"]",
            attrs,
            flags=re.IGNORECASE,
        )
        if not id_match:
            continue
        text = re.sub(r"<br\s*/?>", "\n", match.group("body"), flags=re.IGNORECASE)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if pieces:
            pieces.append("\n\n")
        start = len("".join(pieces))
        pieces.append(text)
        end = start + len(text)
        plain = "".join(pieces)
        key = id_match.group(1)
        anchors[key] = {
            "text": text,
            "prefix": plain[max(0, start - 180) : start],
            "suffix": plain[end : end + 180],
            "href": "",
        }
    plain = "".join(pieces)
    if not anchors:
        raise ValueError("Genius DOM fallback found no annotation referents")
    return {
        "state": {},
        "song_id": None,
        "title": title,
        "plain": plain,
        "anchors": anchors,
        "referent_ids": [int(value) for value in anchors],
    }


def fetch_page(chapter: int, url: str, *, allow_network: bool) -> tuple[Path, dict]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"chapter-{chapter}.html"
    if not path.exists():
        if not allow_network:
            raise FileNotFoundError(f"Offline Genius page cache missing: {path}")
        print(f"Genius: fetching Chapter {chapter} page", flush=True)
        path.write_bytes(genius_helpers.request_bytes(url, "text/html,*/*;q=0.1"))
    source = path.read_text(encoding="utf-8", errors="replace")
    return path, page_info(source, chapter)


def api_cache_files(chapter: int) -> list[Path]:
    return sorted(API_DIR.glob(f"chapter-{chapter}-batch-*.json"))


def api_referents(payload: object) -> dict[str, dict]:
    part = ((payload or {}).get("response") or {}).get("referents") or {}
    if not isinstance(part, dict):
        return {}
    return {str(key): value for key, value in part.items() if isinstance(value, dict)}


def read_api_cache(path: Path) -> dict[str, dict]:
    return api_referents(load_json(path))


def load_api_referents(
    chapter: int, ids: list[int], *, allow_network: bool
) -> dict[str, dict]:
    """Load cached referents and fetch missing IDs in bounded batches."""
    API_DIR.mkdir(parents=True, exist_ok=True)
    referents: dict[str, dict] = {}
    for path in api_cache_files(chapter):
        try:
            referents.update(read_api_cache(path))
        except Exception as exc:
            print(
                f"warning: ignoring malformed Genius API cache {path}: {exc}",
                file=sys.stderr,
            )

    missing = [value for value in ids if str(value) not in referents]
    if missing and not allow_network:
        raise FileNotFoundError(
            f"Offline Genius API cache is missing {len(missing)} referents "
            f"for Chapter {chapter}"
        )
    for batch_number in range(0, len(missing), 80):
        batch = missing[batch_number : batch_number + 80]
        index = batch_number // 80 + 1
        path = API_DIR / f"chapter-{chapter}-batch-{index:03d}.json"
        cached: dict[str, dict] = {}
        if path.exists():
            try:
                cached = read_api_cache(path)
            except Exception as exc:
                print(
                    f"warning: ignoring malformed Genius API cache {path}: {exc}",
                    file=sys.stderr,
                )
        requested = {str(value) for value in batch}
        if requested.issubset(cached):
            referents.update(cached)
            continue

        # A positional batch file may belong to an earlier page revision.
        # Keep it intact and use a deterministic refresh name keyed by the
        # requested IDs, so a rerun reuses the corrected response.
        refresh_key = hashlib.sha256(
            ",".join(str(value) for value in batch).encode("ascii")
        ).hexdigest()[:12]
        refresh_path = API_DIR / (
            f"chapter-{chapter}-batch-{index:03d}-refresh-{refresh_key}.json"
        )
        if refresh_path.exists():
            try:
                refreshed = read_api_cache(refresh_path)
            except Exception as exc:
                print(
                    f"warning: ignoring malformed Genius API cache {refresh_path}: {exc}",
                    file=sys.stderr,
                )
                refreshed = {}
            if requested.issubset(refreshed):
                referents.update(refreshed)
                continue

        if not allow_network:
            missing_batch = sorted(requested - set(cached))
            raise FileNotFoundError(
                f"Offline Genius API cache {path} is missing "
                f"{len(missing_batch)} requested referents for Chapter {chapter}"
            )
        else:
            query = urllib.parse.urlencode({"ids": ",".join(map(str, batch))})
            url = "https://genius.com/api/referents/multi?" + query
            print(
                f"Genius: fetching Chapter {chapter} referents "
                f"{batch_number + 1}\N{EN DASH}{batch_number + len(batch)}",
                flush=True,
            )
            raw = genius_helpers.request_bytes(url, "application/json,*/*;q=0.1")
            refresh_path.write_bytes(raw)
            payload = json.loads(raw.decode("utf-8"))
            fetched = api_referents(payload)
            referents.update(fetched)

    unresolved = [value for value in ids if str(value) not in referents]
    if unresolved:
        preview = ", ".join(str(value) for value in unresolved[:10])
        raise RuntimeError(
            f"Genius API did not return {len(unresolved)} requested referents "
            f"for Chapter {chapter}: {preview}"
        )
    return referents


def normalize_notes(
    chapter: int,
    chapter_id: str,
    page_url: str,
    info: dict,
    refs: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    notes: list[dict] = []
    failed: list[dict] = []
    for referent_id in info["referent_ids"]:
        key = str(referent_id)
        referent = refs.get(key)
        page_anchor = info["anchors"].get(key, {})
        if not referent:
            failed.append(
                {
                    "chapter": chapter,
                    "referent_id": referent_id,
                    "reason": "missing API referent",
                }
            )
            continue
        fragment = str(
            referent.get("fragment") or page_anchor.get("text") or ""
        ).strip()
        annotations = referent.get("annotations") or []
        if not annotations:
            failed.append(
                {
                    "chapter": chapter,
                    "referent_id": referent_id,
                    "referent": fragment,
                    "reason": "no annotation body",
                }
            )
            continue
        for annotation_index, annotation in enumerate(annotations, start=1):
            if not isinstance(annotation, dict) or annotation.get("deleted"):
                failed.append(
                    {
                        "chapter": chapter,
                        "referent_id": referent_id,
                        "referent": fragment,
                        "reason": "deleted or malformed annotation",
                    }
                )
                continue
            body = genius_helpers.annotation_body(annotation)
            if not body:
                failed.append(
                    {
                        "chapter": chapter,
                        "referent_id": referent_id,
                        "referent": fragment,
                        "reason": "empty annotation body",
                        "annotation_id": annotation.get("id"),
                    }
                )
                continue
            original_id = str(
                annotation.get("id") or f"{referent_id}-{annotation_index}"
            )
            note_id = f"ulysses-genius-{original_id}"
            anchor_text = str(
                (referent.get("range") or {}).get("content") or fragment
            ).strip()
            notes.append(
                {
                    "id": note_id,
                    "work_id": "ulysses",
                    "chapter_id": chapter_id,
                    "title": fragment or anchor_text or "Genius annotation",
                    "html_source": body,
                    "media_doc_ids": [],
                    "source": {
                        "type": "genius",
                        "url": annotation.get("url")
                        or referent.get("url")
                        or page_url,
                        "original_id": original_id,
                        "referent_id": referent_id,
                        "page_url": page_url,
                    },
                    "anchor": {
                        "text": anchor_text,
                        "prefix": str(page_anchor.get("prefix") or ""),
                        "suffix": str(page_anchor.get("suffix") or ""),
                        "start": None,
                        "end": None,
                    },
                    "provenance": {
                        "match_method": "unresolved",
                        "confidence": 0.0,
                    },
                    "genius": {
                        "referent_id": referent_id,
                        "annotation_state": annotation.get("state"),
                        "classification": referent.get("classification"),
                        "contributors": genius_helpers.author_metadata(annotation),
                    },
                    "color": "F59627",
                }
            )
    return notes, failed


def import_genius(*, fetch: bool = True, base_dir: Path | None = None) -> list[dict]:
    global MANIFEST_PATH, ULYSSES_DIR, RAW_DIR, API_DIR, NORMALIZED_DIR, REPORT_DIR
    if base_dir is not None:
        MANIFEST_PATH = base_dir / "sources" / "ulysses-genius.json"
        ULYSSES_DIR = base_dir / "data" / "works" / "ulysses"
        RAW_DIR = ULYSSES_DIR / "sources" / "genius" / "raw"
        API_DIR = RAW_DIR / "api"
        NORMALIZED_DIR = ULYSSES_DIR / "normalized" / "annotations"
        REPORT_DIR = ULYSSES_DIR / "manifests"

    manifest = load_json(MANIFEST_PATH)
    all_notes: list[dict] = []
    all_failed: list[dict] = []
    page_summaries: list[dict] = []
    for entry in manifest.get("chapters") or []:
        chapter = int(entry["chapter"])
        chapter_id = str(entry["chapter_id"])
        url = str(entry["url"])
        _, info = fetch_page(chapter, url, allow_network=fetch)
        refs = load_api_referents(
            chapter, info["referent_ids"], allow_network=fetch
        )
        notes, failed = normalize_notes(chapter, chapter_id, url, info, refs)
        all_notes.extend(notes)
        all_failed.extend(failed)
        page_summaries.append(
            {
                "chapter": chapter,
                "chapter_id": chapter_id,
                "title": entry.get("title"),
                "url": url,
                "song_id": info.get("song_id"),
                "referents": len(info["referent_ids"]),
                "notes": len(notes),
                "failed_records": len(failed),
            }
        )

    ids = [note["id"] for note in all_notes]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Genius importer produced duplicate normalized note IDs")
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (NORMALIZED_DIR / "genius_notes.json").write_text(
        json.dumps(
            {
                "work": "ulysses",
                "source": {
                    "type": "genius",
                    "manifest": "sources/ulysses-genius.json",
                },
                "notes": all_notes,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (REPORT_DIR / "genius-failed.json").write_text(
        json.dumps(
            {"failed": all_failed, "chapters": page_summaries},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"Genius: normalized {len(all_notes)} notes; "
        f"{len(all_failed)} acquisition records need inspection",
        flush=True,
    )
    return all_notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch missing page/API caches (the default)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Reparse only cached raw HTML/API responses",
    )
    args = parser.parse_args(argv)
    if args.fetch and args.offline:
        parser.error("--fetch and --offline are mutually exclusive")
    try:
        import_genius(fetch=not args.offline)
    except Exception as exc:  # pragma: no cover - command-line diagnostics
        print(f"Ulysses Genius import failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
