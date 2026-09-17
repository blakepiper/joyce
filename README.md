# Joyce Reader — local mirror

A static local reader for James Joyce with the annotated *Ulysses* mirror and
*A Portrait of the Artist as a Young Man*. The reading pane, annotation links,
Commentary pane, dictionary/Wikipedia lookup, page mode, resizable layout and
local reading state are shared by both works.

## Supported works

- *Ulysses* — 18 episodes, sourced from the local Joyce Project mirror.
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

Ulysses content remains in its original `data/chapters`, `data/notes`,
`data/media` layout through the compatibility paths in `data/works.json`.
Portrait uses the work-scoped layout under `data/works/portrait/`.

## Attribution and use

*Ulysses* and the Gutenberg text of *Portrait* are public-domain texts.
Ulysses annotations, commentary and images are the work of Joyce Project
contributors. Portrait commentary is third-party Genius material and its
provenance is retained in the data. This repository is intended for private
local use; review bulk third-party annotation material before any public
redistribution.
