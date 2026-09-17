#!/usr/bin/env python3
"""Import the canonical Project Gutenberg text of Joyce's *Portrait*.

The browser never fetches Gutenberg.  This module is a build-time importer:
it caches the source HTML and plain text, removes the Gutenberg boilerplate,
and writes five clean chapter sources plus bare chapter JSON files.  The
Portrait build later adds annotation links to those chapter files.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
PORTRAIT_DIR = BASE_DIR / "data" / "works" / "portrait"
RAW_DIR = PORTRAIT_DIR / "sources" / "gutenberg" / "raw"
NORMALIZED_DIR = PORTRAIT_DIR / "sources" / "gutenberg" / "normalized"
CHAPTER_DIR = PORTRAIT_DIR / "chapters"

EBOOK_ID = "4217"
HTML_URL = "https://www.gutenberg.org/cache/epub/4217/pg4217-images.html"
TEXT_URL = "https://www.gutenberg.org/cache/epub/4217/pg4217.txt"

CHAPTER_IDS = {
    1: "portrait-1",
    2: "portrait-2",
    3: "portrait-3",
    4: "portrait-4",
    5: "portrait-5",
}


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Joyce-reader-build/1.0 (+local personal reader)",
            "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def ensure_sources(fetch: bool) -> tuple[Path, Path]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    html_path = RAW_DIR / "pg4217-images.html"
    text_path = RAW_DIR / "pg4217.txt"

    if fetch or not html_path.exists():
        print(f"Gutenberg: fetching {HTML_URL}")
        html_path.write_bytes(fetch_bytes(HTML_URL))
    if fetch or not text_path.exists():
        print(f"Gutenberg: fetching {TEXT_URL}")
        text_path.write_bytes(fetch_bytes(TEXT_URL))

    metadata_path = RAW_DIR / "source.json"
    metadata = {
        "ebook_id": EBOOK_ID,
        "title": "A Portrait of the Artist as a Young Man",
        "author": "James Joyce",
        "html_url": HTML_URL,
        "text_url": TEXT_URL,
        "source_license": "Project Gutenberg public-domain eBook; see the source header for its license terms.",
    }
    if fetch or not metadata_path.exists():
        metadata["retrieved_at"] = datetime.now(timezone.utc).isoformat()
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return html_path, text_path


class ChapterHTMLSerializer(HTMLParser):
    """Keep literary markup while removing source-only HTML noise."""

    ALLOWED = {
        "p",
        "h2",
        "h3",
        "h4",
        "i",
        "em",
        "b",
        "strong",
        "br",
        "hr",
        "blockquote",
        "pre",
    }
    BLOCKS = {"p", "h2", "h3", "h4", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.at_block_start = False
        self.after_break = False

    def _trim_last_space(self) -> None:
        if not self.out:
            return
        self.out[-1] = re.sub(r"[ \t\u00a0]+$", "", self.out[-1])
        if not self.out[-1]:
            self.out.pop()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in self.ALLOWED:
            return
        if tag == "br":
            self._trim_last_space()
            self.out.append("<br />")
            self.after_break = True
            return
        if tag == "hr":
            self._trim_last_space()
            self.out.append("<hr />")
            return

        if tag in self.BLOCKS:
            self.at_block_start = True
        if tag == "p":
            classes = ""
            for key, value in attrs:
                if key == "class" and value:
                    kept = [part for part in value.split() if re.fullmatch(r"[A-Za-z0-9_-]+", part)]
                    if kept:
                        classes = ' class="' + " ".join(kept) + '"'
                    break
            self.out.append(f"<p{classes}>")
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in self.ALLOWED or tag in {"br", "hr"}:
            return
        self._trim_last_space()
        self.out.append(f"</{tag}>")
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        if tag in self.BLOCKS:
            self.at_block_start = False
        self.after_break = False

    def handle_data(self, data: str) -> None:
        if not data:
            return
        data = data.replace("\u00a0", " ")
        if "pre" not in self.stack:
            data = re.sub(r"\s+", " ", data)
            if self.at_block_start or self.after_break:
                data = data.lstrip(" ")
        if data:
            self.out.append(html.escape(data, quote=False))
            self.at_block_start = False
            self.after_break = False

    def result(self) -> str:
        return "".join(self.out).strip()


def extract_chapter_html(source: str) -> list[tuple[int, str, str]]:
    """Return ``(number, title, html)`` for each Gutenberg chapter div."""
    first_chapter_start = re.search(
        r'<div\s+class=["\']chapter["\']\s*>', source, re.IGNORECASE
    )
    epigraph = ""
    if first_chapter_start:
        # Gutenberg places the novel's epigraph immediately before the first
        # chapter div. Treat it as part of Chapter I so it remains readable
        # and can be anchored to the same canonical chapter as the TEI/Genius
        # source records.
        match = re.search(
            r'<p\b[^>]*>\s*<i\b[^>]*>.*?Et\s+ignotas.*?</p>',
            source[: first_chapter_start.start()],
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            epigraph = match.group(0)
    chunks = re.findall(
        r'<div\s+class=["\']chapter["\']\s*>(.*?)</div>\s*<!--\s*end chapter\s*-->',
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if len(chunks) != 5:
        raise RuntimeError(f"Expected five Gutenberg chapters, found {len(chunks)}")

    chapters: list[tuple[int, str, str]] = []
    for chunk in chunks:
        heading = re.search(r">\s*Chapter\s+([IVX]+)\s*</h2>", chunk, re.IGNORECASE)
        if not heading:
            raise RuntimeError("A Gutenberg chapter is missing its Chapter I–V heading")
        roman = heading.group(1).upper()
        number = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}.get(roman)
        if number is None:
            raise RuntimeError(f"Unknown Portrait chapter heading: {roman}")
        parser = ChapterHTMLSerializer()
        parser.feed((epigraph if number == 1 else "") + chunk)
        parser.close()
        chapter_html = parser.result()
        if not chapter_html:
            raise RuntimeError(f"Gutenberg Chapter {roman} produced empty HTML")
        chapters.append((number, f"Chapter {roman}", chapter_html))

    chapters.sort(key=lambda item: item[0])
    if [number for number, _, _ in chapters] != [1, 2, 3, 4, 5]:
        raise RuntimeError("Gutenberg chapters are not in I–V order")
    return chapters


def import_text(*, fetch: bool = True, base_dir: Path | None = None) -> list[dict]:
    """Import and write the five bare canonical chapter records."""
    global PORTRAIT_DIR, RAW_DIR, NORMALIZED_DIR, CHAPTER_DIR
    if base_dir is not None:
        PORTRAIT_DIR = base_dir / "data" / "works" / "portrait"
        RAW_DIR = PORTRAIT_DIR / "sources" / "gutenberg" / "raw"
        NORMALIZED_DIR = PORTRAIT_DIR / "sources" / "gutenberg" / "normalized"
        CHAPTER_DIR = PORTRAIT_DIR / "chapters"

    html_path, _ = ensure_sources(fetch)
    source = html_path.read_text(encoding="utf-8", errors="replace")
    chapters = extract_chapter_html(source)
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
    CHAPTER_DIR.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    for number, title, chapter_html in chapters:
        chapter_id = CHAPTER_IDS[number]
        (NORMALIZED_DIR / f"{chapter_id}.html").write_text(chapter_html + "\n", encoding="utf-8")
        record = {
            "id": chapter_id,
            "work_id": "portrait",
            "number": number,
            "label": ["I", "II", "III", "IV", "V"][number - 1],
            "title": title,
            "html_source": chapter_html,
            "source": {
                "type": "gutenberg",
                "ebook_id": EBOOK_ID,
                "url": HTML_URL,
            },
        }
        (CHAPTER_DIR / f"{chapter_id}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        records.append(record)

    print(f"Gutenberg: normalized {len(records)} chapters")
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", "--no-fetch", action="store_true", help="Use only the cached raw source")
    args = parser.parse_args(argv)
    try:
        import_text(fetch=not args.offline)
    except Exception as exc:  # pragma: no cover - command-line diagnostics
        print(f"Portrait Gutenberg import failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
