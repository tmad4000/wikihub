// WikiHub Milkdown bundle entry point.
//
// Public API (used by app/static/js/editor-surface.js and test-roundtrip.mjs):
//
//   createMilkdownEditor(container, initialMarkdown, onUpdate)
//     -> Milkdown Editor, with `editor.wikihub` = surface API (see below).
//     onUpdate(markdown, {docChanged: true}) fires only for real document
//     changes made after mount (wikihub-k7k5 D8), debounced; call
//     editor.wikihub.flush() before reading the textarea on submit.
//
//   probeRoundTrip(markdown) -> Promise<{ok, serialized, error?}>
//     Parses + re-serializes on a detached editor and compares normalized forms
//     (D3). ok=false means the visual editor would change this file.
//
//   normalizeForProbe(markdown) -> string
//
// Syntax coverage: CommonMark + GFM (tables, task lists, strikethrough,
// autolinks, footnotes) + $math$ / $$math$$ (KaTeX) + [[wikilink]] /
// [[target|alias]] / ![[embed]] lossless atoms (wikihub-oyf4).
//
// Built with esbuild into app/static/js/milkdown-bundle.js (see build.mjs).

import {
  Editor,
  rootCtx,
  defaultValueCtx,
  editorViewCtx,
  serializerCtx,
  commandsCtx,
  remarkStringifyOptionsCtx,
} from '@milkdown/core';
import {
  commonmark,
  toggleStrongCommand,
  toggleEmphasisCommand,
  toggleInlineCodeCommand,
  wrapInHeadingCommand,
  wrapInBulletListCommand,
  wrapInOrderedListCommand,
  wrapInBlockquoteCommand,
  createCodeBlockCommand,
  insertHrCommand,
} from '@milkdown/preset-commonmark';
import { gfm, insertTableCommand, toggleStrikethroughCommand } from '@milkdown/preset-gfm';
import { history, undoCommand, redoCommand } from '@milkdown/plugin-history';
import { listener, listenerCtx } from '@milkdown/plugin-listener';
import { math, mathBlockSchema } from '@milkdown/plugin-math';
import { TextSelection } from '@milkdown/prose/state';
import { $remark } from '@milkdown/utils';
import { defaultHandlers } from 'mdast-util-to-markdown';

// KaTeX stylesheet so rendered math is legible inside the WYSIWYG surface.
import 'katex/dist/katex.min.css';

import { wikihubWikilink, wikilinkSchema, wikiembedSchema } from './wikilink-plugin.mjs';
import { createSurfacePlugin } from './surface-plugin.mjs';

export {
  Editor,
  rootCtx,
  defaultValueCtx,
  editorViewCtx,
  commonmark,
  gfm,
  history,
  listener,
  listenerCtx,
  math,
};

const SYNC_DEBOUNCE_MS = 120;

// Milkdown 7.20 stores list `spread` as the STRING "false"/"true" but hands it
// to remark-stringify as-is, and a non-empty string is truthy — so every tight
// list re-serialized as a loose one (blank line between items) after any edit.
// Coerce to booleans right before the default list handlers run.
function coerceSpread(node) {
  if (node && typeof node.spread === 'string') node.spread = node.spread === 'true';
}
function remarkListSpreadFix() {
  const data = this.data();
  const exts = data.toMarkdownExtensions || (data.toMarkdownExtensions = []);
  const mine = {};
  // Delegate to whichever handler would otherwise run (remark-gfm's task-list
  // listItem handler emits the `[ ]` checkbox), falling back to the default.
  // Extensions may nest (`{extensions: [...]}`, e.g. gfmToMarkdown()).
  const flatten = (list, out = []) => {
    for (const ext of list || []) {
      if (!ext || ext === mine) continue;
      if (ext.extensions) flatten(ext.extensions, out);
      out.push(ext);
    }
    return out;
  };
  const previous = (name) => {
    const all = flatten(exts);
    for (let i = all.length - 1; i >= 0; i--) {
      const h = all[i].handlers && all[i].handlers[name];
      if (h) return h;
    }
    return defaultHandlers[name];
  };
  mine.handlers = {
    list(node, parent, state, info) {
      coerceSpread(node);
      (node.children || []).forEach(coerceSpread);
      return previous('list').call(this, node, parent, state, info);
    },
    listItem(node, parent, state, info) {
      coerceSpread(node);
      coerceSpread(parent);
      return previous('listItem').call(this, node, parent, state, info);
    },
  };
  exts.push(mine);
}
const listSpreadFix = $remark('wikihubListSpreadFix', () => remarkListSpreadFix);

function buildEditor(container, initialContent, surface, { withMath }) {
  let e = Editor.make()
    .config((ctx) => {
      ctx.set(rootCtx, container);
      ctx.set(defaultValueCtx, initialContent || '');
      // `-` bullets: the dominant style in wiki/agent-written Markdown.
      ctx.update(remarkStringifyOptionsCtx, (prev) => ({ ...prev, bullet: '-' }));
    })
    .use(commonmark)
    .use(gfm);
  // plugin-math 7.5.9 is deprecated upstream; if it ever fails to mount we
  // degrade to plain `$…$` text instead of losing the visual editor.
  if (withMath) e = e.use(math);
  e = e.use(wikihubWikilink).use(listSpreadFix).use(history);
  if (surface) e = e.use(surface);
  return e.create();
}

async function makeEditor(container, initialContent, surface) {
  try {
    return await buildEditor(container, initialContent, surface, { withMath: true });
  } catch (err) {
    console.error('[wikihub] Milkdown mount with math failed; retrying without math', err);
    container.innerHTML = '';
    return buildEditor(container, initialContent, surface, { withMath: false });
  }
}

function serialize(ctx) {
  const view = ctx.get(editorViewCtx);
  return ctx.get(serializerCtx)(view.state.doc);
}

export async function createMilkdownEditor(container, initialContent, onUpdate) {
  let ready = false;
  let timer = null;
  let editorRef = null;

  const emit = () => {
    timer = null;
    if (!editorRef || !onUpdate) return;
    let md;
    editorRef.action((ctx) => { md = serialize(ctx); });
    onUpdate(md, { docChanged: true });
  };

  const surface = createSurfacePlugin({
    container,
    isReady: () => ready,
    onDocChanged: () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(emit, SYNC_DEBOUNCE_MS);
    },
  });

  const editor = await makeEditor(container, initialContent, surface);
  editorRef = editor;
  // Mount transaction(s) are done; from here on every doc change is a user edit.
  ready = true;

  editor.wikihub = createSurfaceApi(editor, {
    flush() {
      if (timer) {
        clearTimeout(timer);
        emit();
      }
    },
  });
  return editor;
}

// ---------------------------------------------------------------------------
// Surface API: toolbar commands, active state, menu insertions
// ---------------------------------------------------------------------------

function createSurfaceApi(editor, extra) {
  const withView = (fn) => {
    let out;
    editor.action((ctx) => { out = fn(ctx.get(editorViewCtx), ctx); });
    return out;
  };
  const call = (cmd, payload) => {
    let ok = false;
    editor.action((ctx) => { ok = ctx.get(commandsCtx).call(cmd.key, payload); });
    withView((view) => view.focus());
    return ok;
  };

  function setTaskList() {
    withView((view) => {
      if (!findAncestor(view.state.selection.$from, 'list_item')) {
        editor.action((ctx) => ctx.get(commandsCtx).call(wrapInBulletListCommand.key));
      }
      const { state } = view;
      const item = findAncestor(state.selection.$from, 'list_item');
      if (!item) return;
      const checked = item.node.attrs.checked;
      const next = checked == null ? false : null; // toggle task <-> plain item
      view.dispatch(state.tr.setNodeMarkup(item.pos, undefined, { ...item.node.attrs, checked: next }));
      view.focus();
    });
  }

  function insertNode(build) {
    withView((view, ctx) => {
      const node = build(ctx, view.state.schema);
      if (!node) return;
      view.dispatch(view.state.tr.replaceSelectionWith(node).scrollIntoView());
      view.focus();
    });
  }

  const commands = {
    bold: () => call(toggleStrongCommand),
    italic: () => call(toggleEmphasisCommand),
    code: () => call(toggleInlineCodeCommand),
    strike: () => call(toggleStrikethroughCommand),
    paragraph: () => call(wrapInHeadingCommand, 0),
    h1: () => call(wrapInHeadingCommand, 1),
    h2: () => call(wrapInHeadingCommand, 2),
    h3: () => call(wrapInHeadingCommand, 3),
    bullet: () => call(wrapInBulletListCommand),
    ordered: () => call(wrapInOrderedListCommand),
    task: () => setTaskList(),
    quote: () => call(wrapInBlockquoteCommand),
    codeblock: () => call(createCodeBlockCommand),
    table: () => call(insertTableCommand, { row: 3, col: 3 }),
    divider: () => call(insertHrCommand),
    undo: () => call(undoCommand),
    redo: () => call(redoCommand),
    // `[[` opens the wikilink menu via the trigger plugin.
    pagelink: () => withView((view) => {
      view.focus();
      view.dispatch(view.state.tr.insertText('[[').scrollIntoView());
    }),
    embed: (target) => {
      if (!target) return false;
      insertNode((ctx) => wikiembedSchema.type(ctx).create({
        target, alias: null, raw: `![[${target}]]`,
      }));
      return true;
    },
    math: (tex) => {
      if (!tex) return false;
      insertNode((ctx, schema) => {
        if (!schema.nodes.math_block) return schema.text(`$${tex}$`);
        return mathBlockSchema.type(ctx).create({ value: tex });
      });
      return true;
    },
  };

  return {
    ...extra,
    run(name, payload) {
      const fn = commands[name];
      if (!fn) return false;
      return fn(payload);
    },
    // Delete [from, to) (the trigger text) then run a command / insert a node.
    replaceRange(from, to, name, payload) {
      withView((view) => {
        const tr = view.state.tr.delete(from, to);
        tr.setSelection(TextSelection.create(tr.doc, from));
        view.dispatch(tr);
      });
      if (name) return this.run(name, payload);
      return true;
    },
    insertWikilink({ target, alias, from, to }) {
      withView((view, ctx) => {
        const cleanAlias = alias && alias !== target ? alias : null;
        const raw = cleanAlias ? `[[${target}|${cleanAlias}]]` : `[[${target}]]`;
        const node = wikilinkSchema.type(ctx).create({ target, alias: cleanAlias, raw });
        const tr = view.state.tr.replaceWith(from, to, node);
        const after = from + node.nodeSize;
        tr.insertText(' ', after);
        tr.setSelection(TextSelection.create(tr.doc, after + 1));
        view.dispatch(tr.scrollIntoView());
        view.focus();
      });
    },
    activeState() {
      return withView((view) => {
        const { state } = view;
        const { from, to, empty, $from } = state.selection;
        const marks = state.schema.marks;
        const hasMark = (type) => {
          if (!type) return false;
          if (empty) return !!type.isInSet(state.storedMarks || $from.marks());
          return state.doc.rangeHasMark(from, to, type);
        };
        const heading = findAncestor($from, 'heading');
        const item = findAncestor($from, 'list_item');
        const list = findAncestor($from, 'bullet_list') || findAncestor($from, 'ordered_list');
        const isTask = !!(item && item.node.attrs.checked != null);
        return {
          bold: hasMark(marks.strong),
          italic: hasMark(marks.emphasis),
          code: hasMark(marks.inlineCode),
          strike: hasMark(marks.strike_through),
          heading: heading ? heading.node.attrs.level : 0,
          bullet: !!(list && list.node.type.name === 'bullet_list' && !isTask),
          ordered: !!(list && list.node.type.name === 'ordered_list'),
          task: isTask,
          quote: !!findAncestor($from, 'blockquote'),
          codeblock: !!findAncestor($from, 'code_block'),
        };
      });
    },
    toggleTaskAt(dom) {
      withView((view) => {
        let pos;
        try { pos = view.posAtDOM(dom, 0); } catch (_e) { return; }
        const item = findAncestor(view.state.doc.resolve(Math.min(pos, view.state.doc.content.size)), 'list_item');
        if (!item || item.node.attrs.checked == null) return;
        view.dispatch(view.state.tr.setNodeMarkup(item.pos, undefined, {
          ...item.node.attrs,
          checked: !item.node.attrs.checked,
        }));
      });
    },
    getMarkdown() {
      let md = '';
      editor.action((ctx) => { md = serialize(ctx); });
      return md;
    },
    textContent() {
      return withView((view) => view.state.doc.textBetween(0, view.state.doc.content.size, '\n', ' '));
    },
    focus() {
      withView((view) => view.focus());
    },
  };
}

function findAncestor($pos, name) {
  for (let d = $pos.depth; d > 0; d--) {
    const node = $pos.node(d);
    if (node.type.name === name) return { node, pos: $pos.before(d), depth: d };
  }
  return null;
}

// ---------------------------------------------------------------------------
// Round-trip safety probe (D3)
// ---------------------------------------------------------------------------

// Normalization deliberately ignores differences that do not change meaning or
// rendering: trailing whitespace, blank-line runs, a trailing newline, inline
// `$$x$$` -> `$x$`, and equivalent marker choices (`*`/`+` bullets vs `-`,
// `_em_` vs `*em*`, `__strong__` vs `**strong**`, `***`/`___` rules, `1)` vs
// `1.`). Everything else (raw HTML, reference links, escapes, nesting quirks)
// must survive verbatim or the page opens in Markdown.
// GFM table rows: cell padding and delimiter dash count are cosmetic.
const TABLE_DELIM_RE = /^\|?(\s*:?-+:?\s*\|)+\s*(:?-+:?\s*)?\|?$/;
function normalizeTableLine(line) {
  const t = line.trim();
  if (!t.startsWith('|')) return line;
  if (TABLE_DELIM_RE.test(t)) return t.replace(/\s+/g, '').replace(/-+/g, '-');
  return t.replace(/\s*\|\s*/g, ' | ').trim();
}

export function normalizeForProbe(markdown) {
  let s = (markdown || '').replace(/\r\n?/g, '\n');
  s = s
    .split('\n')
    .map((line) => line.replace(/[ \t]+$/, ''))
    .map((line) => (/^\s*([*_-])(\s*\1){2,}\s*$/.test(line) ? '---' : line))
    .map((line) => line.replace(/^(\s*)[*+](\s+)/, '$1-$2'))
    .map((line) => line.replace(/^(\s*)(\d+)\)(\s+)/, '$1$2.$3'))
    .map(normalizeTableLine)
    .join('\n');
  // Blank-line placement is cosmetic for the serializer: it adds a blank line
  // between a paragraph and a following list/heading, and never merges or
  // splits paragraphs. Compare with blank lines removed.
  s = s.replace(/\n{2,}/g, '\n');
  // Backslash escapes of ASCII punctuation (`\$`, `\*`, `\--`) render the
  // same; the serializer adds them defensively.
  s = s.replace(/\\([!-\/:-@[-`{-~])/g, '$1');
  // GFM autolink literals re-serialize as <autolinks>.
  s = s.replace(/<((?:https?:\/\/|mailto:)[^>\s]+|[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+)>/g, '$1');
  // single-line display math written inline -> inline math
  s = s.replace(/\$\$([^\n$]+?)\$\$/g, (_m, inner) => `$${inner}$`);
  s = s.replace(/__([^_\n]+?)__/g, '**$1**');
  s = s.replace(/(^|[^\w_])_([^_\n]+?)_(?![\w_])/g, '$1*$2*');
  s = s.replace(/^(\s*-\s+)\[[xX]\]/gm, '$1[x]');
  return s.replace(/^\n+/, '').replace(/\n+$/, '');
}

export async function probeRoundTrip(markdown) {
  if (typeof document === 'undefined') return { ok: true, serialized: markdown };
  const host = document.createElement('div');
  host.setAttribute('aria-hidden', 'true');
  host.style.cssText = 'position:absolute;left:-10000px;top:0;width:600px;height:1px;overflow:hidden;';
  document.body.appendChild(host);
  let editor = null;
  try {
    editor = await makeEditor(host, markdown || '', null);
    let serialized = '';
    editor.action((ctx) => { serialized = serialize(ctx); });
    const ok = normalizeForProbe(serialized) === normalizeForProbe(markdown);
    return { ok, serialized };
  } catch (error) {
    return { ok: false, serialized: '', error: String((error && error.message) || error) };
  } finally {
    try { if (editor) await editor.destroy(); } catch (_e) {}
    host.remove();
  }
}
