/* Side peek (wikihub-9k18) — Notion-style slide-over for internal wiki links.
 *
 * Clicking a normal (unmodified) same-wiki page link inside the article opens
 * the target page in a right-side panel instead of navigating away, so the
 * reader keeps their place. Modified clicks (cmd/ctrl/shift/alt/middle) and
 * external / broken links keep their default behavior. The panel content is
 * the server-rendered article body fetched from `?fragment=1` (one shared
 * renderer + ACL path — no client-side duplication, no private-content leak).
 *
 * Deep-linkable: `?peek=<page-path>` restores the peek on load and is pushed
 * to history when a peek opens, so the browser back button (and the close
 * controls) close it and the URL is shareable.
 *
 * One level only: navigating a link inside the peek replaces the panel content
 * (it never spawns a second stacked panel). Below the mobile breakpoint peeks
 * are disabled and links navigate normally.
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

  var overlay, panel, bodyEl, titleEl, openLink, copyBtn;
  var currentFullUrl = null;

  function build() {
    if (overlay) return;
    overlay = document.createElement("div");
    overlay.className = "peek-overlay";
    overlay.setAttribute("hidden", "");
    overlay.innerHTML =
      '<div class="peek-panel" role="dialog" aria-modal="true" aria-label="Page preview">' +
      '  <div class="peek-head">' +
      '    <div class="peek-title" id="peek-title"></div>' +
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
    if (!overlay) return;
    overlay.classList.remove("open");
    document.body.classList.remove("peek-open");
    overlay.setAttribute("hidden", "");
  }

  // Load `rest` (a page path relative to wikiBase, may include #hash) into the
  // peek. When push is true, add a history entry so back/close pops it.
  function loadPeek(rest, push) {
    build();
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
        var ct = r.headers.get("content-type") || "";
        if (!r.ok || ct.indexOf("application/json") < 0) {
          throw new Error("not a peekable page");
        }
        return r.json();
      })
      .then(function (data) {
        titleEl.textContent = data.title || rest;
        bodyEl.innerHTML = data.html || "";
        bodyEl.scrollTop = 0;
        currentFullUrl = new URL(data.url, location.href).href;
        openLink.href = data.url;
        rehydrate(bodyEl);
        if (push) {
          var q = location.pathname + "?peek=" + encodeURIComponent(rest);
          history.pushState({ peek: rest }, "", q);
        }
        showDom();
        if (hash) {
          var t = bodyEl.querySelector(hash.replace(/[^#\w\-:.]/g, ""));
          if (t) t.scrollIntoView();
        }
      })
      .catch(function () {
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

  function onLinkClick(e) {
    if (e.defaultPrevented) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest("a");
    if (!a) return;
    // Only content links that live in the article or the peek body.
    if (!a.closest(".article, .peek-body")) return;
    if (a.target && a.target !== "" && a.target !== "_self") return;
    if (a.classList.contains("external-link") || a.classList.contains("wikilink-broken")) return;
    if (a.hasAttribute("download")) return;
    var url;
    try {
      url = new URL(a.href, location.href);
    } catch (err) {
      return;
    }
    if (url.origin !== location.origin) return;
    if (!url.pathname.startsWith(wikiBase + "/")) return;
    var rest = url.pathname.slice(wikiBase.length + 1);
    if (!rest || rest.indexOf("/-/") >= 0) return; // proposals / suggest etc.
    if (isMobile()) return; // small screens: normal navigation
    e.preventDefault();
    loadPeek(rest + url.hash, true);
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
    if (e.key === "Escape" && overlay && overlay.classList.contains("open")) {
      closePeek();
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    var main = document.querySelector(".reader-layout .article") || document.querySelector(".article");
    if (main) main.addEventListener("click", onLinkClick);
    // Restore a deep-linked peek.
    var p = new URLSearchParams(location.search).get("peek");
    if (p && !isMobile()) loadPeek(p, false);
  });
})();
