const i18n = globalThis.McpI18n || {
  getLocale: () => 'zh-CN',
  initI18n: () => {},
  translateText: (value) => String(value ?? ''),
};
const translateUi = (value) => i18n.translateText(value, i18n.getLocale());

class ApiError extends Error {
  constructor(status, payload, message) {
    super(message || payload?.error?.message || `Admin request failed with HTTP ${status}.`);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

const FORBIDDEN_RESPONSE_KEYS = new Set([
  'client_secret',
  'client_secret_digest',
  'refresh_token',
  'access_token',
  'token_hash',
  'signing_secret',
  'secret_ref',
]);

function sensitiveKey(key) {
  const normalized = String(key || '').toLowerCase();
  return FORBIDDEN_RESPONSE_KEYS.has(normalized)
    || normalized.endsWith('_secret_ref')
    || normalized.endsWith('_digest')
    || normalized.endsWith('_hash');
}

function sanitizeAdminValue(value) {
  if (Array.isArray(value)) return value.map(sanitizeAdminValue);
  if (!value || typeof value !== 'object') return value;
  const result = {};
  for (const [key, child] of Object.entries(value)) {
    if (sensitiveKey(key)) continue;
    if (key === 'source' && child === 'secret_ref') {
      result.source = 'configured credential';
      continue;
    }
    result[key] = sanitizeAdminValue(child);
  }
  return result;
}

function containsCredentialControl(value) {
  if (Array.isArray(value)) return value.some(containsCredentialControl);
  if (!value || typeof value !== 'object') return false;
  return Object.entries(value).some(([key, child]) => {
    const normalized = key.toLowerCase();
    if (normalized === 'secret_ref' || normalized === 'env_ref') return true;
    if (/authorization|api[-_]?key|token|password|credential|secret/.test(normalized)) return true;
    return containsCredentialControl(child);
  });
}

function restartImpactCount(settings, gateway) {
  const settingsPending = Array.isArray(settings?.pendingRestart)
    ? settings.pendingRestart.length
    : 0;
  return settingsPending + (gateway?.restart_required ? 1 : 0);
}

function gatewayServerTemplate(alias = 'new-upstream') {
  return {
    servers: {
      [alias]: {
        transport: 'streamable_http',
        url: 'http://127.0.0.1:9000/mcp',
        enabled: true,
        expose_mode: 'broker',
        pinned_tools: [],
        tags: [],
        tool_policy: {},
      },
    },
    tool_search: { custom_synonyms: {} },
  };
}

function lineValues(value) {
  return String(value || '').split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function argumentLineValues(value) {
  return lineValues(value).map((item) => {
    const first = item[0];
    return item.length >= 2 && first === item[item.length - 1] && (first === '"' || first === "'")
      ? item.slice(1, -1)
      : item;
  });
}

function parseGatewayEnvironment(value) {
  const result = {};
  for (const row of lineValues(value)) {
    const separator = row.indexOf('=');
    if (separator <= 0) throw new Error(`环境变量必须使用 KEY=value 格式：${row}`);
    const key = row.slice(0, separator).trim();
    const raw = row.slice(separator + 1).trim();
    if (!key) throw new Error(`环境变量必须使用 KEY=value 格式：${row}`);
    if (raw.startsWith('secret:')) {
      const secretRef = raw.slice('secret:'.length).trim();
      if (!secretRef) throw new Error(`Secret 引用不能为空：${key}`);
      result[key] = { secret_ref: secretRef };
    } else if (raw.startsWith('env:')) {
      const envRef = raw.slice('env:'.length).trim();
      if (!envRef) throw new Error(`环境变量引用不能为空：${key}`);
      result[key] = { env_ref: envRef };
    } else {
      result[key] = raw;
    }
  }
  return result;
}

function gatewayServerFromForm(values = {}) {
  const alias = String(values.alias || '').trim();
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(alias) || alias.includes('__')) {
    throw new Error('连接 alias 必须是 1–64 位字母、数字、下划线或连字符，且不能包含双下划线。');
  }
  const transport = values.transport === 'streamable_http' ? 'streamable_http' : 'stdio';
  const timeoutMs = Number(values.timeoutMs || 30000);
  if (!Number.isInteger(timeoutMs) || timeoutMs <= 0) throw new Error('等待时间必须是正整数。');
  const config = {
    transport,
    enabled: values.enabled !== false,
    expose_mode: values.exposeMode === 'direct' ? 'direct' : 'broker',
    pinned_tools: lineValues(values.pinnedTools),
    include_tools: lineValues(values.includeTools),
    exclude_tools: lineValues(values.excludeTools),
    tags: lineValues(values.tags),
    timeout_ms: timeoutMs,
  };
  if (transport === 'stdio') {
    config.command = String(values.command || '').trim();
    if (!config.command) throw new Error('本地 stdio 连接必须填写启动命令。');
    config.args = argumentLineValues(values.args);
    const environment = parseGatewayEnvironment(values.environment);
    if (Object.keys(environment).length) config.env = environment;
  } else {
    config.url = String(values.url || '').trim();
    if (!config.url) throw new Error('远程 Streamable HTTP 连接必须填写服务地址。');
    const authorizationEnv = String(values.authorizationEnv || '').trim();
    if (authorizationEnv) config.authorization_env = authorizationEnv;
  }
  return { alias, config };
}

function setAuthenticationUi(documentRef, authenticated) {
  const form = documentRef.getElementById('authForm');
  const connected = documentRef.getElementById('authConnected');
  const token = documentRef.getElementById('adminToken');
  const openButton = documentRef.getElementById('openAuthDialog');
  const environmentLabel = documentRef.getElementById('environmentLabel');
  const environmentDot = documentRef.getElementById('environmentDot');
  if (form) form.hidden = Boolean(authenticated);
  if (connected) connected.hidden = !authenticated;
  if (openButton) openButton.hidden = Boolean(authenticated);
  if (environmentLabel) environmentLabel.textContent = authenticated ? '真实数据 · 已连接' : '未连接';
  if (environmentDot) environmentDot.className = `status-dot ${authenticated ? 'good' : 'warning'}`;
  if (authenticated && token) {
    token.value = '';
    setPasswordVisibility(token, documentRef.getElementById('adminTokenToggle'), false);
  }
}

function resolveTheme(preference) {
  if (preference === 'light' || preference === 'dark') return preference;
  return globalThis.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function applyThemePreference(documentRef, preference) {
  const next = ['system', 'light', 'dark'].includes(preference) ? preference : 'system';
  const resolved = resolveTheme(next);
  documentRef.documentElement.dataset.themePreference = next;
  documentRef.documentElement.dataset.theme = resolved;
  documentRef.documentElement.style.colorScheme = resolved;
  const select = documentRef.getElementById('themeSelect');
  if (select) select.value = next;
  const use = documentRef.querySelector('#themePickerIcon use');
  if (use) use.setAttribute('href', next === 'light' ? '#i-sun' : next === 'dark' ? '#i-moon' : '#i-monitor');
  const meta = documentRef.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = resolved === 'dark' ? '#080d1b' : '#f4f7fb';
  const picker = documentRef.getElementById('themePicker');
  if (picker) picker.title = next === 'system' ? '界面主题：跟随系统' : next === 'dark' ? '界面主题：夜间模式' : '界面主题：日间模式';
  return next;
}

function initThemeControls(documentRef) {
  let preference = applyThemePreference(documentRef, 'system');
  const select = documentRef.getElementById('themeSelect');
  select?.addEventListener('change', () => { preference = applyThemePreference(documentRef, select.value); });
  const media = globalThis.matchMedia?.('(prefers-color-scheme: dark)');
  media?.addEventListener?.('change', () => { if (preference === 'system') applyThemePreference(documentRef, 'system'); });
  return preference;
}

function gatewayExposurePreview(documentValue, activeStatus = {}) {
  const servers = documentValue?.servers && typeof documentValue.servers === 'object'
    ? documentValue.servers
    : {};
  const activeServers = new Map(
    (activeStatus?.exposure_report?.servers || []).map((server) => [server.alias, server]),
  );
  return Object.keys(servers).sort().map((alias) => {
    const config = servers[alias] || {};
    const mode = config.expose_mode === 'broker' ? 'broker' : 'direct';
    const include = new Set(Array.isArray(config.include_tools) ? config.include_tools : []);
    const exclude = new Set(Array.isArray(config.exclude_tools) ? config.exclude_tools : []);
    const pinned = new Set(Array.isArray(config.pinned_tools) ? config.pinned_tools : []);
    const active = activeServers.get(alias);
    return {
      alias,
      mode,
      catalog_known: Boolean(active),
      catalog_count: Number.isInteger(active?.catalog_count) ? active.catalog_count : null,
      direct_count: Number.isInteger(active?.direct_count) ? active.direct_count : null,
      broker_only_count: Number.isInteger(active?.broker_only_count) ? active.broker_only_count : null,
      definition_bytes: Number.isInteger(active?.definition_bytes) ? active.definition_bytes : null,
      configured_pins: [...pinned].sort(),
      configured_include: [...include].sort(),
      configured_exclude: [...exclude].sort(),
    };
  });
}

function renderGatewayExposurePreview(container, preview) {
  const documentRef = container.ownerDocument || document;
  container.replaceChildren();
  if (!preview.length) {
    container.append(createNode(documentRef, 'p', { className: 'muted', text: '草稿中没有 MCP 工具连接。' }));
    return;
  }
  for (const item of preview) {
    const card = createNode(documentRef, 'article', { className: 'card' });
    const directText = item.catalog_known ? String(item.direct_count) : '未知';
    const brokerText = item.catalog_known ? String(item.broker_only_count) : '未知';
    const catalogText = item.catalog_known ? String(item.catalog_count) : '未知';
    card.append(
      createNode(documentRef, 'h4', { text: `${item.alias} · ${item.mode}` }),
      createNode(documentRef, 'p', { text: `当前 Runtime Direct 工具数：${directText}` }),
      createNode(documentRef, 'p', { text: `当前 Runtime 仅 Broker 可见工具数：${brokerText}` }),
      createNode(documentRef, 'p', { text: `当前 Runtime 工具目录总数：${catalogText}` }),
      createNode(documentRef, 'p', { className: 'muted', text: `配置的置顶工具：${item.configured_pins.join(', ') || '无'}；包含：${item.configured_include.join(', ') || '全部'}；排除：${item.configured_exclude.join(', ') || '无'}` }),
      createNode(documentRef, 'p', { className: 'muted', text: '当前 Runtime 仅提供聚合计数；草稿过滤结果需在新 MCP 会话/Runtime 或服务重启后确认。' }),
    );
    container.append(card);
  }
}

function adminComponentForPath(path) {
  const segment = String(path || '').split('?')[0].split('/').filter(Boolean)[0] || 'status';
  if (segment === 'chat') return 'chat';
  if (segment === 'gateway') return 'gateway';
  if (segment === 'workspaces') return 'workspaces';
  if (segment === 'oauth') return 'oauth';
  if (segment === 'secrets') return 'secrets';
  if (segment === 'settings') return 'settings';
  return 'status';
}

async function parseAdminResponse(response) {
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

async function createAdminSession(adminToken, fetchImpl = globalThis.fetch) {
  const response = await fetchImpl('/admin/api/session', {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ admin_token: String(adminToken || '') }),
    credentials: 'same-origin',
    cache: 'no-store',
  });
  return parseAdminResponse(response);
}

async function restoreAdminSession(fetchImpl = globalThis.fetch) {
  const response = await fetchImpl('/admin/api/session', {
    method: 'GET',
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
    cache: 'no-store',
  });
  return parseAdminResponse(response);
}

async function revokeAdminSession(csrfToken, fetchImpl = globalThis.fetch) {
  const headers = new Headers({ Accept: 'application/json' });
  if (csrfToken) headers.set('X-Admin-CSRF', String(csrfToken));
  const response = await fetchImpl('/admin/api/session', {
    method: 'DELETE',
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
  });
  return parseAdminResponse(response);
}

function createApiClient(getCsrfToken, fetchImpl = globalThis.fetch, onActivity = null) {
  async function request(path, options = {}) {
    const method = options.method || 'GET';
    const startedAt = Date.now();
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    if (!['GET', 'HEAD'].includes(method)) {
      const csrfToken = String(getCsrfToken?.() || '');
      if (csrfToken) headers.set('X-Admin-CSRF', csrfToken);
    }
    try {
      const response = await fetchImpl(`/admin/api${path}`, {
        method,
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        credentials: 'same-origin',
        cache: 'no-store',
      });
      let payload = {};
      try { payload = await response.json(); } catch { payload = {}; }
      onActivity?.({
        timestamp: Date.now(),
        level: response.ok ? 'info' : 'error',
        component: adminComponentForPath(path),
        method,
        path: String(path).split('?')[0],
        status: response.status,
        duration_ms: Math.max(0, Date.now() - startedAt),
        message: response.ok ? 'Admin API request completed.' : 'Admin API request failed.',
      });
      if (!response.ok) throw new ApiError(response.status, payload);
      return payload;
    } catch (error) {
      if (!(error instanceof ApiError)) {
        onActivity?.({
          timestamp: Date.now(), level: 'error', component: adminComponentForPath(path), method,
          path: String(path).split('?')[0], status: 0, duration_ms: Math.max(0, Date.now() - startedAt),
          message: 'Network request failed.',
        });
      }
      throw error;
    }
  }
  return { request };
}

async function fetchOAuthCollection(api, overviewStatus, collection) {
  if (overviewStatus?.oauth?.available === false) {
    return { available: false, items: [] };
  }
  const payload = await api.request(`/oauth/${encodeURIComponent(collection)}`);
  return { ...payload, available: true };
}

function createNode(documentRef, tag, options = {}) {
  const node = documentRef.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = String(options.text);
  if (options.type) node.type = options.type;
  if (options.id) node.id = options.id;
  return node;
}

function createSvgIcon(documentRef, name, className = 'icon') {
  const svg = documentRef.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', className);
  svg.setAttribute('aria-hidden', 'true');
  const use = documentRef.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

function setPasswordVisibility(input, toggle, visible) {
  if (!input || !toggle) return;
  input.type = visible ? 'text' : 'password';
  const label = toggle.querySelector?.('span');
  if (label) label.textContent = visible ? '隐藏' : '显示';
  else toggle.textContent = visible ? '隐藏' : '显示';
  toggle.setAttribute('aria-pressed', visible ? 'true' : 'false');
  toggle.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
}

function wirePasswordVisibilityToggles(documentRef) {
  for (const toggle of documentRef.querySelectorAll('[data-password-toggle]')) {
    const input = documentRef.getElementById(toggle.dataset.passwordTarget || '');
    if (!input) continue;
    setPasswordVisibility(input, toggle, false);
    toggle.addEventListener('click', () => {
      setPasswordVisibility(input, toggle, input.type === 'password');
      input.focus();
    });
  }
}

function appendDefinitionList(documentRef, container, value) {
  const safe = sanitizeAdminValue(value);
  const dl = createNode(documentRef, 'dl', { className: 'compact-definition' });
  for (const [key, child] of Object.entries(safe || {})) {
    if (typeof child === 'object' && child !== null) continue;
    dl.append(
      createNode(documentRef, 'dt', { text: key }),
      createNode(documentRef, 'dd', { text: child ?? '—' }),
    );
  }
  container.append(dl);
}

function renderConversationItems(container, items, onSelect) {
  const documentRef = container.ownerDocument || document;
  container.replaceChildren();
  if (!items?.length) {
    container.append(createNode(documentRef, 'p', { className: 'muted', text: '没有匹配的会话摘要。' }));
    return;
  }
  for (const item of items) {
    const card = createNode(documentRef, 'article', { className: 'conversation-card' });
    const button = createNode(documentRef, 'button', { type: 'button' });
    const title = createNode(documentRef, 'h3', { text: item.title || item.conversation_id || 'Untitled conversation' });
    const identity = createNode(documentRef, 'p', { className: 'muted', text: `${item.workspace_id || '—'} / ${item.conversation_id || '—'}` });
    const preview = createNode(documentRef, 'p', { text: item.preview || '无摘要正文。' });
    const execution = item.execution;
    const counts = createNode(
      documentRef,
      'p',
      {
        className: 'muted',
        text: `Messages: ${item.message_count || 0} · Context: ${item.context_count || 0} · Execution: ${execution ? execution.status : 'none'}`,
      },
    );
    button.append(title, identity, preview, counts);
    button.addEventListener('click', () => onSelect?.(item, button));
    card.append(button);
    container.append(card);
  }
}

function renderConversationDetail(container, payload, handlers = {}) {
  const documentRef = container.ownerDocument || document;
  container.replaceChildren();
  const conversation = payload?.conversation || {};
  const heading = createNode(documentRef, 'div', { className: 'section-heading' });
  const titleWrap = createNode(documentRef, 'div');
  titleWrap.append(
    createNode(documentRef, 'p', { className: 'eyebrow', text: `${conversation.workspace_id || '—'} / ${conversation.conversation_id || '—'}` }),
    createNode(documentRef, 'h3', { text: conversation.title || conversation.conversation_id || 'Conversation detail' }),
  );
  const deleteConversation = createNode(documentRef, 'button', { type: 'button', className: 'danger', text: '删除会话' });
  deleteConversation.addEventListener('click', () => handlers.onDeleteConversation?.(conversation, deleteConversation));
  heading.append(titleWrap, deleteConversation);
  container.append(heading);

  const executions = payload.executions || [];
  if (executions.length) {
    const executionHeading = createNode(documentRef, 'h4', { text: `Agent Execution (${executions.length})` });
    const executionList = createNode(documentRef, 'div', { className: 'compact-list' });
    for (const execution of executions) {
      const card = createNode(documentRef, 'section', { className: 'context-entry' });
      card.append(
        createNode(documentRef, 'p', { className: 'muted', text: `${execution.session_id} · ${execution.status} · ${execution.backend_kind}` }),
        createNode(documentRef, 'pre', { text: `last turn: ${execution.last_turn_id || '—'}` }),
      );
      executionList.append(card);
    }
    container.append(executionHeading, executionList);
  }

  const messagesHeading = createNode(documentRef, 'h4', { text: `Messages (${payload.messages_total || 0})` });
  container.append(messagesHeading);
  for (const message of payload.messages || []) {
    const card = createNode(documentRef, 'section', { className: 'message' });
    const meta = createNode(documentRef, 'p', { className: 'muted', text: `${message.role || 'unknown'} · ${message.message_id || '—'} · ${message.timestamp || '—'}` });
    const content = createNode(documentRef, 'pre', { text: message.content || '' });
    const remove = createNode(documentRef, 'button', { type: 'button', className: 'danger', text: '删除 message' });
    remove.addEventListener('click', () => handlers.onDeleteMessage?.(message, remove));
    card.append(meta, content, remove);
    container.append(card);
  }
  const messagePager = createNode(documentRef, 'div', { className: 'pager' });
  const previousMessages = createNode(documentRef, 'button', { type: 'button', className: 'secondary', text: 'Messages 上一页' });
  previousMessages.disabled = (payload.message_page || 1) <= 1;
  previousMessages.addEventListener('click', () => handlers.onMessagePage?.((payload.message_page || 1) - 1));
  const nextMessages = createNode(documentRef, 'button', { type: 'button', className: 'secondary', text: 'Messages 下一页' });
  nextMessages.disabled = (payload.message_page || 1) * (payload.message_page_size || 100) >= (payload.messages_total || 0);
  nextMessages.addEventListener('click', () => handlers.onMessagePage?.((payload.message_page || 1) + 1));
  messagePager.append(previousMessages, createNode(documentRef, 'span', { text: `第 ${payload.message_page || 1} 页` }), nextMessages);
  container.append(messagePager);

  container.append(createNode(documentRef, 'h4', { text: `Context (${payload.contexts_total || 0})` }));
  for (const entry of payload.contexts || []) {
    const card = createNode(documentRef, 'section', { className: 'context-entry' });
    card.append(
      createNode(documentRef, 'p', { className: 'muted', text: `${entry.kind || 'context'} · ${entry.context_id || '—'} · ${entry.timestamp || '—'}` }),
      createNode(documentRef, 'pre', { text: entry.content || '' }),
    );
    const remove = createNode(documentRef, 'button', { type: 'button', className: 'danger', text: '删除 context' });
    remove.addEventListener('click', () => handlers.onDeleteContext?.(entry, remove));
    card.append(remove);
    container.append(card);
  }
  const contextPager = createNode(documentRef, 'div', { className: 'pager' });
  const previousContext = createNode(documentRef, 'button', { type: 'button', className: 'secondary', text: 'Context 上一页' });
  previousContext.disabled = (payload.context_page || 1) <= 1;
  previousContext.addEventListener('click', () => handlers.onContextPage?.((payload.context_page || 1) - 1));
  const nextContext = createNode(documentRef, 'button', { type: 'button', className: 'secondary', text: 'Context 下一页' });
  nextContext.disabled = (payload.context_page || 1) * (payload.context_page_size || 100) >= (payload.contexts_total || 0);
  nextContext.addEventListener('click', () => handlers.onContextPage?.((payload.context_page || 1) + 1));
  contextPager.append(previousContext, createNode(documentRef, 'span', { text: `第 ${payload.context_page || 1} 页` }), nextContext);
  container.append(contextPager);
}

function renderOAuthItems(
  container,
  items,
  collection,
  onAction,
  onClientPassword,
  onClientWorkspaces,
  workspaces = [],
) {
  const documentRef = container.ownerDocument || document;
  container.replaceChildren();
  if (!items?.length) {
    container.append(createNode(documentRef, 'p', { className: 'muted', text: '没有记录。' }));
    return;
  }
  const actionMap = {
    clients: ['enable', 'disable'],
    grants: ['revoke'],
    tokens: ['revoke'],
    'refresh-families': ['revoke'],
    'signing-keys': ['activate', 'retire', 'revoke'],
  };
  const idKey = {
    clients: 'client_id', grants: 'grant_id', tokens: 'jti',
    'refresh-families': 'family_id', 'signing-keys': 'kid', audit: 'event_id',
  }[collection];
  for (const original of items) {
    const item = sanitizeAdminValue(original);
    const card = createNode(documentRef, 'article', { className: 'card' });
    card.append(createNode(documentRef, 'h3', { text: String(item?.[idKey] || `${collection} item`) }));
    if (collection === 'clients') {
      const clientMode = item?.authorize_login?.mode === 'client';
      card.append(createNode(documentRef, 'span', {
        className: `badge ${clientMode ? 'good' : ''}`.trim(),
        text: clientMode ? 'Authorize：专属密码' : 'Authorize：全局密码',
      }));
      const workspaceIds = Array.isArray(item?.workspace_access?.workspace_ids)
        ? item.workspace_access.workspace_ids.map(String)
        : Array.isArray(item?.workspace_ids) ? item.workspace_ids.map(String) : [];
      card.append(createNode(documentRef, 'span', {
        className: `badge ${workspaceIds.length ? 'good' : 'danger'}`,
        text: workspaceIds.length ? `允许 Workspace：${workspaceIds.length} 个` : '未配置 Workspace 权限',
      }));
    }
    appendDefinitionList(documentRef, card, item);
    if (Object.values(item || {}).some((value) => value && typeof value === 'object')) {
      const details = createNode(documentRef, 'details');
      const summary = createNode(documentRef, 'summary', { text: '查看脱敏结构' });
      const pre = createNode(documentRef, 'pre', { className: 'code-block', text: JSON.stringify(item, null, 2) });
      details.append(summary, pre);
      card.append(details);
    }
    const actions = createNode(documentRef, 'div', { className: 'button-row' });
    for (const action of actionMap[collection] || []) {
      const button = createNode(documentRef, 'button', { type: 'button', className: action === 'enable' || action === 'activate' ? 'secondary' : 'danger', text: action });
      button.addEventListener('click', () => onAction?.(collection, String(item?.[idKey] || ''), action, button));
      actions.append(button);
    }
    if (collection === 'clients') {
      const clientId = String(item?.[idKey] || '');
      const clientMode = item?.authorize_login?.mode === 'client';
      const workspaceIds = new Set(
        Array.isArray(item?.workspace_access?.workspace_ids)
          ? item.workspace_access.workspace_ids.map(String)
          : Array.isArray(item?.workspace_ids) ? item.workspace_ids.map(String) : [],
      );
      const access = createNode(documentRef, 'fieldset', { className: 'oauth-workspace-access' });
      access.append(createNode(documentRef, 'legend', { text: '允许的 Workspaces' }));
      const checkboxes = [];
      for (const workspace of workspaces.filter((candidate) => candidate?.enabled !== false)) {
        const checkbox = createNode(documentRef, 'input', { type: 'checkbox' });
        checkbox.value = String(workspace.id || '');
        checkbox.checked = workspaceIds.has(checkbox.value);
        const label = createNode(documentRef, 'label', { className: 'checkline' });
        label.append(
          checkbox,
          createNode(documentRef, 'span', {
            text: `${workspace.name || workspace.id} (${workspace.id})`,
          }),
        );
        access.append(label);
        checkboxes.push(checkbox);
      }
      const saveWorkspaceAccess = createNode(documentRef, 'button', {
        type: 'button',
        className: 'secondary',
        text: '保存 Workspace 权限',
      });
      saveWorkspaceAccess.disabled = checkboxes.length === 0;
      saveWorkspaceAccess.addEventListener('click', () => {
        const selected = checkboxes
          .filter((checkbox) => checkbox.checked)
          .map((checkbox) => String(checkbox.value));
        onClientWorkspaces?.(clientId, selected, saveWorkspaceAccess);
      });
      if (!checkboxes.length) {
        access.append(createNode(documentRef, 'p', {
          className: 'muted',
          text: '当前 Runtime 没有可授权的 Workspace。',
        }));
      }
      access.append(saveWorkspaceAccess);
      card.append(access);
      const configure = createNode(documentRef, 'button', {
        type: 'button',
        className: 'secondary',
        text: clientMode ? '轮换专属密码' : '设置专属密码',
      });
      configure.addEventListener('click', () => onClientPassword?.(clientId, 'configure', configure));
      actions.append(configure);
      if (clientMode) {
        const view = createNode(documentRef, 'button', {
          type: 'button', className: 'secondary', text: '查看专属密码',
        });
        view.addEventListener('click', () => onClientPassword?.(clientId, 'view', view));
        actions.append(view);
        const reset = createNode(documentRef, 'button', {
          type: 'button', className: 'secondary', text: '改用全局密码',
        });
        reset.addEventListener('click', () => onClientPassword?.(clientId, 'reset', reset));
        actions.append(reset);
      }
    }
    if (actions.childNodes.length) card.append(actions);
    container.append(card);
  }
}

function confirmDestructive(documentRef, { title, message, confirmLabel = '确认', returnFocus } = {}) {
  const localizedTitle = translateUi(title || '确认操作');
  const localizedMessage = translateUi(message || '');
  const localizedLabel = translateUi(confirmLabel);
  const dialog = documentRef.getElementById('confirmDialog');
  if (!dialog || typeof dialog.showModal !== 'function') {
    return Promise.resolve(globalThis.confirm ? globalThis.confirm(localizedMessage || localizedTitle || translateUi('确认操作？')) : false);
  }
  documentRef.getElementById('confirmTitle').textContent = localizedTitle;
  documentRef.getElementById('confirmMessage').textContent = localizedMessage;
  documentRef.getElementById('confirmAccept').textContent = localizedLabel;
  return new Promise((resolve) => {
    const finish = () => {
      dialog.removeEventListener('close', finish);
      const accepted = dialog.returnValue === 'confirm';
      if (returnFocus && typeof returnFocus.focus === 'function') returnFocus.focus();
      resolve(accepted);
    };
    dialog.addEventListener('close', finish);
    dialog.showModal();
  });
}

async function handleSettingsSave({ api, state, documentRef }) {
  const model = globalThis.McpSettingsModel;
  const page = globalThis.McpSettingsPage;
  state.settings.draft = model.serializeSettings(page.collectSettingsDraft(documentRef, state.settings.draft));
  try {
    const payload = await api.request('/settings', {
      method: 'PUT',
      body: { expected_revision: state.settings.persistedRevision, updates: state.settings.draft },
    });
    const safePayload = { ...payload, active: sanitizeAdminValue(payload.active), persisted: sanitizeAdminValue(payload.persisted) };
    state.settings = model.hydrateSettings(safePayload);
    page.renderSettingsForm(documentRef, state.settings, globalThis.McpSettingsCopy.permissionPresentation);
    page.renderFormError(documentRef, '');
    return { saved: true, conflict: false };
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      const latest = await api.request('/settings');
      const safeLatest = { ...latest, active: sanitizeAdminValue(latest.active), persisted: sanitizeAdminValue(latest.persisted) };
      state.settings = model.refreshPersistedKeepingDraft(state.settings, safeLatest);
      page.renderSettingsForm(documentRef, state.settings, globalThis.McpSettingsCopy.permissionPresentation);
      const conflict = documentRef.getElementById('settingsConflict');
      conflict?.focus();
      return { saved: false, conflict: true };
    }
    throw error;
  }
}

function initAdminApp(documentRef = document) {
  i18n.initI18n();
  initThemeControls(documentRef);
  const model = globalThis.McpSettingsModel;
  const copy = globalThis.McpSettingsCopy;
  const workspaceEditor = globalThis.McpWorkspaceEditor;
  const settingsPage = globalThis.McpSettingsPage;
  const state = {
    authenticated: false, csrfToken: '', settings: null, workspaces: [], workspaceRevision: '', gateway: null,
    gatewayRevision: '', editingGatewayAlias: '', conversationPage: 1, conversationTotal: 0,
    selectedConversation: null, messagePage: 1, contextPage: 1,
    clientPasswordClientId: '', clientPasswordMode: 'set', clientPasswordReturnFocus: null, section: 'overview',
    overviewStatus: null, conversations: [], oauthClients: [], secrets: [],
    activityLogs: [], selectedActivityLogId: null,
  };
  const byId = (id) => documentRef.getElementById(id);

  function recordActivity(entry) {
    state.activityLogs.unshift({ id: `activity-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, ...entry });
    state.activityLogs = state.activityLogs.slice(0, 250);
    renderActivityLogSummary();
    if (byId('activityLogDialog')?.open) renderActivityLogs();
  }

  const api = createApiClient(() => state.csrfToken, globalThis.fetch, recordActivity);

  function status(message, kind = '') {
    const box = byId('globalStatus');
    if (!box) return;
    box.textContent = message;
    box.className = `status ${kind}`.trim();
  }

  function clientPasswordError(message = '') {
    const box = byId('clientPasswordError');
    box.textContent = message;
    box.hidden = !message;
  }

  function closeClientPasswordDialog({ restoreFocus = true } = {}) {
    const dialog = byId('clientPasswordDialog');
    const returnFocus = state.clientPasswordReturnFocus;
    byId('clientPasswordValue').value = '';
    setPasswordVisibility(
      byId('clientPasswordValue'),
      byId('clientPasswordToggle'),
      false,
    );
    clientPasswordError();
    state.clientPasswordClientId = '';
    state.clientPasswordMode = 'set';
    state.clientPasswordReturnFocus = null;
    byId('clientPasswordTitle').textContent = '设置 Client 专属密码';
    byId('clientPasswordHelp').textContent = '保存后立即生效，只影响这个 Client；未设置或重置后使用全局 OAuth Authorize 密码。';
    byId('clientPasswordCancel').textContent = '取消';
    byId('clientPasswordSubmit').hidden = false;
    byId('clientPasswordSubmit').textContent = '保存并立即生效';
    byId('clientPasswordValue').readOnly = false;
    byId('clientPasswordValue').required = true;
    if (dialog?.open) dialog.close();
    if (restoreFocus && returnFocus && typeof returnFocus.focus === 'function') {
      returnFocus.focus();
    }
  }

  function openClientPasswordDialog(clientId, returnFocus) {
    state.clientPasswordMode = 'set';
    state.clientPasswordClientId = clientId;
    state.clientPasswordReturnFocus = returnFocus;
    byId('clientPasswordClientId').textContent = clientId;
    byId('clientPasswordValue').value = '';
    setPasswordVisibility(
      byId('clientPasswordValue'),
      byId('clientPasswordToggle'),
      false,
    );
    clientPasswordError();
    byId('clientPasswordDialog').showModal();
    byId('clientPasswordValue').focus();
  }

  async function openClientPasswordViewDialog(clientId, returnFocus) {
    state.clientPasswordMode = 'view';
    state.clientPasswordClientId = clientId;
    state.clientPasswordReturnFocus = returnFocus;
    byId('clientPasswordClientId').textContent = clientId;
    byId('clientPasswordTitle').textContent = '查看 Client 专属密码';
    byId('clientPasswordHelp').textContent = '这是该 Client 的专属 OAuth Authorize 密码，默认隐藏；点击“显示”可临时查看。';
    byId('clientPasswordValue').value = '';
    byId('clientPasswordValue').readOnly = true;
    byId('clientPasswordValue').required = false;
    byId('clientPasswordCancel').textContent = '关闭';
    byId('clientPasswordSubmit').hidden = true;
    setPasswordVisibility(
      byId('clientPasswordValue'),
      byId('clientPasswordToggle'),
      false,
    );
    clientPasswordError();
    byId('clientPasswordDialog').showModal();
    byId('clientPasswordToggle').focus();
    try {
      const result = await api.request(`/oauth/clients/${encodeURIComponent(clientId)}/authorization-password`, { method: 'GET' });
      if (state.clientPasswordClientId !== clientId || state.clientPasswordMode !== 'view') return;
      byId('clientPasswordValue').value = String(result.value || '');
    } catch (error) {
      if (state.clientPasswordClientId !== clientId || state.clientPasswordMode !== 'view') return;
      clientPasswordError(error.message);
    }
  }

  function updateGatewayTransportFields() {
    const isStdio = byId('gatewayTransport').value === 'stdio';
    for (const node of documentRef.querySelectorAll('[data-gateway-stdio]')) node.hidden = !isStdio;
    for (const node of documentRef.querySelectorAll('[data-gateway-http]')) node.hidden = isStdio;
  }

  function gatewayFormError(message = '') {
    const box = byId('gatewayFormError');
    box.textContent = message;
    box.hidden = !message;
  }

  function resetGatewayServerForm({ focus = false } = {}) {
    state.editingGatewayAlias = '';
    byId('gatewayServerForm').reset();
    byId('gatewayAlias').disabled = false;
    byId('gatewayTransport').value = 'stdio';
    byId('gatewayExposeMode').value = 'broker';
    byId('gatewayTimeout').value = '30000';
    byId('gatewayEnabled').checked = true;
    byId('gatewayServerFormTitle').textContent = '新增 MCP 连接';
    byId('gatewayCredentialNotice').textContent = '';
    gatewayFormError();
    updateGatewayTransportFields();
    if (focus) byId('gatewayAlias').focus();
  }

  function editableEnvironment(config) {
    const rows = [];
    const preserved = [];
    for (const [key, value] of Object.entries(config?.env || {})) {
      if (typeof value === 'string' && value !== '<redacted>') rows.push(`${key}=${value}`);
      else preserved.push(key);
    }
    return { rows, preserved };
  }

  function editGatewayServer(alias, config) {
    state.editingGatewayAlias = alias;
    byId('gatewayServerFormTitle').textContent = `编辑 MCP 连接：${alias}`;
    byId('gatewayAlias').value = alias;
    byId('gatewayAlias').disabled = true;
    byId('gatewayTransport').value = config.transport === 'stdio' ? 'stdio' : 'streamable_http';
    byId('gatewayCommand').value = config.command || '';
    byId('gatewayArgs').value = Array.isArray(config.args) ? config.args.join('\n') : '';
    byId('gatewayUrl').value = config.url || '';
    byId('gatewayAuthorizationEnv').value = config.authorization_env === '<redacted>' ? '' : (config.authorization_env || '');
    byId('gatewayExposeMode').value = config.expose_mode === 'broker' ? 'broker' : 'direct';
    byId('gatewayPinnedTools').value = Array.isArray(config.pinned_tools) ? config.pinned_tools.join('\n') : '';
    byId('gatewayIncludeTools').value = Array.isArray(config.include_tools) ? config.include_tools.join('\n') : '';
    byId('gatewayExcludeTools').value = Array.isArray(config.exclude_tools) ? config.exclude_tools.join('\n') : '';
    byId('gatewayTags').value = Array.isArray(config.tags) ? config.tags.join('\n') : '';
    byId('gatewayTimeout').value = String(config.timeout_ms || 30000);
    byId('gatewayEnabled').checked = config.enabled !== false;
    const environment = editableEnvironment(config);
    byId('gatewayEnvironment').value = environment.rows.join('\n');
    byId('gatewayCredentialNotice').textContent = environment.preserved.length
      ? `将保留 ${environment.preserved.length} 个未显示的凭据引用：${environment.preserved.join('、')}`
      : '';
    gatewayFormError();
    updateGatewayTransportFields();
    byId('gatewayServerForm').scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    byId('gatewayCommand').focus();
  }

  function readGatewayServerForm() {
    return gatewayServerFromForm({
      alias: state.editingGatewayAlias || byId('gatewayAlias').value,
      transport: byId('gatewayTransport').value,
      command: byId('gatewayCommand').value,
      args: byId('gatewayArgs').value,
      environment: byId('gatewayEnvironment').value,
      url: byId('gatewayUrl').value,
      authorizationEnv: byId('gatewayAuthorizationEnv').value,
      exposeMode: byId('gatewayExposeMode').value,
      pinnedTools: byId('gatewayPinnedTools').value,
      includeTools: byId('gatewayIncludeTools').value,
      excludeTools: byId('gatewayExcludeTools').value,
      tags: byId('gatewayTags').value,
      timeoutMs: byId('gatewayTimeout').value,
      enabled: byId('gatewayEnabled').checked,
    });
  }

  async function saveGatewayServer(alias, config) {
    const payload = await api.request(`/gateway/servers/${encodeURIComponent(alias)}`, {
      method: 'PUT',
      body: { expected_revision: state.gatewayRevision, config },
    });
    renderGateway(payload);
    return payload;
  }

  const sectionMeta = {
    overview: ['概览', '运行状态与运维提醒'],
    gateway: ['工具连接', '工具网关配置、暴露策略与重启语义'],
    workspaces: ['工作区', '会话的文件与进程边界'],
    chat: ['聊天会话', '摘要列表与按需正文'],
    settings: ['服务器设置', '带版本校验的配置管理'],
    oauth: ['授权管理', '客户端、授权记录、令牌与签名密钥'],
    secrets: ['凭据保险箱', '只显示名称的凭据管理'],
    system: ['系统状态', '运行时、工具网关、凭据保险箱与 Telemetry'],
  };

  function clientWorkspaceIds(client) {
    if (Array.isArray(client?.workspace_access?.workspace_ids)) return client.workspace_access.workspace_ids.map(String);
    if (Array.isArray(client?.workspace_ids)) return client.workspace_ids.map(String);
    return [];
  }

  function renderOverviewDashboard() {
    const servers = state.gateway?.persisted?.servers || {};
    const aliases = Object.keys(servers);
    const enabledConnections = aliases.filter((alias) => servers[alias]?.enabled !== false).length;
    const brokerConnections = aliases.filter((alias) => servers[alias]?.expose_mode === 'broker').length;
    const enabledWorkspaces = state.workspaces.filter((item) => item?.enabled !== false);
    const defaultWorkspace = enabledWorkspaces.find((item) => item.default);
    const exposure = state.gateway?.active_status?.exposure_report;
    const catalogCount = Number(exposure?.catalog?.count || exposure?.direct?.count || 0);
    const missingClientAccess = state.oauthClients.filter((client) => client?.enabled !== false && clientWorkspaceIds(client).length === 0);
    const restartCount = restartImpactCount(state.settings, state.gateway);
    const overriddenSettings = Object.entries(state.settings?.fieldStatus || {}).filter(([, item]) => item?.state === 'overridden');

    const navConnections = byId('navConnectionCount');
    const navWorkspaces = byId('navWorkspaceCount');
    if (navConnections) navConnections.textContent = String(aliases.length);
    if (navWorkspaces) navWorkspaces.textContent = String(state.workspaces.length);
    if (byId('sidebarMode')) byId('sidebarMode').textContent = brokerConnections >= (aliases.length - brokerConnections) ? '代理模式' : '直连为主';
    if (byId('overviewRuntimeStatus')) byId('overviewRuntimeStatus').textContent = state.overviewStatus?.admin_api ? '运行中' : '未连接';
    if (byId('overviewPermissionMode')) byId('overviewPermissionMode').textContent = state.settings?.active?.permission_mode || '—';

    const taskRoot = byId('operationsAlerts');
    if (taskRoot) {
      taskRoot.replaceChildren();
      const tasks = [];
      if (restartCount) tasks.push({ kind: 'warning', icon: 'alert', title: '需要重启服务', detail: '工具网关或服务器设置已保存，但当前运行时仍可能使用旧快照。', action: '查看影响', section: 'system' });
      if (overriddenSettings.length) tasks.push({ kind: 'warning', icon: 'settings', title: '启动参数覆盖 Persisted', detail: `${overriddenSettings.length} 个设置由 Desktop、CLI 或环境变量控制；单纯重启不会采用已保存值。`, action: '查看来源', section: 'system' });
      if (!defaultWorkspace) tasks.push({ kind: 'danger', icon: 'folder', title: '检查默认工作区', detail: '当前没有启用且可作为默认项的工作区。', action: '查看工作区', section: 'workspaces' });
      if (missingClientAccess.length) tasks.push({ kind: 'danger', icon: 'users', title: 'OAuth Client 配置', detail: `${missingClientAccess.length} 个启用 Client 缺少 Workspace allowlist。`, action: '查看详情', section: 'oauth' });
      if (!aliases.length) tasks.push({ kind: 'warning', icon: 'plug', title: '尚未配置工具连接', detail: '添加上游 MCP 后才能通过工具网关提供能力。', action: '添加连接', section: 'gateway' });
      if (!tasks.length) tasks.push({ kind: 'success', icon: 'check', title: '没有阻塞提醒', detail: '工具连接、工作区、授权和服务器配置当前没有明显阻塞项。', action: '查看系统状态', section: 'system' });
      tasks.slice(0, 4).forEach((task) => {
        const card = createNode(documentRef, 'article', { className: `task-card ${task.kind}` });
        const icon = createNode(documentRef, 'span', { className: 'task-icon' });
        icon.append(createSvgIcon(documentRef, task.icon, 'icon icon-sm'));
        const copyNode = createNode(documentRef, 'div', { className: 'task-copy' });
        const button = createNode(documentRef, 'button', { type: 'button', className: 'secondary', text: task.action });
        button.addEventListener('click', () => showSection(task.section));
        copyNode.append(createNode(documentRef, 'strong', { text: task.title }), createNode(documentRef, 'p', { text: task.detail }), button);
        card.append(icon, copyNode);
        taskRoot.append(card);
      });
    }

    const metricRoot = byId('overviewMetrics');
    if (metricRoot) {
      metricRoot.replaceChildren();
      const metrics = [
        ['工具连接', aliases.length, '个连接', [[enabledConnections, '启用中', 'good'], [aliases.length - enabledConnections, '已停用', 'danger'], [brokerConnections, 'Broker', 'info']]],
        ['工作区', state.workspaces.length, '个目录', [[enabledWorkspaces.length, '启用中', 'good'], [defaultWorkspace ? 1 : 0, '默认', defaultWorkspace ? 'info' : 'warning']]],
        ['聊天会话', state.conversationTotal || state.conversations.length, '个会话', [[state.conversations.length, '本页摘要', 'good']]],
        ['运行时工具', catalogCount, '个公开定义', [[state.gateway?.active_status ? '正常' : '未报告', 'Gateway', state.gateway?.active_status ? 'good' : 'warning']]],
      ];
      metrics.forEach(([title, value, unit, meta]) => {
        const card = createNode(documentRef, 'article', { className: 'metric-card' });
        card.append(createNode(documentRef, 'div', { className: 'metric-head', text: title }));
        const valueNode = createNode(documentRef, 'strong', { className: 'metric-value', text: value });
        valueNode.append(createNode(documentRef, 'small', { text: unit }));
        const metaNode = createNode(documentRef, 'div', { className: 'metric-meta' });
        meta.forEach(([amount, label, kind]) => {
          const row = createNode(documentRef, 'span');
          row.append(createNode(documentRef, 'span', { className: `mini-dot ${kind}` }), documentRef.createTextNode(`${amount} ${label}`));
          metaNode.append(row);
        });
        card.append(valueNode, metaNode);
        metricRoot.append(card);
      });
    }

    const gatewayRoot = byId('overviewGateway');
    if (gatewayRoot) {
      gatewayRoot.replaceChildren();
      aliases.slice(0, 5).forEach((alias) => {
        const config = servers[alias] || {};
        const row = createNode(documentRef, 'div', { className: 'compact-row' });
        const copyNode = createNode(documentRef, 'span', { className: 'compact-copy' });
        copyNode.append(createNode(documentRef, 'strong', { text: alias }), createNode(documentRef, 'small', { text: config.transport === 'stdio' ? '本地命令' : 'Streamable HTTP' }));
        row.append(copyNode, createNode(documentRef, 'span', { className: 'compact-meta', text: `${config.enabled === false ? '停用' : '启用'} · ${config.expose_mode === 'broker' ? 'Broker' : 'Direct'}` }));
        gatewayRoot.append(row);
      });
      if (!aliases.length) gatewayRoot.append(createNode(documentRef, 'p', { className: 'muted', text: '尚未配置工具连接。' }));
    }

    const conversationRoot = byId('overviewConversations');
    if (conversationRoot) {
      conversationRoot.replaceChildren();
      state.conversations.slice(0, 5).forEach((item) => {
        const button = createNode(documentRef, 'button', { type: 'button', className: 'compact-row' });
        const copyNode = createNode(documentRef, 'span', { className: 'compact-copy' });
        copyNode.append(createNode(documentRef, 'strong', { text: item.title || item.conversation_id }), createNode(documentRef, 'small', { text: item.preview || '无摘要' }));
        button.append(copyNode, createNode(documentRef, 'span', { className: 'compact-meta', text: item.updated_at || '' }));
        button.addEventListener('click', () => {
          if (byId('chatWorkspace')) byId('chatWorkspace').value = item.workspace_id;
          state.selectedConversation = { workspaceId: item.workspace_id, conversationId: item.conversation_id };
          showSection('chat');
          loadConversationDetail().catch((error) => status(error.message, 'danger'));
        });
        conversationRoot.append(button);
      });
      if (!state.conversations.length) conversationRoot.append(createNode(documentRef, 'p', { className: 'muted', text: '暂无会话摘要。' }));
    }

    const systemRoot = byId('overviewSystem');
    if (systemRoot) {
      systemRoot.replaceChildren();
      const rows = [
        ['Admin API', state.overviewStatus?.admin_api ? '正常' : '未连接'],
        ['工具网关', state.overviewStatus?.gateway?.available ? '连接正常' : '不可用'],
        ['凭据保险箱', state.overviewStatus?.vault?.enabled ? '已启用' : '未启用'],
        ['配置版本', state.settings?.persistedRevision || state.gatewayRevision || '—'],
      ];
      rows.forEach(([label, value]) => {
        const row = createNode(documentRef, 'div', { className: 'status-row' });
        row.append(createNode(documentRef, 'span', { className: `status-dot ${/正常|启用/.test(value) ? 'good' : 'warning'}` }), createNode(documentRef, 'strong', { text: label }), createNode(documentRef, 'small', { text: value }));
        systemRoot.append(row);
      });
    }

    const hostStatus = state.settings?.fieldStatus?.host;
    const executionStatus = state.settings?.fieldStatus?.execution_fs_mode;
    const restartNotes = [];
    if (hostStatus?.state === 'overridden' && hostStatus?.source === 'desktop_cli') {
      restartNotes.push('Host 被 Desktop 启动参数覆盖，重启不会采用 Persisted 值。请在 Desktop profile 修改。');
    }
    if (executionStatus?.state === 'in_sync_default') {
      restartNotes.push('execution_fs_mode 未显式配置，默认值与当前运行时一致，无需重启。');
    }
    if (byId('systemRestartCount')) byId('systemRestartCount').textContent = String(restartCount);
    if (byId('systemRestartTitle')) {
      byId('systemRestartTitle').textContent = restartCount
        ? `${restartCount} 项等待生效`
        : overriddenSettings.length
          ? '没有可通过重启生效的项目'
          : '没有等待生效的项目';
    }
    if (byId('systemRestartDetail')) {
      const actionable = restartCount ? '仅上方计数中的项目可通过新建运行时或服务重启生效。' : '';
      const fallback = overriddenSettings.length ? '存在启动器、CLI 或环境变量覆盖，请按来源修改。' : 'Active 与 Effective Persisted 配置已同步。';
      byId('systemRestartDetail').textContent = [...restartNotes, actionable || fallback].filter(Boolean).join(' ');
    }
  }

  function activityTime(timestamp) {
    return new Date(timestamp).toLocaleTimeString(i18n.getLocale() === 'en' ? 'en-US' : 'zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function filteredActivityLogs() {
    const time = byId('activityLogTime')?.value || '24h';
    const level = byId('activityLogLevel')?.value || 'all';
    const component = byId('activityLogComponent')?.value || 'all';
    const query = (byId('activityLogSearch')?.value || '').trim().toLowerCase();
    const limits = { '15m': 15 * 60e3, '1h': 60 * 60e3, '24h': 24 * 60 * 60e3 };
    const now = Date.now();
    return state.activityLogs.filter((item) => (time === 'all' || now - item.timestamp <= limits[time]) && (level === 'all' || item.level === level) && (component === 'all' || item.component === component) && (!query || `${item.method} ${item.path} ${item.status} ${item.message}`.toLowerCase().includes(query)));
  }

  function renderActivityLogSummary() {
    if (byId('activityLogCount')) byId('activityLogCount').textContent = String(state.activityLogs.length);
    if (byId('activityLogErrorCount')) byId('activityLogErrorCount').textContent = String(state.activityLogs.filter((item) => item.level === 'error').length);
    if (byId('activityLogLastUpdated')) byId('activityLogLastUpdated').textContent = state.activityLogs[0] ? activityTime(state.activityLogs[0].timestamp) : '—';
  }

  function renderActivityLogDetail(item) {
    const root = byId('activityLogDetail');
    if (!root) return;
    root.replaceChildren();
    if (!item) {
      const empty = createNode(documentRef, 'div', { className: 'empty-state' });
      empty.append(createNode(documentRef, 'h3', { text: '选择一条日志' }), createNode(documentRef, 'p', { text: '点击记录查看请求方法、路径、HTTP 状态和耗时。' }));
      root.append(empty);
      return;
    }
    root.append(createNode(documentRef, 'h3', { text: `${item.method} ${item.path}` }), createNode(documentRef, 'p', { className: 'muted', text: item.message }));
    const dl = createNode(documentRef, 'dl');
    [['时间', new Date(item.timestamp).toLocaleString()], ['组件', item.component], ['HTTP 状态', item.status || 'network'], ['耗时', `${item.duration_ms} ms`]].forEach(([label, value]) => {
      const row = createNode(documentRef, 'div');
      row.append(createNode(documentRef, 'dt', { text: label }), createNode(documentRef, 'dd', { text: value }));
      dl.append(row);
    });
    root.append(dl);
  }

  function renderActivityLogs() {
    const root = byId('activityLogList');
    if (!root) return;
    const items = filteredActivityLogs();
    if (state.selectedActivityLogId && !items.some((item) => item.id === state.selectedActivityLogId)) state.selectedActivityLogId = null;
    if (byId('activityLogResultCount')) byId('activityLogResultCount').textContent = `${items.length} 条结果`;
    root.replaceChildren();
    items.forEach((item) => {
      const row = createNode(documentRef, 'button', { type: 'button', className: `log-row ${state.selectedActivityLogId === item.id ? 'active' : ''}` });
      const copyNode = createNode(documentRef, 'span', { className: 'log-copy' });
      copyNode.append(createNode(documentRef, 'strong', { text: `${item.method} ${item.path}` }), createNode(documentRef, 'small', { text: item.message }));
      row.append(createNode(documentRef, 'time', { text: activityTime(item.timestamp) }), createNode(documentRef, 'span', { className: `log-level ${item.level}`, text: item.level }), createNode(documentRef, 'span', { text: item.component }), copyNode);
      row.addEventListener('click', () => { state.selectedActivityLogId = item.id; renderActivityLogs(); });
      root.append(row);
    });
    if (!items.length) root.append(createNode(documentRef, 'div', { className: 'empty-state', text: '当前筛选条件没有日志。' }));
    renderActivityLogDetail(items.find((item) => item.id === state.selectedActivityLogId) || null);
  }

  function showSection(name) {
    if (!sectionMeta[name]) return;
    state.section = name;
    for (const section of documentRef.querySelectorAll('.page-section')) {
      const active = section.id === `section-${name}`;
      section.hidden = !active;
      section.classList.toggle('active', active);
    }
    for (const button of documentRef.querySelectorAll('.nav-item')) {
      const active = button.dataset.section === name;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }
    const [title, subtitle] = sectionMeta[name];
    if (byId('pageTitle')) byId('pageTitle').textContent = translateUi(title);
    if (byId('pageSubtitle')) byId('pageSubtitle').textContent = translateUi(subtitle);
    documentRef.title = `${translateUi(title)} · Coding Tools MCP`;
    documentRef.body.classList.remove('sidebar-open');
    byId('menuButton')?.setAttribute('aria-expanded', 'false');
    byId('mainContent')?.focus();
  }

  async function loadOverview() {
    const payload = await api.request('/status');
    state.overviewStatus = payload;
    byId('adminApiStatus').textContent = payload.admin_api ? '可用' : '不可用';
    byId('gatewayRuntimeStatus').textContent = payload.gateway?.available ? '已配置（快照不可变）' : '不可用';
    byId('vaultStatus').textContent = payload.vault?.enabled ? '已启用' : '未启用';
    const telemetry = copy.telemetryPresentation(payload.telemetry);
    byId('telemetryStatus').textContent = telemetry.label;
    byId('telemetryDetail').textContent = telemetry.detail;
    byId('telemetryDisableHelp').textContent = copy.TELEMETRY_DISABLE_HELP;
    byId('fakeReadonlyStatus').textContent = payload.runtime?.annotation_override === 'fake_readonly' ? '已启用' : payload.runtime ? '未启用' : '未报告';
    renderOverviewDashboard();
    return payload;
  }

  function safeSettingsPayload(payload) {
    return {
      ...payload,
      active: sanitizeAdminValue(payload.active),
      persisted: sanitizeAdminValue(payload.persisted),
      effective_persisted: sanitizeAdminValue(payload.effective_persisted),
      field_status: sanitizeAdminValue(payload.field_status),
    };
  }

  async function loadSettings({ preserveDraft = false } = {}) {
    const payload = safeSettingsPayload(await api.request('/settings'));
    state.settings = preserveDraft && state.settings
      ? model.refreshPersistedKeepingDraft(state.settings, payload)
      : model.hydrateSettings(payload);
    settingsPage.renderSettingsForm(documentRef, state.settings, copy.permissionPresentation);
    renderOverviewDashboard();
    return payload;
  }

  async function loadWorkspaces() {
    const payload = await api.request('/workspaces');
    state.workspaces = payload.workspace_catalog || [];
    state.workspaceRevision = payload.persisted_revision || '';
    workspaceEditor.renderWorkspaceRows(byId('workspaceList'), state.workspaces, {
      onCheck: async (workspace) => {
        const result = await api.request(`/workspaces/${encodeURIComponent(workspace.id)}/check`);
        status(`Workspace ${workspace.id}: exists=${result.check?.exists}, directory=${result.check?.is_directory}`);
      },
      onDefault: async (workspace, button) => {
        const accepted = await confirmDestructive(documentRef, {
          title: '更改默认 Workspace',
          message: `Workspace ID: ${workspace.id}\n影响：新的 Runtime/Session 将使用此默认 Workspace；已有 Session 绑定不变。`,
          confirmLabel: '设为默认', returnFocus: button,
        });
        if (!accepted) return;
        await api.request(`/workspaces/${encodeURIComponent(workspace.id)}/default`, { method: 'POST', body: { expected_revision: state.workspaceRevision } });
        await Promise.all([loadWorkspaces(), loadSettings()]);
        status(`默认 Workspace 已改为 ${workspace.id}；需要按返回状态重启。`);
      },
      onDisable: async (workspace, button) => {
        const accepted = await confirmDestructive(documentRef, {
          title: '禁用 Workspace',
          message: `Workspace ID: ${workspace.id}\n影响：新的 Session 将无法绑定此 Workspace；已有 Session 保持冻结直到关闭。`,
          confirmLabel: '禁用', returnFocus: button,
        });
        if (!accepted) return;
        await api.request(`/workspaces/${encodeURIComponent(workspace.id)}/disable`, { method: 'POST', body: { expected_revision: state.workspaceRevision } });
        await Promise.all([loadWorkspaces(), loadSettings()]);
        status(`Workspace ${workspace.id} 已禁用。`);
      },
    });
    workspaceEditor.populateWorkspaceSelect(byId('chatWorkspace'), state.workspaces, byId('chatWorkspace')?.value || payload.default_workspace_id);
    renderOverviewDashboard();
    return payload;
  }

  function updateGatewayExposurePreview() {
    const container = byId('gatewayExposurePreview');
    if (!container) return;
    const draft = byId('gatewayDocument').value.trim();
    try {
      const documentValue = draft ? JSON.parse(draft) : (state.gateway?.persisted || { servers: {} });
      renderGatewayExposurePreview(
        container,
        gatewayExposurePreview(documentValue, state.gateway?.active_status || {}),
      );
    } catch (error) {
      container.replaceChildren(createNode(documentRef, 'p', { className: 'danger-text', text: `JSON 无法预览：${error.message}` }));
    }
  }

  function renderGateway(payload) {
    state.gateway = payload;
    state.gatewayRevision = payload.persisted_revision || '';
    const policy = byId('gatewayCredentialPolicy');
    if (policy) policy.value = payload.credential_policy || 'local';
    const sources = byId('gatewayCredentialSources');
    if (sources) {
      sources.replaceChildren();
      for (const [alias, entries] of Object.entries(payload.credential_sources || {})) {
        sources.append(createNode(documentRef, 'p', { text: `${alias}: ${Object.entries(entries).map(([name, item]) => `${name}=${item.source}`).join(', ')}` }));
      }
    }
    byId('gatewayRestartRequired').textContent = payload.restart_required ? '是' : '否';
    byId('gatewayRevision').textContent = state.gatewayRevision || '—';
    const summary = byId('gatewaySummary');
    summary.replaceChildren();
    const servers = payload.persisted?.servers || {};
    const aliases = Object.keys(servers);
    if (!aliases.length) summary.append(createNode(documentRef, 'p', { className: 'muted', text: '尚未添加 MCP 工具连接。' }));
    for (const alias of aliases) {
      const raw = servers[alias] || {};
      const item = createNode(documentRef, 'article', { className: 'card gateway-server-card' });
      const mode = raw.expose_mode || 'direct';
      const pins = Array.isArray(raw.pinned_tools) ? raw.pinned_tools : [];
      const enabled = raw.enabled !== false;
      const badges = createNode(documentRef, 'p');
      badges.append(
        createNode(documentRef, 'span', { className: `badge ${enabled ? 'good' : 'danger'}`, text: enabled ? '下次启动启用' : '下次启动禁用' }),
        createNode(documentRef, 'span', { className: `badge ${mode === 'broker' ? 'good' : ''}`, text: mode === 'broker' ? 'Broker 按需暴露' : 'Direct 全部直出' }),
      );
      const actions = createNode(documentRef, 'div', { className: 'button-row' });
      const edit = createNode(documentRef, 'button', { type: 'button', className: 'secondary', text: '编辑' });
      edit.addEventListener('click', () => editGatewayServer(alias, raw));
      const toggle = createNode(documentRef, 'button', { type: 'button', className: 'secondary', text: enabled ? '下次启动禁用' : '下次启动启用' });
      toggle.addEventListener('click', async () => {
        try {
          await saveGatewayServer(alias, { enabled: !enabled });
          status(`MCP 连接 ${alias} 已设为下次启动${enabled ? '禁用' : '启用'}；当前 Runtime 不变。`);
        } catch (error) { status(error.message, 'danger'); }
      });
      const remove = createNode(documentRef, 'button', { type: 'button', className: 'danger', text: '删除' });
      remove.addEventListener('click', async () => {
        const accepted = await confirmDestructive(documentRef, {
          title: '删除 MCP 连接',
          message: `连接别名：${alias}\n影响：从持久化配置中删除；当前 Runtime 保持不变，新建 Runtime 或重启后不再加载。`,
          confirmLabel: '删除', returnFocus: remove,
        });
        if (!accepted) return;
        try {
          const result = await api.request(`/gateway/servers/${encodeURIComponent(alias)}`, {
            method: 'DELETE', body: { expected_revision: state.gatewayRevision },
          });
          renderGateway(result);
          if (state.editingGatewayAlias === alias) resetGatewayServerForm();
          status(`MCP 连接 ${alias} 已从持久化配置删除；当前 Runtime 不变。`);
        } catch (error) { status(error.message, 'danger'); }
      });
      actions.append(edit, toggle, remove);
      item.append(
        createNode(documentRef, 'h3', { text: alias }),
        badges,
        createNode(documentRef, 'p', { text: `连接方式：${raw.transport || '未知'} · Broker 置顶工具：${pins.length}` }),
        createNode(documentRef, 'p', { className: 'muted', text: '凭据值和内部引用不会显示；使用表单编辑时会保留未显示的凭据。' }),
        actions,
      );
      summary.append(item);
    }

    const exposureRoot = byId('gatewayExposureReport');
    exposureRoot.replaceChildren();
    const report = payload.active_status?.exposure_report;
    if (!report) {
      exposureRoot.append(createNode(documentRef, 'p', { className: 'muted', text: '当前 Runtime 没有可用的上游工具暴露报告。' }));
    } else {
      exposureRoot.append(
        createNode(documentRef, 'p', { text: `Direct 直接暴露：${report.direct?.count || 0} 个工具 / ${report.direct?.definition_bytes || 0} 字节` }),
        createNode(documentRef, 'p', { text: `Broker 目录：共 ${report.catalog?.count || 0} 个 / 其中 ${report.catalog?.broker_only_count || 0} 个仅 Broker 可见` }),
        createNode(documentRef, 'p', { className: 'muted', text: '仅统计上游公开工具定义；不包含本地工具或 Admin 工具。' }),
      );
      for (const item of report.largest_public_definitions || []) {
        exposureRoot.append(createNode(documentRef, 'p', { className: 'muted', text: `${item.name}：${item.definition_bytes} 字节${item.direct ? ' · Direct' : ' · 仅 Broker'}` }));
      }
    }
    updateGatewayExposurePreview();
    renderOverviewDashboard();
  }

  async function loadGateway() { const payload = await api.request('/gateway'); renderGateway(payload); return payload; }

  async function loadOAuth() {
    const collectionControl = byId('oauthCollection');
    const reloadControl = byId('reloadOAuth');
    const collection = collectionControl.value;
    const payload = await fetchOAuthCollection(api, state.overviewStatus, collection);
    const available = payload.available !== false;
    collectionControl.disabled = !available;
    reloadControl.disabled = !available;
    if (!available) {
      state.oauthClients = [];
      byId('oauthList').replaceChildren(createNode(documentRef, 'p', {
        className: 'muted',
        text: translateUi('OAuth 模式未启用。当前服务使用静态 bearer token；其他 Admin 功能仍可正常使用。如需管理 OAuth 客户端，请使用 --oauth-mode 重启服务。'),
      }));
      return payload;
    }
    if (collection === 'clients') state.oauthClients = payload.items || [];
    renderOAuthItems(byId('oauthList'), payload.items || [], collection, async (resource, id, action, button) => {
      const accepted = await confirmDestructive(documentRef, {
        title: `OAuth ${action}`,
        message: `Resource: ${resource}\nID: ${id}\n影响：仅对该精确 ID 执行幂等状态变更。`,
        confirmLabel: action, returnFocus: button,
      });
      if (!accepted) return;
      const result = await api.request(`/oauth/${encodeURIComponent(resource)}/${encodeURIComponent(id)}/${encodeURIComponent(action)}`, { method: 'POST', body: {} });
      status(`OAuth ${action} 完成，实际影响数量：${result.affected_count || 0}。`);
      await loadOAuth();
    }, async (clientId, action, button) => {
      if (action === 'configure') {
        openClientPasswordDialog(clientId, button);
        return;
      }
      if (action === 'view') {
        await openClientPasswordViewDialog(clientId, button);
        return;
      }
      const accepted = await confirmDestructive(documentRef, {
        title: '改用全局 OAuth 密码',
        message: `Client ID: ${clientId}\n影响：立即删除这个 Client 的专属密码覆盖，并改用全局 OAuth Authorize 密码。`,
        confirmLabel: '改用全局密码', returnFocus: button,
      });
      if (!accepted) return;
      const result = await api.request(`/oauth/clients/${encodeURIComponent(clientId)}/authorization-password`, { method: 'DELETE' });
      status(`Client ${clientId} 已立即改用全局 OAuth Authorize 密码；实际影响数量：${result.affected_count || 0}。`);
      await loadOAuth();
    }, async (clientId, workspaceIds, button) => {
      if (!workspaceIds.length) {
        status('请至少允许该 OAuth Client 访问一个 Workspace。', 'danger');
        return;
      }
      const accepted = await confirmDestructive(documentRef, {
        title: '更新 OAuth Client Workspace 权限',
        message: `Client ID: ${clientId}\n允许的 Workspace IDs: ${workspaceIds.join(', ')}\n影响：立即用于后续 OAuth 授权；已有 Grant 和 Token 的 Workspace 保持不变。`,
        confirmLabel: '保存权限', returnFocus: button,
      });
      if (!accepted) return;
      const result = await api.request(`/oauth/clients/${encodeURIComponent(clientId)}/workspaces`, {
        method: 'PUT', body: { workspace_ids: workspaceIds },
      });
      status(`Client ${clientId} 的 Workspace 权限已更新并立即生效；下次 Authorize 可重新选择。`);
      await loadOAuth();
      return result;
    }, state.workspaces);
    renderOverviewDashboard();
    return payload;
  }

  async function loadSecrets() {
    const payload = await api.request('/secrets');
    state.secrets = payload.secrets || [];
    const root = byId('secretList');
    root.replaceChildren();
    for (const item of payload.secrets || []) {
      const card = createNode(documentRef, 'article', { className: 'card' });
      card.append(createNode(documentRef, 'strong', { text: item.name }));
      if (item.usage === 'oauth_authorization_password') {
        card.append(
          createNode(documentRef, 'span', { className: 'badge good', text: 'OAuth Authorize · 立即生效' }),
          createNode(documentRef, 'p', { className: 'muted', text: '这是当前授权页密码。为避免锁定 OAuth，请直接替换，不支持删除。' }),
        );
        root.append(card);
        continue;
      }
      const remove = createNode(documentRef, 'button', { type: 'button', className: 'danger', text: '删除' });
      remove.addEventListener('click', async () => {
        const accepted = await confirmDestructive(documentRef, {
          title: '删除 Secret Vault 条目',
          message: `Secret 名称: ${item.name}\n影响：删除该名称对应的一个 Vault 值；值本身不会显示。`,
          confirmLabel: '删除', returnFocus: remove,
        });
        if (!accepted) return;
        const result = await api.request(`/secrets/${encodeURIComponent(item.name)}`, { method: 'DELETE' });
        status(`Secret ${item.name} 删除影响数量：${result.affected_count || 0}。`);
        await loadSecrets();
      });
      card.append(remove);
      root.append(card);
    }
    if (!(payload.secrets || []).length) root.append(createNode(documentRef, 'p', { className: 'muted', text: 'Vault 中没有已配置名称。' }));
    renderOverviewDashboard();
  }

  async function loadConversations() {
    const workspaceId = byId('chatWorkspace').value;
    if (!workspaceId) return;
    const query = new URLSearchParams({ workspace_id: workspaceId, page: String(state.conversationPage), page_size: '20' });
    const search = byId('chatQuery').value.trim();
    if (search) query.set('query', search);
    const payload = await api.request(`/conversations?${query}`);
    state.conversationTotal = payload.total || 0;
    state.conversations = payload.items || [];
    byId('conversationPage').textContent = `第 ${payload.page || 1} 页`;
    byId('conversationPrev').disabled = state.conversationPage <= 1;
    byId('conversationNext').disabled = state.conversationPage * (payload.page_size || 20) >= state.conversationTotal;
    renderConversationItems(byId('conversationList'), payload.items || [], async (item) => {
      state.selectedConversation = { workspaceId: item.workspace_id, conversationId: item.conversation_id };
      state.messagePage = 1; state.contextPage = 1;
      await loadConversationDetail();
    });
    renderOverviewDashboard();
  }

  async function deleteChatResource(resource, workspaceId, identifier, button, detailMessage) {
    const accepted = await confirmDestructive(documentRef, {
      title: `删除 ${resource}`,
      message: `Workspace ID: ${workspaceId}\nObject ID: ${identifier}\n影响：${detailMessage}`,
      confirmLabel: '删除', returnFocus: button,
    });
    if (!accepted) return false;
    const result = await api.request(`/chat/${resource}/${encodeURIComponent(workspaceId)}/${encodeURIComponent(identifier)}`, { method: 'DELETE' });
    status(`删除完成，实际影响数量：${result.affected_count || 0}。`);
    return true;
  }

  async function loadConversationDetail() {
    const selected = state.selectedConversation;
    if (!selected) return;
    const query = new URLSearchParams({ message_page: String(state.messagePage), message_page_size: '50', context_page: String(state.contextPage), context_page_size: '50' });
    const payload = await api.request(`/conversations/${encodeURIComponent(selected.workspaceId)}/${encodeURIComponent(selected.conversationId)}`);
    renderConversationDetail(byId('conversationDetail'), { ...payload, ...payload.conversation }, {
      onDeleteMessage: async (message, button) => {
        if (await deleteChatResource('messages', selected.workspaceId, message.message_id, button, '最多删除 1 条 message。')) await loadConversationDetail();
      },
      onDeleteContext: async (entry, button) => {
        if (await deleteChatResource('context', selected.workspaceId, entry.context_id, button, '最多删除 1 条 context entry。')) await loadConversationDetail();
      },
      onDeleteConversation: async (_conversation, button) => {
        const accepted = await confirmDestructive(documentRef, {
          title: '删除 Conversation',
          message: `Workspace ID: ${selected.workspaceId}\nConversation ID: ${selected.conversationId}\n影响：删除该会话以及其所有 messages 和 context entries。`,
          confirmLabel: '删除会话', returnFocus: button,
        });
        if (!accepted) return;
        const result = await api.request(`/chat/conversations/${encodeURIComponent(selected.workspaceId)}/${encodeURIComponent(selected.conversationId)}`, { method: 'DELETE' });
        status(`会话删除：conversation=${result.affected_count || 0}, messages=${result.deleted_message_count || 0}, context=${result.deleted_context_count || 0}。`);
        state.selectedConversation = null;
        byId('conversationDetail').replaceChildren(createNode(documentRef, 'p', { className: 'muted', text: '会话已删除。' }));
        await loadConversations();
      },
      onMessagePage: async (page) => { state.messagePage = page; await loadConversationDetail(); },
      onContextPage: async (page) => { state.contextPage = page; await loadConversationDetail(); },
    });
  }

  async function refreshAll() {
    status('正在读取 Admin API…');
    await Promise.all([loadOverview(), loadSettings(), loadWorkspaces(), loadGateway(), loadSecrets()]);
    await Promise.all([loadOAuth(), loadConversations()]);
    renderOverviewDashboard();
    status('Admin 数据已刷新。');
  }

  async function bootstrapAdminSession() {
    try {
      const session = await restoreAdminSession(globalThis.fetch);
      state.csrfToken = String(session.csrf_token || '');
      state.authenticated = true;
      setAuthenticationUi(documentRef, true);
      await refreshAll();
    } catch (error) {
      state.csrfToken = '';
      state.authenticated = false;
      setAuthenticationUi(documentRef, false);
      if (!(error instanceof ApiError) || error.status !== 401) {
        status(error.message, 'danger');
      }
    }
  }

  byId('fakeReadonlyLabel').textContent = copy.FAKE_READONLY_COPY.label;
  byId('fakeReadonlyWarning').textContent = copy.FAKE_READONLY_COPY.warning;
  byId('fakeReadonlyEnable').textContent = copy.FAKE_READONLY_COPY.enable;

  for (const button of documentRef.querySelectorAll('.nav-item')) {
    button.addEventListener('click', () => showSection(button.dataset.section));
  }
  for (const control of documentRef.querySelectorAll('[data-open-section]')) {
    control.addEventListener('click', (event) => {
      event.preventDefault();
      showSection(control.dataset.openSection);
    });
  }
  byId('menuButton')?.addEventListener('click', () => {
    const open = !documentRef.body.classList.contains('sidebar-open');
    documentRef.body.classList.toggle('sidebar-open', open);
    byId('menuButton')?.setAttribute('aria-expanded', String(open));
  });
  byId('sidebarBackdrop')?.addEventListener('click', () => {
    documentRef.body.classList.remove('sidebar-open');
    byId('menuButton')?.setAttribute('aria-expanded', 'false');
  });
  byId('mobileMoreButton')?.addEventListener('click', () => {
    documentRef.body.classList.add('sidebar-open');
    byId('menuButton')?.setAttribute('aria-expanded', 'true');
  });
  byId('openAuthDialog')?.addEventListener('click', () => {
    byId('authDialog')?.showModal();
    byId('adminToken')?.focus();
  });
  documentRef.querySelectorAll('[data-close-auth]').forEach((control) => {
    control.addEventListener('click', () => byId('authDialog')?.close());
  });
  const openActivityLogs = () => {
    renderActivityLogs();
    byId('activityLogDialog')?.showModal();
  };
  byId('openActivityLogs')?.addEventListener('click', openActivityLogs);
  documentRef.querySelectorAll('[data-open-activity-logs]').forEach((control) => control.addEventListener('click', openActivityLogs));
  documentRef.querySelectorAll('[data-close-activity-logs]').forEach((control) => control.addEventListener('click', () => byId('activityLogDialog')?.close()));
  ['activityLogTime', 'activityLogLevel', 'activityLogComponent'].forEach((id) => byId(id)?.addEventListener('change', renderActivityLogs));
  byId('activityLogSearch')?.addEventListener('input', renderActivityLogs);
  byId('clearActivityLogs')?.addEventListener('click', () => {
    state.activityLogs = [];
    state.selectedActivityLogId = null;
    renderActivityLogSummary();
    renderActivityLogs();
  });
  byId('refreshCurrent')?.addEventListener('click', () => {
    if (!state.authenticated) {
      byId('authDialog')?.showModal();
      byId('adminToken')?.focus();
      return;
    }
    const loaders = {
      overview: refreshAll, system: refreshAll, settings: loadSettings, workspaces: loadWorkspaces,
      gateway: loadGateway, oauth: loadOAuth, secrets: loadSecrets, chat: loadConversations,
    };
    Promise.resolve(loaders[state.section]?.()).catch((error) => status(error.message, 'danger'));
  });
  documentRef.addEventListener('localechange', () => {
    renderOverviewDashboard();
    renderActivityLogs();
    const [title, subtitle] = sectionMeta[state.section] || sectionMeta.overview;
    if (byId('pageTitle')) byId('pageTitle').textContent = translateUi(title);
    if (byId('pageSubtitle')) byId('pageSubtitle').textContent = translateUi(subtitle);
  });
  byId('authForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const adminToken = byId('adminToken').value;
    byId('adminToken').value = '';
    try {
      const session = await createAdminSession(adminToken, globalThis.fetch);
      state.csrfToken = String(session.csrf_token || '');
      state.authenticated = true;
      setAuthenticationUi(documentRef, true);
      await refreshAll();
      byId('authDialog')?.close();
    } catch (error) {
      state.csrfToken = '';
      state.authenticated = false;
      setAuthenticationUi(documentRef, false);
      status(error.message, 'danger');
    }
  });
  byId('forgetToken').addEventListener('click', async () => {
    try {
      if (state.authenticated) {
        await revokeAdminSession(state.csrfToken, globalThis.fetch);
      }
      state.csrfToken = '';
      state.authenticated = false;
      setAuthenticationUi(documentRef, false);
      status('Admin Session 已注销并在服务器端立即吊销。');
      byId('authDialog')?.showModal();
      byId('adminToken').focus();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        state.csrfToken = '';
        state.authenticated = false;
        setAuthenticationUi(documentRef, false);
        status('Admin Session 已失效，请重新连接。', 'warning');
        byId('authDialog')?.showModal();
        byId('adminToken').focus();
        return;
      }
      status(error.message, 'danger');
    }
  });
  byId('refreshAll').addEventListener('click', () => refreshAll().catch((error) => status(error.message, 'danger')));
  byId('reloadSettings').addEventListener('click', () => loadSettings().then(() => status('Settings 已重新读取。')).catch((error) => status(error.message, 'danger')));
  byId('saveSettings').addEventListener('click', async () => {
    try {
      const result = await handleSettingsSave({ api, state, documentRef });
      status(result.conflict ? '检测到 stale revision；草稿已保留，请审阅后重新保存。' : 'Settings 已保存；查看 pending restart。', result.conflict ? 'warning' : '');
      await loadWorkspaces();
    } catch (error) { settingsPage.renderFormError(documentRef, error.message); status(error.message, 'danger'); }
  });
  byId('settingsPermission').addEventListener('change', () => {
    byId('permissionHelp').textContent = copy.permissionPresentation(byId('settingsPermission').value).description;
  });
  byId('reloadWorkspaces').addEventListener('click', () => loadWorkspaces().catch((error) => status(error.message, 'danger')));
  byId('workspaceAddForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const workspace = {
      id: byId('workspaceId').value.trim(), name: byId('workspaceName').value.trim(), root: byId('workspaceRoot').value.trim(),
      enabled: byId('workspaceEnabled').checked, default: byId('workspaceDefault').checked,
    };
    try {
      await api.request('/workspaces', { method: 'POST', body: { expected_revision: state.workspaceRevision, workspace } });
      event.currentTarget.reset(); byId('workspaceEnabled').checked = true;
      await Promise.all([loadWorkspaces(), loadSettings()]);
      status(`Workspace ${workspace.id} 已添加。`);
    } catch (error) { status(error.message, 'danger'); }
  });
  byId('reloadGateway').addEventListener('click', () => loadGateway().catch((error) => status(error.message, 'danger')));
  byId('saveGatewayCredentialPolicy')?.addEventListener('click', async () => {
    try {
      const result = await api.request('/gateway/credential-policy', {
        method: 'PUT',
        body: {
          expected_revision: state.gatewayRevision,
          credential_policy: byId('gatewayCredentialPolicy').value,
        },
      });
      renderGateway(result);
      status('凭据安全策略已保存。');
    } catch (error) { status(error.message, 'danger'); }
  });
  byId('newGatewayForm').addEventListener('click', () => resetGatewayServerForm({ focus: true }));
  byId('gatewayTransport').addEventListener('change', updateGatewayTransportFields);
  byId('cancelGatewayEdit').addEventListener('click', () => resetGatewayServerForm());
  byId('gatewayServerForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    gatewayFormError();
    try {
      const { alias, config } = readGatewayServerForm();
      const editing = Boolean(state.editingGatewayAlias);
      if (!editing && state.gateway?.persisted?.servers?.[alias]) {
        throw new Error(`连接 alias ${alias} 已存在；请从连接卡片选择编辑。`);
      }
      await saveGatewayServer(alias, config);
      resetGatewayServerForm();
      status(`MCP 连接 ${alias} 已${editing ? '更新' : '添加'}；新建 MCP Session/Runtime 或服务重启后生效。`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await loadGateway();
        gatewayFormError('配置版本冲突；已刷新持久化状态，请检查表单后重新保存。');
      } else gatewayFormError(error.message);
      status(error.message, 'danger');
    }
  });
  byId('newGatewayServer').addEventListener('click', () => {
    byId('gatewayDocument').value = JSON.stringify(gatewayServerTemplate(), null, 2);
    updateGatewayExposurePreview();
    byId('gatewayDocument').focus();
  });
  byId('gatewayDocument').addEventListener('input', updateGatewayExposurePreview);
  byId('clearGatewayDraft').addEventListener('click', () => { byId('gatewayDocument').value = ''; updateGatewayExposurePreview(); });
  byId('gatewayForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const draft = byId('gatewayDocument').value;
    try {
      const documentValue = JSON.parse(draft || '{"servers":{}}');
      if (containsCredentialControl(documentValue)) throw new Error('Gateway WebUI 草稿不得包含 credential、secret reference 或敏感 header/env 字段。');
      const result = await api.request('/gateway', { method: 'PUT', body: { expected_revision: state.gatewayRevision, document: documentValue } });
      byId('gatewayDocument').value = '';
      renderGateway(result);
      status(`Gateway 配置已持久化。restart_required=${Boolean(result.restart_required)}；新 MCP Session/Runtime 或服务重启后生效，不发送 list_changed。`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await loadGateway();
        status('Gateway revision 冲突；草稿已保留，persisted revision 已刷新。', 'warning');
      } else status(error.message, 'danger');
    }
  });
  byId('oauthCollection').addEventListener('change', () => loadOAuth().catch((error) => status(error.message, 'danger')));
  byId('reloadOAuth').addEventListener('click', () => loadOAuth().catch((error) => status(error.message, 'danger')));
  byId('clientPasswordCancel').addEventListener('click', () => closeClientPasswordDialog());
  byId('clientPasswordForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const clientId = state.clientPasswordClientId;
    if (state.clientPasswordMode === 'view') {
      closeClientPasswordDialog();
      return;
    }
    const value = byId('clientPasswordValue').value;
    try {
      await api.request(`/oauth/clients/${encodeURIComponent(clientId)}/authorization-password`, {
        method: 'PUT', body: { value },
      });
      closeClientPasswordDialog();
      status(`Client ${clientId} 的专属 OAuth Authorize 密码已保存并立即生效。`);
      await loadOAuth();
    } catch (error) {
      byId('clientPasswordValue').value = '';
      setPasswordVisibility(byId('clientPasswordValue'), byId('clientPasswordToggle'), false);
      clientPasswordError(error.message);
    }
  });
  byId('reloadSecrets').addEventListener('click', () => loadSecrets().catch((error) => status(error.message, 'danger')));
  byId('secretForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = byId('secretName').value.trim();
    const value = byId('secretValue').value;
    try {
      const result = await api.request(`/secrets/${encodeURIComponent(name)}`, { method: 'PUT', body: { value } });
      byId('secretValue').value = '';
      setPasswordVisibility(byId('secretValue'), byId('secretValueToggle'), false);
      status(result.oauth_applied_immediately
        ? 'OAuth Authorize 密码已更新并立即生效；旧密码已失效。'
        : `Secret ${name} 已配置；实际影响数量：${result.affected_count || 0}。`);
      await loadSecrets();
    } catch (error) { byId('secretValue').value = ''; status(error.message, 'danger'); }
  });
  byId('chatWorkspace').addEventListener('change', () => { state.conversationPage = 1; state.selectedConversation = null; loadConversations().catch((error) => status(error.message, 'danger')); });
  byId('searchConversations').addEventListener('click', () => { state.conversationPage = 1; loadConversations().catch((error) => status(error.message, 'danger')); });
  byId('reloadConversations').addEventListener('click', () => loadConversations().catch((error) => status(error.message, 'danger')));
  byId('conversationPrev').addEventListener('click', () => { if (state.conversationPage > 1) { state.conversationPage -= 1; loadConversations().catch((error) => status(error.message, 'danger')); } });
  byId('conversationNext').addEventListener('click', () => { state.conversationPage += 1; loadConversations().catch((error) => status(error.message, 'danger')); });

  resetGatewayServerForm();
  wirePasswordVisibilityToggles(documentRef);
  setAuthenticationUi(documentRef, false);
  void bootstrapAdminSession();

  return { state, api, refreshAll, loadSettings, loadWorkspaces, loadGateway, loadOAuth, loadSecrets, loadConversations, loadConversationDetail, showSection };
}

globalThis.McpAdminApp = {
  ApiError,
  sanitizeAdminValue,
  containsCredentialControl,
  restartImpactCount,
  createAdminSession,
  restoreAdminSession,
  revokeAdminSession,
  gatewayServerFromForm,
  gatewayServerTemplate,
  gatewayExposurePreview,
  createApiClient,
  fetchOAuthCollection,
  renderConversationItems,
  renderConversationDetail,
  renderOAuthItems,
  confirmDestructive,
  handleSettingsSave,
  setPasswordVisibility,
  setAuthenticationUi,
  initAdminApp,
};

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => initAdminApp(document));
}

export { ApiError, sanitizeAdminValue, containsCredentialControl, restartImpactCount, createAdminSession, restoreAdminSession, revokeAdminSession, gatewayServerFromForm, gatewayServerTemplate, gatewayExposurePreview, createApiClient, fetchOAuthCollection, renderConversationItems, renderConversationDetail, renderOAuthItems, confirmDestructive, handleSettingsSave, setPasswordVisibility, setAuthenticationUi, initAdminApp };
