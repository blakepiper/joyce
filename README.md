# The Joyce Project — local mirror

A local copy of [joyceproject.com](https://joyceproject.com/) (an annotated
*Ulysses*), with **one deliberate change**: clicking an in-text hyperlink
opens the note in a dedicated **Commentary panel to the right of the
text** instead of a popup.

Layout, left to right:

1. skinny column — chapter selector (plus About pages),
2. primary column — interactive novel text,
3. commentary column — blank until a hyperlink is clicked.

## Use

- Click a chapter (left) to read it (centre). Click any highlighted
  passage and the note opens in the **Commentary** panel (right),
  with its images/video. Links inside notes drill deeper with a Back
  button.
- **Word lookup:** select any word — double-click it or drag across it —
  and a Foliate-style popup appears showing the **dictionary definition**
  (Webster's Unabridged 1913, bundled offline under `data/dict/`).
  A **Wikipedia** tab in the popup fetches the article summary
  (needs internet). Single-click a passage instead and you get the
  Joyce Project note as before — click = note, select = definition.
- **Layout:** drag the gutters between columns to resize them
  (double-click a gutter to reset). Text and commentary default to 60:40.
  Sizes persist across visits. The theme is dark grey/black; the
  annotation link colors are unchanged from the original.
- **Page mode:** tick *Page mode* in the left column to read ereader-style —
  the chapter is split into screen-sized pages turned with &larr; / &rarr;
  (or the footer buttons). Your page per chapter is remembered.

## Run it

Any static file server works (plain `file://` will not, because the app
uses `fetch()` for the local JSON):

```sh
cd joyce
python3 -m http.server 8000
# open http://localhost:8000/
```

No build step, no dependencies, works fully offline — all chapter text,
notes, media metadata and images are mirrored under `data/` and
`static/img/`.

## Refreshing the mirror

```sh
python3 scripts/rip.py --images --workers 12   # re-fetch JSON + images
python3 scripts/build_dict.py                  # rebuild offline dictionary
python3 scripts/rip.py --help
```

## How it works

- Content comes from the site's own public JSON API (`/api/chapters/`,
  `/api/notes/`, `/api/media/`, `/api/info/`), which is what the original
  frontend queries. `scripts/rip.py` pages through it into `data/`.
- In-text links in the chapter HTML are `<a href="<note-id>"
  data-color="..." data-type="annotation">`. `app.js` colours them from
  `data-color` (as the original does) and intercepts clicks to render the
  note + its images/video into the right panel. Links inside notes push
  onto a history stack (Back button). External links open in a new tab.
- Images resolve to `static/img/<media-id>/img.<ext>`, with a fallback to
  the live site if a file is ever missing.

## Attribution

*Ulysses* itself is public domain. Annotations, commentary and images are
the work of the Joyce Project contributors — this mirror is for personal
local use; please don't redistribute it. When in doubt, use the
[original site](https://joyceproject.com/).
