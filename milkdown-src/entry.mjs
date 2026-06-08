// WikiHub Milkdown bundle entry point.
//
// Mirrors the legacy app/static/js/milkdown-entry.js API (createMilkdownEditor
// with the SAME signature) but adds lossless round-tripping for:
//   - $inline$ / $$display$$ math  (official @milkdown/plugin-math, KaTeX)
//   - [[wikilink]] / [[target|alias]]  (custom plugin, ./wikilink-plugin)
//   - ![[embed]]  (custom plugin, ./wikilink-plugin)
//
// Built with esbuild into app/static/js/milkdown-bundle.js (see build.mjs).

import { Editor, rootCtx, defaultValueCtx, editorViewCtx } from '@milkdown/core';
import { commonmark } from '@milkdown/preset-commonmark';
import { history } from '@milkdown/plugin-history';
import { listener, listenerCtx } from '@milkdown/plugin-listener';
import { math } from '@milkdown/plugin-math';

// KaTeX stylesheet so rendered math is legible inside the WYSIWYG surface.
import 'katex/dist/katex.min.css';

import { wikihubWikilink } from './wikilink-plugin.mjs';

export {
  Editor,
  rootCtx,
  defaultValueCtx,
  editorViewCtx,
  commonmark,
  history,
  listener,
  listenerCtx,
  math,
};

// Unchanged public API: createMilkdownEditor(container, initialContent, onUpdate)
export async function createMilkdownEditor(container, initialContent, onUpdate) {
  const editor = await Editor.make()
    .config((ctx) => {
      ctx.set(rootCtx, container);
      ctx.set(defaultValueCtx, initialContent || '');
    })
    .use(commonmark)
    .use(math)
    .use(wikihubWikilink)
    .use(history)
    .use(listener)
    .create();

  if (onUpdate) {
    editor.action((ctx) => {
      ctx.get(listenerCtx).markdownUpdated((ctx, markdown, prevMarkdown) => {
        onUpdate(markdown);
      });
    });
  }

  return editor;
}
