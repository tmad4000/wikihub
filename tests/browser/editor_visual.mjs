// Browser-level regression for the Visual editor (wikihub-k7k5).
//
// tests/test_e2e.py covers the server side (markup, ?mode=, suggest-edit
// frontmatter handling). This script covers the JS behaviour a Flask test
// client cannot: it drives a REAL running WikiHub in headless Chromium.
//
//   BASE_URL=http://127.0.0.1:7538 node tests/browser/editor_visual.mjs [--out DIR] [--shots-only]
//
// It seeds its own account + wiki through the public API, then checks:
//   1. Visual is the default mode; no "round-trip losslessly" hint.
//   2. Toolbar Bold (a mouse action outside the editor) syncs the textarea (D8).
//   3. `[[` autocomplete inside Visual inserts `[[target|Title]]`.
//   4. Slash menu inserts a block and syncs.
//   5. The safety probe opens a page the visual editor would rewrite in
//      Markdown with the "Open visual anyway" notice, and does NOT remember it.
//   6. Choosing a tab is remembered per browser (localStorage).
//   7. Suggest-edit: Visual by default, frontmatter hidden, submitting a visual
//      edit creates a proposal that keeps the page frontmatter.
//
// --shots-only skips assertions (used for "before" screenshots of old builds).
// Playwright: PLAYWRIGHT_PATH or the global install (`npm root -g`).

import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { execSync } from 'node:child_process';

const args = process.argv.slice(2);
const outIdx = args.indexOf('--out');
const OUT = outIdx !== -1 ? args[outIdx + 1] : null;
const SHOTS_ONLY = args.includes('--shots-only');
const PREFIX = (() => { const i = args.indexOf('--prefix'); return i !== -1 ? args[i + 1] : ''; })();
const BASE = (process.env.BASE_URL || '').replace(/\/$/, '');
if (!BASE) {
  console.error('BASE_URL is required');
  process.exit(2);
}
if (OUT) mkdirSync(OUT, { recursive: true });

const pwPath = process.env.PLAYWRIGHT_PATH || join(execSync('npm root -g', { encoding: 'utf8' }).trim(), 'playwright');
if (!existsSync(join(pwPath, 'index.mjs'))) {
  console.log('SKIP: playwright not installed');
  process.exit(0);
}
const { chromium } = await import(pathToFileURL(join(pwPath, 'index.mjs')).href);

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail && !ok ? ' — ' + detail : ''}`);
}

async function api(method, path, body, key) {
  const res = await fetch(BASE + path, {
    method,
    headers: { 'Content-Type': 'application/json', ...(key ? { Authorization: 'Bearer ' + key } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = JSON.parse(text); } catch (_e) { data = text; }
  return { status: res.status, data };
}

// ---- seed ------------------------------------------------------------------
const user = 'vis' + Math.random().toString(36).slice(2, 8);
const acct = await api('POST', '/api/v1/accounts', { username: user, password: 'visualpass12345' });
if (acct.status !== 201) { console.error('seed account failed', acct); process.exit(1); }
const KEY = acct.data.api_key;
await api('POST', '/api/v1/wikis', { slug: 'guide', title: 'Guide' }, KEY);
const fm = (title) => `---\ntitle: ${title}\nvisibility: public-edit\n---\n\n`;
const PAGES = {
  'Common-App.md': fm('Common App') + '# Common App\n\nThe shared application.\n',
  'Harvard.md': fm('Harvard') + '# Harvard\n\nNotes about Harvard.\n',
  'Checklist.md': fm('Application Checklist') +
    '# Application Checklist\n\nStart with the essentials.\n\n| Program | Deadline |\n| --- | --- |\n| Early Action | November 1 |\n\n- [ ] Ask for recommendations\n- [x] Draft the essay\n',
  'Reference.md': fm('Reference') + '# Reference\n\nSee [the docs][docs].\n\n[docs]: https://example.com\n',
};
for (const [path, content] of Object.entries(PAGES)) {
  const r = await api('POST', `/api/v1/wikis/${user}/guide/pages`, { path, content, visibility: 'public-edit' }, KEY);
  if (r.status !== 201) { console.error('seed page failed', path, r); process.exit(1); }
}
const W = `${BASE}/@${user}/guide`;

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const page = await context.newPage();
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(e.message));
// Leaving a dirty editor raises the beforeunload guard; accept it.
page.on('dialog', (d) => d.accept().catch(() => {}));
const stored = {};
async function storedContent(path) {
  const r = await api('GET', `/api/v1/wikis/${user}/guide/pages/${path}`, null, KEY);
  return r.data.content;
}

const shot = async (name) => { if (OUT) await page.screenshot({ path: join(OUT, PREFIX + name + '.png') }); };
const textarea = () => page.$eval('#editor-textarea', (el) => el.value);
const settle = (ms = 400) => page.waitForTimeout(ms);
const waitVisual = () => page.waitForSelector('#milkdown-editor .ProseMirror', { timeout: 15000 }).catch(() => null);

try {
  // 1. default Visual mode, page editor ------------------------------------
  await page.goto(`${W}/Checklist/edit`);
  await waitVisual();
  await settle(800);
  await shot('editor-default');
  if (!SHOTS_ONLY) {
    const active = await page.$eval('.editor-tab.active', (el) => el.dataset.mode).catch(() => null);
    check('page editor opens in Visual', active === 'visual', `active=${active}`);
    const html = await page.content();
    check('old hint removed', !html.includes('round-trip losslessly'));
    check('table renders as a table in Visual', !!(await page.$('#milkdown-editor table')));
    check('task list renders as tasks', (await page.$$('#milkdown-editor li[data-item-type="task"]')).length === 2);
    stored.checklist = await storedContent('Checklist.md');
    check('viewing does not modify content', (await textarea()) === stored.checklist);
  }

  if (!SHOTS_ONLY) {
    // 2. toolbar Bold syncs ------------------------------------------------
    await page.click('#milkdown-editor .ProseMirror p');
    await page.keyboard.press('End');
    await page.keyboard.type(' Bold me');
    for (let i = 0; i < 7; i++) await page.keyboard.press('Shift+ArrowLeft');
    await page.click('#editor-toolbar [data-cmd="bold"]');
    await settle();
    const afterBold = await textarea();
    check('toolbar Bold syncs to Markdown', afterBold.includes('**Bold me**'), afterBold.slice(0, 200));
    const storedFm = stored.checklist.match(/^---\n[\s\S]*?\n---\n/)[0];
    check('frontmatter preserved through Visual edits', afterBold.startsWith(storedFm), afterBold.slice(0, 120));
    const pressed = await page.$eval('#editor-toolbar [data-cmd="bold"]', (el) => el.getAttribute('aria-pressed'));
    check('Bold button reflects active state', pressed === 'true', `aria-pressed=${pressed}`);

    // 3. [[ autocomplete in Visual -----------------------------------------
    await page.keyboard.press('End');
    await page.keyboard.press('Enter');
    await page.keyboard.type('See [[Common');
    await page.waitForSelector('#wikilink-menu.open .wikilink-item', { timeout: 5000 }).catch(() => null);
    await settle(200);
    await shot('editor-wikilink-menu');
    await page.keyboard.press('Enter');
    await settle();
    const afterLink = await textarea();
    check('[[ autocomplete inserts [[target|Title]] in Visual', afterLink.includes('See [[Common-App|Common App]]'), afterLink.slice(-200));

    // 4. slash menu ----------------------------------------------------------
    await page.keyboard.press('End');
    await page.keyboard.press('Enter');
    await page.keyboard.type('/quo');
    await page.waitForSelector('#slash-menu.open .slash-item', { timeout: 5000 }).catch(() => null);
    await shot('editor-slash-menu');
    await page.keyboard.press('Enter');
    await page.keyboard.type('A quoted line');
    await settle();
    const afterSlash = await textarea();
    check('slash menu inserts a quote block', /\n> A quoted line/.test(afterSlash), afterSlash.slice(-200));
    await shot('editor-after-edits');

    // 6a. Markdown tab is remembered -----------------------------------------
    await page.click('.editor-tab[data-mode="markdown"]');
    const storedMode = await page.evaluate(() => localStorage.getItem('wikihub.editor.mode'));
    check('choosing Markdown is remembered', storedMode === 'markdown', `stored=${storedMode}`);
    await page.goto(`${W}/Harvard/edit`);
    await settle(1200);
    const remembered = await page.$eval('.editor-tab.active', (el) => el.dataset.mode);
    check('remembered Markdown applies on next open', remembered === 'markdown', `active=${remembered}`);
    await page.click('.editor-tab[data-mode="visual"]');
    await waitVisual();
    check('choosing Visual is remembered', (await page.evaluate(() => localStorage.getItem('wikihub.editor.mode'))) === 'visual');
  }

  // 5. safety probe ------------------------------------------------------------
  await page.evaluate(() => localStorage.removeItem('wikihub.editor.mode'));
  await page.goto(`${W}/Reference/edit`);
  await settle(1500);
  await shot('editor-probe-fallback');
  if (!SHOTS_ONLY) {
    const active = await page.$eval('.editor-tab.active', (el) => el.dataset.mode);
    check('probe opens unsafe page in Markdown', active === 'markdown', `active=${active}`);
    const noticeText = await page.$eval('#editor-mode-notice', (el) => el.classList.contains('open') ? el.textContent : '');
    check('probe shows the fallback notice', noticeText.includes('Opened in Markdown'), noticeText);
    check('probe fallback is not remembered', (await page.evaluate(() => localStorage.getItem('wikihub.editor.mode'))) === null);
    await page.click('#editor-mode-notice .editor-banner-btn.primary');
    await waitVisual();
    const nowActive = await page.$eval('.editor-tab.active', (el) => el.dataset.mode);
    check('"Open visual anyway" enters Visual', nowActive === 'visual');
    check('entering Visual without editing leaves content untouched', (await textarea()) === (await storedContent('Reference.md')));

    // The probe also guards tab switches, not just the initial load.
    await page.goto(`${W}/Harvard/edit?mode=markdown`);
    await settle(800);
    await page.$eval('#editor-textarea', (el) => {
      el.value += '\n\nInline $$E=mc^2$$ display.\n';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.click('.editor-tab[data-mode="visual"]');
    await settle(1200);
    const afterTab = await page.$eval('.editor-tab.active', (el) => el.dataset.mode);
    const tabNotice = await page.$eval('#editor-mode-notice', (el) => el.classList.contains('open'));
    check('Visual tab on an unsafe edit stays in Markdown with notice', afterTab === 'markdown' && tabNotice, `active=${afterTab} notice=${tabNotice}`);
    await shot('editor-probe-on-tab');
  }

  // 7. suggest-edit ----------------------------------------------------------------
  const anon = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const sp = await anon.newPage();
  sp.on('pageerror', (e) => pageErrors.push('suggest: ' + e.message));
  await sp.goto(`${W}/-/suggest/Harvard`);
  await sp.waitForSelector('#milkdown-editor .ProseMirror', { timeout: 15000 }).catch(() => null);
  await sp.waitForTimeout(800);
  if (OUT) await sp.screenshot({ path: join(OUT, PREFIX + 'suggest-default.png') });
  if (!SHOTS_ONLY) {
    const active = await sp.$eval('.editor-tab.active', (el) => el.dataset.mode);
    check('suggest-edit opens in Visual', active === 'visual', `active=${active}`);
    const html = await sp.content();
    check('suggest-edit hides frontmatter', !html.includes('visibility: public-edit'));
    await sp.click('#milkdown-editor .ProseMirror p');
    await sp.keyboard.press('End');
    await sp.keyboard.type(' Added by a student.');
    await sp.fill('#suggest-author', 'student');
    await sp.fill('#suggest-note', 'Small addition.');
    await sp.waitForTimeout(300);
    if (OUT) await sp.screenshot({ path: join(OUT, PREFIX + 'suggest-edited.png') });
    await Promise.all([sp.waitForNavigation(), sp.click('.suggest-btn.primary')]);
    check('suggest submit lands on the proposal', /\/-\/proposals\/\d+/.test(sp.url()), sp.url());
    // Owner reads the proposal through the API-key login.
    const owner = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const op = await owner.newPage();
    const proposalPath = new URL(sp.url()).pathname;
    await op.goto(`${BASE}/auth/login?api_key=${KEY}&next=${encodeURIComponent(proposalPath)}`);
    const body = await op.content();
    check('proposal contains the visual edit', body.includes('Added by a student.'));
    if (OUT) await op.screenshot({ path: join(OUT, PREFIX + 'suggest-proposal.png') });
    await owner.close();
  }
  await anon.close();

  // owner view with Properties ------------------------------------------------------
  if (!SHOTS_ONLY) {
    const owner = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const op = await owner.newPage();
    await op.goto(`${BASE}/auth/login?api_key=${KEY}&next=${encodeURIComponent(`/@${user}/guide/Checklist/edit`)}`);
    await op.waitForSelector('#milkdown-editor .ProseMirror', { timeout: 15000 }).catch(() => null);
    await op.click('#page-properties summary');
    await op.fill('#prop-tags', 'admissions, checklist');
    await op.press('#prop-tags', 'Tab');
    await op.check('#prop-pinned');
    await op.waitForTimeout(300);
    const v = await op.$eval('#editor-textarea', (el) => el.value);
    check('Properties write tags/pinned into frontmatter', v.includes('tags: [admissions, checklist]') && v.includes('pinned: true'), v.slice(0, 160));
    if (OUT) await op.screenshot({ path: join(OUT, PREFIX + 'editor-owner-properties.png') });
    await owner.close();
  }

  if (!SHOTS_ONLY) check('no uncaught page errors', pageErrors.length === 0, pageErrors.join(' | '));
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.ok);
console.log(SHOTS_ONLY ? 'shots done' : `\n${results.length - failed.length}/${results.length} browser checks passed`);
process.exit(failed.length ? 1 : 0);
