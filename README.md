# Joyce Reader — local mirror

A static local reader for James Joyce with the annotated *Ulysses* mirror and
*A Portrait of the Artist as a Young Man*. The reading pane, annotation links,
Commentary pane, dictionary/Wikipedia lookup, page mode, resizable layout and
local reading state are shared by both works.

## Supported works

- *Ulysses* — 18 episodes, with the local Joyce Project mirror's annotations
  supplemented by imported Genius commentary.
- *A Portrait of the Artist as a Young Man* — all five chapters, with the
  Gutenberg canonical text, imported Genius commentary and selected semantic
  annotations from Open Editions.

## Use

Run the included launcher:

```sh
./joyce
```

Or serve the repository with any static server:

```sh
python3 -m http.server 8000
```

`fetch()` is used for local JSON, so a static HTTP server is required instead
of opening `index.html` directly. Work-aware links look like
`#/ulysses/chapter/…` and `#/portrait/chapter/1`; old Ulysses
`#/chapter/…` links remain compatible.

Click a highlighted passage to open commentary on the right. Select or
double-click text for the dictionary/Wikipedia lookup. Drag the column gutters
to resize them, and enable Page mode for screen-sized reading pages. Work,
chapter, page, scroll and layout state are stored separately in localStorage.

## Rebuilding Portrait

The generated Portrait data can be rebuilt from cached sources without network
access:

```sh
python3 scripts/build_portrait.py
python3 scripts/build_portrait.py --offline --report
python3 scripts/validate.py
```

The build keeps raw, normalized and rendered layers under
`data/works/portrait/`. It writes failed and ambiguous anchor reports to
`data/works/portrait/manifests/unmatched.json` and `ambiguous.json`.
Manual anchor corrections belong in
`data/works/portrait/overrides.json`; generated chapter and note files should
not be edited by hand. The validator treats a small set of pre-existing
dangling Ulysses source IDs as compatibility warnings; Portrait data failures
are validation errors.

Useful importer options include `--fetch`, `--offline`, `--no-fetch`,
`--genius-only`, `--tei-only` and `--reanchor`. Cached Genius HTML/API data is
reparsed by `--offline`, so parser repairs do not require downloading pages
again.

## Portrait sources

- The displayed base text is Project Gutenberg ebook **4217**:
  <https://www.gutenberg.org/ebooks/4217>.
- Explanatory annotations are imported at build time from the configured
  Genius chapter pages. Their source URLs, IDs and contributor metadata remain
  in the normalized note data.
- Semantic annotations are selected from the Open Editions project
  `open-editions/corpus-joyce-portrait-TEI`:
  <https://github.com/open-editions/corpus-joyce-portrait-TEI>.

The browser never fetches Genius or TEI at runtime. The import scripts convert
these sources into one normalized work/chapter/annotated-span/note model.

## Refreshing the Ulysses mirror

```sh
python3 scripts/rip.py --images --workers 12
python3 scripts/build_dict.py
python3 scripts/rip.py --help
```

The original Ulysses mirror remains in `data/chapters`, `data/notes`, and
`data/media`. After refreshing it, rebuild the enriched reader copy:

```sh
python3 scripts/build_ulysses.py --offline --report
python3 scripts/validate_ulysses_import.py
python3 scripts/validate.py --quiet
```

The reader uses the generated data under `data/works/ulysses/`. The build copies
the original notes and media, preserves the chapter text and existing annotation
links, and adds matched Genius commentary. Overlapping commentary uses the
reader's existing multiple-note interface. The original mirror is never edited
by the Genius importer or builder.

All 18 Genius chapter URLs are configured in `sources/ulysses-genius.json`.
Use `python3 scripts/build_ulysses.py --fetch --report` to fetch missing source
caches. Raw HTML/API responses and normalized notes are retained under
`data/works/ulysses/`, so subsequent builds work offline. Source URLs and
contributor metadata are retained with the notes, and the reader displays
source links.

Review `data/works/ulysses/manifests/ulysses-build.json` for coverage,
`unmatched.json` and `ambiguous.json` for passages that could not be placed
confidently, and `genius-failed.json` for empty or unavailable annotations.
Uncertain matches are retained for review without adding misleading passage
links. Do not edit generated files by hand. The preservation validator compares
against `tests/fixtures/ulysses-baseline.json`; do not regenerate that baseline
merely to make a failed preservation check pass.

Portrait uses the work-scoped layout under `data/works/portrait/`. Both Genius
workflows use Python's standard library and add no project dependencies.

## Attribution and use

*Ulysses* and the Gutenberg text of *Portrait* are public-domain texts.
The original Ulysses annotations, commentary and images are the work of Joyce
Project contributors. Added commentary for both books is third-party Genius material and its
provenance is retained in the data. This repository is intended for private
local use; review bulk third-party annotation material before any public
redistribution.
