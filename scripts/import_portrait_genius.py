#!/usr/bin/env python3
"""Cache and normalize Genius annotations for Joyce's Portrait.

Genius pages contain the annotated text and a structured page state, but the
annotation bodies are loaded through the public referents API. This is an
explicit build-time importer: the browser has no Genius dependency. Raw page
HTML and raw API responses remain cached so parsing can be repaired offline.
"""

from __future__ import annotations

import argparse
import ast
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import warnings
from html.parser import HTMLParser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = BASE_DIR / "sources" / "portrait-genius.json"
PORTRAIT_DIR = BASE_DIR / "data" / "works" / "portrait"
RAW_DIR = PORTRAIT_DIR / "sources" / "genius" / "raw"
API_DIR = RAW_DIR / "api"
NORMALIZED_DIR = PORTRAIT_DIR / "normalized" / "annotations"
REPORT_DIR = PORTRAIT_DIR / "manifests"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/131 Safari/537.36 Joyce-reader-build/1.0"
)


def request_bytes(url: str, accept: str = "*/*") -> bytes:
    """Fetch a URL, retrying the cache-friendly Genius mobile host on 403."""
    candidates = [url]
    if "genius.com/" in url:
        candidates.extend(
            [
                url.replace("https://genius.com/", "https://m.genius.com/"),
                url.replace("https://genius.com/", "http://genius.com/"),
            ]
        )
    seen: set[str] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            request = urllib.request.Request(
                candidate,
                headers={"User-Agent": USER_AGENT, "Accept": accept},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def decode_preloaded_state(source: str) -> dict:
    match = re.search(
        r"window\.__PRELOADED_STATE__\s*=\s*JSON\.parse\('(.*?)'\);",
        source,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError("Genius page has no __PRELOADED_STATE__ JSON")
    payload = match.group(1).replace("\\/", "/")
    # The value is a JavaScript single-quoted string containing JSON. Genius
    # currently emits escapes accepted by Python's literal parser.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        decoded = ast.literal_eval("'" + payload + "'")
    return json.loads(decoded)


def page_anchor_map(node: object) -> tuple[str, dict[str, dict[str, str]]]:
    """Flatten Genius's structured lyric tree and record anchor contexts."""
    pieces: list[str] = []
    found: list[dict[str, object]] = []
    block_tags = {"p", "div", "li", "blockquote"}

    def current_length() -> int:
        return sum(len(part) for part in pieces)

    def visit(value: object) -> None:
        if isinstance(value, str):
            pieces.append(value)
            return
        if not isinstance(value, dict):
            return
        tag = str(value.get("tag") or "").lower()
        start = current_length()
        if tag == "br":
            pieces.append("\n")
        else:
            for child in value.get("children") or []:
                visit(child)
            if tag in block_tags:
                pieces.append("\n\n")
        end = current_length()
        data = value.get("data") or {}
        if tag == "a" and data.get("id") is not None:
            found.append(
                {
                    "id": str(data.get("id")),
                    "start": start,
                    "end": end,
                    "href": str((value.get("attributes") or {}).get("href") or ""),
                }
            )

    visit(node)
    plain = "".join(pieces)
    result: dict[str, dict[str, str]] = {}
    for item in found:
        start = int(item["start"])
        end = int(item["end"])
        raw_text = plain[start:end]
        text = raw_text.strip()
        if not text:
            continue
        left_trim = len(raw_text) - len(raw_text.lstrip())
        right_trim = len(raw_text.rstrip())
        actual_start = start + left_trim
        actual_end = start + right_trim
        result[str(item["id"])] = {
            "text": text,
            "prefix": plain[max(0, actual_start - 180):actual_start],
            "suffix": plain[actual_end:actual_end + 180],
            "href": str(item["href"]),
        }
    return plain, result


def dom_fallback_page_info(source: str, expected_chapter: int) -> dict:
    """Recover referent text from server-rendered anchors if state changes.

    This fallback is intentionally small and permissive.  It is only used
    when Genius omits its structured state; API bodies are still normalized by
    the same code path below.
    """
    title_match = re.search(r"<(?:title|h1)\b[^>]*>(.*?)</(?:title|h1)>", source,
                           flags=re.IGNORECASE | re.DOTALL)
    title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip() if title_match else ""
    chapter_match = re.search(r"(?:Chap(?:ter)?\.?\s*)([1-5])\b", title or source,
                              flags=re.IGNORECASE)
    if not chapter_match or int(chapter_match.group(1)) != expected_chapter:
        raise ValueError(
            f"Genius DOM fallback does not identify Chapter {expected_chapter}: {title!r}"
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
        start = len("".join(pieces))
        if pieces:
            pieces.append("\n\n")
            start += 2
        pieces.append(text)
        end = start + len(text)
        key = id_match.group(1)
        plain = "".join(pieces)
        anchors[key] = {
            "text": text,
            "prefix": plain[max(0, start - 180):start],
            "suffix": plain[end:end + 180],
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


def page_info(source: str, expected_chapter: int) -> dict:
    try:
        state = decode_preloaded_state(source)
        song_page = state.get("songPage") or {}
        title = str(song_page.get("title") or "")
        match = re.search(r"Chap(?:ter)?\.?\s*([1-5])\b", title, re.IGNORECASE)
        if not match or int(match.group(1)) != expected_chapter:
            raise ValueError(
                f"Genius page title does not identify Chapter {expected_chapter}: {title!r}"
            )
        lyrics = song_page.get("lyricsData") or {}
        body = lyrics.get("body") or {}
        plain, anchors = page_anchor_map(body)
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


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_page(chapter: int, url: str, *, allow_network: bool) -> tuple[Path, dict]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"chapter-{chapter}.html"
    if not path.exists():
        if not allow_network:
            raise FileNotFoundError(f"Offline Genius page cache missing: {path}")
        print(f"Genius: fetching Chapter {chapter} page")
        path.write_bytes(request_bytes(url, "text/html,*/*;q=0.1"))
    source = path.read_text(encoding="utf-8", errors="replace")
    return path, page_info(source, chapter)


def api_cache_files(chapter: int) -> list[Path]:
    return sorted(API_DIR.glob(f"chapter-{chapter}-batch-*.json"))


def load_api_referents(
    chapter: int, ids: list[int], *, allow_network: bool
) -> dict[str, dict]:
    API_DIR.mkdir(parents=True, exist_ok=True)
    referents: dict[str, dict] = {}
    for path in api_cache_files(chapter):
        try:
            payload = read_json(path)
            part = ((payload or {}).get("response") or {}).get("referents") or {}
            referents.update({str(key): value for key, value in part.items()})
        except Exception as exc:
            print(
                f"  warning: ignoring malformed Genius API cache {path}: {exc}",
                file=sys.stderr,
            )

    missing = [value for value in ids if str(value) not in referents]
    if missing and not allow_network:
        raise FileNotFoundError(
            f"Offline Genius API cache is missing {len(missing)} referents "
            f"for Chapter {chapter}"
        )
    for batch_number in range(0, len(missing), 80):
        batch = missing[batch_number:batch_number + 80]
        index = batch_number // 80 + 1
        path = API_DIR / f"chapter-{chapter}-batch-{index:03d}.json"
        if path.exists():
            payload = read_json(path)
        else:
            query = urllib.parse.urlencode(
                {"ids": ",".join(str(value) for value in batch)}
            )
            url = "https://genius.com/api/referents/multi?" + query
            print(
                f"Genius: fetching Chapter {chapter} referents "
                f"{batch_number + 1}–{batch_number + len(batch)}"
            )
            raw = request_bytes(url, "application/json,*/*;q=0.1")
            path.write_bytes(raw)
            payload = json.loads(raw.decode("utf-8"))
        part = ((payload or {}).get("response") or {}).get("referents") or {}
        referents.update({str(key): value for key, value in part.items()})
    return referents


def safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return value
    if not parsed.scheme and value.startswith("/"):
        return "https://genius.com" + value
    return None


ALLOWED_NOTE_TAGS = {
    "p",
    "br",
    "em",
    "i",
    "strong",
    "b",
    "u",
    "blockquote",
    "ul",
    "ol",
    "li",
    "a",
    "pre",
    "code",
    "hr",
    "img",
}


def render_dom(node: object) -> str:
    if isinstance(node, str):
        return html.escape(node, quote=False)
    if not isinstance(node, dict):
        return ""
    tag = str(node.get("tag") or "").lower()
    children = "".join(render_dom(child) for child in node.get("children") or [])
    if tag not in ALLOWED_NOTE_TAGS or tag in {"root", "span", "div"}:
        return children
    if tag == "br":
        return "<br />"
    if tag == "hr":
        return "<hr />"
    attrs = node.get("attributes") or {}
    if tag == "a":
        href = safe_url(attrs.get("href"))
        if not href:
            return children
        return (
            f'<a href="{html.escape(href, quote=True)}" '
            f'rel="noopener nofollow">{children}</a>'
        )
    if tag == "img":
        src = safe_url(attrs.get("src"))
        if not src:
            return ""
        alt = attrs.get("alt") if isinstance(attrs.get("alt"), str) else ""
        return (
            f'<img src="{html.escape(src, quote=True)}" '
            f'alt="{html.escape(alt, quote=True)}" />'
        )
    return f"<{tag}>{children}</{tag}>"


class NoteHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "iframe", "object", "svg", "form"}:
            self.skip_depth += 1
            return
        if self.skip_depth or tag not in ALLOWED_NOTE_TAGS:
            return
        if tag in {"br", "hr"}:
            self.out.append(f"<{tag} />")
            return
        attrs_dict = dict(attrs)
        safe_attrs: list[str] = []
        if tag == "a":
            href = safe_url(attrs_dict.get("href"))
            if href:
                safe_attrs.append(f'href="{html.escape(href, quote=True)}"')
                safe_attrs.append('rel="noopener nofollow"')
        elif tag == "img":
            src = safe_url(attrs_dict.get("src"))
            if not src:
                return
            safe_attrs.append(f'src="{html.escape(src, quote=True)}"')
            alt = attrs_dict.get("alt") or ""
            safe_attrs.append(f'alt="{html.escape(alt, quote=True)}"')
        self.out.append(
            f"<{tag}"
            + (" " + " ".join(safe_attrs) if safe_attrs else "")
            + ">"
        )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "iframe", "object", "svg", "form"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if (
            not self.skip_depth
            and tag in ALLOWED_NOTE_TAGS
            and tag not in {"br", "hr", "img"}
        ):
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        return "".join(self.out).strip()


def sanitize_html(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parser = NoteHTMLSanitizer()
    parser.feed(value)
    parser.close()
    return parser.result()


def annotation_body(annotation: dict) -> str:
    body = annotation.get("body") or {}
    if isinstance(body, dict) and body.get("dom"):
        rendered = render_dom(body["dom"]).strip()
        if rendered:
            return rendered
    if isinstance(body, dict) and body.get("html"):
        rendered = sanitize_html(body["html"])
        if rendered:
            return rendered
    markdown = body.get("markdown") if isinstance(body, dict) else None
    if isinstance(markdown, str) and markdown.strip():
        paragraphs = []
        for part in re.split(r"\n\s*\n", markdown.strip()):
            if part.strip() == "***":
                paragraphs.append("<hr />")
            else:
                text = html.escape(part.strip())
                text = re.sub(
                    r"\[([^]]+)\]\((https?://[^)]+)\)",
                    r'<a href="\2">\1</a>',
                    text,
                )
                paragraphs.append(f"<p>{text.replace(chr(10), '<br />')}</p>")
        return "".join(paragraphs)
    return ""


def author_metadata(annotation: dict) -> list[dict]:
    result: list[dict] = []
    for attribution in annotation.get("authors") or []:
        user = attribution.get("user") if isinstance(attribution, dict) else None
        if not isinstance(user, dict):
            continue
        result.append(
            {
                "id": user.get("id"),
                "login": user.get("login"),
                "name": user.get("name") or user.get("login"),
                "role": user.get("role_for_display")
                or user.get("human_readable_role_for_display"),
                "attribution": attribution.get("attribution"),
            }
        )
    return result


def normalize_notes(
    chapter: int, page_url: str, info: dict, refs: dict[str, dict]
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
            body = annotation_body(annotation)
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
            note_id = f"portrait-genius-{original_id}"
            anchor_text = str(
                (referent.get("range") or {}).get("content") or fragment
            ).strip()
            notes.append(
                {
                    "id": note_id,
                    "work_id": "portrait",
                    "chapter_id": f"portrait-{chapter}",
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
                        "contributors": author_metadata(annotation),
                    },
                }
            )
    return notes, failed


def import_genius(
    *, fetch: bool = True, base_dir: Path | None = None
) -> list[dict]:
    global MANIFEST_PATH, PORTRAIT_DIR, RAW_DIR, API_DIR, NORMALIZED_DIR, REPORT_DIR
    if base_dir is not None:
        MANIFEST_PATH = base_dir / "sources" / "portrait-genius.json"
        PORTRAIT_DIR = base_dir / "data" / "works" / "portrait"
        RAW_DIR = PORTRAIT_DIR / "sources" / "genius" / "raw"
        API_DIR = RAW_DIR / "api"
        NORMALIZED_DIR = PORTRAIT_DIR / "normalized" / "annotations"
        REPORT_DIR = PORTRAIT_DIR / "manifests"

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    all_notes: list[dict] = []
    all_failed: list[dict] = []
    page_summaries: list[dict] = []
    for entry in manifest.get("chapters") or []:
        chapter = int(entry["chapter"])
        url = str(entry["url"])
        _, info = fetch_page(chapter, url, allow_network=fetch)
        refs = load_api_referents(
            chapter, info["referent_ids"], allow_network=fetch
        )
        notes, failed = normalize_notes(chapter, url, info, refs)
        all_notes.extend(notes)
        all_failed.extend(failed)
        page_summaries.append(
            {
                "chapter": chapter,
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
                "work": "portrait",
                "source": {
                    "type": "genius",
                    "manifest": "sources/portrait-genius.json",
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
        f"{len(all_failed)} acquisition records need inspection"
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
        print(f"Portrait Genius import failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
