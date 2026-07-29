const clone = (value) => JSON.parse(JSON.stringify(value ?? {}));

function ordered(value) {
  if (Array.isArray(value)) return value.map(ordered).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map((key) => [key, ordered(value[key])]));
  return value;
}

export function canonicalWorkspaces(workspaces = [], defaultId = '') {
  const normalized = workspaces.map((item) => ({
    id: String(item.id || ''), name: String(item.name || ''), root: String(item.root || ''),
    enabled: item.enabled !== false, default: String(item.id || '') === defaultId,
  }));
  const chosen = defaultId || normalized.find((item) => item.default)?.id || normalized[0]?.id || '';
  return normalized.map((item) => ({ ...item, default: item.id === chosen }));
}

export function hydrateSettings(payload = {}) {
  const active = clone(payload.active || {});
  const persisted = clone(payload.persisted || active);
  const defaultId = persisted.default_workspace_id || active.default_workspace_id || '';
  persisted.workspace_catalog = canonicalWorkspaces(persisted.workspace_catalog || active.workspace_catalog || [], defaultId);
  persisted.default_workspace_id = defaultId || persisted.workspace_catalog[0]?.id || '';
  return { active, persisted, draft: clone(persisted), pendingFields: [...(payload.pending_fields || [])], fieldErrors: {} };
}

export function serializeSettings(draft = {}) {
  const settings = clone(draft);
  const catalog = canonicalWorkspaces(settings.workspace_catalog || [], settings.default_workspace_id || '');
  settings.workspace_catalog = catalog;
  settings.default_workspace_id = catalog.find((item) => item.default)?.id || '';
  settings.workspace = catalog.find((item) => item.default)?.root || settings.workspace || '';
  settings.allowed_origins = Array.isArray(settings.allowed_origins) ? settings.allowed_origins.filter(Boolean) : [];
  return settings;
}

export function computeDirty(draft, persisted) {
  return JSON.stringify(ordered(serializeSettings(draft))) !== JSON.stringify(ordered(serializeSettings(persisted)));
}

export function computePendingRestart(active, persisted, fields = []) {
  return fields.filter((field) => JSON.stringify(ordered(active?.[field])) !== JSON.stringify(ordered(persisted?.[field])));
}

export function applyValidationErrors(fieldErrors = {}) {
  return Object.fromEntries(Object.entries(fieldErrors).filter(([, message]) => typeof message === 'string' && message));
}

export function createWorkspace(existing = []) {
  const ids = new Set(existing.map((item) => item.id));
  let index = 1;
  let id = 'ws-new';
  while (ids.has(id)) id = `ws-new-${index++}`;
  return { id, name: '新工作区', root: '', enabled: true, default: existing.length === 0 };
}
