// Build script: bundles entry.mjs into ../app/static/js/milkdown-bundle.js
//
// Run:  npm run build     (from milkdown-src/)
//
// Output is a single ESM file exposing createMilkdownEditor(container,
// initialMarkdown, onUpdate) — the SAME API the editor template imports.
//
// KaTeX's stylesheet is imported from JS; we load .css as text and inject it as
// a <style> tag at runtime so the output stays a single self-contained .js file
// (no separate .css asset to wire into the template). Font files referenced by
// KaTeX CSS resolve against KaTeX's CDN-style relative paths; the editor only
// needs the layout rules, which are inlined here.

import * as esbuild from 'esbuild';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const outfile = resolve(__dirname, '../app/static/js/milkdown-bundle.js');

// esbuild plugin: turn `import 'x.css'` into a runtime style injection.
const cssInjectPlugin = {
  name: 'css-inject',
  setup(build) {
    build.onLoad({ filter: /\.css$/ }, (args) => {
      const css = readFileSync(args.path, 'utf8');
      const contents = `
        (function () {
          if (typeof document === 'undefined') return;
          var id = 'milkdown-katex-css';
          if (document.getElementById(id)) return;
          var style = document.createElement('style');
          style.id = id;
          style.textContent = ${JSON.stringify(css)};
          document.head.appendChild(style);
        })();
      `;
      return { contents, loader: 'js' };
    });
  },
};

await esbuild.build({
  entryPoints: [resolve(__dirname, 'entry.mjs')],
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: ['es2020'],
  minify: true,
  sourcemap: false,
  legalComments: 'eof',
  outfile,
  plugins: [cssInjectPlugin],
  loader: {
    '.woff': 'empty',
    '.woff2': 'empty',
    '.ttf': 'empty',
  },
});

console.log('Built', outfile);
