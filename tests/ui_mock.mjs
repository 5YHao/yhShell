/**
 * Headless UI regression for yhShell.
 *
 * It serves web/ with a mock `window.pywebview` bridge (tests/mock_bridge.js)
 * and drives the real frontend with headless Edge, producing screenshots in
 * .shots/ (override with YHS_SHOT_DIR). Requires `puppeteer-core` and Edge:
 *
 *   cd tests
 *   npm install
 *   node ui_mock.mjs
 *   node ui_mock.mjs --update-docs  # refresh README screenshots after a passing run
 *
 * If puppeteer-core lives outside tests/node_modules, point YHS_PUPPETEER at
 * its ESM entry (…/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js).
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

async function loadPuppeteer() {
  try {
    return (await import('puppeteer-core')).default;
  } catch (error) {
    const fallback = process.env.YHS_PUPPETEER;
    if (!fallback) {
      throw new Error(
        'puppeteer-core 未安装：先在 tests/ 执行 npm install，或设置 YHS_PUPPETEER 指向其入口文件。',
        { cause: error }
      );
    }
    return (await import(pathToFileURL(path.resolve(fallback)).href)).default;
  }
}

const puppeteer = await loadPuppeteer();

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const WEB_ROOT = path.join(ROOT, 'web');
// Override with YHS_SHOT_DIR to keep a run's screenshots out of .shots/.
const SHOT_DIR = process.env.YHS_SHOT_DIR
  ? path.resolve(process.env.YHS_SHOT_DIR)
  : path.join(ROOT, '.shots');
const UPDATE_DOCS = process.argv.includes('--update-docs');
// Keep README assets tied to explicit UI states, not screenshot numbering.
const DOC_SCREENSHOTS = {
  '2-terminal.png': 'terminal.png',
  '3-tool.png': 'agent-command.png',
  '4-answer.png': 'agent-answer.png',
  '7-settings-models.png': 'settings-models.png',
  '8-provider-modal.png': 'provider-modal.png',
  '4b-history-list.png': 'history.png',
  '9-light-theme.png': 'light-theme.png',
  '2e-snippet-palette.png': 'snippets.png',
  '2f-snippet-vars.png': 'snippet-vars.png',
  '2b-transfer-prompt.png': 'transfer-prompt.png',
  '2c-transfer-progress.png': 'transfer-progress.png',
  '2d-transfer-done.png': 'transfer-done.png',
  '1b-hosts-page.png': 'hosts.png',
  '13-snippet-config.png': 'snippet-config.png',
  '14-split-right.png': 'split-right.png',
  '15-split-down.png': 'split-down.png',
  '16-multi-split.png': 'multi-split.png',
};
const MOCK = fs.readFileSync(path.join(HERE, 'mock_bridge.js'), 'utf8');
fs.mkdirSync(SHOT_DIR, { recursive: true });

const EDGE_CANDIDATES = [
  process.env.YHS_EDGE || '',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
].filter(Boolean);
const EDGE = EDGE_CANDIDATES.find((candidate) => fs.existsSync(candidate));
if (!EDGE) {
  console.error('Edge not found. Set YHS_EDGE to msedge.exe');
  process.exit(1);
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
};

const server = http.createServer((req, res) => {
  const urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
  if (urlPath === '/__mock.js') {
    res.writeHead(200, { 'Content-Type': 'text/javascript; charset=utf-8' });
    res.end(MOCK);
    return;
  }
  const rel = urlPath === '/' ? '/index.html' : urlPath;
  const filePath = path.resolve(WEB_ROOT, '.' + rel);
  if (!filePath.startsWith(WEB_ROOT + path.sep)) {
    res.writeHead(403);
    res.end('forbidden');
    return;
  }
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end('not found');
      return;
    }
    if (filePath.endsWith('index.html')) {
      const html = data
        .toString('utf8')
        .replace('<body>', '<body>\n<script src="/__mock.js"></script>');
      data = Buffer.from(html, 'utf8');
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(filePath)] || 'application/octet-stream' });
    res.end(data);
  });
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const shot = async (page, name) => {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOT_DIR, name) });
};
// Toasts are transient feedback; drop them before documentation screenshots.
const clearToasts = (page) =>
  page.evaluate(() => document.querySelectorAll('#toasts .toast').forEach((node) => node.remove()));

// The snippet page lists commands as rows; find one by its visible name.
const snippetRow = (page, name) =>
  page.evaluate((label) => {
    const row = Array.from(document.querySelectorAll('#snippet-table .snippet-row')).find(
      (node) => node.querySelector('.snippet-row-label')?.textContent === label
    );
    if (!row) return null;
    return {
      name: row.querySelector('.snippet-row-label')?.textContent || '',
      command: row.querySelector('.snippet-row-cmd')?.textContent || '',
      note: row.querySelector('.snippet-row-note')?.textContent || '',
      tag: row.querySelector('.tag')?.textContent || '',
      builtin: row.dataset.builtin,
    };
  }, name);

const clickSnippetRow = async (page, name, act) => {
  const clicked = await page.evaluate(
    ({ label, action }) => {
      const row = Array.from(document.querySelectorAll('#snippet-table .snippet-row')).find(
        (node) => node.querySelector('.snippet-row-label')?.textContent === label
      );
      const button = row ? row.querySelector(`[data-act="${action}"]`) : null;
      if (!button) return false;
      button.click();
      return true;
    },
    { label: name, action: act }
  );
  if (!clicked) throw new Error(`snippet row action not found: ${name} / ${act}`);
  await sleep(250);
};

const hasSnippetRow = (page, name) =>
  page.evaluate(
    (label) =>
      Array.from(document.querySelectorAll('#snippet-table .snippet-row-label')).some(
        (node) => node.textContent === label
      ),
    name
  );

async function main() {
  const desiredPort = Number(process.env.YHS_UI_PORT || 0);
  await new Promise((resolve) => server.listen(desiredPort, '127.0.0.1', resolve));
  const port = server.address().port;
  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: true,
    args: ['--no-sandbox', '--disable-gpu', '--force-device-scale-factor=1'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 800, deviceScaleFactor: 1 });

  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().includes('favicon')) errors.push(message.text());
  });

  await page.goto(`http://127.0.0.1:${port}/index.html`, { waitUntil: 'networkidle0' });
  await page.waitForFunction("!document.querySelector('#view-terminal').classList.contains('hidden')", {
    timeout: 10000,
  });
  await sleep(500);
  await shot(page, '1-home.png');

  // Regression: the welcome overlay sits in a positioned stacking context next
  // to #terminal-stack.  When the stack paints above it the transparent
  // container swallows every click, so 新建连接 / 配置 AI 模型 looked alive but
  // did nothing.  Click with real mouse input (hit testing included), and check
  // which element actually receives the click first.
  const welcomeButtons = {};
  for (const [key, buttonSel, modalSel] of [
    ['new', '#btn-welcome-new', '#host-modal'],
    ['settings', '#btn-welcome-settings', '#settings-modal'],
  ]) {
    const center = await page.evaluate((sel) => {
      const rect = document.querySelector(sel).getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    }, buttonSel);
    const topElement = await page.evaluate(({ x, y }) => {
      const node = document.elementFromPoint(x, y);
      if (!node) return null;
      return node.tagName + (node.id ? '#' + node.id : '');
    }, center);
    await page.mouse.click(center.x, center.y);
    await sleep(350);
    const opened = await page.evaluate((sel) => !document.querySelector(sel).classList.contains('hidden'), modalSel);
    welcomeButtons[key] = { topElement, opened };
    if (opened) {
      await page.keyboard.press('Escape');
      await sleep(250);
    }
  }
  const welcomeClosed = await page.evaluate(
    () => document.querySelector('#host-modal').classList.contains('hidden')
      && document.querySelector('#settings-modal').classList.contains('hidden')
  );

  // The left rail is a plain menu: 主机 opens a page with one card per host.
  await page.click('#side-nav .menu-item[data-view="hosts"]');
  await page.waitForFunction("!document.querySelector('#view-hosts').classList.contains('hidden')", {
    timeout: 3000,
  });
  await page.waitForFunction("!!document.querySelector('#host-cards .host-card')", { timeout: 10000 });
  await sleep(300);
  await shot(page, '1b-hosts-page.png');
  const hostPage = await page.evaluate(() => ({
    cards: document.querySelectorAll('#host-cards .host-card').length,
    sub: document.querySelector('#hosts-sub').textContent,
    name: document.querySelector('#host-cards .host-card-name')?.textContent || '',
    connect: !!document.querySelector('#host-cards .host-card [data-act="connect"]'),
    edit: !!document.querySelector('#host-cards .host-card [data-act="edit"]'),
    remove: !!document.querySelector('#host-cards .host-card [data-act="delete"]'),
    terminalHidden: document.querySelector('#view-terminal').classList.contains('hidden'),
    emptyHidden: document.querySelector('#hosts-empty').classList.contains('hidden'),
  }));
  await page.click('#host-cards .host-card [data-act="connect"]');
  await page.waitForFunction("!document.querySelector('#view-terminal').classList.contains('hidden')", {
    timeout: 3000,
  });
  await page.waitForFunction("!!document.querySelector('#tabs .tab-dot.connected')", { timeout: 10000 });
  await sleep(1000);
  await shot(page, '2-terminal.png');

  // 指令集：终端命令面板 + 侧栏列表，点击只会写进终端输入行，绝不回车。
  const lastWrite = () => page.evaluate(() => {
    const writes = window.__yhMockWrites || [];
    return writes.length ? writes[writes.length - 1].data : '';
  });

  const quickBar = await page.evaluate(() => {
    const bar = document.querySelector('#quick-bar');
    const stack = document.querySelector('#terminal-stack').getBoundingClientRect();
    const rect = bar.getBoundingClientRect();
    const status = document.querySelector('#statusbar').getBoundingClientRect();
    return {
      visible: !bar.classList.contains('hidden'),
      groups: document.querySelector('#quick-group').options.length,
      items: document.querySelectorAll('#quick-items .quick-item').length,
      belowTerminal: rect.top >= stack.bottom - 1 && rect.bottom <= status.top + 1,
    };
  });
  await page.click('#quick-items [data-snippet-id="k8s-pods-all"]');
  await sleep(150);
  const quickPlain = await lastWrite();
  await page.select('#quick-group', 'docker');
  const quickDockerItems = await page.$$eval('#quick-items .quick-item', nodes => nodes.length);
  await page.click('#quick-items [data-snippet-id="docker-ps"]');
  await sleep(150);
  const quickDocker = await lastWrite();
  await page.click('#quick-more');
  await page.waitForFunction("!document.querySelector('#snippet-palette').classList.contains('hidden')");
  const quickMoreGroup = await page.evaluate(() => window.__yhState.snippetPalette.groupId);
  await page.click('#snippet-palette-close');
  await page.select('#quick-group', 'k8s-query');
  await page.click('#quick-items [data-snippet-id="k8s-logs"]');
  await page.waitForFunction("!document.querySelector('#snippet-var-form').classList.contains('hidden')");
  await page.click('#snippet-insert-raw');
  await sleep(150);
  const quickRaw = await lastWrite();
  await page.select('#quick-group', 'favorites');
  await shot(page, '2g-terminal-quick-bar.png');

  await page.click('#btn-snippets');
  await page.waitForFunction("!document.querySelector('#snippet-palette').classList.contains('hidden')", { timeout: 5000 });
  await sleep(250);
  await shot(page, '2e-snippet-palette.png');
  const snippetPalette = await page.evaluate(() => ({
    groups: document.querySelectorAll('#snippet-palette-groups .snippet-group-tab').length,
    items: document.querySelectorAll('#snippet-palette-list .snippet-palette-item').length,
    first: document.querySelector('#snippet-palette-list .snippet-palette-item .snippet-palette-item-name')?.textContent || '',
    count: document.querySelector('#snippet-count').textContent,
  }));

  await page.click('#snippet-palette-list .snippet-palette-item[data-snippet-id="k8s-pods-all"]');
  await page.waitForFunction("document.querySelector('#snippet-palette').classList.contains('hidden')", { timeout: 5000 });
  await sleep(150);
  const snippetPlain = await lastWrite();

  await page.click('#btn-snippets');
  await page.waitForFunction("!document.querySelector('#snippet-palette').classList.contains('hidden')", { timeout: 5000 });
  await page.click('#snippet-palette-list .snippet-palette-item[data-snippet-id="k8s-logs"]');
  await page.waitForFunction("!document.querySelector('#snippet-var-form').classList.contains('hidden')", { timeout: 5000 });
  await sleep(200);
  const snippetVarLabels = await page.evaluate(() =>
    Array.from(document.querySelectorAll('#snippet-var-fields .snippet-var-label')).map((node) => node.textContent)
  );
  await page.evaluate(() => {
    const inputs = document.querySelectorAll('#snippet-var-fields input');
    inputs[0].value = 'api-0';
    inputs[0].dispatchEvent(new Event('input', { bubbles: true }));
    inputs[1].value = 'prod';
    inputs[1].dispatchEvent(new Event('input', { bubbles: true }));
  });
  await sleep(150);
  await shot(page, '2f-snippet-vars.png');
  await page.click('#snippet-insert');
  await page.waitForFunction("document.querySelector('#snippet-palette').classList.contains('hidden')", { timeout: 5000 });
  await sleep(150);
  const snippetFilled = await lastWrite();

  await page.click('#btn-snippets');
  await page.waitForFunction("!document.querySelector('#snippet-palette').classList.contains('hidden')", { timeout: 5000 });
  await page.click('#snippet-palette-list .snippet-palette-item[data-snippet-id="k8s-logs"]');
  await page.waitForFunction("!document.querySelector('#snippet-var-form').classList.contains('hidden')", { timeout: 5000 });
  await page.click('#snippet-insert-raw');
  await page.waitForFunction("document.querySelector('#snippet-palette').classList.contains('hidden')", { timeout: 5000 });
  await sleep(150);
  const snippetRaw = await lastWrite();

  // 指令集页面：分组导航 + 搜索 + 自定义指令的增删改。
  await page.click('#side-nav .menu-item[data-view="snippets"]');
  await page.waitForFunction("!document.querySelector('#view-snippets').classList.contains('hidden')", {
    timeout: 3000,
  });
  await page.waitForFunction("!!document.querySelector('#snippet-table .snippet-row')", { timeout: 5000 });
  const snippetPage = await page.evaluate(() => ({
    rows: document.querySelectorAll('#snippet-table .snippet-row').length,
    groups: document.querySelectorAll('#snippet-group-list .snippet-nav-item').length,
    sub: document.querySelector('#snippets-sub').textContent,
    terminalHidden: document.querySelector('#view-terminal').classList.contains('hidden'),
  }));
  await page.type('#snippet-page-search', 'docker');
  await page.waitForFunction("document.querySelectorAll('#snippet-table .snippet-row').length === 1", {
    timeout: 3000,
  });
  const snippetPageSearch = await page.evaluate(() => ({
    rows: document.querySelectorAll('#snippet-table .snippet-row').length,
    name: document.querySelector('#snippet-table .snippet-row .snippet-row-label')?.textContent || '',
    matched: document.querySelector('#snippet-page-matched').textContent,
  }));
  await shot(page, '2h-snippet-page.png');
  await page.click('#snippet-table .snippet-row [data-act="insert"]');
  await sleep(200);
  const snippetPageInsert = await lastWrite();
  await page.click('#side-nav .menu-item[data-view="snippets"]');
  await page.evaluate(() => {
    const search = document.querySelector('#snippet-page-search');
    search.value = '';
    search.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await page.waitForFunction("document.querySelectorAll('#snippet-table .snippet-row').length === 3", {
    timeout: 3000,
  });

  // 新建自定义指令，并同时新建它所在的分组。
  await page.click('#btn-snippet-new');
  await page.waitForFunction("!document.querySelector('#snippet-modal').classList.contains('hidden')", {
    timeout: 5000,
  });
  await page.type('#snippet-name', '自定义巡检');
  await page.type('#snippet-command', 'uptime && df -h');
  await page.select('#snippet-group-select', '__new__');
  await page.waitForFunction("!document.querySelector('#snippet-new-group').classList.contains('hidden')", {
    timeout: 3000,
  });
  await page.type('#snippet-new-group', '我的指令');
  await page.click('#snippet-form button[type="submit"]');
  await page.waitForFunction("document.querySelector('#snippet-modal').classList.contains('hidden')", {
    timeout: 5000,
  });
  await page.waitForFunction(
    "[...document.querySelectorAll('#snippet-group-list .snippet-nav-name')].some((node) => node.textContent === '我的指令')",
    { timeout: 5000 }
  );
  const customSnippet = await snippetRow(page, '自定义巡检');
  const customGroup = await page.evaluate(() => ({
    groups: document.querySelectorAll('#snippet-group-list .snippet-nav-item').length,
    named: Array.from(document.querySelectorAll('#snippet-group-list .snippet-nav-name')).some(
      (node) => node.textContent === '我的指令'
    ),
  }));

  // 编辑自定义指令。
  await clickSnippetRow(page, '自定义巡检', 'edit');
  await page.waitForFunction("!document.querySelector('#snippet-modal').classList.contains('hidden')", {
    timeout: 5000,
  });
  await page.evaluate(() => {
    document.querySelector('#snippet-command').value = '';
  });
  await page.type('#snippet-command', 'uptime && df -h && free -m');
  await page.click('#snippet-form button[type="submit"]');
  await page.waitForFunction("document.querySelector('#snippet-modal').classList.contains('hidden')", {
    timeout: 5000,
  });
  const editedSnippet = await snippetRow(page, '自定义巡检');

  // 编辑内置指令：保存成覆盖版本，并标记为「已修改」。
  await clickSnippetRow(page, '运行中的容器', 'edit');
  await page.waitForFunction("!document.querySelector('#snippet-modal').classList.contains('hidden')", {
    timeout: 5000,
  });
  const builtinEdit = await page.evaluate(() => ({
    builtin: document.querySelector('#snippet-builtin').value,
    groupHidden: document.querySelector('#snippet-group-field').classList.contains('hidden'),
  }));
  await page.evaluate(() => {
    document.querySelector('#snippet-note').value = '';
  });
  await page.type('#snippet-note', '改过的备注');
  await page.click('#snippet-form button[type="submit"]');
  await page.waitForFunction("document.querySelector('#snippet-modal').classList.contains('hidden')", {
    timeout: 5000,
  });
  const builtinEdited = await snippetRow(page, '运行中的容器');

  // 删除内置指令 = 隐藏；「恢复内置」再把它放回来。
  await clickSnippetRow(page, '运行中的容器', 'delete');
  await page.waitForFunction("!document.querySelector('#confirm-modal').classList.contains('hidden')", {
    timeout: 5000,
  });
  await page.click('#confirm-ok');
  await page.waitForFunction(
    "![...document.querySelectorAll('#snippet-table .snippet-row-label')].some((node) => node.textContent === '运行中的容器')",
    { timeout: 5000 }
  );
  await page.click('#btn-snippet-reset');
  await page.waitForFunction("!document.querySelector('#confirm-modal').classList.contains('hidden')", {
    timeout: 5000,
  });
  await page.click('#confirm-ok');
  await page.waitForFunction(
    "[...document.querySelectorAll('#snippet-table .snippet-row-label')].some((node) => node.textContent === '运行中的容器')",
    { timeout: 5000 }
  );
  const afterReset = await snippetRow(page, '运行中的容器');

  // 删除自定义指令。
  await clickSnippetRow(page, '自定义巡检', 'delete');
  await page.waitForFunction("!document.querySelector('#confirm-modal').classList.contains('hidden')", {
    timeout: 5000,
  });
  await page.click('#confirm-ok');
  await page.waitForFunction(
    "![...document.querySelectorAll('#snippet-table .snippet-row-label')].some((node) => node.textContent === '自定义巡检')",
    { timeout: 5000 }
  );
  const afterDelete = await hasSnippetRow(page, '自定义巡检');

  // 回到终端页，后面的传输 / AI 测试都在终端里跑。
  await page.click('#side-nav .menu-item[data-view="terminal"]');
  await page.waitForFunction("!document.querySelector('#view-terminal').classList.contains('hidden')", {
    timeout: 3000,
  });

  await page.keyboard.down('Control');
  await page.keyboard.down('Shift');
  await page.keyboard.press('KeyK');
  await page.keyboard.up('Shift');
  await page.keyboard.up('Control');
  await page.waitForFunction("!document.querySelector('#snippet-palette').classList.contains('hidden')", { timeout: 5000 });
  const snippetShortcut = await page.evaluate(
    () => !document.querySelector('#snippet-palette').classList.contains('hidden')
  );
  await sleep(200);
  await page.keyboard.press('Escape');
  await page.waitForFunction("document.querySelector('#snippet-palette').classList.contains('hidden')", { timeout: 5000 });
  await clearToasts(page);

  // ZMODEM 传输面板：下载确认 → 进度 → 完成 → 关闭，再走一遍上传。
  await page.evaluate(() => window.__yhMockTransfer.promptDownload());
  await page.waitForFunction("!document.querySelector('#transfer-card').classList.contains('hidden')", { timeout: 5000 });
  await sleep(300);
  await shot(page, '2b-transfer-prompt.png');
  const transferPrompt = await page.evaluate(() => ({
    title: document.querySelector('#transfer-title').textContent,
    accept: document.querySelector('#transfer-accept').textContent,
    path: document.querySelector('#transfer-path').textContent,
  }));
  await page.click('#transfer-accept');
  await page.waitForFunction("document.querySelector('#transfer-card').dataset.phase === 'transferring'", { timeout: 5000 });
  await sleep(420);
  await shot(page, '2c-transfer-progress.png');
  await page.waitForFunction("document.querySelector('#transfer-card').dataset.phase === 'done'", { timeout: 15000 });
  await sleep(300);
  await shot(page, '2d-transfer-done.png');
  const transferDone = await page.evaluate(() => ({
    title: document.querySelector('#transfer-title').textContent,
    size: document.querySelector('#transfer-size').textContent,
    accept: document.querySelector('#transfer-accept').textContent,
    browse: document.querySelector('#transfer-browse').textContent,
  }));
  await page.click('#transfer-accept');
  await page.waitForFunction("document.querySelector('#transfer-card').classList.contains('hidden')", { timeout: 5000 });
  const transferClosed = await page.evaluate(() => document.querySelector('#transfer-card').classList.contains('hidden'));

  await page.evaluate(() => window.__yhMockTransfer.promptUpload());
  await page.waitForFunction("document.querySelector('#transfer-card').dataset.phase === 'prompt'", { timeout: 5000 });
  const uploadPrompt = await page.evaluate(() => ({
    title: document.querySelector('#transfer-title').textContent,
    accept: document.querySelector('#transfer-accept').textContent,
  }));
  await page.click('#transfer-accept');
  await page.waitForFunction("document.querySelector('#transfer-card').dataset.phase === 'done'", { timeout: 15000 });
  const uploadDone = await page.evaluate(() => document.querySelector('#transfer-title').textContent);
  await page.click('#transfer-accept');
  await sleep(250);
  const transferClosedAgain = await page.evaluate(() => document.querySelector('#transfer-card').classList.contains('hidden'));

  await page.click('#ai-input');
  await page.type('#ai-input', '\u5e2e\u6211\u68c0\u67e5\u78c1\u76d8\u548c\u5185\u5b58\u7684\u4f7f\u7528\u60c5\u51b5');
  await page.click('#btn-ai-send');
  await page.waitForFunction("!!document.querySelector('.tool-card')", { timeout: 10000 });
  await sleep(600);
  const safetyNote = await page.evaluate(() => {
    const note = document.querySelector('.tool-card .tool-safety-note');
    return {
      visible: !!note && !note.classList.contains('hidden'),
      text: note?.textContent || '',
    };
  });
  await shot(page, '3-tool.png');

  await page.click('.tool-card .tool-actions .btn.primary');
  await page.waitForFunction("!!document.querySelector('.tool-card .chip.done')", { timeout: 10000 });
  await page.waitForFunction("document.querySelector('#chat').innerText.includes('42%')", { timeout: 10000 });
  await page.waitForFunction("!!document.querySelector('#chat .md-table-wrap table')", { timeout: 10000 });
  const safetyNoteAfterRun = await page.evaluate(() => {
    const note = document.querySelector('.tool-card .tool-safety-note');
    return !!note && !note.classList.contains('hidden');
  });
  await sleep(500);
  await shot(page, '4-answer.png');
  const hasTable = await page.evaluate(() => !!document.querySelector('#chat .md-table-wrap table'));
  const tableChecks = await page.evaluate(() => {
    const render = window.__yhRenderMarkdown;
    return {
      piped: render('| a | b |\n| --- | --- |\n| 1 | 2 |').includes('<table>'),
      plain: render('a | b\n--- | ---\n1 | 2').includes('<table>'),
      align: render('| a | b |\n| :--- | ---: |\n| 1 | 2 |').includes('text-align:right'),
      inline: render('| `x` | **y** |\n| --- | --- |\n| 1 | 2 |').includes('<code>x</code>'),
    };
  });

  // Automatic tools do not emit busy:false between model responses. Exercise
  // multiple steps in one poll, including a repeated tool event after text
  // resumes: updating an existing card must not split that text again.
  await page.waitForFunction("!window.__yhState.chat.busy", { timeout: 10000 });
  await page.evaluate(() => window.__yhMockAgentEvents([
    { type: 'busy', busy: true },
    { type: 'delta', text: '时序测试：先检查。' },
    { type: 'tool', call: { callId: 'order-1', command: 'pwd' } },
    { type: 'tool_update', callId: 'order-1', status: 'running' },
    { type: 'tool_update', callId: 'order-1', status: 'done', output: '/tmp', exitCode: 0 },
    { type: 'delta', text: '已得到目录，再检查系统。' },
    { type: 'tool', call: { callId: 'order-1', command: 'pwd' } },
    { type: 'delta', text: '继续检查。' },
    { type: 'tool', call: { callId: 'order-2', command: 'uname -s' } },
    { type: 'tool_update', callId: 'order-2', status: 'done', output: 'Linux', exitCode: 0 },
    { type: 'delta', text: '时序测试结论：目录和系统已确认。' },
    { type: 'busy', busy: false },
  ]));
  await page.waitForFunction("document.querySelector('#chat').innerText.includes('时序测试结论')", { timeout: 10000 });
  const commandOrder = await page.evaluate(() => {
    const nodes = Array.from(document.querySelector('#chat').children);
    const start = nodes.findIndex((node) => node.textContent === '✦时序测试：先检查。');
    return nodes.slice(start).map((node) => node.matches('.tool-card')
      ? { callId: node.dataset.callId, output: node.querySelector('.tool-output pre').textContent }
      : { text: node.querySelector('.assistant-body')?.textContent });
  });
  const expectedOrder = [
    { text: '时序测试：先检查。' },
    { callId: 'order-1', output: '/tmp' },
    { text: '已得到目录，再检查系统。继续检查。' },
    { callId: 'order-2', output: 'Linux' },
    { text: '时序测试结论：目录和系统已确认。' },
  ];
  if (JSON.stringify(commandOrder) !== JSON.stringify(expectedOrder)) {
    throw new Error(`Agent command/result/conclusion order incorrect: ${JSON.stringify(commandOrder)}`);
  }

  await page.click('#btn-ai-history');
  await page.waitForFunction("document.querySelectorAll('#history-list .history-row').length >= 2", { timeout: 10000 });
  await sleep(250);
  await shot(page, '4b-history-list.png');
  await page.evaluate(() => document.querySelector('#history-list .history-row .history-row-actions .btn').click());
  await page.waitForFunction("!!document.querySelector('#chat-banner:not(.hidden)')", { timeout: 10000 });
  await sleep(350);
  await shot(page, '4c-history-view.png');
  const viewing = await page.evaluate(() => Boolean(window.__yhState?.chat?.viewing));
  await page.click('#btn-banner-continue');
  await page.waitForFunction("document.querySelector('#chat-banner').classList.contains('hidden')", { timeout: 10000 });
  await sleep(500);
  await shot(page, '4d-history-continued.png');
  const continued = await page.evaluate(() => !window.__yhState?.chat?.viewing);

  // The composer model dropdown lists every configured provider and its models.
  const modelSelect = await page.evaluate(() => ({
    groups: Array.from(document.querySelectorAll('#ai-model-select optgroup')).map((group) => group.label),
    options: Array.from(document.querySelectorAll('#ai-model-select option')).map((option) => option.value),
    active: document.querySelector('#ai-model-select').value,
  }));
  await page.select('#ai-model-select', 'p2::qwen2.5:7b');
  await page.waitForFunction("window.__yhState.activeModelId === 'p2::qwen2.5:7b'", { timeout: 5000 });
  await sleep(250);
  await shot(page, '5-model-select.png');
  await page.select('#ai-model-select', 'p1::deepseek-chat');
  await page.waitForFunction("window.__yhState.activeModelId === 'p1::deepseek-chat'", { timeout: 5000 });
  await clearToasts(page);

  await page.click('#btn-settings');
  await sleep(300);
  await shot(page, '6-settings-general.png');
  await page.click('.settings-nav button[data-pane="models"]');
  await sleep(250);
  await shot(page, '7-settings-models.png');
  await page.click('#btn-sync-models');
  await page.waitForFunction(
    "Array.from(document.querySelectorAll('.toast')).some((node) => node.textContent.includes('已同步'))",
    { timeout: 8000 }
  );
  await clearToasts(page);
  await sleep(200);

  await page.click('#btn-add-provider');
  await sleep(250);
  await page.select('#model-preset', 'deepseek');
  await page.evaluate(() => {
    document.querySelector('#model-name').value = '\u6df1\u5ea6\u6c42\u7d22\u5907\u7528';
  });
  await page.click('#btn-fetch-models');
  await page.waitForFunction("document.querySelectorAll('#model-picker .model-option').length > 0", { timeout: 10000 });
  await sleep(300);
  const pickerState = await page.evaluate(() => ({
    options: document.querySelectorAll('#model-picker .model-option').length,
    count: document.querySelector('#model-selected-count').textContent,
    hint: document.querySelector('#model-pick-hint').textContent,
  }));
  await clearToasts(page);
  await shot(page, '8-provider-modal.png');
  await page.click('#model-form button[type="submit"]');
  await page.waitForFunction("document.querySelector('#model-modal').classList.contains('hidden')", { timeout: 8000 });
  await sleep(250);
  await shot(page, '8b-providers-saved.png');
  const providersAfterSave = await page.evaluate(() => window.__yhState.providers.length);
  const savedModels = await page.evaluate(() => {
    const provider = window.__yhState.providers[window.__yhState.providers.length - 1];
    return provider ? provider.models : [];
  });

  // Fallback path: an id the upstream never advertised can still be added by hand.
  await page.evaluate(() => {
    const rows = document.querySelectorAll('#provider-list .provider-row');
    rows[rows.length - 1].querySelectorAll('.model-actions .btn')[1].click();
  });
  await page.waitForFunction("!document.querySelector('#model-modal').classList.contains('hidden')", { timeout: 5000 });
  // Let the modal's delayed initial focus settle before typing in the filter.
  await sleep(100);
  await page.type('#model-filter', 'custom-model-x');
  await page.waitForFunction("!!document.querySelector('#model-picker .model-option-add')", { timeout: 5000 });
  await page.click('#model-picker .model-option-add');
  await page.evaluate(() => {
    const filter = document.querySelector('#model-filter');
    filter.value = '';
    filter.dispatchEvent(new Event('input'));
  });
  const manualAddCount = await page.evaluate(
    () => document.querySelectorAll('#model-picker .model-option').length
  );
  await page.click('#model-modal [data-close="model-modal"]');
  await page.waitForFunction("document.querySelector('#model-modal').classList.contains('hidden')", { timeout: 5000 });

  await page.click('.settings-nav button[data-pane="general"]');
  await page.select('#set-theme', 'light');
  await page.click('#btn-save-settings');
  await sleep(300);
  await page.click('#settings-modal [data-close="settings-modal"]');
  await sleep(300);
  await shot(page, '9-light-theme.png');
  await page.click('#btn-settings');
  await sleep(200);
  await page.select('#set-theme', 'dark');
  await page.click('#btn-save-settings');
  await sleep(200);
  await page.click('#settings-modal [data-close="settings-modal"]');

  await page.click('#side-nav .menu-item[data-view="hosts"]');
  await page.waitForFunction("!document.querySelector('#view-hosts').classList.contains('hidden')", {
    timeout: 3000,
  });
  await page.click('#host-cards .host-card:nth-child(2) [data-act="connect"]');
  await page.waitForFunction("document.querySelectorAll('#tabs .tab').length === 2", { timeout: 8000 });
  await page.waitForFunction("document.querySelectorAll('#tabs .tab-dot.connected').length === 2", { timeout: 8000 });
  await sleep(600);
  await shot(page, '10-two-sessions.png');

  await page.evaluate(() => document.querySelectorAll('#tabs .tab')[0].click());
  await sleep(400);
  await shot(page, '11-switch-back.png');

  const before = await page.evaluate(() => document.querySelectorAll('#tabs .tab').length);
  await page.evaluate(() => document.querySelectorAll('#tabs .tab')[1].querySelector('.tab-close').click());
  await sleep(250);
  await shot(page, '12-close-confirm.png');
  await page.click('#confirm-ok');
  await sleep(400);
  const after = await page.evaluate(() => document.querySelectorAll('#tabs .tab').length);

  // Tab context actions must target the right-clicked tab, not just activeId.
  const tabChecks = {};
  const tabAction = async (sid, action) => {
    const gid = await page.evaluate((id) => window.__yhState.sessions.get(id).groupId, sid);
    await page.click(`#tabs .tab[data-gid="${gid}"]`);
    await page.evaluate((id) => document.querySelector(`.terminal-session[data-sid="${id}"]`)
      .dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })), sid);
    await page.click(`.terminal-session[data-sid="${sid}"] .pane-menu`);
    await page.click(`#tab-menu [data-action="${action}"]`);
    await sleep(180);
  };
  const sourceId = await page.evaluate(() => window.__yhState.activeId);
  await tabAction(sourceId, 'duplicate');
  tabChecks.duplicate = await page.evaluate((sourceId) => {
    const s = window.__yhState;
    const source = s.sessions.get(sourceId), copy = s.sessions.get(s.activeId);
    return s.sessions.size === 2 && source.id !== copy.id && source.info.host === copy.info.host;
  }, sourceId);
  const tabsBeforeSplit = await page.evaluate(() => document.querySelectorAll('#tabs .tab').length);
  await tabAction(sourceId, 'split-right');
  await sleep(150);
  tabChecks.splitNoNewTab = await page.evaluate((count) => document.querySelectorAll('#tabs .tab').length === count, tabsBeforeSplit);
  tabChecks.right = await page.evaluate(() => {
    const panes = Array.from(document.querySelectorAll('.terminal-session.split-member'));
    const [a, b] = panes.map((node) => node.getBoundingClientRect());
    return panes.length === 2 && a.width > 100 && b.width > 100 && b.left >= a.right;
  });
  const dragDivider = async (selector, fraction, down = false) => {
    const geometry = await page.evaluate((sel) => {
      const bar = document.querySelector(sel).getBoundingClientRect();
      const stack = document.querySelector('#terminal-stack').getBoundingClientRect();
      return { x: bar.x + bar.width / 2, y: bar.y + bar.height / 2,
        left: stack.left, top: stack.top, width: stack.width, height: stack.height };
    }, selector);
    await page.mouse.move(geometry.x, geometry.y);
    await page.mouse.down();
    await page.mouse.move(down ? geometry.x : geometry.left + geometry.width * fraction,
      down ? geometry.top + geometry.height * fraction : geometry.y, { steps: 8 });
    await page.mouse.up();
    await sleep(120);
  };
  await dragDivider('.split-divider.vertical', 0.65);
  tabChecks.dragWidth = await page.evaluate(() => Math.abs(window.__yhState.splitLayout.ratio - 0.65) < 0.03);
  const splitCopy = await page.evaluate(() => window.__yhState.activeId);
  await shot(page, '14-split-right.png');
  await page.click(`.terminal-session[data-sid="${sourceId}"] .xterm-screen`);
  tabChecks.focus = await page.evaluate((sid) => window.__yhState.activeId === sid, sourceId);
  await sleep(120);
  await page.evaluate(() => {
    window.__splitPerf = { fits: 0, resizes: 0, mutations: 0 };
    for (const session of window.__yhState.sessions.values()) {
      const fit = session.fit.fit.bind(session.fit);
      session.fit.fit = (...args) => { window.__splitPerf.fits++; return fit(...args); };
    }
    const resize = window.pywebview.api.ssh_resize;
    window.pywebview.api.ssh_resize = (...args) => { window.__splitPerf.resizes++; return resize(...args); };
    window.__splitObserver = new MutationObserver((items) => {
      window.__splitPerf.mutations += items.filter((item) => item.attributeName === 'style').length;
    });
    window.__splitObserver.observe(document.querySelector('#terminal-stack'), { subtree: true, attributes: true });
  });
  for (let i = 0; i < 12; i++) {
    await page.click(`.terminal-session[data-sid="${i % 2 ? sourceId : splitCopy}"] .xterm-screen`);
  }
  await sleep(120);
  tabChecks.focusNoLayoutThrash = await page.evaluate(() => {
    window.__splitObserver.disconnect();
    return window.__splitPerf.fits === 0 && window.__splitPerf.resizes === 0 && window.__splitPerf.mutations === 0;
  });
  tabChecks.stableFocus = await page.evaluate((sid) => window.__yhState.activeId === sid
    && window.__yhState.sessions.get(sid).hostEl.contains(document.activeElement), sourceId);
  await page.evaluate(() => {
    window.__idleTabMutations = 0;
    window.__idleTabObserver = new MutationObserver((items) => { window.__idleTabMutations += items.length; });
    window.__idleTabObserver.observe(document.querySelector('#tabs'), { subtree: true, attributes: true, childList: true, characterData: true });
  });
  await sleep(180);
  tabChecks.idlePollingNoTabRepaint = await page.evaluate(() => {
    window.__idleTabObserver.disconnect();
    return window.__idleTabMutations === 0;
  });
  await tabAction(sourceId, 'unsplit');
  tabChecks.unsplit = await page.evaluate(() => !document.querySelector('.split-member'));
  await tabAction(sourceId, 'unsplit');
  await tabAction(splitCopy, 'close');
  await page.click('#confirm-ok');
  await sleep(180);
  await tabAction(sourceId, 'split-down');

  tabChecks.down = await page.evaluate(() => {
    const [a, b] = Array.from(document.querySelectorAll('.split-member')).map((node) => node.getBoundingClientRect());
    return a.height > 80 && b.height > 80 && b.top >= a.bottom;
  });
  await dragDivider('.split-divider.horizontal', 0.6, true);
  tabChecks.dragHeight = await page.evaluate(() => Math.abs(window.__yhState.splitLayout.ratio - 0.6) < 0.03);
  await shot(page, '15-split-down.png');
  const downCopy = await page.evaluate(() => window.__yhState.activeId);
  await tabAction(downCopy, 'close');
  await page.click('#confirm-ok');
  await sleep(180);
  tabChecks.close = await page.evaluate(() => !document.querySelector('.split-member'));
  await page.click(`#tabs .tab[data-sid="${sourceId}"]`, { button: 'right' });
  await page.keyboard.press('Escape');
  tabChecks.escape = await page.evaluate(() => document.querySelector('#tab-menu').classList.contains('hidden'));
  // Repeated mixed-direction splits preserve all existing panes, with no two-pane cap.
  await tabAction(sourceId, 'split-right');
  for (let count = 3; count <= 8; count += 1) {
    const target = await page.evaluate(() => Array.from(document.querySelectorAll('.split-member'))
      .sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0].dataset.sid);
    await tabAction(target, count % 2 ? 'split-down' : 'split-right');
  }
  tabChecks.eightPanes = await page.evaluate(() => {
    const panes = Array.from(document.querySelectorAll('.split-member'));
    const rects = panes.map((node) => node.getBoundingClientRect());
    return panes.length === 8 && rects.every((r) => r.width > 30 && r.height > 30)
      && rects.every((a, i) => rects.every((b, j) => i === j
        || a.right <= b.left + 1 || b.right <= a.left + 1 || a.bottom <= b.top + 1 || b.bottom <= a.top + 1));
  });
  const closeId = await page.evaluate(() => window.__yhState.activeId);
  await tabAction(closeId, 'close');
  await page.click('#confirm-ok');
  await sleep(180);
  tabChecks.closeKeepsOthers = await page.evaluate(() => document.querySelectorAll('.split-member').length === 7);
  const inactiveCloseId = await page.evaluate(() => Array.from(document.querySelectorAll('.split-member'))
    .find((node) => node.dataset.sid !== window.__yhState.activeId).dataset.sid);
  await tabAction(inactiveCloseId, 'close');
  await page.click('#confirm-ok');
  await sleep(180);
  tabChecks.inactiveCloseKeepsOthers = await page.evaluate(() => document.querySelectorAll('.split-member').length === 6);
  await page.setViewport({ width: 1100, height: 800, deviceScaleFactor: 1 });
  await sleep(180);
  tabChecks.resizeKeepsOthers = await page.evaluate(() => document.querySelectorAll('.split-member').length === 6);
  const nestedDivider = await page.evaluate(() => {
    const bars = Array.from(document.querySelectorAll('.split-divider'));
    const bar = bars.find((node) => node.dataset.index !== '0');
    return { selector: `.split-divider[data-index="${bar.dataset.index}"]`, down: bar.classList.contains('horizontal') };
  });
  const nestedBefore = await page.$eval(nestedDivider.selector, (bar) => Number(bar.getAttribute('aria-valuenow')));
  await page.focus(nestedDivider.selector);
  await page.keyboard.press(nestedDivider.down ? 'ArrowDown' : 'ArrowRight');
  tabChecks.nestedResize = await page.$eval(nestedDivider.selector,
    (bar, before) => Number(bar.getAttribute('aria-valuenow')) > before, nestedBefore);
  const resetPoint = await page.$eval(nestedDivider.selector, (bar) => {
    const rect = bar.getBoundingClientRect();
    return bar.classList.contains('horizontal')
      ? { x: rect.left + rect.width * 0.25, y: rect.top + rect.height / 2 }
      : { x: rect.left + rect.width / 2, y: rect.top + rect.height * 0.25 };
  });
  await page.mouse.click(resetPoint.x, resetPoint.y);
  await page.mouse.click(resetPoint.x, resetPoint.y);
  tabChecks.resetDivider = await page.$eval(nestedDivider.selector, (bar) => bar.getAttribute('aria-valuenow') === '50');
  await shot(page, '16-multi-split.png');
  const remainingId = await page.evaluate(() => window.__yhState.activeId);
  const ratioBeforeSwitch = await page.evaluate(() => {
    const bar = document.querySelector('.split-divider');
    return bar.getAttribute('aria-valuenow');
  });
  const currentGroupId = await page.evaluate(() => window.__yhState.activeTabId);
  const otherGroupId = await page.evaluate(() => [...window.__yhState.terminalTabs.keys()]
    .find((id) => id !== window.__yhState.activeTabId));
  await page.click(`#tabs .tab[data-gid="${otherGroupId}"]`);
  tabChecks.otherTabSingle = await page.evaluate(() => !document.querySelector('.split-member'));
  await page.click(`#tabs .tab[data-gid="${currentGroupId}"]`);
  tabChecks.tabLayoutRestored = await page.evaluate(() => document.querySelectorAll('.split-member').length === 6);
  tabChecks.ratioPreserved = await page.$eval('.split-divider', (bar, ratio) => bar.getAttribute('aria-valuenow') === ratio, ratioBeforeSwitch);
  tabChecks.multiNoNewTab = await page.evaluate((count) => document.querySelectorAll('#tabs .tab').length === count, tabsBeforeSplit);
  await tabAction(remainingId, 'unsplit');
  tabChecks.multiUnsplit = await page.evaluate(() => !document.querySelector('.split-member'));
  await tabAction(remainingId, 'unsplit');
  tabChecks.restoreSplit = await page.evaluate(() => document.querySelectorAll('.split-member').length === 6);
  await page.click(`#tabs .tab[data-gid="${currentGroupId}"] .tab-close`);
  await page.click('#confirm-ok');
  await sleep(350);
  tabChecks.closeWorkspace = await page.evaluate((gid) => !window.__yhState.terminalTabs.has(gid)
    && [...window.__yhState.sessions.values()].every((session) => session.groupId !== gid)
    && window.__yhState.sessions.has(window.__yhState.activeId), currentGroupId);

  const pageLayouts = [];
  for (const width of [1400, 1100, 900]) {
    await page.setViewport({ width, height: 800, deviceScaleFactor: 1 });
    for (const view of ['hosts', 'snippets']) {
      await page.click(`#side-nav .menu-item[data-view="${view}"]`);
      await sleep(120);
      const layout = await page.evaluate((view) => {
        const root = document.querySelector(`#view-${view}`);
        const sidebar = document.querySelector('#sidebar');
        return {
          pageOverflow: root.scrollWidth > root.clientWidth + 1,
          clippedActions: Array.from(root.querySelectorAll('[data-act]')).some((node) => {
            const rect = node.getBoundingClientRect();
            const parent = root.getBoundingClientRect();
            return rect.right > parent.right + 1 || rect.left < parent.left - 1;
          }),
          documentOverflow: document.documentElement.scrollWidth > innerWidth,
        };
      }, view);
      pageLayouts.push({ width, view, ...layout });
    }
  }
  await page.setViewport({ width: 1400, height: 800, deviceScaleFactor: 1 });
  await clearToasts(page);
  await shot(page, '13-snippet-config.png');
  await page.click('#side-nav .menu-item[data-view="terminal"]');
  const runtimeErrors = await page.evaluate(() => window.__yhErrors || []);
  const sideMenu = await page.evaluate(() => ({
    items: Array.from(document.querySelectorAll('#side-nav .menu-item')).map((node) => ({
      view: node.dataset.view,
      label: node.querySelector('.menu-item-label').textContent,
      count: node.querySelector('.menu-item-count').textContent,
      active: node.classList.contains('active'),
    })),
    views: document.querySelectorAll('#main .view').length,
    visibleViews: Array.from(document.querySelectorAll('#main .view')).filter(
      (node) => !node.classList.contains('hidden')
    ).length,
    activeView: document.querySelector('#side-nav .menu-item.active')?.dataset.view || '',
  }));
  const summary = {
    tabChecks,
    pageLayouts,
    errors,
    runtimeErrors,
    sideMenu,
    welcomeButtons,
    welcomeClosed,
    hasTable,
    tableChecks,
    safetyNote,
    safetyNoteAfterRun,
    viewing,
    continued,
    quickBar,
    quickPlain,
    quickDockerItems,
    quickDocker,
    quickMoreGroup,
    quickRaw,
    snippetPalette,
    snippetVarLabels,
    snippetPlain,
    snippetFilled,
    snippetRaw,
    hostPage,
    snippetPage,
    snippetPageSearch,
    snippetPageInsert,
    customSnippet,
    customGroup,
    editedSnippet,
    builtinEdit,
    builtinEdited,
    afterReset,
    afterDelete,
    snippetShortcut,
    pickerState,
    modelSelect,
    manualAddCount,
    providersAfterSave,
    savedModels,
    transferPrompt,
    transferDone,
    transferClosed,
    uploadPrompt,
    uploadDone,
    transferClosedAgain,
    sessionsBeforeClose: before,
    sessionsAfterClose: after,
  };
  console.log(JSON.stringify(summary, null, 2));

  await browser.close();
  server.close();
  if (
    errors.length ||
    !Object.values(tabChecks).every(Boolean) ||
    runtimeErrors.length ||
    pageLayouts.some((layout) => layout.pageOverflow || layout.clippedActions || layout.documentOverflow) ||
    !welcomeButtons.new?.opened ||
    !welcomeButtons.settings?.opened ||
    welcomeButtons.new.topElement !== 'BUTTON#btn-welcome-new' ||
    welcomeButtons.settings.topElement !== 'BUTTON#btn-welcome-settings' ||
    !welcomeClosed ||
    before !== 2 ||
    after !== 1 ||
    !hasTable ||
    !Object.values(tableChecks).every(Boolean) ||
    !safetyNote.visible ||
    !safetyNote.text.includes('未自动执行') ||
    safetyNoteAfterRun ||
    !viewing ||
    !continued ||
    !quickBar.visible ||
    !quickBar.belowTerminal ||
    quickBar.groups !== 3 ||
    quickBar.items !== 3 ||
    quickPlain !== 'kubectl get pods -A -o wide' ||
    quickDockerItems !== 1 ||
    quickDocker !== 'docker ps' ||
    quickMoreGroup !== 'docker' ||
    quickRaw !== 'kubectl logs -f <Pod名> -n <命名空间>' ||
    [quickPlain, quickDocker, quickRaw].some(command => /[\r\n]/.test(command)) ||
    snippetPalette.groups !== 3 ||
    snippetPalette.items !== 3 ||
    snippetPalette.count !== '3' ||
    snippetPalette.first !== '查看所有 Pod' ||
    snippetVarLabels.join('|') !== '<Pod名>|<命名空间>' ||
    snippetPlain !== 'kubectl get pods -A -o wide' ||
    /[\r\n]/.test(snippetPlain) ||
    snippetFilled !== 'kubectl logs -f api-0 -n prod' ||
    snippetRaw !== 'kubectl logs -f <Pod名> -n <命名空间>' ||
    hostPage.cards !== 3 ||
    hostPage.connect !== true ||
    hostPage.edit !== true ||
    hostPage.remove !== true ||
    hostPage.terminalHidden !== true ||
    hostPage.emptyHidden !== true ||
    snippetPage.rows !== 3 ||
    snippetPage.groups !== 3 ||
    snippetPage.terminalHidden !== true ||
    snippetPageSearch.rows !== 1 ||
    snippetPageSearch.name !== '运行中的容器' ||
    !snippetPageSearch.matched.includes('匹配') ||
    snippetPageInsert !== 'docker ps' ||
    !customSnippet ||
    customSnippet.command !== 'uptime && df -h' ||
    customSnippet.tag !== '自定义' ||
    customSnippet.builtin !== '0' ||
    !customGroup.named ||
    !editedSnippet ||
    editedSnippet.command !== 'uptime && df -h && free -m' ||
    builtinEdit.builtin !== '1' ||
    builtinEdit.groupHidden !== true ||
    !builtinEdited ||
    builtinEdited.tag !== '已修改' ||
    builtinEdited.note !== '改过的备注' ||
    !afterReset ||
    afterReset.tag !== '内置' ||
    afterReset.note !== '含端口映射与启动时间' ||
    afterDelete !== false ||
    !snippetShortcut ||
    pickerState.options !== 3 ||
    !pickerState.count.includes('3') ||
    modelSelect.groups.join(',') !== 'DeepSeek,本地 Ollama' ||
    modelSelect.options.length !== 3 ||
    modelSelect.active !== 'p1::deepseek-chat' ||
    manualAddCount !== 4 ||
    providersAfterSave !== 3 ||
    savedModels.length !== 3 ||
    savedModels[0] !== 'deepseek-chat' ||
    !transferPrompt.title.includes('接收文件') ||
    transferPrompt.accept !== '开始接收' ||
    !transferDone.title.includes('接收完成') ||
    transferDone.accept !== '关闭' ||
    transferDone.browse !== '打开文件夹' ||
    !transferClosed ||
    uploadPrompt.accept !== '选择文件…' ||
    !uploadPrompt.title.includes('发送文件') ||
    !uploadDone.includes('发送完成') ||
    !transferClosedAgain ||
    sideMenu.items.length !== 3 ||
    sideMenu.items.map((item) => item.view).join(',') !== 'terminal,hosts,snippets' ||
    sideMenu.views !== 3 ||
    sideMenu.visibleViews !== 1 ||
    sideMenu.activeView !== 'terminal' ||
    sideMenu.items.filter((item) => item.active).length !== 1
  ) {
    process.exitCode = 1;
  }
  if (UPDATE_DOCS && !process.exitCode) {
    const docsDir = path.join(ROOT, 'docs', 'screenshots');
    // Check every source before replacing any documentation asset.
    for (const source of Object.keys(DOC_SCREENSHOTS)) {
      if (!fs.existsSync(path.join(SHOT_DIR, source))) {
        throw new Error(`Missing documentation screenshot: ${source}`);
      }
    }
    fs.mkdirSync(docsDir, { recursive: true });
    for (const [source, target] of Object.entries(DOC_SCREENSHOTS)) {
      fs.copyFileSync(path.join(SHOT_DIR, source), path.join(docsDir, target));
    }
    console.log(`Updated ${Object.keys(DOC_SCREENSHOTS).length} documentation screenshots.`);
  }
}

main().catch((error) => {
  console.error('FAILED', error);
  server.close();
  process.exit(1);
});
