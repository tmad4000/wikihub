/* Wiki sidebar sort + async row renderer (wikihub-hnfm, wikihub-9c8j).
 *
 * One implementation shared by the reader, folder and profile sidebars, kept
 * loadable outside a browser so tests can run it in node's `vm`.
 *
 * Determinism contract (wikihub-9c8j, "sidebar reorders on click"): for
 * identical input the client must produce exactly the server's order.
 *   - default/alpha mirrors `_build_sidebar_tree`'s key
 *       (pinned desc, folder-first, name.lower(), path)
 *   - custom mirrors `.wikihub/sidebar.md`: every row carries data-order, the
 *     server's sequence index within its container.
 * Groups and dividers are fixed: sorting reorders rows only within each run
 * between fixed elements, then recurses into folders and groups.
 */
(function (root) {
  "use strict";

  function data(el, key) {
    return (el.dataset && el.dataset[key]) || "";
  }

  function hasClass(el, cls) {
    return !!(el.classList && el.classList.contains(cls));
  }

  function kindOf(el) {
    return data(el, "kind") || (hasClass(el, "sidebar-folder") ? "folder" : "page");
  }

  function isSortable(el) {
    if (hasClass(el, "sidebar-folder")) return true;
    return el.tagName === "A" && hasClass(el, "sidebar-item");
  }

  function cmpStr(a, b) {
    return a < b ? -1 : a > b ? 1 : 0;
  }

  function orderOf(el) {
    var raw = data(el, "order");
    if (raw === "") return null;
    var n = Number(raw);
    return isFinite(n) ? n : null;
  }

  function compare(a, b, mode) {
    var aName = data(a, "name").toLowerCase();
    var bName = data(b, "name").toLowerCase();
    var aPath = data(a, "path") || data(a, "folderPath");
    var bPath = data(b, "path") || data(b, "folderPath");
    if (mode === "custom") {
      var ao = orderOf(a);
      var bo = orderOf(b);
      if (ao !== bo) {
        if (ao === null) return 1;
        if (bo === null) return -1;
        return ao - bo;
      }
      return cmpStr(aName, bName) || cmpStr(aPath, bPath);
    }
    // Pinned pages always float to the top, in every non-custom mode.
    var aPinned = data(a, "pinned") === "1";
    var bPinned = data(b, "pinned") === "1";
    if (aPinned !== bPinned) return aPinned ? -1 : 1;
    // Folders always first.
    var aFolder = kindOf(a) === "folder";
    var bFolder = kindOf(b) === "folder";
    if (aFolder !== bFolder) return aFolder ? -1 : 1;
    if (mode === "alpha-desc") {
      return cmpStr(bName, aName) || cmpStr(bPath, aPath);
    }
    if (mode === "recent") {
      var aUpdated = data(a, "updated");
      var bUpdated = data(b, "updated");
      if (aUpdated !== bUpdated) {
        if (!aUpdated) return 1;
        if (!bUpdated) return -1;
        return cmpStr(bUpdated, aUpdated);
      }
    }
    return cmpStr(aName, bName) || cmpStr(aPath, bPath);
  }

  function childWithClass(el, cls) {
    var kids = el.children || [];
    for (var i = 0; i < kids.length; i++) {
      if (hasClass(kids[i], cls)) return kids[i];
    }
    return null;
  }

  function sortChildren(container, mode) {
    if (!container) return;
    var kids = Array.prototype.slice.call(container.children || []);
    var run = [];
    function flush(before) {
      if (run.length > 1) {
        var sorted = run.slice().sort(function (a, b) {
          return compare(a, b, mode);
        });
        sorted.forEach(function (el) {
          container.insertBefore(el, before);
        });
      }
      run = [];
    }
    kids.forEach(function (el) {
      if (isSortable(el)) run.push(el);
      else flush(el);
    });
    flush(null);
    kids.forEach(function (el) {
      var sub = null;
      if (hasClass(el, "sidebar-folder")) sub = childWithClass(el, "sidebar-children");
      else if (hasClass(el, "sidebar-group")) sub = childWithClass(el, "sidebar-group-children");
      else if (el.id === "async-sidebar") sub = el;
      if (sub) sortChildren(sub, mode);
    });
  }

  function isCustomTree(items) {
    return Array.isArray(items) && items.some(function (item) {
      return item && item.order !== undefined && item.order !== null;
    });
  }

  function storageKey(scope, custom) {
    // A wiki with its own order remembers the reader's choice per wiki and
    // defaults to "custom"; other wikis share the historic global key.
    return custom ? "wikihub-sidebar-sort:" + scope : "wikihub-sidebar-sort";
  }

  function savedMode(scope, custom, storage) {
    var saved = null;
    try {
      saved = storage.getItem(storageKey(scope, custom));
    } catch (e) {}
    if (custom) return saved || "custom";
    if (!saved || saved === "custom") return "alpha";
    return saved;
  }

  var FOLDER_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7h5l2 2h11v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>';
  var PAGE_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
  var PIN_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>';
  var EXT_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>';
  var CHEVRON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 18 6-6-6-6"/></svg>';

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Same markup as the `render_sidebar` macro in _sidebar_tree.html.
  function renderItem(item, level, currentPath) {
    var esc = escapeHtml;
    var order = item.order === undefined || item.order === null ? "" : String(item.order);
    if (item.kind === "folder") {
      var h = '<div class="sidebar-folder' + (item.active ? " open" : "") + '" data-folder-path="' + esc(item.path) + '" data-name="' + esc(item.name) + '" data-kind="folder" data-updated="' + esc(item.updated_at || "") + '" data-order="' + order + '" data-level="' + level + '">';
      h += '<div class="sidebar-folder-row"><button type="button" class="sidebar-folder-toggle" aria-label="Toggle ' + esc(item.name) + '">' + CHEVRON + "</button>";
      h += '<a href="' + esc(item.url) + '" class="sidebar-item' + (item.active ? " active" : "") + '" style="padding-left:' + (12 + level * 16) + 'px;"><span class="sidebar-icon">' + FOLDER_ICON + "</span>" + esc(item.name) + "</a></div>";
      h += '<div class="sidebar-children">' + (item.children || []).map(function (c) { return renderItem(c, level + 1, currentPath); }).join("") + "</div></div>";
      return h;
    }
    if (item.kind === "group") {
      return '<div class="sidebar-group" data-kind="group" data-name="' + esc(item.name) + '" data-order="' + order + '" data-level="' + level + '"><div class="sidebar-heading sidebar-group-heading">' + esc(item.name) + '</div><div class="sidebar-group-children">' + (item.children || []).map(function (c) { return renderItem(c, level, currentPath); }).join("") + "</div></div>";
    }
    if (item.kind === "divider") {
      return '<div class="sidebar-divider' + (item.name ? " sidebar-divider-labeled" : "") + '" data-kind="divider" data-order="' + order + '" role="separator">' + (item.name ? '<span class="sidebar-heading">' + esc(item.name) + "</span>" : "") + "</div>";
    }
    if (item.kind === "external") {
      var url = String(item.url || "");
      if (!/^https?:\/\//i.test(url)) return "";
      return '<a href="' + esc(url) + '" class="sidebar-item sidebar-external" target="_blank" rel="noopener" data-name="' + esc(item.name) + '" data-kind="external" data-pinned="0" data-updated="" data-order="' + order + '" data-level="' + level + '" style="padding-left:' + (28 + level * 16) + 'px;"><span class="sidebar-icon">' + EXT_ICON + "</span>" + esc(item.name) + "</a>";
    }
    var active = item.active || (currentPath && item.path === currentPath) ? " active" : "";
    return '<a href="' + esc(item.url) + '" class="sidebar-item' + (item.pinned ? " sidebar-pinned" : "") + active + '" data-name="' + esc(item.name) + '" data-kind="page" data-pinned="' + (item.pinned ? "1" : "0") + '" data-updated="' + esc(item.updated_at || "") + '" data-path="' + esc(item.path) + '" data-order="' + order + '" data-level="' + level + '" style="padding-left:' + (28 + level * 16) + 'px;"><span class="sidebar-icon">' + (item.pinned ? PIN_ICON : PAGE_ICON) + "</span>" + esc(item.name) + "</a>";
  }

  // Wire the sort buttons of one sidebar. opts: {scope, custom, tree, buttons, storage}
  function init(opts) {
    var storage = opts.storage || root.localStorage;
    var state = { custom: !!opts.custom };
    function setActive(mode) {
      (opts.buttons || []).forEach(function (btn) {
        var on = btn.dataset.sort === mode;
        btn.classList.toggle("active", on);
        btn.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
    function apply(mode, persist) {
      if (mode === "custom" && !state.custom) mode = "alpha";
      if (persist) {
        try {
          storage.setItem(storageKey(opts.scope, state.custom), mode);
        } catch (e) {}
      }
      setActive(mode);
      var tree = typeof opts.tree === "function" ? opts.tree() : opts.tree;
      if (tree) sortChildren(tree, mode);
      return mode;
    }
    (opts.buttons || []).forEach(function (btn) {
      btn.addEventListener("click", function () {
        apply(btn.dataset.sort, true);
      });
    });
    var api = {
      apply: apply,
      // Async sidebars learn about a custom order only from sidebar.json;
      // add the Custom button then (same markup as sidebar_sort_buttons).
      setCustom: function (custom, doc) {
        state.custom = !!custom;
        if (!state.custom || !opts.container || !doc) return;
        if ((opts.buttons || []).some(function (b) { return b.dataset.sort === "custom"; })) return;
        var btn = doc.createElement("button");
        btn.type = "button";
        btn.className = "sidebar-sort-btn";
        btn.dataset.sort = "custom";
        btn.title = "Custom order set by the wiki owner";
        btn.setAttribute("aria-label", "Custom order");
        btn.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/></svg>';
        btn.addEventListener("click", function () {
          apply("custom", true);
        });
        opts.container.insertBefore(btn, opts.container.firstChild);
        opts.container.dataset.custom = "1";
        opts.buttons = [btn].concat(opts.buttons || []);
      },
      current: function () {
        return savedMode(opts.scope, state.custom, storage);
      },
      // Server order is the default for each kind of tree; only re-sort when
      // the reader chose something else, so first paint never jumps.
      restore: function () {
        var mode = savedMode(opts.scope, state.custom, storage);
        var serverMode = state.custom ? "custom" : "alpha";
        if (mode !== serverMode) apply(mode, false);
        else setActive(mode);
        return mode;
      },
    };
    return api;
  }

  root.WikihubSidebar = {
    compare: compare,
    sortChildren: sortChildren,
    renderItem: renderItem,
    isCustomTree: isCustomTree,
    savedMode: savedMode,
    storageKey: storageKey,
    init: init,
  };
})(typeof window !== "undefined" ? window : this);
