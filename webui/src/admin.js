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

function createApiClient(getToken, fetchImpl = globalThis.fetch) {
  async function request(path, options = {}) {
    const token = String(getToken?.() || '');
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    if (token) headers.set('Authorization', `Bearer ${token}`);
    const response = await fetchImpl(`/admin/api${path}`, {
      method: options.method || 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: 'same-origin',
      cache: 'no-store',
    });
    let payload = {};
    try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok) throw new ApiError(response.status, payload);
    return payload;
  }
  return { request };
}

function createNode(documentRef, tag, options = {}) {
  const node = documentRef.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = String(options.text);
  if (options.type) node.type = options.type;
  if (options.id) node.id = options.id;
  return node;
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
    const counts = createNode(documentRef, 'p', { className: 'muted', text: `Messages: ${item.message_count || 0} · Context: ${item.context_count || 0}` });
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

function renderOAuthItems(container, items, collection, onAction) {
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
    if (actions.childNodes.length) card.append(actions);
    container.append(card);
  }
}

function confirmDestructive(documentRef, { title, message, confirmLabel = '确认', returnFocus } = {}) {
  const dialog = documentRef.getElementById('confirmDialog');
  if (!dialog || typeof dialog.showModal !== 'function') {
    return Promise.resolve(globalThis.confirm ? globalThis.confirm(message || title || '确认操作？') : false);
  }
  documentRef.getElementById('confirmTitle').textContent = title || '确认操作';
  documentRef.getElementById('confirmMessage').textContent = message || '';
  documentRef.getElementById('confirmAccept').textContent = confirmLabel;
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
  const model = globalThis.McpSettingsModel;
  const copy = globalThis.McpSettingsCopy;
  const workspaceEditor = globalThis.McpWorkspaceEditor;
  const settingsPage = globalThis.McpSettingsPage;
  const state = {
    token: '', settings: null, workspaces: [], workspaceRevision: '', gateway: null,
    gatewayRevision: '', conversationPage: 1, conversationTotal: 0,
    selectedConversation: null, messagePage: 1, contextPage: 1,
  };
  const api = createApiClient(() => state.token);
  const byId = (id) => documentRef.getElementById(id);

  function status(message, kind = '') {
    const box = byId('globalStatus');
    if (!box) return;
    box.textContent = message;
    box.className = `status ${kind}`.trim();
  }

  function showSection(name) {
    for (const section of documentRef.querySelectorAll('.page-section')) {
      const active = section.id === `section-${name}`;
      section.hidden = !active;
      section.classList.toggle('active', active);
    }
    for (const button of documentRef.querySelectorAll('.nav-item')) {
      button.classList.toggle('active', button.dataset.section === name);
    }
    byId('mainContent')?.focus();
  }

  async function loadOverview() {
    const payload = await api.request('/status');
    byId('adminApiStatus').textContent = payload.admin_api ? '可用' : '不可用';
    byId('gatewayRuntimeStatus').textContent = payload.gateway?.available ? '已配置（快照不可变）' : '不可用';
    byId('vaultStatus').textContent = payload.vault?.enabled ? '已启用' : '未启用';
    const telemetry = copy.telemetryPresentation(payload.telemetry);
    byId('telemetryStatus').textContent = telemetry.label;
    byId('telemetryDetail').textContent = telemetry.detail;
    byId('telemetryDisableHelp').textContent = copy.TELEMETRY_DISABLE_HELP;
    byId('fakeReadonlyStatus').textContent = payload.runtime?.annotation_override === 'fake_readonly' ? '已启用' : payload.runtime ? '未启用' : '未报告';
    return payload;
  }

  function safeSettingsPayload(payload) {
    return { ...payload, active: sanitizeAdminValue(payload.active), persisted: sanitizeAdminValue(payload.persisted) };
  }

  async function loadSettings({ preserveDraft = false } = {}) {
    const payload = safeSettingsPayload(await api.request('/settings'));
    state.settings = preserveDraft && state.settings
      ? model.refreshPersistedKeepingDraft(state.settings, payload)
      : model.hydrateSettings(payload);
    settingsPage.renderSettingsForm(documentRef, state.settings, copy.permissionPresentation);
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
    return payload;
  }

  function renderGateway(payload) {
    state.gateway = payload;
    state.gatewayRevision = payload.persisted_revision || '';
    byId('gatewayRestartRequired').textContent = payload.restart_required ? '是' : '否';
    byId('gatewayRevision').textContent = state.gatewayRevision || '—';
    const summary = byId('gatewaySummary');
    summary.replaceChildren();
    const servers = payload.persisted?.servers || {};
    const aliases = Object.keys(servers);
    if (!aliases.length) summary.append(createNode(documentRef, 'p', { className: 'muted', text: '没有持久化 Gateway server。' }));
    for (const alias of aliases) {
      const raw = servers[alias] || {};
      const item = createNode(documentRef, 'article', { className: 'card' });
      item.append(
        createNode(documentRef, 'h4', { text: alias }),
        createNode(documentRef, 'p', { text: `Transport: ${raw.transport || 'unknown'} · Enabled: ${raw.enabled !== false}` }),
        createNode(documentRef, 'p', { className: 'muted', text: 'Credential fields are configured but intentionally hidden.' }),
      );
      summary.append(item);
    }
  }

  async function loadGateway() { const payload = await api.request('/gateway'); renderGateway(payload); return payload; }

  async function loadOAuth() {
    const collection = byId('oauthCollection').value;
    const payload = await api.request(`/oauth/${encodeURIComponent(collection)}`);
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
    });
    return payload;
  }

  async function loadSecrets() {
    const payload = await api.request('/secrets');
    const root = byId('secretList');
    root.replaceChildren();
    for (const item of payload.secrets || []) {
      const card = createNode(documentRef, 'article', { className: 'card' });
      card.append(createNode(documentRef, 'strong', { text: item.name }));
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
  }

  async function loadConversations() {
    const workspaceId = byId('chatWorkspace').value;
    if (!workspaceId) return;
    const query = new URLSearchParams({ workspace_id: workspaceId, page: String(state.conversationPage), page_size: '20' });
    const search = byId('chatQuery').value.trim();
    if (search) query.set('query', search);
    const payload = await api.request(`/chat/conversations?${query}`);
    state.conversationTotal = payload.total || 0;
    byId('conversationPage').textContent = `第 ${payload.page || 1} 页`;
    byId('conversationPrev').disabled = state.conversationPage <= 1;
    byId('conversationNext').disabled = state.conversationPage * (payload.page_size || 20) >= state.conversationTotal;
    renderConversationItems(byId('conversationList'), payload.items || [], async (item) => {
      state.selectedConversation = { workspaceId: item.workspace_id, conversationId: item.conversation_id };
      state.messagePage = 1; state.contextPage = 1;
      await loadConversationDetail();
    });
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
    const payload = await api.request(`/chat/conversations/${encodeURIComponent(selected.workspaceId)}/${encodeURIComponent(selected.conversationId)}?${query}`);
    renderConversationDetail(byId('conversationDetail'), payload, {
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
    status('Admin 数据已刷新。');
  }

  byId('fakeReadonlyLabel').textContent = copy.FAKE_READONLY_COPY.label;
  byId('fakeReadonlyWarning').textContent = copy.FAKE_READONLY_COPY.warning;
  byId('fakeReadonlyEnable').textContent = copy.FAKE_READONLY_COPY.enable;

  for (const button of documentRef.querySelectorAll('.nav-item')) {
    button.addEventListener('click', () => showSection(button.dataset.section));
  }
  byId('authForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    state.token = byId('adminToken').value;
    try { await refreshAll(); } catch (error) { status(error.message, 'danger'); }
  });
  byId('forgetToken').addEventListener('click', () => {
    state.token = '';
    byId('adminToken').value = '';
    status('Admin token 已从页面内存清除。');
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
  byId('clearGatewayDraft').addEventListener('click', () => { byId('gatewayDocument').value = ''; });
  byId('gatewayForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const draft = byId('gatewayDocument').value;
    try {
      const documentValue = JSON.parse(draft || '{"servers":{}}');
      if (containsCredentialControl(documentValue)) throw new Error('Gateway WebUI 草稿不得包含 credential、secret reference 或敏感 header/env 字段。');
      const result = await api.request('/gateway', { method: 'PUT', body: { expected_revision: state.gatewayRevision, document: documentValue } });
      byId('gatewayDocument').value = '';
      renderGateway(result);
      status(`Gateway 配置已持久化。restart_required=${Boolean(result.restart_required)}；现有 Runtime 未热加载。`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await loadGateway();
        status('Gateway revision 冲突；草稿已保留，persisted revision 已刷新。', 'warning');
      } else status(error.message, 'danger');
    }
  });
  byId('oauthCollection').addEventListener('change', () => loadOAuth().catch((error) => status(error.message, 'danger')));
  byId('reloadOAuth').addEventListener('click', () => loadOAuth().catch((error) => status(error.message, 'danger')));
  byId('reloadSecrets').addEventListener('click', () => loadSecrets().catch((error) => status(error.message, 'danger')));
  byId('secretForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = byId('secretName').value.trim();
    const value = byId('secretValue').value;
    try {
      const result = await api.request(`/secrets/${encodeURIComponent(name)}`, { method: 'PUT', body: { value } });
      byId('secretValue').value = '';
      status(`Secret ${name} 已配置；实际影响数量：${result.affected_count || 0}。`);
      await loadSecrets();
    } catch (error) { byId('secretValue').value = ''; status(error.message, 'danger'); }
  });
  byId('chatWorkspace').addEventListener('change', () => { state.conversationPage = 1; state.selectedConversation = null; loadConversations().catch((error) => status(error.message, 'danger')); });
  byId('searchConversations').addEventListener('click', () => { state.conversationPage = 1; loadConversations().catch((error) => status(error.message, 'danger')); });
  byId('reloadConversations').addEventListener('click', () => loadConversations().catch((error) => status(error.message, 'danger')));
  byId('conversationPrev').addEventListener('click', () => { if (state.conversationPage > 1) { state.conversationPage -= 1; loadConversations().catch((error) => status(error.message, 'danger')); } });
  byId('conversationNext').addEventListener('click', () => { state.conversationPage += 1; loadConversations().catch((error) => status(error.message, 'danger')); });

  return { state, api, refreshAll, loadSettings, loadWorkspaces, loadGateway, loadOAuth, loadSecrets, loadConversations, loadConversationDetail, showSection };
}

globalThis.McpAdminApp = {
  ApiError,
  sanitizeAdminValue,
  containsCredentialControl,
  createApiClient,
  renderConversationItems,
  renderConversationDetail,
  renderOAuthItems,
  confirmDestructive,
  handleSettingsSave,
  initAdminApp,
};

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => initAdminApp(document));
}

export { ApiError, sanitizeAdminValue, containsCredentialControl, createApiClient, renderConversationItems, renderConversationDetail, renderOAuthItems, confirmDestructive, handleSettingsSave, initAdminApp };
