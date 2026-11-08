// Run: node tests/test_updates_ui.mjs (uses installed puppeteer-core and Edge).
import fs from 'node:fs';
import assert from 'node:assert/strict';
import puppeteer from 'puppeteer-core';

const root = new URL('../', import.meta.url);
const browser = await puppeteer.launch({
  executablePath: process.env.YHS_EDGE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  headless: true,
  args: ['--allow-file-access-from-files'],
});
try {
  const page = await browser.newPage();
  await page.evaluateOnNewDocument(fs.readFileSync(new URL('mock_bridge.js', import.meta.url), 'utf8'));
  await page.evaluateOnNewDocument(() => {
    const api = window.pywebview.api;
    const original = api.bootstrap;
    const available = {
      ok: true, status: 'available', currentVersion: '0.1.0', latestVersion: '0.2.0',
      release: { version: '0.2.0', url: 'https://github.com/example/example/releases/tag/v0.2.0',
        notes: '<img src=x onerror="window.pwned=1">\n# release', publishedAt: '2026-11-01T00:00:00Z' },
      lastSuccessAt: '2026-11-01T00:00:00Z', ignored: false,
    };
    window.updates = { value: available, events: [], calls: [], mode: 'started' };
    api.bootstrap = async () => ({ ...await original(), updateState: available });
    api.update_status = async () => ({ ok: true, update: window.updates.value });
    api.poll = async () => ({ ok: true, events: window.updates.events.splice(0) });
    const save = api.save_settings;
    api.save_settings = async (patch) => {
      window.updates.calls.push(['save', patch]);
      if (window.updates.failSave) throw Error('save failed');
      return save(patch);
    };
    api.check_updates = async () => {
      if (window.updates.mode === 'throw') throw Error('network failed');
      return { ok: true, started: window.updates.mode !== 'busy' };
    };
    api.ignore_update = async version => {
      window.updates.calls.push(['ignore', version]);
      return { ok: true, settings: { updateIgnoredVersion: version } };
    };
    api.open_update_release = async (...args) => {
      window.updates.calls.push(['open', args]);
      return { ok: true };
    };
  });
  await page.goto(new URL('web/index.html', root).href);
  await page.waitForFunction(() => window.__yhState?.booted && window.__yhState.update);
  assert.equal(await page.$('.update-toast'), null, 'bootstrap must not notify');
  await page.click('#btn-settings');
  await page.click('.settings-nav [data-pane="about"]');
  assert.equal(await page.$eval('#update-notes', n => n.textContent), '<img src=x onerror="window.pwned=1">\n# release');
  assert.equal(await page.$('#update-notes img'), null);
  await page.evaluate(() => window.updates.events.push({ type: 'update_check', manual: false, update: window.updates.value }));
  await page.waitForSelector('.update-toast');
  assert.equal(await page.$eval('.update-toast', n => getComputedStyle(n).pointerEvents), 'auto');
  await page.click('.update-toast .btn.primary');
  assert.equal(await page.$eval('.settings-pane[data-pane="about"]', n => n.classList.contains('active')), true);
  await page.click('#btn-update-download');
  await page.waitForFunction(() => window.updates.calls.some(c => c[0] === 'open'));
  assert.deepEqual(await page.evaluate(() => window.updates.calls.find(c => c[0] === 'open')[1]), [], 'release URL must stay backend-owned');
  await page.click('#btn-update-ignore');
  await page.waitForFunction(() => window.__yhState.update.ignored);
  assert.equal(await page.$('.update-toast'), null);
  assert.equal(await page.$eval('#update-release', n => n.classList.contains('hidden')), false);
  await page.click('#set-update-auto');
  await page.waitForFunction(() => window.__yhState.settings.updateAutoCheck === false);
  assert.deepEqual(await page.evaluate(() => window.updates.calls.find(c => c[0] === 'save')[1]), { updateAutoCheck: false });
  await page.evaluate(() => {
    window.updates.value = { ...window.updates.value, ignored: false };
    window.updates.events.push({ type: 'update_check', manual: false, update: window.updates.value });
  });
  await page.waitForFunction(() => !window.__yhState.update.ignored);
  assert.equal(await page.$('.update-toast'), null, 'opt-out suppresses automatic notification');
  await page.evaluate(() => { window.updates.mode = 'throw'; });
  await page.click('#btn-check-updates');
  await page.waitForFunction(() => window.__yhState.update.status === 'error');
  assert.equal(await page.$eval('#btn-check-updates', n => n.disabled), false);
  await page.evaluate(() => { window.updates.mode = 'busy'; });
  await page.click('#btn-check-updates');
  await page.waitForFunction(() => window.__yhState.update.status === 'busy');
  assert.equal(await page.$eval('#btn-check-updates', n => n.disabled), false);
  await page.evaluate(() => { window.updates.mode = 'started'; });
  await page.click('#btn-check-updates');
  await page.waitForFunction(() => window.__yhState.update.status === 'checking');
  assert.equal(await page.$eval('#btn-check-updates', n => n.disabled), true);
  await page.evaluate(() => window.updates.events.push({ type: 'update_check', manual: true, update: { ...window.updates.value, ignored: true } }));
  await page.waitForFunction(() => window.__yhState.update.status === 'available');
  assert.equal(await page.$eval('#btn-check-updates', n => n.disabled), false);
  assert.equal(await page.$eval('#update-ignored', n => n.classList.contains('hidden')), false);
  await page.evaluate(() => window.updates.events.push({ type: 'update_check', manual: true, update: { ...window.updates.value, status: 'skipped', reason: 'server_cooldown' } }));
  await page.waitForFunction(() => document.querySelector('#update-status').textContent.includes('限流'));
  assert.equal(await page.$eval('#btn-check-updates', n => n.disabled), false);
  await page.evaluate(() => {
    window.updates.failSave = true;
    window.updates.events.push({ type: 'update_check', manual: false, update: { ...window.updates.value, status: 'error', error: 'automatic failure' } });
  });
  await page.waitForFunction(() => window.__yhState.update.status === 'error');
  assert.equal(await page.$('.update-toast'), null, 'automatic error stays silent');
  assert.equal(await page.$eval('#update-error', n => n.textContent), 'automatic failure');
  await page.click('#set-update-auto');
  await page.waitForFunction(() => !document.querySelector('#set-update-auto').disabled);
  assert.equal(await page.$eval('#set-update-auto', n => n.checked), false, 'failed save restores persisted toggle');
  assert.equal(await page.evaluate(() => window.__yhState.settings.updateAutoCheck), false);
  assert.deepEqual(await page.evaluate(() => window.__yhErrors), []);
  console.log('PASS updates UI: cached state, safe notes, toast/view, backend-only open, ignore visibility, immediate opt-out, exception/busy/manual recovery, cooldown');
} finally {
  await browser.close();
}
