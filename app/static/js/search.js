// Cmd+K omnisearch modal for wikihub
(function() {
  const modal = document.getElementById('search-modal');
  const overlay = document.getElementById('search-overlay');
  const input = document.getElementById('search-input');
  const results = document.getElementById('search-results');
  if (!modal) return;

  let debounceTimer;
  let activeIndex = 0;
  let items = [];

  function open() {
    modal.classList.add('open');
    overlay.classList.add('open');
    input.value = '';
    results.textContent = '';
    items = [];
    activeIndex = 0;
    setTimeout(() => input.focus(), 50);
  }

  function close() {
    modal.classList.remove('open');
    overlay.classList.remove('open');
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
      activateItem();
    }
  });

  overlay.addEventListener('click', close);

  // search on input
  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(doSearch, 150);
  });

  function doSearch() {
    const q = input.value.trim();
    if (!q) {
      results.textContent = '';
      items = [];
      activeIndex = 0;
      return;
    }

    fetch('/api/v1/search?q=' + encodeURIComponent(q) + '&limit=8')
      .then(r => r.json())
      .then(data => {
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

        // always show create option if logged in and in a wiki context
        if (document.querySelector('.nav-avatar')) {
          const match = window.location.pathname.match(/\/@([\w-]+)\/([\w-]+)/);
          if (match) {
            const slug = q.replace(/\s+/g, '-').toLowerCase().replace(/[^a-z0-9\-\/]/g, '').replace(/-+/g, '-').replace(/^-|-$/g, '');
            if (!slug) return; // nothing left after cleaning

            const create = document.createElement('a');
            create.className = 'search-result search-create';
            create.href = '#';

            const createTitle = document.createElement('div');
            createTitle.className = 'search-result-title';
            createTitle.textContent = '+ Create "' + slug + '"';
            create.appendChild(createTitle);

            const createPath = document.createElement('div');
            createPath.className = 'search-result-path';
            createPath.textContent = 'wiki/' + slug + '.md → @' + match[1] + '/' + match[2];
            create.appendChild(createPath);

            create.addEventListener('click', (e) => {
              e.preventDefault();
              window.location.href = '/@' + match[1] + '/' + match[2] +
                '/new?path=wiki/' + encodeURIComponent(slug);
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
