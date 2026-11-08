// Run: node tests/test_keyboard_ui.mjs
import fs from 'node:fs';
import assert from 'node:assert/strict';
import puppeteer from 'puppeteer-core';

const browser = await puppeteer.launch({
  executablePath: process.env.YHS_EDGE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  headless: true,
  args: ['--allow-file-access-from-files'],
});
try {
  const page = await browser.newPage();
  await page.evaluateOnNewDocument(fs.readFileSync(new URL('mock_bridge.js', import.meta.url), 'utf8'));
  await page.goto(new URL('../web/index.html', import.meta.url).href);
  await page.waitForFunction(() => window.__yhState?.booted);
  await page.click('#side-nav [data-view="hosts"]');
  await page.click('#host-cards .host-card [data-act="connect"]');
  await page.waitForFunction(() => [...window.__yhState.sessions.values()].some(s => s.status === 'connected'));
  const chord = async key => {
    await page.keyboard.down('Control');
    await page.keyboard.down('Shift');
    await page.keyboard.press(key);
    await page.keyboard.up('Shift');
    await page.keyboard.up('Control');
  };
  const terminalFocus = () => page.evaluate(() => {
    window.__yhState.sessions.get(window.__yhState.activeId).term.focus();
    window.__yhMockWrites.length = 0;
  });
  const inTerminal = () => page.evaluate(() => !!document.activeElement.closest('.terminal-host'));
  const writes = () => page.evaluate(() => window.__yhMockWrites.map(w => w.data));
  const visible = id => page.$eval(`#${id}`, n => !n.classList.contains('hidden'));

  await terminalFocus();
  await chord('KeyW');
  assert.equal(await visible('confirm-modal'), true);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'confirm-cancel');
  await page.keyboard.type('x');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'confirm-ok');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'confirm-cancel');
  await page.keyboard.down('Shift');
  await page.keyboard.press('Tab');
  await page.keyboard.up('Shift');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'confirm-ok');
  // Even an explicit attempt to focus xterm or paste cannot feed a hidden terminal.
  await page.evaluate(() => {
    const term = window.__yhState.sessions.get(window.__yhState.activeId).term;
    term.focus();
    term.paste('blocked');
  });
  assert.equal(await page.evaluate(() => !!document.activeElement.closest('#confirm-modal')), true);
  assert.deepEqual(await writes(), []);
  // Parser-generated DSR replies must survive even while user input is blocked.
  await page.evaluate(() => new Promise(resolve => {
    window.__yhState.sessions.get(window.__yhState.activeId).term.write('\x1b[6n', resolve);
  }));
  assert.match((await writes()).join(''), /^\x1b\[\d+;\d+R$/);
  await page.evaluate(() => { window.__yhMockWrites.length = 0; });
  await page.keyboard.press('Escape');
  assert.equal(await visible('confirm-modal'), false);
  assert.equal(await inTerminal(), true);
  assert.deepEqual(await writes(), []);

  // Opening an earlier DOM sibling above a confirmation must also update visual order.
  await chord('KeyW');
  await page.evaluate(() => document.querySelector('#btn-settings').click());
  assert.equal(await visible('settings-modal'), true);
  assert.equal(await visible('confirm-modal'), true);
  assert.equal(await page.evaluate(() => Number(getComputedStyle(document.querySelector('#settings-modal')).zIndex)
    > Number(getComputedStyle(document.querySelector('#confirm-modal')).zIndex)), true);
  await page.keyboard.press('Escape');
  assert.equal(await visible('settings-modal'), false);
  assert.equal(await visible('confirm-modal'), true);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'confirm-cancel');
  await page.keyboard.press('Escape');
  assert.equal(await inTerminal(), true);

  // Enter on the default cancel action must cancel, never execute the remote line.
  await chord('KeyW');
  await page.keyboard.press('Enter');
  assert.equal(await visible('confirm-modal'), false);
  assert.equal(await page.evaluate(() => window.__yhState.sessions.size), 1);
  assert.deepEqual(await writes(), []);

  await chord('KeyT');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'host-name');
  await page.keyboard.type('retained name');
  for (const key of ['KeyK', 'KeyW', 'Comma', 'KeyT']) await chord(key);
  assert.equal(await visible('snippet-palette'), false);
  assert.equal(await visible('confirm-modal'), false);
  assert.equal(await visible('settings-modal'), false);
  assert.equal(await page.$eval('#host-name', n => n.value), 'retained name');
  await page.keyboard.press('Escape');
  assert.equal(await visible('host-modal'), false);
  assert.equal(await inTerminal(), true);

  // A legitimate nested dialog restores its parent's field; Escape closes one layer.
  await chord('Comma');
  await page.click('.settings-nav [data-pane="models"]');
  // Use existing settings controls to open a provider dialog.
  await page.click('#btn-add-provider');
  assert.equal(await visible('model-modal'), true);
  await page.keyboard.press('Escape');
  assert.equal(await visible('model-modal'), false);
  assert.equal(await visible('settings-modal'), true);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'btn-add-provider');
  await page.keyboard.press('Escape');
  assert.equal(await inTerminal(), true);
  await chord('KeyK');
  assert.equal(await visible('snippet-palette'), true);
  await page.keyboard.press('Escape');
  assert.equal(await visible('snippet-palette'), false);
  assert.equal(await inTerminal(), true);
  await page.keyboard.down('Control');
  await page.keyboard.press('c');
  await page.keyboard.up('Control');
  assert.deepEqual(await writes(), ['\x03']);
  assert.deepEqual(await page.evaluate(() => window.__yhErrors), []);
  console.log('PASS keyboard UI: confirmation isolation, cancel/escape, focus containment/restoration, modal shortcuts, nested dialogs, palette and shell Ctrl+C');
} finally {
  await browser.close();
}
