/* yhShell frontend — plain JS, no build step. */
(() => {
  'use strict';

  window.__yhErrors = [];
  window.addEventListener('error', (event) => {
    window.__yhErrors.push(`${event.message} @ ${event.filename || ''}:${event.lineno || 0}`);
  });
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    window.__yhErrors.push('rejection: ' + (reason && reason.stack ? reason.stack : String(reason)));
  });

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const TERM_THEMES = {
    dark: {
      background: '#0b0e14',
      foreground: '#d8dee9',
      cursor: '#5b9dff',
      cursorAccent: '#0b0e14',
      selectionBackground: 'rgba(91,157,255,0.30)',
      black: '#1a2230',
      red: '#ff6b7a',
      green: '#45d09e',
      yellow: '#f2c45f',
      blue: '#5b9dff',
      magenta: '#9a8cff',
      cyan: '#4fd1c5',
      white: '#d8dee9',
      brightBlack: '#68738a',
      brightRed: '#ff8a96',
      brightGreen: '#6ee7b7',
      brightYellow: '#ffd77a',
      brightBlue: '#8bb9ff',
      brightMagenta: '#b9adff',
      brightCyan: '#7ee3d8',
      brightWhite: '#f2f5fa',
    },
    light: {
      background: '#f5f7fb',
      foreground: '#2b3446',
      cursor: '#2f74e0',
      cursorAccent: '#f5f7fb',
      selectionBackground: 'rgba(47,116,224,0.20)',
      black: '#2b3446',
      red: '#d93a4c',
      green: '#14966c',
      yellow: '#b8811a',
      blue: '#2f74e0',
      magenta: '#6f5cf0',
      cyan: '#0e8f9e',
      white: '#5b6779',
      brightBlack: '#8b96a9',
      brightRed: '#e4606f',
      brightGreen: '#25b083',
      brightYellow: '#cc9820',
      brightBlue: '#4b8ded',
      brightMagenta: '#8676f5',
      brightCyan: '#25a8b8',
      brightWhite: '#121926',
    },
  };

  const state = {
    booted: false,
    settings: {
      fontSize: 13,
      fontFamily: 'Cascadia Mono, JetBrains Mono, Consolas, monospace',
      theme: 'dark',
      agentApproval: 'confirm',
      commandTimeout: 90,
      maxAgentSteps: 8,
      copyOnSelect: false,
      terminalHighlight: true,
      terminalHighlightRules: { error: true, warning: true, success: true, address: true },
      zmodemConfirm: false,
      zmodemDownloadDir: '',
      zmodemOverwrite: false,
      updateAutoCheck: true,
    },
    update: null,
    models: [],
    providers: [],
    hosts: [],
    activeModelId: '',
    snippets: [],
    quickGroup: 'favorites',
    snippetPalette: {
      groupId: 'all',
      query: '',
      items: [],
      active: -1,
      pending: null,
    },
    sessions: new Map(),
    activeId: null,
    splitLayout: null,
    terminalTabs: new Map(),
    activeTabId: null,
    pendingOutput: {},
    pollBusy: false,
    app: {},
    chat: {
      sessionId: null,
      busy: false,
      streamEl: null,
      streamText: '',
      renderTimer: null,
      loadToken: 0,
      toolCards: new Map(),
      viewing: null,
    },
    collapsed: false,
    view: 'terminal',
    snippetGroupId: 'all',
    transfers: new Map(),
    transferDir: '',
  };

  // Exposed for diagnostics / UI tests.
  window.__yhState = state;

  /* ------------------------------------------------------------- helpers */
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }

  function api(method, ...args) {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge || typeof bridge[method] !== 'function') {
      return Promise.reject(new Error('应用后端未就绪，请稍候重试'));
    }
    return bridge[method](...args);
  }

  async function call(method, ...args) {
    try {
      return await api(method, ...args);
    } catch (err) {
      return { ok: false, error: err && err.message ? err.message : String(err) };
    }
  }

  function toast(message, type = 'info', duration = 4200) {
    const box = $('#toasts');
    const node = el('div', `toast ${type}`, message);
    box.appendChild(node);
    setTimeout(() => {
      node.style.opacity = '0';
      node.style.transform = 'translateY(6px)';
      node.style.transition = 'opacity .2s ease, transform .2s ease';
      setTimeout(() => node.remove(), 220);
    }, duration);
  }

  function activeSession() {
    return state.activeId ? state.sessions.get(state.activeId) || null : null;
  }

  /* --------------------------------------------------------------- bridge */
  let initAttempts = 0;
  let uiBound = false;

  /**
   * Wire every control as soon as the DOM is ready — deliberately before and
   * independently of the pywebview bridge.
   *
   * The bridge is missing whenever the page is opened outside the desktop app
   * (plain browser, a stalled backend, a bootstrap that keeps failing).  While
   * ``bindUI`` lived behind a successful ``bootstrap`` call the whole window
   * stayed inert in those cases, so 新建连接 / 配置 AI 模型 did nothing at all.
   * The handlers themselves are bridge-free: they open a dialog and only talk
   * to the backend once the user submits it.
   */
  function ensureUI() {
    if (uiBound) return;
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', ensureUI, { once: true });
      return;
    }
    uiBound = true;
    bindUI();
    restorePanelState();
    restoreView();
    if (window.innerWidth < 1200) setPanelCollapsed(true);
    updateWelcomeVisibility();
  }

  function waitForBridge() {
    ensureUI();
    let tries = 0;
    const poll = () => {
      if (window.pywebview && window.pywebview.api) {
        init();
        return;
      }
      tries += 1;
      if (tries === 80) {
        toast('无法连接到 Python 后端，界面仍可使用，但连接服务器会失败', 'error', 8000);
      }
      setTimeout(poll, tries < 80 ? 150 : 1000);
    };
    poll();
  }

  async function init() {
    if (state.booted) return;
    state.booted = true;
    let res;
    try {
      res = await call('bootstrap');
      if (!res || !res.ok) throw new Error((res && res.error) || '初始化失败');
      initAttempts = 0;
      ensureUI();
      applyBootstrap(res);
      startPolling();
      showChatEmpty(noSessionEmpty());
      state.chat.sessionId = null;
      updateComposerState();
    } catch (err) {
      state.booted = false;
      ensureUI();
      initAttempts += 1;
      window.__yhErrors.push('init: ' + (err && err.stack ? err.stack : String(err)));
      if (initAttempts <= 8) {
        setTimeout(init, 250 + initAttempts * 150);
      } else {
        toast('初始化失败：' + (err && err.message ? err.message : err), 'error', 8000);
      }
    }
  }

  function applyBootstrap(res) {
    state.settings = { ...state.settings, ...(res.settings || {}) };
    state.models = res.models || [];
    state.providers = res.providers || [];
    state.hosts = res.hosts || [];
    state.activeModelId = res.activeModelId || '';
    state.snippets = Array.isArray(res.snippets) ? res.snippets : [];
    state.app = res;
    if (res.updateState && typeof res.updateState === 'object') applyUpdateState(res.updateState);
    applyTheme(state.settings.theme);
    renderHosts();
    renderModels();
    renderSnippetPage();
    renderQuickBar();
    fillSettingsForm();
    updateApprovalUI();
    $('#about-python').textContent = res.pythonVersion || '—';
    $('#about-webview').textContent = res.webviewVersion || '—';
    $('#about-config').textContent = res.configDir || '—';
    $('#about-version').textContent = 'v' + (res.appVersion || '0.1.0');
    $$('.version-tag').forEach((node) => {
      node.textContent = 'v' + (res.appVersion || '0.1.0');
    });
  }

  /* --------------------------------------------------------------- theme */
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
    const termTheme = TERM_THEMES[theme === 'light' ? 'light' : 'dark'];
    state.sessions.forEach((session) => {
      try {
        session.term.options.theme = termTheme;
        session.highlighter?.refresh();
      } catch (_) { /* ignore */ }
    });
    call('window_set_theme', theme === 'light' ? 'light' : 'dark');
  }

  function applyFontSize(size) {
    state.sessions.forEach((session) => {
      try {
        session.term.options.fontSize = size;
      } catch (_) { /* ignore */ }
    });
    fitActive();
  }

  function restorePanelState() {
    let collapsed = false;
    try {
      collapsed = localStorage.getItem('yhshell.aiCollapsed') === '1';
    } catch (_) { /* storage may be unavailable */ }
    setPanelCollapsed(collapsed);
  }

  function setPanelCollapsed(collapsed) {
    state.collapsed = collapsed;
    $('#ai-panel').classList.toggle('collapsed', collapsed);
    $('#ai-rail').classList.toggle('hidden', !collapsed);
    try {
      localStorage.setItem('yhshell.aiCollapsed', collapsed ? '1' : '0');
    } catch (_) { /* ignore */ }
    fitActive();
  }

  /* --------------------------------------------------------- 页面路由 */
  const VIEWS = ['terminal', 'hosts', 'snippets'];

  function setView(view) {
    const target = VIEWS.includes(view) ? view : 'terminal';
    state.view = target;
    $$('#side-nav .menu-item').forEach((node) => {
      const active = node.dataset.view === target;
      node.classList.toggle('active', active);
      if (active) node.setAttribute('aria-current', 'true');
      else node.removeAttribute('aria-current');
    });
    $('#view-terminal').classList.toggle('hidden', target !== 'terminal');
    $('#view-hosts').classList.toggle('hidden', target !== 'hosts');
    $('#view-snippets').classList.toggle('hidden', target !== 'snippets');
    if (target === 'hosts') renderHostPage();
    if (target === 'snippets') renderSnippetPage();
    try {
      localStorage.setItem('yhshell.view', target);
    } catch (_) { /* ignore */ }
    updateWindowTitle();
    if (target === 'terminal') fitActive();
  }

  function restoreView() {
    let view = 'terminal';
    try {
      view = localStorage.getItem('yhshell.view') || 'terminal';
    } catch (_) { /* storage may be unavailable */ }
    setView(view);
  }

  function updateWindowTitle() {
    if (state.view !== 'terminal') {
      call('set_title', `${state.view === 'hosts' ? '主机' : '指令集'} — yhShell`);
      return;
    }
    const session = activeSession();
    call('set_title', session ? `${session.info.name || session.info.host} — yhShell` : 'yhShell');
  }

  /* --------------------------------------------------------------- hosts */
  function renderHostPage() {
    const box = $('#host-cards');
    if (!box) return;
    box.innerHTML = '';
    state.hosts.forEach((host) => {
      const card = el('div', 'host-card');
      card.dataset.hostId = host.id;
      const avatar = el('div', 'host-card-avatar', (host.name || host.host || '?').trim().charAt(0).toUpperCase());
      const main = el('div', 'host-card-main');
      const title = el('div', 'host-card-title');
      title.appendChild(el('span', 'host-card-name', host.name || host.host));
      title.appendChild(el('span', 'tag', host.auth === 'key' ? '私钥' : '密码'));
      main.appendChild(title);
      main.appendChild(el('div', 'host-card-sub', `${host.username}@${host.host}:${host.port}`));
      const actions = el('div', 'host-card-actions');
      const connect = el('button', 'btn small primary', '连接');
      connect.type = 'button';
      connect.dataset.act = 'connect';
      const edit = el('button', 'icon-btn');
      edit.type = 'button';
      edit.dataset.act = 'edit';
      edit.title = '编辑主机';
      edit.innerHTML = '<svg viewBox="0 0 16 16"><path d="M11.2 2.8l2 2L5.4 12.6l-2.6.6.6-2.6 7.8-7.8Z" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linejoin="round"/></svg>';
      const del = el('button', 'icon-btn');
      del.type = 'button';
      del.dataset.act = 'delete';
      del.title = '删除主机';
      del.innerHTML = '<svg viewBox="0 0 16 16"><path d="M4 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linejoin="round"/></svg>';
      actions.append(connect, edit, del);
      card.append(avatar, main, actions);
      box.appendChild(card);
    });
    $('#hosts-empty').classList.toggle('hidden', state.hosts.length > 0);
    $('#hosts-sub').textContent = `${state.hosts.length} 台已保存`;
    const counter = $('#host-total');
    if (counter) counter.textContent = String(state.hosts.length);
  }

  async function deleteHost(host) {
    const ok = await confirmDialog('删除主机', `确定删除「${host.name}」吗？保存的密码也会一并删除。`, '删除');
    if (!ok) return;
    const res = await call('delete_host', host.id);
    if (res.ok) {
      state.hosts = res.hosts || [];
      renderHostPage();
      toast('主机已删除', 'success');
    } else {
      toast(res.error || '删除失败', 'error');
    }
  }

  function renderHosts() {
    renderHostPage();
  }

  async function connectSavedHost(hostId, item) {
    if (item && item.dataset.busy === '1') return;
    if (item) {
      item.dataset.busy = '1';
      item.style.opacity = '0.6';
    }
    const res = await call('connect', { hostId, cols: 120, rows: 32 });
    if (item) {
      delete item.dataset.busy;
      item.style.opacity = '';
    }
    if (!res.ok) {
      toast(res.error || '连接失败', 'error', 7000);
      return;
    }
    if (res.hosts) {
      state.hosts = res.hosts;
      renderHosts();
    }
    registerSession(res.session);
    setView('terminal');
  }

  /* -------------------------------------------------------------- session */
  function registerSession(info, activate = true, parentGroupId = null) {
    if (state.sessions.has(info.id)) return;
    const session = {
      id: info.id,
      info,
      term: null,
      fit: null,
      el: null,
      tabEl: null,
      groupId: null,
      status: info.status || 'connecting',
      closedNotified: false,
    };

    const groupId = parentGroupId || info.id;
    session.groupId = groupId;
    const tab = el('div', 'tab');
    tab.dataset.gid = groupId;
    tab.dataset.sid = groupId;
    tab.setAttribute('role', 'tab');
    tab.tabIndex = 0;
    tab.title = `${info.username}@${info.host}:${info.port}`;
    const dot = el('span', 'tab-dot');
    const label = el('span', 'tab-label', info.name || info.host);
    const close = el('button', 'tab-close', '×');
    close.title = '关闭工作区';
    close.addEventListener('click', (event) => {
      event.stopPropagation();
      closeTab(groupId);
    });
    tab.append(dot, label, close);
    tab.addEventListener('click', () => activateSession(state.terminalTabs.get(groupId)?.activeId || info.id));
    tab.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      const group = state.terminalTabs.get(groupId);
      showTabMenu(event, state.sessions.get(group?.activeId) || session, true);
    });
    tab.addEventListener('keydown', (event) => {
      if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
        event.preventDefault();
        const rect = tab.getBoundingClientRect();
        const group = state.terminalTabs.get(groupId);
        showTabMenu({ clientX: rect.left, clientY: rect.bottom }, state.sessions.get(group?.activeId) || session, true);
        $('#tab-menu button').focus();
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        activateSession(state.terminalTabs.get(groupId)?.activeId || info.id);
      }
    });
    $('#tabs').appendChild(tab);
    session.tabEl = tab;
    session.tabDot = dot;
    if (parentGroupId) {
      const parent = state.terminalTabs.get(parentGroupId);
      if (parent) {
        tab.remove();
        session.tabEl = parent.tabEl;
        session.tabDot = parent.tabEl.querySelector('.tab-dot');
        parent.members.add(info.id);
      }
    } else {
      state.terminalTabs.set(groupId, { id: groupId, members: new Set([info.id]), layout: null, tabEl: tab });
    }

    const wrap = el('div', 'terminal-session');
    wrap.dataset.sid = info.id;
    const hostEl = el('div', 'terminal-host');
    const paneMenu = el('button', 'pane-menu', '⋯');
    paneMenu.title = '窗格菜单：分屏 / 关闭窗格';
    paneMenu.addEventListener('click', (event) => {
      event.stopPropagation();
      activateSession(info.id);
      const rect = paneMenu.getBoundingClientRect();
      showTabMenu({ clientX: rect.left, clientY: rect.bottom }, session);
    });
    wrap.append(hostEl, paneMenu);
    $('#terminal-stack').appendChild(wrap);
    session.el = wrap;
    session.hostEl = hostEl;

    const term = new Terminal({
      fontFamily: state.settings.fontFamily,
      fontSize: Number(state.settings.fontSize) || 13,
      theme: TERM_THEMES[state.settings.theme === 'light' ? 'light' : 'dark'],
      cursorBlink: true,
      cursorStyle: 'bar',
      scrollback: 8000,
      allowProposedApi: true,
      macOptionIsMeta: true,
      lineHeight: 1.25,
      letterSpacing: 0,
      convertEol: false,
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(hostEl);
    session.term = term;
    session.fit = fit;
    session.highlighter = new TerminalHighlight.Highlighter(term, () => state.settings);

    // onData also carries parser-generated protocol replies (DSR/DA). Do not
    // suppress it when a dialog is open: remote applications still need replies.
    term.onData((data) => {
      api('ssh_write', info.id, data).catch(() => {});
    });
    const paste = term.paste.bind(term);
    term.paste = (text) => {
      if (!topModal()) paste(text);
    };
    term.onResize(({ cols, rows }) => syncTerminalSize(session, cols, rows));
    term.attachCustomKeyEventHandler((event) => {
      if (topModal()) return false;
      if (event.type !== 'keydown') return true;
      if (event.ctrlKey && event.shiftKey && event.code === 'KeyC') {
        copySelection(session);
        return false;
      }
      if (event.ctrlKey && event.shiftKey && event.code === 'KeyV') {
        pasteInto(session);
        return false;
      }
      if (event.ctrlKey && event.shiftKey && event.code === 'KeyA') {
        term.selectAll();
        return false;
      }
      if (event.ctrlKey && event.shiftKey && event.code === 'KeyK') {
        // Opened by the document-level shortcut; keep the keystroke out of
        // the remote PTY.
        return false;
      }
      return true;
    });
    term.onSelectionChange(() => {
      if (state.settings.copyOnSelect && term.hasSelection()) {
        navigator.clipboard.writeText(term.getSelection()).catch(() => {});
      }
    });

    hostEl.addEventListener('focusin', () => {
      if (state.activeId !== info.id) activateSession(info.id, false);
    });
    wrap.addEventListener('pointerdown', () => {
      if (state.activeId !== info.id) activateSession(info.id, false);
    });
    wrap.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      activateSession(info.id);
      showTerminalMenu(event, session);
    });

    state.sessions.set(info.id, session);
    updateSessionCount();
    updateSessionVisuals(session);
    if (activate) activateSession(info.id);

    requestAnimationFrame(() => {
      fitActive();
      const queued = state.pendingOutput[info.id];
      if (queued) {
        delete state.pendingOutput[info.id];
        term.write(queued);
      }
      // A delayed callback from an older pane must never steal focus back.
      if (state.activeId === info.id) term.focus();
    });

    updateComposerState();
  }

  function updateSessionVisuals(session) {
    const status = session.status || 'connecting';
    if (session.tabDot) session.tabDot.className = `tab-dot ${status}`;
    if (session.tabEl) session.tabEl.classList.toggle('active', state.activeTabId === session.groupId);
    if (session.tabEl) {
      const label = session.tabEl.querySelector('.tab-label');
      if (label) label.textContent = session.info.name || session.info.host;
    }
    if (state.activeId === session.id) updateStatusbar();
  }

  function activateSession(sid, focus = true) {
    const target = state.sessions.get(sid);
    if (!target) return;
    if (state.activeId === sid) {
      if (focus) target.term.focus();
      return;
    }
    const workspaceChanged = state.activeTabId !== target.groupId;
    state.activeTabId = target.groupId;
    const group = state.terminalTabs.get(target.groupId);
    group.activeId = sid;
    state.splitLayout = group.layout;
    state.activeId = sid;
    if (workspaceChanged) renderSplit();
    else if (!state.splitLayout) fitActive();
    $$('.terminal-session').forEach((node) => {
      node.classList.toggle('active', node.dataset.sid === sid);
    });
    $$('#tabs .tab').forEach((node) => {
      node.classList.toggle('active', node.dataset.gid === state.activeTabId);
      node.setAttribute('aria-selected', String(node.dataset.gid === state.activeTabId));
    });
    updateWelcomeVisibility();
    updateStatusbar();
    updateComposerState();
    const session = state.sessions.get(sid);
    if (session) {
      setChatSession(sid);
      if (focus) session.term.focus();
    } else {
      setChatSession(null);
    }
    renderTransfer();
    updateWindowTitle();
  }

  function updateSessionCount() {
    const counter = $('#session-total');
    if (counter) counter.textContent = String(state.sessions.size);
  }

  function updateWelcomeVisibility() {
    const has = state.sessions.size > 0;
    $('#welcome').classList.toggle('hidden', has);
    $('#statusbar').classList.toggle('hidden', !has);
    const quick = $('#quick-bar');
    if (quick) quick.classList.toggle('hidden', !has);
  }

  function updateStatusbar() {
    const session = activeSession();
    const dot = $('#status-dot');
    const text = $('#status-text');
    const size = $('#status-size');
    const fp = $('#status-fingerprint');
    if (!session) {
      dot.className = 'dot';
      text.textContent = '未连接';
      size.textContent = '';
      fp.textContent = '';
      return;
    }
    dot.className = `dot ${session.status}`;
    const parts = [];
    if (session.status === 'connecting') parts.push('连接中');
    else if (session.status === 'connected') parts.push('已连接');
    else if (session.status === 'error') parts.push('连接失败');
    else parts.push('已断开');
    parts.push(`${session.info.username}@${session.info.host}:${session.info.port}`);
    if (session.status === 'error' && session.info.error) parts.push(`— ${session.info.error}`);
    text.textContent = parts.join(' · ');
    size.textContent = session.status === 'connected' ? `${session.term.cols}×${session.term.rows}` : '';
    fp.textContent = session.info.fingerprint || '';
  }

  function handleSessions(infos) {
    infos.forEach((info) => {
      const session = state.sessions.get(info.id);
      if (!session) return;
      const previous = session.status;
      const visualsChanged = previous !== info.status || ['name', 'host', 'username', 'port', 'error', 'fingerprint']
        .some((key) => info[key] !== undefined && session.info[key] !== info[key]);
      const composerChanged = previous !== info.status;
      session.info = { ...session.info, ...info };
      session.status = info.status;
      if (visualsChanged) updateSessionVisuals(session);
      if (previous !== info.status) {
        if (info.status === 'error' && !session.closedNotified) {
          session.closedNotified = true;
          toast(`${info.name || info.host} 连接失败：${info.error || '未知错误'}`, 'error', 8000);
        }
        if (info.status === 'connected') {
          toast(`已连接到 ${info.username}@${info.host}`, 'success', 2200);
        }
        if (info.status === 'closed' && !session.closedNotified) {
          session.closedNotified = true;
          toast(`${info.name || info.host} 会话已断开`, 'info', 3500);
        }
      }
      if (state.activeId === info.id && composerChanged) {
        updateCompositeHint();
        updateComposerState();
      }
    });
  }

  function updateCompositeHint() {
    const session = activeSession();
    const hint = $('#composer-hint');
    if (!session) {
      hint.textContent = '未连接会话';
    } else if (session.status === 'connecting') {
      hint.textContent = '连接中…';
    } else if (session.status === 'connected') {
      hint.textContent = 'Enter 发送 · Shift+Enter 换行';
    } else {
      hint.textContent = '会话已断开，无法执行命令';
    }
  }

  async function closeTab(groupId) {
    const group = state.terminalTabs.get(groupId);
    if (!group) return;
    const ids = [...group.members];
    const ok = await confirmDialog('关闭终端标签', `确定关闭此标签及其中 ${ids.length} 个终端会话吗？`, '关闭');
    if (!ok) return;
    for (const sid of ids) await closeSession(sid, true);
  }

  async function closeSession(sid, confirmed = false) {
    const session = state.sessions.get(sid);
    if (!session) return;
    if (!confirmed && session.status === 'connected') {
      const ok = await confirmDialog('断开连接', `确定断开「${session.info.name || session.info.host}」并关闭当前窗格吗？`, '断开');
      if (!ok) return;
    }
    await call('close_session', sid);
    const group = state.terminalTabs.get(session.groupId);
    let splitNext = null;
    if (group) {
      group.layout = pruneSplit(group.layout, sid);
      group.savedLayout = pruneSplit(group.savedLayout, sid);
      splitNext = splitLeaves(group.layout)[0];
      if (splitLeaves(group.layout).length < 2) group.layout = null;
      if (session.groupId === state.activeTabId) state.splitLayout = group.layout;
      group.members.delete(sid);
      if (!group.members.size) {
        group.tabEl.remove();
        state.terminalTabs.delete(group.id);
      } else if (group.activeId === sid) group.activeId = splitNext || [...group.members][0];
      splitNext = group.members.size ? (splitNext || group.activeId) : null;
      renderSplit();
    }
    session.highlighter?.dispose();
    try { session.term.dispose(); } catch (_) { /* ignore */ }
    session.el.remove();
    state.sessions.delete(sid);
    updateSessionCount();
    state.transfers.delete(sid);
    state.transferDir = '';
    delete state.pendingOutput[sid];
    if (state.activeId === sid) {
      state.activeId = null;
      const next = splitNext || Array.from(state.sessions.keys()).pop();
      if (next) {
        activateSession(next);
      } else {
        updateWelcomeVisibility();
        updateStatusbar();
        setChatSession(null);
        updateComposerState();
        call('set_title', 'yhShell');
      }
    } else {
      updateSessionVisuals(session);
    }
    if (state.sessions.size === 0) {
      $('#welcome').classList.remove('hidden');
      $('#statusbar').classList.add('hidden');
    }
  }

  function syncTerminalSize(session, cols, rows) {
    const size = `${cols}x${rows}`;
    if (session.lastSentSize === size) return;
    session.lastSentSize = size;
    api('ssh_resize', session.id, cols, rows).catch(() => {
      if (session.lastSentSize === size) session.lastSentSize = null;
    });
  }

  let fitTimer = null;
  function fitActive() {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(() => {
      const ids = $('#terminal-stack').dataset.splitIds;
      const visible = ids ? ids.split(',').map((id) => state.sessions.get(id)) : [activeSession()];
      visible.forEach((session) => {
        if (!session || !session.fit) return;
        if (!session.hostEl.clientWidth || !session.hostEl.clientHeight) return;
        try {
          const width = session.hostEl.clientWidth, height = session.hostEl.clientHeight;
          const geometry = `${width}x${height}:${state.settings.fontSize}:${state.settings.fontFamily}`;
          if (session.lastFitGeometry !== geometry) {
            session.fit.fit();
            session.lastFitGeometry = geometry;
          }
          syncTerminalSize(session, session.term.cols, session.term.rows);
          session.highlighter?.refresh();
        } catch (_) { /* ignore */ }
      });
      updateStatusbar();
    }, 45);
  }

  /* ---------------------------------------------------------- clipboard */
  async function copySelection(session) {
    if (!session || !session.term) return;
    const text = session.term.getSelection();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制', 'success', 1200);
    } catch (_) {
      toast('复制失败：无法访问剪贴板', 'error');
    }
  }

  async function pasteInto(session) {
    if (!session || !session.term) return;
    try {
      const text = await navigator.clipboard.readText();
      if (text) session.term.paste(text);
    } catch (_) {
      toast('粘贴失败：无法读取剪贴板', 'error');
    }
  }

  function showTerminalMenu(event, session) {
    hideTabMenu();
    const menu = $('#terminal-menu');
    menu.classList.remove('hidden');
    const width = 170;
    const height = 220;
    menu.style.left = Math.min(event.clientX, window.innerWidth - width - 8) + 'px';
    menu.style.top = Math.min(event.clientY, window.innerHeight - height - 8) + 'px';
    menu.dataset.sid = session.id;
  }

  function hideTerminalMenu() {
    $('#terminal-menu').classList.add('hidden');
  }

  function hideTabMenu() {
    $('#tab-menu').classList.add('hidden');
  }

  function showTabMenu(event, session, workspaceMenu = false) {
    hideTerminalMenu();
    const menu = $('#tab-menu');
    menu.dataset.workspace = workspaceMenu ? '1' : '0';
    const group = state.terminalTabs.get(session.groupId);
    $('[data-action="unsplit"]', menu).textContent = group?.layout ? '取消分屏' : '恢复分屏';
    $('[data-action="close"]', menu).textContent = workspaceMenu ? '关闭标签及所有窗格' : '关闭当前窗格';
    menu.classList.remove('hidden');
    const width = 170;
    const height = 190;
    menu.style.left = Math.min(event.clientX, window.innerWidth - width - 8) + 'px';
    menu.style.top = Math.min(event.clientY, window.innerHeight - height - 8) + 'px';
    menu.dataset.sid = session.id;
  }

  async function duplicateSession(sid, splitDirection = '') {
    const source = state.sessions.get(sid);
    if (!source) return;
    const res = await call('duplicate_session', sid, source.term?.cols || 120, source.term?.rows || 32);
    if (!res || !res.ok) {
      toast((res && res.error) || '复制会话失败', 'error');
      return;
    }
    if (splitDirection) activateSession(sid);
    registerSession(res.session, !splitDirection, splitDirection ? source.groupId : null);
    if (splitDirection) setSplit([sid, res.session.id], splitDirection);
    activateSession(res.session.id);
  }

  function splitLeaves(node = state.splitLayout) {
    if (!node) return [];
    return node.sid ? [node.sid] : [...splitLeaves(node.first), ...splitLeaves(node.second)];
  }

  function renderSplit() {
    const group = state.terminalTabs.get(state.activeTabId);
    if (group) group.layout = state.splitLayout;
    const stack = $('#terminal-stack');
    state.sessions.forEach((session) => {
      session.el.classList.toggle('workspace-active', session.groupId === state.activeTabId);
      session.tabEl?.classList.toggle('active', session.groupId === state.activeTabId);
    });
    const ids = splitLeaves();
    const visible = group ? [...group.members] : [];
    $$('.terminal-session', stack).forEach((node) => {
      node.classList.toggle('workspace-hidden', !visible.includes(node.dataset.sid));
    });
    stack.classList.toggle('is-split', ids.length > 1);
    if (ids.length > 1) stack.dataset.splitIds = ids.join(',');
    else delete stack.dataset.splitIds;
    $$('.terminal-session', stack).forEach((node) => {
      node.classList.remove('split-member');
      ['left', 'top', 'width', 'height'].forEach((key) => node.style.removeProperty(key));
    });
    $$('.split-divider', stack).forEach((node) => node.remove());
    applySplitGeometry(true);
    fitActive();
  }

  function applySplitGeometry(createDividers = false) {
    const stack = $('#terminal-stack');
    let index = 0;
    const layout = (node, x, y, width, height) => {
      if (node.sid) {
        const session = state.sessions.get(node.sid);
        if (!session) return;
        session.el.classList.add('split-member');
        Object.assign(session.el.style, { left: `${x}%`, top: `${y}%`, width: `${width}%`, height: `${height}%` });
        return;
      }
      const ratio = node.ratio ?? 0.5;
      const down = node.direction === 'down';
      const dividerIndex = index++;
      let divider = $(`.split-divider[data-index="${dividerIndex}"]`, stack);
      if (createDividers) {
        divider = el('div', `split-divider ${down ? 'horizontal' : 'vertical'}`);
        divider.dataset.index = String(dividerIndex);
        divider.setAttribute('role', 'separator');
        divider.setAttribute('aria-orientation', down ? 'horizontal' : 'vertical');
        divider.setAttribute('aria-valuemin', '10');
        divider.setAttribute('aria-valuemax', '90');
        divider.setAttribute('aria-label', down ? '调整分屏高度' : '调整分屏宽度');
        divider.tabIndex = 0;
        bindSplitDivider(divider, node);
        stack.appendChild(divider);
      }
      if (divider) {
        Object.assign(divider.style, down
          ? { left: `${x}%`, top: `${y + height * ratio}%`, width: `${width}%`, height: '6px' }
          : { left: `${x + width * ratio}%`, top: `${y}%`, width: '6px', height: `${height}%` });
        divider.setAttribute('aria-valuenow', String(Math.round(ratio * 100)));
      }
      if (down) {
        layout(node.first, x, y, width, height * ratio);
        layout(node.second, x, y + height * ratio, width, height * (1 - ratio));
      } else {
        layout(node.first, x, y, width * ratio, height);
        layout(node.second, x + width * ratio, y, width * (1 - ratio), height);
      }
      if (divider) divider.splitBounds = { x, y, width, height };
    };
    if (state.splitLayout) layout(state.splitLayout, 0, 0, 100, 100);
  }

  function bindSplitDivider(divider, node) {
    const down = node.direction === 'down';
    const clamp = (ratio) => Math.max(0.1, Math.min(0.9, ratio));
    let drag = null;
    let lastPress = null;
    let frame = null;
    const paint = () => {
      frame = null;
      applySplitGeometry();
      fitActive();
    };
    const finish = (event) => {
      if (!drag || (event && event.pointerId !== drag.pointerId)) return;
      drag = null;
      divider.classList.remove('dragging');
      $('#terminal-stack').classList.remove('split-dragging');
      if (frame !== null) { cancelAnimationFrame(frame); frame = null; }
      applySplitGeometry();
      fitActive();
    };
    divider.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      // Pointer capture can suppress native dblclick in WebView2.
      const now = performance.now();
      if (lastPress && now - lastPress.time < 400
        && Math.abs(event.clientX - lastPress.x) < 5 && Math.abs(event.clientY - lastPress.y) < 5) {
        lastPress = null;
        node.ratio = 0.5;
        paint();
        return;
      }
      lastPress = { time: now, x: event.clientX, y: event.clientY };
      const rect = $('#terminal-stack').getBoundingClientRect();
      const bounds = divider.splitBounds;
      drag = { pointerId: event.pointerId, rect, bounds: { ...bounds } };
      divider.setPointerCapture(event.pointerId);
      divider.classList.add('dragging');
      $('#terminal-stack').classList.add('split-dragging');
    });
    divider.addEventListener('pointermove', (event) => {
      if (!drag || drag.pointerId !== event.pointerId) return;
      event.preventDefault();
      if (lastPress && (Math.abs(event.clientX - lastPress.x) > 5 || Math.abs(event.clientY - lastPress.y) > 5)) lastPress = null;
      const { rect, bounds } = drag;
      const position = down ? (event.clientY - rect.top) / rect.height * 100 : (event.clientX - rect.left) / rect.width * 100;
      node.ratio = clamp((position - (down ? bounds.y : bounds.x)) / (down ? bounds.height : bounds.width));
      if (frame === null) frame = requestAnimationFrame(paint);
    });
    divider.addEventListener('pointerup', finish);
    divider.addEventListener('pointercancel', finish);
    divider.addEventListener('lostpointercapture', finish);
    divider.addEventListener('dblclick', () => { node.ratio = 0.5; paint(); });
    divider.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? 0.1 : 0.02;
      const delta = (down ? { ArrowUp: -step, ArrowDown: step } : { ArrowLeft: -step, ArrowRight: step })[event.key];
      if (delta === undefined && event.key !== 'Home') return;
      event.preventDefault();
      node.ratio = event.key === 'Home' ? 0.5 : clamp((node.ratio ?? 0.5) + delta);
      paint();
    });
  }

  function setSplit([sid, newId], direction = 'right') {
    if (!state.sessions.has(sid) || !state.sessions.has(newId)) return;
    const group = state.terminalTabs.get(state.activeTabId);
    if (!state.splitLayout && group?.savedLayout) {
      state.splitLayout = group.savedLayout;
      group.savedLayout = null;
    }
    if (!splitLeaves().includes(sid)) state.splitLayout = { sid };
    const replace = (node) => {
      if (node.sid) return node.sid === sid
        ? { direction, first: node, second: { sid: newId } } : node;
      return { ...node, first: replace(node.first), second: replace(node.second) };
    };
    state.splitLayout = replace(state.splitLayout);
    renderSplit();
  }

  function pruneSplit(node, sid) {
    if (!node || node.sid) return node && node.sid !== sid ? node : null;
    const first = pruneSplit(node.first, sid), second = pruneSplit(node.second, sid);
    return first && second ? { ...node, first, second } : first || second;
  }

  function clearSplit() {
    // Keep the layout so hidden panes can be restored without reconnecting.
    const group = state.terminalTabs.get(state.activeTabId);
    if (!group) return;
    if (state.splitLayout) {
      group.savedLayout = state.splitLayout;
      state.splitLayout = null;
    } else {
      state.splitLayout = group.savedLayout || null;
      group.savedLayout = null;
    }
    renderSplit();
  }

  /* ------------------------------------------------------------- snippets */
  const NEW_GROUP = '__new__';
  function snippetGroups() {
    return Array.isArray(state.snippets) ? state.snippets : [];
  }

  function parseSnippetVariables(text) {
    const variables = [];
    String(text || '').replace(/<([^<>\r\n]{1,40})>/g, (match, name) => {
      const clean = String(name || '').trim();
      if (clean && !variables.includes(clean)) variables.push(clean);
      return match;
    });
    return variables;
  }

  function snippetVariables(snippet) {
    if (!snippet) return [];
    const provided = Array.isArray(snippet.variables) ? snippet.variables : [];
    return provided.length ? provided : parseSnippetVariables(snippet.command);
  }

  function findSnippet(snippetId) {
    if (!snippetId) return null;
    let found = null;
    snippetGroups().some((group) => {
      const snippet = (group.items || []).find((item) => item.id === snippetId);
      if (!snippet) return false;
      found = { group, snippet };
      return true;
    });
    return found;
  }

  function snippetMatches(group, snippet, query) {
    if (!query) return true;
    const haystack = [
      group.name,
      group.description || '',
      snippet.name,
      snippet.command,
      snippet.note || '',
    ]
      .join(' ')
      .toLowerCase();
    return haystack.includes(query);
  }

  function highlightVariables(command) {
    return escapeHtml(command).replace(/&lt;([^&<>\r\n]{1,40})&gt;/g, '<span class="snip-var">$&</span>');
  }

  function snippetTotal() {
    return snippetGroups().reduce((sum, group) => sum + (group.items || []).length, 0);
  }

  function customSnippetGroups() {
    return snippetGroups().filter((group) => !group.builtin);
  }

  function applySnippetState(res) {
    if (!res || !Array.isArray(res.snippets)) return false;
    state.snippets = res.snippets;
    renderSnippetPage();
    renderQuickBar();
    return true;
  }

  function renderSnippetPage() {
    renderSnippetGroupList();
    renderSnippetTable();
  }

  function renderSnippetGroupList() {
    const list = $('#snippet-group-list');
    if (!list) return;
    list.innerHTML = '';
    const groups = snippetGroups();
    const total = snippetTotal();
    const all = el('li', `snippet-nav-item${state.snippetGroupId === 'all' ? ' active' : ''}`);
    all.dataset.groupId = 'all';
    all.append(el('span', 'snippet-nav-name', '全部'), el('span', 'snippet-nav-count', String(total)));
    list.appendChild(all);
    groups.forEach((group) => {
      const item = el('li', `snippet-nav-item${state.snippetGroupId === group.id ? ' active' : ''}`);
      item.dataset.groupId = group.id;
      item.append(
        el('span', 'snippet-nav-name', group.name),
        el('span', 'snippet-nav-count', String((group.items || []).length))
      );
      if (!group.builtin) {
        const edit = el('button', 'icon-btn');
        edit.type = 'button';
        edit.dataset.act = 'edit-group';
        edit.title = '重命名分组';
        edit.innerHTML = '<svg viewBox="0 0 16 16"><path d="M11.2 2.8l2 2L5.4 12.6l-2.6.6.6-2.6 7.8-7.8Z" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linejoin="round"/></svg>';
        const del = el('button', 'icon-btn');
        del.type = 'button';
        del.dataset.act = 'delete-group';
        del.title = '删除分组';
        del.innerHTML = '<svg viewBox="0 0 16 16"><path d="M4 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linejoin="round"/></svg>';
        item.append(edit, del);
      }
      list.appendChild(item);
    });
  }

  function snippetRow(group, item) {
    const row = el('div', 'snippet-row');
    row.dataset.snippetId = item.id;
    row.dataset.groupId = group.id;
    row.dataset.builtin = item.builtin ? '1' : '0';
    const main = el('div', 'snippet-row-main');
    const name = el('div', 'snippet-row-name');
    name.appendChild(el('span', 'snippet-row-label', item.name));
    if (item.builtin) {
      name.appendChild(el('span', `tag${item.edited ? ' edited' : ''}`, item.edited ? '已修改' : '内置'));
    } else {
      name.appendChild(el('span', 'tag custom', '自定义'));
    }
    main.appendChild(name);
    const code = el('code', 'snippet-row-cmd');
    code.innerHTML = highlightVariables(item.command);
    main.appendChild(code);
    row.appendChild(main);
    row.appendChild(el('div', 'snippet-row-note', item.note || ''));
    const vars = el('div', 'snippet-row-vars');
    snippetVariables(item).forEach((name) => vars.appendChild(el('span', 'var-chip', `<${name}>`)));
    row.appendChild(vars);
    const actions = el('div', 'snippet-row-actions');
    [
      ['copy', '复制', 'ghost'],
      ['insert', '插入', 'ghost'],
      ['edit', '编辑', 'ghost'],
      ['delete', '删除', 'danger'],
    ].forEach(([act, label, kind]) => {
      const button = el('button', `btn small ${kind}`, label);
      button.type = 'button';
      button.dataset.act = act;
      actions.appendChild(button);
    });
    row.appendChild(actions);
    return row;
  }

  function renderSnippetTable() {
    const box = $('#snippet-table');
    if (!box) return;
    const search = $('#snippet-page-search');
    const query = (search ? search.value : '').trim().toLowerCase();
    const groups = snippetGroups().filter(
      (group) => state.snippetGroupId === 'all' || group.id === state.snippetGroupId
    );
    const total = snippetTotal();
    box.innerHTML = '';
    let shown = 0;
    groups.forEach((group) => {
      const items = (group.items || []).filter((item) => snippetMatches(group, item, query));
      if (!items.length) return;
      shown += items.length;
      const head = el('div', 'snippet-table-head');
      head.append(
        el('span', 'snippet-table-group', group.name),
        el('span', 'snippet-table-count', `${items.length} 条`)
      );
      box.appendChild(head);
      items.forEach((item) => box.appendChild(snippetRow(group, item)));
    });
    if (!shown) {
      const empty = el('div', 'page-empty');
      empty.appendChild(el('p', '', query ? '没有匹配的指令' : '这个分组还没有指令'));
      box.appendChild(empty);
    }
    const sub = $('#snippets-sub');
    if (sub) sub.textContent = `${shown} 条指令`;
    const counter = $('#snippet-total');
    if (counter) counter.textContent = String(total);
    const matched = $('#snippet-page-matched');
    if (matched) {
      matched.textContent = query ? `匹配 ${shown} / ${total}` : '';
      matched.classList.toggle('hidden', !query);
    }
  }

  async function copySnippetCommand(snippet) {
    try {
      await navigator.clipboard.writeText(snippet.command);
      toast('命令已复制', 'success', 1200);
    } catch (_) {
      toast('复制失败：无法访问剪贴板', 'error');
    }
  }

  function showSnippetError(message) {
    const node = $('#snippet-error');
    if (!node) return;
    node.textContent = message;
    node.classList.remove('hidden');
  }

  function openSnippetModal(snippet, group) {
    const editing = Boolean(snippet);
    const builtin = Boolean(snippet && snippet.builtin);
    $('#snippet-modal-title').textContent = editing ? '编辑指令' : '新建指令';
    $('#snippet-id').value = snippet ? snippet.id : '';
    $('#snippet-builtin').value = builtin ? '1' : '';
    $('#snippet-name').value = snippet ? snippet.name : '';
    $('#snippet-command').value = snippet ? snippet.command : '';
    $('#snippet-note').value = snippet ? snippet.note || '' : '';
    $('#snippet-error').classList.add('hidden');
    const groupField = $('#snippet-group-field');
    const select = $('#snippet-group-select');
    const groups = customSnippetGroups();
    groupField.classList.toggle('hidden', builtin);
    $('#snippet-new-group').value = '';
    $('#snippet-new-group').classList.add('hidden');
    if (!builtin) {
      select.innerHTML = '';
      groups.forEach((entry) => {
        const option = document.createElement('option');
        option.value = entry.id;
        option.textContent = entry.name;
        select.appendChild(option);
      });
      const fresh = document.createElement('option');
      fresh.value = NEW_GROUP;
      fresh.textContent = '＋ 新建分组…';
      select.appendChild(fresh);
      const current = group && !group.builtin ? group.id : '';
      select.value = current || (groups.length ? groups[0].id : NEW_GROUP);
      $('#snippet-new-group').classList.toggle('hidden', select.value !== NEW_GROUP);
      $('#snippet-group-hint').textContent = groups.length
        ? '自定义指令保存在你自己的分组里。'
        : '填写新分组名称后，指令会保存在这个分组中。';
    }
    openModal('snippet-modal');
    $('#snippet-name').focus();
  }

  async function submitSnippetForm(event) {
    event.preventDefault();
    $('#snippet-error').classList.add('hidden');
    const command = $('#snippet-command').value.trim();
    if (!command) {
      showSnippetError('请填写命令');
      return;
    }
    const builtin = $('#snippet-builtin').value === '1';
    const payload = {
      id: $('#snippet-id').value,
      builtin,
      name: $('#snippet-name').value.trim(),
      command,
      note: $('#snippet-note').value.trim(),
    };
    if (!builtin) {
      const select = $('#snippet-group-select');
      if (select.value === NEW_GROUP) {
        const groupName = $('#snippet-new-group').value.trim();
        if (!groupName) {
          showSnippetError('请填写新分组名称');
          return;
        }
        payload.groupName = groupName;
      } else {
        payload.groupId = select.value;
      }
    }
    const res = await call('snippet_save', payload);
    if (!res.ok) {
      showSnippetError(res.error || '保存失败');
      return;
    }
    applySnippetState(res);
    closeModal('snippet-modal');
    toast('指令已保存', 'success');
  }

  async function deleteSnippet(snippet) {
    const ok = await confirmDialog(
      '删除指令',
      `确定删除「${snippet.name}」吗？${snippet.builtin ? '内置指令只是被隐藏，随时可以恢复。' : ''}`,
      '删除'
    );
    if (!ok) return;
    const res = await call('snippet_delete', { id: snippet.id, builtin: Boolean(snippet.builtin) });
    if (!res.ok) {
      toast(res.error || '删除失败', 'error');
      return;
    }
    applySnippetState(res);
    toast('指令已删除', 'success');
  }

  function openGroupModal(group) {
    $('#group-modal-title').textContent = group ? '重命名分组' : '新建分组';
    $('#group-id').value = group ? group.id : '';
    $('#group-name').value = group ? group.name : '';
    $('#group-error').classList.add('hidden');
    $('#btn-group-delete').classList.toggle('hidden', !group);
    openModal('group-modal');
    $('#group-name').focus();
  }

  async function submitGroupForm(event) {
    event.preventDefault();
    $('#group-error').classList.add('hidden');
    const name = $('#group-name').value.trim();
    if (!name) {
      $('#group-error').textContent = '请填写分组名称';
      $('#group-error').classList.remove('hidden');
      return;
    }
    const id = $('#group-id').value;
    const res = await call('snippet_save_group', { id, name });
    if (!res.ok) {
      $('#group-error').textContent = res.error || '保存失败';
      $('#group-error').classList.remove('hidden');
      return;
    }
    applySnippetState(res);
    if (!id && res.groupId) state.snippetGroupId = res.groupId;
    renderSnippetPage();
    closeModal('group-modal');
    toast('分组已保存', 'success');
  }

  async function deleteGroup(group) {
    const ok = await confirmDialog(
      '删除分组',
      `确定删除分组「${group.name}」吗？分组里的自定义指令会一起删除。`,
      '删除'
    );
    if (!ok) return;
    const res = await call('snippet_delete_group', { groupId: group.id });
    if (!res.ok) {
      toast(res.error || '删除失败', 'error');
      return;
    }
    if (state.snippetGroupId === group.id) state.snippetGroupId = 'all';
    applySnippetState(res);
    toast('分组已删除', 'success');
  }

  async function resetSnippets() {
    const ok = await confirmDialog(
      '恢复内置指令集',
      '恢复所有内置指令并清除对内置指令的修改？你创建的分组和指令会保留。',
      '恢复'
    );
    if (!ok) return;
    const res = await call('snippet_reset');
    if (!res.ok) {
      toast(res.error || '恢复失败', 'error');
      return;
    }
    applySnippetState(res);
    toast('已恢复内置指令集', 'success');
  }

  /* ------------------------------------------------- 快捷指令条（终端底部） */
  const QUICK_FAVORITES = 'favorites';

  function quickEntries(groupId) {
    const groups = snippetGroups();
    if (groupId && groupId !== QUICK_FAVORITES) {
      const group = groups.find((entry) => entry.id === groupId);
      if (group) {
        return (group.items || []).map((snippet) => ({ group, snippet }));
      }
    }
    const entries = [];
    groups.forEach((group) => {
      (group.items || []).forEach((snippet) => {
        if (snippet.favorite) entries.push({ group, snippet });
      });
    });
    return entries;
  }

  function renderQuickBar() {
    const select = $('#quick-group');
    const items = $('#quick-items');
    if (!select || !items) return;
    const groups = snippetGroups();
    select.innerHTML = '';
    const addOption = (value, label) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    };
    addOption(QUICK_FAVORITES, '常用');
    groups.forEach((group) => addOption(group.id, group.name));
    const wanted = state.quickGroup;
    const known = wanted === QUICK_FAVORITES || groups.some((group) => group.id === wanted);
    state.quickGroup = known ? wanted : QUICK_FAVORITES;
    select.value = state.quickGroup;
    items.innerHTML = '';
    quickEntries(state.quickGroup).forEach((entry) => {
      const snippet = entry.snippet;
      const button = el('button', 'quick-item');
      button.type = 'button';
      button.dataset.snippetId = snippet.id;
      button.title = snippet.command;
      if (snippetVariables(snippet).length) {
        button.appendChild(el('span', 'quick-dot'));
      }
      button.appendChild(el('span', 'quick-item-name', snippet.name));
      items.appendChild(button);
    });
    const more = $('#quick-more');
    if (more) {
      const group = groups.find((entry) => entry.id === state.quickGroup);
      more.title = group ? `打开「${group.name}」的完整指令面板 (Ctrl+Shift+K)` : '打开完整指令面板 (Ctrl+Shift+K)';
    }
  }

  function currentCommandSession() {
    const session = activeSession();
    if (!session) {
      toast('请先连接一台服务器，再插入指令', 'error', 2600);
      return null;
    }
    if (session.status !== 'connected') {
      toast('当前会话未连接，无法插入指令', 'error', 2600);
      return null;
    }
    if (
      (session.term && session.term.options && session.term.options.disableStdin)
      || (session.el && session.el.classList.contains('agent-running'))
    ) {
      toast('AI 正在执行命令，终端输入已锁定，完成后再插入指令', 'error', 3200);
      return null;
    }
    const transfer = state.transfers.get(session.id);
    if (transfer && ['starting', 'prompt', 'transferring'].includes(transfer.phase)) {
      toast('文件传输中，终端输入已屏蔽，完成后再插入指令', 'error', 3200);
      return null;
    }
    return session;
  }

  /**
   * Write a command into the terminal's input line without pressing Enter.
   * The remote shell echoes it exactly like typed keys; the operator reviews
   * (and may fill placeholders) and runs it manually.
   */
  function insertSnippetCommand(command) {
    const session = currentCommandSession();
    if (!session) return false;
    const text = String(command || '').replace(/[\r\n]+/g, ' ').trim();
    if (!text) return false;
    try {
      session.term.paste(text);
      session.term.focus();
    } catch (_) {
      toast('插入失败：终端尚未就绪', 'error');
      return false;
    }
    toast('已插入到终端，按 Enter 执行', 'success', 1500);
    return true;
  }

  function useSnippet(snippet) {
    if (!snippet) return;
    if (snippetVariables(snippet).length) openSnippetPalette({ snippet });
    else insertSnippetCommand(snippet.command);
  }

  function commandWithSnippetValues(snippet, values) {
    let command = String((snippet && snippet.command) || '');
    snippetVariables(snippet).forEach((name) => {
      const value = String((values && values[name]) || '').trim();
      if (value) command = command.split(`<${name}>`).join(value);
    });
    return command;
  }

  function snippetPaletteNode() {
    return $('#snippet-palette');
  }

  function openSnippetPalette(options) {
    if (topModal()) return;
    const node = snippetPaletteNode();
    if (!node) return;
    const palette = state.snippetPalette;
    const found = options && options.snippet ? findSnippet(options.snippet.id) : null;
    palette.pending = found ? found.snippet : null;
    palette.groupId = options && options.groupId ? options.groupId : found ? found.group.id : 'all';
    palette.query = '';
    node.classList.remove('hidden');
    node.classList.remove('var-mode');
    const form = $('#snippet-var-form');
    if (form) form.classList.add('hidden');
    const search = $('#snippet-palette-search');
    if (search) search.value = '';
    renderSnippetPaletteGroups();
    renderSnippetPaletteList();
    if (palette.pending) {
      showSnippetVariables(palette.pending);
    } else if (search) {
      search.focus();
    }
  }

  function closeSnippetPalette() {
    const node = snippetPaletteNode();
    if (!node || node.classList.contains('hidden')) return false;
    node.classList.add('hidden');
    node.classList.remove('var-mode');
    const form = $('#snippet-var-form');
    if (form) form.classList.add('hidden');
    state.snippetPalette.pending = null;
    state.snippetPalette.active = -1;
    const session = activeSession();
    if (session && session.term) {
      try { session.term.focus(); } catch (_) { /* ignore */ }
    }
    return true;
  }

  function renderSnippetPaletteGroups() {
    const nav = $('#snippet-palette-groups');
    if (!nav) return;
    nav.innerHTML = '';
    const groups = snippetGroups();
    const total = groups.reduce((sum, group) => sum + (group.items || []).length, 0);
    const add = (id, name, count) => {
      const button = el('button', 'snippet-group-tab');
      button.type = 'button';
      button.dataset.groupId = id;
      button.classList.toggle('active', state.snippetPalette.groupId === id);
      button.append(el('span', 'snippet-group-tab-name', name), el('span', 'snippet-group-count', String(count)));
      nav.appendChild(button);
    };
    add('all', '全部指令', total);
    groups.forEach((group) => add(group.id, group.name, (group.items || []).length));
  }

  function paletteMatches() {
    const query = state.snippetPalette.query.trim().toLowerCase();
    const groupId = state.snippetPalette.groupId;
    const matches = [];
    snippetGroups().forEach((group) => {
      if (groupId !== 'all' && group.id !== groupId) return;
      (group.items || []).forEach((snippet) => {
        if (snippetMatches(group, snippet, query)) matches.push({ group, snippet });
      });
    });
    return matches;
  }

  function renderSnippetPaletteList() {
    const list = $('#snippet-palette-list');
    if (!list) return;
    const matches = paletteMatches();
    state.snippetPalette.items = matches;
    state.snippetPalette.active = matches.length ? 0 : -1;
    list.innerHTML = '';
    if (!matches.length) {
      list.appendChild(el('div', 'snippet-palette-empty', '没有匹配的指令，换个关键词试试'));
    } else {
      let previousGroup = '';
      matches.forEach((entry, index) => {
        if (state.snippetPalette.groupId === 'all' && entry.group.id !== previousGroup) {
          previousGroup = entry.group.id;
          list.appendChild(el('div', 'snippet-palette-group-label', entry.group.name));
        }
        const variables = snippetVariables(entry.snippet);
        const button = el('button', `snippet-palette-item${index === 0 ? ' active' : ''}`);
        button.type = 'button';
        button.dataset.index = String(index);
        button.dataset.snippetId = entry.snippet.id;
        button.setAttribute('role', 'option');
        button.setAttribute('aria-selected', index === 0 ? 'true' : 'false');
        button.title = entry.snippet.note || entry.snippet.command;
        const head = el('span', 'snippet-palette-item-head');
        head.appendChild(el('span', 'snippet-palette-item-name', entry.snippet.name));
        if (variables.length) head.appendChild(el('span', 'snippet-var-badge', `${variables.length} 个变量`));
        const code = el('code', 'snippet-palette-item-cmd');
        code.innerHTML = highlightVariables(entry.snippet.command);
        button.append(head, code);
        if (entry.snippet.note) button.appendChild(el('span', 'snippet-palette-item-note', entry.snippet.note));
        list.appendChild(button);
      });
    }
    const counter = $('#snippet-count');
    if (counter) counter.textContent = String(matches.length);
    list.scrollTop = 0;
  }

  function paletteActiveItem(index) {
    const items = state.snippetPalette.items;
    if (!items.length) return;
    const next = index < 0 ? items.length - 1 : index >= items.length ? 0 : index;
    state.snippetPalette.active = next;
    $$('#snippet-palette-list .snippet-palette-item').forEach((node) => {
      const selected = Number(node.dataset.index) === next;
      node.classList.toggle('active', selected);
      node.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
    const active = $(`#snippet-palette-list .snippet-palette-item[data-index="${next}"]`);
    if (active) active.scrollIntoView({ block: 'nearest' });
  }

  function snippetVariableValues() {
    const values = {};
    $$('#snippet-var-fields input').forEach((input) => {
      values[input.dataset.varName] = input.value;
    });
    return values;
  }

  function updateSnippetPreview() {
    const snippet = state.snippetPalette.pending;
    const preview = $('#snippet-var-preview');
    if (!snippet || !preview) return;
    preview.innerHTML = highlightVariables(commandWithSnippetValues(snippet, snippetVariableValues()));
  }

  function showSnippetVariables(snippet) {
    if (!snippet) return;
    const node = snippetPaletteNode();
    const form = $('#snippet-var-form');
    const fields = $('#snippet-var-fields');
    if (!node || !form || !fields) return;
    state.snippetPalette.pending = snippet;
    node.classList.add('var-mode');
    form.classList.remove('hidden');
    $('#snippet-var-title').textContent = `填写变量 · ${snippet.name}`;
    fields.innerHTML = '';
    const variables = snippetVariables(snippet);
    if (!variables.length) {
      fields.appendChild(el('p', 'snippet-var-tip', '这条指令没有变量，可以直接插入。'));
    }
    variables.forEach((name) => {
      const label = el('label', 'snippet-var-field');
      label.appendChild(el('span', 'snippet-var-label', `<${name}>`));
      const input = document.createElement('input');
      input.type = 'text';
      input.dataset.varName = name;
      input.placeholder = `输入${name}`;
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.addEventListener('input', updateSnippetPreview);
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.isComposing) {
          event.preventDefault();
          event.stopPropagation();
          insertPendingSnippet(false);
        }
      });
      label.appendChild(input);
      fields.appendChild(label);
    });
    updateSnippetPreview();
    const first = fields.querySelector('input');
    if (first) first.focus();
  }

  function insertPendingSnippet(keepPlaceholders) {
    const snippet = state.snippetPalette.pending;
    if (!snippet) return;
    const command = keepPlaceholders
      ? snippet.command
      : commandWithSnippetValues(snippet, snippetVariableValues());
    if (insertSnippetCommand(command)) closeSnippetPalette();
  }

  function handlePaletteSnippet(snippet) {
    if (!snippet) return;
    if (snippetVariables(snippet).length) showSnippetVariables(snippet);
    else if (insertSnippetCommand(snippet.command)) closeSnippetPalette();
  }

  function handleSnippetPaletteKey(event) {
    const node = snippetPaletteNode();
    if (!node || node.classList.contains('hidden')) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeSnippetPalette();
      return;
    }
    if (node.classList.contains('var-mode')) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const base = state.snippetPalette.active < 0 ? 0 : state.snippetPalette.active;
      paletteActiveItem(base + (event.key === 'ArrowDown' ? 1 : -1));
      return;
    }
    if (event.key === 'Enter') {
      if (event.target.closest('button, a, textarea')) return;
      const entry = state.snippetPalette.items[state.snippetPalette.active];
      if (entry) {
        event.preventDefault();
        handlePaletteSnippet(entry.snippet);
      }
    }
  }

  /* ------------------------------------------------------------ terminals */
  function startPolling() {
    if (state.pollBusy) return;
    const tick = async () => {
      state.pollBusy = true;
      try {
        const res = await api('poll');
        if (res && res.ok) handlePoll(res);
      } catch (_) {
        /* bridge hiccup — retry next tick */
      }
      state.pollBusy = false;
      setTimeout(tick, 30);
    };
    tick();
  }

  function handlePoll(res) {
    if (res.sessions) handleSessions(res.sessions);
    if (res.output) {
      Object.entries(res.output).forEach(([sid, text]) => {
        const session = state.sessions.get(sid);
        if (session && session.term) {
          session.term.write(text);
        } else {
          state.pendingOutput[sid] = (state.pendingOutput[sid] || '') + text;
        }
      });
    }
    if (res.events) {
      res.events.forEach((event) => {
        if (event && event.type === 'models_changed') refreshModelState();
        else if (event && event.type === 'update_check') handleUpdateEvent(event);
        else handleAgentEvent(event);
      });
    }
    if (res.transfers) handleTransfers(res.transfers);
  }

  /* ------------------------------------------------------- zmodem (rz/sz) */
  function formatBytes(value) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = Math.max(0, Number(value) || 0);
    let index = 0;
    while (size >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    return `${index === 0 ? Math.round(size) : size.toFixed(1)} ${units[index]}`;
  }

  function formatSpeed(value) {
    const bps = Number(value) || 0;
    return bps > 0 ? `${formatBytes(bps)}/s` : '';
  }

  function activeTransfer() {
    return state.activeId ? state.transfers.get(state.activeId) : null;
  }

  function handleTransfers(transfers) {
    const seen = new Set();
    Object.entries(transfers || {}).forEach(([sid, snap]) => {
      seen.add(sid);
      const previous = state.transfers.get(sid);
      if (!previous || previous.id !== snap.id) {
        if (snap.phase === 'prompt' && sid !== state.activeId) {
          const session = state.sessions.get(sid);
          const name = (session && (session.info.name || session.info.host)) || '某个会话';
          toast(`${name} 请求文件传输，切换过去即可确认`, 'info', 6000);
        }
        state.transfers.set(sid, snap);
        if (sid === state.activeId) state.transferDir = '';
        return;
      }
      state.transfers.set(sid, snap);
      if (previous.phase !== snap.phase) {
        if (snap.phase === 'done') {
          toast(snap.message || '文件传输完成', 'success', 3600);
        } else if (snap.phase === 'error') {
          toast(`文件传输失败：${snap.error || '未知错误'}`, 'error', 7000);
        } else if (snap.phase === 'cancelled') {
          toast(snap.message || '已取消文件传输', 'info', 3000);
        }
      }
    });
    for (const sid of [...state.transfers.keys()]) {
      if (!seen.has(sid)) state.transfers.delete(sid);
    }
    renderTransfer();
  }

  function renderTransfer() {
    const card = $('#transfer-card');
    if (!card) return;
    const session = activeSession();
    const snap = session ? state.transfers.get(session.id) : null;
    if (!snap || !snap.phase || snap.phase === 'starting') {
      card.classList.add('hidden');
      document.body.classList.remove('transfer-open');
      return;
    }
    const download = snap.direction === 'download';
    const phase = snap.phase || '';
    const directory = state.transferDir || snap.directory || '';
    card.classList.remove('hidden');
    card.classList.toggle('is-upload', !download);
    card.classList.toggle('is-done', phase === 'done');
    card.classList.toggle('is-error', phase === 'error' || phase === 'cancelled');
    card.classList.toggle('is-prompt', phase === 'prompt');
    document.body.classList.add('transfer-open');
    card.dataset.phase = phase;
    card.dataset.direction = snap.direction || '';

    const title = $('#transfer-title');
    const subtitle = $('#transfer-subtitle');
    const file = $('#transfer-file');
    const fill = $('#transfer-fill');
    const size = $('#transfer-size');
    const speed = $('#transfer-speed');
    const count = $('#transfer-count');
    const path = $('#transfer-path');
    const cancel = $('#transfer-cancel');
    const browse = $('#transfer-browse');
    const accept = $('#transfer-accept');

    file.textContent = '';
    size.textContent = '';
    speed.textContent = '';
    count.textContent = '';
    path.textContent = '';
    cancel.classList.add('hidden');
    browse.classList.add('hidden');
    accept.classList.add('hidden');

    if (phase === 'prompt') {
      title.textContent = download ? '接收文件（sz）' : '发送文件（rz）';
      subtitle.textContent = download ? '远端在等你确认保存位置（sz 最多等约 20 秒）' : '远端在等你选择要上传的文件（rz 约等 50 秒）';
      file.textContent = download ? '远端会话请求发送文件' : '选择一个或多个本地文件';
      path.textContent = download ? `保存到：${directory}` : '文件会保存到远端 rz 的当前目录';
      fill.style.width = '100%';
      cancel.classList.remove('hidden');
      accept.classList.remove('hidden');
      accept.textContent = download ? '开始接收' : '选择文件…';
      if (download) {
        browse.classList.remove('hidden');
        browse.textContent = '更改位置';
      }
    } else if (phase === 'transferring') {
      title.textContent = download ? '正在接收文件' : '正在发送文件';
      subtitle.textContent = 'ZMODEM 传输中，按 Ctrl+C 也可以取消';
      file.textContent = snap.file || '准备中…';
      fill.style.width = `${Math.max(0, Math.min(100, Number(snap.percent) || 0))}%`;
      if (snap.fileSize) {
        size.textContent = `${formatBytes(snap.fileDone)} / ${formatBytes(snap.fileSize)}`;
      } else if (snap.fileDone) {
        size.textContent = formatBytes(snap.fileDone);
      }
      speed.textContent = formatSpeed(snap.speed);
      if (snap.files && snap.files.length > 1) count.textContent = `${snap.files.length} 个文件`;
      path.textContent = download ? directory : '';
      cancel.classList.remove('hidden');
    } else if (phase === 'done') {
      title.textContent = download ? '接收完成' : '发送完成';
      subtitle.textContent = snap.message || '';
      file.textContent = snap.savedTo || snap.message || '';
      fill.style.width = '100%';
      size.textContent = formatBytes(snap.doneBytes || snap.fileSize || 0);
      if (snap.files && snap.files.length > 1) count.textContent = `${snap.files.length} 个文件`;
      if (download) path.textContent = `保存到：${snap.savedTo ? snap.savedTo.replace(/[\\/][^\\/]+$/, '') : directory}`;
      accept.classList.remove('hidden');
      accept.textContent = '关闭';
      if (download) {
        browse.classList.remove('hidden');
        browse.textContent = '打开文件夹';
      }
    } else {
      title.textContent = phase === 'cancelled' ? '已取消传输' : '传输失败';
      subtitle.textContent = snap.message || '';
      file.textContent = snap.error || snap.message || '未知错误';
      fill.style.width = '100%';
      accept.classList.remove('hidden');
      accept.textContent = '关闭';
    }
  }

  async function pickTransferDir() {
    const snap = activeTransfer();
    const current = state.transferDir || (snap && snap.directory) || '';
    const res = await call('zmodem_pick_directory', current);
    if (res && res.ok && res.path) {
      state.transferDir = res.path;
      renderTransfer();
    } else if (res && !res.ok) {
      toast(res.error || '无法选择目录', 'error');
    }
  }

  async function acceptTransfer() {
    const sid = state.activeId;
    const snap = activeTransfer();
    if (!sid || !snap) return;
    if (snap.phase === 'prompt' && snap.direction === 'upload') {
      const picked = await call('zmodem_pick_files');
      if (!picked || !picked.ok) {
        toast((picked && picked.error) || '无法打开文件选择框', 'error');
        return;
      }
      if (!picked.files || !picked.files.length) return;
      const res = await call('zmodem_accept', sid, { files: picked.files });
      if (res && !res.ok) toast(res.error || '无法开始传输', 'error');
      return;
    }
    if (snap.phase === 'prompt') {
      const directory = state.transferDir || snap.directory || '';
      const res = await call('zmodem_accept', sid, { directory });
      if (res && !res.ok) toast(res.error || '无法开始传输', 'error');
      return;
    }
    dismissTransfer();
  }

  async function browseTransfer() {
    const snap = activeTransfer();
    if (!snap) return;
    if (snap.phase === 'done') {
      const target = snap.savedTo ? snap.savedTo.replace(/[\\/][^\\/]+$/, '') : snap.directory;
      const res = await call('open_folder', target || '');
      if (res && !res.ok) toast(res.error || '无法打开文件夹', 'error');
      return;
    }
    await pickTransferDir();
  }

  async function dismissTransfer() {
    const sid = state.activeId;
    if (!sid) return;
    state.transfers.delete(sid);
    renderTransfer();
    await call('zmodem_dismiss', sid);
  }

  /* ------------------------------------------------------------ chat core */
  function chatContainer() {
    return $('#chat');
  }

  function scrollChat(force) {
    const chat = chatContainer();
    const nearBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 90;
    if (force || nearBottom) chat.scrollTop = chat.scrollHeight;
  }

  function showChatEmpty(html) {
    const chat = chatContainer();
    chat.innerHTML = '';
    const box = el('div', 'chat-empty');
    box.innerHTML = html;
    chat.appendChild(box);
  }

  function noSessionEmpty() {
    return `
      <div class="chat-empty-icon">✦</div>
      <p class="chat-empty-title">还没有连接的会话</p>
      <p class="chat-empty-text">先连接一台服务器，AI 助手就能在它的终端旁边执行命令、排查问题。</p>
      <button class="suggestion open-history">查看本地历史对话</button>`;
  }

  function suggestionsEmpty() {
    return `
      <div class="chat-empty-icon">✦</div>
      <p class="chat-empty-title">AI 运维助手</p>
      <p class="chat-empty-text">描述你想检查或处理的事情，例如：</p>
      <button class="suggestion" data-text="看看磁盘和内存的使用情况，有什么异常吗？">检查磁盘与内存</button>
      <button class="suggestion" data-text="帮我排查一下最近 nginx 的报错日志">排查 nginx 日志</button>
      <button class="suggestion" data-text="列出当前占用 CPU 最高的 5 个进程">查看高 CPU 进程</button>
      <button class="suggestion open-history">查看本地历史对话</button>`;
  }

  function bindSuggestionButtons() {
    $$('#chat .suggestion').forEach((button) => {
      if (button.classList.contains('open-history')) return;
      button.addEventListener('click', () => {
        const input = $('#ai-input');
        input.value = button.dataset.text || button.textContent;
        autoGrowInput();
        input.focus();
      });
    });
  }

  function setChatSession(sid) {
    if (state.chat.sessionId === sid) {
      updateComposerState();
      return;
    }
    state.chat.viewing = null;
    hideChatBanner();
    state.chat.sessionId = sid;
    state.chat.busy = false;
    state.chat.streamEl = null;
    state.chat.streamText = '';
    state.chat.toolCards = new Map();
    state.chat.loadToken += 1;
    const token = state.chat.loadToken;
    const chat = chatContainer();
    chat.innerHTML = '';
    if (!sid) {
      showChatEmpty(noSessionEmpty());
      updateComposerState();
      return;
    }
    showChatEmpty(suggestionsEmpty());
    bindSuggestionButtons();
    loadChatHistory(sid, token);
  }

  async function loadChatHistory(sid, token) {
    const res = await call('agent_history', sid);
    if (token !== state.chat.loadToken || state.chat.sessionId !== sid) return;
    if (!res || !res.ok) {
      showChatEmpty(suggestionsEmpty());
      bindSuggestionButtons();
      return;
    }
    const messages = res.messages || [];
    const chat = chatContainer();
    chat.innerHTML = '';
    state.chat.toolCards = new Map();
    if (!messages.length) {
      showChatEmpty(suggestionsEmpty());
      bindSuggestionButtons();
    } else {
      messages.forEach((entry) => appendHistoryEntry(entry, false));
      scrollChat(true);
    }
    setChatBusy(Boolean(res.busy));
  }

  function appendHistoryEntry(entry, scroll = true) {
    const chat = chatContainer();
    if (entry.role === 'user') {
      appendUserMessage(entry.text || '');
    } else if (entry.role === 'assistant') {
      if (entry.text && entry.text.trim()) appendAssistantMessage(entry.text);
    } else if (entry.role === 'tool') {
      const card = buildToolCard(entry);
      chat.appendChild(card);
    }
    if (scroll) scrollChat(false);
  }

  function clearChatEmpty() {
    const empty = chatContainer().querySelector('.chat-empty');
    if (empty) empty.remove();
  }

  function appendUserMessage(text) {
    clearChatEmpty();
    const chat = chatContainer();
    const wrapper = el('div', 'msg msg-user');
    const bubble = el('div', 'bubble', text);
    wrapper.appendChild(bubble);
    chat.appendChild(wrapper);
    scrollChat(true);
    return wrapper;
  }

  function appendAssistantMessage(text) {
    clearChatEmpty();
    const chat = chatContainer();
    const wrapper = el('div', 'msg msg-assistant');
    const avatar = el('div', 'assistant-avatar', '✦');
    const body = el('div', 'assistant-body');
    body.innerHTML = renderMarkdown(text);
    wrapper.append(avatar, body);
    chat.appendChild(wrapper);
    scrollChat(false);
    return body;
  }

  function appendNotice(text, type = '') {
    clearChatEmpty();
    const chat = chatContainer();
    const node = el('div', `msg-notice ${type}`, text);
    chat.appendChild(node);
    scrollChat(false);
  }

  function setChatBusy(busy) {
    state.chat.busy = busy;
    updateComposerState();
    if (!busy) {
      finalizeStreaming();
      const typing = $('#chat .typing-indicator');
      if (typing) typing.remove();
      scrollChat(true);
    } else if (!state.chat.streamEl) {
      ensureTypingIndicator();
    }
    updateComposerState();
  }

  function ensureTypingIndicator() {
    if ($('#chat .typing-indicator')) return;
    const chat = chatContainer();
    const wrapper = el('div', 'msg msg-assistant typing-indicator');
    const avatar = el('div', 'assistant-avatar', '✦');
    const body = el('div', 'assistant-body');
    body.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
    wrapper.append(avatar, body);
    chat.appendChild(wrapper);
    scrollChat(true);
  }

  function removeTypingIndicator() {
    const node = $('#chat .typing-indicator');
    if (node) node.remove();
  }

  function ensureStreamEl() {
    if (state.chat.streamEl) return state.chat.streamEl;
    const chat = chatContainer();
    const wrapper = el('div', 'msg msg-assistant streaming');
    const avatar = el('div', 'assistant-avatar', '✦');
    const body = el('div', 'assistant-body');
    wrapper.append(avatar, body);
    chat.appendChild(wrapper);
    state.chat.streamEl = body;
    state.chat.streamText = '';
    return body;
  }

  function onAssistantDelta(text) {
    removeTypingIndicator();
    const body = ensureStreamEl();
    state.chat.streamText += text;
    if (!state.chat.renderTimer) {
      state.chat.renderTimer = setTimeout(() => {
        state.chat.renderTimer = null;
        if (!state.chat.streamEl) return;
        state.chat.streamEl.innerHTML = renderMarkdown(state.chat.streamText) || '<span class="typing"><i></i><i></i><i></i></span>';
        scrollChat(false);
      }, 80);
    }
  }

  function finalizeStreaming() {
    if (!state.chat.streamEl) return;
    if (state.chat.renderTimer) {
      clearTimeout(state.chat.renderTimer);
      state.chat.renderTimer = null;
    }
    const text = state.chat.streamText;
    if (text.trim()) {
      state.chat.streamEl.innerHTML = renderMarkdown(text);
    } else {
      const wrapper = state.chat.streamEl.parentElement;
      if (wrapper && !wrapper.classList.contains('typing-indicator')) wrapper.remove();
    }
    state.chat.streamEl = null;
    state.chat.streamText = '';
  }

  /* ------------------------------------------------------------ markdown */
  function inlineMarkdown(text) {
    if (!text) return '';
    const codes = [];
    let out = text.replace(/`([^`\n]+)`/g, (match, code) => {
      codes.push(code);
      return `\u0000C${codes.length - 1}\u0000`;
    });
    out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>'
    );
    codes.forEach((code, index) => {
      out = out.split(`\u0000C${index}\u0000`).join(`<code>${code}</code>`);
    });
    return out;
  }

  function splitTableRow(line) {
    let value = String(line || '').trim();
    if (value.startsWith('|')) value = value.slice(1);
    if (value.endsWith('|')) value = value.slice(0, -1);
    const cells = [];
    let current = '';
    for (let index = 0; index < value.length; index += 1) {
      const char = value[index];
      if (char === '\\' && value[index + 1] === '|') {
        current += '|';
        index += 1;
        continue;
      }
      if (char === '|') {
        cells.push(current.trim());
        current = '';
        continue;
      }
      current += char;
    }
    cells.push(current.trim());
    return cells;
  }

  function isTableSeparator(line) {
    if (!line || !line.includes('-')) return false;
    const cells = splitTableRow(line);
    if (!cells.length) return false;
    return cells.every((cell) => /^:?-{2,}:?$/.test(cell.replace(/\s/g, '')));
  }

  function tableAlignment(cell) {
    const value = String(cell || '').replace(/\s/g, '');
    const left = value.startsWith(':');
    const right = value.endsWith(':');
    if (left && right) return 'center';
    if (right) return 'right';
    return 'left';
  }

  function renderMarkdown(source) {
    if (!source) return '';
    let text = escapeHtml(String(source).replace(/\r\n?/g, '\n'));
    const codeBlocks = [];
    text = text.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, (match, lang, code) => {
      codeBlocks.push(code.replace(/\n$/, ''));
      return `\n\u0000B${codeBlocks.length - 1}\u0000\n`;
    });

    const lines = text.split('\n');
    let html = '';
    let paragraph = [];
    let listType = '';
    let listItems = [];
    let table = null;

    const flushParagraph = () => {
      if (!paragraph.length) return;
      html += `<p>${inlineMarkdown(paragraph.join('<br>'))}</p>`;
      paragraph = [];
    };
    const flushList = () => {
      if (!listType) return;
      const items = listItems.map((item) => `<li>${inlineMarkdown(item)}</li>`).join('');
      html += `<${listType}>${items}</${listType}>`;
      listType = '';
      listItems = [];
    };
    const flushTable = () => {
      if (!table) return;
      const { header, aligns, rows } = table;
      const columns = header.length;
      html += '<div class="md-table-wrap"><table><thead><tr>';
      header.forEach((cell, index) => {
        html += `<th style="text-align:${aligns[index] || 'left'}">${inlineMarkdown(cell)}</th>`;
      });
      html += '</tr></thead><tbody>';
      rows.forEach((row) => {
        html += '<tr>';
        for (let index = 0; index < columns; index += 1) {
          html += `<td style="text-align:${aligns[index] || 'left'}">${inlineMarkdown(row[index] || '')}</td>`;
        }
        html += '</tr>';
      });
      html += '</tbody></table></div>';
      table = null;
    };
    const flushAll = () => {
      flushParagraph();
      flushList();
      flushTable();
    };

    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index].trim();
      const codeMatch = line.match(/^\u0000B(\d+)\u0000$/);
      if (codeMatch) {
        flushAll();
        html += `<pre><code>${codeBlocks[Number(codeMatch[1])] || ''}</code></pre>`;
        continue;
      }
      if (!line) {
        flushAll();
        continue;
      }

      if (line.includes('|') && index + 1 < lines.length && isTableSeparator(lines[index + 1].trim())) {
        flushParagraph();
        flushList();
        const header = splitTableRow(line);
        const aligns = splitTableRow(lines[index + 1].trim()).map(tableAlignment);
        const rows = [];
        index += 2;
        while (index < lines.length) {
          const rowLine = lines[index].trim();
          if (!rowLine || !rowLine.includes('|') || isTableSeparator(rowLine)) break;
          if (/^\u0000B\d+\u0000$/.test(rowLine)) break;
          rows.push(splitTableRow(rowLine));
          index += 1;
        }
        index -= 1;
        table = { header, aligns, rows };
        flushTable();
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        flushAll();
        html += `<div class="md-h">${inlineMarkdown(heading[2])}</div>`;
        continue;
      }

      if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) {
        flushAll();
        html += '<hr>';
        continue;
      }

      const bullet = line.match(/^[-*+]\s+(.+)$/);
      const ordered = line.match(/^\d+[.)]\s+(.+)$/);
      if (bullet || ordered) {
        flushParagraph();
        flushTable();
        const type = bullet ? 'ul' : 'ol';
        if (listType && listType !== type) flushList();
        listType = type;
        listItems.push((bullet || ordered)[1]);
        continue;
      }

      flushList();
      flushTable();
      paragraph.push(line);
    }

    flushAll();
    return html;
  }

  // Exposed for diagnostics / UI tests.
  window.__yhRenderMarkdown = renderMarkdown;

  /* ----------------------------------------------------------- tool cards */
  const TOOL_STATUS_TEXT = {
    pending: '待确认',
    running: '执行中',
    done: '完成',
    error: '失败',
    timeout: '超时',
    skipped: '已跳过',
  };

  function safetyReasonText(data) {
    if (data.dangerous) return '命中危险命令规则，必须人工确认';
    // Only "仅安全命令自动执行" needs to justify itself; in "每条命令确认"
    // mode nothing runs automatically, so the hint would be noise.
    if (data.safe === false && data.approval === 'safe') return data.safeReason || '不在只读命令白名单';
    return '';
  }

  function buildToolCard(data) {
    const card = el('div', 'tool-card');
    card.dataset.callId = data.callId || '';
    if (data.dangerous) card.classList.add('danger');

    const head = el('div', 'tool-head');
    const title = el('div', 'tool-title');
    const icon = el('span', 'tool-icon', '$_');
    const titleText = el('span', '', data.dangerous ? '危险命令' : '远程命令');
    title.append(icon, titleText);
    const chips = el('div', 'tool-head-chips');
    if (data.dangerous) chips.appendChild(el('span', 'chip danger', '⚠ 危险'));
    const chip = el('span', 'chip', '待确认');
    chips.appendChild(chip);
    head.append(title, chips);
    card.appendChild(head);

    if (data.explanation) {
      card.appendChild(el('div', 'tool-explanation', data.explanation));
    }

    const safetyNote = el('div', 'tool-safety-note hidden');
    card.appendChild(safetyNote);

    const command = el('div', 'tool-command', data.command || '');
    card.appendChild(command);

    const editor = el('textarea', 'tool-command-edit hidden');
    editor.value = data.command || '';
    editor.spellcheck = false;
    card.appendChild(editor);

    const actions = el('div', 'tool-actions');
    card.appendChild(actions);

    const outputBox = el('div', 'tool-output hidden');
    const outputHead = el('div', 'tool-output-head');
    const outputLabel = el('span', '', '输出');
    const outputToggle = el('button', 'ghost-btn', '展开');
    outputHead.append(outputLabel, outputToggle);
    const pre = el('pre', '');
    outputBox.append(outputHead, pre);
    card.appendChild(outputBox);

    outputToggle.addEventListener('click', () => {
      outputBox.classList.toggle('expanded');
      outputToggle.textContent = outputBox.classList.contains('expanded') ? '收起' : '展开';
    });

    const view = {
      card,
      chip,
      safetyNote,
      commandEl: command,
      editor,
      actions,
      outputBox,
      outputLabel,
      pre,
      data: { ...data },
      status: data.status || 'pending',
    };
    state.chat.toolCards.set(data.callId, view);
    renderToolCard(view);
    return card;
  }

  function renderToolCard(view) {
    const { card, chip, actions, data } = view;
    const status = view.status;
    chip.className = `chip ${status}`;
    chip.textContent = TOOL_STATUS_TEXT[status] || status;

    // Explain why the command was not auto-approved; only meaningful while the
    // user can still act on it (pending) or has skipped it.
    const reason = status === 'pending' || status === 'skipped' ? safetyReasonText(data) : '';
    view.safetyNote.textContent = reason ? `未自动执行：${reason}` : '';
    view.safetyNote.classList.toggle('hidden', !reason);
    actions.innerHTML = '';
    view.editor.classList.add('hidden');

    if (status === 'pending') {
      view.commandEl.classList.remove('hidden');
      const run = el('button', 'btn small primary', '执行');
      const edit = el('button', 'btn small ghost', '编辑');
      const skip = el('button', 'btn small ghost', '跳过');
      run.addEventListener('click', () => resolveTool(view, 'run', data.command));
      skip.addEventListener('click', () => resolveTool(view, 'skip', ''));
      edit.addEventListener('click', () => {
        view.commandEl.classList.add('hidden');
        view.editor.classList.remove('hidden');
        view.editor.value = view.data.command || '';
        actions.innerHTML = '';
        const execute = el('button', 'btn small primary', '执行');
        const cancel = el('button', 'btn small ghost', '取消');
        execute.addEventListener('click', () => {
          const edited = view.editor.value.trim();
          if (!edited) {
            toast('命令不能为空', 'error');
            return;
          }
          view.data.command = edited;
          view.commandEl.textContent = edited;
          resolveTool(view, 'run', edited);
        });
        cancel.addEventListener('click', () => {
          view.editor.classList.add('hidden');
          view.commandEl.classList.remove('hidden');
          renderToolCard(view);
        });
        actions.append(execute, cancel);
        view.editor.focus();
        view.editor.select();
      });
      actions.append(run, edit, skip);
    } else if (status === 'running') {
      const stop = el('span', 'field-hint', '正在远端执行…');
      actions.appendChild(stop);
    } else if (status === 'done' || status === 'error' || status === 'timeout') {
      const copy = el('button', 'btn small ghost', '复制输出');
      copy.addEventListener('click', () => {
        navigator.clipboard.writeText(view.pre.textContent || '').then(() => toast('已复制输出', 'success', 1200)).catch(() => {});
      });
      actions.appendChild(copy);
    }

    if (data.output !== undefined && data.output !== null && status !== 'pending' && status !== 'running' && status !== 'skipped') {
      view.outputBox.classList.remove('hidden');
      view.pre.textContent = data.output || '（无输出）';
      if (typeof data.exitCode === 'number') {
        view.outputLabel.textContent = `输出 · 退出码 ${data.exitCode}`;
        view.outputLabel.style.color = data.exitCode === 0 ? 'var(--green)' : 'var(--red)';
      } else {
        view.outputLabel.textContent = '输出';
        view.outputLabel.style.color = '';
      }
    } else {
      view.outputBox.classList.add('hidden');
    }
  }

  async function resolveTool(view, action, command) {
    const sid = state.chat.sessionId;
    if (!sid) return;
    view.status = action === 'skip' ? 'skipped' : 'running';
    renderToolCard(view);
    const res = await call('agent_resolve', sid, view.data.callId, action, command);
    if (!res.ok) {
      view.status = 'pending';
      renderToolCard(view);
      toast(res.error || '操作失败', 'error');
    }
  }

  function handleToolEvent(event) {
    if (event.session !== state.chat.sessionId) return;
    const call = event.call || {};
    let view = state.chat.toolCards.get(call.callId);
    if (!view) {
      // A tool call ends the preceding assistant segment. Keep later deltas
      // below the card instead of appending conclusions above the command.
      finalizeStreaming();
      clearChatEmpty();
      const card = buildToolCard({
        callId: call.callId,
        command: call.command,
        explanation: call.explanation,
        dangerous: call.dangerous,
        safe: call.safe,
        safeReason: call.safeReason,
        approval: call.approval,
        status: 'pending',
      });
      chatContainer().appendChild(card);
      view = state.chat.toolCards.get(call.callId);
      scrollChat(false);
    } else {
      view.data.command = call.command || view.data.command;
      if (call.explanation) view.data.explanation = call.explanation;
      view.commandEl.textContent = view.data.command;
      let refresh = false;
      if (typeof call.safe === 'boolean' && view.data.safe !== call.safe) {
        view.data.safe = call.safe;
        refresh = true;
      }
      if (call.safeReason !== undefined && view.data.safeReason !== call.safeReason) {
        view.data.safeReason = call.safeReason;
        refresh = true;
      }
      if (call.approval !== undefined && view.data.approval !== call.approval) {
        view.data.approval = call.approval;
        refresh = true;
      }
      // Re-render only when the safety hint changed, so an open editor or a
      // repeated event for a finished card keeps its current DOM state.
      if (refresh) renderToolCard(view);
    }
    if (view && call.dangerous && !view.card.classList.contains('danger')) {
      view.card.classList.add('danger');
      const chips = view.card.querySelector('.tool-head-chips');
      if (chips && !chips.querySelector('.chip.danger')) chips.insertBefore(el('span', 'chip danger', '⚠ 危险'), chips.firstChild);
    }
    if (view) view.status = view.status === 'pending' ? 'pending' : view.status;
  }

  function setAgentRunning(sid, running) {
    const target = sid ? state.sessions.get(sid) : null;
    if (!target) return;
    // The agent types into this very PTY, so keep the keyboard out of the way
    // while a command is in flight.
    if (target.term) {
      try { target.term.options.disableStdin = Boolean(running); } catch (_) { /* older xterm */ }
    }
    if (target.el) target.el.classList.toggle('agent-running', Boolean(running));
  }

  function handleAgentEvent(event) {
    if (!event || !event.type) return;
    if (event.type === 'tool_update') {
      setAgentRunning(event.session, event.status === 'running');
    } else if (event.type === 'busy' && !event.busy) {
      setAgentRunning(event.session, false);
    }
    // While browsing an archived conversation, live events of the active
    // session must not leak into the viewed transcript.
    if (state.chat.viewing) return;
    const isActive = event.session && event.session === state.chat.sessionId;
    if (event.type === 'busy') {
      if (isActive) {
        setChatBusy(Boolean(event.busy));
        if (event.busy) removeTypingIndicator();
        if (event.busy) ensureTypingIndicator();
      }
      return;
    }
    if (!isActive) return;
    if (event.type === 'delta') {
      onAssistantDelta(event.text || '');
      return;
    }
    if (event.type === 'tool') {
      removeTypingIndicator();
      handleToolEvent(event);
      return;
    }
    if (event.type === 'tool_update') {
      const view = state.chat.toolCards.get(event.callId);
      if (!view) return;
      view.status = event.status;
      view.data.output = event.output;
      view.data.exitCode = event.exitCode;
      renderToolCard(view);
      scrollChat(false);
      return;
    }
    if (event.type === 'error') {
      removeTypingIndicator();
      appendNotice(event.message || 'AI 请求失败', 'error');
      scrollChat(true);
      return;
    }
    if (event.type === 'notice') {
      appendNotice(event.message || '');
      scrollChat(true);
      return;
    }
    if (event.type === 'cancelled') {
      removeTypingIndicator();
      finalizeStreaming();
      appendNotice('已停止本轮回复');
      return;
    }
  }

  /* ------------------------------------------------------------- composer */
  function autoGrowInput() {
    const input = $('#ai-input');
    input.style.height = 'auto';
    input.style.height = Math.min(140, input.scrollHeight) + 'px';
  }

  function renderModelSelect() {
    const select = $('#ai-model-select');
    if (!select) return;
    select.innerHTML = '';
    if (!state.models.length) {
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = '未配置模型（请到设置添加供应商）';
      placeholder.selected = true;
      placeholder.disabled = true;
      select.appendChild(placeholder);
      select.disabled = true;
      select.title = '还没有可用模型，先到「设置 → AI 模型」添加供应商';
      return;
    }
    select.disabled = false;
    state.providers.forEach((provider) => {
      if (!provider.models.length) return;
      const group = document.createElement('optgroup');
      group.label = provider.name;
      provider.models.forEach((modelId) => {
        const ref = `${provider.id}::${modelId}`;
        const option = document.createElement('option');
        option.value = ref;
        option.textContent = modelId;
        option.title = `${provider.name} · ${modelId}`;
        group.appendChild(option);
      });
      select.appendChild(group);
    });
    select.value = state.activeModelId;
    const active = activeModel();
    select.title = active ? `${active.providerName} · ${active.model}` : '选择用于对话的模型';
  }

  function updateComposerState() {
    const session = activeSession();
    const connected = session && session.status === 'connected';
    const busy = state.chat.busy;
    const viewing = state.chat.viewing;
    if (viewing) {
      $('#btn-ai-send').classList.add('hidden');
      $('#btn-ai-stop').classList.add('hidden');
      $('#ai-input').disabled = true;
      $('#composer-hint').textContent = '正在查看历史记录 · 可点上方「继续此对话」';
      return;
    }
    $('#btn-ai-send').classList.toggle('hidden', busy);
    $('#btn-ai-stop').classList.toggle('hidden', !busy);
    $('#ai-input').disabled = !connected;
    if (!connected) {
      $('#composer-hint').textContent = session ? '会话已断开，无法执行命令' : '未连接会话';
    } else {
      updateCompositeHint();
    }
  }

  function activeModel() {
    return state.models.find((model) => model.id === state.activeModelId) || state.models[0] || null;
  }

  async function sendAgentMessage() {
    const input = $('#ai-input');
    const text = input.value.trim();
    if (!text) return;
    if (state.chat.viewing) {
      toast('正在查看历史记录，请点上方「继续此对话」或退出查看');
      return;
    }
    const session = activeSession();
    if (!session) {
      toast('请先连接一台服务器', 'error');
      return;
    }
    if (session.status !== 'connected') {
      toast('会话未连接，无法执行命令', 'error');
      return;
    }
    if (!activeModel()) {
      toast('还没有可用模型：请在「设置 → AI 模型」获取并勾选模型', 'error', 6000);
      return;
    }
    if (state.chat.busy) {
      toast('AI 正在回复中，请稍候或点击停止');
      return;
    }
    const bubble = appendUserMessage(text);
    input.value = '';
    autoGrowInput();
    setChatBusy(true);
    const res = await call('agent_send', session.id, text);
    if (!res.ok) {
      setChatBusy(false);
      if (bubble && bubble.parentElement) bubble.remove();
      input.value = text;
      autoGrowInput();
      appendNotice(res.error || '发送失败', 'error');
      toast(res.error || '发送失败', 'error');
    }
  }

  async function stopAgent() {
    const session = activeSession();
    if (!session) return;
    await call('agent_cancel', session.id);
  }

  async function clearChat() {
    const session = activeSession();
    if (!session) return;
    if (state.chat.viewing) {
      toast('正在查看历史记录，请先退出查看');
      return;
    }
    const ok = await confirmDialog('新对话', '开始新对话？当前对话已自动保存在本地历史记录中。', '开始新对话');
    if (!ok) return;
    await call('agent_clear', session.id);
    state.chat.sessionId = null;
    setChatSession(session.id);
    toast('已开始新对话', 'success', 1800);
  }

  /* ------------------------------------------------------- history archive */
  function archiveHostLabel(record) {
    if (!record) return '未知主机';
    if (record.hostName) return record.hostName;
    if (record.username && record.host) return `${record.username}@${record.host}`;
    return record.host || '未知主机';
  }

  function formatArchiveTime(seconds) {
    if (!seconds) return '';
    try {
      return new Date(Number(seconds) * 1000).toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_) {
      return '';
    }
  }

  function formatSyncAge(seconds) {
    const stamp = Number(seconds) || 0;
    if (!stamp) return '尚未同步';
    const diff = Math.max(0, Math.floor(Date.now() / 1000) - stamp);
    if (diff < 90) return '刚刚同步';
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前同步`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前同步`;
    return `${Math.floor(diff / 86400)} 天前同步`;
  }

  function hostMatchesRecord(record, info) {
    if (!record || !info) return false;
    return (
      String(record.host || '') === String(info.host || '') &&
      Number(record.port || 0) === Number(info.port || 0) &&
      String(record.username || '') === String(info.username || '')
    );
  }

  function showChatBanner(record) {
    const banner = $('#chat-banner');
    if (!banner) return;
    banner.classList.remove('hidden');
    const stamp = formatArchiveTime(record.updatedAt);
    const label = `历史记录 · ${archiveHostLabel(record)}${stamp ? ' · ' + stamp : ''}`;
    $('#chat-banner-label').textContent = label;
    $('#chat-banner-label').title = label;
    const session = activeSession();
    const canContinue = Boolean(session && session.status === 'connected' && hostMatchesRecord(record, session.info));
    const button = $('#btn-banner-continue');
    button.disabled = !canContinue;
    button.title = canContinue ? '载入该对话并继续提问' : '连接同一台主机后才能继续此对话';
  }

  function hideChatBanner() {
    const banner = $('#chat-banner');
    if (banner) banner.classList.add('hidden');
  }

  async function openHistory() {
    openModal('history-modal');
    await refreshHistoryList();
  }

  async function refreshHistoryList() {
    const list = $('#history-list');
    if (!list) return;
    list.innerHTML = '';
    const loading = el('div', 'history-empty', '正在读取本地历史…');
    list.appendChild(loading);
    const res = await call('conversation_list');
    if (!res.ok) {
      list.innerHTML = '';
      list.appendChild(el('div', 'history-empty', res.error || '读取历史对话失败'));
      return;
    }
    const records = res.conversations || [];
    list.innerHTML = '';
    if (!records.length) {
      list.appendChild(
        el(
          'div',
          'history-empty',
          '还没有历史对话。\n和 AI 对话后会自动保存在本地，下次启动仍可查看或继续。'
        )
      );
      return;
    }
    records.forEach((record) => {
      const row = el('div', 'history-row');
      const main = el('div', 'history-row-main');
      main.appendChild(el('div', 'history-row-title', record.title || '未命名对话'));
      const meta = [archiveHostLabel(record)];
      if (record.model) meta.push(record.model);
      const stamp = formatArchiveTime(record.updatedAt);
      if (stamp) meta.push(stamp);
      meta.push(`${record.messageCount || 0} 条消息`);
      main.appendChild(el('div', 'history-row-meta', meta.join(' · ')));
      row.appendChild(main);

      const actions = el('div', 'history-row-actions');
      const viewButton = el('button', 'btn small ghost', '查看');
      viewButton.addEventListener('click', () => viewConversation(record));
      const continueButton = el('button', 'btn small primary', '继续');
      const session = activeSession();
      const canContinue = Boolean(session && session.status === 'connected' && hostMatchesRecord(record, session.info));
      continueButton.disabled = !canContinue;
      continueButton.title = canContinue ? '载入该对话并继续提问' : '连接同一台主机后才能继续';
      continueButton.addEventListener('click', () => continueConversation(record));
      const exportButton = el('button', 'btn small ghost', '导出');
      exportButton.title = '导出为 Markdown 到本地存储目录';
      exportButton.addEventListener('click', () => exportConversation(record));
      const deleteButton = el('button', 'btn small ghost', '删除');
      deleteButton.addEventListener('click', () => deleteConversation(record));
      actions.append(viewButton, continueButton, exportButton, deleteButton);
      row.appendChild(actions);
      list.appendChild(row);
    });
  }

  async function viewConversation(record) {
    const res = await call('conversation_get', record.id);
    if (!res.ok) {
      toast(res.error || '读取历史对话失败', 'error');
      return;
    }
    closeModal('history-modal');
    enterViewing(res.conversation || record, res.entries || []);
  }

  function enterViewing(record, entries) {
    state.chat.viewing = record;
    state.chat.busy = false;
    state.chat.streamEl = null;
    state.chat.streamText = '';
    state.chat.toolCards = new Map();
    state.chat.loadToken += 1;
    const chat = chatContainer();
    chat.innerHTML = '';
    if (!entries.length) {
      appendNotice('这条历史对话没有可显示的内容');
    } else {
      entries.forEach((entry) => appendHistoryEntry(entry, false));
      scrollChat(true);
    }
    showChatBanner(record);
    updateComposerState();
  }

  function exitViewing() {
    if (!state.chat.viewing) return;
    state.chat.viewing = null;
    hideChatBanner();
    const session = activeSession();
    state.chat.sessionId = null;
    if (session) {
      setChatSession(session.id);
    } else {
      showChatEmpty(noSessionEmpty());
      bindSuggestionButtons();
      updateComposerState();
    }
  }

  async function continueConversation(record) {
    const session = activeSession();
    if (!session) {
      toast('请先连接服务器', 'error');
      return;
    }
    if (session.status !== 'connected') {
      toast('会话未连接，无法继续历史对话', 'error');
      return;
    }
    if (!hostMatchesRecord(record, session.info)) {
      toast('该对话属于另一台主机，请先连接对应服务器', 'error');
      return;
    }
    const res = await call('agent_load', session.id, record.id);
    if (!res.ok) {
      toast(res.error || '载入历史对话失败', 'error');
      return;
    }
    closeModal('history-modal');
    state.chat.viewing = null;
    hideChatBanner();
    state.chat.sessionId = null;
    setChatSession(session.id);
    toast('已载入历史对话，可以继续提问', 'success');
  }

  async function exportConversation(record) {
    const res = await call('conversation_export', record.id);
    if (!res.ok) {
      toast(res.error || '导出失败', 'error');
      return;
    }
    toast(`已导出 Markdown：${res.path}`, 'success', 7000);
  }

  async function deleteConversation(record) {
    const ok = await confirmDialog('删除历史对话', `确定删除「${record.title || '未命名对话'}」吗？此操作不可恢复。`, '删除');
    if (!ok) return;
    const res = await call('conversation_delete', record.id);
    if (!res.ok) {
      toast(res.error || '删除失败', 'error');
      return;
    }
    if (state.chat.viewing && state.chat.viewing.id === record.id) {
      exitViewing();
    }
    toast('历史对话已删除', 'success', 1800);
    refreshHistoryList();
  }

  /* ---------------------------------------------------------------- modals */
  const modalStack = [];
  const modalReturnFocus = new Map();

  function topModal() {
    return [...modalStack].reverse().find((node) => !node.classList.contains('hidden')) || null;
  }

  function modalFocusables(node) {
    return Array.from(node.querySelectorAll(
      'input:not([type=hidden]), select, textarea, button, a[href], [tabindex]'
    )).filter((item) => !item.disabled && item.tabIndex >= 0 && item.getClientRects().length);
  }

  function focusModal(node) {
    // Confirmations default to the safe action, never to disconnect/delete.
    const items = modalFocusables(node);
    const target = (node.id === 'confirm-modal' && $('#confirm-cancel'))
      || items.find((item) => item.matches('input, select, textarea')) || items[0];
    if (target) target.focus();
    else {
      node.tabIndex = -1;
      node.focus();
    }
  }

  function openModal(id) {
    const node = document.getElementById(id);
    if (!node) return;
    if (node.classList.contains('hidden')) modalReturnFocus.set(node, document.activeElement);
    const index = modalStack.indexOf(node);
    if (index !== -1) modalStack.splice(index, 1);
    modalStack.push(node);
    modalStack.forEach((modal, depth) => { modal.style.zIndex = String(50 + depth); });
    node.classList.remove('hidden');
    // Synchronous focus prevents keys leaking to xterm during a delayed focus timer.
    focusModal(node);
  }

  function closeModal(id) {
    const node = document.getElementById(id);
    if (!node || node.classList.contains('hidden')) return;
    const wasTop = topModal() === node;
    const previous = modalReturnFocus.get(node);
    node.classList.add('hidden');
    modalReturnFocus.delete(node);
    const index = modalStack.indexOf(node);
    if (index !== -1) modalStack.splice(index, 1);
    node.style.removeProperty('z-index');
    modalStack.forEach((modal, depth) => { modal.style.zIndex = String(50 + depth); });
    if (!wasTop) return;
    const top = topModal();
    if (previous?.isConnected && previous.getClientRects().length
        && (!top || top.contains(previous))) previous.focus();
    else if (top) focusModal(top);
    else activeSession()?.term?.focus();
  }

  function closeTopModal() {
    const top = topModal();
    if (!top) return false;
    if (top.id === 'confirm-modal') settleConfirm(false);
    else closeModal(top.id);
    return true;
  }

  let confirmResolve = null;
  function confirmDialog(title, text, okText = '确定') {
    $('#confirm-title').textContent = title;
    $('#confirm-text').textContent = text;
    $('#confirm-ok').textContent = okText;
    openModal('confirm-modal');
    return new Promise((resolve) => {
      confirmResolve = resolve;
    });
  }

  function settleConfirm(value) {
    closeModal('confirm-modal');
    if (confirmResolve) {
      const resolve = confirmResolve;
      confirmResolve = null;
      resolve(value);
    }
  }

  /* ---------------------------------------------------------------- host UI */
  function setHostAuth(auth) {
    $$('#host-auth button').forEach((button) => {
      button.classList.toggle('active', button.dataset.value === auth);
    });
    $$('#host-form .auth-panel').forEach((panel) => {
      panel.classList.toggle('hidden', panel.dataset.auth !== auth);
    });
  }

  function openHostModal(host) {
    $('#host-form').reset();
    $('#host-error').classList.add('hidden');
    $('#host-id').value = '';
    $('#host-port').value = '22';
    $('#host-save').checked = true;
    $('#host-save-line').classList.remove('hidden');
    $('#host-password-hint').textContent = '';
    $('#host-modal-title').textContent = '新建连接';
    $('#host-connect').textContent = '连接';
    setHostAuth('password');
    if (host) {
      $('#host-modal-title').textContent = '编辑连接';
      $('#host-id').value = host.id;
      $('#host-name').value = host.name || '';
      $('#host-address').value = host.host || '';
      $('#host-port').value = host.port || 22;
      $('#host-user').value = host.username || '';
      $('#host-keypath').value = host.keyPath || '';
      setHostAuth(host.auth || 'password');
      $('#host-connect').textContent = '保存并连接';
      $('#host-save-line').classList.add('hidden');
      $('#host-password-hint').textContent = host.hasPassword ? '已保存密码，留空则保持不变' : '';
      if (host.hasPassphrase) $('#host-passphrase').placeholder = '已保存口令，留空则保持不变';
    }
    openModal('host-modal');
  }

  async function submitHostForm(event) {
    event.preventDefault();
    const error = $('#host-error');
    error.classList.add('hidden');
    const hostId = $('#host-id').value;
    const activeAuth = $('#host-auth button.active');
    const auth = activeAuth ? activeAuth.dataset.value : 'password';
    const payload = {
      id: hostId || undefined,
      name: $('#host-name').value.trim(),
      host: $('#host-address').value.trim(),
      port: Number($('#host-port').value || 22),
      username: $('#host-user').value.trim(),
      auth,
      password: $('#host-password').value,
      keyPath: $('#host-keypath').value.trim(),
      passphrase: $('#host-passphrase').value,
    };
    if (!payload.host || !payload.username) {
      error.textContent = '请填写主机地址和用户名';
      error.classList.remove('hidden');
      return;
    }
    if (!hostId && auth === 'password' && !payload.password) {
      error.textContent = '请填写登录密码（或选择私钥 / SSH Agent 认证）';
      error.classList.remove('hidden');
      return;
    }
    if (auth === 'key' && !payload.keyPath) {
      error.textContent = '请选择私钥文件';
      error.classList.remove('hidden');
      return;
    }
    const button = $('#host-connect');
    button.disabled = true;
    button.textContent = '连接中…';
    let res;
    if (hostId) {
      const saved = await call('save_host', payload);
      if (!saved.ok) {
        error.textContent = saved.error || '保存失败';
        error.classList.remove('hidden');
        button.disabled = false;
        button.textContent = '保存并连接';
        return;
      }
      if (saved.hosts) {
        state.hosts = saved.hosts;
        renderHosts();
      }
      res = await call('connect', { hostId, cols: 120, rows: 32 });
    } else {
      res = await call('connect', {
        profile: payload,
        save: $('#host-save').checked,
        cols: 120,
        rows: 32,
      });
    }
    button.disabled = false;
    button.textContent = hostId ? '保存并连接' : '连接';
    if (!res.ok) {
      error.textContent = res.error || '连接失败';
      error.classList.remove('hidden');
      return;
    }
    closeModal('host-modal');
    if (res.hosts) {
      state.hosts = res.hosts;
      renderHosts();
    }
    registerSession(res.session);
    setView('terminal');
  }

  /* --------------------------------------------------------------- updates */
  let updateRequestPending = false;
  let updateActionPending = false;
  let updateSavePending = false;
  let updateWatchdog = null;
  let updateRevision = 0;
  let updateToast = null;

  // A lost bridge response must not leave any update control disabled forever.
  async function updateCall(method, ...args) {
    let timer;
    try {
      return await Promise.race([
        call(method, ...args),
        new Promise((resolve) => {
          timer = setTimeout(() => resolve({ ok: false, error: '请求超时，请重试' }), 15000);
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }

  function updateTime(value) {
    if (!value) return '';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function hideUpdateNotification() {
    if (updateToast) updateToast.remove();
    updateToast = null;
  }

  function renderUpdateState() {
    const update = state.update || {};
    const status = update.status || '';
    const labels = {
      checking: '正在检查更新…', available: '发现新版本', latest: '当前已是最新版本',
      ahead: '当前版本比最新发布版本更新', no_release: '尚无可用的正式发布版本',
      error: '检查更新失败，请重试', skipped: update.reason ? '已跳过自动检查，可手动检查' : '尚未检查更新',
      busy: '已有更新检查正在进行，请稍候重试',
    };
    $('#update-status').textContent = status === 'skipped' && update.reason === 'server_cooldown'
      ? 'GitHub 请求已限流，请稍后重试'
      : (labels[status] || '尚未检查更新');
    $('#update-error').textContent = update.error || '';
    $('#update-error').classList.toggle('hidden', !update.error);
    $('#update-last-success').textContent = '上次成功检查：' + (updateTime(update.lastSuccessAt) || '尚无记录');
    $('#set-update-auto').checked = state.settings.updateAutoCheck !== false;
    $('#set-update-auto').disabled = updateSavePending;
    const checking = status === 'checking';
    $('#btn-check-updates').disabled = updateRequestPending || checking;
    $('#btn-check-updates').textContent = updateRequestPending || checking ? '检查中…' : '检查更新';
    const release = update.release;
    $('#update-release').classList.toggle('hidden', !release);
    if (release) {
      $('#update-release-version').textContent = '最新正式版 ' + (release.version || update.latestVersion || '');
      $('#update-published').textContent = release.publishedAt ? '发布时间：' + updateTime(release.publishedAt) : '';
      // Release notes are untrusted remote text, never markdown / HTML.
      $('#update-notes').textContent = release.notes || '此版本暂无发布说明。';
      $('#update-ignored').classList.toggle('hidden', !update.ignored);
    }
    $('#btn-update-download').disabled = !release || updateActionPending || checking;
    $('#btn-update-ignore').classList.toggle('hidden', status !== 'available');
    $('#btn-update-ignore').disabled = !release || updateActionPending || checking || Boolean(update.ignored);
    $('#btn-update-ignore').textContent = update.ignored ? '已忽略此版本' : '忽略此版本';
    if (state.settings.updateAutoCheck === false || update.ignored) hideUpdateNotification();
  }

  function applyUpdateState(update) {
    if (!update || typeof update !== 'object') return;
    updateRevision += 1;
    state.update = { ...update };
    clearTimeout(updateWatchdog);
    updateWatchdog = null;
    if (update.status === 'checking') {
      // Recover even if the completion event was lost; only About is updated.
      updateWatchdog = setTimeout(async () => {
        const revision = updateRevision;
        const res = await updateCall('update_status');
        if (revision !== updateRevision) return;
        if (res && res.ok && res.update && res.update.status !== 'checking') applyUpdateState(res.update);
        else applyUpdateState({ ...state.update, status: 'error', error: '检查尚未完成或连接超时，请重试' });
      }, 45000);
    }
    if (updateToast && updateToast.dataset.version !== String(update.release?.version || update.latestVersion || '')) hideUpdateNotification();
    renderUpdateState();
  }

  function handleUpdateEvent(event) {
    applyUpdateState(event.update);
    const update = state.update;
    if (!update || event.manual || update.status !== 'available' || update.ignored || state.settings.updateAutoCheck === false) return;
    hideUpdateNotification();
    const node = el('div', 'toast update-toast');
    node.dataset.version = String(update.release?.version || update.latestVersion || '');
    node.setAttribute('role', 'status');
    node.appendChild(el('span', '', 'yhShell 有新版本：' + node.dataset.version));
    const actions = el('div', 'update-actions');
    const view = el('button', 'btn small primary', '查看更新');
    view.type = 'button';
    view.addEventListener('click', () => { hideUpdateNotification(); openSettings('about'); });
    const dismiss = el('button', 'btn small ghost', '稍后');
    dismiss.type = 'button';
    dismiss.addEventListener('click', hideUpdateNotification);
    actions.append(view, dismiss);
    node.appendChild(actions);
    $('#toasts').appendChild(node);
    updateToast = node;
  }

  async function refreshUpdateStatus() {
    const revision = updateRevision;
    const res = await updateCall('update_status');
    if (revision === updateRevision && res && res.ok && res.update) applyUpdateState(res.update);
  }

  async function checkUpdates() {
    if (updateRequestPending || state.update?.status === 'checking') return;
    updateRequestPending = true;
    const revision = updateRevision;
    renderUpdateState();
    try {
      const res = await updateCall('check_updates');
      // The completion event may arrive before the asynchronous start response.
      if (revision !== updateRevision) return;
      if (!res || !res.ok) applyUpdateState({ ...state.update, status: res?.status === 'busy' ? 'busy' : 'error', error: res?.error || '检查失败，请重试' });
      else if (res.update) applyUpdateState(res.update);
      else if (res.started) applyUpdateState({ ...state.update, status: 'checking', error: '' });
      else applyUpdateState({ ...state.update, status: 'busy', error: '' });
    } finally {
      updateRequestPending = false;
      renderUpdateState();
    }
  }

  async function saveUpdateAutoCheck() {
    if (updateSavePending) return;
    const enabled = $('#set-update-auto').checked;
    updateSavePending = true;
    // Disable notifications immediately while an opt-out is being saved.
    const previous = state.settings.updateAutoCheck;
    state.settings.updateAutoCheck = enabled;
    renderUpdateState();
    try {
      const res = await updateCall('save_settings', { updateAutoCheck: enabled });
      if (!res || !res.ok) {
        state.settings.updateAutoCheck = previous;
        toast(res?.error || '保存自动检查设置失败', 'error');
      } else state.settings = { ...state.settings, ...(res.settings || {}) };
    } finally {
      updateSavePending = false;
      renderUpdateState();
    }
  }

  async function updateReleaseAction(ignore) {
    const release = state.update?.release;
    if (!release || updateActionPending) return;
    updateActionPending = true;
    renderUpdateState();
    try {
      const res = await updateCall(ignore ? 'ignore_update' : 'open_update_release', ...(ignore ? [release.version] : []));
      if (!res || !res.ok) {
        toast(res?.error || (ignore ? '忽略版本失败' : '无法打开发布页'), 'error');
        return;
      }
      if (ignore) {
        state.settings = { ...state.settings, ...(res.settings || {}) };
        if (state.update?.release?.version === release.version) state.update.ignored = true;
        hideUpdateNotification();
      }
    } finally {
      updateActionPending = false;
      renderUpdateState();
    }
  }

  /* ------------------------------------------------------------- settings */
  function fillSettingsForm() {
    const settings = state.settings;
    $('#set-theme').value = settings.theme || 'dark';
    $('#set-fontsize').value = settings.fontSize || 13;
    $('#set-approval').value = settings.agentApproval || 'confirm';
    $('#set-timeout').value = settings.commandTimeout || 90;
    $('#set-steps').value = settings.maxAgentSteps || 8;
    $('#set-copyonselect').checked = Boolean(settings.copyOnSelect);
    $('#set-terminal-highlight').checked = settings.terminalHighlight !== false;
    $$('[data-highlight-rule]').forEach((input) => {
      input.checked = settings.terminalHighlightRules?.[input.dataset.highlightRule] !== false;
    });
    $('#set-zmodem-confirm').checked = Boolean(settings.zmodemConfirm);
    $('#set-zmodem-overwrite').checked = Boolean(settings.zmodemOverwrite);
    const downloadDir = settings.zmodemDownloadDir || '';
    $('#set-download-dir').value = downloadDir;
    $('#set-download-dir').placeholder = `默认：${state.app.defaultDownloadDir || '下载\\yhShell'}`;
    $('#ai-approval-select').value = settings.agentApproval || 'confirm';
    updateSyncHint();
    renderUpdateState();
  }

  function updateSyncHint() {
    const hours = Number(state.settings.modelSyncHours) || 0;
    const select = $('#set-sync-hours');
    if (select) {
      if (!Array.from(select.options).some((option) => Number(option.value) === hours)) {
        const option = document.createElement('option');
        option.value = String(hours);
        option.textContent = `每 ${hours} 小时`;
        select.appendChild(option);
      }
      select.value = String(hours);
    }
    const hint = $('#model-sync-hint');
    if (hint) {
      hint.textContent = hours > 0
        ? '到期自动从上游刷新，无需手动获取'
        : '已关闭自动同步；仍可在供应商里手动「获取模型」';
    }
  }

  async function syncAllModels() {
    const button = $('#btn-sync-models');
    button.disabled = true;
    button.textContent = '同步中…';
    const res = await call('sync_all_models');
    button.disabled = false;
    button.textContent = '立即同步';
    if (!res || !res.ok) {
      toast((res && res.error) || '同步失败', 'error');
      return;
    }
    applyModelState(res);
    renderProviderList();
    renderModels();
    const failed = res.failed || [];
    if (failed.length) {
      toast(
        `已同步 ${res.synced} 个供应商，${failed.length} 个失败：${failed[0].provider}（${failed[0].message}）`,
        'error',
        7000
      );
    } else if (res.synced) {
      toast(`已同步 ${res.synced} 个供应商的模型列表`, 'success', 3200);
    } else {
      toast('没有可同步的供应商：先填写 API Key', 'info', 4000);
    }
  }

  function updateApprovalUI() {
    $('#ai-approval-select').value = state.settings.agentApproval || 'confirm';
  }

  function openSettings(pane = 'general') {
    fillSettingsForm();
    renderProviderList();
    $$('.settings-nav button').forEach((button) => {
      button.classList.toggle('active', button.dataset.pane === pane);
    });
    $$('.settings-pane').forEach((section) => {
      section.classList.toggle('active', section.dataset.pane === pane);
    });
    openModal('settings-modal');
    if (pane === 'about') refreshUpdateStatus();
  }

  async function saveSettings() {
    const patch = {
      theme: $('#set-theme').value,
      fontSize: Number($('#set-fontsize').value || 13),
      agentApproval: $('#set-approval').value,
      commandTimeout: Number($('#set-timeout').value || 90),
      maxAgentSteps: Number($('#set-steps').value || 8),
      copyOnSelect: $('#set-copyonselect').checked,
      terminalHighlight: $('#set-terminal-highlight').checked,
      terminalHighlightRules: Object.fromEntries($$('[data-highlight-rule]').map((input) => [input.dataset.highlightRule, input.checked])),
      zmodemConfirm: $('#set-zmodem-confirm').checked,
      zmodemOverwrite: $('#set-zmodem-overwrite').checked,
      zmodemDownloadDir: $('#set-download-dir').value.trim(),
    };
    const res = await call('save_settings', patch);
    if (!res.ok) {
      toast(res.error || '保存失败', 'error');
      return;
    }
    state.settings = { ...state.settings, ...res.settings };
    applyTheme(state.settings.theme);
    applyFontSize(Number(state.settings.fontSize) || 13);
    updateApprovalUI();
    $('#settings-save-hint').textContent = '已保存';
    setTimeout(() => { $('#settings-save-hint').textContent = ''; }, 1800);
  }

  function applyModelState(res) {
    if (res.models) state.models = res.models;
    if (res.providers) state.providers = res.providers;
    if (typeof res.activeModelId === 'string') state.activeModelId = res.activeModelId;
  }

  let modelRefreshPending = false;
  async function refreshModelState() {
    if (modelRefreshPending) return;
    modelRefreshPending = true;
    try {
      const res = await call('bootstrap');
      if (!res || !res.ok) return;
      state.models = res.models || [];
      state.providers = res.providers || [];
      state.activeModelId = res.activeModelId || '';
      const settingsPane = $('#settings-modal');
      if (settingsPane && !settingsPane.classList.contains('hidden')) renderProviderList();
      renderModels();
    } finally {
      modelRefreshPending = false;
    }
  }

  async function setActiveModel(ref, label) {
    const res = await call('set_active_model', ref);
    if (!res || !res.ok) {
      toast((res && res.error) || '切换模型失败', 'error');
      return;
    }
    applyModelState(res);
    renderProviderList();
    renderModels();
    updateComposerState();
    toast(`已切换到 ${label}`, 'success', 1600);
  }

  function renderProviderList() {
    const list = $('#provider-list');
    if (!list) return;
    list.innerHTML = '';
    if (!state.providers.length) {
      list.appendChild(el('div', 'field-hint', '还没有供应商，点击右上角「添加供应商」。'));
      return;
    }
    state.providers.forEach((provider) => {
      const row = el('div', 'model-row provider-row' + (provider.isActive ? ' active' : ''));
      const check = el('button', 'model-check' + (provider.isActive ? ' active' : ''));
      check.disabled = !provider.models.length;
      check.title = provider.models.length ? '使用这个供应商的模型' : '还没有勾选模型';
      check.addEventListener('click', () => {
        if (provider.isActive) return;
        const target = state.models.find((model) => model.providerId === provider.id);
        if (target) setActiveModel(target.id, target.label);
      });

      const info = el('div', 'model-info');
      const name = el('div', 'model-name');
      name.appendChild(el('span', '', provider.name));
      if (provider.isActive) name.appendChild(el('span', 'chip done', '当前'));
      if (!provider.hasKey) name.appendChild(el('span', 'chip', '未设置 Key'));
      if (!provider.models.length) name.appendChild(el('span', 'chip pending', '未选模型'));
      info.appendChild(name);

      const count = provider.models.length;
      const meta = [
        provider.baseUrl,
        count ? `共 ${count} 个模型` : '暂无模型',
        formatSyncAge(provider.lastSync),
      ].join(' · ');
      info.appendChild(el('div', 'model-meta', meta));

      if (count) {
        const tags = el('div', 'model-tags');
        const limit = 8;
        provider.models.slice(0, limit).forEach((modelId) => {
          const ref = `${provider.id}::${modelId}`;
          const isCurrent = ref === state.activeModelId;
          const tag = el('button', 'model-tag' + (isCurrent ? ' active' : ''), modelId);
          tag.title = '切换到此模型';
          tag.addEventListener('click', () => {
            if (isCurrent) return;
            setActiveModel(ref, `${provider.name} · ${modelId}`);
          });
          tags.appendChild(tag);
        });
        if (count > limit) {
          const more = el('span', 'model-tag more', `+${count - limit}`);
          more.title = '完整列表见「编辑」或 AI 面板的模型菜单';
          tags.appendChild(more);
        }
        info.appendChild(tags);
      }

      const actions = el('div', 'model-actions');
      const test = el('button', 'btn small ghost', '测试');
      test.disabled = !provider.models.length;
      if (!provider.models.length) test.title = '先获取并勾选模型后再测试';
      test.addEventListener('click', async () => {
        const active = state.models.find((model) => model.providerId === provider.id && model.isActive);
        const ref = `${provider.id}::${active ? active.model : provider.models[0]}`;
        test.disabled = true;
        test.textContent = '测试中…';
        const res = await call('test_model', ref);
        test.disabled = false;
        test.textContent = '测试';
        const success = Boolean(res && res.ok);
        toast(
          (res && (res.message || res.error)) || (success ? '连接成功' : '测试失败'),
          success ? 'success' : 'error',
          6000
        );
      });
      const edit = el('button', 'btn small ghost', '编辑');
      edit.addEventListener('click', () => openModelModal(provider));
      const del = el('button', 'btn small ghost', '删除');
      del.addEventListener('click', async () => {
        const ok = await confirmDialog('删除供应商', `确定删除「${provider.name}」及其已选模型吗？`, '删除');
        if (!ok) return;
        const res = await call('delete_provider', provider.id);
        if (res && res.ok) {
          applyModelState(res);
          renderProviderList();
          renderModels();
          updateComposerState();
          toast('供应商已删除', 'success');
        } else {
          toast((res && res.error) || '删除失败', 'error');
        }
      });
      actions.append(test, edit, del);
      row.append(check, info, actions);
      list.appendChild(row);
    });
  }

  function renderModels() {
    renderModelSelect();
    updateComposerState();
  }

  /* ----------------------------------------------------------- provider UI */
  const PROVIDER_PRESETS = {
    openai: { name: 'OpenAI', baseUrl: 'https://api.openai.com/v1' },
    deepseek: { name: 'DeepSeek', baseUrl: 'https://api.deepseek.com' },
    moonshot: { name: 'Moonshot Kimi', baseUrl: 'https://api.moonshot.cn/v1' },
    dashscope: { name: '通义千问', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
    zhipu: { name: '智谱 GLM', baseUrl: 'https://open.bigmodel.cn/api/paas/v4' },
    openrouter: { name: 'OpenRouter', baseUrl: 'https://openrouter.ai/api/v1' },
    ollama: { name: 'Ollama 本地', baseUrl: 'http://127.0.0.1:11434/v1' },
  };

  const picker = {
    models: [],
    fetched: [],
    endpoint: '',
  };

  function uniqueModels(values) {
    const out = [];
    values.forEach((value) => {
      if (value && !out.includes(value)) out.push(value);
    });
    return out;
  }

  function updatePickerSummary() {
    const total = picker.models.length;
    $('#model-count').textContent = total ? `共 ${total} 个模型` : '';
    $('#model-selected-count').textContent = total
      ? `获取后自动全部加入（${total} 个），无需逐个勾选`
      : '还没有模型；保存后这个供应商暂时不能用于对话';
    $('#model-pick-foot').classList.toggle('hidden', !total);
  }

  function renderModelPicker() {
    const box = $('#model-picker');
    box.innerHTML = '';
    const rawFilter = ($('#model-filter').value || '').trim();
    const filter = rawFilter.toLowerCase();
    const visible = picker.models.filter((id) => !filter || id.toLowerCase().includes(filter));
    if (!picker.models.length) {
      box.classList.add('empty');
      box.appendChild(
        el('p', 'picker-empty', '还没有模型。填好 Base URL 与 API Key 后点上方「获取模型」，上游模型会自动全部加入。')
      );
    } else if (!visible.length) {
      box.classList.add('empty');
      box.appendChild(el('p', 'picker-empty', '没有匹配的模型。'));
    } else {
      box.classList.remove('empty');
      visible.forEach((id) => {
        const row = el('div', 'model-option');
        row.appendChild(el('span', 'model-option-id', id));
        box.appendChild(row);
      });
    }
    // Upstreams without a usable /models endpoint: let the typed id be added by hand.
    if (rawFilter && !picker.models.some((id) => id.toLowerCase() === filter)) {
      box.classList.remove('empty');
      const add = el('button', 'model-option model-option-add');
      add.type = 'button';
      add.appendChild(el('span', '', `添加「${rawFilter}」`));
      add.title = '上游没有返回这个模型时，可以手动加进来';
      add.addEventListener('click', () => {
        if (!picker.models.includes(rawFilter)) picker.models.push(rawFilter);
        renderModelPicker();
      });
      box.appendChild(add);
    }
    updatePickerSummary();
  }

  function resetPicker() {
    picker.models = [];
    picker.fetched = [];
    picker.endpoint = '';
    $('#model-filter').value = '';
    $('#model-filter').classList.add('hidden');
    $('#model-pick-hint').textContent = '点「获取模型」请求上游 /models；成功后全部模型自动加入，无需逐个勾选。';
    renderModelPicker();
  }

  function openModelModal(provider) {
    $('#model-form').reset();
    $('#model-error').classList.add('hidden');
    $('#model-id').value = '';
    $('#model-modal-title').textContent = '添加供应商';
    $('#model-apikey-hint').textContent = '';
    $('#model-preset').value = '';
    $('#model-temp').value = '0.3';
    $('#model-tokens').value = '2048';
    resetPicker();
    if (provider) {
      $('#model-modal-title').textContent = '编辑供应商';
      $('#model-id').value = provider.id;
      $('#model-name').value = provider.name;
      $('#model-baseurl').value = provider.baseUrl;
      $('#model-temp').value = provider.temperature;
      $('#model-tokens').value = provider.maxTokens;
      $('#model-apikey-hint').textContent = provider.hasKey
        ? `已保存 Key（${provider.keyHint}），留空则保持不变`
        : '还没有设置 API Key；本地 http 地址可以留空';
      picker.models = uniqueModels([...provider.modelOptions, ...provider.models]);
      if (picker.models.length) {
        $('#model-filter').classList.remove('hidden');
        $('#model-pick-hint').textContent = `当前保存了 ${picker.models.length} 个模型；重新「获取模型」会以上游列表整体刷新。`;
      }
      renderModelPicker();
    }
    openModal('model-modal');
  }

  async function fetchProviderModels() {
    const error = $('#model-error');
    error.classList.add('hidden');
    const baseUrl = $('#model-baseurl').value.trim();
    if (!baseUrl) {
      error.textContent = '请先填写 Base URL';
      error.classList.remove('hidden');
      return;
    }
    const button = $('#btn-fetch-models');
    button.disabled = true;
    button.textContent = '获取中…';
    $('#model-pick-hint').textContent = '正在请求上游 /models …';
    const res = await call('fetch_provider_models', {
      providerId: $('#model-id').value || '',
      baseUrl,
      apiKey: $('#model-apikey').value,
    });
    button.disabled = false;
    button.textContent = '获取模型';
    if (!res || !res.ok) {
      $('#model-pick-hint').textContent = '获取失败：检查 Base URL 与 API Key 后重试。';
      error.textContent = (res && (res.message || res.error)) || '获取模型失败';
      error.classList.remove('hidden');
      return;
    }
    const fetched = res.models || [];
    picker.fetched = fetched;
    picker.endpoint = res.endpoint || '';
    // The upstream catalogue is the truth: take it whole, no manual selection.
    picker.models = fetched.slice();
    $('#model-filter').classList.remove('hidden');
    $('#model-pick-hint').textContent = res.endpoint
      ? `${res.message || '获取成功'}（${res.endpoint}）· 已全部加入`
      : `${res.message || '获取成功'} · 已全部加入`;
    renderModelPicker();
    toast(`已从上游同步 ${fetched.length} 个模型`, 'success', 3200);
  }

  async function submitModelForm(event) {
    event.preventDefault();
    const error = $('#model-error');
    error.classList.add('hidden');
    const id = $('#model-id').value;
    const name = $('#model-name').value.trim();
    const baseUrl = $('#model-baseurl').value.trim();
    if (!baseUrl) {
      error.textContent = '请填写 Base URL';
      error.classList.remove('hidden');
      return;
    }
    if (!/^https?:\/\//i.test(baseUrl)) {
      error.textContent = 'Base URL 必须以 http:// 或 https:// 开头';
      error.classList.remove('hidden');
      return;
    }
    const models = uniqueModels(picker.models);
    const payload = {
      id: id || '',
      name,
      baseUrl,
      apiKey: $('#model-apikey').value,
      temperature: Number($('#model-temp').value || 0.3),
      maxTokens: Number($('#model-tokens').value || 2048),
      models,
      modelOptions: models,
      lastSync: models.length ? Math.floor(Date.now() / 1000) : 0,
    };
    const res = await call('save_provider', payload);
    if (!res || !res.ok) {
      error.textContent = (res && res.error) || '保存失败';
      error.classList.remove('hidden');
      return;
    }
    applyModelState(res);
    closeModal('model-modal');
    renderProviderList();
    renderModels();
    updateComposerState();
    if (models.length) toast(id ? `供应商已更新（${models.length} 个模型）` : `供应商已添加（${models.length} 个模型）`, 'success');
    else toast('供应商已保存，但还没有模型；点「获取模型」即可自动加入', 'info', 5000);
  }

  function applyPreset(key) {
    const preset = PROVIDER_PRESETS[key];
    if (!preset) return;
    $('#model-name').value = preset.name;
    $('#model-baseurl').value = preset.baseUrl;
    if (!picker.models.length) {
      $('#model-pick-hint').textContent = `已填入 ${preset.name} 的地址，填好 API Key 后点「获取模型」。`;
    }
  }

  /* --------------------------------------------------------- window chrome */
  // The window is frameless; the HTML title bar drives the native behaviour
  // through the window_* bridge methods (see yhshell/window.py).
  let windowMaximized = false;
  let lastTitlePress = 0;
  let lastTitleX = 0;
  let lastTitleY = 0;
  let windowStateTimer = 0;

  function setWindowMaximized(maximized) {
    windowMaximized = Boolean(maximized);
    const app = $('#app');
    if (app) app.classList.toggle('is-maximized', windowMaximized);
    const button = $('#btn-win-max');
    if (button) {
      const label = windowMaximized ? '还原' : '最大化';
      button.title = label;
      button.setAttribute('aria-label', label);
    }
  }

  function refreshWindowState() {
    call('window_state').then((res) => {
      if (res && res.ok) setWindowMaximized(res.maximized);
    });
  }

  function scheduleWindowState() {
    clearTimeout(windowStateTimer);
    windowStateTimer = setTimeout(refreshWindowState, 120);
  }

  async function toggleWindowMaximized() {
    const res = await call('window_toggle_maximize');
    if (res && res.ok) setWindowMaximized(res.maximized);
  }

  function onTitlebarMouseDown(event) {
    if (event.button !== 0) return;
    if (event.target.closest('button, a, input, select, textarea')) return;
    const now = Date.now();
    const doubleClick =
      now - lastTitlePress < 420 &&
      Math.abs(event.screenX - lastTitleX) < 6 &&
      Math.abs(event.screenY - lastTitleY) < 6;
    event.preventDefault();
    if (doubleClick) {
      lastTitlePress = 0;
      toggleWindowMaximized();
      return;
    }
    lastTitlePress = now;
    lastTitleX = event.screenX;
    lastTitleY = event.screenY;
    call('window_drag');
  }

  function bindWindowChrome() {
    const titlebar = $('#titlebar');
    if (!titlebar) return;
    $('#btn-win-min').addEventListener('click', () => call('window_minimize'));
    $('#btn-win-max').addEventListener('click', toggleWindowMaximized);
    $('#btn-win-close').addEventListener('click', () => call('window_close'));
    titlebar.addEventListener('mousedown', onTitlebarMouseDown);
    titlebar.addEventListener('contextmenu', (event) => event.preventDefault());
    $$('#window-resizers [data-edge]').forEach((handle) => {
      handle.addEventListener('mousedown', (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        call('window_resize', handle.dataset.edge);
      });
    });
    refreshWindowState();
  }

  /* ------------------------------------------------------------- bindings */
  function bindUI() {
    bindWindowChrome();
    $('#btn-new-host').addEventListener('click', () => openHostModal(null));
    $('#side-nav').addEventListener('click', (event) => {
      const item = event.target.closest('.menu-item');
      if (!item) return;
      setView(item.dataset.view);
    });
    $('#btn-welcome-new').addEventListener('click', () => openHostModal(null));
    $('#btn-welcome-settings').addEventListener('click', () => openSettings('models'));
    $('#btn-tab-new').addEventListener('click', () => openHostModal(null));
    $('#btn-settings').addEventListener('click', () => openSettings('general'));
    $('#host-form').addEventListener('submit', submitHostForm);
    $('#btn-pick-key').addEventListener('click', async () => {
      const res = await call('pick_key_file');
      if (res.ok && res.path) $('#host-keypath').value = res.path;
    });
    $$('[data-close]').forEach((button) => {
      button.addEventListener('click', () => closeModal(button.dataset.close));
    });
    $$('.modal-backdrop').forEach((backdrop) => {
      backdrop.addEventListener('mousedown', (event) => {
        if (event.target !== backdrop) return;
        if (backdrop.id === 'confirm-modal') settleConfirm(false);
        else closeModal(backdrop.id);
      });
    });
    $$('#host-auth button').forEach((button) => {
      button.addEventListener('click', () => setHostAuth(button.dataset.value));
    });
    $$('[data-toggle-password]').forEach((button) => {
      button.addEventListener('click', () => {
        const input = document.getElementById(button.dataset.togglePassword);
        if (!input) return;
        input.type = input.type === 'password' ? 'text' : 'password';
        button.textContent = input.type === 'password' ? '显示' : '隐藏';
      });
    });

    $('#settings-modal').addEventListener('click', (event) => {
      if (event.target.id === 'settings-modal') closeModal('settings-modal');
    });
    $$('.settings-nav button').forEach((button) => {
      button.addEventListener('click', () => {
        $$('.settings-nav button').forEach((b) => b.classList.toggle('active', b === button));
        $$('.settings-pane').forEach((pane) => pane.classList.toggle('active', pane.dataset.pane === button.dataset.pane));
        if (button.dataset.pane === 'about') refreshUpdateStatus();
      });
    });
    $('#btn-save-settings').addEventListener('click', saveSettings);
    $('#btn-check-updates').addEventListener('click', checkUpdates);
    $('#set-update-auto').addEventListener('change', saveUpdateAutoCheck);
    $('#btn-update-download').addEventListener('click', () => updateReleaseAction(false));
    $('#btn-update-ignore').addEventListener('click', () => updateReleaseAction(true));
    $('#btn-add-provider').addEventListener('click', () => openModelModal(null));
    $('#model-form').addEventListener('submit', submitModelForm);
    $('#model-preset').addEventListener('change', (event) => applyPreset(event.target.value));
    $('#btn-fetch-models').addEventListener('click', fetchProviderModels);
    $('#model-filter').addEventListener('input', renderModelPicker);
    $('#model-filter').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') event.preventDefault();
    });
    $('#set-sync-hours').addEventListener('change', async (event) => {
      const hours = Number(event.target.value) || 0;
      const res = await call('save_settings', { modelSyncHours: hours });
      if (res && res.ok) {
        state.settings = { ...state.settings, ...res.settings };
        updateSyncHint();
        toast(hours > 0 ? `已改为每 ${hours} 小时自动同步模型` : '已关闭自动同步', 'success', 2600);
      } else {
        toast((res && res.error) || '保存失败', 'error');
      }
    });
    $('#btn-sync-models').addEventListener('click', syncAllModels);
    $('#btn-open-config').addEventListener('click', () => call('open_config_dir'));
    $('#btn-pick-download-dir').addEventListener('click', async () => {
      const current = $('#set-download-dir').value.trim() || state.app.defaultDownloadDir || '';
      const res = await call('zmodem_pick_directory', current);
      if (res && res.ok && res.path) $('#set-download-dir').value = res.path;
    });
    $('#transfer-cancel').addEventListener('click', () => {
      if (state.activeId) call('zmodem_cancel', state.activeId);
    });
    $('#transfer-accept').addEventListener('click', acceptTransfer);
    $('#transfer-browse').addEventListener('click', browseTransfer);
    $('#transfer-close').addEventListener('click', dismissTransfer);
    $('#confirm-cancel').addEventListener('click', () => settleConfirm(false));
    $('#confirm-ok').addEventListener('click', () => settleConfirm(true));

    $('#ai-approval-select').addEventListener('change', async (event) => {
      const value = event.target.value;
      const res = await call('save_settings', { agentApproval: value });
      if (res.ok) {
        state.settings = { ...state.settings, ...res.settings };
        $('#set-approval').value = value;
      } else {
        toast(res.error || '保存失败', 'error');
      }
    });

    $('#btn-ai-send').addEventListener('click', sendAgentMessage);
    $('#btn-ai-stop').addEventListener('click', stopAgent);
    $('#btn-ai-clear').addEventListener('click', clearChat);
    $('#btn-ai-history').addEventListener('click', openHistory);
    $('#btn-banner-continue').addEventListener('click', () => {
      if (state.chat.viewing) continueConversation(state.chat.viewing);
    });
    $('#btn-banner-exit').addEventListener('click', exitViewing);
    $('#btn-open-history-dir').addEventListener('click', () => call('open_history_dir'));
    $('#btn-ai-collapse').addEventListener('click', () => setPanelCollapsed(true));
    $('#ai-rail').addEventListener('click', () => setPanelCollapsed(false));
    $('#ai-model-select').addEventListener('change', async (event) => {
      const ref = event.target.value;
      if (!ref || ref === state.activeModelId) return;
      const model = state.models.find((item) => item.id === ref);
      if (!model) {
        renderModelSelect();
        return;
      }
      await setActiveModel(ref, model.label || model.name);
      // setActiveModel re-renders the select; make sure a failed switch reverts.
      renderModelSelect();
    });

    const input = $('#ai-input');
    input.addEventListener('input', autoGrowInput);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendAgentMessage();
      }
    });
    input.addEventListener('focus', () => {
      setTimeout(() => {
        input.scrollIntoView({ block: 'nearest' });
      }, 10);
    });

    $$('#tab-menu button').forEach((button) => {
      button.addEventListener('click', async () => {
        const sid = $('#tab-menu').dataset.sid;
        const action = button.dataset.action;
        hideTabMenu();
        if (!state.sessions.has(sid)) return;
        const groupMenu = $('#tab-menu').dataset.workspace === '1';
        const targetSession = state.sessions.get(sid);
        if (action === 'duplicate') await duplicateSession(sid);
        else if (action === 'split-right') await duplicateSession(sid, 'right');
        else if (action === 'split-down') await duplicateSession(sid, 'down');
        else if (action === 'unsplit') { activateSession(sid); clearSplit(); }
        else if (action === 'close') {
          if (groupMenu) await closeTab(targetSession.groupId);
          else await closeSession(sid);
        }
      });
    });

    $$('#terminal-menu button').forEach((button) => {
      button.addEventListener('click', () => {
        const sid = $('#terminal-menu').dataset.sid;
        const session = state.sessions.get(sid);
        hideTerminalMenu();
        if (!session) return;
        const action = button.dataset.action;
        if (action === 'copy') copySelection(session);
        else if (action === 'paste') pasteInto(session);
        else if (action === 'clear') {
          session.term.clear();
          session.highlighter?.refresh();
        }
        else if (action === 'interrupt') api('ssh_write', sid, '\x03').catch(() => {});
        session.term.focus();
      });
    });

    // 指令集：侧栏列表 + 终端里的命令面板（点击只输入，不执行）
    $('#btn-snippets').addEventListener('click', () => openSnippetPalette());

    // 主机页
    $('#btn-host-new').addEventListener('click', () => openHostModal(null));
    $('#btn-hosts-empty-new').addEventListener('click', () => openHostModal(null));
    $('#btn-host-refresh').addEventListener('click', () => {
      call('bootstrap').then((res) => {
        if (res && res.ok && Array.isArray(res.hosts)) {
          state.hosts = res.hosts;
          renderHostPage();
          toast('已刷新主机列表', 'success', 1200);
        }
      });
    });
    $('#host-cards').addEventListener('click', (event) => {
      const card = event.target.closest('.host-card');
      if (!card) return;
      const host = state.hosts.find((entry) => entry.id === card.dataset.hostId);
      if (!host) return;
      const button = event.target.closest('[data-act]');
      const act = button ? button.dataset.act : 'connect';
      if (act === 'edit') {
        openHostModal(host);
      } else if (act === 'delete') {
        deleteHost(host);
      } else {
        connectSavedHost(host.id, card);
      }
    });

    // 指令集页
    $('#btn-snippet-new').addEventListener('click', () => openSnippetModal(null, null));
    $('#btn-snippet-reset').addEventListener('click', resetSnippets);
    $('#btn-group-new').addEventListener('click', () => openGroupModal(null));
    $('#snippet-form').addEventListener('submit', submitSnippetForm);
    $('#group-form').addEventListener('submit', submitGroupForm);
    $('#btn-group-delete').addEventListener('click', async () => {
      const group = customSnippetGroups().find((entry) => entry.id === $('#group-id').value);
      if (!group) return;
      closeModal('group-modal');
      await deleteGroup(group);
    });
    $('#snippet-group-select').addEventListener('change', (event) => {
      $('#snippet-new-group').classList.toggle('hidden', event.target.value !== NEW_GROUP);
    });
    $('#snippet-page-search').addEventListener('input', renderSnippetTable);
    $('#snippet-group-list').addEventListener('click', (event) => {
      const button = event.target.closest('[data-act]');
      const item = event.target.closest('.snippet-nav-item');
      if (!item) return;
      const group = snippetGroups().find((entry) => entry.id === item.dataset.groupId);
      if (button) {
        event.stopPropagation();
        if (button.dataset.act === 'edit-group' && group) openGroupModal(group);
        else if (button.dataset.act === 'delete-group' && group) deleteGroup(group);
        return;
      }
      state.snippetGroupId = item.dataset.groupId;
      renderSnippetPage();
    });
    $('#snippet-table').addEventListener('click', (event) => {
      const row = event.target.closest('.snippet-row');
      if (!row) return;
      const button = event.target.closest('[data-act]');
      if (!button) return;
      const found = findSnippet(row.dataset.snippetId);
      if (!found) return;
      const act = button.dataset.act;
      if (act === 'copy') copySnippetCommand(found.snippet);
      else if (act === 'insert') {
        if (!currentCommandSession()) return;
        setView('terminal');
        useSnippet(found.snippet);
      }
      else if (act === 'edit') openSnippetModal(found.snippet, found.group);
      else if (act === 'delete') deleteSnippet(found.snippet);
    });
    $('#quick-group').addEventListener('change', (event) => {
      state.quickGroup = event.target.value;
      renderQuickBar();
    });
    $('#quick-items').addEventListener('click', (event) => {
      const button = event.target.closest('.quick-item');
      if (!button) return;
      const found = findSnippet(button.dataset.snippetId);
      if (found) useSnippet(found.snippet);
    });
    $('#quick-more').addEventListener('click', () => {
      const group = state.quickGroup === QUICK_FAVORITES ? 'all' : state.quickGroup;
      openSnippetPalette({ groupId: group });
    });
    $('#snippet-palette-close').addEventListener('click', closeSnippetPalette);
    $$('#snippet-palette [data-snippet-close]').forEach((node) => {
      node.addEventListener('click', closeSnippetPalette);
    });
    $('#snippet-palette-search').addEventListener('input', (event) => {
      state.snippetPalette.query = event.target.value;
      renderSnippetPaletteList();
    });
    $('#snippet-palette-groups').addEventListener('click', (event) => {
      const tab = event.target.closest('.snippet-group-tab');
      if (!tab) return;
      state.snippetPalette.groupId = tab.dataset.groupId;
      $$('#snippet-palette-groups .snippet-group-tab').forEach((node) => {
        node.classList.toggle('active', node === tab);
      });
      renderSnippetPaletteList();
      const search = $('#snippet-palette-search');
      if (search) search.focus();
    });
    $('#snippet-palette-list').addEventListener('click', (event) => {
      const item = event.target.closest('.snippet-palette-item');
      if (!item) return;
      const found = findSnippet(item.dataset.snippetId);
      if (found) handlePaletteSnippet(found.snippet);
    });
    $('#snippet-palette').addEventListener('keydown', handleSnippetPaletteKey);
    $('#snippet-var-back').addEventListener('click', () => {
      const node = snippetPaletteNode();
      if (node) node.classList.remove('var-mode');
      const form = $('#snippet-var-form');
      if (form) form.classList.add('hidden');
      const search = $('#snippet-palette-search');
      if (search) search.focus();
    });
    $('#snippet-insert').addEventListener('click', () => insertPendingSnippet(false));
    $('#snippet-insert-raw').addEventListener('click', () => insertPendingSnippet(true));

    document.addEventListener('mousedown', (event) => {
      if (!event.target.closest('#terminal-menu')) hideTerminalMenu();
      if (!event.target.closest('#tab-menu')) hideTabMenu();
    });
    // Capture before xterm: an open dialog owns keyboard focus and Escape.
    document.addEventListener('focusin', (event) => {
      const top = topModal();
      if (top && !top.contains(event.target)) focusModal(top);
    });
    document.addEventListener('keydown', (event) => {
      const top = topModal();
      if (!top) return;
      if (event.key === 'Escape' && !event.isComposing) {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeTopModal();
        return;
      }
      const ctrl = event.ctrlKey || event.metaKey;
      if (ctrl && event.shiftKey && ['KeyT', 'KeyW', 'Comma', 'KeyK'].includes(event.code)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      if (!top.contains(event.target)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        focusModal(top);
        return;
      }
      if (event.key === 'Tab') {
        const items = modalFocusables(top);
        const index = items.indexOf(document.activeElement);
        if (!items.length || index === -1 || (event.shiftKey ? index === 0 : index === items.length - 1)) {
          event.preventDefault();
          (event.shiftKey ? items[items.length - 1] : items[0])?.focus();
        }
      }
    }, true);
    // Native paste/IME input must not reach a terminal underneath a modal.
    ['paste', 'beforeinput', 'input', 'compositionstart', 'compositionupdate', 'compositionend'].forEach((type) => {
      document.addEventListener(type, (event) => {
        const top = topModal();
        if (top && !top.contains(event.target)) {
          event.preventDefault();
          event.stopImmediatePropagation();
          focusModal(top);
        }
      }, true);
    });
    document.addEventListener('keydown', (event) => {
      if (event.defaultPrevented || event.isComposing || topModal()) return;
      const ctrl = event.ctrlKey || event.metaKey;
      if (event.key === 'Escape') {
        hideTabMenu();
        if (!closeTopModal()) {
          if (!closeSnippetPalette()) hideTerminalMenu();
        }
        return;
      }
      if (ctrl && event.shiftKey && event.code === 'KeyT') {
        event.preventDefault();
        openHostModal(null);
      } else if (ctrl && event.shiftKey && event.code === 'KeyW') {
        event.preventDefault();
        if (state.activeId) closeSession(state.activeId);
      } else if (ctrl && event.shiftKey && event.code === 'Comma') {
        event.preventDefault();
        openSettings('general');
      } else if (ctrl && event.shiftKey && event.code === 'KeyK') {
        event.preventDefault();
        openSnippetPalette();
      }
    });

    const observer = new ResizeObserver(() => fitActive());
    observer.observe($('#terminal-area'));

    $('#chat').addEventListener('click', (event) => {
      if (event.target.closest('.open-history')) {
        openHistory();
        return;
      }
      const suggestion = event.target.closest('.suggestion');
      if (!suggestion) return;
      const inputEl = $('#ai-input');
      inputEl.value = suggestion.dataset.text || suggestion.textContent;
      autoGrowInput();
      inputEl.focus();
    });

    updateComposerState();
    updateWelcomeVisibility();
  }

  /* ------------------------------------------------------------- keyboard */
  window.addEventListener('resize', () => {
    fitActive();
    scheduleWindowState();
  });

  window.addEventListener('error', (event) => {
    if (event && event.message) console.error(event.message);
  });

  window.addEventListener('pywebviewready', () => {
    if (!state.booted) init();
  });

  document.addEventListener('DOMContentLoaded', waitForBridge);
  if (document.readyState !== 'loading') waitForBridge();
})();
