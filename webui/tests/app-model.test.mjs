import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createSessionViewState,
  normalizeSessionDetail,
  projectWorkspaces,
  reduceAgentEvent,
  sortSessions,
} from '../src/app/model.js';
import {
  createOperatorApiClient,
  parseSseChunk,
} from '../src/app/api-client.js';

test('workspace projection excludes entries explicitly denied to the principal', () => {
  const workspaces = projectWorkspaces([
    { id: 'allowed', name: 'Allowed', authorized: true, enabled: true },
    { id: 'implicit', name: 'Implicit API-filtered workspace', enabled: true },
    { id: 'denied', name: 'Denied', authorized: false, enabled: true },
    { id: 'denied-legacy', name: 'Denied legacy', can_access: false, enabled: true },
  ]);
  assert.deepEqual(workspaces.map((workspace) => workspace.id), ['allowed', 'implicit']);
});

test('session summaries sort newest first and do not expose backend thread identifiers', () => {
  const sessions = sortSessions([
    {
      session_id: 'older', workspace_id: 'ws-a', status: 'ready', backend_kind: 'codex',
      backend_thread_id: 'secret-thread-a', updated_at: 10,
    },
    {
      session_id: 'newer', workspace_id: 'ws-a', status: 'running', backend_kind: 'codex',
      backend_thread_id: 'secret-thread-b', updated_at: 20,
    },
  ]);
  assert.deepEqual(sessions.map((session) => session.session_id), ['newer', 'older']);
  assert.equal('backend_thread_id' in sessions[0], false);
  assert.equal('owner_principal_id' in sessions[0], false);
});

test('session detail restores explicit instructions and reports repository context drift', () => {
  const detail = normalizeSessionDetail({
    session_id: 'session-a',
    workspace_id: 'ws-a',
    backend_kind: 'codex',
    status: 'ready',
    explicit_instructions: 'Prefer focused tests.',
    repo_fingerprint: {
      context_changed: true,
      changes: ['HEAD changed', 'AGENTS.md digest changed'],
    },
  });
  assert.equal(detail.explicit_instructions, 'Prefer focused tests.');
  assert.equal(detail.context_changed, true);
  assert.deepEqual(detail.context_changes, ['HEAD changed', 'AGENTS.md digest changed']);
});

test('agent event reducer keeps bounded history and projects approval/context state', () => {
  let state = createSessionViewState({ session_id: 'session-a', status: 'running' });
  state = reduceAgentEvent(state, {
    sequence: 1, kind: 'assistant', method: 'turn/assistant', params: { text: 'first' },
  }, 3);
  state = reduceAgentEvent(state, {
    sequence: 2, kind: 'approval', method: 'approval/requested', params: { reason: 'write file' }, approval_id: 'approval-1',
  }, 3);
  assert.equal(state.pending_approval.approval_id, 'approval-1');
  assert.equal(state.status, 'waiting_approval');

  state = reduceAgentEvent(state, {
    sequence: 3, kind: 'system', method: 'session/context_changed', params: { changes: ['HEAD changed'] },
  }, 3);
  assert.equal(state.context_changed, true);
  assert.deepEqual(state.context_changes, ['HEAD changed']);

  state = reduceAgentEvent(state, {
    sequence: 4, kind: 'system', method: 'approval/resolved', params: { approval_id: 'approval-1' },
  }, 3);
  assert.equal(state.pending_approval, null);
  assert.equal(state.events.length, 3);
  assert.deepEqual(state.events.map((event) => event.sequence), [2, 3, 4]);
  assert.equal(state.event_cursor, 4);
});

test('operator API client uses the operator namespace and keeps bearer material out of URLs/storage', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      async json() { return { sessions: [] }; },
    };
  };
  const client = createOperatorApiClient({
    getAccessToken: () => 'memory-only-bearer',
    fetchImpl,
  });
  await client.listSessions('workspace/a');
  await client.createSession({
    workspace_id: 'workspace/a',
    backend_kind: 'codex',
    instructions: 'Keep changes scoped.',
  });

  assert.equal(calls[0].url, '/api/app/sessions?workspace_id=workspace%2Fa');
  assert.equal(calls[0].url.includes('memory-only-bearer'), false);
  assert.equal(calls[0].options.headers.get('Authorization'), 'Bearer memory-only-bearer');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.cache, 'no-store');
  assert.equal(calls[1].url, '/api/app/sessions');
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    workspace_id: 'workspace/a',
    backend_kind: 'codex',
    instructions: 'Keep changes scoped.',
  });
});

test('SSE parser preserves incomplete frames and parses bounded JSON events', () => {
  const first = parseSseChunk('', 'id: 7\ndata: {"sequence":7,"kind":"assistant"}\n\nid: 8\ndata: {"sequence":');
  assert.equal(first.events.length, 1);
  assert.equal(first.events[0].sequence, 7);
  assert.match(first.rest, /id: 8/);

  const second = parseSseChunk(first.rest, '8,"kind":"progress"}\n\n');
  assert.equal(second.events.length, 1);
  assert.equal(second.events[0].sequence, 8);
  assert.equal(second.rest, '');
});
