#!/usr/bin/env python3
"""Check that a Ulysses annotation import preserves the existing mirror.

The normal repository validator checks that the data set is internally
consistent.  This validator also keeps a small, dependency-free baseline of
the pre-import Ulysses state.  New Genius notes and links may be added, but
the original chapter text, chapter metadata, note files, note manifest
entries, and annotation links must remain present.

Usage::

    python3 scripts/validate_ulysses_import.py --write-baseline
    python3 scripts/validate_ulysses_import.py

For an idempotence check, save the first import's file state, run the import
again, and compare the state::

    python3 scripts/validate_ulysses_import.py --write-state /tmp/ulysses-1.json
    python3 scripts/validate_ulysses_import.py --compare-state /tmp/ulysses-1.json

Only Python's standard library is used.  The baseline is intentionally
content-addressed rather than storing the large note bodies a second time.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import html
import json
import re
import sys
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = BASE_DIR / "tests" / "fixtures" / "ulysses-baseline.json"
WORKS_PATH = BASE_DIR / "data" / "works.json"
NOTE_MANIFEST_PATH = BASE_DIR / "data" / "notes.json"

sys.path.insert(0, str(BASE_DIR / "scripts"))
import validate as repository_validator

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "blockquote", "pre", "li"}
EXTERNAL_SCHEMES = ("http:", "https:", "mailto:", "#", "info:")


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_anchor_text(value: str) -> str:
    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKC", value)
    return " ".join(value.split()).casefold()


class HtmlSnapshotParser(HTMLParser):
    """Extract visible text and annotation anchors from chapter HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.anchor_stack: list[dict[str, Any]] = []
        self.anchors: list[dict[str, Any]] = []
        self.position = 0

    def _append(self, text: str) -> None:
        if not text:
            return
        self.parts.append(text)
        self.position += len(text)
        for anchor in self.anchor_stack:
            anchor["parts"].append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {key: value or "" for key, value in attrs}
        if tag == "br":
            self._append("\n")
        if tag == "a":
            self.anchor_stack.append({"attrs": attrs_map, "parts": [], "start": self.position})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() == "a" and self.anchor_stack:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in BLOCK_TAGS:
            self._append("\n\n")
        if tag != "a" or not self.anchor_stack:
            return
        anchor = self.anchor_stack.pop()
        attrs = anchor["attrs"]
        href = str(attrs.get("href") or "")
        raw_ids = str(attrs.get("data-notes") or "")
        ids = [item for item in re.split(r"[,\s]+", raw_ids) if item]
        if href and href not in ids:
            ids.insert(0, href)
        self.anchors.append(
            {
                "href": href,
                "data_type": str(attrs.get("data-type") or ""),
                "ids": ids,
                "text": "".join(anchor["parts"]),
                "start": anchor["start"],
                "end": self.position,
            }
        )

    def handle_data(self, data: str) -> None:
        self._append(data)

    @property
    def plain_text(self) -> str:
        return "".join(self.parts)


def parse_html(source: str) -> HtmlSnapshotParser:
    parser = HtmlSnapshotParser()
    parser.feed(source or "")
    parser.close()
    return parser


def annotation_ids(anchor: dict[str, Any]) -> list[str]:
    """Return local note IDs, excluding normal outbound/app links."""
    result = []
    for value in anchor.get("ids") or []:
        value = str(value)
        if value and not value.lower().startswith(EXTERNAL_SCHEMES):
            result.append(value)
    return result


def chapter_paths() -> tuple[dict[str, Any], Path, dict[str, Path]]:
    works = read_json(WORKS_PATH)
    if not isinstance(works, list):
        raise ValueError("data/works.json must contain an array")
    work = next((item for item in works if item.get("id") == "ulysses"), None)
    if not work:
        raise ValueError("data/works.json has no Ulysses work")
    legacy = work.get("legacy_paths") or {}
    configured = legacy.get("chapters")
    if configured:
        chapter_dir = BASE_DIR / configured
    else:
        generated = BASE_DIR / "data" / "works" / "ulysses" / "chapters"
        chapter_dir = generated if generated.exists() else BASE_DIR / "data" / "chapters"
    descriptors = {
        str(item["id"]): item for item in work.get("chapters", []) if item.get("id")
    }
    return work, chapter_dir, {
        chapter_id: chapter_dir / f"{chapter_id}.json"
        for chapter_id in descriptors
    }


def note_paths() -> tuple[list[dict[str, Any]], Path, dict[str, Path], Path]:
    works = read_json(WORKS_PATH)
    work = next((item for item in works if item.get("id") == "ulysses"), None)
    if not work:
        raise ValueError("data/works.json has no Ulysses work")
    legacy = work.get("legacy_paths") or {}
    configured = legacy.get("notes")
    if configured:
        note_dir = BASE_DIR / configured
    else:
        generated = BASE_DIR / "data" / "works" / "ulysses" / "notes"
        note_dir = generated if generated.exists() else BASE_DIR / "data" / "notes"
    # Legacy Ulysses uses a top-level notes.json manifest; generated
    # work-scoped output uses notes/index.json. Follow the path the reader is
    # configured to serve, while retaining the old fallback for compatibility.
    if (note_dir / "index.json").exists():
        manifest_path = note_dir / "index.json"
    elif note_dir == BASE_DIR / "data" / "notes" and NOTE_MANIFEST_PATH.exists():
        manifest_path = NOTE_MANIFEST_PATH
    else:
        manifest_path = note_dir / "index.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, list):
        raise ValueError(f"{manifest_path.relative_to(BASE_DIR)} must contain an array")
    paths = {
        str(entry["id"]): note_dir / f"{entry['id']}.json"
        for entry in manifest
        if isinstance(entry, dict) and entry.get("id")
    }
    return manifest, note_dir, paths, manifest_path


def reference_counts(chapter_files: dict[str, Path]) -> tuple[collections.Counter[str], collections.Counter[tuple[str, str]]]:
    ids: collections.Counter[str] = collections.Counter()
    keyed: collections.Counter[tuple[str, str]] = collections.Counter()
    for path in chapter_files.values():
        if not path.exists():
            continue
        chapter = read_json(path)
        parser = parse_html(str(chapter.get("html_source") or ""))
        for anchor in parser.anchors:
            local_ids = annotation_ids(anchor)
            text = normalized_anchor_text(str(anchor.get("text") or ""))
            for note_id in local_ids:
                ids[note_id] += 1
                keyed[(note_id, text)] += 1
    return ids, keyed


def original_anchor_coverage(
    expected_path: str | None, current_parser: HtmlSnapshotParser
) -> list[str] | None:
    """Check old links by visible-span coverage, allowing safe overlay splits."""
    if not expected_path:
        return None
    old_path = BASE_DIR / expected_path
    if not old_path.exists():
        return None
    try:
        old = read_json(old_path)
        old_parser = parse_html(str(old.get("html_source") or ""))
    except Exception:
        return None
    by_id: dict[str, list[tuple[int, int]]] = collections.defaultdict(list)
    for anchor in current_parser.anchors:
        for note_id in annotation_ids(anchor):
            by_id[note_id].append((int(anchor["start"]), int(anchor["end"])))
    errors: list[str] = []
    for anchor in old_parser.anchors:
        start, end = int(anchor["start"]), int(anchor["end"])
        for note_id in annotation_ids(anchor):
            intervals = sorted(
                (left, right)
                for left, right in by_id.get(note_id, [])
                if right > start and left < end
            )
            covered = start
            for left, right in intervals:
                if left > covered:
                    break
                covered = max(covered, right)
                if covered >= end:
                    break
            if covered < end:
                text = normalized_anchor_text(str(anchor.get("text") or ""))
                errors.append(
                    f"lost annotation {note_id!r} on {text[:60]!r} "
                    f"(coverage {covered - start}/{end - start})"
                )
    return errors


def build_baseline() -> dict[str, Any]:
    work, chapter_dir, chapter_files = chapter_paths()
    manifest, note_dir, notes, manifest_path = note_paths()
    descriptors = {
        str(item["id"]): item for item in work.get("chapters", []) if item.get("id")
    }
    chapters: dict[str, Any] = {}
    for chapter_id, path in chapter_files.items():
        descriptor = descriptors[chapter_id]
        if not path.exists():
            raise FileNotFoundError(path)
        chapter = read_json(path)
        parser = parse_html(str(chapter.get("html_source") or ""))
        anchor_counts: collections.Counter[tuple[str, str]] = collections.Counter()
        for anchor in parser.anchors:
            text = normalized_anchor_text(str(anchor.get("text") or ""))
            for note_id in annotation_ids(anchor):
                anchor_counts[(note_id, text)] += 1
        chapters[chapter_id] = {
            "path": str(path.relative_to(BASE_DIR)),
            "number": descriptor.get("number"),
            "title": descriptor.get("title"),
            "id": chapter.get("id"),
            "html_text_sha256": hashlib.sha256(parser.plain_text.encode("utf-8")).hexdigest(),
            "html_text_length": len(parser.plain_text),
            "search_text_sha256": json_digest(chapter.get("search_text", [])),
            "anchor_counts": {
                f"{note_id}\u0000{text}": count
                for (note_id, text), count in sorted(anchor_counts.items())
            },
        }

    note_entries = {}
    note_files = {}
    for entry in manifest:
        note_id = str(entry.get("id") or "")
        if not note_id:
            raise ValueError("data/notes.json contains an entry without an id")
        path = notes[note_id]
        if not path.exists():
            raise FileNotFoundError(path)
        note_entries[note_id] = {
            "manifest_sha256": json_digest(entry),
            "title": entry.get("title"),
        }
        note_files[note_id] = {
            "path": str(path.relative_to(BASE_DIR)),
            "sha256": file_digest(path),
        }

    ids, _ = reference_counts(chapter_files)
    missing = {note_id: count for note_id, count in ids.items() if note_id not in notes}
    orphaned = sorted(note_id for note_id in notes if not ids.get(note_id))
    return {
        "schema": 1,
        "work_id": "ulysses",
        "work_chapter_ids": [str(item["id"]) for item in work.get("chapters", [])],
        "chapters": chapters,
        "note_manifest_path": str(manifest_path.relative_to(BASE_DIR)),
        "note_manifest_order": [str(entry["id"]) for entry in manifest],
        "note_entries": note_entries,
        "note_files": note_files,
        "baseline_dangling_references": missing,
        "baseline_orphaned_notes": orphaned,
    }


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_baseline(path: Path) -> dict[str, Any]:
    baseline = read_json(path)
    if baseline.get("schema") != 1:
        raise ValueError(f"unsupported baseline schema in {path}")
    return baseline


def check_preservation(baseline: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        work, chapter_dir, chapter_files = chapter_paths()
        manifest, note_dir, notes, manifest_path = note_paths()
    except Exception as exc:
        return [str(exc)]

    current_chapter_ids = [str(item.get("id")) for item in work.get("chapters", [])]
    expected_chapter_ids = list(baseline.get("work_chapter_ids") or [])
    if not all(item in current_chapter_ids for item in expected_chapter_ids):
        errors.append("one or more original Ulysses chapters are missing from works.json")
    elif [item for item in current_chapter_ids if item in expected_chapter_ids] != expected_chapter_ids:
        errors.append("original Ulysses chapter order changed")

    for chapter_id, expected in (baseline.get("chapters") or {}).items():
        path = chapter_files.get(chapter_id)
        if not path or not path.exists():
            errors.append(f"missing original chapter file: {expected.get('path', chapter_id)}")
            continue
        try:
            chapter = read_json(path)
            parser = parse_html(str(chapter.get("html_source") or ""))
        except Exception as exc:
            errors.append(f"{path.relative_to(BASE_DIR)} cannot be read: {exc}")
            continue
        if chapter.get("id") != expected.get("id"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its id")
        if chapter.get("number") != expected.get("number"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its chapter number")
        if chapter.get("title") != expected.get("title"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its title")
        text_digest = hashlib.sha256(parser.plain_text.encode("utf-8")).hexdigest()
        if text_digest != expected.get("html_text_sha256") or len(parser.plain_text) != expected.get("html_text_length"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its visible text")
        if json_digest(chapter.get("search_text", [])) != expected.get("search_text_sha256"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its search index")

        coverage_errors = original_anchor_coverage(expected.get("path"), parser)
        if coverage_errors is not None:
            errors.extend(f"{path.relative_to(BASE_DIR)} {item}" for item in coverage_errors)
        else:
            current_counts: collections.Counter[tuple[str, str]] = collections.Counter()
            for anchor in parser.anchors:
                text = normalized_anchor_text(str(anchor.get("text") or ""))
                for note_id in annotation_ids(anchor):
                    current_counts[(note_id, text)] += 1
            for raw_key, count in (expected.get("anchor_counts") or {}).items():
                note_id, text = raw_key.split("\u0000", 1)
                if current_counts[(note_id, text)] < count:
                    errors.append(
                        f"{path.relative_to(BASE_DIR)} lost annotation {note_id!r} "
                        f"on {text[:60]!r} (expected {count}, found {current_counts[(note_id, text)]})"
                    )

    current_manifest_by_id = {
        str(entry.get("id")): entry for entry in manifest if isinstance(entry, dict) and entry.get("id")
    }
    current_order = [str(entry.get("id")) for entry in manifest if isinstance(entry, dict) and entry.get("id")]
    seen: set[str] = set()
    for note_id in current_order:
        if note_id in seen:
            errors.append(f"duplicate note id in data/notes.json: {note_id}")
        seen.add(note_id)
    expected_order = list(baseline.get("note_manifest_order") or [])
    missing_manifest_ids = [note_id for note_id in expected_order if note_id not in current_manifest_by_id]
    if missing_manifest_ids:
        errors.append(
            "original note manifest entries were removed: "
            + ", ".join(missing_manifest_ids[:20])
        )
        if len(missing_manifest_ids) > 20:
            errors.append(f"... and {len(missing_manifest_ids) - 20} more removed note entries")

    baseline_manifest_path = str(baseline.get("note_manifest_path") or "data/notes.json")
    current_manifest_path = str(manifest_path.relative_to(BASE_DIR))
    for note_id, expected in (baseline.get("note_entries") or {}).items():
        entry = current_manifest_by_id.get(note_id)
        if entry is None:
            errors.append(f"missing original note manifest entry: {note_id}")
        elif current_manifest_path == baseline_manifest_path and json_digest(entry) != expected.get("manifest_sha256"):
            errors.append(f"changed original note manifest entry: {note_id}")
    for note_id, expected in (baseline.get("note_files") or {}).items():
        path = notes.get(note_id)
        if not path or not path.exists():
            errors.append(f"missing original note file: {expected.get('path', note_id)}")
        elif file_digest(path) != expected.get("sha256"):
            errors.append(f"changed original note file: {path.relative_to(BASE_DIR)}")
    return errors


def overlay_quarantine_ids() -> set[str]:
    result: set[str] = set()
    report_dir = BASE_DIR / "data" / "works" / "ulysses" / "manifests"
    for report_name in ("unmatched.json", "ambiguous.json"):
        report_path = report_dir / report_name
        if not report_path.exists():
            continue
        report = read_json(report_path) or []
        if isinstance(report, list):
            result.update(
                str(item.get("source_id"))
                for item in report
                if isinstance(item, dict) and item.get("source_id")
            )
    return result


def overlay_integrity(baseline: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate the work-scoped files that the current reader serves."""
    root = BASE_DIR / "data" / "works" / "ulysses"
    manifest_path = root / "manifests" / "overlay-manifest.json"
    if not manifest_path.exists():
        return [], []
    errors: list[str] = []
    warnings: list[str] = []
    chapter_dir = root / "chapters"
    note_dir = root / "notes"
    media_dir = root / "media"
    try:
        overlay = read_json(manifest_path)
        if not isinstance(overlay, dict):
            raise ValueError("overlay manifest is not an object")
    except Exception as exc:
        return [f"{manifest_path.relative_to(BASE_DIR)} is invalid: {exc}"], []

    expected_manifest_paths = {
        "base_chapters": "data/chapters",
        "base_notes": "data/notes",
        "generated_chapters": "data/works/ulysses/chapters",
        "generated_notes": "data/works/ulysses/notes",
        "source_manifest": "sources/ulysses-genius.json",
    }
    for key, expected in expected_manifest_paths.items():
        if overlay.get(key) != expected:
            errors.append(f"overlay manifest {key} is {overlay.get(key)!r}, expected {expected!r}")

    try:
        manifest, served_note_dir, served_notes, served_manifest_path = note_paths()
        work, _chapter_dir, served_chapters = chapter_paths()
    except Exception as exc:
        return [str(exc)], []
    # The app falls back to work-scoped paths when legacy_paths is absent.
    # Require those paths to be the generated overlay so the fetched Genius
    # annotations are actually visible.
    served_chapter_dir = next(iter(served_chapters.values()), chapter_dir).parent
    if served_chapter_dir != chapter_dir:
        errors.append("works.json does not route Ulysses chapters to the generated overlay")
    if served_note_dir != note_dir:
        errors.append("works.json does not route Ulysses notes to the generated overlay")
    legacy = work.get("legacy_paths") or {}
    served_media = BASE_DIR / legacy.get("media", "data/works/ulysses/media")
    if served_media != media_dir:
        errors.append("works.json does not route Ulysses media to the generated overlay")

    # Generated note index and files must agree exactly.
    index_path = note_dir / "index.json"
    try:
        index = read_json(index_path)
        if not isinstance(index, list):
            raise ValueError("notes/index.json is not an array")
    except Exception as exc:
        return errors + [f"{index_path.relative_to(BASE_DIR)} is invalid: {exc}"], warnings
    index_ids = [str(item.get("id") or "") for item in index if isinstance(item, dict)]
    if len(index_ids) != len(set(index_ids)):
        errors.append("generated note index contains duplicate IDs")
    index_by_id = {
        str(item.get("id")): item
        for item in index
        if isinstance(item, dict) and item.get("id")
    }
    note_files: dict[str, Path] = {}
    for path in sorted(note_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            note = read_json(path)
        except Exception as exc:
            errors.append(f"{path.relative_to(BASE_DIR)} is invalid JSON: {exc}")
            continue
        note_id = str(note.get("id") or "") if isinstance(note, dict) else ""
        if not note_id:
            errors.append(f"{path.relative_to(BASE_DIR)} has no id")
            continue
        if note_id in note_files:
            errors.append(f"generated notes contain duplicate file ID: {note_id}")
        note_files[note_id] = path
        if path.stem != note_id:
            errors.append(f"{path.relative_to(BASE_DIR)} filename does not match its id")
        if note_id not in index_by_id:
            errors.append(f"generated note is absent from notes/index.json: {note_id}")
        if not isinstance(note.get("html_source"), str) or not note.get("html_source", "").strip():
            errors.append(f"{path.relative_to(BASE_DIR)} has no html_source")
        if note_id.startswith("ulysses-genius-"):
            parser = repository_validator.StructureParser()
            try:
                parser.feed(note.get("html_source", ""))
                parser.close()
            except Exception as exc:
                errors.append(f"{path.relative_to(BASE_DIR)} has malformed HTML: {exc}")
            for issue in parser.errors:
                errors.append(f"{path.relative_to(BASE_DIR)}: {issue}")
    for note_id in index_by_id:
        if note_id not in note_files:
            errors.append(f"generated note index points to missing file: {note_id}")

    baseline_ids = set((baseline.get("note_files") or {}).keys())
    for note_id, expected in (baseline.get("note_files") or {}).items():
        path = note_files.get(note_id)
        if not path:
            errors.append(f"generated overlay is missing original note: {note_id}")
        elif file_digest(path) != expected.get("sha256"):
            errors.append(f"generated overlay changed original note: {note_id}")
        entry = index_by_id.get(note_id) or {}
        if entry.get("title") != (baseline.get("note_entries", {}).get(note_id) or {}).get("title"):
            errors.append(f"generated overlay changed original note title: {note_id}")

    # Generated media must be an exact copy of the existing media catalog.
    legacy_media = BASE_DIR / "data" / "media"
    legacy_media_paths = {
        path.name: path for path in legacy_media.glob("*.json")
    }
    generated_media_paths = {path.name: path for path in media_dir.glob("*.json")}
    if set(generated_media_paths) != set(legacy_media_paths):
        errors.append(
            "generated media catalog differs from the legacy catalog "
            f"({len(generated_media_paths)} vs {len(legacy_media_paths)} files)"
        )
    for name, source in legacy_media_paths.items():
        target = generated_media_paths.get(name)
        if target and file_digest(source) != file_digest(target):
            errors.append(f"generated media changed original file: {name}")

    reference_ids: collections.Counter[str] = collections.Counter()
    reference_keys: collections.Counter[tuple[str, str]] = collections.Counter()
    for chapter_id, expected in (baseline.get("chapters") or {}).items():
        path = chapter_dir / f"{chapter_id}.json"
        if not path.exists():
            errors.append(f"generated overlay is missing chapter: {chapter_id}")
            continue
        try:
            chapter = read_json(path)
            source = str(chapter.get("html_source") or "")
            parser = parse_html(source)
        except Exception as exc:
            errors.append(f"{path.relative_to(BASE_DIR)} is invalid: {exc}")
            continue
        structure = repository_validator.StructureParser()
        try:
            structure.feed(source)
            structure.close()
        except Exception as exc:
            errors.append(f"{path.relative_to(BASE_DIR)} has malformed HTML: {exc}")
        for issue in structure.errors:
            errors.append(f"{path.relative_to(BASE_DIR)}: {issue}")
        if chapter.get("id") != expected.get("id"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its id")
        if chapter.get("number") != expected.get("number"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its chapter number")
        if chapter.get("title") != expected.get("title"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its title")
        if hashlib.sha256(parser.plain_text.encode("utf-8")).hexdigest() != expected.get("html_text_sha256"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its visible text")
        if len(parser.plain_text) != expected.get("html_text_length"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its visible text length")
        if json_digest(chapter.get("search_text", [])) != expected.get("search_text_sha256"):
            errors.append(f"{path.relative_to(BASE_DIR)} changed its search index")
        for anchor_index, anchor in enumerate(parser.anchors, 1):
            ids = annotation_ids(anchor)
            if str(anchor.get("data_type") or "").lower() == "annotation" and not ids:
                errors.append(f"{path.relative_to(BASE_DIR)} annotation anchor {anchor_index} has no note id")
            if len(ids) != len(set(ids)):
                errors.append(f"{path.relative_to(BASE_DIR)} annotation anchor {anchor_index} repeats a note id")
            text = normalized_anchor_text(str(anchor.get("text") or ""))
            for note_id in ids:
                reference_ids[note_id] += 1
                reference_keys[(note_id, text)] += 1
        coverage_errors = original_anchor_coverage(expected.get("path"), parser)
        if coverage_errors is not None:
            errors.extend(f"{path.relative_to(BASE_DIR)} {item}" for item in coverage_errors)
        else:
            current_counts: collections.Counter[tuple[str, str]] = collections.Counter()
            for anchor in parser.anchors:
                text = normalized_anchor_text(str(anchor.get("text") or ""))
                for note_id in annotation_ids(anchor):
                    current_counts[(note_id, text)] += 1
            for raw_key, count in (expected.get("anchor_counts") or {}).items():
                note_id, text = raw_key.split("\u0000", 1)
                if current_counts[(note_id, text)] < count:
                    errors.append(
                        f"{path.relative_to(BASE_DIR)} lost original annotation {note_id!r} "
                        f"on {text[:60]!r}"
                    )
        ranges = chapter.get("annotation_ranges") or []
        if not isinstance(ranges, list):
            errors.append(f"{path.relative_to(BASE_DIR)} annotation_ranges is not an array")
            ranges = []
        for item in ranges:
            if not isinstance(item, dict):
                errors.append(f"{path.relative_to(BASE_DIR)} has a non-object annotation range")
                continue
            note_id = str(item.get("note_id") or "")
            if note_id not in note_files and note_id not in (baseline.get("baseline_dangling_references") or {}):
                errors.append(f"{path.relative_to(BASE_DIR)} range points to missing note: {note_id}")
            start, end = item.get("start"), item.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or not (0 <= start < end <= len(parser.plain_text)):
                errors.append(f"{path.relative_to(BASE_DIR)} has invalid range for {note_id}")
            elif normalized_anchor_text(parser.plain_text[start:end]) != normalized_anchor_text(str(item.get("text") or "")):
                errors.append(f"{path.relative_to(BASE_DIR)} range text mismatch for {note_id}")
            if note_id and not reference_ids.get(note_id):
                errors.append(f"{path.relative_to(BASE_DIR)} range is not rendered as a link: {note_id}")

    baseline_dangling = set((baseline.get("baseline_dangling_references") or {}).keys())
    for note_id, count in sorted(reference_ids.items()):
        if note_id not in note_files and note_id not in baseline_dangling:
            errors.append(f"generated chapter reference has no note file: {note_id} ({count})")
    quarantine = overlay_quarantine_ids()
    new_orphans = sorted(
        note_id for note_id in note_files
        if note_id not in baseline_ids and note_id not in quarantine and not reference_ids.get(note_id)
    )
    if new_orphans:
        errors.append("generated Genius notes are never referenced: " + ", ".join(new_orphans[:20]))
    rendered_quarantine = sorted(note_id for note_id in quarantine if reference_ids.get(note_id))
    if rendered_quarantine:
        errors.append("quarantined Genius notes were rendered: " + ", ".join(rendered_quarantine[:20]))
    build_summary_for_quarantine = read_json(root / "manifests" / "ulysses-build.json") if (root / "manifests" / "ulysses-build.json").exists() else {}
    reported_quarantine = build_summary_for_quarantine.get("genius_notes_quarantined")
    if reported_quarantine is not None and len(quarantine) != reported_quarantine:
        warnings.append("quarantine report count differs from build summary")

    build_report_path = root / "manifests" / "ulysses-build.json"
    if build_report_path.exists():
        try:
            report = read_json(build_report_path)
            if report.get("chapters") != len(baseline.get("chapters") or {}):
                errors.append("Ulysses build report does not cover all chapters")
            if not report.get("legacy_chapters_untouched") or not report.get("legacy_notes_untouched"):
                errors.append("Ulysses build report does not mark legacy inputs untouched")
            normalized_output = root / "normalized" / "annotations" / "genius_notes.json"
            normalized_data = read_json(normalized_output) if normalized_output.exists() else {}
            expected_genius = len((normalized_data or {}).get("notes") or [])
            if report.get("genius_notes_discovered") != expected_genius:
                errors.append("Ulysses build report Genius count disagrees with normalized notes")
        except Exception as exc:
            errors.append(f"{build_report_path.relative_to(BASE_DIR)} is invalid: {exc}")

    return errors, warnings


def check_integrity(baseline: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        _work, _chapter_dir, chapter_files = chapter_paths()
        manifest, note_dir, notes, manifest_path = note_paths()
    except Exception as exc:
        return [str(exc)], []

    normalized_path = (
        BASE_DIR / "data" / "works" / "ulysses" / "normalized" /
        "annotations" / "genius_notes.json"
    )
    if normalized_path.exists():
        try:
            normalized = read_json(normalized_path)
            normalized_notes = normalized.get("notes") or []
            if not isinstance(normalized_notes, list):
                raise ValueError("notes is not an array")
            normalized_ids = [str(note.get("id") or "") for note in normalized_notes]
            if any(not note_id for note_id in normalized_ids):
                errors.append("normalized Genius notes contain an entry without an id")
            duplicates = sorted(
                note_id for note_id, count in collections.Counter(normalized_ids).items()
                if note_id and count > 1
            )
            if duplicates:
                errors.append("normalized Genius notes contain duplicate IDs: " + ", ".join(duplicates[:20]))
            chapter_ids = set(chapter_files)
            for note in normalized_notes:
                if not isinstance(note, dict):
                    errors.append("normalized Genius notes contain a non-object entry")
                    continue
                if note.get("work_id") != "ulysses":
                    errors.append(f"normalized note {note.get('id')!r} has the wrong work_id")
                if note.get("chapter_id") not in chapter_ids:
                    errors.append(f"normalized note {note.get('id')!r} points to an unknown chapter")
                if not isinstance(note.get("html_source"), str) or not note.get("html_source", "").strip():
                    errors.append(f"normalized note {note.get('id')!r} has no html_source")
                source = note.get("source") or {}
                if source.get("type") != "genius" or not source.get("url"):
                    errors.append(f"normalized note {note.get('id')!r} has incomplete Genius provenance")
                if not str((note.get("anchor") or {}).get("text") or "").strip():
                    errors.append(f"normalized note {note.get('id')!r} has no anchor text")
            report_path = BASE_DIR / "data" / "works" / "ulysses" / "manifests" / "genius-failed.json"
            if report_path.exists():
                report = read_json(report_path)
                summaries = report.get("chapters") or []
                if {item.get("chapter") for item in summaries} != set(range(1, 19)):
                    errors.append("Genius import report does not cover all 18 Ulysses chapters")
                failed = report.get("failed") or []
                if failed:
                    warnings.append(f"Genius import retained {len(failed)} empty/malformed annotation records")
        except Exception as exc:
            errors.append(f"{normalized_path.relative_to(BASE_DIR)} is invalid: {exc}")

    if len(notes) != len(manifest):
        errors.append(
            f"{manifest_path.relative_to(BASE_DIR)} has {len(manifest)} entries but its id map has {len(notes)} unique ids"
        )
    for note_id, path in notes.items():
        if not path.exists():
            errors.append(f"manifest note file is missing: {path.relative_to(BASE_DIR)}")
            continue
        try:
            note = read_json(path)
        except Exception as exc:
            errors.append(f"{path.relative_to(BASE_DIR)} is invalid JSON: {exc}")
            continue
        if note.get("id") != note_id:
            errors.append(f"{path.relative_to(BASE_DIR)} has id {note.get('id')!r}, expected {note_id!r}")
        if not isinstance(note.get("html_source"), str) or not note.get("html_source", "").strip():
            errors.append(f"{path.relative_to(BASE_DIR)} has no html_source")

    file_ids = {}
    for path in sorted(note_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            note = read_json(path)
        except Exception as exc:
            errors.append(f"{path.relative_to(BASE_DIR)} is invalid JSON: {exc}")
            continue
        note_id = note.get("id")
        if not note_id:
            errors.append(f"{path.relative_to(BASE_DIR)} has no id")
        elif note_id in file_ids and file_ids[note_id] != path:
            errors.append(f"duplicate note id across files: {note_id}")
        else:
            file_ids[note_id] = path
    for note_id in file_ids:
        if note_id not in notes:
            errors.append(f"note file is absent from {manifest_path.relative_to(BASE_DIR)}: {note_id}")

    reference_ids, _ = reference_counts(chapter_files)
    baseline_dangling = set((baseline.get("baseline_dangling_references") or {}).keys())
    for note_id, count in sorted(reference_ids.items()):
        if note_id not in notes:
            if note_id in baseline_dangling:
                warnings.append(f"legacy dangling reference retained: {note_id} ({count})")
            else:
                errors.append(f"new chapter reference has no note file: {note_id} ({count})")

    baseline_ids = set((baseline.get("note_files") or {}).keys())
    quarantine_ids = set()
    report_dir = BASE_DIR / "data" / "works" / "ulysses" / "manifests"
    for report_name in ("unmatched.json", "ambiguous.json"):
        report_path = report_dir / report_name
        if report_path.exists():
            report = read_json(report_path) or []
            if isinstance(report, list):
                quarantine_ids.update(str(item.get("source_id")) for item in report if item.get("source_id"))
    new_orphans = sorted(
        note_id for note_id in notes
        if note_id not in baseline_ids
        and note_id not in quarantine_ids
        and not reference_ids.get(note_id)
    )
    if new_orphans:
        errors.append("new note files are never referenced by a chapter: " + ", ".join(new_orphans[:20]))
        if len(new_orphans) > 20:
            errors.append(f"... and {len(new_orphans) - 20} more new orphan notes")

    # Every local annotation anchor must have at least one id, and an anchor
    # must not repeat an id.  These checks catch duplicate output on reruns
    # even when the duplicate note file happens to overwrite itself.
    for path in chapter_files.values():
        if not path.exists():
            continue
        chapter = read_json(path)
        parser = parse_html(str(chapter.get("html_source") or ""))
        for index, anchor in enumerate(parser.anchors, 1):
            if str(anchor.get("data_type") or "").lower() == "annotation" and not annotation_ids(anchor):
                errors.append(f"{path.relative_to(BASE_DIR)} annotation anchor {index} has no note id")
            ids = annotation_ids(anchor)
            if len(ids) != len(set(ids)):
                errors.append(f"{path.relative_to(BASE_DIR)} annotation anchor {index} repeats a note id")
    return errors, warnings


def tracked_state() -> dict[str, Any]:
    paths = [WORKS_PATH, NOTE_MANIFEST_PATH, BASE_DIR / "data" / "chapters.json"]
    paths.extend(sorted((BASE_DIR / "data" / "chapters").glob("*.json")))
    paths.extend(sorted((BASE_DIR / "data" / "notes").glob("*.json")))
    generated_root = BASE_DIR / "data" / "works" / "ulysses"
    if generated_root.exists():
        paths.extend(sorted(path for path in generated_root.rglob("*") if path.is_file()))
    return {
        "schema": 1,
        "files": {
            str(path.relative_to(BASE_DIR)): file_digest(path)
            for path in paths
            if path.exists()
        },
    }


def check_state(path: Path) -> list[str]:
    expected = read_json(path)
    actual = tracked_state()
    errors: list[str] = []
    expected_files = expected.get("files") or {}
    actual_files = actual.get("files") or {}
    for name in sorted(set(expected_files) - set(actual_files)):
        errors.append(f"idempotence state file removed: {name}")
    for name in sorted(set(actual_files) - set(expected_files)):
        errors.append(f"idempotence state file added: {name}")
    for name in sorted(set(expected_files) & set(actual_files)):
        if expected_files[name] != actual_files[name]:
            errors.append(f"idempotence state file changed: {name}")
    return errors


def run(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline)
    if not baseline_path.is_absolute():
        baseline_path = BASE_DIR / baseline_path

    if args.write_baseline:
        save_json(baseline_path, build_baseline())
        print(f"Wrote Ulysses baseline: {baseline_path.relative_to(BASE_DIR)}")
        return 0

    if args.write_state:
        save_json(Path(args.write_state), tracked_state())
        print(f"Wrote idempotence state: {args.write_state}")
        return 0

    errors: list[str] = []
    warnings: list[str] = []
    try:
        baseline = load_baseline(baseline_path)
    except Exception as exc:
        print(f"Cannot load Ulysses baseline {baseline_path}: {exc}", file=sys.stderr)
        return 1
    errors.extend(check_preservation(baseline))
    integrity_errors, integrity_warnings = check_integrity(baseline)
    errors.extend(integrity_errors)
    warnings.extend(integrity_warnings)
    overlay_errors, overlay_warnings = overlay_integrity(baseline)
    errors.extend(overlay_errors)
    warnings.extend(overlay_warnings)
    if args.compare_state:
        errors.extend(check_state(Path(args.compare_state)))
    if warnings and not args.quiet:
        for warning in warnings:
            print("warning:", warning)
    if errors:
        print(f"Ulysses import validation failed: {len(errors)} error(s)", file=sys.stderr)
        for error in errors:
            print("error:", error, file=sys.stderr)
        return 1
    print(
        "Ulysses import validation passed"
        f" ({len(baseline.get('note_files') or {})} baseline notes;"
        f" {len(warnings)} warning(s))"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE), help="baseline JSON path")
    parser.add_argument("--write-baseline", action="store_true", help="capture the current pre-import baseline")
    parser.add_argument("--write-state", help="write a full import state snapshot for an idempotence comparison")
    parser.add_argument("--compare-state", help="compare the current state with a previous --write-state snapshot")
    parser.add_argument("--quiet", action="store_true", help="suppress warnings")
    args = parser.parse_args()
    selected = sum(bool(value) for value in (args.write_baseline, args.write_state, args.compare_state))
    if selected > 1:
        parser.error("--write-baseline, --write-state, and --compare-state are mutually exclusive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
