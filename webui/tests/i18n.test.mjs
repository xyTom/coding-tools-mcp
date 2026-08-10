import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { EN_MESSAGES, initI18n, normalizeLocale, translateText } from '../src/i18n.js';

test('locale normalization supports Chinese and English variants', () => {
  assert.equal(normalizeLocale('zh-CN'), 'zh-CN');
  assert.equal(normalizeLocale('zh-TW'), 'zh-CN');
  assert.equal(normalizeLocale('en-US'), 'en');
  assert.equal(normalizeLocale('fr-FR'), 'zh-CN');
});

test('static interface copy translates to English and preserves spacing', () => {
  assert.equal(translateText('  添加工具连接  ', 'en'), '  Add tool connection  ');
  assert.equal(translateText('添加工具连接', 'zh-CN'), '添加工具连接');
  assert.equal(EN_MESSAGES['保存连接'], 'Save connection');
});

test('dynamic counts and editor titles translate without changing data', () => {
  assert.equal(translateText('12 个项目', 'en'), '12 projects');
  assert.equal(translateText('读取 500 条', 'en'), 'Load 500');
  assert.equal(translateText('编辑工具连接：research-files', 'en'), 'Edit tool connection: research-files');
  assert.equal(translateText('还有未保存的编辑，确认切换页面？', 'en'), 'There are unsaved edits. Continue with switching pages?');
  assert.equal(translateText('禁用客户端 codex-desktop？禁用会立即撤销其现有授权。', 'en'), 'Disable client codex-desktop? Disabling it immediately revokes its existing authorization.');
});

test('technical and user-provided text without Chinese is unchanged', () => {
  assert.equal(translateText('https://example.com/mcp', 'en'), 'https://example.com/mcp');
  assert.equal(translateText('CODING_TOOLS_MCP_TOKEN', 'en'), 'CODING_TOOLS_MCP_TOKEN');
});

test('Admin HTML and critical dynamic Gateway copy have complete English coverage', async () => {
  const html = await readFile(new URL('../src/admin.html', import.meta.url), 'utf8');
  const visibleText = [...html.matchAll(/>([^<>]+)</g)]
    .map((match) => match[1].trim())
    .filter((value) => /[\u3400-\u9fff]/.test(value));
  const attributes = [...html.matchAll(/(?:placeholder|title|aria-label)="([^"]+)"/g)]
    .map((match) => match[1].trim())
    .filter((value) => /[\u3400-\u9fff]/.test(value));
  const dynamicCopy = [
    '尚未添加 MCP 工具连接。',
    '下次启动启用',
    '下次启动禁用',
    'Broker 按需暴露',
    'Direct 全部直出',
    '凭据值和内部引用不会显示；使用表单编辑时会保留未显示的凭据。',
    '当前 Runtime 没有可用的上游工具暴露报告。',
    '配置版本冲突；已刷新持久化状态，请检查表单后重新保存。',
    'Authorize：专属密码',
    'Authorize：全局密码',
    '轮换专属密码',
    '设置专属密码',
    '改用全局密码',
    '改用全局 OAuth 密码',
    '未配置 Workspace 权限',
    '允许的 Workspaces',
    '保存 Workspace 权限',
    '当前 Runtime 没有可授权的 Workspace。',
    '更新 OAuth Client Workspace 权限',
    '保存权限',
  ];
  const missing = [];
  for (const source of new Set([...visibleText, ...attributes, ...dynamicCopy])) {
    const translated = translateText(source, 'en');
    if (/[\u3400-\u9fff]/.test(translated)) missing.push(source);
  }
  assert.deepEqual(missing, []);
});

test('Operator onboarding boundaries have explicit English translations', () => {
  const onboardingCopy = [
    '这是可选的 Codex Agent 工作台，不是 GPT/ChatGPT 网页。它在执行主机上调用已经登录的 Codex CLI。',
    '执行主机必须安装 Codex CLI，并已完成 Codex 登录（例如先运行 codex login）。本地 Workspace 使用本机；Runner Workspace 使用对应 Runner 主机。',
    'App bearer、Admin token、Codex 登录凭据是三套不同凭据。App bearer 登录 /app，Admin token 登录 /admin，Codex 登录凭据由 Codex CLI 自己管理。',
    '持久化由 Coding Tools MCP SQLite 与 Codex thread store 共同完成。前者保存 Agent Session 元数据，后者保存模型 thread 与对话连续性。',
  ];
  for (const source of onboardingCopy) {
    assert.doesNotMatch(translateText(source, 'en'), /[\u3400-\u9fff]/);
  }
});

test('language-toggle mutations settle instead of retriggering the observer forever', () => {
  const pending = [];
  let observerCallback;
  const label = {
    value: 'EN',
    get textContent() { return this.value; },
    set textContent(value) {
      this.value = String(value);
      pending.push({ type: 'childList', addedNodes: [] });
    },
  };
  const attributes = new Map();
  const button = {
    addEventListener() {},
    closest() { return null; },
    querySelector() { return label; },
    hasAttribute(name) { return attributes.has(name); },
    getAttribute(name) { return attributes.get(name) ?? null; },
    setAttribute(name, value) {
      attributes.set(name, String(value));
      pending.push({ type: 'attributes', target: button, addedNodes: [] });
    },
  };
  const documentElement = {
    nodeType: 1,
    dataset: {},
    querySelectorAll() { return []; },
  };
  const documentRef = {
    nodeType: 9,
    documentElement,
    querySelectorAll(selector) { return selector === '[data-language-toggle]' ? [button] : []; },
    createTreeWalker() { return { nextNode() { return null; } }; },
    dispatchEvent() {},
  };
  class TestMutationObserver {
    constructor(callback) { observerCallback = callback; }
    observe() {}
  }
  const replacements = {
    document: documentRef,
    MutationObserver: TestMutationObserver,
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } },
    Node: { TEXT_NODE: 3, ELEMENT_NODE: 1, DOCUMENT_NODE: 9 },
    NodeFilter: { SHOW_TEXT: 4 },
    navigator: { language: 'zh-CN' },
  };
  const originals = new Map(
    Object.keys(replacements).map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
  );
  try {
    for (const [name, value] of Object.entries(replacements)) {
      Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
    }
    initI18n();
    let cycles = 0;
    while (pending.length && cycles < 10) {
      const batch = pending.splice(0);
      observerCallback(batch);
      cycles += 1;
    }
    assert.equal(pending.length, 0, 'observer callback must stop scheduling equivalent mutations');
    assert.ok(cycles < 10, 'observer callback must settle before the safety limit');
  } finally {
    for (const [name, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
});
