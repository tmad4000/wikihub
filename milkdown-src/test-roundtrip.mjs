// Acceptance test for the Milkdown bundle (wikihub-oyf4, wikihub-k7k5).
//
// Loads the BUILT app/static/js/milkdown-bundle.js into headless Chromium and:
//   1. exercises createMilkdownEditor directly (the same API the editor uses),
//      performs a real edit to force re-serialization through the doc-changed
//      sync path, and asserts the constructs round-trip without corruption;
//   2. checks probeRoundTrip() (the Visual-mode safety probe): GFM constructs
//      must probe ok, constructs the visual editor would rewrite (reference
//      links) must probe NOT ok so the page opens in Markdown;
//   3. checks the load-time parse never fires onUpdate (D8).
//
// Run:  node test-roundtrip.mjs   (from milkdown-src/)
// Browser driver: PUPPETEER_PATH=/path/to/node_modules/puppeteer, or
// PLAYWRIGHT_PATH=/path/to/node_modules/playwright (defaults to the global
// playwright install if present). Skips with exit 0 when neither is available.

import http from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { execSync } from 'node:child_process';
import { createRequire } from 'node:module';

const __dirname = dirname(fileURLToPath(import.meta.url));
const bundlePath = resolve(__dirname, '../app/static/js/milkdown-bundle.js');

if (!existsSync(bundlePath)) {
  console.error('FAIL: bundle not built at', bundlePath);
  process.exit(1);
}
const bundleSrc = readFileSync(bundlePath, 'utf8');

async function launchBrowser() {
  if (process.env.PUPPETEER_PATH) {
    const require = createRequire(import.meta.url);
    const puppeteer = require(process.env.PUPPETEER_PATH);
    const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
    return { browser, kind: 'puppeteer' };
  }
  let pwPath = process.env.PLAYWRIGHT_PATH;
  if (!pwPath) {
    try {
      pwPath = join(execSync('npm root -g', { encoding: 'utf8' }).trim(), 'playwright');
    } catch (_e) { /* ignore */ }
  }
  if (pwPath && existsSync(join(pwPath, 'index.mjs'))) {
    const pw = await import(pathToFileURL(join(pwPath, 'index.mjs')).href);
    const browser = await pw.chromium.launch({ headless: true });
    return { browser, kind: 'playwright' };
  }
  return null;
}

// Each round-trip case asserts the output after a real edit. `mode`:
//   'exact'    — output must equal input byte-for-byte.
//   'noescape' — no backslash escaping may be introduced and math content
//                survives verbatim modulo inline `$$..$$` -> `$..$`.
//   'probe'    — output equals input under normalizeForProbe (marker style).
const ROUNDTRIP_CASES = [
  { input: 'See [[SomePage]] and [[a/b|Alias]].', mode: 'exact' },
  { input: 'Math $E=mc^2$ and $$\\int_0^1 x^2 dx$$', mode: 'noescape' },
  { input: 'Embed ![[diagram.png]]', mode: 'exact' },
  { input: '| a | b |\n| - | - |\n| 1 | 2 |\n| 3 | 4 |', mode: 'probe' },
  { input: '- [ ] todo\n- [x] done', mode: 'probe' },
  { input: 'Some ~~struck~~ text.', mode: 'exact' },
  { input: 'Footnote here.[^1]\n\n[^1]: The note.', mode: 'probe' },
];

// probeRoundTrip expectations.
const PROBE_CASES = [
  { input: '# Title\n\nPara with **bold**, *em*, `code` and [[Wiki|Link]].\n\n- a\n- b\n', ok: true },
  { input: '| a | b |\n| --- | --- |\n| 1 | 2 |\n', ok: true },
  { input: '- [ ] task\n- [x] done\n', ok: true },
  { input: 'A ~~strike~~ word.\n', ok: true },
  { input: 'Note.[^1]\n\n[^1]: Footnote body.\n', ok: true },
  { input: 'See [the docs][docs].\n\n[docs]: https://example.com\n', ok: false },
  // inline $$..$$ renders as display math; Visual would turn it into $..$
  { input: 'Energy $$E=mc^2$$ inline.\n', ok: false },
  { input: 'Inline $E=mc^2$ math.\n', ok: true },
  { input: '$$\n\\int_0^1 x^2 dx\n$$\n', ok: true },
];

const html = `<!doctype html><html><head><meta charset="utf-8"></head>
<body>
  <div id="milkdown-editor"></div>
  <script type="module">
    import * as M from '/milkdown-bundle.js';
    window.__M = M;
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

const launched = await launchBrowser();
if (!launched) {
  console.log('SKIP: no browser driver (set PUPPETEER_PATH or PLAYWRIGHT_PATH)');
  process.exit(0);
}
const { browser } = launched;

await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;

let failed = false;
const lines = [];

async function freshPage() {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.goto(base);
  for (let i = 0; i < 150; i++) {
    if (await page.evaluate(() => window.__ready === true)) break;
    await new Promise((r) => setTimeout(r, 100));
  }
  return { page, errors };
}

try {
  for (const { input, mode } of ROUNDTRIP_CASES) {
    const { page, errors } = await freshPage();
    const out = await page.evaluate(async (initial) => {
      const container = document.getElementById('milkdown-editor');
      let last = null;
      let fired = 0;
      const editor = await window.__M.createMilkdownEditor(container, initial, (md) => { fired++; last = md; });
      await new Promise((r) => setTimeout(r, 300));
      const loadFires = fired;
      let view;
      editor.action((ctx) => { view = ctx.get(window.__M.editorViewCtx); });
      // Insert then delete a char inside the first textblock: a genuine pair
      // of transactions leaving the doc identical, forcing serialization.
      let pos = 1;
      view.state.doc.descendants((node, p) => {
        if (pos === 1 && node.isTextblock && node.content.size > 0) { pos = p + 1; return false; }
        return pos === 1;
      });
      view.dispatch(view.state.tr.insertText('x', pos));
      view.dispatch(view.state.tr.delete(pos, pos + 1));
      await new Promise((r) => setTimeout(r, 400));
      return { md: last, fired, loadFires };
    }, input);
    const normalized = (out.md || '').trim();
    const inBs = (input.match(/\\/g) || []).length;
    const outBs = (normalized.match(/\\/g) || []).length;
    let ok;
    if (mode === 'exact') ok = normalized === input.trim();
    else if (mode === 'noescape') {
      const strip = (s) => s.replace(/\${1,2}/g, '$');
      ok = outBs <= inBs && strip(normalized) === strip(input.trim());
    } else {
      ok = await page.evaluate(([a, b]) => window.__M.normalizeForProbe(a) === window.__M.normalizeForProbe(b), [normalized, input]);
    }
    ok = ok && out.fired > 0 && out.loadFires === 0 && errors.length === 0;
    if (!ok) failed = true;
    lines.push(`${ok ? 'PASS' : 'FAIL'}  roundtrip [${mode}] ${JSON.stringify(input)}\n      out: ${JSON.stringify(normalized)}  loadFires=${out.loadFires} fires=${out.fired}${errors.length ? ' errors=' + JSON.stringify(errors) : ''}`);
    await page.close();
  }

  {
    const { page, errors } = await freshPage();
    for (const { input, ok: expected } of PROBE_CASES) {
      const res = await page.evaluate((md) => window.__M.probeRoundTrip(md), input);
      const ok = res.ok === expected && errors.length === 0;
      if (!ok) failed = true;
      lines.push(`${ok ? 'PASS' : 'FAIL'}  probe expect ok=${expected} got ok=${res.ok} ${JSON.stringify(input)}\n      serialized: ${JSON.stringify(res.serialized)}`);
    }
    await page.close();
  }
} finally {
  await browser.close();
  server.close();
}

console.log('\n=== Milkdown bundle acceptance (wikihub-oyf4, wikihub-k7k5) ===\n');
console.log(lines.join('\n'));
console.log(failed ? '\nRESULT: FAIL' : '\nRESULT: PASS');
process.exit(failed ? 1 : 0);
