import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canonicalWorkspaces,
  computeDirty,
  createWorkspace,
  hydrateSettings,
  refreshPersistedKeepingDraft,
  serializeSettings,
  setDefaultWorkspace,
} from '../src/settings-model.js';
import {
  FAKE_READONLY_COPY,
  permissionPresentation,
  telemetryPresentation,
} from '../src/settings-copy.js';

test('settings hydration preserves one default and revision-aware draft', () => {
  const state = hydrateSettings({
    active: { permission_mode: 'safe', workspace_catalog: [{ id: 'a', root: '/a', enabled: true, default: true }] },
    persisted: { permission_mode: 'trusted', workspace_catalog: [{ id: 'a', root: '/a', enabled: true, default: false }], default_workspace_id: 'a' },
    persisted_revision: 'rev-1',
    pending_restart: ['permission_mode'],
  });
  assert.equal(state.draft.workspace_catalog[0].default, true);
  assert.equal(state.persistedRevision, 'rev-1');
  assert.equal(computeDirty(state.draft, state.persisted), false);
  assert.deepEqual(state.pendingRestart, ['permission_mode']);
});

test('stale refresh keeps user draft and adopts latest persisted revision', () => {
  const state = hydrateSettings({ persisted: { port: 8000 }, persisted_revision: 'rev-old' });
  state.draft.port = 9000;
  const refreshed = refreshPersistedKeepingDraft(state, {
    active: { port: 8000 },
    persisted: { port: 8100 },
    persisted_revision: 'rev-new',
    pending_restart: ['port'],
  });
  assert.equal(refreshed.draft.port, 9000);
  assert.equal(refreshed.persisted.port, 8100);
  assert.equal(refreshed.persistedRevision, 'rev-new');
  assert.equal(refreshed.conflict.code, 'stale_revision');
});

test('workspace helpers maintain one enabled default', () => {
  const rows = canonicalWorkspaces([
    { id: 'a', root: '/a', enabled: true, default: true },
    { id: 'b', root: '/b', enabled: true, default: true },
  ], 'b');
  assert.deepEqual(rows.map((item) => item.default), [false, true]);
  assert.deepEqual(setDefaultWorkspace(rows, 'a').map((item) => item.default), [true, false]);
  assert.throws(() => setDefaultWorkspace([{ id: 'x', enabled: false }], 'x'));
  assert.equal(createWorkspace([{ id: 'ws-new' }]).id, 'ws-new-1');
  assert.equal(serializeSettings({ workspace_catalog: rows, default_workspace_id: 'b' }).workspace, '/b');
});

test('permission copy does not claim the fixed catalog hides mutation tools', () => {
  const safe = permissionPresentation('safe');
  assert.match(safe.description, /完整固定工具目录/);
  assert.match(safe.description, /不会隐藏或禁用 mutation tools/);
  assert.match(FAKE_READONLY_COPY.warning, /不会隐藏工具/);
  assert.match(FAKE_READONLY_COPY.warning, /不会.*阻止 mutation/);
});

test('telemetry presentation supports enabled, off and debug states', () => {
  assert.equal(telemetryPresentation('enabled').mode, 'enabled');
  assert.equal(telemetryPresentation('off').mode, 'off');
  assert.equal(telemetryPresentation('debug').mode, 'debug');
  assert.equal(telemetryPresentation(undefined).mode, 'unknown');
});
