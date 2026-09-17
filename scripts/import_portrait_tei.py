#!/usr/bin/env python3
"""Import useful semantic annotations from the Open Editions Portrait TEI.

The TEI edition is a secondary source.  This importer deliberately selects
semantic elements rather than making every structural XML element clickable.
It writes normalized note candidates; the Portrait build subsequently anchors
them to the Gutenberg text.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

from xml.etree import ElementTree as ET


BASE_DIR = Path(__file__).resolve().parent.parent
PORTRAIT_DIR = BASE_DIR / "data" / "works" / "portrait"
RAW_DIR = PORTRAIT_DIR / "sources" / "tei" / "raw"
NORMALIZED_DIR = PORTRAIT_DIR / "normalized" / "annotations"
REPORT_DIR = PORTRAIT_DIR / "manifests"

REPO_URL = "https://github.com/open-editions/corpus-joyce-portrait-TEI"
XML_URL = REPO_URL + "/blob/master/portrait.xml"
RAW_XML_URL = "https://raw.githubusercontent.com/open-editions/corpus-joyce-portrait-TEI/master/portrait.xml"
RAW_README_URL = "https://raw.githubusercontent.com/open-editions/corpus-joyce-portrait-TEI/master/README.md"
RAW_LICENSE_URL = "https://raw.githubusercontent.com/open-editions/corpus-joyce-portrait-TEI/master/LICENSE"

XML_NS = "{http://www.w3.org/XML/1998/namespace}"


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Joyce-reader-build/1.0 (+local personal reader)",
            "Accept": "application/xml,text/plain;q=0.9,*/*;q=0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def ensure_source(fetch: bool) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    xml_path = RAW_DIR / "portrait.xml"
    if fetch or not xml_path.exists():
        print(f"Open Editions TEI: fetching {RAW_XML_URL}")
        xml_path.write_bytes(fetch_bytes(RAW_XML_URL))

    # These are useful for provenance/offline inspection, but parsing depends
    # only on portrait.xml so a missing auxiliary file is non-fatal offline.
    for filename, url in (("README.md", RAW_README_URL), ("LICENSE", RAW_LICENSE_URL)):
        path = RAW_DIR / filename
        if fetch or not path.exists():
            try:
                path.write_bytes(fetch_bytes(url))
            except Exception as exc:
                if not path.exists():
                    print(f"  warning: could not cache TEI {filename}: {exc}", file=sys.stderr)

    source_meta = RAW_DIR / "source.json"
    if fetch or not source_meta.exists():
        source_meta.write_text(
            json.dumps(
                {
                    "repository": REPO_URL,
                    "revision": "master",
                    "xml_url": XML_URL,
                    "raw_url": RAW_XML_URL,
                    "license_url": REPO_URL + "/blob/master/LICENSE",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return xml_path


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_attributes(element: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in element.attrib.items():
        result[local_name(key)] = value
    return result


SKIP_TEXT_TAGS = {"lb", "milestone", "geo", "location"}


def visible_text(element: ET.Element, skip_tags: set[str] | None = None) -> str:
    """Text visible to a reader; omit line-number and coordinate scaffolding."""
    skip_tags = set(skip_tags or ())
    pieces: list[str] = []

    def visit(node: ET.Element) -> None:
        if local_name(node.tag) in SKIP_TEXT_TAGS or local_name(node.tag) in skip_tags:
            return
        if node.text:
            pieces.append(node.text)
        for child in node:
            visit(child)
            if child.tail:
                pieces.append(child.tail)

    visit(element)
    return re.sub(r"\s+", " ", "".join(pieces)).strip()


def nearest_ancestor(element: ET.Element, parents: dict[ET.Element, ET.Element], name: str) -> ET.Element | None:
    current = parents.get(element)
    while current is not None:
        if local_name(current.tag) == name:
            return current
        current = parents.get(current)
    return None


def place_metadata(element: ET.Element, parents: dict[ET.Element, ET.Element]) -> dict[str, object]:
    place = nearest_ancestor(element, parents, "place")
    if place is None:
        return {}
    location = next((child for child in place.iter() if local_name(child.tag) == "location"), None)
    geo = next((child for child in place.iter() if local_name(child.tag) == "geo"), None)
    result: dict[str, object] = {}
    if geo is not None and geo.text:
        result["coordinates"] = " ".join(geo.text.split())
    if location is not None:
        result["location"] = xml_attributes(location)
    return result


def semantic_kind(element: ET.Element) -> tuple[str, str] | None:
    tag = local_name(element.tag)
    attrs = xml_attributes(element)
    typ = attrs.get("type", "")
    lang = attrs.get("lang", "")

    if tag == "persName":
        return "person", "person name"
    if tag == "placeName":
        return "place", "place name"
    if tag == "date":
        return "date", "date"
    if tag == "ref":
        return "reference", "cross-reference"
    if tag == "quote":
        return "quotation", "quotation"
    if tag == "bibl":
        return "bibliography", "bibliographic reference"
    if tag == "lg" and typ in {"song", "prayer", "chant", "letter"}:
        return typ, typ
    if tag == "seg" and (typ in {"neologism", "prayer", "song", "chant"} or lang):
        return lang or typ, (f"{lang} passage" if lang else typ)
    return None


def note_title(kind: str, label: str, text: str) -> str:
    short = text if len(text) <= 72 else text[:69].rstrip() + "…"
    if kind == "person":
        return f"Person: {short}"
    if kind == "place":
        return f"Place: {short}"
    if kind == "date":
        return f"Date: {short}"
    if kind == "reference":
        return f"Reference: {short}"
    if kind == "bibliography":
        return f"Bibliographic reference: {short}"
    if kind == "quotation":
        return f"Quotation: {short}"
    if kind in {"la", "fr", "ita"}:
        return f"{kind.upper()} passage: {short}"
    return f"{label.capitalize()}: {short}"


def note_html(kind: str, label: str, text: str, attrs: dict[str, str], extra: dict[str, object]) -> str:
    safe_text = html.escape(text)
    details: list[str] = []
    if attrs.get("lang"):
        details.append(f"Language: {html.escape(attrs['lang'])}")
    if attrs.get("type"):
        details.append(f"TEI type: {html.escape(attrs['type'])}")
    if extra.get("coordinates"):
        details.append(f"Coordinates: {html.escape(str(extra['coordinates']))}")
    detail_html = ""
    if details:
        detail_html = "<p class=\"tei-detail\">" + " · ".join(details) + "</p>"
    return (
        f"<p>This passage is marked in the Open Editions semantic edition as a "
        f"{html.escape(label)}.</p>"
        f"<blockquote><p>{safe_text}</p></blockquote>"
        f"{detail_html}"
    )


def extract_annotations(xml_path: Path) -> list[dict]:
    root = ET.parse(xml_path).getroot()
    parents: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parents[child] = parent

    body = next((element for element in root.iter() if local_name(element.tag) == "body"), None)
    if body is None:
        raise RuntimeError("TEI source has no text/body element")

    annotations: list[dict] = []
    for chapter in body.iter():
        if local_name(chapter.tag) != "div" or xml_attributes(chapter).get("type") != "chapter":
            continue
        chapter_number = xml_attributes(chapter).get("n")
        if chapter_number not in {"1", "2", "3", "4", "5"}:
            continue
        chapter_id = f"portrait-{chapter_number}"
        ordinal = 0
        for element in chapter.iter():
            kind_info = semantic_kind(element)
            if kind_info is None:
                continue
            text = visible_text(element, {"ref"} if local_name(element.tag) == "quote" else None)
            if not text:
                continue
            kind, label = kind_info
            ordinal += 1
            attrs = xml_attributes(element)
            extra = place_metadata(element, parents)
            source_id = f"portrait-tei-{chapter_number}-{ordinal:04d}"
            annotations.append(
                {
                    "id": source_id,
                    "work_id": "portrait",
                    "chapter_id": chapter_id,
                    "title": note_title(kind, label, text),
                    "html_source": note_html(kind, label, text, attrs, extra),
                    "source": {
                        "type": "tei",
                        "url": XML_URL,
                        "original_id": f"portrait.xml:chapter-{chapter_number}:{ordinal}",
                    },
                    "anchor": {
                        "text": text,
                        "prefix": "",
                        "suffix": "",
                        "start": None,
                        "end": None,
                    },
                    "media_doc_ids": [],
                    "tei": {
                        "tag": local_name(element.tag),
                        "attributes": attrs,
                        **extra,
                    },
                    "provenance": {
                        "match_method": "unresolved",
                        "confidence": 0.0,
                    },
                }
            )
    return annotations


def import_tei(*, fetch: bool = True, base_dir: Path | None = None) -> list[dict]:
    global PORTRAIT_DIR, RAW_DIR, NORMALIZED_DIR, REPORT_DIR
    if base_dir is not None:
        PORTRAIT_DIR = base_dir / "data" / "works" / "portrait"
        RAW_DIR = PORTRAIT_DIR / "sources" / "tei" / "raw"
        NORMALIZED_DIR = PORTRAIT_DIR / "normalized" / "annotations"
        REPORT_DIR = PORTRAIT_DIR / "manifests"

    xml_path = ensure_source(fetch)
    annotations = extract_annotations(xml_path)
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (NORMALIZED_DIR / "tei_annotations.json").write_text(
        json.dumps(
            {
                "work": "portrait",
                "source": {"type": "tei", "url": XML_URL},
                "annotations": annotations,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    by_kind: dict[str, int] = {}
    for annotation in annotations:
        kind = (
            annotation["tei"]["attributes"].get("type")
            or annotation["tei"]["attributes"].get("lang")
            or annotation["tei"]["tag"]
        )
        by_kind[kind] = by_kind.get(kind, 0) + 1
    (REPORT_DIR / "tei-summary.json").write_text(
        json.dumps({"discovered": len(annotations), "by_kind": by_kind}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Open Editions TEI: normalized {len(annotations)} semantic annotations")
    return annotations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", "--no-fetch", action="store_true", help="Use only cached TEI source files")
    args = parser.parse_args(argv)
    try:
        import_tei(fetch=not args.offline)
    except Exception as exc:  # pragma: no cover - command-line diagnostics
        print(f"Portrait TEI import failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
