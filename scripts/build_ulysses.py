#!/usr/bin/env python3
"""Build the Genius-enriched Ulysses reader data.

The original Joyce Project mirror remains the canonical input.  This builder
copies its notes/media and writes a work-scoped output containing the same
chapter text and legacy annotation links plus safely matched Genius links.
Unmatched or ambiguous Genius anchors remain in the note data and reports but
are not injected into the reader.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import html
import json
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
LEGACY_CHAPTER_DIR = BASE_DIR / "data" / "chapters"
LEGACY_NOTE_DIR = BASE_DIR / "data" / "notes"
LEGACY_MEDIA_DIR = BASE_DIR / "data" / "media"
LEGACY_NOTE_MANIFEST = BASE_DIR / "data" / "notes.json"
ULYSSES_DIR = BASE_DIR / "data" / "works" / "ulysses"
CHAPTER_DIR = ULYSSES_DIR / "chapters"
NOTE_DIR = ULYSSES_DIR / "notes"
MEDIA_DIR = ULYSSES_DIR / "media"
NORMALIZED_DIR = ULYSSES_DIR / "normalized" / "annotations"
REPORT_DIR = ULYSSES_DIR / "manifests"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_portrait as matching
import import_ulysses_genius


def load_json(path: Path, fallback: object = None) -> object:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key.lower(): value or "" for key, value in attrs}


class LegacyHTMLParser(HTMLParser):
    """Keep the legacy HTML tags while recording old annotation spans."""

    BLOCKS = {"p", "h1", "h2", "h3", "h4", "blockquote", "pre", "li"}
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple] = []
        self.anchors: list[dict] = []
        self.anchor_stack: list[dict] = []
        self.next_anchor_index = 0
        self.position = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        raw = self.get_starttag_text() or self._render_start(tag, attrs)
        values = attrs_dict(attrs)
        if tag == "a" and values.get("data-type") == "annotation":
            ids = [item for item in re.split(r"[,\s]+", values.get("data-notes", "")) if item]
            href = values.get("href", "")
            if href and href not in ids:
                ids.insert(0, href)
            anchor = {
                "index": self.next_anchor_index,
                "start": self.position,
                "ids": ids,
                "href": href,
                "color": values.get("data-color") or "7AA8FF",
                "raw_start": raw,
                "attrs": attrs,
            }
            self.next_anchor_index += 1
            self.anchor_stack.append(anchor)
            self.events.append(("anchor_start", anchor))
            return
        self.events.append(("raw", raw))
        if tag == "br":
            self.position += 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        raw = self.get_starttag_text() or self._render_start(tag, attrs)
        self.events.append(("raw", raw))
        if tag == "br":
            self.position += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self.anchor_stack:
            anchor = self.anchor_stack.pop()
            anchor["end"] = self.position
            if anchor["ids"] and anchor["start"] < anchor["end"]:
                self.anchors.append(anchor)
            self.events.append(("anchor_end", anchor))
            return
        self.events.append(("raw", f"</{tag}>"))
        if tag in self.BLOCKS:
            self.position += 2

    def handle_data(self, data: str) -> None:
        anchor_index = self.anchor_stack[-1]["index"] if self.anchor_stack else None
        self.events.append(("text", data, self.position, anchor_index))
        self.position += len(data)

    @staticmethod
    def _render_start(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        rendered = []
        for key, value in attrs:
            rendered.append(key if value is None else f'{key}="{html.escape(value, quote=True)}"')
        return f"<{tag}" + (" " + " ".join(rendered) if rendered else "") + ">"


def parse_legacy(source: str) -> tuple[LegacyHTMLParser, str]:
    parser = LegacyHTMLParser()
    parser.feed(source)
    parser.close()
    return parser, matching.plain_text(source)


def all_occurrences(haystack: str, needle: str) -> list[int]:
    return matching.find_all(haystack, needle)


class TextForms:
    def __init__(self, plain: str) -> None:
        self.plain = plain
        self.normalized, self.normalized_map = matching.normalized_chars(
            plain, with_map=True
        )
        self.compact, self.compact_map = matching.compact_normalized(
            plain, with_map=True
        )
        self.punctuation, self.punctuation_map = matching.punctuation_normalized(
            plain, with_map=True
        )

    @staticmethod
    def mapped_ranges(
        positions: list[int], target_len: int, mapping: list[int]
    ) -> list[tuple[int, int]]:
        result = []
        for position in positions:
            if position < 0 or position + target_len > len(mapping) or not target_len:
                continue
            result.append(
                (mapping[position], mapping[position + target_len - 1] + 1)
            )
        return result

    def occurrences(self, target: str) -> tuple[list[tuple[int, int]], str]:
        target = html.unescape(str(target or "")).strip()
        direct = all_occurrences(self.plain, target) if target else []
        if direct:
            return [(start, start + len(target)) for start in direct], "exact"
        target_norm = matching.normalized_chars(target)
        if not target_norm:
            return [], "none"
        ranges = self.mapped_ranges(
            all_occurrences(self.normalized, target_norm),
            len(target_norm),
            self.normalized_map,
        )
        if ranges:
            return ranges, "normalized-exact"
        target_compact = matching.compact_normalized(target)
        ranges = self.mapped_ranges(
            all_occurrences(self.compact, target_compact),
            len(target_compact),
            self.compact_map,
        )
        if ranges:
            return ranges, "compact-exact"
        target_punctuation = matching.punctuation_normalized(target)
        ranges = self.mapped_ranges(
            all_occurrences(self.punctuation, target_punctuation),
            len(target_punctuation),
            self.punctuation_map,
        )
        return ranges, "punctuation-exact" if ranges else "none"

    def map_normalized_range(self, start: int, end: int) -> tuple[int, int] | None:
        if start < 0 or end <= start or start >= len(self.normalized_map):
            return None
        last = min(end, len(self.normalized_map)) - 1
        return self.normalized_map[start], self.normalized_map[last] + 1


def resolve_note(note: dict, forms: TextForms) -> tuple[dict | None, dict | None, dict | None]:
    anchor = note.get("anchor") or {}
    target = str(anchor.get("text") or "").strip()
    candidates, stage = forms.occurrences(target)
    note["_match_stage"] = stage
    chosen, confidence, method = matching.choose_contextual(
        forms.plain, candidates, note, None
    )
    if chosen is not None:
        return {
            "start": chosen[0],
            "end": chosen[1],
            "method": method,
            "confidence": confidence,
        }, None, None
    if candidates and method == "ambiguous":
        return None, {
            "source_id": note["id"],
            "referent": target,
            "candidates": [
                {
                    "text": forms.plain[start:end],
                    "start": start,
                    "end": end,
                    "context_score": matching.context_score(
                        forms.plain,
                        start,
                        end,
                        str(anchor.get("prefix") or ""),
                        str(anchor.get("suffix") or ""),
                    ),
                }
                for start, end in candidates[:12]
            ],
        }, None

    fuzzy = matching.fuzzy_candidates(forms.plain, target)
    if fuzzy:
        best = fuzzy[0]
        second = fuzzy[1] if len(fuzzy) > 1 else None
        normalized_range = forms.map_normalized_range(best[1], best[2])
        if normalized_range is None:
            return None, None, {
                "source_id": note["id"],
                "referent": target,
                "best_candidate": "",
                "score": round(best[0], 4),
                "reason": "could not map fuzzy candidate to canonical text",
            }
        context = matching.context_score(
            forms.plain,
            normalized_range[0],
            normalized_range[1],
            str(anchor.get("prefix") or ""),
            str(anchor.get("suffix") or ""),
        )
        unique = second is None or best[0] - second[0] >= 0.002 or context >= 0.55
        close_enough = best[0] >= 0.97 and unique
        if 0.90 <= best[0] < 0.97:
            close_enough = unique and (context >= 0.35 or len(target) >= 24)
        if close_enough and unique:
            return {
                "start": normalized_range[0],
                "end": normalized_range[1],
                "method": "fuzzy",
                "confidence": best[0],
            }, None, None
        return None, None, {
            "source_id": note["id"],
            "referent": target,
            "best_candidate": forms.plain[normalized_range[0] : normalized_range[1]],
            "score": round(best[0], 4),
            "reason": "fuzzy score below acceptance policy",
        }
    return None, None, {
        "source_id": note["id"],
        "referent": target,
        "best_candidate": "",
        "score": 0.0,
        "reason": "no candidate in chapter",
    }


def old_ranges(parser: LegacyHTMLParser) -> list[dict]:
    ranges = []
    for anchor in parser.anchors:
        for note_id in anchor["ids"]:
            ranges.append(
                {
                    "note_id": note_id,
                    "start": anchor["start"],
                    "end": anchor["end"],
                    "text": "",
                    "legacy": True,
                    "anchor": anchor,
                }
            )
    return ranges


def merge_legacy_tag(anchor: dict) -> str:
    """Keep a legacy opening tag and append Genius IDs without splitting it."""
    extra_ids = list(dict.fromkeys(anchor.get("genius_ids") or []))
    if not extra_ids:
        return str(anchor["raw_start"])
    all_ids = list(dict.fromkeys(list(anchor.get("ids") or []) + extra_ids))
    raw = str(anchor["raw_start"])
    pattern = re.compile(
        r"(\sdata-notes\s*=\s*)(['\"])(.*?)(\2)", flags=re.IGNORECASE
    )
    if pattern.search(raw):
        return pattern.sub(
            lambda match: (
                match.group(1)
                + match.group(2)
                + html.escape(",".join(all_ids), quote=True)
                + match.group(4)
            ),
            raw,
            count=1,
        )
    close = "/>" if raw.rstrip().endswith("/>") else ">"
    return raw[: -len(close)] + f' data-notes="{html.escape(",".join(all_ids), quote=True)}"' + close


def new_anchor_tag(ids: list[str], note_by_id: dict[str, dict]) -> str:
    ids = list(dict.fromkeys(ids))
    first = note_by_id.get(ids[0], {})
    color = str(first.get("color") or "F59627")
    return (
        f'href="{html.escape(ids[0], quote=True)}" '
        f'data-notes="{html.escape(",".join(ids), quote=True)}" '
        f'data-color="{html.escape(color, quote=True)}" '
        'data-type="annotation"'
    ).join(("<a ", ">"))


def inject_annotations(
    source: str,
    parser: LegacyHTMLParser,
    ranges: list[dict],
    note_by_id: dict[str, dict],
) -> str:
    """Inject links around text while retaining every legacy link target."""
    legacy = [item for item in ranges if item.get("legacy")]
    new_ranges = [item for item in ranges if not item.get("legacy")]
    # A Genius span which touches a legacy anchor is represented by that
    # existing anchor for its overlapping text. This preserves the legacy
    # anchor's contiguous visible span and avoids invalid nested <a> tags.
    for old in legacy:
        anchor = old["anchor"]
        anchor["genius_ids"] = list(
            dict.fromkeys(
                item["note_id"]
                for item in new_ranges
                if old["start"] < item["end"] and item["start"] < old["end"]
            )
        )
    old_by_index = {
        item["anchor"]["index"]: item["anchor"] for item in legacy
    }
    output: list[str] = []
    for event in parser.events:
        if event[0] == "anchor_start":
            output.append(merge_legacy_tag(event[1]))
            continue
        if event[0] == "anchor_end":
            output.append("</a>")
            continue
        if event[0] == "raw":
            output.append(event[1])
            continue
        _, data, start, anchor_index = event
        if not data:
            continue
        # The outer legacy anchor remains intact. Its opening tag already
        # carries any overlapping Genius IDs.
        if anchor_index is not None and anchor_index in old_by_index:
            output.append(html.escape(data, quote=False))
            continue
        cuts = {0, len(data)}
        for item in new_ranges:
            local_start = max(0, item["start"] - start)
            local_end = min(len(data), item["end"] - start)
            if local_start < local_end:
                cuts.add(local_start)
                cuts.add(local_end)
        sorted_cuts = sorted(cuts)
        for left, right in zip(sorted_cuts, sorted_cuts[1:]):
            if left == right:
                continue
            position = start + left
            active = [
                item
                for item in new_ranges
                if item["start"] <= position < item["end"]
            ]
            active.sort(
                key=lambda item: (
                    item["note_id"],
                )
            )
            if not active:
                output.append(html.escape(data[left:right], quote=False))
                continue
            ids = list(dict.fromkeys(item["note_id"] for item in active))
            output.append(new_anchor_tag(ids, note_by_id))
            output.append(html.escape(data[left:right], quote=False))
            output.append("</a>")
    return "".join(output)


def copy_legacy_notes_and_media() -> list[dict]:
    NOTE_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    records = load_json(LEGACY_NOTE_MANIFEST, []) or []
    if not isinstance(records, list):
        raise RuntimeError("data/notes.json must contain a list")
    legacy_ids: list[str] = []
    for record in records:
        note_id = str(record.get("id") or "")
        if not note_id:
            continue
        source = LEGACY_NOTE_DIR / f"{note_id}.json"
        if not source.exists():
            raise RuntimeError(f"legacy note file is missing: {source}")
        shutil.copyfile(source, NOTE_DIR / source.name)
        legacy_ids.append(note_id)
    for source in LEGACY_MEDIA_DIR.glob("*.json"):
        shutil.copyfile(source, MEDIA_DIR / source.name)
    return legacy_ids


def load_legacy_notes(legacy_ids: list[str]) -> dict[str, dict]:
    notes = {}
    for note_id in legacy_ids:
        note = load_json(NOTE_DIR / f"{note_id}.json", {}) or {}
        if isinstance(note, dict):
            notes[note_id] = note
    return notes


def write_notes(notes: list[dict], legacy_ids: list[str]) -> None:
    # Remove only generated Ulysses Genius files from an earlier build. The
    # copied legacy note files are independently preserved above.
    for path in NOTE_DIR.glob("ulysses-genius-*.json"):
        path.unlink()
    index: list[dict] = []
    for note_id in legacy_ids:
        note = load_json(NOTE_DIR / f"{note_id}.json", {}) or {}
        index.append(
            {
                "id": note_id,
                "work_id": "ulysses",
                "chapter_id": note.get("chapter_id"),
                "title": note.get("title"),
                "source": (note.get("source") or {}).get("type", "local"),
            }
        )
    for note in sorted(notes, key=lambda item: item["id"]):
        clean = dict(note)
        clean.pop("_match_stage", None)
        (NOTE_DIR / f"{clean['id']}.json").write_text(
            json.dumps(clean, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        index.append(
            {
                "id": clean["id"],
                "work_id": clean.get("work_id"),
                "chapter_id": clean.get("chapter_id"),
                "title": clean.get("title"),
                "source": (clean.get("source") or {}).get("type"),
            }
        )
    (NOTE_DIR / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def build(*, fetch: bool = True) -> dict:
    notes = import_ulysses_genius.import_genius(fetch=fetch)
    legacy_ids = copy_legacy_notes_and_media()
    legacy_notes = load_legacy_notes(legacy_ids)
    note_by_id = {**legacy_notes, **{note["id"]: note for note in notes}}
    if len(note_by_id) != len(legacy_notes) + len(notes):
        raise RuntimeError("legacy and Genius note IDs overlap")

    unmatched: list[dict] = []
    ambiguous: list[dict] = []
    resolved_by_chapter: dict[str, list[dict]] = defaultdict(list)
    chapter_reports: list[dict] = []
    legacy_snapshot: list[dict] = []
    by_chapter: dict[str, list[dict]] = defaultdict(list)
    for note in notes:
        by_chapter[str(note.get("chapter_id"))].append(note)

    manifest = load_json(BASE_DIR / "sources" / "ulysses-genius.json", {}) or {}
    entries = {str(entry["chapter_id"]): entry for entry in manifest.get("chapters", [])}
    chapter_descriptors = load_json(BASE_DIR / "data" / "chapters.json", []) or []
    for descriptor in chapter_descriptors:
        chapter_id = str(descriptor["id"])
        base_path = LEGACY_CHAPTER_DIR / f"{chapter_id}.json"
        base = load_json(base_path, {}) or {}
        source = str(base.get("html_source") or "")
        if not source:
            raise RuntimeError(f"legacy chapter has no html_source: {base_path}")
        parser, plain = parse_legacy(source)
        forms = TextForms(plain)
        old = old_ranges(parser)
        for item in old:
            item["text"] = plain[item["start"] : item["end"]]
        for note in by_chapter.get(chapter_id, []):
            resolved, ambiguous_record, unmatched_record = resolve_note(note, forms)
            if resolved is None:
                note["provenance"] = {
                    "match_method": "ambiguous" if ambiguous_record else "unmatched",
                    "confidence": (
                        max(
                            (
                                candidate.get("context_score", 0.0)
                                for candidate in (ambiguous_record or {}).get("candidates", [])
                            ),
                            default=0.0,
                        )
                        if ambiguous_record
                        else (unmatched_record or {}).get("score", 0.0)
                    ),
                }
                note["quarantine"] = True
                if ambiguous_record:
                    ambiguous.append(ambiguous_record)
                if unmatched_record:
                    unmatched.append(unmatched_record)
                continue
            start, end = resolved["start"], resolved["end"]
            matched_text = plain[start:end]
            note["anchor"]["start"] = start
            note["anchor"]["end"] = end
            note["anchor"]["canonical_text"] = matched_text
            note["anchor"]["prefix"] = plain[max(0, start - 180) : start]
            note["anchor"]["suffix"] = plain[end : end + 180]
            note["provenance"] = {
                "match_method": resolved["method"],
                "confidence": round(float(resolved["confidence"]), 4),
            }
            resolved_by_chapter[chapter_id].append(
                {
                    "note_id": note["id"],
                    "start": start,
                    "end": end,
                    "text": matched_text,
                }
            )

        all_ranges = old + resolved_by_chapter.get(chapter_id, [])
        generated_source = inject_annotations(source, parser, all_ranges, note_by_id)
        if matching.plain_text(generated_source) != plain:
            raise RuntimeError(f"generated chapter changed visible text: {chapter_id}")
        legacy_snapshot.append(
            {
                "chapter_id": chapter_id,
                "html_sha256": sha256_text(source),
                "visible_text_sha256": sha256_text(plain),
                "legacy_anchor_count": len(parser.anchors),
                "legacy_reference_count": len(old),
            }
        )
        final = dict(base)
        final["work_id"] = "ulysses"
        final["html_source"] = generated_source
        final["annotation_ranges"] = [
            dict(item) for item in resolved_by_chapter.get(chapter_id, [])
        ]
        CHAPTER_DIR.mkdir(parents=True, exist_ok=True)
        (CHAPTER_DIR / f"{chapter_id}.json").write_text(
            json.dumps(final, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        chapter_reports.append(
            {
                "chapter": descriptor.get("number"),
                "chapter_id": chapter_id,
                "title": descriptor.get("title"),
                "genius_discovered": len(by_chapter.get(chapter_id, [])),
                "genius_matched": len(resolved_by_chapter.get(chapter_id, [])),
                "legacy_anchors_preserved": len(parser.anchors),
                "legacy_references_preserved": len(old),
                "source_entry": entries.get(chapter_id),
            }
        )

    write_notes(notes, legacy_ids)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "unmatched.json").write_text(
        json.dumps(unmatched, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (REPORT_DIR / "ambiguous.json").write_text(
        json.dumps(ambiguous, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    matched_count = sum(len(value) for value in resolved_by_chapter.values())
    summary = {
        "work": "ulysses",
        "chapters": len(chapter_reports),
        "legacy_notes_copied": len(legacy_ids),
        "legacy_media_copied": len(list(MEDIA_DIR.glob("*.json"))),
        "genius_notes_discovered": len(notes),
        "genius_notes_matched": matched_count,
        "genius_notes_quarantined": len(unmatched) + len(ambiguous),
        "ambiguous_matches": len(ambiguous),
        "unmatched_notes": len(unmatched),
        "legacy_chapters_untouched": True,
        "legacy_notes_untouched": True,
        "legacy_snapshot": legacy_snapshot,
        "chapters_report": chapter_reports,
    }
    (REPORT_DIR / "ulysses-build.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (REPORT_DIR / "overlay-manifest.json").write_text(
        json.dumps(
            {
                "work": "ulysses",
                "base_chapters": "data/chapters",
                "base_notes": "data/notes",
                "generated_chapters": "data/works/ulysses/chapters",
                "generated_notes": "data/works/ulysses/notes",
                "source_manifest": "sources/ulysses-genius.json",
                "legacy_snapshot": legacy_snapshot,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="Fetch missing Genius caches")
    parser.add_argument("--no-fetch", action="store_true", help="Use only cached Genius data")
    parser.add_argument("--offline", action="store_true", help="Alias for --no-fetch")
    parser.add_argument("--report", action="store_true", help="Print report paths")
    args = parser.parse_args(argv)
    if args.fetch and (args.no_fetch or args.offline):
        parser.error("--fetch cannot be combined with --no-fetch/--offline")
    try:
        summary = build(fetch=not (args.no_fetch or args.offline))
        print("\nUlysses Genius build complete")
        for key in (
            "chapters",
            "legacy_notes_copied",
            "genius_notes_discovered",
            "genius_notes_matched",
            "genius_notes_quarantined",
            "ambiguous_matches",
            "unmatched_notes",
        ):
            print(f"{key.replace('_', ' ').capitalize()}: {summary[key]}")
        if args.report:
            print(f"Reports: {REPORT_DIR}")
        return 0
    except Exception as exc:  # pragma: no cover - command-line diagnostics
        print(f"Ulysses build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
