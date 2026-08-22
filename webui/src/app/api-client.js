export class OperatorApiError extends Error {
  constructor(status, payload, fallbackMessage = '') {
    super(payload?.error?.message || payload?.message || fallbackMessage || `Operator request failed with HTTP ${status}.`);
    this.name = 'OperatorApiError';
    this.status = status;
    this.payload = payload;
    this.retryable = Boolean(payload?.error?.retryable ?? payload?.retryable);
  }
}

export function createOperatorApiClient({
  getAccessToken = () => '',
  fetchImpl = globalThis.fetch,
} = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');
  let csrfToken = '';

  async function request(path, options = {}) {
    const response = await rawRequest(path, options);
    const payload = await readPayload(response);
    if (!response.ok) throw new OperatorApiError(response.status, payload);
    return payload;
  }

  async function rawRequest(path, options = {}) {
    const normalized = path.startsWith('/') ? path : `/${path}`;
    const { accessToken, ...fetchOptions } = options;
    const headers = new Headers(options.headers || {});
    if (!headers.has('Accept')) headers.set('Accept', 'application/json');
    const token = String(accessToken ?? getAccessToken?.() ?? '').trim();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    const method = String(fetchOptions.method || 'GET').toUpperCase();
    if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      headers.set('X-Operator-CSRF', csrfToken);
    }
    let body = options.body;
    if (body !== undefined && body !== null && typeof body !== 'string') {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(body);
    }
    return fetchImpl(`/api/app${normalized}`, {
      ...fetchOptions,
      headers,
      body,
      cache: 'no-store',
      credentials: 'same-origin',
    });
  }

  async function establishBrowserSession(accessToken) {
    const payload = await request('/session', { method: 'POST', accessToken });
    csrfToken = String(payload?.csrf_token || '');
    return payload;
  }

  async function resumeBrowserSession() {
    const payload = await request('/session');
    if (payload?.authenticated === false) {
      csrfToken = '';
      throw new OperatorApiError(401, { error: { message: 'Operator authentication is required.' } });
    }
    csrfToken = String(payload?.csrf_token || '');
    return payload;
  }

  async function endBrowserSession() {
    try {
      return await request('/session', { method: 'DELETE' });
    } finally {
      csrfToken = '';
    }
  }

  async function streamEvents(sessionId, {
    after = 0,
    signal,
    onEvent = () => {},
  } = {}) {
    const encoded = encodeURIComponent(sessionId);
    const cursor = Number(after) || 0;
    const response = await rawRequest(`/sessions/${encoded}/events?after=${encodeURIComponent(cursor)}`, {
      method: 'GET',
      signal,
      headers: { Accept: 'text/event-stream' },
    });
    if (!response.ok) {
      const payload = await readPayload(response);
      throw new OperatorApiError(response.status, payload);
    }
    if (!response.body?.getReader) {
      throw new OperatorApiError(503, { error: { message: 'Streaming response body is unavailable.' } });
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let rest = '';
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const parsed = parseSseChunk(rest, decoder.decode(value, { stream: true }));
        rest = parsed.rest;
        for (const event of parsed.events) onEvent(event);
      }
      const tail = parseSseChunk(rest, decoder.decode());
      for (const event of tail.events) onEvent(event);
    } finally {
      reader.releaseLock?.();
    }
  }

  return Object.freeze({
    request,
    establishBrowserSession,
    resumeBrowserSession,
    endBrowserSession,
    listWorkspaces: () => request('/workspaces'),
    listSessions: (workspaceId = '') => request(`/sessions${workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ''}`),
    getSession: (sessionId) => request(`/sessions/${encodeURIComponent(sessionId)}`),
    createSession: (payload) => request('/sessions', { method: 'POST', body: payload }),
    sendTurn: (sessionId, message) => request(`/sessions/${encodeURIComponent(sessionId)}/turns`, {
      method: 'POST',
      body: { message: String(message ?? '') },
    }),
    interrupt: (sessionId) => request(`/sessions/${encodeURIComponent(sessionId)}/interrupt`, { method: 'POST' }),
    decideApproval: (sessionId, approvalId, decision) => request(
      `/sessions/${encodeURIComponent(sessionId)}/approvals/${encodeURIComponent(approvalId)}`,
      { method: 'POST', body: { decision } },
    ),
    streamEvents,
  });
}

export function parseSseChunk(previous = '', chunk = '') {
  const combined = `${previous}${chunk}`.replace(/\r\n/g, '\n');
  const frames = combined.split('\n\n');
  const rest = frames.pop() ?? '';
  const events = [];
  for (const frame of frames) {
    if (!frame.trim()) continue;
    const dataLines = [];
    let eventId = null;
    for (const line of frame.split('\n')) {
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
      else if (line.startsWith('id:')) eventId = line.slice(3).trim();
    }
    if (!dataLines.length) continue;
    try {
      const payload = JSON.parse(dataLines.join('\n'));
      if (payload && typeof payload === 'object' && payload.sequence == null && eventId != null) {
        const numeric = Number(eventId);
        payload.sequence = Number.isFinite(numeric) ? numeric : eventId;
      }
      events.push(payload);
    } catch {
      // Ignore malformed frames; the server owns retry/error semantics and a later valid frame can continue.
    }
  }
  return { events, rest };
}

async function readPayload(response) {
  if (response.status === 204) return {};
  const contentType = response.headers?.get?.('content-type') || '';
  if (contentType.includes('application/json')) return response.json();
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { message: text };
  }
}

globalThis.OperatorAppApi = Object.freeze({
  OperatorApiError,
  createOperatorApiClient,
  parseSseChunk,
});
