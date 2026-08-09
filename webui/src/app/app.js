const apiModule = () => globalThis.OperatorAppApi || {};
const modelModule = () => globalThis.OperatorAppModel || {};

export function renderWorkspaceOptions(select, workspaces = [], selectedId = '') {
  if (!select) return;
  const documentRef = select.ownerDocument;
  select.replaceChildren();
  for (const workspace of workspaces) {
    const option = documentRef.createElement('option');
    option.value = String(workspace.id ?? '');
    option.textContent = String(workspace.name ?? workspace.id ?? 'Workspace');
    option.disabled = workspace.available === false;
    option.selected = option.value === selectedId;
    select.append(option);
  }
  if (selectedId && !select.value) select.value = selectedId;
}

export function renderSessionList(root, sessions = [], handlers = {}) {
  if (!root) return;
  const documentRef = root.ownerDocument;
  root.replaceChildren();
  if (!sessions.length) {
    const empty = documentRef.createElement('p');
    empty.className = 'operator-empty-copy';
    empty.textContent = '这个 Workspace 还没有 Agent Session。';
    root.append(empty);
    return;
  }
  for (const session of sessions) {
    const row = documentRef.createElement('article');
    row.className = 'session-row';

    const selectButton = documentRef.createElement('button');
    selectButton.type = 'button';
    selectButton.className = 'session-select';
    selectButton.addEventListener('click', () => handlers.onSelect?.(session));
    const title = documentRef.createElement('strong');
    title.textContent = String(session.title ?? session.session_id ?? 'Agent Session');
    const meta = documentRef.createElement('span');
    meta.textContent = `${String(session.backend_kind ?? 'unknown')} · ${String(session.status ?? 'unknown')}`;
    const id = documentRef.createElement('small');
    id.textContent = String(session.session_id ?? '');
    selectButton.append(title, meta, id);

    const resume = documentRef.createElement('button');
    resume.type = 'button';
    resume.className = 'session-resume';
    resume.textContent = '继续';
    resume.disabled = String(session.status ?? '').toLowerCase() === 'closed';
    resume.addEventListener('click', () => handlers.onResume?.(session));

    row.append(selectButton, resume);
    root.append(row);
  }
}

export function renderEventHistory(root, events = []) {
  if (!root) return;
  const documentRef = root.ownerDocument;
  root.replaceChildren();
  if (!events.length) {
    const empty = documentRef.createElement('div');
    empty.className = 'agent-empty-state';
    const title = documentRef.createElement('strong');
    title.textContent = '等待 Agent 消息';
    const copy = documentRef.createElement('p');
    copy.textContent = '发送第一条消息后，assistant、tool、progress 与 system 事件会出现在这里。';
    empty.append(title, copy);
    root.append(empty);
    return;
  }
  for (const event of events) {
    const article = documentRef.createElement('article');
    article.className = `agent-event event-${safeClass(event.kind)}`;
    const header = documentRef.createElement('header');
    const kind = documentRef.createElement('strong');
    kind.textContent = eventLabel(event);
    const sequence = documentRef.createElement('span');
    sequence.textContent = event.sequence ? `#${event.sequence}` : '';
    header.append(kind, sequence);
    const body = documentRef.createElement('pre');
    body.textContent = eventText(event);
    article.append(header, body);
    root.append(article);
  }
}

export function renderApprovalPrompt(root, approval, onDecision = () => {}) {
  if (!root) return;
  const documentRef = root.ownerDocument;
  root.replaceChildren();
  root.hidden = !approval;
  if (!approval) return;
  const panel = documentRef.createElement('article');
  panel.className = 'approval-card';
  const title = documentRef.createElement('strong');
  title.textContent = '需要你的审批';
  const reason = documentRef.createElement('p');
  reason.textContent = String(approval.params?.reason ?? approval.params?.message ?? 'Agent 请求继续执行受控操作。');
  const actions = documentRef.createElement('div');
  actions.className = 'approval-actions';
  const approve = documentRef.createElement('button');
  approve.type = 'button';
  approve.className = 'operator-button primary';
  approve.textContent = '批准';
  approve.addEventListener('click', () => onDecision('approve'));
  const deny = documentRef.createElement('button');
  deny.type = 'button';
  deny.className = 'operator-button secondary';
  deny.textContent = '拒绝';
  deny.addEventListener('click', () => onDecision('deny'));
  actions.append(approve, deny);
  panel.append(title, reason, actions);
  root.append(panel);
}

export function renderContextChanged(root, changed, changes = []) {
  if (!root) return;
  const documentRef = root.ownerDocument;
  root.replaceChildren();
  root.hidden = !changed;
  if (!changed) return;
  const title = documentRef.createElement('strong');
  title.textContent = 'Repository context 已变化';
  const copy = documentRef.createElement('p');
  copy.textContent = '当前 Workspace 与上次有效上下文不同。继续前请确认这些变化符合预期。';
  const list = documentRef.createElement('ul');
  for (const change of changes.slice(0, 20)) {
    const item = documentRef.createElement('li');
    item.textContent = String(change);
    list.append(item);
  }
  root.append(title, copy, list);
}

export function initOperatorApp(documentRef = globalThis.document) {
  const apiFactory = apiModule().createOperatorApiClient;
  const model = modelModule();
  if (!documentRef || typeof apiFactory !== 'function' || typeof model.projectWorkspaces !== 'function') return null;

  const state = {
    accessToken: '',
    workspaces: [],
    sessions: [],
    workspaceId: '',
    sessionView: null,
    streamAbort: null,
    api: null,
  };
  state.api = apiFactory({ getAccessToken: () => state.accessToken });

  const nodes = collectNodes(documentRef);
  const preferred = readRouteState();

  const setBanner = (message, tone = 'muted') => {
    if (!nodes.banner) return;
    nodes.banner.textContent = message;
    nodes.banner.dataset.tone = tone;
    nodes.banner.hidden = !message;
  };

  const renderShell = () => {
    renderWorkspaceOptions(nodes.workspacePicker, state.workspaces, state.workspaceId);
    renderWorkspaceOptions(nodes.newSessionWorkspace, state.workspaces, state.workspaceId);
    renderSessionList(nodes.sessionList, state.sessions, {
      onSelect: (session) => openSession(session.session_id),
      onResume: (session) => openSession(session.session_id),
    });
    if (nodes.newSessionButton) nodes.newSessionButton.disabled = state.workspaces.length === 0;
    if (nodes.startNewSessionEmpty) nodes.startNewSessionEmpty.disabled = state.workspaces.length === 0;
    renderSession(state, nodes, (decision) => decideApproval(decision));
  };

  const loadSessions = async ({ preserveSession = true } = {}) => {
    if (!state.workspaceId) {
      state.sessions = [];
      renderShell();
      return;
    }
    try {
      const payload = await state.api.listSessions(state.workspaceId);
      state.sessions = model.sortSessions(payload?.sessions ?? payload?.items ?? []);
      renderShell();
      if (!preserveSession && state.sessionView && state.sessionView.workspace_id !== state.workspaceId) {
        state.sessionView = null;
        stopStream();
        renderShell();
      }
    } catch (error) {
      handleApiError(error, setBanner, nodes.authDialog);
    }
  };

  const loadWorkspaces = async () => {
    setBanner('正在连接 Operator API…');
    try {
      const payload = await state.api.listWorkspaces();
      state.workspaces = model.projectWorkspaces(payload?.workspaces ?? payload?.items ?? []);
      const allowedIds = new Set(state.workspaces.map((workspace) => workspace.id));
      if (preferred.workspaceId && allowedIds.has(preferred.workspaceId)) state.workspaceId = preferred.workspaceId;
      if (!state.workspaceId || !allowedIds.has(state.workspaceId)) state.workspaceId = state.workspaces[0]?.id ?? '';
      setBanner(state.workspaces.length ? '' : '当前身份没有可用 Workspace。', state.workspaces.length ? 'muted' : 'warning');
      renderShell();
      await loadSessions();
      if (preferred.sessionId) await openSession(preferred.sessionId, { updateRoute: false });
    } catch (error) {
      handleApiError(error, setBanner, nodes.authDialog);
      state.workspaces = [];
      state.sessions = [];
      renderShell();
    }
  };

  const stopStream = () => {
    state.streamAbort?.abort();
    state.streamAbort = null;
  };

  const startStream = (sessionId) => {
    stopStream();
    if (!sessionId || typeof state.api.streamEvents !== 'function') return;
    const abort = new AbortController();
    state.streamAbort = abort;
    state.api.streamEvents(sessionId, {
      after: state.sessionView?.event_cursor ?? 0,
      signal: abort.signal,
      onEvent: (event) => {
        if (!state.sessionView || state.sessionView.session_id !== sessionId) return;
        state.sessionView = model.reduceAgentEvent(state.sessionView, event, 200);
        renderShell();
      },
    }).catch((error) => {
      if (error?.name === 'AbortError') return;
      setBanner('实时事件连接已中断；可以重新选择 Session 以恢复连接。', 'warning');
    });
  };

  const openSession = async (sessionId, { updateRoute = true } = {}) => {
    if (!sessionId) return;
    setBanner('正在恢复 Agent Session…');
    try {
      const payload = await state.api.getSession(sessionId);
      const detail = model.normalizeSessionDetail(payload?.session ?? payload);
      if (!detail) throw new Error('Operator API returned an invalid Agent Session.');
      state.workspaceId = detail.workspace_id || state.workspaceId;
      state.sessionView = model.createSessionViewState(detail);
      for (const event of payload?.events ?? []) {
        state.sessionView = model.reduceAgentEvent(state.sessionView, event, 200);
      }
      setBanner('');
      renderShell();
      if (updateRoute) writeRouteState(state.workspaceId, detail.session_id);
      startStream(detail.session_id);
    } catch (error) {
      handleApiError(error, setBanner, nodes.authDialog);
    }
  };

  const decideApproval = async (decision) => {
    const approval = state.sessionView?.pending_approval;
    if (!approval || !state.sessionView?.session_id) return;
    try {
      await state.api.decideApproval(state.sessionView.session_id, approval.approval_id, decision);
      state.sessionView = model.reduceAgentEvent(state.sessionView, {
        sequence: state.sessionView.event_cursor,
        kind: 'system',
        method: 'approval/resolved',
        params: { approval_id: approval.approval_id, decision },
      }, 200);
      renderShell();
    } catch (error) {
      handleApiError(error, setBanner, nodes.authDialog);
    }
  };

  nodes.workspacePicker?.addEventListener('change', async () => {
    state.workspaceId = nodes.workspacePicker.value;
    writeRouteState(state.workspaceId, '');
    await loadSessions({ preserveSession: false });
  });
  nodes.reloadWorkspaces?.addEventListener('click', () => loadWorkspaces());
  nodes.reloadSessions?.addEventListener('click', () => loadSessions());
  nodes.newSessionButton?.addEventListener('click', () => {
    renderWorkspaceOptions(nodes.newSessionWorkspace, state.workspaces, state.workspaceId);
    nodes.newSessionDialog?.showModal?.();
  });
  nodes.startNewSessionEmpty?.addEventListener('click', () => nodes.newSessionButton?.click?.());
  nodes.closeNewSession?.addEventListener('click', () => nodes.newSessionDialog?.close?.());
  nodes.newSessionForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const workspaceId = nodes.newSessionWorkspace?.value || state.workspaceId;
    try {
      const payload = model.createSessionPayload({
        workspaceId,
        instructions: nodes.newSessionInstructions?.value || '',
        backendKind: 'codex',
      });
      const created = await state.api.createSession(payload);
      nodes.newSessionDialog?.close?.();
      if (nodes.newSessionInstructions) nodes.newSessionInstructions.value = '';
      state.workspaceId = workspaceId;
      await loadSessions();
      const sessionId = created?.session?.session_id ?? created?.session_id;
      if (sessionId) await openSession(sessionId);
    } catch (error) {
      handleApiError(error, setBanner, nodes.authDialog);
    }
  });
  nodes.turnForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const message = String(nodes.turnInput?.value ?? '').trim();
    if (!message || !state.sessionView?.session_id) return;
    nodes.sendTurnButton && (nodes.sendTurnButton.disabled = true);
    try {
      await state.api.sendTurn(state.sessionView.session_id, message);
      if (nodes.turnInput) nodes.turnInput.value = '';
      setBanner('Turn 已提交，等待 Agent 事件…');
    } catch (error) {
      handleApiError(error, setBanner, nodes.authDialog);
    } finally {
      nodes.sendTurnButton && (nodes.sendTurnButton.disabled = false);
    }
  });
  nodes.interruptButton?.addEventListener('click', async () => {
    if (!state.sessionView?.session_id) return;
    try {
      await state.api.interrupt(state.sessionView.session_id);
      setBanner('已请求中断当前 Turn。', 'warning');
    } catch (error) {
      handleApiError(error, setBanner, nodes.authDialog);
    }
  });
  nodes.openAuth?.addEventListener('click', () => nodes.authDialog?.showModal?.());
  nodes.closeAuth?.addEventListener('click', () => nodes.authDialog?.close?.());
  nodes.authForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    state.accessToken = String(nodes.accessToken?.value ?? '').trim();
    if (nodes.accessToken) nodes.accessToken.value = '';
    nodes.authDialog?.close?.();
    await loadWorkspaces();
  });
  nodes.disconnect?.addEventListener('click', () => {
    state.accessToken = '';
    stopStream();
    state.workspaces = [];
    state.sessions = [];
    state.sessionView = null;
    setBanner('已断开当前 Operator 凭据。');
    renderShell();
  });

  renderShell();
  loadWorkspaces();
  return Object.freeze({ state, reload: loadWorkspaces, openSession, stop: stopStream });
}

function renderSession(state, nodes, onApprovalDecision) {
  const session = state.sessionView;
  if (nodes.sessionEmpty) nodes.sessionEmpty.hidden = Boolean(session);
  if (nodes.sessionPanel) nodes.sessionPanel.hidden = !session;
  if (!session) {
    renderApprovalPrompt(nodes.approvalPrompt, null);
    renderContextChanged(nodes.contextChanged, false);
    renderEventHistory(nodes.eventHistory, []);
    return;
  }
  setText(nodes.sessionTitle, session.title || `Session ${session.session_id.slice(0, 8)}`);
  setText(nodes.sessionId, session.session_id);
  setText(nodes.sessionBackend, session.backend_kind);
  setText(nodes.sessionStatus, statusLabel(session.status));
  setText(nodes.sessionUpdated, relativeTime(session.updated_at));
  if (nodes.sessionInstructions) nodes.sessionInstructions.value = session.explicit_instructions || '';
  renderContextChanged(nodes.contextChanged, session.context_changed, session.context_changes);
  renderEventHistory(nodes.eventHistory, session.events || []);
  renderApprovalPrompt(nodes.approvalPrompt, session.pending_approval, onApprovalDecision);
  if (nodes.turnInput) nodes.turnInput.disabled = session.status === 'closed';
  if (nodes.sendTurnButton) nodes.sendTurnButton.disabled = session.status === 'closed';
  if (nodes.interruptButton) nodes.interruptButton.disabled = session.status !== 'running';
}

function collectNodes(documentRef) {
  const get = (id) => documentRef.getElementById(id);
  return {
    banner: get('operatorBanner'),
    workspacePicker: get('operatorWorkspace'),
    reloadWorkspaces: get('reloadOperatorWorkspaces'),
    reloadSessions: get('reloadOperatorSessions'),
    sessionList: get('operatorSessionList'),
    newSessionButton: get('newAgentSession'),
    startNewSessionEmpty: get('startNewSessionEmpty'),
    newSessionDialog: get('newSessionDialog'),
    newSessionForm: get('newSessionForm'),
    newSessionWorkspace: get('newSessionWorkspace'),
    newSessionInstructions: get('newSessionInstructions'),
    closeNewSession: get('closeNewSession'),
    sessionEmpty: get('agentEmpty'),
    sessionPanel: get('agentSessionPanel'),
    sessionTitle: get('agentSessionTitle'),
    sessionId: get('agentSessionId'),
    sessionBackend: get('agentSessionBackend'),
    sessionStatus: get('agentSessionStatus'),
    sessionUpdated: get('agentSessionUpdated'),
    sessionInstructions: get('agentSessionInstructions'),
    contextChanged: get('contextChanged'),
    eventHistory: get('eventHistory'),
    approvalPrompt: get('approvalPrompt'),
    turnForm: get('turnForm'),
    turnInput: get('turnInput'),
    sendTurnButton: get('sendTurn'),
    interruptButton: get('interruptTurn'),
    openAuth: get('openOperatorAuth'),
    closeAuth: get('closeOperatorAuth'),
    authDialog: get('operatorAuthDialog'),
    authForm: get('operatorAuthForm'),
    accessToken: get('operatorAccessToken'),
    disconnect: get('disconnectOperator'),
  };
}

function handleApiError(error, setBanner, authDialog) {
  const status = Number(error?.status ?? 0);
  if (status === 401 || status === 403) {
    setBanner('需要有效的 Operator/OAuth 凭据。Admin token 不用于此接口。', 'warning');
    authDialog?.showModal?.();
    return;
  }
  if (status === 404 || status === 501 || status === 503) {
    setBanner('Operator API 当前不可用；工作台保持离线，不会切换到 Admin API。', 'warning');
    return;
  }
  if (!status || /fetch|network|connection/i.test(String(error?.message || ''))) {
    setBanner('无法连接 Operator API；工作台保持离线，不会切换到 Admin API。', 'warning');
    return;
  }
  setBanner(String(error?.message || 'Operator API 请求失败。'), 'danger');
}

function readRouteState() {
  try {
    const params = new URL(globalThis.location?.href || 'http://localhost/app').searchParams;
    return { workspaceId: params.get('workspace') || '', sessionId: params.get('session') || '' };
  } catch {
    return { workspaceId: '', sessionId: '' };
  }
}

function writeRouteState(workspaceId, sessionId) {
  try {
    const url = new URL(globalThis.location.href);
    if (workspaceId) url.searchParams.set('workspace', workspaceId); else url.searchParams.delete('workspace');
    if (sessionId) url.searchParams.set('session', sessionId); else url.searchParams.delete('session');
    globalThis.history?.replaceState?.(null, '', url);
  } catch {
    // Route state is a convenience only; durable state remains server-side.
  }
}

function eventLabel(event) {
  const kind = String(event?.kind ?? 'system');
  if (kind === 'assistant') return 'Assistant';
  if (kind === 'tool') return 'Tool';
  if (kind === 'progress') return 'Progress';
  if (kind === 'approval') return 'Approval';
  if (kind === 'error') return 'Error';
  return event?.method ? String(event.method) : 'System';
}

function eventText(event) {
  const params = event?.params && typeof event.params === 'object' ? event.params : {};
  for (const key of ['text', 'message', 'content', 'reason', 'summary']) {
    if (params[key] != null) return String(params[key]);
  }
  const keys = Object.keys(params);
  return keys.length ? JSON.stringify(params, null, 2) : String(event?.method ?? '');
}

function safeClass(value) {
  return String(value ?? 'system').toLowerCase().replace(/[^a-z0-9_-]/g, '-').slice(0, 32) || 'system';
}

function setText(node, value) {
  if (node) node.textContent = String(value ?? '');
}

function statusLabel(status) {
  return modelModule().sessionStatusPresentation?.(status)?.label || String(status ?? 'unknown');
}

function relativeTime(value) {
  return modelModule().formatRelativeTime?.(value) || String(value ?? '—');
}

if (typeof document !== 'undefined' && document.documentElement?.dataset?.surface === 'operator') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initOperatorApp(document), { once: true });
  else initOperatorApp(document);
}
