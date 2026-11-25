/* Reader assistant panel (wikihub-1bfx).
 *
 * The server answers only from pages this visitor can read and returns
 * verified citations. Everything here is built with DOM APIs (no innerHTML)
 * because answers are influenced by page content. The conversation is kept
 * in sessionStorage per wiki so following a source link doesn't lose it.
 */
(function () {
  "use strict";
  var panel = document.getElementById("reader-assistant");
  if (!panel) return;
  var askUrl = panel.getAttribute("data-ask-url");
  var pagePath = panel.getAttribute("data-page-path") || "";
  var storeKey = panel.getAttribute("data-store-key");
  var storePrefix = panel.getAttribute("data-store-prefix");
  // Conversations are per viewer: after sign-out (or a different sign-in) a
  // previous identity's answers are dropped from this tab, never shown.
  try {
    for (var i = sessionStorage.length - 1; i >= 0; i--) {
      var k = sessionStorage.key(i);
      if (k && k.indexOf("wikihub-ask:") === 0 && k.indexOf(storePrefix) !== 0) sessionStorage.removeItem(k);
    }
  } catch (e) {}
  var list = document.getElementById("ra-messages");
  var input = document.getElementById("ra-input");
  var form = panel.querySelector("[data-ra-form]");
  var sendBtn = panel.querySelector("[data-ra-send]");
  var turns = load();
  var lastFocus = null;

  function load() {
    try { return JSON.parse(sessionStorage.getItem(storeKey) || "[]") || []; } catch (e) { return []; }
  }
  function save() {
    try { sessionStorage.setItem(storeKey, JSON.stringify(turns.slice(-20))); } catch (e) {}
  }
  function triggers() { return document.querySelectorAll("[data-ra-open]"); }

  function open(restoring) {
    lastFocus = restoring ? null : document.activeElement;
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    triggers().forEach(function (t) { t.setAttribute("aria-expanded", "true"); });
    if (!restoring) input.focus();
    list.scrollTop = list.scrollHeight;
    try { sessionStorage.setItem(storeKey + ":open", "1"); } catch (e) {}
  }
  function close() {
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
    triggers().forEach(function (t) { t.setAttribute("aria-expanded", "false"); });
    try { sessionStorage.removeItem(storeKey + ":open"); } catch (e) {}
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function safeHref(url) {
    // Citation URLs are server-built same-site paths; refuse anything else.
    return typeof url === "string" && url.indexOf("/@") === 0 && url.indexOf("\\") < 0 ? url : null;
  }

  // Inline: **bold**, `code`, [n] citation references.
  function inline(parent, text, citeByN) {
    var re = /(\*\*[^*]+\*\*|`[^`]+`|\[\d{1,3}\])/g;
    var last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      var tok = m[0];
      if (tok.charAt(0) === "*") parent.appendChild(el("strong", null, tok.slice(2, -2)));
      else if (tok.charAt(0) === "`") parent.appendChild(el("code", null, tok.slice(1, -1)));
      else {
        var n = parseInt(tok.slice(1, -1), 10);
        var c = citeByN[n];
        var href = c && safeHref(c.url);
        if (href) {
          var a = el("a", "ra-ref", "[" + n + "]");
          a.href = href;
          a.title = c.title + (c.quote ? " — “" + c.quote + "”" : "");
          parent.appendChild(a);
        } else parent.appendChild(document.createTextNode(tok));
      }
      last = m.index + tok.length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function renderAnswer(text, citations) {
    var wrap = el("div", "ra-a");
    var citeByN = {};
    (citations || []).forEach(function (c) { citeByN[c.n] = c; });
    var listEl = null;
    String(text || "").split("\n").forEach(function (line) {
      var item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
      if (item) {
        if (!listEl) { listEl = el(/^\s*\d/.test(line) ? "ol" : "ul"); wrap.appendChild(listEl); }
        var li = el("li"); inline(li, item[1], citeByN); listEl.appendChild(li);
        return;
      }
      listEl = null;
      if (!line.trim()) return;
      var p = el("p"); inline(p, line.replace(/^#+\s*/, ""), citeByN); wrap.appendChild(p);
    });
    if (citations && citations.length) {
      var src = el("div", "ra-sources");
      src.appendChild(el("div", "ra-sources-label", "Sources"));
      citations.forEach(function (c) {
        var href = safeHref(c.url);
        if (!href) return;
        var a = el("a", "ra-source");
        a.href = href;
        a.setAttribute("data-ra-source", c.path);
        a.appendChild(el("span", "ra-source-n", String(c.n)));
        a.appendChild(el("span", "ra-source-title", c.title));
        if (c.quote) a.appendChild(el("span", "ra-source-quote", "“" + c.quote + "”"));
        a.appendChild(el("span", "ra-source-path", c.path));
        src.appendChild(a);
      });
      wrap.appendChild(src);
    }
    var comments = document.getElementById("page-comments");
    if (comments && comments.dataset.viewer) {
      var lines = (citations || []).filter(function (c) { return safeHref(c.url); })
        .map(function (c) { return "[" + c.n + "] " + new URL(c.url, location.origin).href; });
      var body = text + (lines.length ? "\n\nSources\n" + lines.join("\n") : "");
      function offer(label, type) {
        var button = el("button", "action-btn", label);
        button.type = "button";
        button.onclick = function () {
          close();
          comments.dispatchEvent(new CustomEvent("review-reader-draft", { detail: { type: type, body: body } }));
        };
        wrap.appendChild(button);
      }
      offer("Draft a comment", "comment");
      if (comments.querySelector("[data-page-form]")) offer("Draft a private page", "page");
    }
    return wrap;
  }

  function renderTurn(t) {
    var empty = list.querySelector("[data-ra-empty]");
    if (empty) empty.remove();
    if (t.role === "user") list.appendChild(el("div", "ra-q", t.content));
    else if (t.error) list.appendChild(el("div", "ra-error", t.content));
    else list.appendChild(renderAnswer(t.content, t.citations));
    list.scrollTop = list.scrollHeight;
  }

  function history() {
    // Plain text only; the server treats it as conversation, never authority.
    return turns.filter(function (t) { return !t.error; }).slice(-12).map(function (t) {
      return { role: t.role, content: t.content };
    });
  }

  function ask(question) {
    var prior = history();
    var q = { role: "user", content: question };
    turns.push(q); save(); renderTurn(q);
    var thinking = el("div", "ra-thinking", "Reading the wiki…");
    list.appendChild(thinking); list.scrollTop = list.scrollHeight;
    sendBtn.disabled = true; input.disabled = true;
    fetch(askUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ question: question, page_path: pagePath, history: prior }),
    })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var t = res.ok
          ? { role: "assistant", content: res.body.answer || "", citations: res.body.citations || [] }
          : { role: "assistant", error: true, content: res.body.message || "The assistant couldn't answer just now." };
        turns.push(t); save(); thinking.remove(); renderTurn(t);
      })
      .catch(function () {
        var t = { role: "assistant", error: true, content: "Network error — please try again." };
        turns.push(t); save(); thinking.remove(); renderTurn(t);
      })
      .then(function () { sendBtn.disabled = false; input.disabled = false; input.focus(); });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var question = input.value.trim();
    if (!question || sendBtn.disabled) return;
    input.value = "";
    input.style.height = "";
    ask(question);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener("input", function () {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 140) + "px";
  });
  panel.querySelector("[data-ra-close]").addEventListener("click", close);
  panel.querySelector("[data-ra-clear]").addEventListener("click", function () {
    turns = []; save();
    while (list.firstChild) list.removeChild(list.firstChild);
    list.appendChild(el("div", "ra-empty", "Conversation cleared."));
    input.focus();
  });
  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("[data-ra-open]");
    if (!t) return;
    e.preventDefault();
    if (panel.classList.contains("open")) close(); else open(false);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && panel.classList.contains("open")) close();
  });

  turns.forEach(renderTurn);
  // Keep the conversation beside the source after following a citation --
  // on wide screens only, so a phone lands on the page it asked for.
  try {
    if (sessionStorage.getItem(storeKey + ":open") === "1" && window.innerWidth > 900) open(true);
  } catch (e) {}
})();
