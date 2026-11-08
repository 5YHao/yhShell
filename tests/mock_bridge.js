(() => {
  const settings = {
    fontSize: 13,
    fontFamily: 'Cascadia Mono, Consolas, monospace',
    theme: 'dark',
    agentApproval: 'safe',
    commandTimeout: 90,
    maxAgentSteps: 8,
    copyOnSelect: false,
    modelSyncHours: 24,
    zmodemConfirm: true,
    zmodemDownloadDir: '',
    zmodemOverwrite: false,
  };

  const DEFAULT_DOWNLOAD = 'C:\\Users\\you\\Downloads\\yhShell';

  // A compact stand-in for yhshell/snippets.py: enough variety for the UI
  // regression (plain command, variable placeholders, two groups).
  const snippets = [
    {
      id: 'k8s-query',
      name: 'Kubernetes 查看',
      description: 'kubectl 查询、日志与排障',
      items: [
        {
          id: 'k8s-pods-all',
          name: '查看所有 Pod',
          command: 'kubectl get pods -A -o wide',
          note: '全命名空间，含节点与 Pod IP',
          variables: [],
          favorite: true,
        },
        {
          id: 'k8s-logs',
          name: '查看 Pod 日志',
          command: 'kubectl logs -f <Pod名> -n <命名空间>',
          note: '持续跟随输出',
          variables: ['Pod名', '命名空间'],
          favorite: true,
        },
      ],
    },
    {
      id: 'docker',
      name: 'Docker',
      description: '容器、镜像与 Compose 常用命令',
      items: [
        {
          id: 'docker-ps',
          name: '运行中的容器',
          command: 'docker ps',
          note: '含端口映射与启动时间',
          variables: [],
          favorite: true,
        },
      ],
    },
  ];

  // 用户对指令集的修改：自定义分组、内置指令的覆盖与隐藏
  const snippetState = { groups: [], overrides: {}, hidden: [] };

  const parseVars = (text) => {
    const names = [];
    String(text || '').replace(/<([^<>\r\n]{1,40})>/g, (_match, name) => {
      const clean = String(name || '').trim();
      if (clean && !names.includes(clean)) names.push(clean);
      return '';
    });
    return names;
  };

  function mergedSnippets() {
    const groups = snippets.map((group) => ({
      id: group.id,
      name: group.name,
      description: group.description || '',
      builtin: true,
      items: (group.items || [])
        .filter((item) => !snippetState.hidden.includes(item.id))
        .map((item) => {
          const patch = snippetState.overrides[item.id];
          if (!patch) return { ...item, builtin: true, edited: false };
          const command = patch.command || item.command;
          return {
            ...item,
            ...patch,
            command,
            variables: parseVars(command),
            builtin: true,
            edited: true,
          };
        }),
    }));
    snippetState.groups.forEach((group) => {
      groups.push({
        id: group.id,
        name: group.name,
        description: group.description || '',
        builtin: false,
        items: (group.items || []).map((item) => ({
          ...item,
          variables: parseVars(item.command),
          favorite: false,
          builtin: false,
          edited: false,
        })),
      });
    });
    return groups;
  }

  const providers = [
    {
      id: 'p1',
      name: 'DeepSeek',
      baseUrl: 'https://api.deepseek.com',
      temperature: 0.3,
      maxTokens: 2048,
      models: ['deepseek-chat', 'deepseek-reasoner'],
      modelOptions: ['deepseek-chat', 'deepseek-reasoner'],
      lastSync: Math.floor(Date.now() / 1000) - 600,
      hasKey: true,
      keyHint: '••••f2c',
    },
    {
      id: 'p2',
      name: '本地 Ollama',
      baseUrl: 'http://127.0.0.1:11434/v1',
      temperature: 0.4,
      maxTokens: 2048,
      models: ['qwen2.5:7b'],
      modelOptions: ['qwen2.5:7b', 'llama3.1', 'nomic-embed-text'],
      lastSync: Math.floor(Date.now() / 1000) - 7200,
      hasKey: false,
      keyHint: '',
    },
  ];

  let activeModelId = 'p1::deepseek-chat';

  function flatModels() {
    const out = [];
    providers.forEach((provider) => {
      provider.models.forEach((model) => {
        const ref = `${provider.id}::${model}`;
        out.push({
          id: ref,
          providerId: provider.id,
          providerName: provider.name,
          name: model,
          label: provider.models.length > 1 ? `${provider.name} · ${model}` : provider.name,
          model,
          baseUrl: provider.baseUrl,
          hasKey: provider.hasKey,
          keyHint: provider.keyHint,
          isActive: ref === activeModelId,
        });
      });
    });
    return out;
  }

  function publicProviders() {
    return providers.map((provider) => ({
      ...provider,
      models: [...provider.models],
      modelOptions: [...provider.modelOptions],
      isActive: provider.models.some((model) => `${provider.id}::${model}` === activeModelId),
    }));
  }

  function modelState() {
    return { ok: true, models: flatModels(), providers: publicProviders(), activeModelId };
  }

  function ensureActive() {
    const valid = providers.some((provider) =>
      provider.models.some((model) => `${provider.id}::${model}` === activeModelId)
    );
    if (valid) return;
    const first = providers.find((provider) => provider.models.length);
    activeModelId = first ? `${first.id}::${first.models[0]}` : '';
  }

  const hosts = [
    {
      id: 'h1',
      name: '生产环境',
      host: '10.0.12.8',
      port: 22,
      username: 'root',
      auth: 'password',
      hasPassword: true,
      keyPath: '',
      hasPassphrase: false,
      lastUsed: 3,
    },
    {
      id: 'h2',
      name: '测试机',
      host: '192.168.1.20',
      port: 2222,
      username: 'deploy',
      auth: 'key',
      hasPassword: false,
      keyPath: 'C:\\Users\\you\\.ssh\\id_ed25519',
      hasPassphrase: true,
      lastUsed: 2,
    },
    {
      id: 'h3',
      name: '数据库',
      host: '10.0.12.31',
      port: 22,
      username: 'dba',
      auth: 'agent',
      hasPassword: false,
      keyPath: '',
      hasPassphrase: false,
      lastUsed: 1,
    },
  ];

  const state = {
    sessions: [],
    output: {},
    events: [],
    history: [],
    transfers: {},
  };

  let transferSeq = 0;
  let transferTimer = null;

  function transferShell(direction) {
    transferSeq += 1;
    return {
      id: transferSeq,
      direction,
      phase: 'prompt',
      file: '',
      files: [],
      fileSize: 0,
      fileDone: 0,
      doneBytes: 0,
      totalBytes: 0,
      percent: 0,
      speed: 0,
      directory: settings.zmodemDownloadDir || DEFAULT_DOWNLOAD,
      savedTo: '',
      message: '',
      error: '',
      startedAt: Date.now() / 1000,
      finishedAt: 0,
      confirm: true,
      filesDone: 0,
    };
  }

  function promptTransfer(direction) {
    state.transfers[sid] = transferShell(direction);
    return state.transfers[sid];
  }

  function runProgress(sessionId) {
    const transfer = state.transfers[sessionId];
    if (!transfer) return;
    clearInterval(transferTimer);
    transfer.phase = 'transferring';
    transfer.file = transfer.direction === 'download' ? 'nginx-access.log' : 'app.log';
    transfer.files = transfer.direction === 'download' ? ['nginx-access.log'] : ['app.log', 'config.yaml'];
    transfer.fileSize = transfer.direction === 'download' ? 8_400_000 : 1_320_000;
    transfer.speed = 2_100_000;
    transfer.startedAt = Date.now() / 1000;
    let done = 0;
    transferTimer = setInterval(() => {
      done = Math.min(transfer.fileSize, done + 640_000);
      transfer.fileDone = done;
      transfer.doneBytes = done;
      transfer.percent = Math.round((done / transfer.fileSize) * 1000) / 10;
      if (done >= transfer.fileSize) {
        clearInterval(transferTimer);
        transfer.phase = 'done';
        transfer.finishedAt = Date.now() / 1000;
        transfer.filesDone = transfer.files.length;
        transfer.message = transfer.direction === 'download' ? '已接收 nginx-access.log' : '已发送 2 个文件';
        transfer.savedTo = transfer.direction === 'download'
          ? `${transfer.directory}\\nginx-access.log`
          : 'D:\\Files';
      }
    }, 140);
  }

  const now = Math.floor(Date.now() / 1000);
  const archiveRecords = [
    {
      id: 'a1b2c3d4e5f6a7b8',
      title: '帮我检查磁盘和内存的使用情况',
      host: '10.0.12.8',
      port: 22,
      username: 'root',
      hostName: '生产环境',
      model: 'deepseek-chat',
      createdAt: now - 3600,
      updatedAt: now - 1800,
      messageCount: 4,
      entries: [
        { role: 'user', text: '帮我检查磁盘和内存的使用情况' },
        { role: 'assistant', text: '我先看一下整体资源使用率。' },
        {
          role: 'tool',
          callId: 'arch1',
          command: 'df -h',
          explanation: '查看磁盘使用率',
          status: 'done',
          output:
            'Filesystem      Size  Used Avail Use% Mounted on\n' +
            '/dev/sda1        98G   39G   55G  42% /',
          exitCode: 0,
          dangerous: false,
        },
        { role: 'assistant', text: '磁盘使用率 **42%**，内存充足，暂时不需要清理。' },
      ],
    },
    {
      id: 'b2c3d4e5f6a7b8c9',
      title: '排查 nginx 502 错误',
      host: '192.168.1.20',
      port: 2222,
      username: 'deploy',
      hostName: '测试机',
      model: 'qwen2.5:7b',
      createdAt: now - 86400 * 3,
      updatedAt: now - 86400 * 2,
      messageCount: 2,
      entries: [
        { role: 'user', text: '排查 nginx 502 错误' },
        { role: 'assistant', text: '先检查 upstream 是否健康。' },
      ],
    },
  ];
  let sid = 's1';
  let connectCount = 0;
  const tick = (fn, ms) => setTimeout(fn, ms);
  let toolResolved = false;

  function pushEvent(event) {
    state.events.push(event);
  }

  function pushOutput(sessionId, text) {
    state.output[sessionId] = (state.output[sessionId] || '') + text;
  }

  function connect() {
    connectCount += 1;
    const id = 's' + connectCount;
    const name = connectCount === 1 ? '生产环境' : '测试机';
    const host = connectCount === 1 ? '10.0.12.8' : '192.168.1.20';
    const user = connectCount === 1 ? 'root' : 'deploy';
    if (connectCount === 1) sid = id;
    const session = {
      id,
      status: 'connecting',
      name,
      host,
      port: connectCount === 1 ? 22 : 2222,
      username: user,
      fingerprint: connectCount === 1 ? 'ssh-ed25519 SHA256:J3kL…Qz' : 'ssh-ed25519 SHA256:Pp2m…Xd',
    };
    state.sessions.push(session);
    tick(() => {
      session.status = 'connected';
      pushOutput(
        id,
        'Welcome to Ubuntu 22.04.4 LTS (GNU/Linux 5.15.0-105-generic x86_64)\r\n' +
          '\r\n' +
          ' * Documentation:  https://help.ubuntu.com\r\n' +
          '\r\n' +
          'Last login: Wed Nov  5 23:41:02 2026 from 10.0.0.5\r\n'
      );
      pushOutput(id, connectCount === 1 ? '\x1b[32mroot@prod\x1b[0m:\x1b[34m~\x1b[0m# ' : '\x1b[32mdeploy@staging\x1b[0m:\x1b[34m~\x1b[0m$ ');
    }, 500);
    return { ok: true, session, hosts };
  }

  function streamAssistant(parts, startDelay, step, done) {
    let delay = startDelay;
    const text = parts.join('');
    pushEvent({ type: 'busy', session: sid, busy: true });
    parts.forEach((part) => {
      tick(() => pushEvent({ type: 'delta', session: sid, text: part }), delay);
      delay += step;
    });
    tick(() => {
      pushEvent({ type: 'busy', session: sid, busy: false });
      state.history.push({ role: 'assistant', text });
      if (done) done();
    }, delay + 120);
  }

  function sendAgent(text) {
    toolResolved = false;
    state.history.push({ role: 'user', text });
    streamAssistant(['我先检查一下', '磁盘和内存的使用情况，', '稍等。'], 250, 220, () => {
      pushEvent({
        type: 'tool',
        session: sid,
        call: {
          callId: 'c1',
          command: 'df -h; free -m',
          explanation: '查看磁盘使用率与内存占用，判断是否需要清理。',
          dangerous: false,
          safe: false,
          safeReason: '含分号 ;',
          approval: 'safe',
        },
      });
    });
  }

  function resolveTool() {
    if (toolResolved) return;
    toolResolved = true;
    pushEvent({
      type: 'tool',
      session: sid,
      call: {
        callId: 'c1',
        command: 'df -h; free -m',
        explanation: '查看磁盘使用率与内存占用，判断是否需要清理。',
        dangerous: false,
        safe: false,
        safeReason: '含分号 ;',
        approval: 'safe',
      },
    });
    pushEvent({ type: 'tool_update', session: sid, callId: 'c1', status: 'running', output: '', exitCode: null });
    tick(() => {
      const output =
        'Filesystem      Size  Used Avail Use% Mounted on\n' +
        '/dev/sda1        98G   39G   55G  42% /\n' +
        'tmpfs           3.9G     0  3.9G   0% /dev/shm\n' +
        '              total        used        free\n' +
        'Mem:           7832        4381        1210\n' +
        'Swap:          2047           0        2047';
      const command = 'df -h; free -m';
      const explanation = '查看磁盘使用率与内存占用，判断是否需要清理。';
      pushEvent({
        type: 'tool_update',
        session: sid,
        callId: 'c1',
        status: 'done',
        exitCode: 0,
        output,
      });
      state.history.push({
        role: 'tool',
        callId: 'c1',
        command,
        explanation,
        status: 'done',
        output,
        exitCode: 0,
        dangerous: false,
      });
      streamAssistant(
        [
          '磁盘和内存看起来都很健康：\n\n',
          '- 根分区使用率 **42%**，剩余 55G\n',
          '- 可用内存约 **1.2 GB**，没有 swap 压力\n\n',
          '| 指标 | 使用率 | 状态 |\n',
          '| --- | ---: | :---: |\n',
          '| 根分区 | 42% | 正常 |\n',
          '| 内存 | 78% | 关注 |\n\n',
          '暂时不需要清理。如果后续使用率继续上涨，可以再检查 `/var/log` 下的大文件。',
        ],
        380,
        260
      );
    }, 700);
  }

  const api = {
    bootstrap: async () => ({
      ok: true,
      settings,
      ...modelState(),
      hosts,
      snippets: mergedSnippets(),
      appName: 'yhShell',
      defaultDownloadDir: DEFAULT_DOWNLOAD,
      configDir: 'C:\\Users\\you\\AppData\\Roaming\\yhShell',
      platform: 'Windows',
      pythonVersion: '3.12.9',
      webviewVersion: '6.2.1',
      appVersion: '0.1.0',
    }),
    poll: async () => {
      const events = state.events.splice(0);
      const output = state.output;
      state.output = {};
      return {
        ok: true,
        events,
        output,
        sessions: state.sessions,
        transfers: JSON.parse(JSON.stringify(state.transfers)),
        time: Date.now() / 1000,
      };
    },
    connect: async () => connect(),
    duplicate_session: async (sourceId) => {
      const source = state.sessions.find((session) => session.id === sourceId);
      if (!source) return { ok: false, error: '会话不存在' };
      connectCount += 1;
      const id = 's' + connectCount;
      const session = { ...source, id, status: 'connected' };
      state.sessions.push(session);
      pushOutput(id, '\\r\\n' + (source.username || 'user') + '@' + source.host + ':~$ ');
      return { ok: true, session, hosts };
    },
    close_session: async (sessionId) => {
      state.sessions = state.sessions.filter((session) => session.id !== sessionId);
      return { ok: true };
    },
    disconnect: async () => ({ ok: true }),
    ssh_write: async (sessionId, data) => {
      window.__yhMockWrites.push({ sessionId, data });
      return { ok: true };
    },
    ssh_resize: async () => ({ ok: true }),
    zmodem_pick_files: async () => ({
      ok: true,
      files: ['C:\\Users\\you\\Desktop\\app.log', 'C:\\Users\\you\\Desktop\\config.yaml'],
    }),
    zmodem_pick_directory: async () => ({ ok: true, path: 'D:\\Files' }),
    zmodem_accept: async (sessionId, options) => {
      const transfer = state.transfers[sessionId];
      if (!transfer) return { ok: false, error: '没有正在等待确认的文件传输' };
      if (transfer.phase !== 'prompt') return { ok: true };
      const chosen = options || {};
      if (transfer.direction === 'download') {
        transfer.directory = chosen.directory || transfer.directory;
      } else {
        const files = chosen.files || [];
        if (!files.length) return { ok: false, error: '没有选择文件' };
        transfer.files = files.map((file) => String(file).split('\\').pop());
      }
      runProgress(sessionId);
      return { ok: true };
    },
    zmodem_cancel: async (sessionId) => {
      const transfer = state.transfers[sessionId];
      if (transfer && (transfer.phase === 'prompt' || transfer.phase === 'transferring')) {
        clearInterval(transferTimer);
        transfer.phase = 'cancelled';
        transfer.message = '已取消传输';
        transfer.finishedAt = Date.now() / 1000;
      }
      return { ok: true };
    },
    zmodem_dismiss: async (sessionId) => {
      delete state.transfers[sessionId];
      return { ok: true };
    },
    snippet_save: async (payload) => {
      const data = payload || {};
      if (data.builtin) {
        snippetState.overrides[data.id] = {
          name: data.name || '',
          command: data.command || '',
          note: data.note || '',
        };
        snippetState.hidden = snippetState.hidden.filter((id) => id !== data.id);
        return { ok: true, id: data.id, snippets: mergedSnippets() };
      }
      let group = snippetState.groups.find((entry) => entry.id === data.groupId);
      if (!group) {
        group = {
          id: `g${Date.now()}`,
          name: data.groupName || '我的指令',
          description: '',
          items: [],
        };
        snippetState.groups.push(group);
      }
      const existing = group.items.find((item) => item.id === data.id);
      if (existing) {
        existing.name = data.name || existing.name;
        existing.command = data.command || existing.command;
        existing.note = data.note || '';
      } else {
        group.items.push({
          id: `s${Date.now()}`,
          name: data.name || data.command,
          command: data.command || '',
          note: data.note || '',
        });
      }
      return { ok: true, snippets: mergedSnippets() };
    },
    snippet_delete: async (payload) => {
      const data = payload || {};
      if (data.builtin) {
        delete snippetState.overrides[data.id];
        if (!snippetState.hidden.includes(data.id)) snippetState.hidden.push(data.id);
      } else {
        snippetState.groups.forEach((group) => {
          group.items = group.items.filter((item) => item.id !== data.id);
        });
      }
      return { ok: true, snippets: mergedSnippets() };
    },
    snippet_save_group: async (payload) => {
      const data = payload || {};
      let group = snippetState.groups.find((entry) => entry.id === data.id);
      if (!group) {
        group = { id: `g${Date.now()}`, name: data.name || '我的指令', description: '', items: [] };
        snippetState.groups.push(group);
      }
      group.name = data.name || group.name;
      return { ok: true, groupId: group.id, snippets: mergedSnippets() };
    },
    snippet_delete_group: async (payload) => {
      const data = payload || {};
      snippetState.groups = snippetState.groups.filter((group) => group.id !== data.groupId);
      return { ok: true, snippets: mergedSnippets() };
    },
    snippet_reset: async () => {
      snippetState.overrides = {};
      snippetState.hidden = [];
      return { ok: true, snippets: mergedSnippets() };
    },
    open_folder: async () => ({ ok: true }),
    agent_history: async (sessionId) => ({
      ok: true,
      messages: sessionId === sid ? state.history : [],
      busy: false,
    }),
    agent_send: async (_session, text) => {
      sendAgent(text);
      return { ok: true };
    },
    agent_resolve: async () => {
      resolveTool();
      return { ok: true };
    },
    agent_cancel: async () => ({ ok: true }),
    agent_clear: async () => {
      state.history = [];
      return { ok: true };
    },
    agent_load: async (sessionId, conversationId) => {
      const record = archiveRecords.find((item) => item.id === conversationId);
      if (!record) return { ok: false, error: '历史对话不存在' };
      state.history = record.entries.map((entry) => ({ ...entry }));
      return { ok: true, entries: state.history };
    },
    conversation_list: async () => ({
      ok: true,
      conversations: archiveRecords.map(({ entries, ...meta }) => ({ ...meta })),
    }),
    conversation_get: async (conversationId) => {
      const record = archiveRecords.find((item) => item.id === conversationId);
      if (!record) return { ok: false, error: '历史对话不存在' };
      const { entries, ...meta } = record;
      return { ok: true, conversation: { ...meta }, entries: entries.map((entry) => ({ ...entry })) };
    },
    conversation_delete: async (conversationId) => {
      const index = archiveRecords.findIndex((item) => item.id === conversationId);
      if (index >= 0) archiveRecords.splice(index, 1);
      return { ok: true, deleted: index >= 0 };
    },
    conversation_export: async () => ({
      ok: true,
      path: 'C:\\Users\\you\\AppData\\Roaming\\yhShell\\conversations\\a1b2c3d4e5f6a7b8.md',
    }),
    open_history_dir: async () => ({ ok: true }),
    save_settings: async (patch) => {
      Object.assign(settings, patch);
      return { ok: true, settings };
    },
    save_host: async () => ({ ok: true, hosts }),
    delete_host: async () => ({ ok: true, hosts }),
    save_provider: async (payload) => {
      const models = Array.isArray(payload.models) ? [...payload.models] : [];
      const options = Array.isArray(payload.modelOptions) ? [...payload.modelOptions] : models;
      if (payload.id) {
        const provider = providers.find((item) => item.id === payload.id);
        if (provider) {
          Object.assign(provider, {
            name: payload.name,
            baseUrl: payload.baseUrl,
            temperature: payload.temperature,
            maxTokens: payload.maxTokens,
            models,
            modelOptions: options,
            hasKey: provider.hasKey || Boolean(payload.apiKey),
            lastSync: payload.lastSync || Math.floor(Date.now() / 1000),
          });
        }
      } else {
        providers.push({
          id: 'p' + (providers.length + 1),
          name: payload.name,
          baseUrl: payload.baseUrl,
          temperature: payload.temperature,
          maxTokens: payload.maxTokens,
          models,
          modelOptions: options,
          hasKey: Boolean(payload.apiKey),
          keyHint: payload.apiKey ? '••••000' : '',
          lastSync: payload.lastSync || Math.floor(Date.now() / 1000),
        });
      }
      ensureActive();
      return modelState();
    },
    delete_provider: async (providerId) => {
      const index = providers.findIndex((item) => item.id === providerId);
      if (index >= 0) providers.splice(index, 1);
      if (!providers.length) providers.push({
        id: 'p1',
        name: 'DeepSeek',
        baseUrl: 'https://api.deepseek.com',
        temperature: 0.3,
        maxTokens: 2048,
        models: ['deepseek-chat'],
        modelOptions: ['deepseek-chat'],
        hasKey: false,
        keyHint: '',
      });
      const first = providers.find((provider) => provider.models.length);
      if (!first) activeModelId = '';
      else if (!first.models.some((model) => `${first.id}::${model}` === activeModelId)) {
        activeModelId = `${first.id}::${first.models[0]}`;
      }
      return modelState();
    },
    set_provider_models: async (providerId, models, modelOptions) => {
      const provider = providers.find((item) => item.id === providerId);
      if (provider) {
        provider.models = Array.isArray(models) ? [...models] : [];
        if (Array.isArray(modelOptions)) provider.modelOptions = [...modelOptions];
        provider.models.forEach((model) => {
          if (!provider.modelOptions.includes(model)) provider.modelOptions.push(model);
        });
      }
      ensureActive();
      return modelState();
    },
    fetch_provider_models: async () => ({
      ok: true,
      models: ['deepseek-chat', 'deepseek-reasoner', 'deepseek-coder'],
      endpoint: 'https://api.deepseek.com/v1/models',
      message: '获取到 3 个模型',
    }),
    sync_all_models: async () => {
      providers.forEach((provider) => {
        provider.lastSync = Math.floor(Date.now() / 1000);
      });
      return { ok: true, synced: providers.length, changed: 0, failed: [], ...modelState() };
    },
    set_active_model: async (modelId) => {
      activeModelId = modelId;
      return modelState();
    },
    test_model: async () => ({ ok: true, message: '连接成功（82 ms）' }),
    reveal_secret: async () => ({ ok: true, value: '' }),
    pick_key_file: async () => ({ ok: true, path: 'C:\\Users\\you\\.ssh\\id_ed25519' }),
    open_config_dir: async () => ({ ok: true }),
    set_title: async () => ({ ok: true }),
    window_state: async () => ({ ok: true, maximized: false }),
    window_minimize: async () => ({ ok: true }),
    window_toggle_maximize: async () => ({ ok: true, maximized: false }),
    window_close: async () => ({ ok: true }),
    window_drag: async () => ({ ok: true }),
    window_resize: async () => ({ ok: true }),
    window_set_theme: async () => ({ ok: true }),
    shutdown: async () => ({ ok: true }),
    shutdown_sessions: async () => ({ ok: true }),
  };

  window.__yhMockTransfer = {
    promptDownload: () => promptTransfer('download'),
    promptUpload: () => promptTransfer('upload'),
  };

  // Recorded ssh_write payloads so the UI test can prove a snippet was typed
  // into the terminal without a trailing Enter.
  window.__yhMockWrites = [];

  // Deliver realistic event batches without inserting artificial idle/busy
  // boundaries between automatic agent steps.
  window.__yhMockAgentEvents = (events) => {
    events.forEach((event) => pushEvent({ ...event, session: sid }));
  };

  window.pywebview = { api };
})();
