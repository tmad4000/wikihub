// Cmd+K omnisearch modal for wikihub
(function() {
  const modal = document.getElementById('search-modal');
  const overlay = document.getElementById('search-overlay');
  const input = document.getElementById('search-input');
  const results = document.getElementById('search-results');
  if (!modal) return;

  let debounceTimer;

  function open() {
    modal.classList.add('open');
    overlay.classList.add('open');
    input.value = '';
    results.textContent = '';
    setTimeout(() => input.focus(), 50);
  }

  function close() {
    modal.classList.remove('open');
    overlay.classList.remove('open');
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

  overlay.addEventListener('click', close);

  // search on input
  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(doSearch, 200);
  });

  function doSearch() {
    const q = input.value.trim();
    if (!q) {
      results.textContent = '';
      return;
    }

    fetch('/api/v1/search?q=' + encodeURIComponent(q) + '&limit=8')
      .then(r => r.json())
      .then(data => {
        results.textContent = '';

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
          });
        } else {
          // search-or-create fallback
          const empty = document.createElement('div');
          empty.className = 'search-empty';
          empty.textContent = 'No results found.';
          results.appendChild(empty);

          // only show create if user is logged in (check for avatar in nav)
          if (document.querySelector('.nav-avatar')) {
            const create = document.createElement('a');
            create.className = 'search-create';
            create.textContent = 'Create "' + q + '"';
            create.href = '#';
            create.addEventListener('click', (e) => {
              e.preventDefault();
              // get current wiki context from URL
              const match = window.location.pathname.match(/\/@(\w+)\/(\w+)/);
              if (match) {
                window.location.href = '/@' + match[1] + '/' + match[2] +
                  '/new?title=' + encodeURIComponent(q);
              }
              close();
            });
            results.appendChild(create);
          }
        }
      })
      .catch(() => {});
  }

  // expose for external triggers
  window.wikihubSearch = { open, close };
})();
