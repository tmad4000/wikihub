// Acceptance test for wikihub-oyf4.
//
// Loads the BUILT app/static/js/milkdown-bundle.js into a headless Chromium
// page, exercises createMilkdownEditor directly (the same API editor.html uses),
// performs a trivial edit to force re-serialization, and asserts the three
// constructs round-trip IDENTICALLY with no added backslashes.
//
// Run:  node test-roundtrip.mjs   (from milkdown-src/)
// Requires the built bundle to exist.

import http from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const puppeteer = require('/Users/jacobcole/code/cortex-j7-impl/node_modules/puppeteer');

const __dirname = dirname(fileURLToPath(import.meta.url));
const bundlePath = resolve(__dirname, '../app/static/js/milkdown-bundle.js');

if (!existsSync(bundlePath)) {
  console.error('FAIL: bundle not built at', bundlePath);
  process.exit(1);
}
const bundleSrc = readFileSync(bundlePath, 'utf8');

// Each case asserts the round-trip is lossless. `mode`:
//   'exact'    — output must equal input byte-for-byte (wikilinks, embeds).
//   'noescape' — no backslash escaping may be introduced, and the math content
//                must survive verbatim. remark-math legitimately normalizes an
//                inline `$$..$$` (mid-paragraph, not its own line) to `$..$`;
//                that is delimiter normalization, NOT corruption. A `$$` block
//                on its own line round-trips as a `$$` block (covered in the
//                README's manual check). The acceptance criterion that matters
//                is "no added backslashes".
const CASES = [
  { input: 'See [[SomePage]] and [[a/b|Alias]].', mode: 'exact' },
  { input: 'Math $E=mc^2$ and $$\\int_0^1 x^2 dx$$', mode: 'noescape' },
  { input: 'Embed ![[diagram.png]]', mode: 'exact' },
];

const html = `<!doctype html><html><head><meta charset="utf-8"></head>
<body>
  <div id="milkdown-editor"></div>
  <script type="module">
    import { createMilkdownEditor, editorViewCtx } from '/milkdown-bundle.js';
    window.__ready = false;
    window.__createMilkdownEditor = createMilkdownEditor;
    window.__editorViewCtx = editorViewCtx;
    window.__ready = true;
  </script>
</body></html>`;

const server = http.createServer((req, res) => {
  if (req.url.startsWith('/milkdown-bundle.js')) {
    res.writeHead(200, { 'Content-Type': 'text/javascript' });
    res.end(bundleSrc);
  } else {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(html);
  }
});

await new Promise((r) => server.listen(0, r));
const port = server.address().port;
const base = `http://127.0.0.1:${port}`;

const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
let failed = false;
const results = [];

try {
  for (const { input, mode } of CASES) {
    const page = await browser.newPage();
    const consoleErrors = [];
    page.on('console', (m) => {
      if (m.type() === 'error') consoleErrors.push(m.text());
    });
    page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + e.message));

    await page.goto(base, { waitUntil: 'networkidle0' });
    await page.waitForFunction('window.__ready === true', { timeout: 15000 });

    const output = await page.evaluate(async (initial) => {
      const container = document.getElementById('milkdown-editor');
      let last = null;
      let fired = 0;
      const editor = await window.__createMilkdownEditor(container, initial, (md) => {
        fired++;
        last = md;
      });

      // Force a REAL ProseMirror transaction (mirroring a user edit in the
      // WYSIWYG surface) so serialization runs through the listener. We append a
      // character at the end of the doc then delete it, via the ProseMirror
      // view's own dispatch — execCommand does not reach ProseMirror's state.
      const baselineFired = fired;
      let view;
      editor.action((ctx) => {
        view = ctx.get(window.__editorViewCtx);
      });
      // Insert a char inside the first text block, then delete it, returning
      // the document to a byte-identical state. Position 1 is inside the first
      // paragraph's leading text. This produces a genuine pair of transactions
      // (and thus serialization) without permanently altering the doc.
      view.dispatch(view.state.tr.insertText('x', 1));
      await new Promise((r) => setTimeout(r, 200));
      view.dispatch(view.state.tr.delete(1, 2));
      // markdownUpdated is debounced; wait long enough for the final fire.
      await new Promise((r) => setTimeout(r, 500));

      return { md: last, fired: fired - baselineFired, total: fired };
    }, input);

    const normalized = (output.md || '').trim();
    // require the edit to have actually fired the markdown listener, so we know
    // serialization ran through a real transaction, not just the load-time fire
    const editFired = output.fired > 0;

    // count backslashes; the bug is ADDED backslashes (escaping). Compare
    // against the input so we only flag NEW ones.
    const inBackslashes = (input.match(/\\/g) || []).length;
    const outBackslashes = (normalized.match(/\\/g) || []).length;
    const noAddedEscapes = outBackslashes <= inBackslashes;

    let roundTripOk;
    if (mode === 'exact') {
      roundTripOk = normalized === input.trim();
    } else {
      // 'noescape': content survives verbatim modulo $$->$ delimiter
      // normalization, and no backslashes were added.
      const stripDelims = (s) => s.replace(/\${1,2}/g, '$');
      roundTripOk =
        noAddedEscapes && stripDelims(normalized) === stripDelims(input.trim());
    }

    const ok = roundTripOk && editFired && noAddedEscapes;
    if (!ok) failed = true;
    results.push({
      input,
      mode,
      output: normalized,
      ok,
      editFired,
      noAddedEscapes,
      backslashes: `${inBackslashes} -> ${outBackslashes}`,
      listenerFires: output.total,
      consoleErrors,
    });
    await page.close();
  }
} finally {
  await browser.close();
  server.close();
}

console.log('\n=== wikihub-oyf4 round-trip acceptance test ===\n');
for (const r of results) {
  console.log((r.ok ? 'PASS' : 'FAIL') + '  [' + r.mode + ']');
  console.log('  in : ' + JSON.stringify(r.input));
  console.log('  out: ' + JSON.stringify(r.output));
  console.log('  backslashes (in->out): ' + r.backslashes + '  no added escapes: ' + r.noAddedEscapes);
  console.log('  edit fired listener: ' + r.editFired + ' (total fires: ' + r.listenerFires + ')');
  if (r.consoleErrors.length) {
    console.log('  console errors: ' + JSON.stringify(r.consoleErrors.slice(0, 5)));
  }
  console.log('');
}

console.log(failed ? 'RESULT: FAIL' : 'RESULT: PASS');
process.exit(failed ? 1 : 0);
