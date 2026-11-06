/*
 * WikiHub editor surface (wikihub-k7k5).
 *
 * One editing surface shared by the page editor (editor.html) and the
 * suggest-edit flow (suggest_edit.html), rendered by _editor_surface.html:
 *
 *   Visual (Milkdown)  ·  Markdown (textarea)  ·  Preview
 *
 * The hidden-or-visible #editor-textarea is ALWAYS the source of truth that
 * the form submits. Visual mode edits the body (frontmatter held aside) and
 * writes back to the textarea only on real document changes.
 *
 * Mode resolution (design D2): ?mode=markdown|wysiwyg|visual → safety probe
 * (Visual would change the file → open Markdown with a notice) → remembered
 * per-browser choice (localStorage "wikihub.editor.mode") → Visual.
 *
 * Page-editor-only behaviour (save via API with If-Match, conflict polling,
 * visibility menu, page properties, WebMCP tool) runs when the surface root
 * has data-surface="page".
 */
(function () {
  'use strict';

  var MODE_KEY = 'wikihub.editor.mode';
  var MODES = ['visual', 'markdown', 'preview'];

  var root = document.getElementById('editor-surface');
  if (!root) return;

  var ta = document.getElementById('editor-textarea');
  var milk = document.getElementById('milkdown-editor');
  var previewWrap = document.getElementById('preview-wrap');
  var previewEl = document.getElementById('preview-article');
  var toolbar = document.getElementById('editor-toolbar');
  var notice = document.getElementById('editor-mode-notice');
  var noticeText = document.getElementById('editor-mode-notice-text');
  var noticeActions = document.getElementById('editor-mode-notice-actions');
  var modeLabel = document.getElementById('editor-mode-label');
  var countEl = document.getElementById('line-count');
  var wikilinkMenu = document.getElementById('wikilink-menu');
  var wikilinkItems = document.getElementById('wikilink-items');
  var slashMenu = document.getElementById('slash-menu');
  var wrap = root.querySelector('.editor-wrap');
  var tabs = Array.prototype.slice.call(root.querySelectorAll('.editor-tab'));

  var cfg = {
    surface: root.dataset.surface || 'page',
    bundleUrl: root.dataset.bundleUrl,
    previewUrl: root.dataset.previewUrl,
    pagesUrl: root.dataset.pagesUrl,
    excludePath: root.dataset.excludePath || '',
    serverMode: root.dataset.initialMode || 'visual',
  };

  var state = {
    mode: null,
    editor: null,         // Milkdown editor (editor.wikihub = surface API)
    storedFm: '',         // YAML frontmatter held aside in Visual mode
    milkdown: null,       // bundle module
    visualFailed: false,
    pages: null,
    pagesPromise: null,
    suggestions: [],
    suggestionIndex: -1,
    linkTarget: null,     // {kind:'textarea'} | {kind:'visual', from, to}
    slashItems: [],
    slashIndex: -1,
    slashRange: null,
    submitting: false,
    initialValue: ta.value,
    hooks: { isDirty: null, onInput: [] },
  };

  // ---------------------------------------------------------------------------
  // helpers
  // ---------------------------------------------------------------------------

  function splitFrontmatter(text) {
    // commonmark would mangle a leading ---fm--- block, so keep it out of Milkdown
    var m = (text || '').match(/^(---\r?\n[\s\S]*?\r?\n---\r?\n?)([\s\S]*)$/);
    if (m) return { fm: m[1], body: m[2] };
    return { fm: '', body: text || '' };
  }

  function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  function stripMd(path) { return path.replace(/\.md$/, ''); }

  function readStoredMode() {
    try {
      var v = window.localStorage.getItem(MODE_KEY);
      return v === 'visual' || v === 'markdown' ? v : null;
    } catch (_e) { return null; }
  }

  function writeStoredMode(mode) {
    if (mode !== 'visual' && mode !== 'markdown') return;
    try { window.localStorage.setItem(MODE_KEY, mode); } catch (_e) {}
  }

  function urlMode() {
    var v = new URLSearchParams(window.location.search).get('mode');
    if (v === 'markdown') return 'markdown';
    if (v === 'wysiwyg' || v === 'visual') return 'visual';
    return null;
  }

  function api() { return state.editor && state.editor.wikihub; }

  function fireInput() {
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // ---------------------------------------------------------------------------
  // notice bar (D3 fallback only)
  // ---------------------------------------------------------------------------

  function showNotice(message, actions) {
    noticeText.textContent = message;
    noticeActions.innerHTML = '';
    (actions || []).forEach(function (action) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'editor-banner-btn' + (action.primary ? ' primary' : '');
      btn.textContent = action.label;
      btn.addEventListener('click', action.onClick);
      noticeActions.appendChild(btn);
    });
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'editor-banner-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';
    close.addEventListener('click', hideNotice);
    noticeActions.appendChild(close);
    notice.classList.add('open');
  }

  function hideNotice() {
    notice.classList.remove('open');
    noticeText.textContent = '';
    noticeActions.innerHTML = '';
  }

  // ---------------------------------------------------------------------------
  // status bar
  // ---------------------------------------------------------------------------

  var MODE_NAMES = { visual: 'Visual', markdown: 'Markdown', preview: 'Preview' };

  function updateCount() {
    if (!countEl) return;
    if (state.mode === 'visual' && api()) {
      var text = api().textContent().trim();
      var words = text ? text.split(/\s+/).length : 0;
      countEl.textContent = words + (words === 1 ? ' word' : ' words');
    } else {
      var lines = ta.value.split('\n').length;
      countEl.textContent = lines + (lines === 1 ? ' line' : ' lines');
    }
  }

  function updateModeLabel() {
    if (!modeLabel) return;
    if (state.visualFailed && state.mode === 'markdown') {
      modeLabel.textContent = 'Visual editor unavailable; editing Markdown.';
    } else {
      modeLabel.textContent = MODE_NAMES[state.mode] || 'Markdown';
    }
  }

  // ---------------------------------------------------------------------------
  // Milkdown
  // ---------------------------------------------------------------------------

  function loadBundle() {
    if (state.milkdown) return Promise.resolve(state.milkdown);
    if (!cfg.bundleUrl) return Promise.reject(new Error('no bundle url'));
    return import(cfg.bundleUrl).then(function (mod) {
      state.milkdown = mod;
      return mod;
    });
  }

  function syncToTextarea(markdown) {
    var next = state.storedFm + markdown;
    if (next === ta.value) return;
    ta.value = next;
    fireInput();
  }

  function destroyEditor() {
    var ed = state.editor;
    state.editor = null;
    closeMenus();
    if (ed) {
      try { ed.destroy(); } catch (_e) {}
    }
    milk.innerHTML = '';
  }

  // (Re)mount Milkdown from the current textarea content.
  function mountVisual() {
    return loadBundle().then(function (mod) {
      var parts = splitFrontmatter(ta.value);
      destroyEditor();
      state.storedFm = parts.fm;
      return mod.createMilkdownEditor(milk, parts.body, syncToTextarea).then(function (editor) {
        state.editor = editor;
        updateToolbarState();
        updateCount();
        return editor;
      });
    });
  }

  function probeBody() {
    return loadBundle().then(function (mod) {
      if (!mod.probeRoundTrip) return { ok: true };
      return mod.probeRoundTrip(splitFrontmatter(ta.value).body);
    });
  }

  function flush() {
    var a = api();
    if (a && a.flush) a.flush();
  }

  // ---------------------------------------------------------------------------
  // modes
  // ---------------------------------------------------------------------------

  function setActiveTab(mode) {
    tabs.forEach(function (t) {
      var on = t.dataset.mode === mode;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
    });
    root.dataset.mode = mode;
  }

  function showSurface(mode) {
    ta.style.display = mode === 'markdown' ? 'block' : 'none';
    milk.style.display = mode === 'visual' ? 'block' : 'none';
    previewWrap.style.display = mode === 'preview' ? 'block' : 'none';
    if (toolbar) toolbar.hidden = mode !== 'visual';
  }

  function renderPreview() {
    previewEl.textContent = 'Rendering…';
    fetch(cfg.previewUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: ta.value }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) { previewEl.innerHTML = d.html; })
      .catch(function () { previewEl.textContent = 'Preview failed'; });
  }

  function enterMarkdown(opts) {
    flush();
    destroyEditor();
    state.mode = 'markdown';
    setActiveTab('markdown');
    showSurface('markdown');
    updateModeLabel();
    updateCount();
    if (opts && opts.focus) ta.focus();
  }

  function showUnsafeNotice() {
    showNotice(
      'Opened in Markdown \u2014 the visual editor would change this page\u2019s formatting.',
      [{ label: 'Open visual anyway', primary: true, onClick: function () {
        hideNotice();
        enterVisual({ force: true, focus: true });
      } }]
    );
  }

  function mountVisualNow(opts) {
    closeMenus();
    state.mode = 'visual';
    setActiveTab('visual');
    showSurface('visual');
    updateModeLabel();
    return mountVisual().then(function () {
      if (opts.focus && api()) api().focus();
      updateModeLabel();
    });
  }

  // Every entry into Visual (initial load, tab click, keyboard cycle, "Load
  // latest version") runs the round-trip probe first, unless the user
  // explicitly chose "Open visual anyway" or ?mode=visual (opts.force).
  // A failing probe keeps (or puts) the editor in Markdown with the notice.
  function enterVisual(opts) {
    opts = opts || {};
    var ready = opts.force ? Promise.resolve({ ok: true }) : probeBody();
    return ready.then(function (res) {
      if (res && res.ok) return mountVisualNow(opts);
      enterMarkdown();
      showUnsafeNotice();
      return null;
    }).catch(function (err) {
      console.error('[wikihub] visual editor failed to load', err);
      state.visualFailed = true;
      enterMarkdown();
    });
  }

  function enterPreview() {
    flush();
    destroyEditor();
    state.mode = 'preview';
    setActiveTab('preview');
    showSurface('preview');
    updateModeLabel();
    renderPreview();
  }

  // User-initiated switch (tab click, keyboard). Remembers visual/markdown.
  function switchMode(mode, opts) {
    if (mode === 'wysiwyg') mode = 'visual';
    if (mode === 'edit') mode = 'markdown';
    if (MODES.indexOf(mode) === -1) return;
    hideNotice();
    writeStoredMode(mode);
    if (mode === state.mode && mode !== 'preview') return;
    if (mode === 'visual') enterVisual({ focus: !(opts && opts.noFocus) });
    else if (mode === 'markdown') enterMarkdown({ focus: !(opts && opts.noFocus) });
    else enterPreview();
  }

  // Legacy global used by older inline handlers / tests.
  window.showTab = function (tab) { switchMode(tab); };

  function initialMode() {
    var fromUrl = urlMode();
    if (fromUrl === 'markdown') { enterMarkdown(); return; }
    if (fromUrl === 'visual') { enterVisual({ force: true }); return; }
    if (readStoredMode() === 'markdown') { enterMarkdown(); return; }
    // Visual by default, guarded by the probe. Never writes the preference.
    enterVisual();
  }

  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () { switchMode(tab.dataset.mode); });
    tab.addEventListener('keydown', function (event) {
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
      event.preventDefault();
      var i = tabs.indexOf(tab);
      var next = tabs[(i + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      next.focus();
      switchMode(next.dataset.mode, { noFocus: true });
      next.focus();
    });
  });

  document.addEventListener('keydown', function (event) {
    var mod = event.metaKey || event.ctrlKey;
    if (mod && event.shiftKey && (event.key === 'm' || event.key === 'M')) {
      event.preventDefault();
      var i = MODES.indexOf(state.mode);
      switchMode(MODES[(i + 1) % MODES.length]);
    } else if (event.key === 'Escape' && notice.classList.contains('open')) {
      hideNotice();
    }
  });

  // ---------------------------------------------------------------------------
  // toolbar
  // ---------------------------------------------------------------------------

  function toolbarItems() {
    return Array.prototype.slice.call(toolbar.querySelectorAll('[data-cmd], select'));
  }

  function runCommand(name, payload) {
    var a = api();
    if (!a) return;
    if (name === 'embed' && payload == null) {
      var target = window.prompt('Embed a file or page (e.g. diagram.png):');
      if (!target) { a.focus(); return; }
      payload = target.trim();
    }
    if (name === 'math' && payload == null) {
      var tex = window.prompt('LaTeX for the math block:');
      if (!tex) { a.focus(); return; }
      payload = tex;
    }
    a.run(name, payload);
    updateToolbarState();
  }

  function updateToolbarState() {
    if (!toolbar) return;
    var a = api();
    if (!a) return;
    var s;
    try { s = a.activeState(); } catch (_e) { return; }
    toolbar.querySelectorAll('[data-cmd]').forEach(function (btn) {
      var key = btn.dataset.cmd;
      if (key in s && typeof s[key] === 'boolean') {
        btn.setAttribute('aria-pressed', s[key] ? 'true' : 'false');
      }
    });
    var sel = toolbar.querySelector('select[data-heading]');
    if (sel) sel.value = String(s.heading || 0);
  }

  if (toolbar) {
    toolbar.addEventListener('mousedown', function (event) {
      // keep the editor selection when clicking buttons
      if (event.target.closest('button[data-cmd]')) event.preventDefault();
    });
    toolbar.addEventListener('click', function (event) {
      var btn = event.target.closest('button[data-cmd]');
      if (!btn) return;
      runCommand(btn.dataset.cmd);
    });
    var headingSelect = toolbar.querySelector('select[data-heading]');
    if (headingSelect) {
      headingSelect.addEventListener('change', function () {
        var level = parseInt(headingSelect.value, 10) || 0;
        runCommand(level ? 'h' + level : 'paragraph');
      });
    }
    // roving tabindex
    var items = toolbarItems();
    items.forEach(function (el, i) { el.tabIndex = i === 0 ? 0 : -1; });
    toolbar.addEventListener('keydown', function (event) {
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
      if (event.target.tagName === 'SELECT') return;
      var list = toolbarItems();
      var i = list.indexOf(event.target);
      if (i === -1) return;
      event.preventDefault();
      var next = list[(i + (event.key === 'ArrowRight' ? 1 : list.length - 1)) % list.length];
      list.forEach(function (el) { el.tabIndex = -1; });
      next.tabIndex = 0;
      next.focus();
    });
  }

  // Task-list checkboxes are drawn with ::before in the gutter left of the
  // item; a click there toggles the item.
  milk.addEventListener('mousedown', function (event) {
    var li = event.target.closest && event.target.closest('li[data-item-type="task"]');
    if (!li || !api()) return;
    var rect = li.getBoundingClientRect();
    if (event.clientX >= rect.left) return;
    event.preventDefault();
    api().toggleTaskAt(li);
  });

  milk.addEventListener('wikihub:selection', function () {
    updateToolbarState();
    updateCount();
  });

  // ---------------------------------------------------------------------------
  // wikilink autocomplete (textarea + Visual)
  // ---------------------------------------------------------------------------

  function ensurePages() {
    if (state.pages) return Promise.resolve(state.pages);
    if (state.pagesPromise) return state.pagesPromise;
    state.pagesPromise = fetch(cfg.pagesUrl)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.pages = (data.pages || []).map(function (page) {
          return {
            title: page.title || stripMd(page.path).split('/').pop(),
            path: page.path,
            target: stripMd(page.path),
          };
        });
        return state.pages;
      })
      .catch(function () { state.pages = []; return state.pages; });
    return state.pagesPromise;
  }

  function rankSuggestions(pages, query) {
    query = (query || '').trim().toLowerCase();
    var scored = pages
      .filter(function (page) { return page.path !== cfg.excludePath; })
      .map(function (page) {
        var title = (page.title || '').toLowerCase();
        var path = page.target.toLowerCase();
        var score = 0;
        if (!query) score = 1;
        else if (title === query || path === query) score = 100;
        else if (title.indexOf(query) === 0 || path.indexOf(query) === 0) score = 70;
        else if (title.indexOf(query) !== -1) score = 50;
        else if (path.indexOf(query) !== -1) score = 40;
        return { page: page, score: score };
      })
      .filter(function (item) { return item.score > 0; })
      .sort(function (a, b) { return b.score - a.score || a.page.title.localeCompare(b.page.title); });
    return scored.slice(0, 8).map(function (item) { return item.page; });
  }

  function positionMenu(menu, coords) {
    if (!coords) {
      menu.style.top = '';
      menu.style.left = '';
      menu.style.bottom = '';
      menu.classList.remove('anchored');
      return;
    }
    var box = wrap.getBoundingClientRect();
    var top = coords.bottom - box.top + wrap.scrollTop + 6;
    var left = Math.max(8, Math.min(coords.left - box.left, box.width - 340));
    menu.classList.add('anchored');
    menu.style.bottom = 'auto';
    menu.style.top = top + 'px';
    menu.style.left = left + 'px';
  }

  function setMenuOpenFlag() {
    var open = (wikilinkMenu.classList.contains('open') && state.suggestions.length > 0) ||
      (slashMenu && slashMenu.classList.contains('open') && state.slashItems.length > 0);
    milk.dataset.menuOpen = open ? '1' : '0';
  }

  function closeWikilinkMenu() {
    wikilinkMenu.classList.remove('open');
    wikilinkItems.innerHTML = '';
    state.suggestions = [];
    state.suggestionIndex = -1;
    state.linkTarget = null;
    setMenuOpenFlag();
  }

  function renderWikilinkSuggestions() {
    if (!state.suggestions.length) {
      wikilinkItems.innerHTML = '<div class="wikilink-menu-empty">No matching pages in this wiki.</div>';
      wikilinkMenu.classList.add('open');
      setMenuOpenFlag();
      return;
    }
    wikilinkItems.innerHTML = '';
    state.suggestions.forEach(function (page, index) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.setAttribute('role', 'option');
      btn.className = 'wikilink-item' + (index === state.suggestionIndex ? ' active' : '');
      btn.setAttribute('aria-selected', index === state.suggestionIndex ? 'true' : 'false');
      btn.innerHTML =
        '<span class="wikilink-item-main">' +
          '<span class="wikilink-item-title">' + escapeHtml(page.title) + '</span>' +
          '<span class="wikilink-item-path">' + escapeHtml(page.target) + '</span>' +
        '</span>';
      btn.addEventListener('mousedown', function (event) {
        event.preventDefault();
        insertWikilink(page);
      });
      wikilinkItems.appendChild(btn);
    });
    wikilinkMenu.classList.add('open');
    setMenuOpenFlag();
  }

  function currentTextareaWikilinkContext() {
    var caret = ta.selectionStart;
    var before = ta.value.slice(0, caret);
    var match = before.match(/\[\[([^\]\n]*)$/);
    if (!match) return null;
    return { query: match[1], start: caret - match[0].length, end: caret };
  }

  function insertWikilink(page) {
    var target = state.linkTarget;
    if (!target) return;
    if (target.kind === 'visual') {
      var a = api();
      if (a) {
        a.insertWikilink({
          target: page.target,
          alias: page.title && page.title !== page.target ? page.title : null,
          from: target.from,
          to: target.to,
        });
      }
      closeWikilinkMenu();
      return;
    }
    var ctx = currentTextareaWikilinkContext();
    if (!ctx) return;
    var insertion = page.title && page.title !== page.target
      ? '[[' + page.target + '|' + page.title + ']]'
      : '[[' + page.target + ']]';
    ta.setRangeText(insertion, ctx.start, ctx.end, 'end');
    closeWikilinkMenu();
    ta.focus();
    fireInput();
  }

  function openWikilinkMenu(query, linkTarget, coords) {
    state.linkTarget = linkTarget;
    return ensurePages().then(function (pages) {
      if (state.linkTarget !== linkTarget) return;
      state.suggestions = rankSuggestions(pages, query);
      state.suggestionIndex = state.suggestions.length ? 0 : -1;
      positionMenu(wikilinkMenu, coords);
      renderWikilinkSuggestions();
    });
  }

  function updateTextareaAutocomplete() {
    if (state.mode !== 'markdown') return;
    var ctx = currentTextareaWikilinkContext();
    if (!ctx) { closeWikilinkMenu(); return; }
    openWikilinkMenu(ctx.query, { kind: 'textarea' }, null);
  }

  function moveSelection(listKey, indexKey, delta, render) {
    var len = state[listKey].length;
    if (!len) return;
    state[indexKey] = Math.max(0, Math.min(state[indexKey] + delta, len - 1));
    render();
  }

  ta.addEventListener('keydown', function (event) {
    if (!wikilinkMenu.classList.contains('open') || !state.suggestions.length) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveSelection('suggestions', 'suggestionIndex', 1, renderWikilinkSuggestions);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveSelection('suggestions', 'suggestionIndex', -1, renderWikilinkSuggestions);
    } else if ((event.key === 'Enter' || event.key === 'Tab') && state.suggestionIndex >= 0) {
      event.preventDefault();
      insertWikilink(state.suggestions[state.suggestionIndex]);
    } else if (event.key === 'Escape') {
      closeWikilinkMenu();
    }
  });
  ta.addEventListener('input', updateTextareaAutocomplete);
  ta.addEventListener('click', updateTextareaAutocomplete);
  ta.addEventListener('keyup', function (event) {
    if (['ArrowUp', 'ArrowDown', 'Enter', 'Tab', 'Escape'].indexOf(event.key) !== -1) return;
    updateTextareaAutocomplete();
  });
  ta.addEventListener('blur', function () { setTimeout(closeWikilinkMenu, 150); });

  // ---------------------------------------------------------------------------
  // slash menu (Visual)
  // ---------------------------------------------------------------------------

  var SLASH_ITEMS = [
    { label: 'Heading 1', cmd: 'h1', keys: 'h1 heading title' },
    { label: 'Heading 2', cmd: 'h2', keys: 'h2 heading subtitle' },
    { label: 'Heading 3', cmd: 'h3', keys: 'h3 heading' },
    { label: 'Bulleted list', cmd: 'bullet', keys: 'bullet list ul unordered' },
    { label: 'Numbered list', cmd: 'ordered', keys: 'numbered ordered list ol' },
    { label: 'Task list', cmd: 'task', keys: 'task todo checkbox list' },
    { label: 'Quote', cmd: 'quote', keys: 'quote blockquote' },
    { label: 'Code block', cmd: 'codeblock', keys: 'code block pre' },
    { label: 'Table', cmd: 'table', keys: 'table grid' },
    { label: 'Divider', cmd: 'divider', keys: 'divider rule hr line' },
    { label: 'Math block', cmd: 'math', keys: 'math latex equation formula' },
    { label: 'Link to page', cmd: 'pagelink', keys: 'link page wikilink' },
    { label: 'Embed', cmd: 'embed', keys: 'embed image file' },
  ];

  function filterSlash(query) {
    var q = (query || '').trim().toLowerCase();
    if (!q) return SLASH_ITEMS.slice();
    return SLASH_ITEMS.filter(function (item) {
      return item.label.toLowerCase().indexOf(q) !== -1 || item.keys.indexOf(q) !== -1;
    });
  }

  function closeSlashMenu() {
    if (!slashMenu) return;
    slashMenu.classList.remove('open');
    slashMenu.innerHTML = '';
    state.slashItems = [];
    state.slashIndex = -1;
    state.slashRange = null;
    setMenuOpenFlag();
  }

  function renderSlashMenu() {
    slashMenu.innerHTML = '';
    if (!state.slashItems.length) {
      slashMenu.innerHTML = '<div class="wikilink-menu-empty">No matching blocks.</div>';
    }
    state.slashItems.forEach(function (item, index) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.setAttribute('role', 'option');
      btn.className = 'slash-item' + (index === state.slashIndex ? ' active' : '');
      btn.setAttribute('aria-selected', index === state.slashIndex ? 'true' : 'false');
      btn.textContent = item.label;
      btn.addEventListener('mousedown', function (event) {
        event.preventDefault();
        applySlash(item);
      });
      slashMenu.appendChild(btn);
    });
    slashMenu.classList.add('open');
    setMenuOpenFlag();
  }

  function applySlash(item) {
    var range = state.slashRange;
    var a = api();
    closeSlashMenu();
    if (!a || !range) return;
    a.replaceRange(range.from, range.to, null);
    runCommand(item.cmd);
  }

  function closeMenus() {
    closeWikilinkMenu();
    closeSlashMenu();
  }

  milk.addEventListener('wikihub:trigger', function (event) {
    var d = event.detail || {};
    if (d.kind === 'wikilink') {
      closeSlashMenu();
      openWikilinkMenu(d.query, { kind: 'visual', from: d.from, to: d.to }, d.coords);
    } else if (d.kind === 'slash') {
      closeWikilinkMenu();
      state.slashRange = { from: d.from, to: d.to };
      state.slashItems = filterSlash(d.query);
      state.slashIndex = state.slashItems.length ? 0 : -1;
      positionMenu(slashMenu, d.coords);
      renderSlashMenu();
    } else {
      closeMenus();
    }
  });

  milk.addEventListener('wikihub:menukey', function (event) {
    var key = event.detail && event.detail.key;
    var slashOpen = slashMenu && slashMenu.classList.contains('open') && state.slashItems.length;
    if (slashOpen) {
      if (key === 'ArrowDown') moveSelection('slashItems', 'slashIndex', 1, renderSlashMenu);
      else if (key === 'ArrowUp') moveSelection('slashItems', 'slashIndex', -1, renderSlashMenu);
      else if (key === 'Enter' || key === 'Tab') applySlash(state.slashItems[state.slashIndex]);
      else if (key === 'Escape') closeSlashMenu();
      return;
    }
    if (key === 'ArrowDown') moveSelection('suggestions', 'suggestionIndex', 1, renderWikilinkSuggestions);
    else if (key === 'ArrowUp') moveSelection('suggestions', 'suggestionIndex', -1, renderWikilinkSuggestions);
    else if ((key === 'Enter' || key === 'Tab') && state.suggestionIndex >= 0) insertWikilink(state.suggestions[state.suggestionIndex]);
    else if (key === 'Escape') closeWikilinkMenu();
  });

  milk.addEventListener('focusout', function () {
    setTimeout(function () {
      if (!milk.contains(document.activeElement)) closeMenus();
    }, 150);
  });

  // ---------------------------------------------------------------------------
  // dirty tracking + beforeunload guard
  // ---------------------------------------------------------------------------

  function isDirty() {
    if (state.hooks.isDirty) return state.hooks.isDirty();
    return ta.value !== state.initialValue;
  }

  ta.addEventListener('input', function () {
    updateCount();
    state.hooks.onInput.forEach(function (fn) { fn(); });
  });

  window.addEventListener('beforeunload', function (event) {
    flush();
    if (state.submitting || !isDirty()) return;
    event.preventDefault();
    event.returnValue = '';
  });

  // ---------------------------------------------------------------------------
  // public surface API (used by the page/suggest scripts below)
  // ---------------------------------------------------------------------------

  var surface = {
    flush: flush,
    mode: function () { return state.mode; },
    switchMode: switchMode,
    isDirty: isDirty,
    setDirtyCheck: function (fn) { state.hooks.isDirty = fn; },
    onInput: function (fn) { state.hooks.onInput.push(fn); },
    markSubmitting: function (v) { state.submitting = v !== false; },
    // Replace the textarea content (e.g. "Load latest version") and re-mount
    // Visual so it shows the new content.
    setContent: function (value) {
      ta.value = value;
      updateCount();
      if (state.mode === 'visual') return enterVisual();
      return Promise.resolve();
    },
    // Frontmatter was edited outside the body (page properties): refresh the
    // copy Visual mode re-attaches on sync.
    refreshFrontmatter: function () {
      state.storedFm = splitFrontmatter(ta.value).fm;
    },
    splitFrontmatter: splitFrontmatter,
  };
  window.WikihubEditorSurface = surface;

  // ---------------------------------------------------------------------------
  // page editor (data-surface="page")
  // ---------------------------------------------------------------------------

  if (cfg.surface === 'page') initPageEditor(surface);
  if (cfg.surface === 'suggest') initSuggest(surface);

  updateCount();
  initialMode();

  function initSuggest(surface) {
    var form = ta.form;
    if (!form) return;
    var status = document.getElementById('save-status');
    surface.onInput(function () {
      if (status) status.textContent = surface.isDirty() ? 'Unsaved changes' : 'Ready';
    });
    form.addEventListener('submit', function () {
      surface.flush();
      surface.markSubmitting(true);
    });
    document.addEventListener('keydown', function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        surface.flush();
        form.requestSubmit();
      }
    });
  }

  function initPageEditor(surface) {
    var configEl = document.getElementById('editor-config');
    var editorConfig = configEl ? JSON.parse(configEl.textContent) : {};
    var form = document.getElementById('editor-form');
    var visibilityInput = document.getElementById('visibility-input');
    var pathInput = document.getElementById('path-input');
    var saveStatus = document.getElementById('save-status');
    var saveButton = form.querySelector('.btn-save');
    var banner = document.getElementById('editor-banner');
    var bannerText = document.getElementById('editor-banner-text');
    var bannerActions = document.getElementById('editor-banner-actions');

    if (editorConfig.isOwner) {
      // Private sections (Markdown tab): Cmd/Ctrl+Shift+P wraps the selected
      // lines (or the current line) in owner-only <!-- private --> markers, each
      // on its own line. Non-owner editors never load band markers.
      ta.addEventListener('keydown', function (event) {
        if (!((event.metaKey || event.ctrlKey) && event.shiftKey && (event.key === 'p' || event.key === 'P'))) return;
        event.preventDefault();
        var value = ta.value;
        var lineStart = value.lastIndexOf('\n', ta.selectionStart - 1) + 1;
        var lineEnd = value.indexOf('\n', Math.max(ta.selectionEnd - (ta.selectionEnd > ta.selectionStart ? 1 : 0), ta.selectionStart));
        if (lineEnd === -1) lineEnd = value.length;
        var block = value.slice(lineStart, lineEnd);
        ta.setRangeText('<!-- private -->\n' + block + '\n<!-- /private -->', lineStart, lineEnd, 'end');
        ta.selectionStart = lineStart + '<!-- private -->\n'.length;
        ta.selectionEnd = ta.selectionStart + block.length;
        ta.dispatchEvent(new Event('input', { bubbles: true }));
      });
    }

    var editorState = {
      baseContent: ta.value,
      baseVisibility: visibilityInput.value,
      remoteHash: editorConfig.initialHash,
      pendingRemote: null,
      saving: false,
    };

    window.setVisibility = function (v) {
      visibilityInput.value = v;
      document.getElementById('vis-label').textContent = v;
      document.querySelectorAll('.vis-option').forEach(function (opt) {
        opt.classList.toggle('selected', opt.textContent.trim() === v);
      });
      document.getElementById('vis-menu').classList.remove('open');
      updateSaveStatus();
    };

    function normalizePath(path) {
      var trimmed = (path || '').trim();
      if (!trimmed) return editorConfig.initialPath;
      return /\.md$/.test(trimmed) ? trimmed : trimmed + '.md';
    }

    function encodedPath(path) {
      return path.split('/').map(encodeURIComponent).join('/');
    }

    function pageIsDirty() {
      var pathDirty = pathInput ? normalizePath(pathInput.value) !== editorConfig.initialPath : false;
      return ta.value !== editorState.baseContent || visibilityInput.value !== editorState.baseVisibility || pathDirty;
    }
    surface.setDirtyCheck(pageIsDirty);

    function updateSaveStatus(text) {
      if (text) { saveStatus.textContent = text; return; }
      if (editorState.saving) saveStatus.textContent = 'Saving…';
      else if (editorState.pendingRemote) saveStatus.textContent = 'Conflict detected';
      else if (pageIsDirty()) saveStatus.textContent = 'Unsaved changes';
      else saveStatus.textContent = 'Ready';
    }

    function setSaveButtonLoading(loading) {
      editorState.saving = loading;
      saveButton.classList.toggle('btn-loading', loading);
      saveButton.disabled = loading;
      saveButton.textContent = '';
      if (loading) {
        var sp = document.createElement('span');
        sp.className = 'wh-spinner';
        saveButton.appendChild(sp);
        saveButton.appendChild(document.createTextNode('Saving...'));
      } else {
        saveButton.textContent = 'Save';
      }
      updateSaveStatus();
    }

    function setBanner(message, actions, kind) {
      banner.classList.add('open');
      banner.classList.toggle('conflict', kind === 'conflict');
      banner.classList.toggle('sync', kind !== 'conflict');
      bannerText.textContent = message;
      bannerActions.innerHTML = '';
      (actions || []).forEach(function (action) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'editor-banner-btn' + (action.primary ? ' primary' : '');
        btn.textContent = action.label;
        btn.addEventListener('click', action.onClick);
        bannerActions.appendChild(btn);
      });
    }

    function clearBanner() {
      banner.classList.remove('open', 'conflict', 'sync');
      bannerText.textContent = '';
      bannerActions.innerHTML = '';
    }

    function readRemotePage(path) {
      return fetch('/api/v1/wikis/' + encodeURIComponent(editorConfig.owner) + '/' + encodeURIComponent(editorConfig.slug) + '/pages/' + encodedPath(path))
        .then(function (response) {
          if (!response.ok) throw new Error('Failed to fetch latest page state');
          return response.json();
        });
    }

    function applyRemoteState(remote) {
      surface.setContent(remote.content || '');
      if (editorConfig.isOwner && remote.visibility) window.setVisibility(remote.visibility);
      editorState.baseContent = ta.value;
      editorState.baseVisibility = visibilityInput.value;
      editorState.remoteHash = remote.content_hash || null;
      editorState.pendingRemote = null;
      clearBanner();
      refreshProperties();
      updateSaveStatus('Loaded latest version');
    }

    function tryAutoMerge(base, local, remote) {
      if (local === base) return { content: remote };
      if (remote === base) return { content: local };
      if (local.indexOf(base) === 0 && remote.indexOf(base) === 0) {
        var localTail = local.slice(base.length);
        var remoteTail = remote.slice(base.length);
        return { content: base + remoteTail + (remoteTail && localTail ? '\n' : '') + localTail };
      }
      if (local.indexOf(base) === 0 && remote.indexOf(local.slice(base.length)) === -1) {
        return { content: remote + local.slice(base.length) };
      }
      if (remote.indexOf(base) === 0 && local.indexOf(remote.slice(base.length)) === -1) {
        return { content: local + remote.slice(base.length) };
      }
      return { conflict: true };
    }

    function saveExistingPage(mode) {
      surface.flush();
      var payload = { content: ta.value, visibility: visibilityInput.value };
      if (editorConfig.isOwner && pathInput) {
        var nextPath = normalizePath(pathInput.value);
        if (nextPath !== editorConfig.initialPath) payload.new_path = nextPath;
      }
      var headers = { 'Content-Type': 'application/json' };
      if (mode === 'normal' && editorState.remoteHash) {
        headers['If-Match'] = '"' + editorState.remoteHash + '"';
      }
      if (mode === 'merge' && editorState.pendingRemote && editorState.pendingRemote.content_hash) {
        headers['If-Match'] = '"' + editorState.pendingRemote.content_hash + '"';
      }
      setSaveButtonLoading(true);
      return fetch(
        '/api/v1/wikis/' + encodeURIComponent(editorConfig.owner) + '/' + encodeURIComponent(editorConfig.slug) + '/pages/' + encodedPath(editorConfig.initialPath),
        { method: 'PUT', headers: headers, body: JSON.stringify(payload) }
      ).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (response.ok) {
            var targetPath = data.path || payload.new_path || editorConfig.initialPath;
            surface.markSubmitting(true);
            window.location.href = '/@' + editorConfig.owner + '/' + editorConfig.slug + '/' + targetPath.replace(/\.md$/, '').split('/').map(encodeURIComponent).join('/');
            return;
          }
          if (response.status === 409 && data.reason === 'private_bands') {
            // The owner's private sections overlap this edit; the server saved it
            // as a suggestion instead of writing (nothing is lost).
            setBanner(data.message || 'Saved as a suggestion.', data.proposal_url ? [
              { label: 'View suggestion', onClick: function () { window.location.href = data.proposal_url; }, primary: true },
            ] : [], 'conflict');
            return;
          }
          if (response.status === 409) {
            return readRemotePage(editorConfig.initialPath).then(function (remote) {
              editorState.pendingRemote = remote;
              setBanner(
                'This page changed elsewhere while you were editing. Choose how to resolve it.',
                [
                  { label: 'Discard local changes', onClick: discardLocalChanges },
                  { label: 'Auto-merge to history', onClick: autoMergeChanges, primary: true },
                  { label: 'Overwrite remote', onClick: overwriteRemoteVersion },
                ],
                'conflict'
              );
            });
          }
          setBanner(data.message || 'Save failed.', [], 'conflict');
        });
      }).catch(function () {
        setBanner('Save failed because the latest page state could not be reached.', [], 'conflict');
      }).then(function () {
        setSaveButtonLoading(false);
        updateSaveStatus();
      });
    }

    function discardLocalChanges() {
      var p = editorState.pendingRemote ? Promise.resolve(editorState.pendingRemote) : readRemotePage(editorConfig.initialPath);
      return p.then(applyRemoteState);
    }

    function overwriteRemoteVersion() {
      editorState.pendingRemote = null;
      clearBanner();
      return saveExistingPage('overwrite');
    }

    function autoMergeChanges() {
      surface.flush();
      var p = editorState.pendingRemote ? Promise.resolve(editorState.pendingRemote) : readRemotePage(editorConfig.initialPath);
      return p.then(function (remote) {
        var merged = tryAutoMerge(editorState.baseContent, ta.value, remote.content || '');
        if (merged.conflict) {
          setBanner(
            'Automatic merge could not confidently combine both versions. Reload latest or overwrite if you intend to replace it.',
            [
              { label: 'Discard local changes', onClick: discardLocalChanges },
              { label: 'Overwrite remote', onClick: overwriteRemoteVersion, primary: true },
            ],
            'conflict'
          );
          return;
        }
        ta.value = merged.content;
        editorState.pendingRemote = remote;
        updateSaveStatus('Merged locally; saving…');
        return saveExistingPage('merge');
      });
    }

    function pollForRemoteChanges() {
      if (!editorConfig.existingPage || !editorState.remoteHash || editorState.saving) return;
      readRemotePage(editorConfig.initialPath).then(function (remote) {
        if (remote.content_hash && remote.content_hash !== editorState.remoteHash) {
          editorState.pendingRemote = remote;
          var dirty = pageIsDirty();
          setBanner(
            dirty ? 'A newer version was saved elsewhere while you have local changes.' : 'A newer version was saved elsewhere.',
            dirty
              ? [
                  { label: 'Discard local changes', onClick: discardLocalChanges },
                  { label: 'Auto-merge to history', onClick: autoMergeChanges, primary: true },
                  { label: 'Overwrite remote', onClick: overwriteRemoteVersion },
                ]
              : [{ label: 'Load latest version', onClick: discardLocalChanges, primary: true }],
            dirty ? 'conflict' : 'sync'
          );
          updateSaveStatus();
        }
      }).catch(function () { /* transient; manual save still surfaces conflicts */ });
    }

    // ---- page properties (owner only): title / pinned / tags ---------------

    var propsEl = document.getElementById('page-properties');
    var propTitle = document.getElementById('prop-title');
    var propPinned = document.getElementById('prop-pinned');
    var propTags = document.getElementById('prop-tags');

    function fmLines(fm) {
      var m = fm.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?$/);
      return m ? m[1].split(/\r?\n/) : [];
    }

    function unquote(v) {
      v = (v || '').trim();
      if ((v[0] === '"' && v[v.length - 1] === '"') || (v[0] === "'" && v[v.length - 1] === "'")) {
        try { return v[0] === '"' ? JSON.parse(v) : v.slice(1, -1).replace(/''/g, "'"); } catch (_e) { return v.slice(1, -1); }
      }
      return v;
    }

    // Find `key:` at top level; returns {start, end} line indexes (end exclusive)
    // covering the key line plus any indented / `- ` continuation lines.
    function findKey(lines, key) {
      var re = new RegExp('^' + key + '\\s*:', 'i');
      for (var i = 0; i < lines.length; i++) {
        if (re.test(lines[i])) {
          var j = i + 1;
          while (j < lines.length && /^(\s+\S|\s*-\s)/.test(lines[j])) j++;
          return { start: i, end: j };
        }
      }
      return null;
    }

    function readFm(key) {
      var lines = fmLines(splitFrontmatter(ta.value).fm);
      var hit = findKey(lines, key);
      if (!hit) return null;
      var first = lines[hit.start].replace(/^[^:]*:\s*/, '');
      if (key === 'tags') {
        if (first.trim().charAt(0) === '[') {
          return first.trim().slice(1, -1).split(',').map(unquote).filter(Boolean);
        }
        if (first.trim()) return first.split(',').map(unquote).filter(Boolean);
        return lines.slice(hit.start + 1, hit.end)
          .map(function (l) { return unquote(l.replace(/^\s*-\s*/, '')); })
          .filter(Boolean);
      }
      return unquote(first);
    }

    function yamlScalar(v) {
      return /^[\w][\w .,'()&/-]*$/.test(v) && !/^(true|false|null|yes|no|on|off|~)$/i.test(v) && !/:\s|\s#/.test(v)
        ? v
        : JSON.stringify(v);
    }

    // Same semantics as content_utils.upsert_frontmatter_value: null removes.
    function upsertFm(key, valueLine) {
      var parts = splitFrontmatter(ta.value);
      var lines = fmLines(parts.fm);
      var hit = findKey(lines, key);
      if (valueLine == null) {
        if (hit) lines.splice(hit.start, hit.end - hit.start);
      } else if (hit) {
        lines.splice(hit.start, hit.end - hit.start, key + ': ' + valueLine);
      } else {
        lines.push(key + ': ' + valueLine);
      }
      var fm = lines.length ? '---\n' + lines.join('\n') + '\n---\n' : '';
      var body = parts.body;
      if (fm && !parts.fm && body && body.charAt(0) !== '\n') body = '\n' + body;
      ta.value = fm + body;
      surface.refreshFrontmatter();
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function refreshProperties() {
      if (!propsEl) return;
      var title = readFm('title');
      var pinned = readFm('pinned');
      var tags = readFm('tags');
      propTitle.value = title || '';
      propPinned.checked = String(pinned).toLowerCase() === 'true';
      propTags.value = Array.isArray(tags) ? tags.join(', ') : '';
    }

    if (propsEl) {
      refreshProperties();
      propsEl.addEventListener('toggle', refreshProperties);
      propTitle.addEventListener('change', function () {
        var v = propTitle.value.trim();
        upsertFm('title', v ? yamlScalar(v) : null);
      });
      propPinned.addEventListener('change', function () {
        upsertFm('pinned', propPinned.checked ? 'true' : null);
      });
      propTags.addEventListener('change', function () {
        var tags = propTags.value.split(',').map(function (t) { return t.trim().replace(/^#/, ''); }).filter(Boolean);
        upsertFm('tags', tags.length ? '[' + tags.map(yamlScalar).join(', ') + ']' : null);
      });
    }

    // ---- wiring -------------------------------------------------------------

    surface.onInput(function () { updateSaveStatus(); });
    if (pathInput) pathInput.addEventListener('input', function () { updateSaveStatus(); });
    updateSaveStatus();

    if (navigator.modelContext && typeof navigator.modelContext.registerTool === 'function') {
      navigator.modelContext.registerTool({
        name: 'save_wikihub_page',
        description: 'Save the current wikihub page using the logged-in browser session.',
        inputSchema: {
          type: 'object',
          properties: { content: { type: 'string' }, visibility: { type: 'string' } },
          required: ['content'],
        },
        handler: function (args) {
          surface.flush();
          ta.value = args.content;
          if (args.visibility) window.setVisibility(args.visibility);
          form.requestSubmit();
          return { status: 'submitted' };
        },
      });
    }

    form.addEventListener('submit', function (event) {
      surface.flush();
      if (!editorConfig.existingPage) {
        surface.markSubmitting(true);
        setSaveButtonLoading(true);
        return;
      }
      event.preventDefault();
      saveExistingPage('normal');
    });

    document.addEventListener('keydown', function (event) {
      if ((event.metaKey || event.ctrlKey) && (event.key === 's' || event.key === 'S') && !event.shiftKey) {
        event.preventDefault();
        form.requestSubmit();
      }
    });

    setInterval(pollForRemoteChanges, 15000);
  }
})();
