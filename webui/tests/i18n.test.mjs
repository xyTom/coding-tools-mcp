import assert from 'node:assert/strict';
import test from 'node:test';

import { EN_MESSAGES, normalizeLocale, translateText } from '../src/i18n.js';

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
