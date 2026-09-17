#!/usr/bin/env python3
"""Build the complete local Portrait reader data set.

Build layers:
  raw source -> normalized note candidates -> chapter HTML/note JSON

The rendered chapter HTML contains only the reader's normalized annotation
links. Source-specific parsing is kept in the import modules.
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import html
import json
import re
import sys
import unicodedata
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


BASE_DIR = Path(__file__).resolve().parent.parent
PORTRAIT_DIR = BASE_DIR / "data" / "works" / "portrait"
CHAPTER_DIR = PORTRAIT_DIR / "chapters"
NOTE_DIR = PORTRAIT_DIR / "notes"
NORMALIZED_DIR = PORTRAIT_DIR / "normalized" / "annotations"
REPORT_DIR = PORTRAIT_DIR / "manifests"
OVERRIDE_PATH = PORTRAIT_DIR / "overrides.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_portrait_genius
import import_portrait_tei
import import_portrait_text
import validate as validate_repository


SOURCE_COLORS = {
    "genius": "F59627",
    "person": "307EE3",
    "place": "40B324",
    "date": "9C632A",
    "reference": "AB59C2",
    "quotation": "CF2929",
    "bibliography": "F59627",
    "song": "CF2929",
    "prayer": "AB59C2",
    "chant": "CF2929",
    "letter": "40B324",
    "neologism": "9C632A",
    "la": "AB59C2",
    "fr": "AB59C2",
    "ita": "AB59C2",
}


def load_json(path: Path, fallback: object) -> object:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def normalized_chars(value: str, *, with_map: bool = False):
    """Normalize quote/space/dash variants without changing displayed text."""
    out: list[str] = []
    mapping: list[int] = []
    quote_map = {
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "«": '"',
        "»": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
    }
    hyphen_chars = set("‐‑‒−")
    space_dash_chars = set("–—―")
    for source_index, original in enumerate(value):
        decomposed = unicodedata.normalize("NFKC", original)
        for char in decomposed:
            if char in {"\u00ad", "\u200b", "\u200c", "\u200d", "\ufeff"}:
                continue
            if char.isspace():
                replacement = " "
            elif char == "…":
                replacement = "..."
            elif char in {"œ", "Œ"}:
                replacement = "oe"
            elif char in {"æ", "Æ"}:
                replacement = "ae"
            elif char in space_dash_chars:
                replacement = " "
            elif char in hyphen_chars or char == "-":
                # Gutenberg and the TEI differ on compounds such as
                # ``fiveshilling`` / ``five-shilling`` and on soft line
                # breaks. Treat a word-internal hyphen as optional for
                # matching; the displayed text remains untouched.
                replacement = ""
            else:
                replacement = quote_map.get(char, char)
            for output_char in replacement:
                if output_char == " " and out and out[-1] == " ":
                    continue
                out.append(output_char)
                mapping.append(source_index)
    while out and out[0] == " ":
        out.pop(0)
        mapping.pop(0)
    while out and out[-1] == " ":
        out.pop()
        mapping.pop()
    result = "".join(out).casefold()
    return (result, mapping) if with_map else result


def compact_normalized(value: str, *, with_map: bool = False):
    """Secondary comparison form for compounds split differently by sources."""
    normalized, mapping = normalized_chars(value, with_map=True)
    compact = [(char, mapping[index]) for index, char in enumerate(normalized)
               if not char.isspace() and char != "-"]
    text = "".join(char for char, _ in compact)
    if with_map:
        return text, [source_index for _, source_index in compact]
    return text


def punctuation_normalized(value: str, *, with_map: bool = False):
    """Comparison form for source punctuation that is editorially optional."""
    normalized, mapping = normalized_chars(value, with_map=True)
    kept = [
        (char, mapping[index])
        for index, char in enumerate(normalized)
        if not unicodedata.category(char).startswith("P")
    ]
    text = "".join(char for char, _ in kept)
    if with_map:
        return text, [source_index for _, source_index in kept]
    return text


def find_all(text: str, needle: str) -> list[int]:
    if not needle:
        return []
    result: list[int] = []
    start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return result
        result.append(found)
        start = found + 1


class PlainTextParser(HTMLParser):
    BLOCKS = {"p", "h1", "h2", "h3", "h4", "blockquote", "pre", "li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCKS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def result(self) -> str:
        return "".join(self.parts)


def plain_text(html_source: str) -> str:
    parser = PlainTextParser()
    parser.feed(html_source)
    parser.close()
    return parser.result()


def occurrences_for_target(
    chapter_plain: str, target: str
) -> tuple[list[tuple[int, int]], str, list[int]]:
    target = html.unescape(str(target or "")).strip()
    direct = find_all(chapter_plain, target) if target else []
    if direct:
        return [(start, start + len(target)) for start in direct], "exact", []
    target_norm = normalized_chars(target)
    haystack_norm, haystack_map = normalized_chars(chapter_plain, with_map=True)
    if not target_norm:
        return [], "none", []
    positions = find_all(haystack_norm, target_norm)
    ranges: list[tuple[int, int]] = []
    for position in positions:
        source_start = haystack_map[position]
        source_end = haystack_map[position + len(target_norm) - 1] + 1
        ranges.append((source_start, source_end))
    if ranges:
        return ranges, "normalized-exact", haystack_map

    # Open Editions and Gutenberg sometimes render the same compound as
    # ``studyhall`` versus ``study hall`` or ``half-door`` versus
    # ``halfdoor``. Keep this as a fallback so ordinary phrase matching does
    # not ignore meaningful spaces.
    compact_haystack, compact_map = compact_normalized(
        chapter_plain, with_map=True
    )
    compact_target = compact_normalized(target)
    compact_positions = find_all(compact_haystack, compact_target)
    for position in compact_positions:
        source_start = compact_map[position]
        source_end = compact_map[position + len(compact_target) - 1] + 1
        ranges.append((source_start, source_end))
    if ranges:
        return ranges, "compact-exact", compact_map

    # TEI often preserves a citation's punctuation while another edition
    # drops it (for example ``Ite, missa est`` / ``Ite missa est``). This is
    # still an exact semantic span, but punctuation is excluded from the
    # comparison and the displayed Gutenberg text is left untouched.
    punctuation_haystack, punctuation_map = punctuation_normalized(
        chapter_plain, with_map=True
    )
    punctuation_target = punctuation_normalized(target)
    punctuation_positions = find_all(punctuation_haystack, punctuation_target)
    for position in punctuation_positions:
        source_start = punctuation_map[position]
        source_end = punctuation_map[position + len(punctuation_target) - 1] + 1
        ranges.append((source_start, source_end))
    return ranges, "punctuation-exact" if ranges else "none", punctuation_map


def context_score(
    chapter_plain: str,
    start: int,
    end: int,
    prefix: str,
    suffix: str,
) -> float:
    if not prefix and not suffix:
        return 0.0
    score_parts: list[float] = []
    if prefix:
        expected = normalized_chars(prefix[-140:])
        actual = normalized_chars(chapter_plain[max(0, start - 140):start])
        score_parts.append(
            difflib.SequenceMatcher(None, expected, actual, autojunk=False).ratio()
        )
    if suffix:
        expected = normalized_chars(suffix[:140])
        actual = normalized_chars(chapter_plain[end:end + 140])
        score_parts.append(
            difflib.SequenceMatcher(None, expected, actual, autojunk=False).ratio()
        )
    return sum(score_parts) / len(score_parts) if score_parts else 0.0


def choose_contextual(
    chapter_plain: str,
    candidates: list[tuple[int, int]],
    note: dict,
    occurrence_hint: int | None = None,
) -> tuple[tuple[int, int] | None, float, str]:
    if not candidates:
        return None, 0.0, "unmatched"
    override = note.get("_override") or {}
    occurrence = override.get("occurrence") or occurrence_hint
    if occurrence is not None:
        try:
            index = int(occurrence) - 1
            if 0 <= index < len(candidates):
                return candidates[index], 1.0, "occurrence"
        except (TypeError, ValueError):
            pass
    if len(candidates) == 1:
        stage = note.get("_match_stage")
        method = stage if stage in {
            "exact", "normalized-exact", "compact-exact", "punctuation-exact"
        } else "normalized-exact"
        return candidates[0], 1.0 if method == "exact" else 0.99, method
    prefix = str((note.get("anchor") or {}).get("prefix") or "")
    suffix = str((note.get("anchor") or {}).get("suffix") or "")
    scored = [
        (
            context_score(chapter_plain, start, end, prefix, suffix),
            start,
            end,
        )
        for start, end in candidates
    ]
    scored.sort(reverse=True)
    if scored and scored[0][0] >= 0.55 and (
        len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08
    ):
        return (scored[0][1], scored[0][2]), min(0.98, scored[0][0]), "contextual"
    return None, scored[0][0] if scored else 0.0, "ambiguous"


def fuzzy_candidates(chapter_plain: str, target: str) -> list[tuple[float, int, int]]:
    haystack = normalized_chars(chapter_plain)
    target = normalized_chars(target)
    if len(target) < 4:
        return []
    words = list(re.finditer(r"\S+", haystack))
    target_words = target.split()
    if not words or not target_words:
        return []
    starts = [match.start() for match in words]
    first_token = target_words[0]
    prefix = " ".join(target_words[: min(3, len(target_words))])
    candidate_starts: list[int] = []
    for match in re.finditer(re.escape(prefix), haystack):
        index = bisect.bisect_left(starts, match.start())
        if index < len(words) and words[index].start() == match.start():
            candidate_starts.append(index)
    if not candidate_starts and len(target_words) > 1:
        # If the first word differs by an OCR/editorial correction (for
        # example Impleta/Inpleta), search the next two-word windows before
        # falling back to a bounded sample of the chapter.
        for offset in range(1, min(3, len(target_words) - 1)):
            pair = " ".join(target_words[offset:offset + 2])
            for match in re.finditer(re.escape(pair), haystack):
                index = bisect.bisect_left(starts, match.start())
                if index < len(words) and words[index].start() == match.start():
                    candidate_starts.append(max(0, index - offset))
            if candidate_starts:
                break
    if not candidate_starts:
        candidate_starts = [
            index for index, word in enumerate(words)
            if word.group() == first_token
        ]
    if not candidate_starts:
        # A spelling change can alter the first token. Keep the fallback
        # bounded; the exact and normalized stages have already failed here.
        step = max(1, len(words) // 600)
        candidate_starts = list(range(0, len(words), step))
    elif len(candidate_starts) > 250:
        candidate_starts = candidate_starts[:250]
    results: dict[tuple[int, int], float] = {}
    target_length = len(target)
    for word_index in candidate_starts:
        word = words[word_index]
        expected_end = word.start() + target_length
        approximate = bisect.bisect_left(starts, expected_end, lo=word_index)
        first_end = max(word_index + 1, approximate - 2)
        last_end = min(len(words), approximate + 3)
        for end_index in range(first_end, last_end):
            end = words[end_index - 1].end()
            candidate = haystack[word.start():end]
            if len(candidate) < target_length * 0.45 or len(candidate) > target_length * 1.8:
                continue
            matcher = difflib.SequenceMatcher(None, target, candidate)
            if matcher.quick_ratio() < 0.72:
                continue
            score = matcher.ratio()
            key = (word.start(), end)
            results[key] = max(results.get(key, 0.0), score)
    return sorted(
        [(score, start, end) for (start, end), score in results.items()],
        reverse=True,
    )[:8]


def map_normalized_range(
    chapter_plain: str, normalized_start: int, normalized_end: int
) -> tuple[int, int] | None:
    _, mapping = normalized_chars(chapter_plain, with_map=True)
    if not mapping or normalized_start < 0 or normalized_end <= normalized_start:
        return None
    if normalized_start >= len(mapping):
        return None
    last = min(normalized_end, len(mapping)) - 1
    return mapping[normalized_start], mapping[last] + 1


def infer_tei_occurrences(notes: list[dict]) -> dict[str, int]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    result: dict[str, int] = {}
    for note in notes:
        if (note.get("source") or {}).get("type") != "tei":
            continue
        key = (
            str(note.get("chapter_id")),
            normalized_chars(str((note.get("anchor") or {}).get("text") or "")),
        )
        counts[key] += 1
        result[note["id"]] = counts[key]
    return result


def resolve_note(
    note: dict,
    chapter_plain: str,
    occurrence_hint: int | None,
) -> tuple[dict | None, dict | None, dict | None]:
    anchor = note.get("anchor") or {}
    target = str(anchor.get("text") or "").strip()
    override = note.get("_override") or {}
    if "start" in override and "end" in override:
        start, end = int(override["start"]), int(override["end"])
        if 0 <= start < end <= len(chapter_plain):
            return (
                {
                    "start": start,
                    "end": end,
                    "method": "override",
                    "confidence": 1.0,
                },
                None,
                None,
            )
        return None, None, {
            "source_id": note["id"],
            "referent": target,
            "best_candidate": "",
            "score": 0.0,
            "reason": "override offsets are outside the chapter",
        }

    candidates, stage, _ = occurrences_for_target(chapter_plain, target)
    note["_match_stage"] = stage
    chosen, confidence, method = choose_contextual(
        chapter_plain, candidates, note, occurrence_hint
    )
    if chosen is not None:
        return (
            {
                "start": chosen[0],
                "end": chosen[1],
                "method": method,
                "confidence": confidence,
            },
            None,
            None,
        )
    if candidates and method == "ambiguous":
        return None, {
            "source_id": note["id"],
            "referent": target,
            "candidates": [
                {
                    "text": chapter_plain[start:end],
                    "start": start,
                    "end": end,
                    "context_score": context_score(
                        chapter_plain,
                        start,
                        end,
                        str(anchor.get("prefix") or ""),
                        str(anchor.get("suffix") or ""),
                    ),
                }
                for start, end in candidates[:12]
            ],
        }, None

    fuzzy = fuzzy_candidates(chapter_plain, target)
    if fuzzy:
        best = fuzzy[0]
        second = fuzzy[1] if len(fuzzy) > 1 else None
        # Fuzzy candidates are offsets in the normalized chapter string; map
        # them back to the displayed canonical string before injecting links.
        normalized_range = map_normalized_range(
            chapter_plain, best[1], best[2]
        )
        if normalized_range is None:
            return None, None, {
                "source_id": note["id"],
                "referent": target,
                "best_candidate": "",
                "score": round(best[0], 4),
                "reason": "could not map fuzzy candidate to canonical text",
            }
        context = context_score(
            chapter_plain,
            normalized_range[0],
            normalized_range[1],
            str(anchor.get("prefix") or ""),
            str(anchor.get("suffix") or ""),
        )
        # Follow the documented policy: very high unique matches are safe;
        # the 0.90–0.97 band also needs contextual or substantial-span
        # evidence. A tiny score gap is not treated as uniqueness unless the
        # source context agrees.
        unique = second is None or best[0] - second[0] >= 0.002 or context >= 0.55
        close_enough = best[0] >= 0.97 and unique
        if 0.90 <= best[0] < 0.97:
            close_enough = unique and (context >= 0.35 or len(target) >= 24)
        if close_enough and unique:
            return (
                {
                    "start": normalized_range[0],
                    "end": normalized_range[1],
                    "method": "fuzzy",
                    "confidence": best[0],
                },
                None,
                None,
            )
        best_text = chapter_plain[normalized_range[0]:normalized_range[1]]
        return None, None, {
            "source_id": note["id"],
            "referent": target,
            "best_candidate": best_text,
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


class AnnotationHTMLParser(HTMLParser):
    BLOCKS = {"p", "h1", "h2", "h3", "h4", "blockquote", "pre", "li"}
    VOID = {"br", "hr", "img"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple] = []
        self.position = 0

    @staticmethod
    def start_tag(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        rendered: list[str] = []
        for key, value in attrs:
            if key not in {"class", "id"} or value is None:
                continue
            rendered.append(f'{key}="{html.escape(value, quote=True)}"')
        return f"<{tag}" + ((" " + " ".join(rendered)) if rendered else "") + ">"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "a":
            raise ValueError("canonical chapter unexpectedly contains an anchor")
        self.events.append(("tag", self.start_tag(tag, attrs)))
        if tag == "br":
            self.position += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        self.events.append(("tag", f"</{tag}>"))
        if tag in self.BLOCKS:
            self.position += 2

    def handle_data(self, data: str) -> None:
        self.events.append(("text", data, self.position))
        self.position += len(data)


def note_priority(note: dict) -> int:
    source_type = (note.get("source") or {}).get("type")
    return {"genius": 3, "local": 2, "tei": 1}.get(source_type, 0)


def note_color(note: dict) -> str:
    if note.get("color"):
        return str(note["color"])
    source = note.get("source") or {}
    if source.get("type") == "genius":
        return SOURCE_COLORS["genius"]
    tei = note.get("tei") or {}
    attrs = tei.get("attributes") or {}
    return SOURCE_COLORS.get(
        attrs.get("type") or attrs.get("lang") or tei.get("tag"),
        "7AA8FF",
    )


def active_ids(
    ranges: list[dict], notes_by_id: dict[str, dict], position: int
) -> list[str]:
    active = [
        item["note_id"]
        for item in ranges
        if item["start"] <= position < item["end"]
    ]
    active = list(dict.fromkeys(active))
    active.sort(
        key=lambda note_id: (
            -note_priority(notes_by_id[note_id]),
            note_id,
        )
    )
    return active


def inject_annotations(
    chapter_html: str, ranges: list[dict], notes_by_id: dict[str, dict]
) -> str:
    parser = AnnotationHTMLParser()
    parser.feed(chapter_html)
    parser.close()
    output: list[str] = []
    for event in parser.events:
        if event[0] == "tag":
            output.append(event[1])
            continue
        _, data, start = event
        if not data:
            continue
        cuts = {0, len(data)}
        for item in ranges:
            local_start = max(0, item["start"] - start)
            local_end = min(len(data), item["end"] - start)
            if local_start < local_end:
                cuts.add(local_start)
                cuts.add(local_end)
        sorted_cuts = sorted(cuts)
        for left, right in zip(sorted_cuts, sorted_cuts[1:]):
            if left == right:
                continue
            ids = active_ids(ranges, notes_by_id, start + left)
            text = html.escape(data[left:right], quote=False)
            if not ids:
                output.append(text)
                continue
            primary_color = note_color(notes_by_id[ids[0]])
            attributes = (
                f'href="{html.escape(ids[0], quote=True)}" '
                f'data-notes="{html.escape(",".join(ids), quote=True)}" '
                f'data-color="{html.escape(primary_color, quote=True)}" '
                'data-type="annotation"'
            )
            output.append(f"<a {attributes}>{text}</a>")
    return "".join(output)


def overlap_group_count(ranges: list[dict]) -> int:
    groups: list[list[dict]] = []
    for item in sorted(ranges, key=lambda value: (value["start"], value["end"])):
        touching = [group for group in groups if any(
            other["start"] < item["end"] and item["start"] < other["end"]
            for other in group
        )]
        if not touching:
            groups.append([item])
        else:
            merged = [item]
            for group in touching:
                merged.extend(group)
                groups.remove(group)
            groups.append(merged)
    return sum(1 for group in groups if len({item["note_id"] for item in group}) > 1)


def load_normalized_notes() -> list[dict]:
    notes: list[dict] = []
    genius = load_json(NORMALIZED_DIR / "genius_notes.json", {})
    tei = load_json(NORMALIZED_DIR / "tei_annotations.json", {})
    notes.extend((genius or {}).get("notes") or [])
    notes.extend((tei or {}).get("annotations") or [])
    return notes


def clean_generated_notes() -> None:
    NOTE_DIR.mkdir(parents=True, exist_ok=True)
    for path in NOTE_DIR.glob("portrait-*.json"):
        path.unlink()


def write_note_files(notes: list[dict]) -> None:
    clean_generated_notes()
    index = []
    for note in sorted(notes, key=lambda item: item["id"]):
        note.pop("_override", None)
        note.pop("_match_stage", None)
        NOTE_DIR.joinpath(f"{note['id']}.json").write_text(
            json.dumps(note, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        index.append(
            {
                "id": note["id"],
                "work_id": note.get("work_id"),
                "chapter_id": note.get("chapter_id"),
                "title": note.get("title"),
                "source": (note.get("source") or {}).get("type"),
            }
        )
    NOTE_DIR.joinpath("index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def build(*, fetch: bool = True, genius: bool = True, tei: bool = True) -> dict:
    chapters = import_portrait_text.import_text(fetch=fetch)
    if genius:
        import_portrait_genius.import_genius(fetch=fetch)
    if tei:
        import_portrait_tei.import_tei(fetch=fetch)

    notes = load_normalized_notes()
    overrides = load_json(OVERRIDE_PATH, {}) or {}
    if not isinstance(overrides, dict):
        raise RuntimeError("Portrait overrides.json must contain an object")
    notes_by_id = {note["id"]: note for note in notes}
    if len(notes_by_id) != len(notes):
        raise RuntimeError("Portrait normalized sources contain duplicate note IDs")
    for note in notes:
        note["_override"] = overrides.get(note["id"], {})
        if note["_override"].get("chapter"):
            note["chapter_id"] = note["_override"]["chapter"]
        note["color"] = note_color(note)

    tei_occurrence_hints = infer_tei_occurrences(notes)
    by_chapter: dict[str, list[dict]] = defaultdict(list)
    for note in notes:
        by_chapter[note.get("chapter_id")].append(note)

    unmatched: list[dict] = []
    ambiguous: list[dict] = []
    resolved_ranges: dict[str, list[dict]] = defaultdict(list)
    matched_count = 0
    fuzzy_count = 0
    for chapter in chapters:
        chapter_id = chapter["id"]
        base_html = chapter["html_source"]
        chapter_plain = plain_text(base_html)
        for note in by_chapter.get(chapter_id, []):
            resolved, ambiguous_record, unmatched_record = resolve_note(
                note,
                chapter_plain,
                tei_occurrence_hints.get(note["id"]),
            )
            if resolved is None:
                note["provenance"] = {
                    "match_method": "ambiguous"
                    if ambiguous_record is not None
                    else "unmatched",
                    "confidence": (
                        max(
                            (
                                candidate.get("context_score", 0.0)
                                for candidate in (ambiguous_record or {}).get(
                                    "candidates", []
                                )
                            ),
                            default=0.0,
                        )
                        if ambiguous_record
                        else (unmatched_record or {}).get("score", 0.0),
                    ),
                }
                if ambiguous_record:
                    ambiguous.append(ambiguous_record)
                if unmatched_record:
                    unmatched.append(unmatched_record)
                continue
            start, end = resolved["start"], resolved["end"]
            matched_text = chapter_plain[start:end]
            note["anchor"]["start"] = start
            note["anchor"]["end"] = end
            note["anchor"]["canonical_text"] = matched_text
            note["anchor"]["prefix"] = chapter_plain[max(0, start - 180):start]
            note["anchor"]["suffix"] = chapter_plain[end:end + 180]
            note["provenance"] = {
                "match_method": resolved["method"],
                "confidence": round(float(resolved["confidence"]), 4),
            }
            matched_count += 1
            if resolved["method"] == "fuzzy":
                fuzzy_count += 1
            resolved_ranges[chapter_id].append(
                {
                    "note_id": note["id"],
                    "start": start,
                    "end": end,
                    "text": matched_text,
                }
            )

    report_ranges: dict[str, list[dict]] = {}
    rendered_chapters: list[dict] = []
    overlap_groups = 0
    for chapter in chapters:
        chapter_id = chapter["id"]
        ranges = resolved_ranges.get(chapter_id, [])
        overlap_groups += overlap_group_count(ranges)
        report_ranges[chapter_id] = ranges
        chapter_notes = {note["id"]: note for note in by_chapter.get(chapter_id, [])}
        rendered = inject_annotations(chapter["html_source"], ranges, chapter_notes)
        final = dict(chapter)
        final["html_source"] = rendered
        final["annotation_ranges"] = ranges
        CHAPTER_DIR.joinpath(f"{chapter_id}.json").write_text(
            json.dumps(final, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        rendered_chapters.append(final)

    write_note_files(notes)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "unmatched.json").write_text(
        json.dumps(unmatched, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (REPORT_DIR / "ambiguous.json").write_text(
        json.dumps(ambiguous, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    summary = {
        "chapters": len(rendered_chapters),
        "genius_notes_discovered": sum(
            1 for note in notes if (note.get("source") or {}).get("type") == "genius"
        ),
        "genius_notes_matched": sum(
            1
            for note in notes
            if (note.get("source") or {}).get("type") == "genius"
            and note.get("provenance", {}).get("match_method")
            not in {"unmatched", "ambiguous"}
        ),
        "tei_annotations_discovered": sum(
            1 for note in notes if (note.get("source") or {}).get("type") == "tei"
        ),
        "tei_annotations_included": sum(
            1
            for note in notes
            if (note.get("source") or {}).get("type") == "tei"
            and note.get("provenance", {}).get("match_method")
            not in {"unmatched", "ambiguous"}
        ),
        "annotations_matched": matched_count,
        "fuzzy_matches": fuzzy_count,
        "overlap_groups_merged": overlap_groups,
        "ambiguous_matches": len(ambiguous),
        "unmatched_notes": len(unmatched),
    }
    validation_errors, validation_warnings = validate_repository.validate()
    summary["validation_errors"] = len(validation_errors)
    summary["validation_warnings"] = len(validation_warnings)
    if validation_errors:
        raise RuntimeError(
            "validation failed: " + "; ".join(validation_errors[:5])
        )
    (REPORT_DIR / "portrait-build.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-fetch", action="store_true", help="Use cached raw source data")
    parser.add_argument("--offline", action="store_true", help="Alias for --no-fetch")
    parser.add_argument("--genius-only", action="store_true", help="Rebuild Genius normalization/anchors; keep existing TEI normalization")
    parser.add_argument("--tei-only", action="store_true", help="Rebuild TEI normalization/anchors; keep existing Genius normalization")
    parser.add_argument("--reanchor", action="store_true", help="Explicitly rerun anchoring (the default)")
    parser.add_argument("--report", action="store_true", help="Print the build summary")
    args = parser.parse_args(argv)
    if args.genius_only and args.tei_only:
        parser.error("--genius-only and --tei-only are mutually exclusive")
    try:
        summary = build(
            fetch=not (args.no_fetch or args.offline),
            genius=not args.tei_only,
            tei=not args.genius_only,
        )
        print("\nPortrait build complete")
        for key, value in summary.items():
            print(f"{key.replace('_', ' ').capitalize()}: {value}")
        if args.report:
            print(f"Reports: {REPORT_DIR}")
        return 0
    except Exception as exc:  # pragma: no cover - command-line diagnostics
        print(f"Portrait build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
