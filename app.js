/* Local mirror of joyceproject.com.
 * Same content & look; the one deliberate change: in-text annotation
 * links render into the right-hand "Commentary" panel instead of
 * opening a modal popup.
 */
(function () {
  "use strict";
  var REMOTE = "https://joyceproject.com";
  var cache = { chapters: {}, notes: {}, media: {} };
  var works = [];
  var currentWork = null;
  var chapterList = [];
  var infoList = [];
  var currentChapter = null;
  var noteStack = []; // {workId, ids} or {infoId} entries for Back nav
  var loadingNote = null;

  function workById(workId) {
    return works.find(function (work) { return work.id === workId; }) || null;
  }

  function workDataPath(workId, type, id) {
    var work = workById(workId);
    var legacy = work && work.legacy_paths && work.legacy_paths[type];
    var base = legacy || ("data/works/" + encodeURIComponent(workId) + "/" + type);
    return base + "/" + encodeURIComponent(id) + ".json";
  }

  function cacheKey(workId, id) {
    return String(workId) + ":" + String(id);
  }

  function chapterDescriptor(workId, idOrLabel) {
    var work = workById(workId);
    if (!work) return null;
    var token = String(idOrLabel);
    return (work.chapters || []).find(function (chapter) {
      return String(chapter.id) === token ||
        String(chapter.number) === token ||
        String(chapter.label || "").toLowerCase() === token.toLowerCase();
    }) || null;
  }

  function routeFor(workId, chapterId) {
    return "#/" + encodeURIComponent(workId) + "/chapter/" +
      encodeURIComponent(chapterId);
  }

  function parseRoute() {
    var hash = location.hash || "";
    var match = hash.match(/^#\/([^/]+)\/chapter\/([^/]+)/);
    if (match) {
      return { workId: decodeURIComponent(match[1]), chapter: decodeURIComponent(match[2]) };
    }
    // Compatibility with links copied from the original single-work reader.
    match = hash.match(/^#\/chapter\/([A-Za-z0-9\-_]+)/);
    if (match) return { workId: "ulysses", chapter: match[1] };
    return null;
  }

  function storageGet(key, fallback) {
    try {
      var value = localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch (e) { return fallback; }
  }

  function storageSet(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }

  function $(id) { return document.getElementById(id); }
  function fetchJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error(r.status + " " + url);
      return r.json();
    });
  }

  // The original palette was designed for a white page; on our dark theme
  // dark hues (e.g. the annotation blue #307EE3) are unreadable. Lift each
  // color toward white just enough to clear a legibility floor, so hues
  // stay recognizable while everything passes on black.
  function legibleOnDark(hex) {
    var n = parseInt(hex, 16);
    var r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    function lin(c) {
      c /= 255;
      return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    }
    var lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
    var t = Math.max(0, Math.min(1, (0.55 - lum) / 0.55)) * 0.65;
    if (t <= 0) return "#" + hex.toUpperCase();
    function mix(c) { return Math.round(c + (255 - c) * t); }
    function hx(c) {
      var s = mix(c).toString(16);
      return s.length < 2 ? "0" + s : s;
    }
    return "#" + (hx(r) + hx(g) + hx(b)).toUpperCase();
  }

  function colorizeLinks(root) {
    var links = root.querySelectorAll("a[href]");
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      var href = a.getAttribute("href") || "";
      if (/^https?:/i.test(href)) {
        a.setAttribute("target", "_blank");
        a.setAttribute("rel", "noopener");
        continue;
      }
      var color = a.getAttribute("data-color") || "7AA8FF";
      if (/^[0-9a-fA-F]{6}$/.test(color)) a.style.color = legibleOnDark(color);
      else a.style.color = "#7aa8ff";
    }
  }

  function onAnnotatedClick(evt) {
    var a = evt.target.closest ? evt.target.closest("a[href]") : null;
    if (!a) return;
    var href = a.getAttribute("href") || "";
    if (/^https?:/i.test(href)) return; // let external links open normally
    evt.preventDefault();
    var sel = window.getSelection();
    var hasSel = sel && !sel.isCollapsed && String(sel).trim().length > 0;
    // Double-click or drag-select means "look the word up", not "open note".
    // (The mouseup handler below already showed the lookup popup.)
    if (evt.detail > 1 || hasSel) return;
    var textBody = $("text-body");
    var prev = textBody.querySelectorAll("a.active-note");
    for (var i = 0; i < prev.length; i++) prev[i].classList.remove("active-note");
    var inReader = !!a.closest("#text-body");
    if (inReader) a.classList.add("active-note");
    var ids = (a.getAttribute("data-notes") || "").split(/[,\s]+/)
      .filter(Boolean);
    if (!ids.length && href) ids = [href];
    if (ids.length === 1 && ids[0].indexOf("info:") === 0) {
      openInfo(ids[0].slice(5), null, true);
    } else if (ids.length) {
      showNoteByIds(currentWork ? currentWork.id : "ulysses", ids, true);
    }
  }

  /* ---------- chapters (left + center) ---------- */
  function renderChapterList() {
    var ol = $("chapter-list");
    ol.innerHTML = "";
    works.forEach(function (work) {
      var heading = document.createElement("li");
      heading.className = "work-heading" +
        (currentWork && currentWork.id === work.id ? " active-work" : "");
      var headingButton = document.createElement("button");
      headingButton.textContent = work.short_title || work.title;
      headingButton.title = work.title;
      headingButton.addEventListener("click", function () {
        var last = storageGet("joyce:last-chapter:" + work.id, "");
        var descriptor = chapterDescriptor(work.id, last) || (work.chapters || [])[0];
        if (descriptor) loadChapter(work.id, descriptor.id, true);
      });
      heading.appendChild(headingButton);
      ol.appendChild(heading);
      (work.chapters || []).forEach(function (c) {
        var li = document.createElement("li");
        if (currentWork && currentWork.id === work.id &&
            currentChapter && c.id === currentChapter.id) li.className = "active";
        var b = document.createElement("button");
        var num = document.createElement("span");
        num.className = "ep-num";
        num.textContent = c.label || (c.number + ".");
        b.appendChild(num);
        b.appendChild(document.createTextNode(c.title));
        b.addEventListener("click", function () { loadChapter(work.id, c.id, true); });
        li.appendChild(b);
        ol.appendChild(li);
      });
    });
    var idx = chapterList.findIndex(function (c) {
      return currentChapter && c.id === currentChapter.id;
    });
    $("prev-chapter").disabled = idx <= 0;
    $("next-chapter").disabled = idx < 0 || idx >= chapterList.length - 1;
  }

  function getChapter(workId, id) {
    var key = cacheKey(workId, id);
    if (cache.chapters[key]) return Promise.resolve(cache.chapters[key]);
    return fetchJSON(workDataPath(workId, "chapters", id)).then(function (d) {
      if (!d.work_id) d.work_id = workId; // legacy Ulysses data compatibility
      cache.chapters[key] = d;
      return d;
    });
  }

  var chapterLoadSeq = 0;
  function loadChapter(workId, id, pushHash) {
    var work = workById(workId);
    if (!work) return;
    var descriptor = chapterDescriptor(workId, id);
    if (!descriptor) return;
    var loadSeq = ++chapterLoadSeq;
    var body = $("text-body");
    hideLookup();
    if (currentChapter && (currentChapter.id !== descriptor.id ||
        !currentWork || currentWork.id !== workId)) clearPreview();
    currentWork = work;
    chapterList = work.chapters || [];
    body.innerHTML = "<p><em>Loading&hellip;</em></p>";
    getChapter(workId, descriptor.id).then(function (d) {
      if (loadSeq !== chapterLoadSeq) return;
      currentChapter = d;
      $("chapter-title").textContent = work.id === "ulysses" ?
        (d.number + ". " + d.title) : (d.title || descriptor.title);
      document.title = (d.title || descriptor.title) + " — " + work.title;
      body.innerHTML = "";
      var wrap = document.createElement("div");
      wrap.className = "read-width";
      wrap.innerHTML = d.html_source;
      body.appendChild(wrap);
      colorizeLinks(body);
      body.scrollTop = pageMode ? 0 : Number(storageGet(
        "joyce:scroll:" + workId + ":" + descriptor.id, "0")) || 0;
      body.scrollLeft = 0;
      pageIndex = 0;
      storageSet("joyce:last-work", workId);
      storageSet("joyce:last-chapter:" + workId, descriptor.id);
      renderChapterList();
      scheduleLayout(false); // fresh chapter: restore its saved page
      if (pushHash !== false) {
        try {
          history.pushState({ work: workId, chapter: descriptor.id }, "",
            routeFor(workId, descriptor.id));
        } catch (e) { location.hash = routeFor(workId, descriptor.id); }
      }
    }).catch(function (err) {
      if (loadSeq !== chapterLoadSeq) return;
      body.innerHTML = "<p>Could not load chapter: " +
        String(err && err.message || err) + "</p>";
    });
  }

  /* ---------- notes (right panel) ---------- */
  function getNote(workId, id) {
    var key = cacheKey(workId, id);
    if (cache.notes[key]) return Promise.resolve(cache.notes[key]);
    return fetchJSON(workDataPath(workId, "notes", id)).then(function (d) {
      if (!d.work_id) d.work_id = workId; // legacy Ulysses data compatibility
      cache.notes[key] = d;
      return d;
    });
  }
  function getMedia(workId, id) {
    var key = cacheKey(workId, id);
    if (cache.media[key]) return Promise.resolve(cache.media[key]);
    return fetchJSON(workDataPath(workId, "media", id)).then(function (d) {
      cache.media[key] = d;
      return d;
    }).catch(function () { return null; });
  }

  function mediaFigure(m, workId) {
    if (!m) return null;
    if (m.type === "yt" && m.youtube_url) {
      var frame = document.createElement("iframe");
      frame.src = m.youtube_url;
      frame.setAttribute("allowfullscreen", "");
      frame.setAttribute("loading", "lazy");
      var fig = document.createElement("figure");
      fig.appendChild(frame);
      if (m.html_source) {
        var cap = document.createElement("div");
        cap.innerHTML = m.html_source;
        fig.appendChild(cap);
      }
      return fig;
    }
    if (m.type === "img" && m.file_ext) {
      var fig2 = document.createElement("figure");
      var img = document.createElement("img");
      img.alt = m.title || "";
      img.loading = "lazy";
      img.src = m.local_path || ("static/img/" + m.id + "/img." + m.file_ext);
      img.onerror = function () {
        img.onerror = null;
        if (!m.local_path) {
          img.src = REMOTE + "/static/img/" + m.id + "/img." + m.file_ext;
        }
      };
      fig2.appendChild(img);
      if (m.html_source) {
        var cap2 = document.createElement("div");
        cap2.innerHTML = m.html_source;
        fig2.appendChild(cap2);
      }
      return fig2;
    }
    return null;
  }

  function renderNotePanel(noteOrNotes, mediaDocs) {
    var notes = Array.isArray(noteOrNotes) ? noteOrNotes : [noteOrNotes];
    notes = notes.filter(Boolean);
    var first = notes[0] || { title: "(untitled note)", html_source: "" };
    $("preview-empty").hidden = true;
    $("preview-content").hidden = false;
    $("note-title").textContent = notes.length > 1 ?
      "Related commentary" : (first.title || "(untitled note)");
    var nb = $("note-body");
    nb.innerHTML = "";
    notes.forEach(function (note, index) {
      var group = document.createElement("section");
      group.className = "note-group";
      if (note.id) group.setAttribute("data-note-id", note.id);
      if (notes.length > 1) {
        var heading = document.createElement("h3");
        heading.textContent = note.title || "Related note " + (index + 1);
        group.appendChild(heading);
      }
      var body = document.createElement("div");
      body.innerHTML = note.html_source || "<p><em>(empty note)</em></p>";
      group.appendChild(body);
      if (note.source && note.source.url) {
        var provenance = document.createElement("div");
        provenance.className = "note-provenance";
        var sourceLink = document.createElement("a");
        sourceLink.href = note.source.url;
        sourceLink.target = "_blank";
        sourceLink.rel = "noopener";
        sourceLink.textContent = "Source";
        provenance.appendChild(sourceLink);
        group.appendChild(provenance);
      }
      nb.appendChild(group);
    });
    colorizeLinks(nb);
    var gal = $("media-gallery");
    gal.innerHTML = "";
    (mediaDocs || []).forEach(function (m) {
      var f = mediaFigure(m, first.work_id);
      if (f) gal.appendChild(f);
    });
    $("preview-content").scrollTop = 0;
    $("preview-status").textContent = "";
    $("preview-back").disabled = noteStack.length <= 1;
  }

  function openNotes(workId, ids) {
    if (!ids || !ids.length) return;
    $("preview-empty").hidden = true;
    $("preview-content").hidden = true;
    $("preview-status").textContent = "Loading note…";
    var token = loadingNote = {};
    Promise.all(ids.map(function (id) {
      return getNote(workId, id).catch(function () { return null; });
    })).then(function (notes) {
      if (loadingNote !== token) return;
      notes = notes.filter(Boolean);
      if (!notes.length) throw new Error("note not found");
      var mediaIds = [];
      notes.forEach(function (note) {
        (note.media_doc_ids || []).forEach(function (id) {
          if (mediaIds.indexOf(id) < 0) mediaIds.push(id);
        });
      });
      return Promise.all(mediaIds.map(function (id) { return getMedia(workId, id); }))
        .then(function (docs) {
          if (loadingNote !== token) return;
          renderNotePanel(notes, docs.filter(Boolean));
        });
    }).catch(function () {
      if (loadingNote !== token) return;
      $("preview-content").hidden = true;
      $("preview-empty").hidden = false;
      $("preview-status").textContent =
        "Note not found locally (" + ids.join(", ") + "). " +
        "It may be a dangling reference in the source data.";
    });
  }

  function sameNoteEntry(left, right) {
    if (!left || !right || left.infoId || right.infoId) return false;
    return left.workId === right.workId &&
      left.ids.join(",") === right.ids.join(",");
  }

  function showNoteByIds(workId, ids, push) {
    ids = ids.filter(function (id, index) { return id && ids.indexOf(id) === index; });
    if (!ids.length) return;
    var entry = { workId: workId, ids: ids };
    if (push === true && !sameNoteEntry(noteStack[noteStack.length - 1], entry)) {
      noteStack.push(entry);
    }
    openNotes(workId, ids);
  }

  function showNoteById(id, push) {
    if (String(id).indexOf("info:") === 0) {
      openInfo(String(id).slice(5), null, push);
    } else {
      showNoteByIds(currentWork ? currentWork.id : "ulysses", [id], push);
    }
  }

  function showHistoryEntry(entry) {
    if (!entry) return;
    if (entry.infoId) openInfo(entry.infoId, entry.title, false);
    else openNotes(entry.workId, entry.ids);
  }

  function clearPreview() {
    noteStack = [];
    loadingNote = null;
    $("preview-content").hidden = true;
    $("preview-empty").hidden = false;
    $("preview-status").textContent = "";
    $("preview-back").disabled = true;
    var prev = $("text-body").querySelectorAll("a.active-note");
    for (var i = 0; i < prev.length; i++) prev[i].classList.remove("active-note");
  }

  /* ---------- info pages (About links -> right panel) ---------- */
  function openInfo(id, title, push) {
    if (push !== false) {
      var entry = { infoId: id, title: title };
      var top = noteStack[noteStack.length - 1];
      if (!top || top.infoId !== id) noteStack.push(entry);
    }
    $("preview-empty").hidden = true;
    $("preview-content").hidden = true;
    $("preview-status").textContent = "Loading…";
    fetchJSON("data/info/" + id + ".json").then(function (d) {
      renderNotePanel([{ title: title || d.title, html_source: d.html_source }],
                      []);
    }).catch(function () {
      $("preview-status").textContent = "Could not load page.";
    });
  }

  /* ---------- word lookup popup (dictionary + Wikipedia) ---------- */
  var WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/";
  var WIKI_SEARCH = "https://en.wikipedia.org/w/api.php?action=opensearch" +
    "&limit=1&namespace=0&format=json&origin=*&search=";
  var dictShards = {};      // shard letter -> {word: definition}
  var lookupSeq = 0;        // async race guard
  var lookupPhrase = "";    // current selection text
  var lookupTab = "dict";   // 'dict' | 'wiki'
  var wikiCache = {};       // phrase -> summary json (or null)

  function escHTML(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // Candidate base forms for a single token, most likely first.
  function wordCandidates(token) {
    var w = token.toLowerCase().replace(/^['\u2018\u2019]+|['\u2018\u2019]+$/g, "");
    w = w.replace(/(['\u2018\u2019])s$/g, "");
    var out = [w];
    function add(c) { if (c && c.length > 1 && out.indexOf(c) < 0) out.push(c); }
    if (/ies$/.test(w) && w.length > 4) add(w.slice(0, -3) + "y");
    else if (/es$/.test(w) && w.length > 4) { add(w.slice(0, -2)); add(w.slice(0, -1)); }
    else if (/s$/.test(w) && !/ss$/.test(w)) add(w.slice(0, -1));
    if (/ied$/.test(w)) add(w.slice(0, -3) + "y");
    else if (/ed$/.test(w) && w.length > 4) { add(w.slice(0, -1)); add(w.slice(0, -2)); }
    if (/ing$/.test(w) && w.length > 5) {
      var b = w.slice(0, -3);
      add(b); add(b + "e");
      if (/(.)\1$/.test(b)) add(b.slice(0, -1)); // running -> run
    }
    return out.slice(0, 8);
  }

  function lookupCandidates(phrase) {
    var tokens = phrase.toLowerCase().replace(/[^a-zA-Z'\u2018\u2019-]+/g, " ")
      .trim().split(/\s+/).filter(Boolean);
    var cands = [];
    if (tokens.length > 1 && tokens.length <= 3) cands.push(tokens.join(" "));
    if (tokens.length) {
      wordCandidates(tokens[0]).forEach(function (c) {
        if (cands.indexOf(c) < 0) cands.push(c);
      });
    }
    return cands;
  }

  function getDictShard(letter) {
    var sh = (/^[a-z]$/.test(letter)) ? letter : "0";
    if (dictShards[sh]) return Promise.resolve(dictShards[sh]);
    return fetchJSON("data/dict/" + sh + ".json").then(function (d) {
      dictShards[sh] = d || {};
      return dictShards[sh];
    }).catch(function () { return {}; });
  }

  function findDefinition(phrase) {
    var cands = lookupCandidates(phrase);
    var chain = Promise.resolve(null);
    cands.forEach(function (c) {
      chain = chain.then(function (found) {
        if (found) return found;
        return getDictShard(c.charAt(0)).then(function (shard) {
          return shard[c] ? { word: c, text: shard[c] } : null;
        });
      });
    });
    return chain;
  }

  function fetchWikiSummary(phrase) {
    var key = phrase.toLowerCase();
    if (key in wikiCache) return Promise.resolve(wikiCache[key]);
    var title = phrase.trim().replace(/\s+/g, "_");
    function store(v) { wikiCache[key] = v; return v; }
    return fetch(WIKI_SUMMARY + encodeURIComponent(title))
      .then(function (r) {
        if (r.ok) return r.json().then(store);
        if (r.status !== 404) throw new Error("wiki " + r.status);
        // No exact title: fall back to search, take top hit.
        return fetch(WIKI_SEARCH + encodeURIComponent(phrase.trim()))
          .then(function (r2) {
            if (!r2.ok) throw new Error("search " + r2.status);
            return r2.json();
          }).then(function (sj) {
            var hit = sj && sj[1] && sj[1][0];
            if (!hit) return store(null);
            return fetch(WIKI_SUMMARY + encodeURIComponent(
              String(hit).replace(/\s+/g, "_")))
              .then(function (r3) {
                if (!r3.ok) return store(null);
                return r3.json().then(store);
              });
          });
      }).catch(function () { return store(null); });
  }

  function positionPopup(rect) {
    var pop = $("lookup-popup");
    var w = Math.min(340, window.innerWidth - 16);
    var h = pop.offsetHeight || 200;
    var x = rect.left + rect.width / 2 - w / 2;
    x = Math.max(8, Math.min(x, window.innerWidth - w - 8));
    var above = rect.top - h - 14;
    var below = rect.bottom + 14;
    var y, arrowTop;
    if (above >= 8) { y = above; arrowTop = -6; }
    else { y = Math.min(below, window.innerHeight - h - 8); arrowTop = null; }
    pop.style.left = x + "px";
    pop.style.top = Math.max(8, y) + "px";
    var arrow = $("lookup-arrow");
    var ax = Math.max(14, Math.min(
      rect.left + rect.width / 2 - x, w - 14)) - 6;
    arrow.style.left = ax + "px";
    if (arrowTop === null) {
      arrow.style.top = "-6px";
      arrow.style.bottom = "";
    } else {
      arrow.style.top = "";
      arrow.style.bottom = "-6px";
    }
  }

  function hideLookup() {
    lookupSeq++;
    $("lookup-popup").hidden = true;
  }

  function setLookupTab(tab) {
    lookupTab = tab;
    $("tab-dict").className = tab === "dict" ? "active" : "";
    $("tab-wiki").className = tab === "wiki" ? "active" : "";
    if (tab === "dict") renderDict();
    else renderWiki();
  }

  function renderDict() {
    var body = $("lookup-body");
    body.innerHTML = "<p class=\"dim\">Looking up…</p>";
    var seq = lookupSeq;
    findDefinition(lookupPhrase).then(function (found) {
      if (seq !== lookupSeq || lookupTab !== "dict") return;
      if (found) {
        var paras = escHTML(found.text).split(/\n{2,}|\r\n\r\n/)
          .map(function (p) {
            return "<p>" + p.replace(/\n/g, "<br />") + "</p>";
          }).join("");
        body.innerHTML = "<div class=\"dict-head\">" + escHTML(found.word) +
          "</div>" + paras +
          "<div class=\"dict-src\">Webster's Unabridged (1913), " +
          "bundled offline</div>";
      } else {
        body.innerHTML = "<p>No dictionary entry for &ldquo;" +
          escHTML(lookupPhrase) + "&rdquo;.</p>" +
          "<p><button class=\"linklike\" id=\"lookup-try-wiki\">" +
          "Try Wikipedia instead</button></p>";
        var b = $("lookup-try-wiki");
        if (b) b.addEventListener("click", function () { setLookupTab("wiki"); });
      }
      var r = lastLookupRect();
      if (r) positionPopup(r);
    });
  }

  var _lastRect = null;
  function lastLookupRect() { return _lastRect; }

  function renderWiki() {
    var body = $("lookup-body");
    // serve from cache instantly when available
    var cached = wikiCache[lookupPhrase.toLowerCase()];
    if (cached !== undefined) { paintWiki(cached); return; }
    body.innerHTML = "<p class=\"dim\">Searching Wikipedia…</p>";
    var seq = lookupSeq;
    fetchWikiSummary(lookupPhrase).then(function (sum) {
      if (seq !== lookupSeq || lookupTab !== "wiki") return;
      paintWiki(sum);
    });
  }

  function paintWiki(sum) {
    var body = $("lookup-body");
    if (!sum) {
      body.innerHTML = "<p>No Wikipedia article found for &ldquo;" +
        escHTML(lookupPhrase) + "&rdquo;.</p>" +
        "<p class=\"dim\">Wikipedia needs an internet connection; " +
        "the dictionary above works offline.</p>";
      return;
    }
    var html = "";
    if (sum.thumbnail && sum.thumbnail.source) {
      html += "<img class=\"wiki-thumb\" src=\"" +
        escHTML(sum.thumbnail.source) + "\" alt=\"\" />";
    }
    html += "<div class=\"wiki-title\">" + escHTML(sum.title || lookupPhrase) +
      "</div>";
    if (sum.description) {
      html += "<p class=\"wiki-desc\">" + escHTML(sum.description) + "</p>";
    }
    if (sum.extract_html) html += "<div>" + sum.extract_html + "</div>";
    else if (sum.extract) html += "<p>" + escHTML(sum.extract) + "</p>";
    var page = sum.content_urls && sum.content_urls.desktop &&
      sum.content_urls.desktop.page;
    if (page) {
      html += "<a class=\"wiki-more\" href=\"" + escHTML(page) +
        "\" target=\"_blank\" rel=\"noopener\">Read more on Wikipedia</a>";
    }
    body.innerHTML = html;
    // keep navigation inside the app: externalize + new-tab all links
    var links = body.querySelectorAll("a[href]");
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute("href");
      if (href && href.charAt(0) === "/") {
        links[i].setAttribute("href", "https://en.wikipedia.org" + href);
      }
      links[i].setAttribute("target", "_blank");
      links[i].setAttribute("rel", "noopener");
    }
    var r = lastLookupRect();
    if (r) positionPopup(r);
  }

  function selectionInsideReader(sel) {
    if (!sel || sel.rangeCount === 0) return null;
    var node = sel.anchorNode;
    var el = node && (node.nodeType === 1 ? node : node.parentElement);
    if (!el || !el.closest) return null;
    if (el.closest("#lookup-popup")) return null;
    var zone = el.closest("#text-body, #note-body");
    return zone || null;
  }

  function selectionRect(sel, evt) {
    try {
      var r = sel.getRangeAt(0).getBoundingClientRect();
      if (r && (r.width || r.height || r.top || r.left)) return r;
    } catch (e) { /* Range without layout (or no range) -> fall back */ }
    if (evt && typeof evt.clientX === "number") {
      var x = evt.clientX, y = evt.clientY;
      return { left: x, right: x, top: y, bottom: y, width: 0, height: 0 };
    }
    var cx = window.innerWidth / 2;
    return { left: cx, right: cx, top: 120, bottom: 120, width: 0, height: 0 };
  }

  function maybeShowLookup(evt) {
    var sel = window.getSelection();
    var zone = selectionInsideReader(sel);
    var text = sel ? String(sel).trim() : "";
    if (!zone || text.length < 2 || text.length > 120) return false;
    lookupPhrase = text.replace(/\s+/g, " ");
    _lastRect = selectionRect(sel, evt);
    $("lookup-word").textContent = lookupPhrase.length > 40 ?
      lookupPhrase.slice(0, 40) + "…" : lookupPhrase;
    $("lookup-popup").hidden = false;
    setLookupTab("dict"); // definition first, as requested
    positionPopup(_lastRect);
    return true;
  }

  function wireLookup() {
    document.addEventListener("mouseup", function (evt) {
      if (evt.target && evt.target.closest &&
          evt.target.closest("#lookup-popup")) return; // interacting with popup
      var sel = window.getSelection();
      var collapsed = !sel || sel.isCollapsed;
      window.setTimeout(function () {
        if (collapsed) {
          // single click elsewhere dismisses (link clicks open notes instead)
          if (!evt.target || !evt.target.closest ||
              !evt.target.closest("#lookup-popup")) hideLookup();
        } else {
          maybeShowLookup(evt);
        }
      }, 0);
    });
    // Double-click selects the word just after mouseup in some browsers.
    document.addEventListener("dblclick", function (evt) {
      window.setTimeout(function () { maybeShowLookup(evt); }, 0);
    });
    document.addEventListener("keydown", function (evt) {
      if (evt.key === "Escape") hideLookup();
    });
    $("lookup-close").addEventListener("click", hideLookup);
    $("tab-dict").addEventListener("click", function () { setLookupTab("dict"); });
    $("tab-wiki").addEventListener("click", function () { setLookupTab("wiki"); });
  }

  /* ---------- resizable columns (persisted) ---------- */
  var LAYOUT_KEY = "joyce-layout";
  // ratio = fraction of (text + preview) width given to the text column.
  var layout = { left: 210, ratio: 0.6 };

  function loadLayout() {
    try {
      var saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "null");
      if (saved && typeof saved.left === "number" &&
          typeof saved.ratio === "number") {
        layout.left = Math.max(140, Math.min(440, saved.left));
        layout.ratio = Math.max(0.2, Math.min(0.85, saved.ratio));
      }
    } catch (e) { /* private mode etc: stick with defaults */ }
  }

  function saveLayout() {
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); }
    catch (e) { /* ignore */ }
  }

  function applyLayout() {
    $("chapters-col").style.width = layout.left + "px";
    var tc = $("text-col"), pc = $("preview-col");
    // Longhands (not the `flex` shorthand) for maximal compatibility.
    tc.style.flexGrow = String(layout.ratio);
    tc.style.flexShrink = "1";
    tc.style.flexBasis = "0%";
    pc.style.flexGrow = String(1 - layout.ratio);
    pc.style.flexShrink = "1";
    pc.style.flexBasis = "0%";
  }

  function wireResize() {
    var hasPointer = typeof window.PointerEvent !== "undefined";
    var suppressMouseUntil = 0;
    [["resize-left", "left"], ["resize-right", "ratio"]].forEach(function (pair) {
      var el = $(pair[0]);
      var mode = pair[1];
      el.addEventListener("dblclick", function () {
        layout = { left: 210, ratio: 0.6 };
        applyLayout();
        saveLayout();
      });
      function onDown(evt, usePointerEvents) {
        if (evt.button !== undefined && evt.button !== 0) return;
        evt.preventDefault();
        el.classList.add("dragging");
        var startX = evt.clientX;
        var startLeft = layout.left;
        var textW = $("text-col").getBoundingClientRect().width;
        var prevW = $("preview-col").getBoundingClientRect().width;
        var moveEvt = usePointerEvents ? "pointermove" : "mousemove";
        var upEvt = usePointerEvents ? "pointerup" : "mouseup";
        function onMove(e) {
          var dx = e.clientX - startX;
          if (mode === "left") {
            layout.left = Math.max(140, Math.min(440, startLeft + dx));
          } else {
            var total = textW + prevW;
            if (total > 0) {
              var c = Math.max(220, Math.min(total - 220, textW + dx));
              layout.ratio = Math.max(0.2, Math.min(0.85, c / total));
            }
          }
          applyLayout();
        }
        function onUp() {
          window.removeEventListener(moveEvt, onMove);
          window.removeEventListener(upEvt, onUp);
          el.classList.remove("dragging");
          saveLayout();
        }
        window.addEventListener(moveEvt, onMove);
        window.addEventListener(upEvt, onUp);
      }
      if (hasPointer) {
        el.addEventListener("pointerdown", function (evt) {
          suppressMouseUntil = Date.now() + 500;
          onDown(evt, true);
        });
      }
      el.addEventListener("mousedown", function (evt) {
        if (Date.now() < suppressMouseUntil) return; // pointer already handled
        onDown(evt, false);
      });
    });
  }

  /* ---------- e-reader page mode ---------- */
  var PAGE_GAP = 48; // px; must match the column-gap set in layoutPages
  var PAGEMODE_KEY = "joyce-pagemode";
  var PAGES_KEY = "joyce-pages";
  var pageMode = false;
  var pageIndex = 0;
  var pageCount = 1;
  var pageW = 0;

  function loadPagePrefs() {
    try {
      pageMode = localStorage.getItem(PAGEMODE_KEY) === "1";
    } catch (e) { pageMode = false; }
  }
  function savedPages() {
    try { return JSON.parse(localStorage.getItem(PAGES_KEY) || "{}") || {}; }
    catch (e) { return {}; }
  }

  function pageKey() {
    if (!currentWork || !currentChapter) return null;
    return "joyce:page:" + currentWork.id + ":" + currentChapter.id;
  }

  function savedPage() {
    var key = pageKey();
    var namespaced = key ? storageGet(key, null) : null;
    if (namespaced !== null) return Number(namespaced) || 0;
    // Keep page positions from the original Ulysses-only reader usable.
    if (currentWork && currentWork.id === "ulysses" && currentChapter) {
      return Number(savedPages()[currentChapter.id]) || 0;
    }
    return 0;
  }

  function savePage() {
    var key = pageKey();
    if (key) storageSet(key, String(pageIndex));
    // Also update the legacy object for old Ulysses sessions.
    if (currentWork && currentWork.id === "ulysses" && currentChapter) {
      try {
        var m = savedPages();
        m[currentChapter.id] = pageIndex;
        localStorage.setItem(PAGES_KEY, JSON.stringify(m));
      } catch (e) { /* ignore */ }
    }
  }

  function saveScroll() {
    if (!currentWork || !currentChapter || pageMode) return;
    storageSet("joyce:scroll:" + currentWork.id + ":" + currentChapter.id,
      String($("text-body").scrollTop || 0));
  }

  function wireReadingPosition() {
    var pending = null;
    $("text-body").addEventListener("scroll", function () {
      if (pageMode || pending) return;
      pending = window.setTimeout(function () {
        pending = null;
        saveScroll();
      }, 120);
    });
  }

  function applyPageMode() {
    document.body.classList.toggle("paged", pageMode);
    $("pagemode-toggle").checked = pageMode;
    try { localStorage.setItem(PAGEMODE_KEY, pageMode ? "1" : "0"); }
    catch (e) { /* ignore */ }
    scheduleLayout(false); // (re)entering mode: restore the chapter's saved page
  }

  function scheduleLayout(keepPosition) {
    // layout needs settled CSS/DOM: next frame when available.
    var keep = keepPosition !== false;
    if (window.requestAnimationFrame) {
      window.requestAnimationFrame(function () { layoutPages(keep); });
    } else {
      window.setTimeout(function () { layoutPages(keep); }, 0);
    }
  }

  function readWrap() {
    return document.querySelector("#text-body .read-width");
  }

  function layoutPages(keepPosition) {
    if (!pageMode) {
      var wrap = readWrap();
      if (wrap) { wrap.style.columnWidth = ""; wrap.style.columnGap = ""; }
      return;
    }
    var body = $("text-body");
    var w = readWrap();
    if (!body || !w) return;
    var cs = window.getComputedStyle(body);
    var pad = (parseFloat(cs.paddingLeft) || 0) +
      (parseFloat(cs.paddingRight) || 0);
    pageW = body.clientWidth - pad;
    if (!(pageW > 0)) { pageCount = 1; updatePager(); return; }
    w.style.columnWidth = pageW + "px";
    w.style.columnGap = PAGE_GAP + "px";
    var ratio = keepPosition && pageCount > 1 ? pageIndex / (pageCount - 1) : 0;
    var total = w.scrollWidth + PAGE_GAP;
    pageCount = Math.max(1, Math.round(total / (pageW + PAGE_GAP)));
    pageIndex = keepPosition ?
      Math.round(ratio * (pageCount - 1)) :
      savedPage();
    goToPage(Math.max(0, Math.min(pageCount - 1, pageIndex)), true);
  }

  function goToPage(n, instant) {
    if (!pageMode) return;
    pageIndex = Math.max(0, Math.min(pageCount - 1, n));
    var body = $("text-body");
    if (body) {
      if (instant || !body.scrollTo) body.scrollLeft = pageIndex * (pageW + PAGE_GAP);
      else body.scrollTo({ left: pageIndex * (pageW + PAGE_GAP), behavior: "smooth" });
    }
    updatePager();
    savePage();
  }

  function updatePager() {
    $("page-count").textContent = pageMode ?
      ((pageIndex + 1) + " / " + pageCount) : "";
    $("page-prev").disabled = !pageMode || pageIndex <= 0;
    $("page-next").disabled = !pageMode || pageIndex >= pageCount - 1;
  }

  function wirePages() {
    $("pagemode-toggle").addEventListener("change", function (evt) {
      pageMode = !!evt.target.checked;
      pageIndex = 0;
      applyPageMode();
    });
    $("page-prev").addEventListener("click", function () { goToPage(pageIndex - 1); });
    $("page-next").addEventListener("click", function () { goToPage(pageIndex + 1); });
    document.addEventListener("keydown", function (evt) {
      if (!pageMode || evt.ctrlKey || evt.metaKey || evt.altKey) return;
      var t = evt.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT" || t.isContentEditable)) return;
      if (evt.key === "ArrowRight") { evt.preventDefault(); goToPage(pageIndex + 1); }
      else if (evt.key === "ArrowLeft") { evt.preventDefault(); goToPage(pageIndex - 1); }
    });
    var rT = null;
    window.addEventListener("resize", function () {
      if (!pageMode) return;
      if (rT) window.clearTimeout(rT);
      rT = window.setTimeout(function () { layoutPages(true); }, 150);
    });
    // Re-paginate whenever the reader box changes size for any reason
    // (window resize, column-gutter drags, fonts loading).
    if (window.ResizeObserver) {
      var roPending = false;
      var ro = new ResizeObserver(function () {
        if (!pageMode || roPending) return;
        roPending = true;
        window.setTimeout(function () {
          roPending = false;
          layoutPages(true);
        }, 60);
      });
      ro.observe($("text-body"));
    }
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(function () { layoutPages(true); });
    }
  }

  /* ---------- boot ---------- */
  function boot() {
    loadLayout();
    applyLayout();
    wireResize();
    loadPagePrefs();
    wirePages();
    applyPageMode();
    wireLookup();
    wireReadingPosition();
    $("text-body").addEventListener("click", onAnnotatedClick);
    $("note-body").addEventListener("click", onAnnotatedClick);
    $("preview-clear").addEventListener("click", clearPreview);
    $("preview-back").addEventListener("click", function () {
      if (noteStack.length <= 1) return;
      noteStack.pop(); // drop current
      showHistoryEntry(noteStack[noteStack.length - 1]);
    });
    $("prev-chapter").addEventListener("click", function () {
      var idx = chapterList.findIndex(function (c) {
        return currentChapter && c.id === currentChapter.id;
      });
      if (idx > 0 && currentWork) {
        loadChapter(currentWork.id, chapterList[idx - 1].id, true);
      }
    });
    $("next-chapter").addEventListener("click", function () {
      var idx = chapterList.findIndex(function (c) {
        return currentChapter && c.id === currentChapter.id;
      });
      if (idx >= 0 && idx < chapterList.length - 1) {
        if (currentWork) loadChapter(currentWork.id, chapterList[idx + 1].id, true);
      }
    });
    function routeChanged() {
      var route = parseRoute();
      var work = route && workById(route.workId);
      var descriptor = work && chapterDescriptor(work.id, route.chapter);
      if (descriptor) loadChapter(work.id, descriptor.id, false);
    }
    window.addEventListener("popstate", routeChanged);
    window.addEventListener("hashchange", routeChanged);

    Promise.all([
      fetchJSON("data/works.json"),
      fetchJSON("data/info.json").catch(function () { return []; })
    ]).then(function (res) {
      works = res[0] || [];
      infoList = res[1] || [];
      if (!works.length) throw new Error("data/works.json contains no works");
      var ul = $("info-list");
      infoList.forEach(function (info) {
        var li = document.createElement("li");
        var b = document.createElement("button");
        b.textContent = info.title;
        b.addEventListener("click", function () {
          openInfo(info.id, info.title);
        });
        li.appendChild(b);
        ul.appendChild(li);
      });
      var route = parseRoute();
      var work = route && workById(route.workId);
      if (!work) work = workById(storageGet("joyce:last-work", ""));
      if (!work) work = works[0];
      var descriptor = route && work && route.workId === work.id &&
        chapterDescriptor(work.id, route.chapter);
      if (!descriptor) {
        descriptor = chapterDescriptor(work.id,
          storageGet("joyce:last-chapter:" + work.id, ""));
      }
      if (!descriptor) descriptor = (work.chapters || [])[0];
      if (descriptor) loadChapter(work.id, descriptor.id, false);
      else $("chapter-title").textContent = "No chapters found";
    }).catch(function (err) {
      $("chapter-title").textContent = "Failed to load";
      $("text-body").innerHTML = "<p>Could not load data/: " +
        String(err && err.message || err) +
        ". Serve this folder over HTTP (e.g. <code>python3 -m http.server</code>), " +
        "fetch() does not work from file://.</p>";
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
