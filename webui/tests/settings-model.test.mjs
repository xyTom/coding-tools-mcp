import assert from 'node:assert/strict';
import test from 'node:test';

import { canonicalWorkspaces, computeDirty, createWorkspace, hydrateSettings, serializeSettings } from '../src/settings-model.js';

test('hydrate preserves persisted draft and a single default workspace', () => {
  const state = hydrateSettings({
    active: { workspace_catalog: [{ id:'a', name:'A', root:'/a', enabled:true, default:true }] },
    persisted: { workspace_catalog: [{ id:'a', name:'A', root:'/a', enabled:true, default:false }], default_workspace_id:'a' },
  });
  assert.equal(state.draft.workspace_catalog[0].default, true);
  assert.equal(computeDirty(state.draft, state.persisted), false);
});

test('new workspace has a stable non-conflicting id and the serializer syncs legacy workspace', () => {
  const first = createWorkspace([{ id:'ws-new' }]);
  assert.equal(first.id, 'ws-new-1');
  const settings = serializeSettings({ workspace_catalog:[{ id:'lab', name:'Lab', root:'/lab', enabled:true }], default_workspace_id:'lab' });
  assert.equal(settings.workspace, '/lab');
  assert.equal(canonicalWorkspaces(settings.workspace_catalog, 'lab')[0].default, true);
});
