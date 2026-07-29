import { createWorkspace } from './settings-model.js';

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[char]));

export function renderWorkspaceEditor(root, workspaces = [], errors = {}) {
  if (!root) return;
  root.innerHTML = workspaces.map((workspace, index) => `
    <fieldset class="workspace-card" data-workspace-index="${index}">
      <legend>${esc(workspace.name || `工作区 ${index + 1}`)}</legend>
      <label>显示名称<input data-workspace-field="name" value="${esc(workspace.name)}" aria-describedby="workspace-error-${index}"></label>
      <label>文件夹路径<input data-workspace-field="root" value="${esc(workspace.root)}" placeholder="例如 G:\\Research" aria-describedby="workspace-error-${index}"></label>
      <div class="workspace-actions">
        <label class="checkline"><input type="checkbox" data-workspace-field="enabled" ${workspace.enabled !== false ? 'checked' : ''}> 启用</label>
        <label class="checkline"><input type="radio" name="defaultWorkspace" data-workspace-default ${workspace.default ? 'checked' : ''}> 默认工作区</label>
        <button type="button" class="secondary" data-workspace-check>检查目录</button>
        <button type="button" class="danger" data-workspace-delete ${workspaces.length === 1 ? 'disabled' : ''}>删除</button>
      </div>
      <p id="workspace-error-${index}" class="field-error" role="alert">${esc(errors[`workspace_catalog.${index}`] || errors.workspace_catalog || '')}</p>
    </fieldset>`).join('') || '<p class="muted">尚未添加工作区。</p>';
}

export function bindWorkspaceEditor(root, callbacks) {
  root?.addEventListener('input', (event) => {
    const card = event.target.closest('[data-workspace-index]');
    if (!card || !event.target.dataset.workspaceField) return;
    callbacks.update(Number(card.dataset.workspaceIndex), event.target.dataset.workspaceField, event.target.type === 'checkbox' ? event.target.checked : event.target.value);
  });
  root?.addEventListener('change', (event) => {
    const card = event.target.closest('[data-workspace-index]');
    if (!card) return;
    if (event.target.matches('[data-workspace-default]')) callbacks.setDefault(Number(card.dataset.workspaceIndex));
  });
  root?.addEventListener('click', (event) => {
    const card = event.target.closest('[data-workspace-index]');
    if (!card) return;
    if (event.target.matches('[data-workspace-delete]')) callbacks.remove(Number(card.dataset.workspaceIndex));
    if (event.target.matches('[data-workspace-check]')) callbacks.check(Number(card.dataset.workspaceIndex));
  });
}

export { createWorkspace };
