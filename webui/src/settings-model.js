function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function canonicalWorkspaces(workspaces, defaultId) {
  const rows = Array.isArray(workspaces) ? workspaces.map((item) => ({ ...item })) : [];
  let resolved = defaultId || rows.find((item) => item.default)?.id || rows.find((item) => item.enabled !== false)?.id || rows[0]?.id || '';
  if (resolved && !rows.some((item) => item.id === resolved && item.enabled !== false)) {
    resolved = rows.find((item) => item.enabled !== false)?.id || '';
  }
  return rows.map((item) => ({ ...item, default: Boolean(resolved && item.id === resolved) }));
}

function canonicalSettings(settings = {}) {
  const result = clone(settings) || {};
  const catalog = canonicalWorkspaces(result.workspace_catalog, result.default_workspace_id);
  if (catalog.length) {
    result.workspace_catalog = catalog;
    result.default_workspace_id = catalog.find((item) => item.default)?.id || '';
    result.workspace = catalog.find((item) => item.default)?.root || result.workspace || '';
  }
  return result;
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function hydrateSettings(payload = {}) {
  const active = canonicalSettings(payload.active || {});
  const persisted = canonicalSettings(payload.persisted || {});
  return {
    active,
    persisted,
    draft: clone(persisted),
    persistedRevision: String(payload.persisted_revision || ''),
    pendingRestart: Array.isArray(payload.pending_restart) ? [...payload.pending_restart] : [],
    restartRequired: Boolean(payload.restart_required),
    conflict: null,
    schema: clone(payload.schema || {}),
  };
}

function refreshPersistedKeepingDraft(state, payload = {}) {
  const draft = clone(state.draft);
  const next = hydrateSettings(payload);
  next.draft = draft;
  next.conflict = {
    code: 'stale_revision',
    message: '服务器设置已被其他管理员更新。草稿已保留，请审阅最新 persisted revision 后再次保存。',
  };
  return next;
}

function computeDirty(draft, persisted) {
  return stableJson(canonicalSettings(draft || {})) !== stableJson(canonicalSettings(persisted || {}));
}

function serializeSettings(draft = {}) {
  return canonicalSettings(draft);
}

function createWorkspace(existing = []) {
  const ids = new Set(existing.map((item) => item.id));
  let index = 1;
  let id = 'ws-new';
  while (ids.has(id)) id = `ws-new-${index++}`;
  return { id, name: 'New Workspace', root: '', enabled: true, default: existing.length === 0 };
}

function setDefaultWorkspace(workspaces, workspaceId) {
  if (!workspaces.some((item) => item.id === workspaceId && item.enabled !== false)) {
    throw new Error('Only an enabled Workspace can be the default.');
  }
  return workspaces.map((item) => ({ ...item, default: item.id === workspaceId }));
}

globalThis.McpSettingsModel = {
  canonicalWorkspaces,
  canonicalSettings,
  hydrateSettings,
  refreshPersistedKeepingDraft,
  computeDirty,
  serializeSettings,
  createWorkspace,
  setDefaultWorkspace,
};

export { canonicalWorkspaces, canonicalSettings, hydrateSettings, refreshPersistedKeepingDraft, computeDirty, serializeSettings, createWorkspace, setDefaultWorkspace };
