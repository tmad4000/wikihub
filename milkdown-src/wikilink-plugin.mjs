// WikiHub custom Milkdown plugins: lossless [[wikilink]], [[target|alias]], and
// ![[embed]] round-tripping.
//
// Approach (per Milkdown discussion #1674 / the marker-plugin example):
//   - a $remark plugin that
//       (parse)     walks mdast text nodes and splits out custom `wikiLink` /
//                   `wikiEmbed` nodes for [[..]] / ![[..]] syntax, and
//       (stringify) registers mdast-util-to-markdown handlers that re-emit the
//                   LITERAL [[..]] / ![[..]] text with NO backslash escaping.
//   - a $node (ProseMirror schema) with parseMarkdown/toMarkdown runners that
//     bridge the custom mdast node <-> an atom inline ProseMirror node.
//   - a $inputRule for live typing.
//
// The crucial bug this fixes: the commonmark-only serializer treated [[ ]] as
// literal text and escaped the brackets (and `_` inside math) on save. Because
// our stringify handlers return raw strings (custom node types never pass
// through mdast's `safe()` text escaper), the syntax round-trips byte-for-byte.

import { $remark, $nodeSchema, $inputRule } from '@milkdown/utils';
import { InputRule } from '@milkdown/prose/inputrules';

// ---------------------------------------------------------------------------
// mdast helpers
// ---------------------------------------------------------------------------

// [[target]] or [[target|alias]]  — captures target and optional alias
const WIKILINK_RE = /!?\[\[([^\]\n|]+)(?:\|([^\]\n]+))?\]\]/g;

// Split a single mdast `text` node into a sequence of text / wikiLink / wikiEmbed
// nodes. Returns null when there is nothing to split (so callers can skip).
function splitTextNode(value) {
  WIKILINK_RE.lastIndex = 0;
  let match;
  let lastIndex = 0;
  const out = [];
  while ((match = WIKILINK_RE.exec(value)) !== null) {
    const full = match[0];
    const isEmbed = full.startsWith('!');
    const start = match.index;
    if (start > lastIndex) {
      out.push({ type: 'text', value: value.slice(lastIndex, start) });
    }
    const target = match[1].trim();
    const alias = match[2] != null ? match[2].trim() : null;
    out.push({
      type: isEmbed ? 'wikiEmbed' : 'wikiLink',
      target,
      alias,
      // keep the exact original literal so serialization is byte-identical
      value: full,
    });
    lastIndex = start + full.length;
  }
  if (out.length === 0) return null;
  if (lastIndex < value.length) {
    out.push({ type: 'text', value: value.slice(lastIndex) });
  }
  return out;
}

// unified transformer: walk the tree, expand text nodes that contain [[..]].
function remarkWikilinkTransformer() {
  return (tree) => {
    visit(tree);
  };

  function visit(node) {
    if (!node || !Array.isArray(node.children)) return;
    const next = [];
    for (const child of node.children) {
      if (child.type === 'text' && typeof child.value === 'string') {
        const parts = splitTextNode(child.value);
        if (parts) {
          next.push(...parts);
          continue;
        }
      }
      visit(child);
      next.push(child);
    }
    node.children = next;
  }
}

// stringify handlers: re-emit the literal syntax, unescaped.
function emitWikiLink(node) {
  if (typeof node.value === 'string' && node.value) return node.value;
  const inner = node.alias ? `${node.target}|${node.alias}` : node.target;
  return `[[${inner}]]`;
}
function emitWikiEmbed(node) {
  if (typeof node.value === 'string' && node.value) return node.value;
  const inner = node.alias ? `${node.target}|${node.alias}` : node.target;
  return `![[${inner}]]`;
}

// The unified plugin combining parse-transform + stringify extension.
function remarkWikilink() {
  const self = this;
  const data = self.data();
  const toMarkdownExtensions =
    data.toMarkdownExtensions || (data.toMarkdownExtensions = []);
  toMarkdownExtensions.push({
    handlers: {
      wikiLink: emitWikiLink,
      wikiEmbed: emitWikiEmbed,
    },
  });
  return remarkWikilinkTransformer();
}

// ---------------------------------------------------------------------------
// $remark plugin
// ---------------------------------------------------------------------------

export const remarkWikilinkPlugin = $remark('wikihubWikilink', () => remarkWikilink);

// ---------------------------------------------------------------------------
// ProseMirror node: inline atom for [[wikilink]] / [[target|alias]]
// ---------------------------------------------------------------------------

export const wikilinkSchema = $nodeSchema('wikilink', () => ({
  group: 'inline',
  inline: true,
  atom: true,
  attrs: {
    target: { default: '' },
    alias: { default: null },
    raw: { default: '' },
  },
  parseDOM: [
    {
      tag: 'span[data-wikilink]',
      getAttrs: (dom) => ({
        target: dom.getAttribute('data-target') || '',
        alias: dom.getAttribute('data-alias') || null,
        raw: dom.getAttribute('data-raw') || '',
      }),
    },
  ],
  toDOM: (node) => {
    const { target, alias, raw } = node.attrs;
    const label = alias || target;
    return [
      'span',
      {
        'data-wikilink': 'true',
        'data-target': target,
        'data-alias': alias || '',
        'data-raw': raw,
        class: 'wikilink',
      },
      `[[${label}]]`,
    ];
  },
  parseMarkdown: {
    match: (node) => node.type === 'wikiLink',
    // parser addNode(nodeType, attrs, content)
    runner: (state, node, type) => {
      state.addNode(type, {
        target: node.target,
        alias: node.alias ?? null,
        raw: node.value || '',
      });
    },
  },
  toMarkdown: {
    match: (node) => node.type.name === 'wikilink',
    runner: (state, node) => {
      state.addNode('wikiLink', undefined, undefined, {
        target: node.attrs.target,
        alias: node.attrs.alias,
        value:
          node.attrs.raw ||
          `[[${node.attrs.alias ? `${node.attrs.target}|${node.attrs.alias}` : node.attrs.target}]]`,
      });
    },
  },
}));

// ---------------------------------------------------------------------------
// ProseMirror node: inline atom for ![[embed]]
// ---------------------------------------------------------------------------

export const wikiembedSchema = $nodeSchema('wikiembed', () => ({
  group: 'inline',
  inline: true,
  atom: true,
  attrs: {
    target: { default: '' },
    alias: { default: null },
    raw: { default: '' },
  },
  parseDOM: [
    {
      tag: 'span[data-wikiembed]',
      getAttrs: (dom) => ({
        target: dom.getAttribute('data-target') || '',
        alias: dom.getAttribute('data-alias') || null,
        raw: dom.getAttribute('data-raw') || '',
      }),
    },
  ],
  toDOM: (node) => {
    const { target, alias, raw } = node.attrs;
    const label = alias || target;
    return [
      'span',
      {
        'data-wikiembed': 'true',
        'data-target': target,
        'data-alias': alias || '',
        'data-raw': raw,
        class: 'wikiembed',
      },
      `![[${label}]]`,
    ];
  },
  parseMarkdown: {
    match: (node) => node.type === 'wikiEmbed',
    // parser addNode(nodeType, attrs, content)
    runner: (state, node, type) => {
      state.addNode(type, {
        target: node.target,
        alias: node.alias ?? null,
        raw: node.value || '',
      });
    },
  },
  toMarkdown: {
    match: (node) => node.type.name === 'wikiembed',
    runner: (state, node) => {
      state.addNode('wikiEmbed', undefined, undefined, {
        target: node.attrs.target,
        alias: node.attrs.alias,
        value:
          node.attrs.raw ||
          `![[${node.attrs.alias ? `${node.attrs.target}|${node.attrs.alias}` : node.attrs.target}]]`,
      });
    },
  },
}));

// ---------------------------------------------------------------------------
// Input rules — live typing turns [[x]] / ![[x]] into the atom node
// ---------------------------------------------------------------------------

// ![[..]] must be tested before [[..]] so the embed wins.
export const wikiembedInputRule = $inputRule((ctx) =>
  new InputRule(/!\[\[([^\]\n|]+)(?:\|([^\]\n]+))?\]\]$/, (state, match, start, end) => {
    const [full, target, alias] = match;
    const type = wikiembedSchema.type(ctx);
    const node = type.create({
      target: (target || '').trim(),
      alias: alias != null ? alias.trim() : null,
      raw: full,
    });
    return state.tr.replaceWith(start, end, node);
  })
);

export const wikilinkInputRule = $inputRule((ctx) =>
  new InputRule(/\[\[([^\]\n|]+)(?:\|([^\]\n]+))?\]\]$/, (state, match, start, end) => {
    const [full, target, alias] = match;
    const type = wikilinkSchema.type(ctx);
    const node = type.create({
      target: (target || '').trim(),
      alias: alias != null ? alias.trim() : null,
      raw: full,
    });
    return state.tr.replaceWith(start, end, node);
  })
);

export const wikihubWikilink = [
  remarkWikilinkPlugin,
  wikilinkSchema,
  wikiembedSchema,
  wikiembedInputRule,
  wikilinkInputRule,
].flat();
