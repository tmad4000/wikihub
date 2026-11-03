/* Side peek (wikihub-9k18, wikihub-9i28) — Notion-style slide-over for
 * internal wiki links.
 *
 * Link model (design 2026-11-03, matches Roam/Logseq/Tana/Notion):
 *   - Plain click on a same-wiki link NAVIGATES (full page), on every device.
 *   - Shift+click opens the target in the side peek — in the article, inside an
 *     open peek, and on page rows of the left sidebar tree (folders excluded).
 *   - A hover/focus "open in side peek" button (.peek-trigger) is injected
 *     after each peekable article link on desktop widths (never in headings).
 *   - Shift+Enter on a focused peekable link opens the peek; Enter navigates.
 *   - Cmd/Ctrl/Alt/middle clicks are never touched (new tab etc. stay native).
 *   - Inside an open peek a plain click replaces the panel content (you chose a
 *     reading-aside context — stay in it). Cmd/Ctrl+Enter or the header button
 *     opens the peeked page as a full page; Esc closes.
 * External / broken links, `/-/` routes, same-page anchors keep their default
 * behavior. The panel content is the server-rendered article body fetched from
 * `?fragment=1` (one shared renderer + ACL path — no private-content leak).
 *
 * Deep-linkable: `?peek=<page-path>` restores the peek on load and is pushed
 * to history when a peek opens, so the browser back button (and the close
 * controls) close it and the URL is shareable.
 *
 * Below the mobile breakpoint peeks are disabled and links navigate normally.
 */
(function () {
  "use strict";
  var cfg = window.__wikihubPeek;
  if (!cfg || !cfg.base) return;
  var wikiBase = cfg.base; // "/@owner/slug"
  var MOBILE_MAX = 767;

  function isMobile() {
    return window.innerWidth <= MOBILE_MAX;
  }

  // Hover affordances only make sense with a fine hovering pointer; touch
  // devices (including wide tablets) keep plain navigation only.
  function canHover() {
    if (typeof window.matchMedia !== "function") return true;
    try {
      return window.matchMedia("(hover: hover) and (pointer: fine)").matches;
    } catch (e) {
      return true;
    }
  }

  var overlay, panel, bodyEl, titleEl, openLink, copyBtn;
  var currentFullUrl = null;
  var currentPeekPath = null;
  var loadRequestSeq = 0;

  function build() {
    if (overlay) return;
    overlay = document.createElement("div");
    overlay.className = "peek-overlay";
    overlay.setAttribute("hidden", "");
    overlay.innerHTML =
      '<div class="peek-panel" role="dialog" aria-modal="true" aria-label="Page preview">' +
      '  <div class="peek-head">' +
      '    <div class="peek-title" id="peek-title" title="Tip: Shift+click any link to open it here"></div>' +
      '    <div class="peek-actions">' +
      '      <a class="peek-btn peek-open" title="Open as full page" aria-label="Open as full page">' +
      '        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>' +
      '      </a>' +
      '      <button type="button" class="peek-btn peek-copy" title="Copy link" aria-label="Copy link">' +
      '        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>' +
      '      </button>' +
      '      <button type="button" class="peek-btn peek-close" title="Close (Esc)" aria-label="Close preview">' +
      '        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="M6 6l12 12"/></svg>' +
      '      </button>' +
      '    </div>' +
      '  </div>' +
      '  <div class="peek-body article thin-scroll" id="peek-body"></div>' +
      "</div>";
    document.body.appendChild(overlay);
    panel = overlay.querySelector(".peek-panel");
    bodyEl = overlay.querySelector("#peek-body");
    titleEl = overlay.querySelector("#peek-title");
    openLink = overlay.querySelector(".peek-open");
    copyBtn = overlay.querySelector(".peek-copy");

    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) closePeek();
    });
    overlay.querySelector(".peek-close").addEventListener("click", closePeek);
    copyBtn.addEventListener("click", onCopy);
    // Links clicked inside the peek navigate within the peek (one level).
    bodyEl.addEventListener("click", onLinkClick);
    bodyEl.addEventListener("mousedown", onLinkMouseDown);
  }

  function showDom() {
    build();
    overlay.removeAttribute("hidden");
    // force reflow so the CSS transition runs on the added class
    void overlay.offsetWidth;
    overlay.classList.add("open");
    document.body.classList.add("peek-open");
  }

  function hideDom() {
    loadRequestSeq++;
    if (!overlay) return;
    overlay.classList.remove("open");
    document.body.classList.remove("peek-open");
    overlay.setAttribute("hidden", "");
  }

  function loadPeek(rest, historyMode) {
    build();
    var requestId = ++loadRequestSeq;
    var hash = "";
    var hi = rest.indexOf("#");
    if (hi >= 0) {
      hash = rest.slice(hi);
      rest = rest.slice(0, hi);
    }
    var pageUrl = wikiBase + "/" + rest;
    var fragUrl = pageUrl + (pageUrl.indexOf("?") >= 0 ? "&" : "?") + "fragment=1";

    fetch(fragUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) {
        if (requestId !== loadRequestSeq) return null;
        var ct = r.headers.get("content-type") || "";
        if (!r.ok || ct.indexOf("application/json") < 0) {
          throw new Error("not a peekable page");
        }
        return r.json();
      })
      .then(function (data) {
        if (requestId !== loadRequestSeq || !data) return;
        titleEl.textContent = data.title || rest;
        bodyEl.innerHTML = data.html || "";
        bodyEl.scrollTop = 0;
        currentFullUrl = new URL(data.url, location.href).href;
        currentPeekPath = new URL(currentFullUrl).pathname;
        openLink.href = data.url;
        rehydrate(bodyEl);
        if (historyMode) {
          var q = location.pathname + "?peek=" + encodeURIComponent(rest);
          var state = { peek: rest };
          if (historyMode === "replace") {
            history.replaceState(history.state && history.state.peek ? state : null, "", q);
          } else {
            history.pushState(state, "", q);
          }
        }
        showDom();
        if (hash) {
          scrollPeekHash(hash);
        }
        try {
          decorateLinks(bodyEl);
        } catch (e) {}
      })
      .catch(function () {
        if (requestId !== loadRequestSeq) return;
        // Not a peekable target (missing, private, non-markdown, network) —
        // fall back to a normal full-page navigation.
        window.location.href = pageUrl + hash;
      });
  }

  // Re-run optional in-content rendering (KaTeX) on freshly injected HTML.
  function rehydrate(el) {
    if (typeof window.renderMathInElement === "function") {
      try {
        window.renderMathInElement(el, {
          delimiters: [
            { left: "$$", right: "$$", display: true },
            { left: "$", right: "$", display: false },
          ],
          throwOnError: false,
        });
      } catch (e) {}
    }
  }

  function findPeekHashTarget(hash) {
    if (!hash || hash.charAt(0) !== "#") return null;
    var id;
    try {
      id = decodeURIComponent(hash.slice(1));
    } catch (e) {
      id = hash.slice(1);
    }
    if (!id) return null;
    if (window.CSS && typeof window.CSS.escape === "function") {
      return bodyEl.querySelector("#" + window.CSS.escape(id));
    }
    var candidates = bodyEl.querySelectorAll("[id]");
    for (var i = 0; i < candidates.length; i++) {
      if (candidates[i].id === id) return candidates[i];
    }
    return null;
  }

  function scrollPeekHash(hash) {
    var t = findPeekHashTarget(hash);
    if (t) t.scrollIntoView();
  }

  function onCopy() {
    if (!currentFullUrl) return;
    var done = function () {
      copyBtn.classList.add("copied");
      setTimeout(function () {
        copyBtn.classList.remove("copied");
      }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(currentFullUrl).then(done, done);
    } else {
      var ta = document.createElement("textarea");
      ta.value = currentFullUrl;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch (e) {}
      document.body.removeChild(ta);
      done();
    }
  }

  // Close via X / Esc / click-outside. If a history entry was pushed for the
  // peek, pop it (keeps history clean + syncs the URL); otherwise just hide.
  function closePeek() {
    if (history.state && history.state.peek) {
      history.back();
    } else {
      hideDom();
      if (new URLSearchParams(location.search).has("peek")) {
        history.replaceState(null, "", location.pathname);
      }
    }
  }

  // Resolve an anchor to a peek target, or null when the link must keep its
  // native behavior. Returns {rest, inPeek} for a peekable same-wiki page, or
  // {selfHash} for an in-peek anchor to the page currently shown in the panel.
  function peekTarget(a) {
    if (!a || !a.closest) return null;
    var inContent = a.closest(".article, .peek-body");
    var inSidebar = !inContent && a.closest("#sidebar-tree, #async-sidebar");
    if (!inContent && !inSidebar) return null;
    // Folder rows navigate to the folder listing; only page rows can peek.
    if (inSidebar && a.closest(".sidebar-folder-row")) return null;
    if (a.target && a.target !== "" && a.target !== "_self") return null;
    if (a.classList.contains("external-link") || a.classList.contains("wikilink-broken")) return null;
    if (a.hasAttribute("download")) return null;
    var href = a.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#") return null;
    var inPeek = !!a.closest(".peek-body");
    var url;
    try {
      url = new URL(href, inPeek && currentFullUrl ? currentFullUrl : location.href);
    } catch (err) {
      return null;
    }
    var currentPath = location.pathname;
    if (inPeek && currentPeekPath) {
      currentPath = currentPeekPath;
    }
    if (inPeek && currentPeekPath && url.pathname === currentPeekPath && url.hash) {
      return { selfHash: url.hash };
    }
    if (url.pathname === currentPath) return null;
    if (url.origin !== location.origin) return null;
    if (url.search) return null; // e.g. HTML deck viewer (?view=1)
    if (!url.pathname.startsWith(wikiBase + "/")) return null;
    var rest = url.pathname.slice(wikiBase.length + 1);
    if (!rest || rest.indexOf("/-/") >= 0) return null; // proposals / suggest etc.
    return { rest: rest + url.hash, inPeek: inPeek };
  }

  function isPlainPrimary(e) {
    return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.altKey;
  }

  function onLinkClick(e) {
    if (e.defaultPrevented) return;
    if (!isPlainPrimary(e)) return;
    var trigger = e.target && e.target.closest ? e.target.closest(".peek-trigger") : null;
    if (trigger) {
      e.preventDefault();
      if (isMobile()) return;
      var tt = peekTarget(trigger.__peekAnchor || trigger.previousElementSibling);
      if (!tt || tt.selfHash) return;
      loadPeek(tt.rest, tt.inPeek ? "replace" : "push");
      return;
    }
    var a = e.target.closest("a");
    var t = peekTarget(a);
    if (!t) return;
    if (t.selfHash) {
      e.preventDefault();
      scrollPeekHash(t.selfHash);
      return;
    }
    // Plain click navigates; Shift+click (or any click inside an open peek)
    // opens in the panel.
    if (!e.shiftKey && !t.inPeek) return;
    if (isMobile()) return; // small screens: normal navigation
    e.preventDefault();
    loadPeek(t.rest, t.inPeek ? "replace" : "push");
  }

  // Shift+mousedown would extend the text selection before the click lands;
  // suppress that only for links we are about to open in the peek.
  function onLinkMouseDown(e) {
    if (!e.shiftKey || !isPlainPrimary(e) || isMobile()) return;
    var a = e.target && e.target.closest ? e.target.closest("a") : null;
    var t = peekTarget(a);
    if (t && !t.selfHash) e.preventDefault();
  }

  var TRIGGER_ICON =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M14 4v16"/></svg>';

  // Inject a hover/focus "open in side peek" button after each peekable link.
  function decorateLinks(root) {
    if (!root || typeof root.querySelectorAll !== "function" || isMobile() || !canHover()) return;
    var links = root.querySelectorAll("a[href]");
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      if (!a || typeof a.closest !== "function" || a.__peekDecorated) continue;
      if (a.closest("h1, h2, h3, h4, h5, h6")) continue;
      if (a.querySelector && a.querySelector("img")) continue;
      var t = peekTarget(a);
      if (!t || t.selfHash) continue;
      var label = (a.textContent || "").replace(/\s+/g, " ").trim() || "page";
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "peek-trigger";
      btn.setAttribute("aria-label", "Open " + label + " in side peek");
      btn.setAttribute("title", "Open in side peek (Shift+click)");
      btn.innerHTML = TRIGGER_ICON;
      btn.__peekAnchor = a;
      a.__peekDecorated = true;
      if (a.parentNode) a.parentNode.insertBefore(btn, a.nextSibling);
    }
  }

  window.addEventListener("popstate", function () {
    var p = new URLSearchParams(location.search).get("peek");
    if (p) {
      loadPeek(p, false);
    } else {
      hideDom();
    }
  });

  document.addEventListener("keydown", function (e) {
    var peekOpen = !!(overlay && overlay.classList.contains("open"));
    if (e.key === "Escape" && peekOpen) {
      closePeek();
      return;
    }
    if (e.key !== "Enter") return;
    if (peekOpen && (e.metaKey || e.ctrlKey) && currentFullUrl) {
      e.preventDefault();
      location.href = currentFullUrl;
      return;
    }
    if (!e.shiftKey || e.metaKey || e.ctrlKey || e.altKey || isMobile()) return;
    var active = document.activeElement;
    if (!active || active.tagName !== "A") return;
    var t = peekTarget(active);
    if (!t || t.selfHash) return;
    e.preventDefault();
    loadPeek(t.rest, t.inPeek ? "replace" : "push");
  });

  document.addEventListener("DOMContentLoaded", function () {
    var main = document.querySelector(".reader-layout .article") || document.querySelector(".article");
    if (main) {
      main.addEventListener("click", onLinkClick);
      main.addEventListener("mousedown", onLinkMouseDown);
      decorateLinks(main);
    }
    // Shift+click on sidebar page rows (async rows live inside #sidebar-tree).
    var tree = document.querySelector("#sidebar-tree");
    if (tree) {
      tree.addEventListener("click", onLinkClick);
      tree.addEventListener("mousedown", onLinkMouseDown);
    }
    // Restore a deep-linked peek.
    var p = new URLSearchParams(location.search).get("peek");
    if (p && !isMobile()) loadPeek(p, false);
  });
})();
