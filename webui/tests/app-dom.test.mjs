import assert from 'node:assert/strict';
import test from 'node:test';

import {
  renderApprovalPrompt,
  renderContextChanged,
  renderEventHistory,
  renderSessionList,
  renderWorkspaceOptions,
} from '../src/app/app.js';

class FakeClassList {
  constructor() { this.values = new Set(); }
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
    this.classList = new FakeClassList();
    this.className = '';
    this.disabled = false;
    this.hidden = false;
    this.value = '';
    this._text = text;
  }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  set textContent(value) { this._text = String(value ?? ''); this.children = []; }
  append(...nodes) {
    for (const node of nodes) {
      this.children.push(typeof node === 'string' ? this.ownerDocument.createTextNode(node) : node);
    }
  }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  addEventListener(type, callback) {
    const values = this.listeners.get(type) || [];
    values.push(callback);
    this.listeners.set(type, values);
  }
  click() {
    for (const callback of this.listeners.get('click') || []) callback({ type: 'click', currentTarget: this, preventDefault() {} });
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
}

class FakeDocument {
  createElement(tag) { return new FakeNode(this, tag); }
  createTextNode(text) { return new FakeNode(this, '#text', String(text)); }
}

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}

test('workspace picker renders only projected text and retains the selected workspace', () => {
  const documentRef = new FakeDocument();
  const select = new FakeNode(documentRef, 'select');
  renderWorkspaceOptions(select, [
    { id: 'ws-a', name: 'Workspace <A>', available: true },
    { id: 'ws-b', name: 'Workspace B', available: false },
  ], 'ws-a');
  assert.equal(select.children.length, 2);
  assert.match(select.textContent, /Workspace <A>/);
  assert.equal(select.children[0].value, 'ws-a');
  assert.equal(select.children[0].selected, true);
  assert.equal(select.children[1].disabled, true);
});

test('session list renders untrusted identifiers as text and wires resume/select actions', () => {
  const documentRef = new FakeDocument();
  const root = new FakeNode(documentRef, 'div');
  const calls = [];
  renderSessionList(root, [{
    session_id: '<img src=x onerror=evil()>',
    workspace_id: 'ws-a',
    backend_kind: 'codex',
    status: 'ready',
    updated_at: 20,
  }], {
    onSelect: (session) => calls.push(['select', session.session_id]),
    onResume: (session) => calls.push(['resume', session.session_id]),
  });
  assert.equal(descendants(root).some((node) => node.tagName === 'IMG'), false);
  assert.match(root.textContent, /<img src=x/);
  const buttons = descendants(root).filter((node) => node.tagName === 'BUTTON');
  buttons[0].click();
  buttons[1].click();
  assert.deepEqual(calls, [
    ['select', '<img src=x onerror=evil()>'],
    ['resume', '<img src=x onerror=evil()>'],
  ]);
});

test('event history, approval, and context drift render backend text without markup execution', () => {
  const documentRef = new FakeDocument();
  const events = new FakeNode(documentRef, 'div');
  renderEventHistory(events, [{
    sequence: 1,
    kind: 'assistant',
    method: 'turn/assistant',
    params: { text: '<script>steal()</script>' },
  }]);
  assert.equal(descendants(events).some((node) => node.tagName === 'SCRIPT'), false);
  assert.match(events.textContent, /<script>steal/);

  const approval = new FakeNode(documentRef, 'div');
  const decisions = [];
  renderApprovalPrompt(approval, {
    approval_id: 'approval-a',
    params: { reason: '<svg onload=evil()>write file' },
  }, (decision) => decisions.push(decision));
  assert.equal(descendants(approval).some((node) => node.tagName === 'SVG'), false);
  const approvalButtons = descendants(approval).filter((node) => node.tagName === 'BUTTON');
  approvalButtons[0].click();
  approvalButtons[1].click();
  assert.deepEqual(decisions, ['approve', 'deny']);

  const changed = new FakeNode(documentRef, 'div');
  renderContextChanged(changed, true, ['HEAD changed', '<img onerror=evil()>']);
  assert.equal(descendants(changed).some((node) => node.tagName === 'IMG'), false);
  assert.match(changed.textContent, /HEAD changed/);
  assert.match(changed.textContent, /<img onerror=evil/);
});
