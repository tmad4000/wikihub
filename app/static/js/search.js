// Cmd+K omnisearch modal for wikihub
(function() {
  const modal = document.getElementById('search-modal');
  const overlay = document.getElementById('search-overlay');
  const input = document.getElementById('search-input');
  const results = document.getElementById('search-results');
  const scopeContainer = document.getElementById('search-scope');
  const closeBtn = document.getElementById('search-close');
  if (!modal) return;

  let debounceTimer;
  let activeIndex = 0;
  let items = [];
  let searchId = 0; // track request freshness
  let savedScrollY = 0; // restore body scroll position on close
  let didPushState = false; // track whether we pushed history (wikihub-zlgt: mobile back-button)

  // --- scope detection ---
  let currentScope = null; // { type: 'wiki'|'author', owner, slug? }
  let originalScope = null; // remember initial scope for re-scoping

  function detectScope() {
    const host = window.location.host;
    const path = window.location.pathname;

    // per-user subdomain: <owner>.wikihub.md/<slug>/<page-path>
    // (e.g. jacobcole.wikihub.md/systematic-awesome/thoughtfulweb
    //       -> owner=jacobcole, slug=systematic-awesome)
    // The subdomain label is the OWNER (username); the first path segment is the WIKI SLUG.
    // (wikihub-icbe: this was previously swapped, mislabeling the scope pill and
    //  breaking scoped search on subdomain URLs.)
    const subMatch = host.match(/^([\w-]+)\.wikihub\.md/i);
    if (subMatch) {
      const owner = subMatch[1];
      // Skip the bare wikihub.md / www case (no real subdomain wiki context)
      if (owner.toLowerCase() !== 'www') {
        const m = path.match(/^\/([\w-]+)/);
        if (m) {
          return { type: 'wiki', owner: owner, slug: m[1] };
        }
        // subdomain root (e.g. jacobcole.wikihub.md/) — scope to this author so
        // "search within my stuff" works from the landing page too.
        return { type: 'author', owner: owner };
      }
    }

    // wiki page: /@owner/slug/...
    const wikiMatch = path.match(/^\/@([\w-]+)\/([\w-]+)/);
    if (wikiMatch) {
      return { type: 'wiki', owner: wikiMatch[1], slug: wikiMatch[2] };
    }
    // profile page: /@owner (but not /@owner/slug)
    const profileMatch = path.match(/^\/@([\w-]+)\/?$/);
    if (profileMatch) {
      return { type: 'author', owner: profileMatch[1] };
    }
    return null;
  }

  function scopedPlaceholder() {
    return currentScope ? 'Search this wiki…' : 'Search wikihub…';
  }

  function renderScope() {
    scopeContainer.textContent = '';
    if (currentScope) {
      scopeContainer.classList.add('active');
      const pill = document.createElement('span');
      pill.className = 'search-scope-pill';
      if (currentScope.type === 'wiki') {
        pill.textContent = 'in @' + currentScope.owner + '/' + currentScope.slug + ' ';
      } else {
        pill.textContent = 'by @' + currentScope.owner + ' ';
      }
      const dismiss = document.createElement('button');
      dismiss.textContent = '×';
      dismiss.title = 'Search all wikis';
      dismiss.addEventListener('click', (e) => {
        e.preventDefault();
        clearScope();
        input.focus();
      });
      pill.appendChild(dismiss);
      scopeContainer.appendChild(pill);
      input.placeholder = 'Search this wiki…';
    } else if (originalScope) {
      // show "All wikis" with option to re-scope
      scopeContainer.classList.add('active');
      const label = document.createElement('span');
      label.className = 'search-scope-global';
      if (originalScope.type === 'wiki') {
        label.textContent = 'All wikis — click to scope to @' + originalScope.owner + '/' + originalScope.slug;
      } else {
        label.textContent = 'All wikis — click to scope to @' + originalScope.owner;
      }
      label.addEventListener('click', (e) => {
        e.preventDefault();
        currentScope = { ...originalScope };
        renderScope();
        doSearch();
        input.focus();
      });
      scopeContainer.appendChild(label);
      input.placeholder = 'Search all wikis…';
    } else {
      scopeContainer.classList.remove('active');
      input.placeholder = 'Search wikihub…';
    }
  }

  function clearScope() {
    currentScope = null;
    renderScope();
    doSearch();
  }

  function renderEmptyState() {
    results.textContent = '';
    const hint = document.createElement('div');
    hint.className = 'search-empty search-empty-hint';
    if (currentScope && currentScope.type === 'wiki') {
      hint.textContent = 'Type to search @' + currentScope.owner + '/' + currentScope.slug + '…';
    } else if (currentScope && currentScope.type === 'author') {
      hint.textContent = 'Type to search @' + currentScope.owner + '…';
    } else {
      hint.textContent = 'Type to search wikihub…';
    }
    results.appendChild(hint);
  }

  function open() {
    if (modal.classList.contains('open')) return;
    modal.classList.add('open');
    overlay.classList.add('open');
    input.value = '';
    items = [];
    activeIndex = 0;
    // detect scope from current URL
    originalScope = detectScope();
    currentScope = originalScope ? { ...originalScope } : null;
    renderScope();
    renderEmptyState();
    // lock body scroll while modal is open (wikihub-zlgt mobile fix #3)
    savedScrollY = window.scrollY || window.pageYOffset || 0;
    document.body.style.overflow = 'hidden';
    // push history state so system back-button closes modal (wikihub-zlgt fix #4)
    try {
      history.pushState({ wikihubSearch: true }, '', '');
      didPushState = true;
    } catch (_) {
      didPushState = false;
    }
    // focus the input immediately so the mobile keyboard raises (wikihub-zlgt fix #2)
    // Use rAF + setTimeout for iOS Safari which needs a tick after layout.
    requestAnimationFrame(() => {
      input.focus();
      // Belt-and-suspenders for iOS — sometimes the first focus doesn't take.
      setTimeout(() => { if (document.activeElement !== input) input.focus(); }, 50);
    });
  }

  function close(opts) {
    if (!modal.classList.contains('open')) return;
    modal.classList.remove('open');
    overlay.classList.remove('open');
    // restore body scroll
    document.body.style.overflow = '';
    // unwind the history entry we pushed (skip if called from popstate — browser already did it)
    if (didPushState && !(opts && opts.fromPopstate)) {
      didPushState = false;
      try { history.back(); } catch (_) {}
    } else {
      didPushState = false;
    }
  }

  function setActive(index) {
    items.forEach((el, i) => {
      el.classList.toggle('active', i === index);
      if (i === index) el.scrollIntoView({ block: 'nearest' });
    });
    activeIndex = index;
  }

  function activateItem() {
    if (items[activeIndex]) items[activeIndex].click();
  }

  // keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      if (modal.classList.contains('open')) close();
      else open();
    }
    if (e.key === 'Escape' && modal.classList.contains('open')) {
      close();
    }
  });

  // arrow keys + enter inside the modal
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (items.length) setActive(Math.min(activeIndex + 1, items.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (items.length) setActive(Math.max(activeIndex - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (e.metaKey || e.ctrlKey) {
        // Cmd+Enter: always trigger create (last item) if available
        const createEl = results.querySelector('.search-create');
        if (createEl) createEl.click();
        else activateItem();
      } else {
        activateItem();
      }
    } else if (e.key === 'Backspace' && input.value === '' && currentScope) {
      // Backspace on empty input clears scope
      e.preventDefault();
      clearScope();
    }
  });

  overlay.addEventListener('click', () => close());

  // close button (wikihub-zlgt fix #1)
  if (closeBtn) {
    closeBtn.addEventListener('click', (e) => {
      e.preventDefault();
      close();
    });
  }

  // popstate handler — system/browser back-button closes the modal (wikihub-zlgt fix #4)
  window.addEventListener('popstate', (e) => {
    if (modal.classList.contains('open')) {
      // The history entry we pushed has already been popped by the browser.
      close({ fromPopstate: true });
    }
  });

  // search on input
  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(doSearch, 150);
  });

  function doSearch() {
    const q = input.value.trim();
    if (!q) {
      items = [];
      activeIndex = 0;
      renderEmptyState();
      return;
    }

    const thisSearch = ++searchId;

    let url = '/api/v1/search?q=' + encodeURIComponent(q) + '&limit=8';
    if (currentScope) {
      if (currentScope.type === 'wiki') {
        url += '&scope=wiki&wiki=' + encodeURIComponent(currentScope.owner + '/' + currentScope.slug);
      } else if (currentScope.type === 'author') {
        url += '&scope=author&author=' + encodeURIComponent(currentScope.owner);
      }
    }

    fetch(url)
      .then(r => r.json())
      .then(data => {
        // ignore stale responses from earlier searches
        if (thisSearch !== searchId) return;

        results.textContent = '';
        items = [];
        activeIndex = 0;

        if (data.results && data.results.length > 0) {
          data.results.forEach(r => {
            const item = document.createElement('a');
            item.className = 'search-result';
            item.href = '/@' + r.wiki + '/' + r.page.replace('.md', '');

            const path = document.createElement('div');
            path.className = 'search-result-path';
            path.textContent = '@' + r.wiki;
            item.appendChild(path);

            const title = document.createElement('div');
            title.className = 'search-result-title';
            title.textContent = r.title || r.page;
            item.appendChild(title);

            if (r.excerpt) {
              const excerpt = document.createElement('div');
              excerpt.className = 'search-result-excerpt';
              excerpt.textContent = r.excerpt;
              item.appendChild(excerpt);
            }

            results.appendChild(item);
            items.push(item);
          });
        } else {
          const empty = document.createElement('div');
          empty.className = 'search-empty';
          empty.textContent = 'No results found.';
          results.appendChild(empty);
        }

        // show create option if logged in
        const username = modal.dataset.username;
        if (username) {
          const slug = q.replace(/\s+/g, '-').toLowerCase().replace(/[^a-z0-9\-\/]/g, '').replace(/-+/g, '-').replace(/^-|-$/g, '');
          if (slug) {
            // if inside a wiki, create in that wiki; otherwise create in personal wiki
            const match = window.location.pathname.match(/\/@([\w-]+)\/([\w-]+)/);
            const wikiOwner = match ? match[1] : username;
            const wikiSlug = match ? match[2] : username;

            const create = document.createElement('a');
            create.className = 'search-result search-create';
            create.href = '#';

            const createTitle = document.createElement('div');
            createTitle.className = 'search-result-title';
            createTitle.textContent = '+ Create "' + slug + '"';
            create.appendChild(createTitle);

            const createPath = document.createElement('div');
            createPath.className = 'search-result-path';
            createPath.textContent = slug + '.md → @' + wikiOwner + '/' + wikiSlug;
            create.appendChild(createPath);

            create.addEventListener('click', (e) => {
              e.preventDefault();
              window.location.href = '/@' + wikiOwner + '/' + wikiSlug +
                '/new?path=' + encodeURIComponent(slug);
              close();
            });
            results.appendChild(create);
            items.push(create);
          }
        }

        // auto-select first item
        if (items.length) setActive(0);
      })
      .catch(() => {});
  }

  // expose for external triggers
  window.wikihubSearch = { open, close };
})();
