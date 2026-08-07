(() => {
  'use strict';

  const routeMeta = {
    overview: ['概览', '运行状态与运维提醒'],
    connections: ['工具连接', '工具网关配置、暴露策略与重启语义'],
    workspaces: ['工作区', '会话的文件与进程边界'],
    conversations: ['聊天会话', '摘要列表与按需正文'],
    settings: ['服务器设置', '带版本校验的配置管理'],
    oauth: ['授权管理', '客户端、授权记录、令牌与签名密钥'],
    vault: ['凭据保险箱', '只显示名称的凭据管理'],
    system: ['系统状态', '运行时、工具网关、凭据保险箱与遥测'],
  };

  const iconNames = {
    filesystem: 'folder',
    github: 'code',
    'web-search': 'globe',
    memory: 'database',
    database: 'database',
    analytics: 'activity',
  };

  const state = {
    route: 'overview',
    connectionFilter: 'all',
    connectionQuery: '',
    oauthTab: 'clients',
    conversationPage: 1,
    conversationWorkspace: 'main',
    conversationQuery: '',
    selectedConversation: null,
    live: false,
    loading: false,
    apiBase: '',
    apiToken: '',
    gatewayRestartRequired: true,
    settingsConflict: false,
    theme: document.documentElement.dataset.themePreference || 'system',
    logFilters: { time: '24h', level: 'all', component: 'all', query: '' },
    selectedLogId: null,
    settings: {
      active: {
        host: '127.0.0.1',
        port: 8765,
        permission_mode: 'safe',
        shell_env: 'core',
        oauth_server_url: '',
        allowed_origins: ['http://127.0.0.1:8765'],
        oauth_compatibility_mode: false,
        dangerously_fake_readonly_annotations: false,
      },
      persisted: {
        host: '127.0.0.1',
        port: 8000,
        permission_mode: 'safe',
        shell_env: 'core',
        oauth_server_url: '',
        allowed_origins: ['http://127.0.0.1:8000'],
        oauth_compatibility_mode: false,
        dangerously_fake_readonly_annotations: false,
      },
      pending_restart: ['port', 'allowed_origins'],
      revision: 'rev-20260807-a4f2',
    },
    connections: [
      { alias: 'filesystem', transport: 'stdio', command: 'uvx', mode: 'broker', enabled: true, pins: ['read_file', 'search_text'], tags: ['files', 'workspace'], tools: 12, lastUsed: '3 分钟前' },
      { alias: 'github', transport: 'streamable_http', url: 'https://github.example/mcp', mode: 'broker', enabled: true, pins: ['search_repositories'], tags: ['code', 'repository'], tools: 15, lastUsed: '45 分钟前' },
      { alias: 'web-search', transport: 'streamable_http', url: 'https://search.example/mcp', mode: 'direct', enabled: true, pins: [], tags: ['web', 'research'], tools: 8, lastUsed: '12 分钟前' },
      { alias: 'memory', transport: 'stdio', command: 'npx', mode: 'broker', enabled: true, pins: ['search_nodes'], tags: ['knowledge'], tools: 7, lastUsed: '1 小时前' },
      { alias: 'database', transport: 'stdio', command: 'uvx', mode: 'direct', enabled: false, pins: [], tags: ['data'], tools: 9, lastUsed: '3 天前' },
      { alias: 'analytics', transport: 'streamable_http', url: 'https://analytics.example/mcp', mode: 'broker', enabled: true, pins: ['query_metrics'], tags: ['metrics', 'team'], tools: 11, lastUsed: '昨天' },
    ],
    workspaces: [
      { id: 'main', name: '项目代码', root: 'G:\\LLM\\coding-tools-mcp', enabled: true, default: true, exists: true, directory: true, lastCheck: '18 分钟前' },
      { id: 'research', name: '研究资料', root: 'D:\\Research', enabled: true, default: false, exists: true, directory: true, lastCheck: '今天 09:20' },
      { id: 'exports', name: '导出文件', root: 'D:\\Exports', enabled: true, default: false, exists: false, directory: false, lastCheck: '昨天 16:05' },
    ],
    conversations: [
      { workspace_id: 'main', conversation_id: 'conv-auth', title: '优化用户认证流程', preview: '讨论如何简化 OAuth 授权页面和客户端工作区选择。', updated_at: '今天 10:30', message_count: 18, messages: [
        { role: 'user', created_at: '10:18', content: 'OAuth 客户端已注册，但没有工作区权限时，管理台应该怎么提示？' },
        { role: 'assistant', created_at: '10:19', content: '应该把它视为阻塞任务，而不是普通状态。客户端卡片需要明确显示“未分配工作区”，并提供直接编辑允许列表的入口。' },
        { role: 'user', created_at: '10:22', content: '更新允许列表会影响已有令牌吗？' },
        { role: 'assistant', created_at: '10:23', content: '不会。它只影响后续授权；已有授权记录和令牌保留原来的工作区身份。' },
      ] },
      { workspace_id: 'main', conversation_id: 'conv-db', title: '数据库查询性能分析', preview: '分析慢查询日志和索引优化方案。', updated_at: '今天 09:15', message_count: 26, messages: [
        { role: 'user', created_at: '09:04', content: '帮我分析 reports/query-plan.json 里的慢查询。' },
        { role: 'assistant', created_at: '09:05', content: '我会先读取执行计划，再按扫描行数、排序和索引命中情况整理问题。' },
      ] },
      { workspace_id: 'main', conversation_id: 'conv-api', title: 'API 接口设计讨论', preview: '设计新的 RESTful API 接口和 revision 冲突处理。', updated_at: '昨天', message_count: 12, messages: [
        { role: 'user', created_at: '昨天 16:42', content: '设置保存遇到旧 revision 时不能覆盖服务器新配置。' },
        { role: 'assistant', created_at: '昨天 16:43', content: '返回 409 stale_revision 后，页面保留草稿，重新读取最新持久化配置并展示差异。' },
      ] },
      { workspace_id: 'research', conversation_id: 'conv-paper', title: '整理 MCP 安全边界资料', preview: '汇总工作区、OAuth、凭据保险箱与外部沙箱的职责。', updated_at: '周二', message_count: 31, messages: [
        { role: 'user', created_at: '周二 14:11', content: '帮我把安全边界拆成用户能理解的四层。' },
        { role: 'assistant', created_at: '周二 14:13', content: '可以分为工作区范围、命令权限门、管理员与 OAuth 身份，以及外部操作系统沙箱。' },
      ] },
    ],
    oauth: {
      clients: [
        { client_id: 'cursor-desktop', name: 'Cursor Desktop', enabled: true, authorize_login: { mode: 'client', configured: true }, workspace_ids: ['main', 'research'], created_at: '2026-08-04' },
        { client_id: 'claude-remote', name: 'Claude Remote', enabled: true, authorize_login: { mode: 'global', configured: true }, workspace_ids: [], created_at: '2026-08-05' },
        { client_id: 'internal-agent', name: 'Internal Agent', enabled: false, authorize_login: { mode: 'global', configured: true }, workspace_ids: ['main'], created_at: '2026-07-28' },
      ],
      grants: [
        { id: 'grant-8fd2', client_id: 'cursor-desktop', workspace_id: 'main', status: 'active', created_at: '2026-08-06 21:14' },
        { id: 'grant-20ab', client_id: 'claude-remote', workspace_id: 'research', status: 'revoked', created_at: '2026-08-03 09:40' },
      ],
      tokens: [
        { id: 'jti-7a91', client_id: 'cursor-desktop', workspace_id: 'main', status: 'active', expires_at: '2026-08-07 18:00' },
        { id: 'jti-29f0', client_id: 'internal-agent', workspace_id: 'main', status: 'revoked', expires_at: '2026-08-06 18:00' },
      ],
      'refresh-families': [
        { id: 'family-c8a2', client_id: 'cursor-desktop', status: 'active', rotated_at: '2026-08-07 09:12' },
      ],
      'signing-keys': [
        { id: 'kid-2026-08', status: 'active', created_at: '2026-08-01' },
        { id: 'kid-2026-07', status: 'retired', created_at: '2026-07-01' },
      ],
      audit: [
        { id: 'evt-1038', action: 'client.workspace_access.updated', actor: 'admin', created_at: '2026-08-07 09:32' },
        { id: 'evt-1037', action: 'token.revoked', actor: 'admin', created_at: '2026-08-06 19:18' },
      ],
    },
    vault: [
      { name: 'oauth/authorization-password', usage: 'OAuth 授权全局密码', protected: true },
      { name: 'gateway/github-token', usage: 'github 连接凭据', protected: false },
      { name: 'oauth/signing-key-2026-08', usage: 'OAuth 签名材料', protected: true },
    ],
    logs: [
      { id: 'log-1001', timestamp: Date.now() - 2 * 60 * 1000, level: 'info', component: 'gateway', event: 'runtime.snapshot.ready', message: '工具网关运行时已完成不可变工具快照初始化。', context: { tool_count: 48, exposure_mode: 'broker', restart_required: true } },
      { id: 'log-1002', timestamp: Date.now() - 7 * 60 * 1000, level: 'debug', component: 'runtime', event: 'session.initialize', message: '新的 HTTP MCP 会话已绑定默认工作区。', context: { workspace_id: 'main', permission_mode: 'safe', credential: '[redacted]' } },
      { id: 'log-1003', timestamp: Date.now() - 12 * 60 * 1000, level: 'warn', component: 'oauth', event: 'client.workspace_missing', message: '启用的 OAuth 客户端没有可授权的工作区允许列表。', context: { client_id: 'claude-remote', workspace_count: 0 } },
      { id: 'log-1004', timestamp: Date.now() - 18 * 60 * 1000, level: 'info', component: 'admin', event: 'settings.read', message: '管理员读取了当前生效、已持久化和等待重启的配置摘要。', context: { revision: 'rev-20260807-a4f2', secret_fields: '[redacted]' } },
      { id: 'log-1005', timestamp: Date.now() - 27 * 60 * 1000, level: 'info', component: 'vault', event: 'secret.reference.resolved', message: '工具网关凭据引用已从凭据保险箱安全解析。', context: { secret_name: 'gateway/github-token', value: '[redacted]' } },
      { id: 'log-1006', timestamp: Date.now() - 44 * 60 * 1000, level: 'warn', component: 'gateway', event: 'restart.required', message: '持久化工具网关配置已变化，当前运行时仍使用旧工具快照。', context: { changed_server: 'github', current_runtime_mutated: false } },
      { id: 'log-1007', timestamp: Date.now() - 65 * 60 * 1000, level: 'error', component: 'runtime', event: 'command.failed', message: '一个受管命令以非零退出码结束；输出已保留供后续读取。', context: { command_id: 'cmd-demo-47', exit_code: 1, output_ref: 'session:[redacted]' } },
      { id: 'log-1008', timestamp: Date.now() - 110 * 60 * 1000, level: 'info', component: 'oauth', event: 'token.revoked', message: '管理员按稳定 jti 撤销了一个访问令牌元数据记录。', context: { token_id: 'jti-29f0', affected_count: 1 } },
      { id: 'log-1009', timestamp: Date.now() - 180 * 60 * 1000, level: 'info', component: 'admin', event: 'workspace.checked', message: '工作区目录路径检查完成。', context: { workspace_id: 'main', exists: true, is_directory: true } },
      { id: 'log-1010', timestamp: Date.now() - 360 * 60 * 1000, level: 'debug', component: 'gateway', event: 'broker.catalog.search', message: '代理目录搜索返回匹配的上游工具定义。', context: { query: 'repository search', result_count: 6 } },
      { id: 'log-1011', timestamp: Date.now() - 720 * 60 * 1000, level: 'warn', component: 'vault', event: 'signing_key.rotation_due', message: '当前 OAuth 签名密钥即将进入建议轮换窗口。', context: { key_id: 'kid-2026-08', secret_material: '[redacted]' } },
      { id: 'log-1012', timestamp: Date.now() - 1500 * 60 * 1000, level: 'info', component: 'runtime', event: 'service.started', message: 'Coding Tools MCP HTTP 服务启动完成。', context: { host: '127.0.0.1', port: 8000, telemetry: 'on' } },
    ],
    system: {
      admin_api: true,
      gateway: true,
      runtime: true,
      vault: true,
      telemetry: 'on',
      permission_mode: 'safe',
      exposure_count: 48,
      config_version: 'v25.08.05-14',
      api_url: 'http://127.0.0.1:8000/admin/api',
    },
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  function createIcon(name, className = 'icon') {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', className);
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function node(tag, options = {}, children = []) {
    const element = document.createElement(tag);
    if (options.className) element.className = options.className;
    if (options.text !== undefined) element.textContent = String(options.text);
    if (options.type) element.type = options.type;
    if (options.id) element.id = options.id;
    if (options.title) element.title = options.title;
    if (options.value !== undefined) element.value = options.value;
    if (options.disabled) element.disabled = true;
    Object.entries(options.attrs || {}).forEach(([key, value]) => element.setAttribute(key, String(value)));
    element.append(...children);
    return element;
  }

  function statusBadge(text, kind = 'info') {
    return node('span', { className: `badge ${kind}`, text });
  }

  function showToast(title, detail = '') {
    const region = $('#toastRegion');
    const toast = node('div', { className: 'toast' });
    const mark = node('span', {}, [createIcon('check', 'icon icon-sm')]);
    const copy = node('span');
    copy.append(node('strong', { text: title }), node('small', { text: detail }));
    toast.append(mark, copy);
    region.append(toast);
    window.setTimeout(() => toast.remove(), 3600);
  }

  function applyThemePreference(preference, persist = true) {
    const allowed = ['light', 'dark', 'system'];
    const next = allowed.includes(preference) ? preference : 'system';
    const resolved = next === 'system'
      ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
      : next;
    state.theme = next;
    document.documentElement.dataset.themePreference = next;
    document.documentElement.dataset.theme = resolved;
    document.documentElement.style.colorScheme = resolved;
    const themeMeta = $('meta[name="theme-color"]');
    if (themeMeta) themeMeta.content = resolved === 'dark' ? '#080d1b' : '#f4f7fb';
    const select = $('#themeSelect');
    if (select) select.value = next;
    const iconUse = $('#themePickerIcon use');
    if (iconUse) iconUse.setAttribute('href', next === 'light' ? '#i-sun' : next === 'dark' ? '#i-moon' : '#i-monitor');
    const labels = { light: '日间模式', dark: '夜间模式', system: `跟随系统（当前${resolved === 'dark' ? '夜间' : '日间'}）` };
    const picker = $('#themePicker');
    if (picker) picker.title = `界面主题：${labels[next]}`;
    if (persist) {
      try { localStorage.setItem('showcase-bwa-theme', next); } catch { /* localStorage may be unavailable */ }
    }
  }

  function closeSidebar() {
    document.body.classList.remove('sidebar-open');
    $('#menuButton').setAttribute('aria-expanded', 'false');
  }

  function setRoute(route, focus = true) {
    if (!routeMeta[route]) return;
    state.route = route;
    $$('[data-page]').forEach((page) => { page.hidden = page.dataset.page !== route; });
    $$('[data-route]').forEach((control) => {
      const active = control.dataset.route === route;
      control.classList.toggle('active', active);
      if (active) control.setAttribute('aria-current', 'page');
      else control.removeAttribute('aria-current');
    });
    const [title, subtitle] = routeMeta[route];
    $('#pageTitle').textContent = title;
    $('#pageSubtitle').textContent = subtitle;
    document.title = `${title} · Coding Tools MCP 管理控制台设计`;
    closeSidebar();
    renderCurrentPage();
    if (focus) {
      $('#mainContent').focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }

  function defaultWorkspace() {
    return state.workspaces.find((item) => item.default && item.enabled) || state.workspaces.find((item) => item.enabled);
  }

  function buildTasks() {
    const tasks = [];
    const invalidWorkspace = state.workspaces.find((workspace) => workspace.default && (!workspace.exists || !workspace.directory));
    const unboundClients = state.oauth.clients.filter((client) => client.enabled && client.workspace_ids.length === 0);
    if (state.gatewayRestartRequired || state.settings.pending_restart.length) {
      tasks.push({ kind: 'warning', icon: 'alert', title: '需要重启服务', detail: '工具网关或服务器设置已保存，但当前运行时仍使用旧快照。', action: '查看影响', route: 'system' });
    }
    if (invalidWorkspace) {
      tasks.push({ kind: 'danger', icon: 'folder', title: '检查默认工作区', detail: `${invalidWorkspace.name} 的路径不存在或不是目录。`, action: '检查并修复', route: 'workspaces' });
    } else {
      const unchecked = state.workspaces.find((workspace) => !workspace.exists || !workspace.directory);
      if (unchecked) tasks.push({ kind: 'warning', icon: 'folder', title: '复核工作区路径', detail: `${unchecked.name} 的最近检查未通过。`, action: '查看工作区', route: 'workspaces' });
    }
    if (state.conversations.length) {
      tasks.push({ kind: 'success', icon: 'check', title: '会话存储运行正常', detail: `已保存 ${state.conversations.length} 个会话摘要，正文可按需读取。`, action: '查看会话', route: 'conversations' });
    }
    if (unboundClients.length) {
      tasks.push({ kind: 'danger', icon: 'xcircle', title: 'OAuth 客户端配置', detail: `${unboundClients.length} 个启用客户端缺少工作区允许列表。`, action: '查看详情', route: 'oauth' });
    }
    if (!tasks.length) tasks.push({ kind: 'success', icon: 'check', title: '没有阻塞任务', detail: '工具连接、工作区、OAuth 与服务器配置均可用。', action: '查看系统状态', route: 'system' });
    return tasks.slice(0, 4);
  }

  function renderTasks() {
    const root = $('#taskGrid');
    root.replaceChildren();
    buildTasks().forEach((task) => {
      const card = node('article', { className: `task-card ${task.kind}` });
      const icon = node('span', { className: 'task-icon' }, [createIcon(task.icon, 'icon icon-sm')]);
      const copy = node('div', { className: 'task-copy' });
      const button = node('button', { type: 'button', text: task.action });
      button.addEventListener('click', () => setRoute(task.route));
      copy.append(node('strong', { text: task.title }), node('p', { text: task.detail }), button);
      card.append(icon, copy);
      root.append(card);
    });
  }

  function renderMetrics() {
    const root = $('#metricGrid');
    root.replaceChildren();
    const enabledConnections = state.connections.filter((item) => item.enabled).length;
    const brokerConnections = state.connections.filter((item) => item.mode === 'broker').length;
    const validWorkspaces = state.workspaces.filter((item) => item.exists && item.directory && item.enabled).length;
    const totalMessages = state.conversations.reduce((sum, item) => sum + (item.message_count || 0), 0);
    const metrics = [
      { title: '工具连接', icon: 'plug', value: state.connections.length, unit: '个连接', meta: [[enabledConnections, '启用中', 'good'], [state.connections.length - enabledConnections, '已停用', 'danger'], [brokerConnections, '代理模式', 'info']] },
      { title: '工作区', icon: 'folder', value: state.workspaces.length, unit: '个目录', meta: [[validWorkspaces, '正常', 'good'], [state.workspaces.length - validWorkspaces, '需检查', 'warning'], [defaultWorkspace() ? 1 : 0, '默认', 'info']] },
      { title: '聊天会话', icon: 'message', value: state.conversations.length, unit: '个会话', meta: [[state.conversations.length, '可恢复', 'good'], [totalMessages, '总消息数', 'info']] },
      { title: '运行时工具', icon: 'activity', value: state.system.exposure_count, unit: '个公开定义', meta: [[state.system.gateway ? '正常' : '异常', '工具网关', state.system.gateway ? 'good' : 'danger'], [state.system.permission_mode, '权限模式', 'info']] },
    ];
    metrics.forEach((metric) => {
      const card = node('article', { className: 'metric-card' });
      const head = node('div', { className: 'metric-head' }, [node('span', { text: metric.title }), node('span', { className: 'metric-icon' }, [createIcon(metric.icon)])]);
      const value = node('strong', { className: 'metric-value' });
      value.append(document.createTextNode(metric.value), node('small', { text: metric.unit }));
      const meta = node('div', { className: 'metric-meta' });
      metric.meta.forEach(([amount, label, kind]) => meta.append(node('span', {}, [node('span', { className: `mini-dot ${kind}` }), document.createTextNode(`${amount} ${label}`)])));
      card.append(head, value, meta);
      root.append(card);
    });
  }

  function compactIcon(name, modifier = '') {
    return node('span', { className: `compact-icon ${modifier}`.trim() }, [createIcon(name, 'icon icon-sm')]);
  }

  function renderOverviewConnections() {
    const root = $('#overviewConnections');
    root.replaceChildren();
    state.connections.slice(0, 5).forEach((item) => {
      const row = node('div', { className: 'compact-row' });
      const copy = node('span', { className: 'compact-copy' }, [node('strong', { text: item.alias }), node('span', { text: item.transport === 'stdio' ? '本地命令' : '流式 HTTP' })]);
      const meta = node('span', { className: 'compact-meta' }, [statusBadge(item.enabled ? '启用中' : '已停用', item.enabled ? 'good' : 'danger'), statusBadge(item.mode === 'broker' ? '代理模式' : '直连模式', item.mode === 'broker' ? 'purple' : 'info')]);
      row.append(compactIcon(iconNames[item.alias] || 'plug', item.alias === 'filesystem' ? 'folder' : ''), copy, meta);
      root.append(row);
    });
  }

  function renderOverviewConversations() {
    const root = $('#overviewConversations');
    root.replaceChildren();
    state.conversations.slice(0, 5).forEach((item) => {
      const row = node('button', { className: 'compact-row', type: 'button' });
      row.style.width = '100%';
      row.style.borderLeft = '0';
      row.style.borderRight = '0';
      row.style.borderBottom = '0';
      row.style.background = 'transparent';
      row.style.color = 'inherit';
      row.style.textAlign = 'left';
      row.addEventListener('click', () => {
        state.selectedConversation = item.conversation_id;
        state.conversationWorkspace = item.workspace_id;
        setRoute('conversations');
        renderConversationDetail(item);
      });
      row.append(compactIcon('message', 'message'), node('span', { className: 'compact-copy' }, [node('strong', { text: item.title }), node('span', { text: item.preview })]), node('span', { className: 'compact-meta', text: item.updated_at }));
      root.append(row);
    });
  }

  function systemRows() {
    return [
      ['server', 'MCP 服务', state.system.runtime ? '运行中' : '异常', state.system.runtime],
      ['server', '工具网关服务', state.system.gateway ? '连接正常' : '不可用', state.system.gateway],
      ['shield', '凭据保险箱', state.system.vault ? '已启用' : '未启用', state.system.vault],
      ['settings', '配置版本', state.system.config_version, true],
      ['clock', '最后更新', '刚刚', true],
    ];
  }

  function renderOverviewSystem() {
    const root = $('#overviewSystem');
    root.replaceChildren();
    systemRows().forEach(([icon, label, value, ok]) => {
      const row = node('div', { className: 'status-row' });
      row.append(node('span', {}, [createIcon(icon, 'icon icon-sm')]), node('strong', { text: label }), node('small', { text: value, className: ok ? 'good-text' : '' }));
      root.append(row);
    });
  }

  function renderOverview() {
    renderTasks();
    renderMetrics();
    renderOverviewConnections();
    renderOverviewConversations();
    renderOverviewSystem();
    $('#runtimePermission').textContent = state.system.permission_mode;
    $('#runtimeApiUrl').textContent = state.system.api_url;
    $('#runtimeStatusText').textContent = state.system.runtime ? '运行中' : '异常';
    $('#sidebarMode').textContent = state.connections.filter((item) => item.mode === 'broker').length >= state.connections.filter((item) => item.mode === 'direct').length ? '代理模式' : '直连模式为主';
  }

  function connectionMatches(item) {
    const query = state.connectionQuery.toLowerCase();
    const searchable = `${item.alias} ${item.transport} ${item.mode} ${(item.tags || []).join(' ')}`.toLowerCase();
    const filter = state.connectionFilter;
    return (!query || searchable.includes(query)) && (
      filter === 'all' ||
      (filter === 'enabled' && item.enabled) ||
      (filter === 'disabled' && !item.enabled) ||
      filter === item.mode
    );
  }

  async function confirmAction(title, message) {
    const dialog = $('#confirmDialog');
    $('#confirmTitle').textContent = title;
    $('#confirmMessage').textContent = message;
    return new Promise((resolve) => {
      const handler = () => {
        dialog.removeEventListener('close', handler);
        resolve(dialog.returnValue === 'confirm');
      };
      dialog.addEventListener('close', handler);
      dialog.showModal();
    });
  }

  function renderConnections() {
    const root = $('#connectionTable');
    root.replaceChildren();
    const header = node('div', { className: 'table-header' }, ['连接', '状态', '暴露方式', '工具数量', '最近使用', '操作'].map((text) => node('span', { text })));
    root.append(header);
    const items = state.connections.filter(connectionMatches);
    items.forEach((item) => {
      const row = node('div', { className: 'table-row' });
      const primary = node('div', { className: 'table-primary' });
      primary.append(node('span', {}, [createIcon(iconNames[item.alias] || 'plug', 'icon icon-sm')]), node('span', {}, [node('strong', { text: item.alias }), node('small', { text: item.transport === 'stdio' ? `标准输入输出 · ${item.command || 'command'}` : item.url || '流式 HTTP' })]));
      const actions = node('div', { className: 'table-actions' });
      const edit = node('button', { className: 'icon-button', type: 'button', title: '编辑', attrs: { 'aria-label': `编辑 ${item.alias}` } }, [createIcon('edit', 'icon icon-sm')]);
      edit.addEventListener('click', () => showToast('打开编辑表单', `${item.alias} 的隐藏凭据字段会被保留。`));
      const toggle = node('button', { className: 'icon-button', type: 'button', title: item.enabled ? '下次启动禁用' : '下次启动启用', attrs: { 'aria-label': item.enabled ? `下次启动禁用 ${item.alias}` : `下次启动启用 ${item.alias}` } }, [createIcon(item.enabled ? 'xcircle' : 'check', 'icon icon-sm')]);
      toggle.addEventListener('click', () => {
        if (state.live) return showToast('真实连接为只读模式', '请在正式 /admin 管理台中修改工具网关。');
        item.enabled = !item.enabled;
        state.gatewayRestartRequired = true;
        renderAll();
        showToast(`${item.alias} 已设为下次启动${item.enabled ? '启用' : '禁用'}`, '当前运行时保持不变。');
      });
      const remove = node('button', { className: 'icon-button', type: 'button', title: '删除', attrs: { 'aria-label': `删除 ${item.alias}` } }, [createIcon('trash', 'icon icon-sm')]);
      remove.addEventListener('click', async () => {
        if (state.live) return showToast('真实连接为只读模式', '展示页不会删除真实工具网关配置。');
        if (!await confirmAction('删除 MCP 连接', `连接别名：${item.alias}。只从展示数据中删除；当前运行时不变。`)) return;
        state.connections = state.connections.filter((candidate) => candidate !== item);
        state.gatewayRestartRequired = true;
        renderAll();
        showToast('连接已删除', `${item.alias} 已从展示草稿移除。`);
      });
      actions.append(edit, toggle, remove);
      row.append(primary, statusBadge(item.enabled ? '下次启动启用' : '已停用', item.enabled ? 'good' : 'danger'), statusBadge(item.mode === 'broker' ? '代理模式' : '直连模式', item.mode === 'broker' ? 'purple' : 'info'), node('span', { text: `${item.tools || 0} 项` }), node('span', { text: item.lastUsed || '未使用' }), actions);
      root.append(row);
    });
    if (!items.length) root.append(node('div', { className: 'empty-state' }, [node('span', {}, [createIcon('search')]), node('h2', { text: '没有匹配的连接' }), node('p', { text: '调整搜索词或筛选条件。' })]));
    const enabled = state.connections.filter((item) => item.enabled).length;
    const broker = state.connections.filter((item) => item.mode === 'broker').length;
    $('#connectionSummaryTotal').textContent = String(state.connections.length);
    $('#connectionSummaryEnabled').textContent = `${enabled} 个启用`;
    $('#connectionRuntimeTools').textContent = String(state.system.exposure_count);
    $('#connectionExposureMode').textContent = `${broker} 个代理 / ${state.connections.length - broker} 个直连`;
    $('#gatewayRestartStrip').hidden = !state.gatewayRestartRequired;
  }

  function renderWorkspaces() {
    const root = $('#workspaceGrid');
    root.replaceChildren();
    state.workspaces.forEach((workspace) => {
      const card = node('article', { className: 'workspace-card' });
      const head = node('div', { className: 'workspace-head' });
      const title = node('div', { className: 'workspace-title' }, [node('span', {}, [createIcon('folder')]), node('span', {}, [node('strong', { text: workspace.name }), node('small', { text: workspace.default ? '默认工作区' : workspace.id })])]);
      head.append(title, statusBadge(workspace.enabled ? (workspace.exists && workspace.directory ? '可用' : '需检查') : '已停用', workspace.enabled ? (workspace.exists && workspace.directory ? 'good' : 'warning') : 'danger'));
      const meta = node('div', { className: 'workspace-meta' });
      [['位置', workspace.root], ['工作区标识', workspace.id], ['最近检查', workspace.lastCheck || '未检查']].forEach(([label, value]) => meta.append(node('div', {}, [node('span', { text: label }), node('strong', { text: value, title: value })])));
      const actions = node('div', { className: 'workspace-actions' });
      const check = node('button', { className: 'button primary compact', type: 'button', text: '检查路径' });
      check.addEventListener('click', async () => {
        if (state.live) {
          try {
            check.disabled = true;
            const result = await apiRequest(`/workspaces/${encodeURIComponent(workspace.id)}/check`);
            const info = result.check || result;
            workspace.exists = Boolean(info.exists);
            workspace.directory = Boolean(info.is_directory);
            workspace.lastCheck = '刚刚';
            renderWorkspaces();
            showToast(`${workspace.name} 检查完成`, `存在：${workspace.exists ? '是' : '否'} · 文件夹：${workspace.directory ? '是' : '否'}`);
          } catch (error) { showToast('工作区检查失败', error.message); }
          finally { check.disabled = false; }
          return;
        }
        workspace.exists = true;
        workspace.directory = true;
        workspace.lastCheck = '刚刚';
        renderAll();
        showToast(`${workspace.name} 检查通过`, '展示数据已更新。');
      });
      const makeDefault = node('button', { className: 'button secondary compact', type: 'button', text: workspace.default ? '当前默认' : '设为默认', disabled: workspace.default });
      makeDefault.addEventListener('click', () => {
        if (state.live) return showToast('真实工作区为只读模式', '请在正式 /admin 管理台中设置默认工作区。');
        state.workspaces.forEach((item) => { item.default = item === workspace; });
        renderAll();
        showToast('默认工作区已更新', workspace.name);
      });
      const toggle = node('button', { className: 'button secondary compact', type: 'button', text: workspace.enabled ? '停用' : '启用', disabled: workspace.default && workspace.enabled });
      toggle.addEventListener('click', () => {
        if (state.live) return showToast('真实工作区为只读模式', '展示页不会修改真实目录。');
        workspace.enabled = !workspace.enabled;
        renderAll();
        showToast(`${workspace.name}${workspace.enabled ? '已启用' : '已停用'}`, workspace.default ? '默认工作区不能停用。' : '展示状态已更新。');
      });
      actions.append(check, makeDefault, toggle);
      card.append(head, node('p', { text: '会话初始化后绑定该目录；当前目录、进程、输出与项目指令均在此边界内独立。' }), meta, actions);
      root.append(card);
    });
  }

  function filteredConversations() {
    const query = state.conversationQuery.toLowerCase();
    return state.conversations.filter((item) => item.workspace_id === state.conversationWorkspace && (!query || `${item.title} ${item.preview}`.toLowerCase().includes(query)));
  }

  function renderConversationList() {
    const select = $('#conversationWorkspace');
    select.replaceChildren();
    state.workspaces.filter((item) => item.enabled).forEach((workspace) => select.append(node('option', { text: workspace.name, value: workspace.id })));
    if (!state.workspaces.some((item) => item.id === state.conversationWorkspace && item.enabled)) state.conversationWorkspace = defaultWorkspace()?.id || '';
    select.value = state.conversationWorkspace;
    const root = $('#conversationList');
    root.replaceChildren();
    const items = filteredConversations();
    items.forEach((item) => {
      const button = node('button', { className: `conversation-item ${state.selectedConversation === item.conversation_id ? 'active' : ''}`, type: 'button' });
      button.append(node('strong', { text: item.title }), node('p', { text: item.preview }), node('span', { className: 'conversation-item-meta' }, [node('span', { text: item.updated_at || '' }), node('span', { text: `${item.message_count || 0} 条消息` })]));
      button.addEventListener('click', async () => {
        state.selectedConversation = item.conversation_id;
        renderConversationList();
        if (state.live && !item.messages) {
          try {
            const payload = await apiRequest(`/chat/conversations/${encodeURIComponent(item.workspace_id)}/${encodeURIComponent(item.conversation_id)}?message_page=1&message_page_size=50&context_page=1&context_page_size=20`);
            item.messages = payload.messages || [];
            item.contexts = payload.contexts || payload.context || [];
          } catch (error) { showToast('读取会话详情失败', error.message); }
        }
        renderConversationDetail(item);
      });
      root.append(button);
    });
    if (!items.length) root.append(node('div', { className: 'empty-state' }, [node('span', {}, [createIcon('search')]), node('h2', { text: '没有会话摘要' }), node('p', { text: '当前工作区或搜索条件没有结果。' })]));
    $('#conversationPageLabel').textContent = `第 ${state.conversationPage} 页`;
  }

  function renderConversationDetail(item) {
    const root = $('#conversationDetail');
    root.replaceChildren();
    if (!item) {
      root.append(node('div', { className: 'empty-state' }, [node('span', {}, [createIcon('message')]), node('h2', { text: '选择一条会话' }), node('p', { text: '只有选择摘要后，才会加载消息正文和持久化上下文。' })]));
      return;
    }
    const workspace = state.workspaces.find((candidate) => candidate.id === item.workspace_id);
    const head = node('header', { className: 'conversation-detail-head' });
    const copy = node('div', {}, [node('h2', { text: item.title }), node('p', { text: `${workspace?.name || item.workspace_id} · ${item.message_count || 0} 条消息 · ${item.updated_at || ''}` })]);
    const actions = node('div', { className: 'button-row' });
    const copyId = node('button', { className: 'button secondary compact', type: 'button', text: '复制恢复信息' });
    copyId.addEventListener('click', () => {
      navigator.clipboard?.writeText(`${item.workspace_id}/${item.conversation_id}`).catch(() => {});
      showToast('恢复信息已复制', `${item.workspace_id}/${item.conversation_id}`);
    });
    actions.append(statusBadge('按需详情', 'purple'), copyId);
    head.append(copy, actions);
    const messages = node('div', { className: 'message-list' });
    (item.messages || []).forEach((message) => {
      const article = node('article', { className: `message-bubble ${message.role === 'user' ? 'user' : ''}` });
      article.append(node('header', {}, [node('strong', { text: message.role === 'user' ? '用户' : (message.role || 'assistant') }), node('time', { text: message.created_at || '' })]), node('p', { text: typeof message.content === 'string' ? message.content : JSON.stringify(message.content) }));
      messages.append(article);
    });
    if (!(item.messages || []).length) messages.append(node('div', { className: 'empty-state' }, [node('span', {}, [createIcon('message')]), node('h2', { text: '本页没有消息正文' }), node('p', { text: '摘要存在，但当前详情页为空。' })]));
    root.append(head, messages);
  }

  function renderConversations() {
    renderConversationList();
    const item = state.conversations.find((candidate) => candidate.conversation_id === state.selectedConversation);
    renderConversationDetail(item || null);
  }

  function renderSettings() {
    const persisted = state.settings.persisted;
    $('#settingsHost').value = persisted.host || '';
    $('#settingsPort').value = persisted.port || '';
    $('#settingsPermission').value = persisted.permission_mode || 'safe';
    $('#settingsShellEnv').value = persisted.shell_env || 'core';
    $('#settingsOauthUrl').value = persisted.oauth_server_url || '';
    $('#settingsOrigins').value = (persisted.allowed_origins || []).join('\n');
    $('#settingsOauthCompatibility').checked = Boolean(persisted.oauth_compatibility_mode);
    $('#fakeReadonlyToggle').checked = Boolean(persisted.dangerously_fake_readonly_annotations);
    $('#settingsRevision').textContent = state.settings.revision || '—';
    $('#settingsConflictAlert').hidden = !state.settingsConflict;
    const root = $('#settingsPendingList');
    root.replaceChildren();
    const pending = state.settings.pending_restart || [];
    pending.forEach((field) => root.append(node('li', {}, [node('span', { text: field }), node('code', { text: '已保存，等待重启' })])));
    if (!pending.length) root.append(node('li', {}, [node('span', { text: '没有等待重启的字段' }), node('code', { text: '已同步' })]));
  }

  function renderOAuthTable(items, tab) {
    const panel = node('article', { className: 'panel toolbar-panel' });
    const labels = tab === 'audit' ? ['事件标识', '动作', '操作者', '时间'] : ['资源标识', '客户端', '工作区 / 状态', '时间'];
    const root = node('div', { className: 'data-table' });
    root.append(node('div', { className: 'table-header', style: '' }, labels.map((text) => node('span', { text }))));
    items.forEach((item) => {
      const row = node('div', { className: 'table-row' });
      if (tab === 'audit') row.append(node('span', { className: 'mono', text: item.id }), node('span', { text: item.action }), node('span', { text: item.actor }), node('span', { text: item.created_at }), node('span'), node('span'));
      else row.append(node('span', { className: 'mono', text: item.id }), node('span', { text: item.client_id || '—' }), node('span', { text: item.workspace_id || item.status || '—' }), node('span', { text: item.expires_at || item.rotated_at || item.created_at || '' }), node('span'), node('span'));
      root.append(row);
    });
    panel.append(root);
    return panel;
  }

  function renderOAuth() {
    $$('#oauthTabs button').forEach((button) => {
      const active = button.dataset.oauthTab === state.oauthTab;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    const root = $('#oauthContent');
    root.replaceChildren();
    if (state.oauthTab === 'clients') {
      const grid = node('div', { className: 'oauth-client-grid' });
      state.oauth.clients.forEach((client) => {
        const card = node('article', { className: 'oauth-card' });
        const head = node('div', { className: 'oauth-card-head' });
        head.append(node('div', {}, [node('h2', { text: client.name || client.client_id }), node('span', { className: 'mono', text: client.client_id })]), statusBadge(client.enabled ? '启用' : '停用', client.enabled ? 'good' : 'danger'));
        const meta = node('div', { className: 'oauth-meta' });
        meta.append(node('div', {}, [node('span', { text: '授权登录' }), node('strong', { text: client.authorize_login?.mode === 'client' ? '客户端专属密码' : '全局密码' })]), node('div', {}, [node('span', { text: '创建时间' }), node('strong', { text: client.created_at || '—' })]));
        const allowlist = node('div', { className: 'workspace-allowlist' });
        (client.workspace_ids || []).forEach((id) => allowlist.append(node('span', { className: 'workspace-chip', text: id })));
        if (!(client.workspace_ids || []).length) allowlist.append(statusBadge('未分配工作区', 'warning'));
        const actions = node('div', { className: 'oauth-card-actions' });
        const password = node('button', { className: 'button secondary compact', type: 'button', text: client.authorize_login?.mode === 'client' ? '轮换专属密码' : '设置专属密码' });
        password.addEventListener('click', () => state.live ? showToast('真实 OAuth 为只读模式', '请在正式 /admin 管理台中修改客户端密码。') : showToast('密码表单已准备', '正式实现中该操作立即生效，且不会回显旧值。'));
        const workspace = node('button', { className: 'button primary compact', type: 'button', text: '编辑工作区权限' });
        workspace.addEventListener('click', () => {
          if (state.live) return showToast('真实 OAuth 为只读模式', '展示页不会修改客户端允许列表。');
          const defaultId = defaultWorkspace()?.id;
          if (defaultId && !client.workspace_ids.includes(defaultId)) client.workspace_ids.push(defaultId);
          renderAll();
          showToast('工作区允许列表已更新', `已允许 ${client.client_id} 访问 ${defaultId || '默认工作区'}。`);
        });
        actions.append(password, workspace);
        card.append(head, node('p', { text: '客户端密钥、摘要、令牌材料和内部保险箱引用均不会显示。' }), meta, allowlist, actions);
        grid.append(card);
      });
      root.append(grid);
      return;
    }
    root.append(renderOAuthTable(state.oauth[state.oauthTab] || [], state.oauthTab));
  }

  function renderVault() {
    const root = $('#vaultList');
    root.replaceChildren();
    state.vault.forEach((secret) => {
      const row = node('div', { className: 'vault-row' });
      const copy = node('span', {}, [node('strong', { text: secret.name }), node('small', { text: secret.usage || '凭据保险箱条目' })]);
      const remove = node('button', { className: 'button secondary compact', type: 'button', text: secret.protected ? '活动中' : '删除', disabled: secret.protected });
      remove.addEventListener('click', async () => {
        if (state.live) return showToast('真实凭据保险箱为只读模式', '展示页不会删除真实凭据。');
        if (!await confirmAction('删除凭据名称', `名称：${secret.name}。值本身不会显示。`)) return;
        state.vault = state.vault.filter((item) => item !== secret);
        renderVault();
        showToast('凭据名称已删除', secret.name);
      });
      row.append(node('span', {}, [createIcon('key', 'icon icon-sm')]), copy, remove);
      root.append(row);
    });
    if (!state.vault.length) root.append(node('div', { className: 'empty-state' }, [node('span', {}, [createIcon('key')]), node('h2', { text: '凭据保险箱中没有名称' }), node('p', { text: '可以通过右侧表单设置第一个值。' })]));
  }

  const logComponentLabels = {
    admin: '管理接口',
    gateway: '工具网关',
    runtime: '运行时',
    oauth: 'OAuth',
    vault: '凭据保险箱',
  };

  function formatLogTime(timestamp, withDate = false) {
    const date = new Date(timestamp);
    return withDate
      ? date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
      : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }

  function filteredLogs() {
    const limits = { '15m': 15 * 60 * 1000, '1h': 60 * 60 * 1000, '24h': 24 * 60 * 60 * 1000 };
    const query = state.logFilters.query.trim().toLowerCase();
    const now = Date.now();
    return state.logs
      .filter((item) => state.logFilters.time === 'all' || now - item.timestamp <= limits[state.logFilters.time])
      .filter((item) => state.logFilters.level === 'all' || item.level === state.logFilters.level)
      .filter((item) => state.logFilters.component === 'all' || item.component === state.logFilters.component)
      .filter((item) => {
        if (!query) return true;
        const context = Object.entries(item.context || {}).map(([key, value]) => `${key} ${value}`).join(' ');
        return `${item.event} ${item.message} ${item.component} ${context}`.toLowerCase().includes(query);
      })
      .sort((a, b) => b.timestamp - a.timestamp);
  }

  function renderLogSummary() {
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    const recent = state.logs.filter((item) => item.timestamp >= cutoff);
    const latest = [...state.logs].sort((a, b) => b.timestamp - a.timestamp)[0];
    $('#logTotalCount').textContent = String(recent.length);
    $('#logErrorCount').textContent = String(recent.filter((item) => item.level === 'error').length);
    $('#logWarningCount').textContent = String(recent.filter((item) => item.level === 'warn').length);
    $('#logLastUpdated').textContent = latest ? formatLogTime(latest.timestamp) : '—';
  }

  function renderLogDetail(log) {
    const root = $('#logDetail');
    root.replaceChildren();
    if (!log) {
      root.append(node('div', { className: 'log-detail-empty' }, [
        node('span', {}, [createIcon('file-text')]),
        node('h3', { text: '选择一条日志' }),
        node('p', { text: '点击左侧记录后查看完整消息和脱敏上下文。' }),
      ]));
      return;
    }
    const head = node('div', { className: 'log-detail-head' });
    head.append(
      node('div', {}, [node('h3', { text: log.event }), node('p', { text: `${logComponentLabels[log.component] || log.component} · ${formatLogTime(log.timestamp, true)}` })]),
      node('span', { className: `log-level ${log.level}`, text: log.level }),
    );
    const message = node('div', { className: 'log-message-block' }, [node('span', { text: '完整消息' }), node('p', { text: log.message })]);
    const context = node('dl', { className: 'log-context-list' });
    Object.entries(log.context || {}).forEach(([key, value]) => {
      const display = typeof value === 'object' ? JSON.stringify(value) : String(value);
      context.append(node('div', {}, [node('dt', { text: key }), node('dd', { text: display })]));
    });
    root.append(head, message, context);
  }

  function renderLogViewer() {
    $('#logTimeFilter').value = state.logFilters.time;
    $('#logLevelFilter').value = state.logFilters.level;
    $('#logComponentFilter').value = state.logFilters.component;
    $('#logSearchInput').value = state.logFilters.query;
    const items = filteredLogs();
    if (state.selectedLogId && !items.some((item) => item.id === state.selectedLogId)) state.selectedLogId = null;
    $('#logResultCount').textContent = `${items.length} 条结果`;
    const root = $('#logList');
    root.replaceChildren(node('div', { className: 'log-list-header' }, ['时间', '级别', '组件', '事件与摘要'].map((text) => node('span', { text }))));
    items.forEach((log) => {
      const row = node('button', { className: `log-row ${state.selectedLogId === log.id ? 'active' : ''}`, type: 'button' });
      row.append(
        node('time', { text: formatLogTime(log.timestamp), attrs: { datetime: new Date(log.timestamp).toISOString() } }),
        node('span', { className: `log-level ${log.level}`, text: log.level }),
        node('span', { className: 'log-component', text: logComponentLabels[log.component] || log.component }),
        node('span', { className: 'log-row-copy' }, [node('strong', { text: log.event }), node('small', { text: log.message })]),
      );
      row.addEventListener('click', () => {
        state.selectedLogId = log.id;
        renderLogViewer();
      });
      root.append(row);
    });
    if (!items.length) root.append(node('div', { className: 'empty-state' }, [node('span', {}, [createIcon('search')]), node('h2', { text: '没有匹配的日志' }), node('p', { text: '调整时间范围、级别、组件或关键词。' })]));
    renderLogDetail(items.find((item) => item.id === state.selectedLogId) || null);
  }

  function renderSystem() {
    const root = $('#systemGrid');
    root.replaceChildren();
    const cards = [
      ['server', '管理接口', state.system.admin_api ? '正常' : '不可用', '专用管理令牌权限面', state.system.admin_api],
      ['server', '工具网关运行时', state.system.gateway ? '运行中' : '未配置', `${state.system.exposure_count} 个公开工具定义`, state.system.gateway],
      ['shield', '凭据保险箱', state.system.vault ? '已启用' : '未启用', '值不会通过管理接口返回', state.system.vault],
      ['activity', '权限模式', state.system.permission_mode, '权限模式不改变固定工具目录', true],
    ];
    cards.forEach(([icon, title, value, detail, ok]) => {
      const card = node('article', { className: 'system-card' });
      card.append(node('div', { className: 'system-card-head' }, [node('span', { className: 'system-card-icon' }, [createIcon(icon)]), statusBadge(ok ? '正常' : '注意', ok ? 'good' : 'warning')]), node('strong', { text: value }), node('p', { text: `${title} · ${detail}` }));
      root.append(card);
    });
    $('#telemetryMode').textContent = state.system.telemetry;
    const pending = state.settings.pending_restart.length + (state.gatewayRestartRequired ? 1 : 0);
    const impact = $('#restartImpact');
    $('.restart-count', impact).textContent = String(pending);
    $('strong', impact).textContent = pending ? `${pending} 项等待生效` : '没有等待生效的项目';
    $('p', impact).textContent = pending ? '工具网关与部分服务器设置需要新建运行时或服务重启。' : '当前生效配置与持久化配置已同步。';
    renderLogSummary();
  }

  function renderCurrentPage() {
    if (state.route === 'overview') renderOverview();
    if (state.route === 'connections') renderConnections();
    if (state.route === 'workspaces') renderWorkspaces();
    if (state.route === 'conversations') renderConversations();
    if (state.route === 'settings') renderSettings();
    if (state.route === 'oauth') renderOAuth();
    if (state.route === 'vault') renderVault();
    if (state.route === 'system') renderSystem();
  }

  function renderGlobalChrome() {
    $('#navConnectionCount').textContent = String(state.connections.length);
    $('#navWorkspaceCount').textContent = String(state.workspaces.length);
    $('#oauthNavAlert').hidden = !state.oauth.clients.some((client) => client.enabled && client.workspace_ids.length === 0);
    $('#environmentLabel').textContent = state.live ? '真实数据 · 只读' : '演示数据';
    $('#environmentDot').className = `status-dot ${state.live ? 'good' : 'warning'}`;
    $('#connectApiButton').querySelector('span').textContent = state.live ? '已连接管理接口' : '连接管理接口';
  }

  function renderAll() {
    renderGlobalChrome();
    renderCurrentPage();
  }

  function openDialog(dialog) {
    if (!dialog.open) dialog.showModal();
  }

  function togglePassword(input, button) {
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    button.replaceChildren(createIcon(show ? 'eyeoff' : 'eye'));
  }

  async function apiRequest(path) {
    const response = await fetch(`${state.apiBase}${path}`, {
      method: 'GET',
      headers: { Authorization: `Bearer ${state.apiToken}`, Accept: 'application/json' },
      cache: 'no-store',
      credentials: 'omit',
    });
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    if (!response.ok) throw new Error(payload?.error?.message || payload?.detail || `${response.status} ${response.statusText}`);
    return payload || {};
  }

  function mapGateway(payload) {
    const servers = payload.persisted?.servers || {};
    state.connections = Object.entries(servers).map(([alias, item]) => ({
      alias,
      transport: item.transport || 'stdio',
      command: item.command || '',
      url: item.url || '',
      mode: item.expose_mode || 'direct',
      enabled: item.enabled !== false,
      pins: item.pinned_tools || [],
      tags: item.tags || [],
      tools: payload.active_status?.servers?.[alias]?.tool_count || 0,
      lastUsed: '真实配置',
    }));
    state.gatewayRestartRequired = Boolean(payload.restart_required);
    state.system.exposure_count = payload.active_status?.exposure_report?.catalog?.count || payload.active_status?.exposure_report?.direct?.count || 0;
  }

  function mapWorkspaces(payload) {
    state.workspaces = (payload.workspace_catalog || []).map((item) => ({
      id: item.id,
      name: item.name || item.id,
      root: item.root || '',
      enabled: item.enabled !== false,
      default: Boolean(item.default),
      exists: true,
      directory: true,
      lastCheck: '尚未检查',
    }));
  }

  function mapSettings(payload) {
    state.settings.active = payload.active || {};
    state.settings.persisted = payload.persisted || payload.active || {};
    state.settings.pending_restart = Array.isArray(payload.pending_restart) ? payload.pending_restart : Object.keys(payload.pending_restart || {});
    state.settings.revision = payload.persisted_revision || payload.revision || '—';
    state.system.permission_mode = state.settings.active.permission_mode || state.settings.persisted.permission_mode || 'safe';
    state.system.api_url = state.apiBase;
  }

  function mapClients(payload) {
    state.oauth.clients = (payload.items || []).map((item) => ({
      client_id: item.client_id || item.id,
      name: item.client_name || item.name || item.client_id || item.id,
      enabled: item.enabled !== false,
      authorize_login: item.authorize_login || { mode: 'global', configured: false },
      workspace_ids: item.workspace_access?.workspace_ids || item.workspace_ids || [],
      created_at: item.created_at || '',
    }));
  }

  async function loadLiveData() {
    if (state.loading) return;
    state.loading = true;
    $('#environmentLabel').textContent = '连接中…';
    try {
      const [status, settings, gateway, workspaces, secrets, clients] = await Promise.all([
        apiRequest('/status'), apiRequest('/settings'), apiRequest('/gateway'), apiRequest('/workspaces'), apiRequest('/secrets'), apiRequest('/oauth/clients'),
      ]);
      mapSettings(settings);
      mapGateway(gateway);
      mapWorkspaces(workspaces);
      mapClients(clients);
      state.vault = (secrets.secrets || []).map((item) => ({ name: item.name, usage: item.usage || 'Secret Vault 条目', protected: item.usage === 'oauth_authorization_password' }));
      state.system.admin_api = true;
      state.system.gateway = status.gateway?.available !== false;
      state.system.runtime = true;
      state.system.vault = Boolean(status.vault?.enabled);
      state.system.telemetry = status.telemetry?.mode || 'unknown';
      state.system.config_version = settings.persisted_revision?.slice(0, 14) || gateway.persisted_revision?.slice(0, 14) || '真实配置';
      const workspace = defaultWorkspace();
      if (workspace) {
        const [check, conversations] = await Promise.all([
          apiRequest(`/workspaces/${encodeURIComponent(workspace.id)}/check`).catch(() => null),
          apiRequest(`/chat/conversations?workspace_id=${encodeURIComponent(workspace.id)}&page=1&page_size=20`).catch(() => ({ items: [] })),
        ]);
        if (check) {
          const info = check.check || check;
          workspace.exists = Boolean(info.exists);
          workspace.directory = Boolean(info.is_directory);
          workspace.lastCheck = '刚刚';
        }
        state.conversations = (conversations.items || []).map((item) => ({ ...item, title: item.title || item.conversation_id, preview: item.preview || '无摘要' }));
        state.conversationWorkspace = workspace.id;
      }
      state.live = true;
      state.selectedConversation = null;
      renderAll();
      showToast('已连接真实 Admin API', '页面进入只读模式，写入和删除操作不会发送到服务器。');
    } finally {
      state.loading = false;
    }
  }

  document.addEventListener('click', (event) => {
    const routeControl = event.target.closest('[data-route]');
    if (routeControl) {
      event.preventDefault();
      setRoute(routeControl.dataset.route);
      return;
    }
    if (event.target.closest('[data-close-dialog]')) {
      event.target.closest('dialog')?.close();
      return;
    }
    const actionControl = event.target.closest('[data-action]');
    if (!actionControl) return;
    const action = actionControl.dataset.action;
    if (action === 'new-connection') openDialog($('#connectionDialog'));
    if (action === 'new-workspace') openDialog($('#workspaceDialog'));
    if (action === 'open-logs') {
      renderLogViewer();
      openDialog($('#logDialog'));
    }
    if (action === 'open-mobile-more') document.body.classList.add('sidebar-open');
    if (action === 'profile') showToast('Admin 专用权限', '普通 MCP bearer 和 OAuth access token 不会自动获得管理员权限。');
    if (action === 'refresh' || action.startsWith('refresh-')) {
      if (state.live) loadLiveData().catch((error) => showToast('刷新失败', error.message));
      else { renderCurrentPage(); showToast('演示数据已刷新', routeMeta[state.route][0]); }
    }
    if (action === 'show-all-tasks') showToast('运维提醒', `当前显示 ${buildTasks().length} 项需要管理员关注的提醒。`);
    if (action === 'check-all-workspaces') {
      state.workspaces.forEach((workspace) => { if (!state.live) { workspace.exists = true; workspace.directory = true; workspace.lastCheck = '刚刚'; } });
      renderAll();
      showToast(state.live ? '请逐项检查真实 Workspace' : '全部 Workspace 检查完成', state.live ? '真实模式会调用每个 Workspace 的只读检查端点。' : '演示数据已更新。');
    }
    if (action === 'save-credential-policy') showToast(state.live ? '真实 Gateway 为只读模式' : '凭据策略已保存', state.live ? '请在正式 /admin 管理台中保存。' : $('#credentialPolicy').selectedOptions[0].textContent);
    if (action === 'reload-settings') { state.settingsConflict = false; renderSettings(); showToast('设置已重新读取', '草稿已与 persisted 配置同步。'); }
    if (action === 'save-settings') {
      if (state.live) return showToast('真实设置为只读模式', '展示页不会发送 PUT /settings。');
      if (state.settingsConflict) return showToast('无法保存', '请先重新读取最新 revision。');
      state.settings.persisted = {
        ...state.settings.persisted,
        host: $('#settingsHost').value,
        port: Number($('#settingsPort').value),
        permission_mode: $('#settingsPermission').value,
        shell_env: $('#settingsShellEnv').value,
        oauth_server_url: $('#settingsOauthUrl').value,
        allowed_origins: $('#settingsOrigins').value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
        oauth_compatibility_mode: $('#settingsOauthCompatibility').checked,
        dangerously_fake_readonly_annotations: $('#fakeReadonlyToggle').checked,
      };
      state.settings.pending_restart = ['host', 'port', 'allowed_origins'];
      state.settings.revision = `rev-demo-${Date.now().toString(36)}`;
      renderAll();
      showToast('设置草稿已保存', 'Active Runtime 保持不变，相关字段等待重启。');
    }
    if (action === 'toggle-vault-password') togglePassword($('#vaultValue'), actionControl);
    if (action === 'toggle-api-password') togglePassword($('#apiToken'), actionControl);
    if (action === 'conversation-prev') { state.conversationPage = Math.max(1, state.conversationPage - 1); renderConversations(); }
    if (action === 'conversation-next') { state.conversationPage += 1; renderConversations(); }
    if (action === 'refresh-logs') {
      state.logs.unshift({
        id: `log-${Date.now()}`,
        timestamp: Date.now(),
        level: 'info',
        component: 'admin',
        event: 'logs.view.refreshed',
        message: '管理员刷新了展示日志查看器。',
        context: { source: 'showcase', backend_endpoint: 'not_configured' },
      });
      renderLogViewer();
      if (state.route === 'system') renderLogSummary();
      showToast('展示日志已刷新', '新增一条本地演示记录；未读取服务器日志文件。');
    }
  });

  $$('#connectionFilters button').forEach((button) => button.addEventListener('click', () => {
    state.connectionFilter = button.dataset.filter;
    $$('#connectionFilters button').forEach((item) => item.classList.toggle('active', item === button));
    renderConnections();
  }));

  $('#connectionSearch').addEventListener('input', (event) => { state.connectionQuery = event.target.value; renderConnections(); });
  $('#conversationSearch').addEventListener('input', (event) => { state.conversationQuery = event.target.value; renderConversations(); });
  $('#conversationWorkspace').addEventListener('change', (event) => { state.conversationWorkspace = event.target.value; state.selectedConversation = null; renderConversations(); });

  $('#themeSelect').addEventListener('change', (event) => {
    applyThemePreference(event.target.value);
    showToast('界面主题已更新', event.target.selectedOptions[0].textContent);
  });

  $('#logTimeFilter').addEventListener('change', (event) => { state.logFilters.time = event.target.value; renderLogViewer(); });
  $('#logLevelFilter').addEventListener('change', (event) => { state.logFilters.level = event.target.value; renderLogViewer(); });
  $('#logComponentFilter').addEventListener('change', (event) => { state.logFilters.component = event.target.value; renderLogViewer(); });
  $('#logSearchInput').addEventListener('input', (event) => { state.logFilters.query = event.target.value; renderLogViewer(); });

  $$('#oauthTabs button').forEach((button) => button.addEventListener('click', () => { state.oauthTab = button.dataset.oauthTab; renderOAuth(); }));

  $('#connectApiButton').addEventListener('click', () => {
    if (state.live) return showToast('已连接真实 Admin API', state.apiBase);
    openDialog($('#apiDialog'));
  });

  $('#menuButton').addEventListener('click', () => {
    const open = document.body.classList.toggle('sidebar-open');
    $('#menuButton').setAttribute('aria-expanded', String(open));
  });
  $('#sidebarBackdrop').addEventListener('click', closeSidebar);
  window.addEventListener('resize', () => { if (window.innerWidth > 1020) closeSidebar(); });

  $('#connectionTransport').addEventListener('change', (event) => {
    const http = event.target.value === 'streamable_http';
    $$('[data-transport="stdio"]').forEach((field) => { field.hidden = http; });
    $$('[data-transport="http"]').forEach((field) => { field.hidden = !http; });
  });

  $('#connectionForm').addEventListener('submit', (event) => {
    event.preventDefault();
    if (state.live) return showToast('真实 Gateway 为只读模式', '展示页不会发送 Gateway 写入。');
    const alias = $('#connectionAlias').value.trim();
    if (state.connections.some((item) => item.alias === alias)) return showToast('连接别名已存在', alias);
    const transport = $('#connectionTransport').value;
    state.connections.push({
      alias,
      transport,
      command: $('#connectionCommand').value.trim(),
      url: $('#connectionUrl').value.trim(),
      mode: $('#connectionMode').value,
      enabled: $('#connectionEnabled').checked,
      pins: $('#connectionPins').value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
      tags: $('#connectionTags').value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
      tools: 0,
      lastUsed: '尚未启动',
    });
    state.gatewayRestartRequired = true;
    event.target.reset();
    $('#connectionEnabled').checked = true;
    $('#connectionTimeout').value = 30000;
    $('#connectionDialog').close();
    renderAll();
    setRoute('connections');
    showToast('连接已加入展示草稿', '新建 Runtime 或服务重启后才会生效。');
  });

  $('#workspaceForm').addEventListener('submit', (event) => {
    event.preventDefault();
    if (state.live) return showToast('真实 Workspace 为只读模式', '展示页不会发送 Workspace 写入。');
    const id = $('#workspaceId').value.trim();
    if (state.workspaces.some((item) => item.id === id)) return showToast('Workspace ID 已存在', id);
    const makeDefault = $('#workspaceDefault').checked;
    if (makeDefault) state.workspaces.forEach((item) => { item.default = false; });
    state.workspaces.push({ id, name: $('#workspaceName').value.trim(), root: $('#workspaceRoot').value.trim(), enabled: $('#workspaceEnabled').checked, default: makeDefault, exists: true, directory: true, lastCheck: '刚刚' });
    event.target.reset();
    $('#workspaceEnabled').checked = true;
    $('#workspaceDialog').close();
    renderAll();
    setRoute('workspaces');
    showToast('Workspace 已加入展示数据', id);
  });

  $('#vaultForm').addEventListener('submit', (event) => {
    event.preventDefault();
    if (state.live) return showToast('真实 Vault 为只读模式', '展示页不会发送 Secret 值。');
    const name = $('#vaultName').value.trim();
    const existing = state.vault.find((item) => item.name === name);
    if (existing) existing.usage = existing.usage || 'Secret Vault 条目';
    else state.vault.push({ name, usage: name === 'oauth/authorization-password' ? 'OAuth Authorize 全局密码' : 'Secret Vault 条目', protected: name === 'oauth/authorization-password' });
    $('#vaultValue').value = '';
    $('#vaultName').value = '';
    renderVault();
    showToast(name === 'oauth/authorization-password' ? 'OAuth Authorize 密码已替换' : 'Secret 已设置', '值已从当前输入框清除，不会显示在页面中。');
  });

  $('#apiForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const error = $('#apiDialogError');
    error.hidden = true;
    state.apiBase = $('#apiBaseUrl').value.trim().replace(/\/+$/, '');
    state.apiToken = $('#apiToken').value;
    const submit = event.submitter;
    submit.disabled = true;
    submit.textContent = '正在连接…';
    try {
      await loadLiveData();
      $('#apiToken').value = '';
      $('#apiDialog').close();
    } catch (connectError) {
      error.textContent = connectError instanceof TypeError ? '无法连接服务。直接用 file:// 打开时浏览器可能阻止跨域请求；请用本地 HTTP 服务打开展示页，并确认 Admin API 的 allowed origins。' : connectError.message;
      error.hidden = false;
    } finally {
      submit.disabled = false;
      submit.textContent = '连接并读取';
    }
  });

  $$('dialog').forEach((dialog) => dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  }));

  const systemThemeMedia = window.matchMedia('(prefers-color-scheme: dark)');
  const syncSystemTheme = () => { if (state.theme === 'system') applyThemePreference('system', false); };
  if (typeof systemThemeMedia.addEventListener === 'function') systemThemeMedia.addEventListener('change', syncSystemTheme);
  else if (typeof systemThemeMedia.addListener === 'function') systemThemeMedia.addListener(syncSystemTheme);

  applyThemePreference(state.theme, false);
  setRoute('overview', false);
  renderAll();
})();
