function setControlValue(documentRef, id, value) {
  const control = documentRef.getElementById(id);
  if (!control) return;
  if (control.type === 'checkbox') control.checked = Boolean(value);
  else control.value = value ?? '';
}

function renderSettingsForm(documentRef, state, permissionPresentation) {
  const draft = state?.draft || {};
  setControlValue(documentRef, 'settingsHost', draft.host || '');
  setControlValue(documentRef, 'settingsPort', draft.port || '');
  setControlValue(documentRef, 'settingsPermission', draft.permission_mode || 'safe');
  setControlValue(documentRef, 'settingsShellEnv', draft.shell_env_inherit || 'core');
  setControlValue(documentRef, 'settingsOauthServerUrl', draft.oauth_server_url || '');
  setControlValue(documentRef, 'settingsOauthCompatibility', draft.oauth_compatibility_mode || false);
  setControlValue(documentRef, 'settingsAllowedOrigins', Array.isArray(draft.allowed_origins) ? draft.allowed_origins.join('\n') : '');
  const presentation = permissionPresentation(draft.permission_mode || 'safe');
  const help = documentRef.getElementById('permissionHelp');
  if (help) help.textContent = presentation.description;
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
      item.textContent = field;
      pending.append(item);
    }
  }
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
