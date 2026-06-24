const $ = (id) => document.getElementById(id);
    const state = {
      token: localStorage.getItem('mcpAdminToken') || '',
      status: null,
      active: 'overview',
      templates: {},
      selectedConversation: '',
      activeChatTrack: 'backup',
      chatMessages: [],
      chatContextEntries: [],
    };
    const toolTemplate = { name: 'mcp_catalog_list', arguments: {} };
    const defaultServer = { alias:'filesystem', transport:'stdio', command:'uvx', args:['mcp-server-filesystem','G:/LLM'], env:{TOKEN:{secret_ref:'github_token'}}, include_tools:['read_file'] };
    const templates = {
      filesystem: defaultServer,
      browser: { alias:'browser', transport:'stdio', command:'npx', args:['-y','@browsermcp/mcp@latest'], enabled:false },
      github: { alias:'github', transport:'stdio', command:'npx', args:['-y','@modelcontextprotocol/server-github'], env:{GITHUB_PERSONAL_ACCESS_TOKEN:{secret_ref:'github_token'}}, enabled:false },
      local: { alias:'local-command', transport:'stdio', command:'uvx', args:['your-mcp-package'], enabled:false },
      http: { alias:'remote-http', transport:'http', url:'http://127.0.0.1:3000/mcp', headers:{Authorization:'Bearer ${TOKEN}'}, enabled:false },
    };
    const esc = (v) => String(v ?? '').replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
    const jsArg = (v) => String(v ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    const setText = (id, value) => { const el = $(id); if (el) el.textContent = value ?? ''; };
    const setHtml = (id, value) => { const el = $(id); if (el) el.innerHTML = value ?? ''; };
    const setValue = (id, value) => { const el = $(id); if (el) el.value = value ?? ''; };
    const headers = () => ({ 'Content-Type':'application/json', ...(state.token ? {'Authorization':'Bearer '+state.token} : {}) });

    function out(value) {
      const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      setText('output', text);
      setText('outputMirror', text);
      setText('resultSummary', text.length > 120 ? text.slice(0, 120) + '...' : text);
      $('outputPanel')?.classList.remove('hidden');
      return text;
    }

    function setView(name) {
      state.active = name;
      document.querySelectorAll('[data-view]').forEach((el) => el.classList.toggle('active', el.dataset.view === name));
      document.querySelectorAll('[data-nav]').forEach((el) => el.classList.toggle('active', el.dataset.nav === name));
      $('adminApp')?.classList.remove('nav-open');
    }

    function setChatTrack(name) {
      state.activeChatTrack = name;
      document.querySelectorAll('[data-chat-track]').forEach((el) => el.classList.toggle('active', el.dataset.chatTrack === name));
      document.querySelectorAll('[data-chat-pane]').forEach((el) => el.classList.toggle('active', el.dataset.chatPane === name));
    }

    function setAuthGate(kind, message) {
      const gate = $('authGate');
      if (gate) gate.classList.toggle('needs-auth', kind !== 'ok');
      const pill = $('authState');
      if (pill) {
        pill.textContent = kind === 'ok' ? '已连接' : (kind === 'auth' ? '需要认证' : '连接异常');
        pill.className = `status-pill ${kind === 'ok' ? 'ok' : kind === 'auth' ? 'warn' : 'bad'}`;
      }
      setText('homeAuthHint', message || (kind === 'ok' ? '管理接口可用。' : '输入管理 token 或使用 OAuth 登录后刷新。'));
    }

    async function api(path, body) {
      const res = await fetch(path, { method:'POST', headers:headers(), body:JSON.stringify(body || {}) });
      const data = await res.json().catch(() => ({ ok:false, error:'响应不是 JSON' }));
      if (!res.ok) throw { ...data, status: res.status };
      return data;
    }

    async function callTool(name, args = {}) {
      const data = await api('/api/admin/tool', { name, arguments: args });
      return data.structuredContent || data;
    }

    function parseList(value) {
      return String(value || '').split(/[\n,]+/).map((item) => item.trim()).filter(Boolean);
    }

    function parsePairs(value, { allowSecret = false } = {}) {
      const result = {};
      for (const line of String(value || '').split(/\n+/)) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        const idx = trimmed.indexOf('=');
        if (idx <= 0) throw new Error(`无法解析 ${trimmed}，请使用 KEY=value`);
        const key = trimmed.slice(0, idx).trim();
        const raw = trimmed.slice(idx + 1).trim();
        result[key] = allowSecret && raw.startsWith('secret:') ? { secret_ref: raw.slice(7).trim() } : raw;
      }
      return result;
    }

    function pairsText(value) {
      return Object.entries(value || {}).map(([key, raw]) => {
        const shown = raw && typeof raw === 'object' && raw.secret_ref ? `secret:${raw.secret_ref}` : raw;
        return `${key}=${shown ?? ''}`;
      }).join('\n');
    }

    function updateTransportFields() {
      const stdio = $('wizardTransport').value === 'stdio';
      document.querySelectorAll('[data-stdio]').forEach((el) => el.style.display = stdio ? '' : 'none');
      document.querySelectorAll('[data-http]').forEach((el) => el.style.display = stdio ? 'none' : '');
    }

    function wizardConfig() {
      const transport = $('wizardTransport').value;
      const config = { alias:$('wizardAlias').value.trim(), transport, enabled:$('wizardEnabled').checked };
      const timeout = Number($('wizardTimeout').value || 0);
      if (timeout > 0) config.timeout_ms = timeout;
      if (transport === 'stdio') {
        config.command = $('wizardCommand').value.trim();
        const args = parseList($('wizardArgs').value);
        const env = parsePairs($('wizardEnv').value, { allowSecret:true });
        if (args.length) config.args = args;
        if (Object.keys(env).length) config.env = env;
      } else {
        config.url = $('wizardUrl').value.trim();
        const headers = parsePairs($('wizardHeaders').value);
        if (Object.keys(headers).length) config.headers = headers;
      }
      const includeTools = parseList($('wizardInclude').value);
      const excludeTools = parseList($('wizardExclude').value);
      if (includeTools.length) config.include_tools = includeTools;
      if (excludeTools.length) config.exclude_tools = excludeTools;
      return config;
    }

    function populateWizard(config) {
      setValue('wizardAlias', config.alias || '');
      setValue('wizardTransport', config.transport === 'streamable_http' ? 'http' : (config.transport || 'stdio'));
      setValue('wizardCommand', config.command || '');
      setValue('wizardUrl', config.url || '');
      setValue('wizardArgs', (config.args || []).join('\n'));
      setValue('wizardEnv', pairsText(config.env));
      setValue('wizardHeaders', pairsText(config.headers));
      setValue('wizardInclude', (config.include_tools || []).join('\n'));
      setValue('wizardExclude', (config.exclude_tools || []).join('\n'));
      setValue('wizardTimeout', config.timeout_ms || '');
      $('wizardEnabled').checked = config.enabled !== false;
      updateTransportFields();
    }

    function syncJsonFromWizard() {
      try { setValue('serverConfig', JSON.stringify(wizardConfig(), null, 2)); }
      catch (err) { out({ok:false, error:`向导配置错误：${err.message}`}); }
    }

    function syncWizardFromJson() {
      try { populateWizard(JSON.parse($('serverConfig').value)); }
      catch (err) { out({ok:false, error:`JSON 格式错误：${err.message}`}); }
    }

    function parseServerConfig() {
      try { return JSON.parse($('serverConfig').value); }
      catch (err) { out({ok:false, error:`配置 JSON 格式错误：${err.message}`}); throw err; }
    }

    function randomHex(bytes) {
      return Array.from(crypto.getRandomValues(new Uint8Array(bytes))).map((b) => b.toString(16).padStart(2, '0')).join('');
    }

    function startupSettingsPayload() {
      const settings = {
        workspace:$('settingsWorkspace').value,
        host:$('settingsHost').value,
        port:$('settingsPort').value,
        oauth_server_url:$('settingsOAuthServerUrl').value,
        permission_mode:$('settingsPermission').value,
        shell_env_inherit:$('settingsShellEnv').value,
      };
      const tokenSecret = $('settingsOAuthTokenSecret').value.trim();
      if (tokenSecret) settings.oauth_token_secret = tokenSecret;
      return settings;
    }

    async function refreshStatus() {
      try {
        const res = await fetch('/api/admin/status', { headers: state.token ? {'Authorization':'Bearer '+state.token} : {} });
        const data = await res.json().catch(() => ({ ok:false, error:`HTTP ${res.status}` }));
        if (!res.ok) throw { ...data, status: res.status };
        state.status = data;
        setAuthGate('ok', '管理接口已连接，首页 token 会保存在本机浏览器。');
        renderStatus(data);
      } catch (err) {
        const status = err && typeof err === 'object' ? err.status : undefined;
        setAuthGate(status === 401 || status === 403 ? 'auth' : 'bad', status === 401 || status === 403 ? '需要管理 token 或 OAuth 登录。' : '无法读取管理状态，请检查服务是否运行。');
        out(err);
      }
    }

    function renderStatus(data) {
      const conversations = data.chat_conversations?.conversations || [];
      const contextCount = conversations.reduce((sum, item) => sum + Number(item.context_entry_count || 0), 0);
      setText('metricServers', data.catalog?.server_count ?? 0);
      setText('metricTools', data.tool_counts?.total ?? 0);
      setText('metricSessions', data.http_sessions?.length ?? data.runtime?.http_session_count ?? 0);
      setText('metricCalls', data.recent_tool_calls?.length ?? 0);
      setText('metricChats', conversations.length);
      setText('metricContext', contextCount);
      setHtml('endpointInfo', '<code>/mcp</code> <code>/admin</code> <code>/oauth/authorize</code>');
      setText('pathInfo', JSON.stringify(data.config_paths || {}, null, 2));
      setText('serverInfo', JSON.stringify(data.server || {}, null, 2));
      renderServers(data.catalog?.servers || []);
      renderTemplates(data.templates?.templates || []);
      renderSessions(data.http_sessions || [], data.exec_sessions || []);
      renderChatConversations(conversations);
      renderMcpRequests(data.recent_mcp_requests || []);
      renderCalls(data.recent_tool_calls || []);
      setValue('defaultCwd', data.runtime?.default_cwd_display || '.');
      renderSessionOverview(data, contextCount);
      const startup = data.startup_settings || {};
      setValue('settingsWorkspace', data.runtime?.workspace || '');
      setValue('settingsHost', data.server?.host || '');
      setValue('settingsPort', data.server?.port || '');
      setValue('settingsOAuthServerUrl', startup.oauth_server_url || '');
      const tokenSecret = $('settingsOAuthTokenSecret');
      if (tokenSecret) tokenSecret.placeholder = startup.oauth_token_secret_configured ? '已保存，留空不变' : '留空时首次 OAuth 启动会自动生成';
      setValue('settingsPermission', data.runtime?.permission_mode || 'safe');
      setValue('settingsShellEnv', data.runtime?.shell_env_inherit || 'core');
      setText('resultSummary', JSON.stringify({ ok:true, summary:'status refreshed', runtime:data.runtime, tool_counts:data.tool_counts }, null, 2));
    }

    function renderSessionOverview(data, contextCount) {
      const httpSessions = data.http_sessions || [];
      const execSessions = data.exec_sessions || [];
      const conversations = data.chat_conversations?.conversations || [];
      setText('sessionWorkspace', data.runtime?.workspace || '');
      setText('sessionDefaultCwd', data.runtime?.default_cwd_display || '.');
      setText('sessionHttpCount', httpSessions.length);
      setText('sessionExecCount', execSessions.length);
      setText('sessionChatCount', conversations.length);
      setText('sessionChatContextCount', contextCount);
      setText('httpSessionBadge', `${httpSessions.length} 个`);
      setText('execSessionBadge', `${execSessions.length} 个`);
      setText('chatSessionBadge', `${conversations.length} 个`);
      setText('chatContextBadge', `${contextCount} 条`);
    }

    function renderTemplates(items) {
      if (!Array.isArray(items) || !items.length) return;
      const current = $('templateSelect').value;
      state.templates = Object.fromEntries(items.map((item) => [item.id, item.config || item.config_redacted]));
      setHtml('templateSelect', items.map((item) => `<option value="${esc(item.id)}">${esc(item.title || item.id)}</option>`).join(''));
      if (state.templates[current]) $('templateSelect').value = current;
    }

    function selectedTemplateConfig() {
      return state.templates[$('templateSelect').value] || templates[$('templateSelect').value] || defaultServer;
    }

    async function refreshTemplatesFromTool() {
      const data = await callTool('mcp_template_list', {});
      renderTemplates(data.templates || []);
      out(data);
    }

    function renderServers(servers) {
      setHtml('servers', servers.map((s) => {
        const alias = String(s.alias || '');
        const enabled = s.config?.enabled !== false;
        const status = s.status ? (s.status.error ? '错误' : (s.status.initialized ? '已连接' : '未初始化')) : '未运行';
        return `<tr><td><code>${esc(alias)}</code></td><td>${esc(s.config?.transport || '')}</td><td>${enabled ? '启用' : '停用'}</td><td>${esc(status)}</td><td class="toolbar"><button class="secondary" onclick="checkServer('${esc(jsArg(alias))}')">健康</button><button class="secondary" onclick="serverLogs('${esc(jsArg(alias))}')">日志</button><button class="secondary" onclick="restartServer('${esc(jsArg(alias))}')">重启</button><button class="secondary" onclick="toggleServer('${esc(jsArg(alias))}', ${!enabled})">${enabled ? '停用' : '启用'}</button><button class="danger" onclick="removeServer('${esc(jsArg(alias))}')">删除</button></td></tr>`;
      }).join('') || '<tr><td colspan="5" class="muted">还没有配置 MCP。</td></tr>');
    }

    function renderSessions(httpSessions, execSessions) {
      setHtml('httpSessions', httpSessions.map((s) => `<tr><td><code>${esc(s.session_id)}</code><div class="muted">请求 ${esc(s.request_count || 0)} 次</div></td><td><span class="mini-label">最近 RPC</span><code>${esc(s.last_rpc_method || s.last_method || '')}</code><div class="muted">${esc(s.last_seen || '')}</div></td><td class="path-cell"><span class="mini-label">会话默认目录</span><code>${esc(s.default_cwd_display || s.default_cwd || '')}</code></td><td class="path-cell"><span class="mini-label">工作区</span><code>${esc(s.workspace || '')}</code></td><td><span class="mini-label">来源</span>${esc(s.remote_addr || '')}<div class="muted">${esc(s.user_agent || '')}</div></td><td><button class="secondary" onclick="exportTranscript('${esc(jsArg(s.session_id))}')">导出 MD</button></td></tr>`).join('') || '<tr><td colspan="6" class="muted">暂无 MCP HTTP 会话。访问 /mcp 后会显示在这里。</td></tr>');
      setHtml('execSessions', execSessions.map((s) => `<tr><td><code>${esc(s.session_id)}</code><div class="muted">${esc(s.started_at || '')}</div></td><td>${esc(s.status)}</td><td class="path-cell"><span class="mini-label">命令执行目录</span><code>${esc(s.workdir)}</code></td><td><span class="mini-label">命令</span>${esc(s.command)}</td><td><button class="danger" onclick="terminateSession('${esc(jsArg(s.session_id))}')">终止</button></td></tr>`).join('') || '<tr><td colspan="5" class="muted">暂无运行命令会话。</td></tr>');
    }

    function renderChatConversations(conversations) {
      let currentDate = '';
      const rows = [];
      for (const c of conversations) {
        const id = String(c.conversation_id || '');
        const date = c.date || String(c.last_seen || '').slice(0, 10) || '未分组';
        if (date !== currentDate) {
          currentDate = date;
          rows.push(`<tr class="group-row"><td colspan="8">${esc(date)}</td></tr>`);
        }
        const label = c.title ? `${c.title} / ${id}` : id;
        rows.push(`<tr><td><label class="checkline"><input type="checkbox" class="chat-merge-select" value="${esc(id)}"><span><strong>${esc(c.title || '未命名会话')}</strong><br><code>${esc(id)}</code>${c.unique_id ? `<div class="muted">UID: ${esc(c.unique_id)}</div>` : ''}</span></label></td><td>${esc(c.message_count || 0)}</td><td>${esc(c.context_entry_count || 0)}</td><td>${esc(c.first_seen || '')}</td><td>${esc(c.last_seen || '')}</td><td>${esc(c.source || '')}</td><td><input class="merge-target" value="${esc(id)}" aria-label="合并目标 ${esc(label)}" oninput="document.getElementById('chatMergeTarget').value=this.value"></td><td class="toolbar"><button class="secondary" onclick="readChatConversation('${esc(jsArg(id))}')">读取</button><button class="secondary" onclick="exportChatTranscript('${esc(jsArg(id))}')">聊天 MD</button><button class="secondary" onclick="exportChatContext('${esc(jsArg(id))}')">上下文 MD</button><button class="danger" onclick="deleteChatConversation('${esc(jsArg(id))}')">删除</button></td></tr>`);
      }
      setHtml('chatConversations', rows.join('') || '<tr><td colspan="8" class="muted">暂无聊天记录。agent 调用 record_chat_transcript 或 record_chat_message 后会显示在这里。</td></tr>');
    }

    function renderChatMessages(data) {
      const messages = data.messages || [];
      state.chatMessages = messages;
      state.selectedConversation = data.conversation_id || state.selectedConversation || '';
      setText('chatSelectedConversation', state.selectedConversation || '未选择');
      setText('backupCount', `${messages.length} 条`);
      setHtml('chatMessages', messages.map((m) => {
        const id = Number(m.id);
        return `<div class="chat-message"><div class="chat-message-head"><strong>#${esc(id)} ${esc(m.role || '')}</strong><span class="muted">${esc(m.timestamp || '')}</span><button class="danger" onclick="deleteChatMessage(${id})">删除</button></div><div class="form-grid"><div><label>角色</label><input id="chatRole-${id}" value="${esc(m.role || '')}"></div><div><label>时间</label><input id="chatTime-${id}" value="${esc(m.timestamp || '')}"></div><div class="full"><label>来源</label><input id="chatSource-${id}" value="${esc(m.source || '')}"></div><div class="full"><label>聊天文本</label><textarea class="chat-content" id="chatContent-${id}">${esc(m.content || '')}</textarea></div></div><div class="toolbar"><button onclick="saveChatMessage(${id})">保存修改</button></div></div>`;
      }).join('') || '<div class="muted">选择一个聊天会话后，这里会显示可编辑的完整消息文本。</div>');
    }

    function renderChatContext(data) {
      const entries = data.entries || [];
      state.chatContextEntries = entries;
      state.selectedConversation = data.conversation_id || state.selectedConversation || '';
      setText('chatSelectedConversation', state.selectedConversation || '未选择');
      setText('contextCount', `${entries.length} 条`);
      setHtml('chatContextEntries', entries.map((entry) => `<div class="context-entry"><div class="context-entry-head"><strong>#${esc(entry.id)} ${esc(entry.kind || 'context')}</strong><span class="muted">${esc(entry.timestamp || '')}</span></div><div class="muted">来源：${esc(entry.source || '')}${entry.entry_id ? ` / entry_id: ${esc(entry.entry_id)}` : ''}</div><pre>${esc(entry.content || '')}</pre></div>`).join('') || '<div class="muted">这个会话还没有恢复上下文条目。agent 调用 record-context 或兼容入口后会显示在这里。</div>');
    }

    function renderMcpRequests(requests) {
      setHtml('mcpRequests', requests.slice().reverse().map((r) => `<tr><td>${esc(r.timestamp)}</td><td><code>${esc(r.session_id)}</code></td><td><code>${esc(r.method)} ${esc(r.path)}</code></td><td><code>${esc(r.rpc_method || '')}</code></td><td>${esc(r.status)}</td><td><code>${esc(r.default_cwd_display || '')}</code></td></tr>`).join('') || '<tr><td colspan="6" class="muted">暂无 /mcp 访问记录。</td></tr>');
    }

    function renderCalls(calls) {
      setHtml('calls', calls.slice().reverse().map((c) => `<tr><td>${esc(c.timestamp)}</td><td><code>${esc(c.tool)}</code></td><td>${c.ok ? '成功' : '失败'}</td><td>${esc(c.duration_ms)} ms</td><td>${esc(c.error_code || '')}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">暂无调用记录。</td></tr>');
    }

    window.toggleServer = async (alias, enabled) => { out(await callTool(enabled ? 'mcp_server_enable' : 'mcp_server_disable', { alias, apply:true })); await refreshStatus(); };
    window.checkServer = async (alias) => { out(await callTool('mcp_server_health', { alias })); };
    window.serverLogs = async (alias) => { out(await callTool('mcp_server_logs', { alias, max_lines:80 })); };
    window.restartServer = async (alias) => { await callTool('mcp_server_stop', { alias }); out(await callTool('mcp_server_start', { alias })); await refreshStatus(); };
    window.removeServer = async (alias) => { if (!confirm('确认删除这个 MCP 配置？')) return; out(await callTool('mcp_server_remove', { alias, apply:true })); await refreshStatus(); };
    window.terminateSession = async (sessionId) => { if (!confirm('确认终止这个会话？')) return; out(await api('/api/admin/runtime', { terminate_session: sessionId })); await refreshStatus(); };
    window.exportTranscript = async (sessionId) => exportTranscriptPayload(sessionId);
    window.readChatConversation = async (conversationId) => {
      state.selectedConversation = conversationId;
      const [messages, context] = await Promise.all([
        callTool('mcp_chat_messages', { conversation_id: conversationId, limit:5000 }),
        callTool('mcp_chat_context', { conversation_id: conversationId, limit:5000 }),
      ]);
      renderChatMessages(messages);
      renderChatContext(context);
      out({ ok:true, conversation_id:conversationId, chat_backup_messages:messages.message_count || 0, restore_context_entries:context.entry_count || 0 });
    };
    window.exportChatTranscript = async (conversationId) => exportChatPayload(conversationId);
    window.exportChatContext = async (conversationId) => exportChatContextPayload(conversationId);
    window.saveChatMessage = async (id) => {
      const payload = { id, role:$(`chatRole-${id}`).value, timestamp:$(`chatTime-${id}`).value, source:$(`chatSource-${id}`).value, content:$(`chatContent-${id}`).value };
      out(await callTool('mcp_chat_update_message', payload));
      if (state.selectedConversation) await readChatConversation(state.selectedConversation);
      await refreshStatus();
    };
    window.deleteChatMessage = async (id) => {
      if (!confirm('确认删除这条聊天消息？')) return;
      out(await callTool('mcp_chat_delete_message', { id }));
      if (state.selectedConversation) await readChatConversation(state.selectedConversation);
      await refreshStatus();
    };
    window.deleteChatConversation = async (conversationId) => {
      if (!confirm('确认删除这个聊天会话的全部消息与上下文？')) return;
      out(await callTool('mcp_chat_delete_conversation', { conversation_id: conversationId }));
      if (state.selectedConversation === conversationId) {
        renderChatMessages({ conversation_id:'', messages:[] });
        renderChatContext({ conversation_id:'', entries:[] });
      }
      await refreshStatus();
    };

    async function clearChatRecords() {
      if (!confirm('确认清空全部聊天记录和恢复上下文？这个操作不可撤销。')) return;
      out(await callTool('mcp_chat_clear', {}));
      renderChatMessages({ conversation_id:'', messages:[] });
      renderChatContext({ conversation_id:'', entries:[] });
      await refreshStatus();
    }

    async function mergeChatConversations() {
      const target = $('chatMergeTarget').value.trim();
      const sources = Array.from(document.querySelectorAll('.chat-merge-select:checked')).map((el) => el.value).filter((item) => item && item !== target);
      if (!target || !sources.length) { out({ok:false, error:'请选择要合并的会话，并填写目标会话 ID'}); return; }
      out(await callTool('mcp_chat_merge', { target_conversation_id: target, source_conversation_ids: sources }));
      await refreshStatus();
      await readChatConversation(target);
    }

    const safeName = (value, fallback) => String(value || fallback).replace(/[^A-Za-z0-9_.-]+/g, '-');
    const transcriptFilename = (sessionId) => `mcp-transcript-${safeName(sessionId, 'all-sessions')}.md`;
    const chatFilename = (conversationId) => `chat-transcript-${safeName(conversationId, 'all-conversations')}.md`;
    const contextFilename = (conversationId) => `chat-context-${safeName(conversationId, 'all-conversations')}.md`;

    function downloadMarkdown(markdown, filename) {
      const blob = new Blob([markdown || ''], { type:'text/markdown;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }

    async function exportTranscriptPayload(sessionId) {
      const args = { max_events:1000, write_file:true };
      if (sessionId) args.session_id = sessionId;
      const data = await callTool('mcp_transcript_export', args);
      downloadMarkdown(data.markdown || '', transcriptFilename(sessionId));
      out({ ...data, markdown:`${(data.markdown || '').length} chars` });
      await refreshStatus();
    }

    async function exportChatPayload(conversationId) {
      const args = { max_messages:5000, write_file:true };
      if (conversationId) args.conversation_id = conversationId;
      const data = await callTool('mcp_chat_export', args);
      downloadMarkdown(data.markdown || '', chatFilename(conversationId));
      out({ ...data, markdown:`${(data.markdown || '').length} chars` });
      await refreshStatus();
    }

    async function exportChatContextPayload(conversationId) {
      const args = { max_entries:5000, write_file:true };
      if (conversationId) args.conversation_id = conversationId;
      const data = await callTool('mcp_chat_context_export', args);
      downloadMarkdown(data.markdown || '', contextFilename(conversationId));
      out({ ...data, markdown:`${(data.markdown || '').length} chars` });
      await refreshStatus();
    }

    async function oauthLogin() {
      const verifier = Array.from(crypto.getRandomValues(new Uint8Array(48))).map((b) => 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~'[b % 66]).join('');
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
      const challenge = btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
      sessionStorage.setItem('mcpAdminVerifier', verifier);
      const params = new URLSearchParams({ response_type:'code', client_id:'admin-console', redirect_uri:location.origin + '/admin', code_challenge:challenge, code_challenge_method:'S256', scope:'admin', state:String(Date.now()) });
      location.href = '/oauth/authorize?' + params.toString();
    }

    async function exchangeOAuthCode() {
      const url = new URL(location.href);
      const code = url.searchParams.get('code');
      const verifier = sessionStorage.getItem('mcpAdminVerifier');
      if (!code || !verifier) return;
      const body = new URLSearchParams({ grant_type:'authorization_code', client_id:'admin-console', redirect_uri:location.origin + '/admin', code, code_verifier:verifier });
      const res = await fetch('/oauth/token', { method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body });
      const data = await res.json().catch(() => ({ ok:false, error:`HTTP ${res.status}` }));
      if (!res.ok || !data.access_token) throw { ...data, status: res.status };
      state.token = data.access_token;
      localStorage.setItem('mcpAdminToken', state.token);
      sessionStorage.removeItem('mcpAdminVerifier');
      setValue('token', state.token);
      history.replaceState(null, '', '/admin');
    }

    function wireEvents() {
      document.addEventListener('click', (event) => {
        const nav = event.target.closest('[data-nav]');
        if (nav) setView(nav.dataset.nav);
        const track = event.target.closest('[data-chat-track]');
        if (track) setChatTrack(track.dataset.chatTrack);
      });
      $('navToggle').onclick = () => {
        const app = $('adminApp');
        if (window.matchMedia('(max-width: 780px)').matches) app.classList.toggle('nav-open');
        else app.classList.toggle('nav-collapsed');
      };
      $('navClose').onclick = () => $('adminApp').classList.remove('nav-open');
      $('navBackdrop').onclick = () => $('adminApp').classList.remove('nav-open');
      $('closeOutput').onclick = () => $('outputPanel').classList.add('hidden');
      $('showOutput').onclick = () => $('outputPanel').classList.remove('hidden');
      $('token').value = state.token;
      populateWizard(defaultServer);
      syncJsonFromWizard();
      setValue('advancedPayload', JSON.stringify(toolTemplate, null, 2));
      $('saveToken').onclick = () => { state.token = $('token').value.trim(); localStorage.setItem('mcpAdminToken', state.token); refreshStatus(); };
      $('clearToken').onclick = () => { if (!confirm('确认清除本地保存的 token？')) return; state.token = ''; localStorage.removeItem('mcpAdminToken'); setValue('token', ''); refreshStatus(); };
      $('oauthLogin').onclick = oauthLogin;
      $('refresh').onclick = refreshStatus;
      $('wizardTransport').onchange = updateTransportFields;
      $('applyTemplate').onclick = () => { populateWizard(selectedTemplateConfig()); syncJsonFromWizard(); };
      $('refreshTemplates').onclick = refreshTemplatesFromTool;
      $('syncJson').onclick = syncJsonFromWizard;
      $('syncWizard').onclick = syncWizardFromJson;
      $('formatJson').onclick = () => { const config = parseServerConfig(); setValue('serverConfig', JSON.stringify(config, null, 2)); out({ok:true, message:'JSON 已格式化'}); };
      $('planServer').onclick = async () => out(await callTool('mcp_server_plan', { config: parseServerConfig() }));
      $('installServer').onclick = async () => { out(await callTool('mcp_server_install', { config: parseServerConfig(), apply:true })); await refreshStatus(); };
      $('secretSet').onclick = async () => {
        const name = $('secretName').value.trim();
        const value = $('secretValue').value;
        if (!name || !value) { out({ok:false, error:'密钥名和值不能为空'}); return; }
        out(await callTool('mcp_secret_set', { name, value }));
        setValue('secretValue', '');
        await refreshStatus();
      };
      $('secretDelete').onclick = async () => {
        const name = $('secretName').value.trim();
        if (!name) { out({ok:false, error:'密钥名不能为空'}); return; }
        if (!confirm('确认删除这个密钥？')) return;
        out(await callTool('mcp_secret_delete', { name }));
        await refreshStatus();
      };
      $('reloadUpstream').onclick = async () => { out(await api('/api/admin/runtime', { reload_upstream:true })); await refreshStatus(); };
      $('setDefaultCwd').onclick = async () => { out(await api('/api/admin/runtime', { default_cwd:$('defaultCwd').value })); await refreshStatus(); };
      $('exportAllTranscripts').onclick = async () => exportTranscriptPayload();
      $('exportAllChatTranscripts').onclick = async () => exportChatPayload();
      $('exportAllChatContexts').onclick = async () => exportChatContextPayload();
      $('clearChatRecords').onclick = clearChatRecords;
      $('mergeChatConversations').onclick = mergeChatConversations;
      $('saveRuntimeAuth').onclick = async () => { out(await api('/api/admin/runtime', { auth_token:$('runtimeAuthToken').value, admin_token:$('runtimeAdminToken').value, oauth_password:$('runtimeOAuthPassword').value })); await refreshStatus(); };
      $('generateOAuthTokenSecret').onclick = () => { $('settingsOAuthTokenSecret').value = randomHex(32); };
      $('saveStartupSettings').onclick = async () => { out(await api('/api/admin/settings', { settings:startupSettingsPayload() })); setValue('settingsOAuthTokenSecret', ''); await refreshStatus(); };
      $('advancedRun').onclick = async () => { const payload = JSON.parse($('advancedPayload').value); out(await callTool(payload.name, payload.arguments || {})); await refreshStatus(); };
    }

    wireEvents();
    setView('overview');
    setChatTrack('backup');
    exchangeOAuthCode().catch(out).finally(() => { refreshStatus(); setInterval(refreshStatus, 5000); });
