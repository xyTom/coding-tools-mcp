function setControlValue(documentRef, id, value) {
  const control = documentRef.getElementById(id);
  if (!control) return;
  if (control.type === 'checkbox') control.checked = Boolean(value);
  else control.value = value ?? '';
}

function formatSettingValue(value) {
  if (value === undefined || value === null || value === '') return '—';
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

function sourceLabel(source) {
  return ({
    desktop_cli: 'Desktop CLI override',
    cli: 'CLI override',
    environment: '环境变量',
    persisted: 'persisted',
    default: 'default',
    unknown: '未报告',
  })[source] || source || '未报告';
}

function conclusionLabel(status = {}) {
  if (status.state === 'overridden') {
    if (status.source === 'desktop_cli') return '由 Desktop 启动参数覆盖；单纯重启不会生效，请在 Desktop profile 修改。';
    if (status.source === 'environment') return '由环境变量覆盖；使用相同环境重启仍不会采用 Persisted 值。';
    return '由启动命令参数覆盖；使用相同命令重启仍不会采用 Persisted 值。';
  }
  if (status.state === 'pending_restart') return 'Effective Persisted 与 Active 不同；重启后可生效。';
  if (status.state === 'in_sync_default') return '未显式配置；默认值与 Active 一致，无需重启。';
  return 'Active 与 Effective Persisted 一致，无需重启。';
}

function renderStatusRows(documentRef, state) {
  const statuses = state?.fieldStatus || {};
  const priority = ['host', 'port', 'execution_fs_mode', 'permission_mode', 'shell_env_inherit'];
  const fields = [...new Set([...priority, ...Object.keys(statuses)])].filter((field) => statuses[field]);
  for (const id of ['settingsStatusRows', 'systemSettingsStatusRows']) {
    const root = documentRef.getElementById(id);
    if (!root) continue;
    root.replaceChildren();
    if (!fields.length) {
      const row = documentRef.createElement('tr');
      const cell = documentRef.createElement('td');
      cell.textContent = '后端尚未报告配置来源。';
      cell.setAttribute('colspan', '5');
      row.append(cell);
      root.append(row);
      continue;
    }
    for (const field of fields) {
      const status = statuses[field] || {};
      const row = documentRef.createElement('tr');
      const fieldCell = documentRef.createElement('td');
      const fieldCode = documentRef.createElement('code');
      fieldCode.textContent = field;
      fieldCell.append(fieldCode);
      const activeCell = documentRef.createElement('td');
      activeCell.textContent = formatSettingValue(status.active);
      const persistedCell = documentRef.createElement('td');
      persistedCell.textContent = status.persisted_explicit
        ? formatSettingValue(status.persisted)
        : status.default_value !== undefined && status.default_value !== null
          ? `未设置（默认 ${formatSettingValue(status.effective_persisted)}）`
          : '未设置';
      const sourceCell = documentRef.createElement('td');
      sourceCell.textContent = sourceLabel(status.source);
      const conclusionCell = documentRef.createElement('td');
      conclusionCell.textContent = conclusionLabel(status);
      row.append(fieldCell, activeCell, persistedCell, sourceCell, conclusionCell);
      root.append(row);
    }
  }
}

function renderSettingsForm(documentRef, state, permissionPresentation) {
  const draft = state?.draft || {};
  setControlValue(documentRef, 'settingsHost', draft.host ?? state?.effectivePersisted?.host ?? '');
  setControlValue(documentRef, 'settingsPort', draft.port ?? state?.effectivePersisted?.port ?? '');
  setControlValue(documentRef, 'settingsPermission', draft.permission_mode || 'safe');
  setControlValue(documentRef, 'settingsShellEnv', draft.shell_env_inherit || 'core');
  setControlValue(documentRef, 'settingsOauthServerUrl', draft.oauth_server_url || '');
  setControlValue(documentRef, 'settingsOauthCompatibility', draft.oauth_compatibility_mode || false);
  setControlValue(documentRef, 'settingsAllowedOrigins', Array.isArray(draft.allowed_origins) ? draft.allowed_origins.join('\n') : '');
  const presentation = permissionPresentation(draft.permission_mode || 'safe');
  const help = documentRef.getElementById('permissionHelp');
  if (help) help.textContent = presentation.description;
  const managedFields = new Set(state?.managedFields || []);
  for (const [field, id, helpId] of [
    ['host', 'settingsHost', 'settingsHostHelp'],
    ['port', 'settingsPort', 'settingsPortHelp'],
    ['permission_mode', 'settingsPermission', 'permissionHelp'],
  ]) {
    const control = documentRef.getElementById(id);
    if (!control) continue;
    const managed = state?.launcher === 'desktop' && managedFields.has(field);
    control.disabled = managed;
    if (managed) {
      const fieldHelp = documentRef.getElementById(helpId);
      if (fieldHelp) fieldHelp.textContent = '由 Desktop profile 的启动参数管理；请在 Desktop profile 修改。';
    }
  }
  const managedNotice = documentRef.getElementById('settingsManagedNotice');
  if (managedNotice) {
    managedNotice.hidden = state?.launcher !== 'desktop';
    managedNotice.textContent = state?.launcher === 'desktop'
      ? '当前服务由 Desktop 管理。标记为 Desktop CLI override 的字段不会通过此页保存后在 Desktop 重启时生效，请前往 Desktop profile 修改。'
      : '';
  }
  const active = documentRef.getElementById('settingsActiveJson');
  if (active) active.textContent = JSON.stringify(state?.active || {}, null, 2);
  const persisted = documentRef.getElementById('settingsPersistedJson');
  if (persisted) persisted.textContent = JSON.stringify(state?.persisted || {}, null, 2);
  const pending = documentRef.getElementById('settingsPending');
  if (pending) {
    pending.replaceChildren();
    const values = state?.pendingRestart || [];
    if (!values.length) pending.append(documentRef.createTextNode('无待重启字段。'));
    for (const field of values) {
      const item = documentRef.createElement('li');
      item.textContent = `${field} — ${conclusionLabel(state?.fieldStatus?.[field] || { state: 'pending_restart' })}`;
      pending.append(item);
    }
  }
  renderStatusRows(documentRef, state);
  const revision = documentRef.getElementById('settingsRevision');
  if (revision) revision.textContent = state?.persistedRevision || '—';
  const conflict = documentRef.getElementById('settingsConflict');
  if (conflict) {
    conflict.textContent = state?.conflict?.message || '';
    conflict.hidden = !state?.conflict;
  }
}

function collectSettingsDraft(documentRef, previous = {}) {
  const allowedOrigins = String(documentRef.getElementById('settingsAllowedOrigins')?.value || '')
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    ...previous,
    host: String(documentRef.getElementById('settingsHost')?.value || '').trim(),
    port: Number(documentRef.getElementById('settingsPort')?.value || 0),
    permission_mode: String(documentRef.getElementById('settingsPermission')?.value || 'safe'),
    shell_env_inherit: String(documentRef.getElementById('settingsShellEnv')?.value || 'core'),
    oauth_server_url: String(documentRef.getElementById('settingsOauthServerUrl')?.value || '').trim(),
    oauth_compatibility_mode: Boolean(documentRef.getElementById('settingsOauthCompatibility')?.checked),
    allowed_origins: allowedOrigins,
  };
}

function renderFormError(documentRef, message, fieldId = '') {
  const box = documentRef.getElementById('settingsError');
  if (box) {
    box.textContent = message || '';
    box.hidden = !message;
  }
  for (const control of documentRef.querySelectorAll('[aria-invalid="true"]')) {
    control.removeAttribute('aria-invalid');
  }
  if (fieldId) {
    const control = documentRef.getElementById(fieldId);
    if (control) {
      control.setAttribute('aria-invalid', 'true');
      control.focus();
    }
  } else if (message && box) {
    box.focus();
  }
}

globalThis.McpSettingsPage = { renderSettingsForm, collectSettingsDraft, renderFormError };
export { renderSettingsForm, collectSettingsDraft, renderFormError };
