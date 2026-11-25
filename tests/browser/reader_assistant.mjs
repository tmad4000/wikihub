// Browser-level regression for the reader assistant (wikihub-1bfx).
//
// Drives a REAL running WikiHub in headless Chromium. Start the server with
// the scripted model first (no API key needed):
//
//   python3 tests/browser/reader_assistant_server.py --port PORT &
//   BASE_URL=http://127.0.0.1:PORT node tests/browser/reader_assistant.mjs [--out DIR]
//
// Checks, through the normal reader UI:
//   1. A signed-out visitor sees "Ask", asks, and gets an answer whose [n]
//      reference and Sources card link to the readable page -- never the
//      private page or private-band text, even though the scripted model
//      tries to read and quote them.
//   2. Following a source lands on that page; the conversation is still there.
//   3. The private page itself is restricted for that visitor (the boundary).
//   4. The owner, asking from the private page, gets it as a cited source and
//      the link opens it.
//   5. Phone width: the panel is usable full-width.
// Playwright: PLAYWRIGHT_PATH or the global install (`npm root -g`).

import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { execSync } from 'node:child_process';

const args = process.argv.slice(2);
const outIdx = args.indexOf('--out');
const OUT = outIdx !== -1 ? args[outIdx + 1] : null;
const BASE = (process.env.BASE_URL || '').replace(/\/$/, '');
if (!BASE) { console.error('BASE_URL is required'); process.exit(2); }
if (OUT) mkdirSync(OUT, { recursive: true });

const pwPath = process.env.PLAYWRIGHT_PATH || join(execSync('npm root -g', { encoding: 'utf8' }).trim(), 'playwright');
if (!existsSync(join(pwPath, 'index.mjs'))) { console.log('SKIP: playwright not installed'); process.exit(0); }
const { chromium } = await import(pathToFileURL(join(pwPath, 'index.mjs')).href);

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail && !ok ? ' — ' + detail : ''}`);
}
async function api(method, path, body, key) {
  const res = await fetch(BASE + path, {
    method,
    headers: { 'Content-Type': 'application/json', ...(key ? { Authorization: 'Bearer ' + key } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null; try { data = JSON.parse(text); } catch (_e) { data = text; }
  return { status: res.status, data };
}

// ---- seed --------------------------------------------------------------
const user = 'ask' + Math.random().toString(36).slice(2, 8);
const acct = await api('POST', '/api/v1/accounts', { username: user, password: 'askpass12345' });
if (acct.status !== 201) { console.error('seed account failed', acct); process.exit(1); }
const KEY = acct.data.api_key;
await api('POST', '/api/v1/wikis', { slug: 'garden', title: 'Garden Notes', description: 'A small garden wiki' }, KEY);
const seed = [
  ['guide.md', 'public', '---\ntitle: Garden Guide\nvisibility: public\n---\n\n# Garden Guide\n\nTomatoes need full sun and deep watering every week.\n\nSee also [[compost]].\n\n<!-- private -->\nBANDSECRET owner only reminder.\n<!-- /private -->\n'],
  ['compost.md', 'public', '---\ntitle: Compost\nvisibility: public\n---\n\n# Compost\n\nTurn the compost pile every two weeks to keep it aerobic.\n'],
  ['private-notes.md', 'private', '---\ntitle: PRIVTITLE Notes\nvisibility: private\n---\n\n# PRIVTITLE Notes\n\nPRIVSECRET greenhouse budget is 400 dollars.\n'],
];
for (const [path, visibility, content] of seed) {
  let r = await api('POST', `/api/v1/wikis/${user}/garden/pages`, { path, content, visibility }, KEY);
  if (r.status === 409) r = await api('PUT', `/api/v1/wikis/${user}/garden/pages/${path}`, { content, visibility }, KEY);
  if (r.status >= 300) { console.error('seed page failed', path, r); process.exit(1); }
}
const W = `${BASE}/@${user}/garden`;
const SECRETS = ['PRIVSECRET', 'PRIVTITLE', 'private-notes', 'BANDSECRET'];

const browser = await chromium.launch({ headless: true });
const pageErrors = [];
async function newPage(viewport = { width: 1440, height: 900 }) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => pageErrors.push(e.message));
  return page;
}
const shot = async (page, name) => {
  if (!OUT) return;
  await page.waitForTimeout(350); // let the panel finish sliding in
  await page.screenshot({ path: join(OUT, name + '.png') });
};

async function ask(page, question) {
  const before = await page.$$eval('.ra-panel .ra-a, .ra-panel .ra-error', (n) => n.length);
  await page.fill('#ra-input', question);
  await page.press('#ra-input', 'Enter');
  await page.waitForFunction((n) => document.querySelectorAll('.ra-panel .ra-a, .ra-panel .ra-error').length > n, before, { timeout: 15000 });
}

try {
  // 1. signed-out visitor ------------------------------------------------
  const anon = await newPage();
  await anon.goto(`${W}/guide`, { waitUntil: 'domcontentloaded' });
  const launcher = await anon.$('[data-ra-open]');
  check('signed-out visitor sees the Ask launcher', !!launcher);
  await launcher.click();
  await anon.waitForSelector('#reader-assistant.open');
  check('standard reader omits comparison controls', await anon.locator('[data-switches]').count() === 0);
  const scope = await anon.locator('#reader-assistant .ra-header').innerText();
  check('panel states it answers from pages signed-out visitors can read', /signed-out visitors can read/.test(scope), scope);
  await ask(anon, 'How should I water tomatoes, and what is the greenhouse budget?');
  const panelText = await anon.textContent('#reader-assistant');
  check('answer rendered', /Here is what I found/.test(panelText), panelText.slice(0, 300));
  check('no private page or private-band text in the panel', SECRETS.every((s) => !panelText.includes(s)), panelText);
  const sources = await anon.$$eval('.ra-source', (els) => els.map((a) => ({ href: a.getAttribute('href'), path: a.getAttribute('data-ra-source'), text: a.textContent })));
  check('sources list the readable pages only', sources.length >= 2 && sources.every((s) => !s.path.includes('private')), JSON.stringify(sources));
  const guideSrc = sources.find((s) => s.path === 'guide.md');
  check('guide source carries a verified quote + text-fragment link',
    guideSrc && guideSrc.href === `/@${user}/garden/guide#:~:text=Tomatoes%20need%20full%20sun%20and%20deep` && /Tomatoes need full sun/.test(guideSrc.text),
    JSON.stringify(guideSrc));
  const refs = await anon.$$eval('.ra-a .ra-ref', (els) => els.map((a) => a.getAttribute('href')));
  check('inline [n] references link to sources', refs.length >= 2 && refs.every((h) => h.startsWith(`/@${user}/garden/`)), JSON.stringify(refs));
  check('invented source id never shown', !panelText.includes('[S99]') && !panelText.includes('[S'));
  await shot(anon, '1-anon-answer');

  // 2. follow the compost source; conversation persists --------------------
  await anon.click('.ra-source[data-ra-source="compost.md"]');
  await anon.waitForURL((u) => u.pathname === `/@${user}/garden/compost`, { timeout: 10000 });
  const articleText = await anon.textContent('body');
  check('source link navigates to the cited page', /Turn the compost pile/.test(articleText));
  await anon.waitForSelector('#reader-assistant.open', { timeout: 5000 }).catch(() => null);
  const kept = await anon.$$eval('#reader-assistant .ra-q', (n) => n.length);
  check('conversation kept beside the source after navigating', kept === 1, String(kept));
  await shot(anon, '2-anon-followed-source');

  // 3. the boundary itself --------------------------------------------------
  const resp = await anon.goto(`${W}/private-notes`, { waitUntil: 'domcontentloaded' });
  const restrictedText = await anon.textContent('body');
  check('private page is restricted for the signed-out visitor', [401, 403].includes(resp.status()) && !restrictedText.includes('PRIVSECRET'), String(resp.status()));
  await shot(anon, '3-anon-private-restricted');

  // 4. owner -----------------------------------------------------------------
  const ml = await api('POST', '/api/v1/auth/magic-link', { next: `/@${user}/garden/private-notes` }, KEY);
  const owner = await newPage();
  await owner.goto(ml.data.login_url, { waitUntil: 'domcontentloaded' });
  await owner.waitForURL((u) => u.pathname === `/@${user}/garden/private-notes`, { timeout: 10000 });
  await owner.click('[data-ra-open]');
  await owner.waitForSelector('#reader-assistant.open');
  check('owner panel names the signed-in identity', (await owner.locator('#reader-assistant .ra-header').innerText()).includes(`@${user}`));
  await ask(owner, 'What is the greenhouse budget?');
  const ownerSources = await owner.$$eval('.ra-source', (els) => els.map((a) => a.getAttribute('data-ra-source')));
  check('owner gets the private page as a cited source', ownerSources.includes('private-notes.md'), JSON.stringify(ownerSources));
  const ownerText = await owner.textContent('#reader-assistant .ra-messages');
  check('owner answer has no raw source markers', !/\[S\d/.test(ownerText), ownerText);
  await shot(owner, '4-owner-answer');
  await owner.click('.ra-source[data-ra-source="private-notes.md"]');
  await owner.waitForLoadState('domcontentloaded');
  check('owner source link opens the private page', /PRIVSECRET greenhouse budget/.test(await owner.textContent('body')));

  // Post through the signed-in form and read back the exact approved text.
  const comment = '  Inspect the greenhouse after rain.\n';
  await owner.keyboard.press('Escape');
  await owner.click('[data-compose]');
  await owner.fill('[data-body]', comment);
  await owner.click('[data-comment-form] [type=submit]');
  const saved = owner.locator('[data-comments] li').first();
  await saved.waitFor({ timeout: 10000 });
  await owner.reload({ waitUntil: 'domcontentloaded' });
  await saved.waitFor({ timeout: 10000 });
  check('comment retains approved text and author after reload',
    await saved.locator('p').textContent() === comment && await saved.locator('strong').textContent() === '@' + user);

  await owner.goto(W + '/guide');
  await owner.locator('article p').filter({hasText: 'BANDSECRET'}).evaluate(p => {
    const range = document.createRange();
    range.selectNodeContents(p);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
  });
  await owner.click('[data-selection]');
  const approveComment = owner.locator('[data-comment-form] [type=submit]');
  await owner.fill('[data-body]', 'Private reminder');
  await approveComment.waitFor();
  await owner.waitForFunction(() => !document.querySelector('[data-comment-form] [type=submit]').disabled);
  const destination = await owner.textContent('[data-destination]');
  if (!destination.includes('Owner only')) throw new Error('Private passage review showed ' + destination);
  await approveComment.click();
  await owner.locator('[data-comments] li').filter({hasText:'Private reminder'}).waitFor();
  check('private passage audience agrees before and after approval',
    (await owner.textContent('[data-comments]')).includes('Owner only'));

  // An ACL edit can change the audience without changing the page's text.
  const pages = '/api/v1/wikis/' + user + '/garden/pages';
  const setAcl = async rule => {
    const result = await api('PUT', pages + '/.wikihub/acl', {content: '* ' + rule + '\n'}, KEY);
    if (result.status >= 300) throw new Error('Could not change fixture ACL');
  };
  await setAcl('private');
  const inherited = await api('POST', pages, {path:'inherited.md', content:'# Field notes'}, KEY);
  if (inherited.status !== 201) throw new Error('Could not create inherited page');
  await owner.goto(W + '/inherited');
  await owner.click('[data-compose]');
  await owner.fill('[data-body]', 'Reviewed note');
  await owner.locator('[data-destination]').filter({hasText:'private page'}).waitFor();
  await setAcl('public-view');
  await approveComment.click();
  await owner.locator('[data-destination]').filter({hasText:'Public'}).waitFor();
  const thread = await owner.evaluate(async () => {
    const root = document.querySelector('#page-comments');
    const res = await fetch(root.dataset.url, {headers:{'X-CSRFToken':root.querySelector('[data-token]').value}});
    return res.json();
  });
  if (thread.comments.length) throw new Error('Audience change posted without renewed approval');
  await approveComment.click();
  await owner.locator('[data-comments] li').filter({hasText:'Reviewed note'}).waitFor();

  // 5. phone width --------------------------------------------------------------
  const phone = await newPage({ width: 390, height: 844 });
  await phone.addInitScript(() => Object.defineProperty(window, 'sessionStorage', {
    get() { throw new DOMException('Storage denied', 'SecurityError'); },
  }));
  await phone.goto(`${W}/guide`, { waitUntil: 'domcontentloaded' });
  await phone.click('[data-ra-open]');
  await phone.waitForSelector('#reader-assistant.open');
  const box = await phone.$eval('#reader-assistant', (el) => el.getBoundingClientRect().width);
  check('panel is full-width on a phone', Math.round(box) === 390, String(box));
  await ask(phone, 'compost?');
  await shot(phone, '5-phone-answer');
  await phone.keyboard.press('Escape');
  check('Escape closes the panel', !(await phone.$('#reader-assistant.open')));

  check('no page JS errors', pageErrors.length === 0, pageErrors.join(' | '));
} catch (e) {
  check('browser flow completed', false, e.stack || String(e));
} finally {
  await browser.close();
}
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
