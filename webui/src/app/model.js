const ACTIVE_WORKSPACE_STATES = new Set(['online', 'ready', 'local', 'connected', 'unknown', '']);
const FAILURE_SESSION_STATES = new Set(['failed', 'error', 'unavailable', 'backend_unavailable']);

export function projectWorkspaces(rawItems = []) {
  return rawItems
    .map((raw) => {
      const id = String(raw?.id ?? raw?.workspace_id ?? '').trim();
      if (!id) return null;
      const explicitlyDenied = raw?.authorized === false || raw?.can_access === false;
      const enabled = raw?.enabled !== false;
      const runnerStatus = String(raw?.runner_status ?? raw?.runnerStatus ?? 'unknown').toLowerCase();
      return {
        id,
        name: String(raw?.name ?? raw?.display_name ?? id),
        available: enabled && !explicitlyDenied && ACTIVE_WORKSPACE_STATES.has(runnerStatus),
        runner_status: runnerStatus || 'unknown',
      };
    })
    .filter((workspace) => workspace && workspace.available)
    .sort((a, b) => a.name.localeCompare(b.name));
}

export function sortSessions(rawItems = []) {
  return rawItems
    .map(projectSessionSummary)
    .filter(Boolean)
    .sort((a, b) => timestampValue(b.updated_at ?? b.created_at) - timestampValue(a.updated_at ?? a.created_at));
}

export function normalizeSessionDetail(raw = {}) {
  const summary = projectSessionSummary(raw);
  if (!summary) return null;
  const fingerprint = raw?.repo_fingerprint && typeof raw.repo_fingerprint === 'object'
    ? raw.repo_fingerprint
    : {};
  const changes = Array.isArray(fingerprint.changes)
    ? fingerprint.changes.map((value) => String(value)).slice(0, 50)
    : Array.isArray(raw?.context_changes)
      ? raw.context_changes.map((value) => String(value)).slice(0, 50)
      : [];
  return {
    ...summary,
    explicit_instructions: String(raw?.explicit_instructions ?? raw?.instructions ?? ''),
    conversation_id: raw?.conversation_id == null ? null : String(raw.conversation_id),
    last_turn_id: raw?.last_turn_id == null ? null : String(raw.last_turn_id),
    context_changed: Boolean(raw?.context_changed ?? fingerprint.context_changed),
    context_changes: changes,
  };
}

export function createSessionPayload({ workspaceId, instructions = '', backendKind = 'codex' } = {}) {
  const workspace = String(workspaceId ?? '').trim();
  if (!workspace) throw new Error('workspace_id is required');
  const payload = {
    workspace_id: workspace,
    backend_kind: String(backendKind || 'codex'),
  };
  const explicit = String(instructions ?? '').trim();
  if (explicit) payload.instructions = explicit;
  return payload;
}

export function createSessionViewState(raw = {}) {
  const detail = normalizeSessionDetail(raw) || {
    session_id: '',
    workspace_id: '',
    backend_kind: 'unknown',
    status: 'unknown',
    created_at: null,
    updated_at: null,
    explicit_instructions: '',
    context_changed: false,
    context_changes: [],
  };
  return {
    ...detail,
    events: [],
    event_cursor: 0,
    pending_approval: null,
  };
}

export function reduceAgentEvent(current, rawEvent, maxEvents = 200) {
  const state = current ? { ...current } : createSessionViewState();
  const event = projectEvent(rawEvent);
  if (!event) return state;
  const limit = Math.max(1, Math.min(Number(maxEvents) || 200, 500));
  const nextEvents = [...(Array.isArray(state.events) ? state.events : []), event];
  state.events = nextEvents.slice(-limit);
  state.event_cursor = Math.max(Number(state.event_cursor) || 0, Number(event.sequence) || 0);

  if (event.kind === 'approval' || event.method === 'approval/requested') {
    state.pending_approval = event;
    state.status = 'waiting_approval';
  }
  if (event.method === 'approval/resolved') {
    const resolvedId = event.params?.approval_id ?? event.approval_id;
    if (!state.pending_approval || !resolvedId || state.pending_approval.approval_id === resolvedId) {
      state.pending_approval = null;
      if (state.status === 'waiting_approval') state.status = 'ready';
    }
  }
  if (event.method === 'session/context_changed') {
    state.context_changed = true;
    state.context_changes = Array.isArray(event.params?.changes)
      ? event.params.changes.map((value) => String(value)).slice(0, 50)
      : [];
  }
  if (event.method === 'session/status' && event.params?.status) {
    state.status = String(event.params.status);
  }
  if (event.kind === 'error' || event.method === 'turn/failed') state.status = 'failed';
  return state;
}

export function sessionStatusPresentation(status) {
  const normalized = String(status ?? 'unknown').toLowerCase();
  if (normalized === 'waiting_approval' || normalized === 'waiting_for_approval') {
    return { tone: 'warning', label: '等待审批' };
  }
  if (normalized === 'recovering') return { tone: 'warning', label: '恢复中' };
  if (FAILURE_SESSION_STATES.has(normalized)) {
    return { tone: 'danger', label: normalized.includes('unavailable') ? '后端不可用' : '失败' };
  }
  if (normalized === 'closed' || normalized === 'completed') return { tone: 'muted', label: '已结束' };
  if (['creating', 'ready', 'running', 'active', 'waiting_for_input'].includes(normalized)) {
    return { tone: 'good', label: normalized === 'creating' ? '创建中' : '活动中' };
  }
  return { tone: 'muted', label: normalized === 'unknown' ? '未知' : normalized };
}

export function formatRelativeTime(value, now = Date.now()) {
  const timestamp = timestampValue(value);
  if (!timestamp) return '—';
  const deltaSeconds = Math.round((timestamp - now) / 1000);
  const absolute = Math.abs(deltaSeconds);
  if (absolute < 60) return deltaSeconds <= 0 ? '刚刚' : '即将';
  const minutes = Math.round(deltaSeconds / 60);
  if (Math.abs(minutes) < 60) return `${Math.abs(minutes)} 分钟${minutes <= 0 ? '前' : '后'}`;
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return `${Math.abs(hours)} 小时${hours <= 0 ? '前' : '后'}`;
  const days = Math.round(hours / 24);
  if (Math.abs(days) < 7) return `${Math.abs(days)} 天${days <= 0 ? '前' : '后'}`;
  return new Date(timestamp).toLocaleString();
}

function projectSessionSummary(raw = {}) {
  const sessionId = String(raw?.session_id ?? raw?.id ?? '').trim();
  if (!sessionId) return null;
  return {
    session_id: sessionId,
    workspace_id: String(raw?.workspace_id ?? raw?.workspaceId ?? ''),
    backend_kind: String(raw?.backend_kind ?? raw?.backend ?? 'unknown'),
    status: String(raw?.status ?? 'unknown'),
    title: String(raw?.title ?? raw?.summary ?? `Session ${sessionId.slice(0, 8)}`),
    conversation_id: raw?.conversation_id == null ? null : String(raw.conversation_id),
    last_turn_id: raw?.last_turn_id == null ? null : String(raw.last_turn_id),
    created_at: raw?.created_at ?? raw?.createdAt ?? null,
    updated_at: raw?.updated_at ?? raw?.updatedAt ?? null,
  };
}

function projectEvent(raw = {}) {
  if (!raw || typeof raw !== 'object') return null;
  const sequence = Number(raw.sequence ?? raw.cursor ?? 0);
  return {
    sequence: Number.isFinite(sequence) ? sequence : 0,
    kind: String(raw.kind ?? 'system'),
    method: String(raw.method ?? ''),
    params: raw.params && typeof raw.params === 'object' ? raw.params : {},
    approval_id: raw.approval_id == null ? null : String(raw.approval_id),
    retryable: Boolean(raw.retryable),
  };
}

function timestampValue(value) {
  if (value == null || value === '') return 0;
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 1e12 ? value : value > 1e9 ? value * 1000 : value;
  }
  const numeric = Number(value);
  if (Number.isFinite(numeric) && String(value).trim() !== '') {
    return numeric > 1e12 ? numeric : numeric > 1e9 ? numeric * 1000 : numeric;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

globalThis.OperatorAppModel = Object.freeze({
  projectWorkspaces,
  sortSessions,
  normalizeSessionDetail,
  createSessionPayload,
  createSessionViewState,
  reduceAgentEvent,
  sessionStatusPresentation,
  formatRelativeTime,
});
