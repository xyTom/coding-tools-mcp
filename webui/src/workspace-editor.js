function element(documentRef, tag, options = {}) {
  const node = documentRef.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = String(options.text);
  if (options.type) node.type = options.type;
  if (options.id) node.id = options.id;
  if (options.title) node.title = options.title;
  return node;
}

function renderWorkspaceRows(container, workspaces, handlers = {}) {
  const documentRef = container.ownerDocument || document;
  container.replaceChildren();
  if (!Array.isArray(workspaces) || workspaces.length === 0) {
    container.append(element(documentRef, 'p', { className: 'muted', text: '尚未登记 Workspace。' }));
    return;
  }
  for (const workspace of workspaces) {
    const card = element(documentRef, 'article', { className: 'workspace-card' });
    card.dataset.workspaceId = String(workspace.id || '');
    const heading = element(documentRef, 'div', { className: 'workspace-heading' });
    const title = element(documentRef, 'h3', { text: workspace.name || workspace.id || 'Unnamed Workspace' });
    const badge = element(documentRef, 'span', {
      className: `badge ${workspace.default ? 'good' : workspace.enabled === false ? 'danger' : ''}`,
      text: workspace.default ? '默认' : workspace.enabled === false ? '已禁用' : '已启用',
    });
    heading.append(title, badge);
    card.append(heading);

    const ids = element(documentRef, 'dl', { className: 'compact-definition' });
    for (const [label, value] of [['ID', workspace.id], ['Root', workspace.root]]) {
      const dt = element(documentRef, 'dt', { text: label });
      const dd = element(documentRef, 'dd');
      const code = element(documentRef, 'code', { text: value || '—' });
      dd.append(code);
      ids.append(dt, dd);
    }
    card.append(ids);

    const actions = element(documentRef, 'div', { className: 'button-row' });
    const check = element(documentRef, 'button', { type: 'button', className: 'secondary', text: '检查' });
    check.addEventListener('click', () => handlers.onCheck?.(workspace, check));
    actions.append(check);
    if (!workspace.default && workspace.enabled !== false) {
      const makeDefault = element(documentRef, 'button', { type: 'button', className: 'secondary', text: '设为默认' });
      makeDefault.addEventListener('click', () => handlers.onDefault?.(workspace, makeDefault));
      actions.append(makeDefault);
      const disable = element(documentRef, 'button', { type: 'button', className: 'danger', text: '禁用' });
      disable.addEventListener('click', () => handlers.onDisable?.(workspace, disable));
      actions.append(disable);
    }
    card.append(actions);
    container.append(card);
  }
}

function populateWorkspaceSelect(select, workspaces, selectedId = '') {
  const documentRef = select.ownerDocument || document;
  select.replaceChildren();
  for (const workspace of workspaces || []) {
    if (workspace.enabled === false) continue;
    const option = documentRef.createElement('option');
    option.value = String(workspace.id || '');
    option.textContent = `${workspace.name || workspace.id} (${workspace.id})`;
    option.selected = option.value === selectedId;
    select.append(option);
  }
}

globalThis.McpWorkspaceEditor = { renderWorkspaceRows, populateWorkspaceSelect };
export { renderWorkspaceRows, populateWorkspaceSelect };
