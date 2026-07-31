import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ApiError,
  confirmDestructive,
  createApiClient,
  handleSettingsSave,
  renderConversationDetail,
  renderConversationItems,
} from '../src/admin.js';
import '../src/settings-copy.js';
import { hydrateSettings } from '../src/settings-model.js';
import '../src/settings-page.js';
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
  });
  assert.equal(tags(detail).includes('IFRAME'), false);
  assert.equal(tags(detail).includes('SVG'), false);
  assert.match(detail.textContent, /<iframe src=evil>/);
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

test('API client keeps Admin token in request header, never URL or storage', async () => {
  let captured;
  const client = createApiClient(() => 'memory-only-token', async (url, options) => {
    captured = { url, options };
    return { ok: true, status: 200, async json() { return { ok: true }; } };
  });
  await client.request('/status');
  assert.equal(captured.url, '/admin/api/status');
  assert.equal(captured.url.includes('memory-only-token'), false);
  assert.equal(captured.options.headers.get('Authorization'), 'Bearer memory-only-token');
  assert.equal(captured.options.cache, 'no-store');
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
