import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ApiError,
  confirmDestructive,
  createAdminSession,
  createApiClient,
  fetchOAuthCollection,
  handleSettingsSave,
  renderOAuthItems,
  renderConversationDetail,
  renderConversationItems,
  setPasswordVisibility,
  setAuthenticationUi,
} from '../src/admin.js';
import '../src/settings-copy.js';
import { hydrateSettings } from '../src/settings-model.js';
import { renderSettingsForm } from '../src/settings-page.js';
import { renderWorkspaceRows } from '../src/workspace-editor.js';

class FakeClassList {
  constructor(node) { this.node = node; this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  toggle(value, force) {
    const enabled = force === undefined ? !this.values.has(value) : Boolean(force);
    if (enabled) this.values.add(value); else this.values.delete(value);
    return enabled;
  }
  contains(value) { return this.values.has(value); }
}

class FakeNode {
  constructor(documentRef, tagName = '#text', text = '') {
    this.ownerDocument = documentRef;
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.classList = new FakeClassList(this);
    this.className = '';
    this.disabled = false;
    this.hidden = false;
    this.value = '';
    this.returnValue = '';
    this._text = text;
  }
  get childNodes() { return this.children; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  set textContent(value) { this._text = String(value ?? ''); this.children = []; }
  append(...nodes) {
    for (const node of nodes) this.children.push(typeof node === 'string' ? this.ownerDocument.createTextNode(node) : node);
  }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  addEventListener(type, callback) {
    const values = this.listeners.get(type) || [];
    values.push(callback); this.listeners.set(type, values);
  }
  removeEventListener(type, callback) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter((value) => value !== callback));
  }
  dispatchEvent(event) { for (const callback of this.listeners.get(event.type) || []) callback.call(this, event); }
  click() { this.dispatchEvent({ type: 'click', currentTarget: this, preventDefault() {} }); }
  focus() { this.ownerDocument.activeElement = this; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
}

class FakeDialog extends FakeNode {
  showModal() { this.open = true; }
  close(value = '') { this.returnValue = value; this.open = false; this.dispatchEvent({ type: 'close' }); }
}

class FakeDocument {
  constructor() { this.ids = new Map(); this.activeElement = null; }
  createElement(tag) { return tag === 'dialog' ? new FakeDialog(this, tag) : new FakeNode(this, tag); }
  createTextNode(text) { return new FakeNode(this, '#text', String(text)); }
  getElementById(id) { return this.ids.get(id) || null; }
  querySelectorAll() { return []; }
  register(id, node) { node.id = id; this.ids.set(id, node); return node; }
}

function tags(node) {
  return [node.tagName, ...node.children.flatMap(tags)];
}

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}

test('conversation summary and detail render untrusted text without creating markup', () => {
  const documentRef = new FakeDocument();
  const list = new FakeNode(documentRef, 'div');
  renderConversationItems(list, [{
    workspace_id: 'ws-a', conversation_id: 'conv-a', title: '<img src=x onerror=alert(1)>',
    preview: '<script>steal()</script>', message_count: 1, context_count: 1,
  }], () => {});
  assert.equal(tags(list).includes('IMG'), false);
  assert.equal(tags(list).includes('SCRIPT'), false);
  assert.match(list.textContent, /<img src=x/);
  assert.match(list.textContent, /<script>steal/);

  const detail = new FakeNode(documentRef, 'div');
  renderConversationDetail(detail, {
    conversation: { workspace_id: 'ws-a', conversation_id: 'conv-a', title: '<svg onload=evil()>' },
    messages: [{ message_id: 'm1', role: 'user', content: '<iframe src=evil></iframe>' }],
    messages_total: 1, message_page: 1, message_page_size: 50,
    contexts: [{ context_id: 'c1', kind: 'note', content: '<img onerror=evil()>' }],
    contexts_total: 1, context_page: 1, context_page_size: 50,
    continuation: { attempt: { attempt_id: '<script>alert("continuation")</script>' } },
    handoff: { note: '<img src=x onerror=alert("handoff")>' },
  });
  assert.equal(tags(detail).includes('IFRAME'), false);
  assert.equal(tags(detail).includes('SVG'), false);
  assert.equal(tags(detail).includes('SCRIPT'), false);
  assert.equal(tags(detail).includes('IMG'), false);
  assert.match(detail.textContent, /<iframe src=evil>/);
  assert.match(detail.textContent, /Work \/ Progress/);
  assert.match(detail.textContent, /<script>alert\("continuation"\)<\/script>/);
  assert.match(detail.textContent, /Handoff/);
  assert.match(detail.textContent, /<img src=x onerror=alert\(\\"handoff\\"\)>/);
});

test('conversation summaries and structured detail expose progress and controls', async () => {
  const documentRef = new FakeDocument();
  const list = new FakeNode(documentRef, 'div');
  renderConversationItems(list, [{
    workspace_id: 'ws-a', conversation_id: 'conv-a', title: 'Work',
    message_count: 2, context_count: 7, execution: { status: 'running' },
    progress: {
      changed_path_count: 3, explored_path_count: 4, validation_status: 'failed',
      unresolved_failure_count: 1, active_job_count: 2, pending_approval_count: 1,
    },
  }], () => {});
  assert.match(list.textContent, /Changes: 3/);
  assert.match(list.textContent, /Explored: 4/);
  assert.match(list.textContent, /Validation: failed/);
  assert.match(list.textContent, /Failures: 1/);
  assert.match(list.textContent, /Jobs: 2/);
  assert.match(list.textContent, /Approvals: 1/);

  const detail = new FakeNode(documentRef, 'div');
  const calls = [];
  renderConversationDetail(detail, {
    conversation: { workspace_id: 'ws-a', conversation_id: 'conv-a', title: 'Work' },
    executions: [{ session_id: 'session-a', status: 'recovering', backend_kind: 'codex-app-server' }],
    continuation: {
      attempt: { attempt_id: 'attempt-a' },
      previous_instruction: 'Continue the focused work',
      changes: { paths: ['src/a.py'], total: 1, truncated: false },
      exploration: { paths: ['docs/a.md'], total: 1, truncated: false },
      validation: { status: 'failed', failures: ['pytest:focus'] },
      jobs: { active: 1, recovering: 1, items: [{ id: 'job-a', status: 'recovering' }] },
      approvals: { pending: 1, items: [{ id: 'approval-a', status: 'pending' }] },
      checkpoints: ['saved'],
      suggested_actions: ['resolve_failure'],
    },
    handoff: { instruction: 'Continue the focused work' },
  }, {
    onCreateExecution: async (backendKind) => calls.push(['create', backendKind]),
    onSendTurn: async (sessionId, message) => calls.push(['send', sessionId, message]),
    onResume: async (sessionId) => calls.push(['resume', sessionId]),
    onClose: async (sessionId) => calls.push(['close', sessionId]),
    onValidate: async (sessionId, recipe) => calls.push(['validate', sessionId, recipe]),
    onApproval: async (approvalId, decision) => calls.push(['approval', approvalId, decision]),
  });
  assert.match(detail.textContent, /Work \/ Progress · attempt-a/);
  assert.match(detail.textContent, /Continue the focused work/);
  assert.match(detail.textContent, /src\/a\.py/);
  assert.match(detail.textContent, /docs\/a\.md/);
  assert.match(detail.textContent, /pytest:focus/);
  assert.match(detail.textContent, /Job job-a: recovering/);
  assert.match(detail.textContent, /saved/);
  assert.match(detail.textContent, /resolve_failure/);

  const buttons = descendants(detail).filter((node) => node.tagName === 'BUTTON');
  const button = (label) => buttons.find((node) => node.textContent === label);
  button('Start execution').click();
  const executionCard = descendants(detail).find((node) => node.tagName === 'SECTION' && node.textContent.includes('session-a · recovering'));
  const inputs = descendants(executionCard).filter((node) => node.tagName === 'INPUT');
  inputs[0].value = 'continue';
  inputs[1].value = 'pytest:focus';
  const cardButtons = descendants(executionCard).filter((node) => node.tagName === 'BUTTON');
  const cardButton = (label) => cardButtons.find((node) => node.textContent === label);
  cardButton('Send').click();
  cardButton('Resume').click();
  cardButton('Close').click();
  cardButton('Validate').click();
  const approvalRow = descendants(detail).find((node) => node.textContent.includes('Approval approval-a'));
  descendants(approvalRow).find((node) => node.tagName === 'BUTTON' && node.textContent === 'Approve').click();
  await Promise.resolve();
  assert.deepEqual(calls, [
    ['create', 'codex-app-server'],
    ['send', 'session-a', 'continue'],
    ['resume', 'session-a'],
    ['close', 'session-a'],
    ['validate', 'session-a', 'pytest:focus'],
    ['approval', 'approval-a', 'approve'],
  ]);
});

test('projection failures render bounded inline retry actions', async () => {
  const documentRef = new FakeDocument();
  const detail = new FakeNode(documentRef, 'div');
  let retries = 0;
  renderConversationDetail(detail, {
    conversation: { workspace_id: 'ws-a', conversation_id: 'conv-a', title: 'Work' },
    continuationError: 'projection unavailable',
    handoffError: 'handoff unavailable',
  }, { onRetryProjections: () => { retries += 1; } });
  assert.match(detail.textContent, /Continuation failed/);
  assert.match(detail.textContent, /projection unavailable/);
  assert.match(detail.textContent, /Handoff failed/);
  assert.match(detail.textContent, /handoff unavailable/);
  const retry = descendants(detail).filter((node) => node.tagName === 'BUTTON' && node.textContent === 'Retry');
  retry[0].click();
  retry[1].click();
  assert.equal(retries, 2);
});

test('workspace renderer keeps identifiers as text and wires exact actions', () => {
  const documentRef = new FakeDocument();
  const root = new FakeNode(documentRef, 'div');
  const calls = [];
  renderWorkspaceRows(root, [
    { id: 'a<script>', name: 'A<img>', root: 'C:/workspace/a', enabled: true, default: true },
    { id: 'b', name: 'B', root: 'C:/workspace/b', enabled: true, default: false },
  ], {
    onDefault: (workspace) => calls.push(['default', workspace.id]),
    onDisable: (workspace) => calls.push(['disable', workspace.id]),
  });
  assert.equal(tags(root).includes('SCRIPT'), false);
  const secondCard = root.children[1];
  const actions = secondCard.children.at(-1);
  actions.children[1].click();
  actions.children[2].click();
  assert.deepEqual(calls, [['default', 'b'], ['disable', 'b']]);
});

test('confirmation dialog restores focus and requires explicit confirm', async () => {
  const documentRef = new FakeDocument();
  const dialog = documentRef.register('confirmDialog', new FakeDialog(documentRef, 'dialog'));
  documentRef.register('confirmTitle', new FakeNode(documentRef, 'h2'));
  documentRef.register('confirmMessage', new FakeNode(documentRef, 'p'));
  documentRef.register('confirmAccept', new FakeNode(documentRef, 'button'));
  const trigger = new FakeNode(documentRef, 'button');
  const promise = confirmDestructive(documentRef, { title: 'Delete', message: 'Workspace a / object b', returnFocus: trigger });
  assert.equal(dialog.open, true);
  dialog.close('confirm');
  assert.equal(await promise, true);
  assert.equal(documentRef.activeElement, trigger);
});

test('Admin token is used only for the one-time session exchange body', async () => {
  let captured;
  const session = await createAdminSession('one-time-admin-token', async (url, options) => {
    captured = { url, options };
    return { ok: true, status: 201, async json() { return { ok: true, csrf_token: 'csrf-a' }; } };
  });
  assert.equal(session.csrf_token, 'csrf-a');
  assert.equal(captured.url, '/admin/api/session');
  assert.equal(captured.url.includes('one-time-admin-token'), false);
  assert.equal(captured.options.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(captured.options.body), { admin_token: 'one-time-admin-token' });
  assert.equal(captured.options.credentials, 'same-origin');
  assert.equal(captured.options.cache, 'no-store');
});

test('Admin API client relies on HttpOnly session cookie and CSRF, not Authorization', async () => {
  let captured;
  const client = createApiClient(() => 'csrf-memory-only', async (url, options) => {
    captured = { url, options };
    return { ok: true, status: 200, async json() { return { ok: true }; } };
  });
  await client.request('/settings', { method: 'PUT', body: { value: 1 } });
  assert.equal(captured.url, '/admin/api/settings');
  assert.equal(captured.options.headers.get('Authorization'), null);
  assert.equal(captured.options.headers.get('X-Admin-CSRF'), 'csrf-memory-only');
  assert.equal(captured.options.credentials, 'same-origin');
  assert.equal(captured.options.cache, 'no-store');
});

test('static bearer Admin mode treats OAuth as optional without requesting its unavailable backend', async () => {
  const calls = [];
  const api = {
    async request(path) {
      calls.push(path);
      throw new Error('OAuth endpoint must not be requested when status marks it unavailable.');
    },
  };

  const payload = await fetchOAuthCollection(api, {
    oauth: { available: false },
  }, 'clients');

  assert.deepEqual(payload, { available: false, items: [] });
  assert.deepEqual(calls, []);
});

test('OAuth-enabled Admin mode requests the selected collection', async () => {
  const calls = [];
  const api = {
    async request(path) {
      calls.push(path);
      return { items: [{ client_id: 'client-a' }] };
    },
  };

  const payload = await fetchOAuthCollection(api, {
    oauth: { available: true },
  }, 'clients');

  assert.equal(payload.available, true);
  assert.deepEqual(payload.items, [{ client_id: 'client-a' }]);
  assert.deepEqual(calls, ['/oauth/clients']);
});

test('successful authentication hides and clears the password field until sign-out', () => {
  const documentRef = new FakeDocument();
  const form = documentRef.register('authForm', new FakeNode(documentRef, 'form'));
  const connected = documentRef.register('authConnected', new FakeNode(documentRef, 'div'));
  const token = documentRef.register('adminToken', new FakeNode(documentRef, 'input'));
  token.value = 'memory-only-token';
  connected.hidden = true;

  setAuthenticationUi(documentRef, true);
  assert.equal(form.hidden, true);
  assert.equal(connected.hidden, false);
  assert.equal(token.value, '');

  setAuthenticationUi(documentRef, false);
  assert.equal(form.hidden, false);
  assert.equal(connected.hidden, true);
});

test('password visibility toggle updates type, label, and pressed state', () => {
  const documentRef = new FakeDocument();
  const input = new FakeNode(documentRef, 'input');
  const toggle = new FakeNode(documentRef, 'button');
  input.type = 'password';

  setPasswordVisibility(input, toggle, true);
  assert.equal(input.type, 'text');
  assert.equal(toggle.textContent, '隐藏');
  assert.equal(toggle.getAttribute('aria-pressed'), 'true');
  assert.equal(toggle.getAttribute('aria-label'), '隐藏密码');

  setPasswordVisibility(input, toggle, false);
  assert.equal(input.type, 'password');
  assert.equal(toggle.textContent, '显示');
  assert.equal(toggle.getAttribute('aria-pressed'), 'false');
});

test('OAuth client cards expose dedicated-password rotate and global-fallback actions', () => {
  const documentRef = new FakeDocument();
  const root = new FakeNode(documentRef, 'div');
  const calls = [];
  renderOAuthItems(
    root,
    [{
      client_id: 'client-a',
      display_name: 'Client A',
      authorize_login: { configured: true, mode: 'client' },
    }],
    'clients',
    () => {},
    (clientId, action) => calls.push([clientId, action]),
  );

  assert.match(root.textContent, /专属密码/);
  assert.match(root.textContent, /轮换专属密码/);
  assert.match(root.textContent, /查看专属密码/);
  assert.match(root.textContent, /改用全局密码/);
  const buttons = descendants(root).filter((node) => node.tagName === 'BUTTON');
  buttons.find((node) => node.textContent === '轮换专属密码').click();
  buttons.find((node) => node.textContent === '查看专属密码').click();
  buttons.find((node) => node.textContent === '改用全局密码').click();
  assert.deepEqual(calls, [['client-a', 'configure'], ['client-a', 'view'], ['client-a', 'reset']]);
});

test('OAuth client cards edit multiple allowed Workspaces immediately', () => {
  const documentRef = new FakeDocument();
  const root = new FakeNode(documentRef, 'div');
  const calls = [];
  renderOAuthItems(
    root,
    [{
      client_id: 'client-unbound',
      display_name: 'Unbound Client',
      workspace_ids: ['ws-a'],
      workspace_access: { configured: true, workspace_ids: ['ws-a'] },
      authorize_login: { configured: false, mode: 'global' },
    }],
    'clients',
    () => {},
    () => {},
    (clientId, workspaceId) => calls.push([clientId, workspaceId]),
    [
      { id: 'ws-a', name: 'Workspace A', enabled: true },
      { id: 'ws-b', name: 'Workspace B', enabled: true },
      { id: 'ws-disabled', name: 'Disabled', enabled: false },
    ],
  );

  assert.match(root.textContent, /允许的 Workspaces/);
  const checkboxes = descendants(root).filter(
    (node) => node.tagName === 'INPUT' && node.type === 'checkbox',
  );
  assert.equal(checkboxes.length, 2);
  assert.equal(checkboxes[0].checked, true);
  checkboxes[1].checked = true;
  const save = descendants(root).find(
    (node) => node.tagName === 'BUTTON' && node.textContent === '保存 Workspace 权限',
  );
  save.click();
  assert.deepEqual(calls, [['client-unbound', ['ws-a', 'ws-b']]]);
});

test('Desktop-managed settings show values, sources, and non-restartable conclusions', () => {
  const documentRef = new FakeDocument();
  const ids = [
    'settingsHost', 'settingsPort', 'settingsPermission', 'settingsShellEnv',
    'settingsOauthServerUrl', 'settingsOauthCompatibility', 'settingsAllowedOrigins',
    'permissionHelp', 'settingsHostHelp', 'settingsPortHelp', 'settingsManagedNotice',
    'settingsActiveJson', 'settingsPersistedJson', 'settingsPending',
    'settingsStatusRows', 'systemSettingsStatusRows', 'settingsRevision',
    'settingsConflict',
  ];
  for (const id of ids) {
    const tag = id.endsWith('Rows') ? 'tbody' : id.includes('Json') ? 'pre' : 'div';
    documentRef.register(id, new FakeNode(documentRef, tag));
  }
  const state = hydrateSettings({
    active: {
      host: '127.0.0.1', port: 8765, permission_mode: 'safe',
      execution_fs_mode: 'normal', shell_env_inherit: 'core',
    },
    persisted: { host: '0.0.0.0', port: 8765, permission_mode: 'safe', shell_env_inherit: 'core' },
    effective_persisted: {
      host: '0.0.0.0', port: 8765, permission_mode: 'safe',
      execution_fs_mode: 'normal', shell_env_inherit: 'core',
    },
    persisted_revision: 'rev-source',
    pending_restart: [],
    restart_required: false,
    launcher: 'desktop',
    managed_fields: ['host'],
    active_sources: { host: 'desktop_cli', execution_fs_mode: 'default' },
    field_status: {
      host: {
        active: '127.0.0.1', persisted: '0.0.0.0', persisted_explicit: true,
        effective_persisted: '0.0.0.0', source: 'desktop_cli', state: 'overridden',
      },
      execution_fs_mode: {
        active: 'normal', persisted: null, persisted_explicit: false,
        effective_persisted: 'normal', default_value: 'normal', source: 'default',
        state: 'in_sync_default',
      },
    },
  });

  renderSettingsForm(documentRef, state, () => ({ description: 'safe mode' }));

  assert.equal(documentRef.getElementById('settingsHost').disabled, true);
  assert.match(documentRef.getElementById('settingsHostHelp').textContent, /Desktop profile/);
  assert.match(documentRef.getElementById('settingsManagedNotice').textContent, /Desktop 管理/);
  assert.match(documentRef.getElementById('settingsStatusRows').textContent, /Desktop CLI override/);
  assert.match(documentRef.getElementById('settingsStatusRows').textContent, /0\.0\.0\.0/);
  assert.match(documentRef.getElementById('settingsStatusRows').textContent, /单纯重启不会生效/);
  assert.match(documentRef.getElementById('settingsStatusRows').textContent, /未设置（默认 normal）/);
  assert.match(documentRef.getElementById('settingsStatusRows').textContent, /无需重启/);
  assert.match(documentRef.getElementById('systemSettingsStatusRows').textContent, /Desktop CLI override/);
});


test('stale settings save keeps DOM draft and refreshes persisted revision', async () => {
  const documentRef = new FakeDocument();
  const ids = [
    'settingsHost', 'settingsPort', 'settingsPermission', 'settingsShellEnv',
    'settingsOauthServerUrl', 'settingsOauthCompatibility', 'settingsAllowedOrigins',
    'permissionHelp', 'settingsActiveJson', 'settingsPersistedJson', 'settingsPending',
    'settingsRevision', 'settingsConflict', 'settingsError',
  ];
  for (const id of ids) documentRef.register(id, new FakeNode(documentRef, id.includes('Json') ? 'pre' : 'input'));
  documentRef.getElementById('settingsPort').value = '9000';
  documentRef.getElementById('settingsPermission').value = 'safe';
  documentRef.getElementById('settingsShellEnv').value = 'core';
  const state = { settings: hydrateSettings({ persisted: { port: 8000, permission_mode: 'safe', shell_env_inherit: 'core' }, persisted_revision: 'rev-old' }) };
  const calls = [];
  const api = {
    async request(path, options = {}) {
      calls.push([path, options.method || 'GET']);
      if (options.method === 'PUT') throw new ApiError(409, { error: { code: 'stale_revision' } });
      return {
        active: { port: 8000, permission_mode: 'safe', shell_env_inherit: 'core' },
        persisted: { port: 8100, permission_mode: 'safe', shell_env_inherit: 'core' },
        persisted_revision: 'rev-new',
        pending_restart: ['port'],
      };
    },
  };
  const result = await handleSettingsSave({ api, state, documentRef });
  assert.equal(result.conflict, true);
  assert.equal(state.settings.draft.port, 9000);
  assert.equal(state.settings.persisted.port, 8100);
  assert.equal(state.settings.persistedRevision, 'rev-new');
  assert.equal(documentRef.activeElement, documentRef.getElementById('settingsConflict'));
  assert.deepEqual(calls, [['/settings', 'PUT'], ['/settings', 'GET']]);
});
