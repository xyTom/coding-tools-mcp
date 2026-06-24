const $ = (id) => document.getElementById(id);
    const state = {
      token: localStorage.getItem('mcpAdminToken') || '',
      status: null,
      active: 'overview',
      templates: {},
      selectedConversation: '',
      activeChatTrack: 'backup',
      chatConversations: [],
      chatSearch: '',
      chatRecordKind: 'all',
      showChatSource: false,
      chatMessages: [],
      chatContextEntries: [],
      editingContextId: null,
      busyCount: 0,
      dirty: false,
      suppressDirtyTracking: false,
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
    const debounce = (fn, wait = 280) => {
      let timer = null;
      return (...args) => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => fn(...args), wait);
      };
    };
    const headers = () => ({ 'Content-Type':'application/json', ...(state.token ? {'Authorization':'Bearer '+state.token} : {}) });
    const east8Formatter = new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hourCycle: 'h23',
    });

    function parseTime(value) {
      if (!value) return null;
      const raw = String(value).trim();
      const normalized = raw.includes('T') || /Z$|[+-]\d\d:?\d\d$/.test(raw) ? raw : raw.replace(' ', 'T') + 'Z';
      const date = new Date(normalized);
      return Number.isNaN(date.getTime()) ? null : date;
    }

    function formatEast8(value) {
      const date = parseTime(value);
      if (!date) return String(value || '');
      return east8Formatter.format(date).replace(/\//g, '-');
    }

    function east8DateKey(value) {
      const formatted = formatEast8(value);
      return formatted ? formatted.slice(0, 10) : '未分组';
    }

    function timeCell(value) {
      const formatted = formatEast8(value);
      return formatted ? `<span class="time-main">${esc(formatted)}</span><span class="mini-label">UTC+8</span>` : '<span class="muted">-</span>';
    }

    function utcTimestamp() {
      return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
    }

    function roleLabel(role) {
      return ({ user:'用户', assistant:'助手', system:'系统', tool:'工具' })[String(role || '').toLowerCase()] || String(role || '未知');
    }

    function kindLabel(kind) {
      return ({ checkpoint:'上下文检查点', summary:'摘要', note:'备注', context:'上下文' })[String(kind || '').toLowerCase()] || String(kind || '上下文');
    }

    function localizeConversationTitle(title) {
      const raw = String(title || '').trim();
      if (!raw) return '';
      return raw
        .replace(/MCP Chat Backup Dual Track/gi, 'MCP 聊天备份双轨')
        .replace(/Chat Backup Test/gi, '聊天备份测试')
        .replace(/Chat Backup/gi, '聊天备份')
        .replace(/Dual Track/gi, '双轨')
        .replace(/Encoding Fixed/gi, '编码修复')
        .replace(/Clean/gi, '清理')
        .replace(/-/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    }

    function conversationTitle(conversation) {
      const title = localizeConversationTitle(conversation.title);
      if (title) return title;
      const id = String(conversation.conversation_id || '');
      const date = east8DateKey(conversation.last_seen || conversation.first_seen || conversation.date);
      return date && date !== '未分组' ? `聊天会话 ${date}` : '未命名会话';
    }

    function conversationSearchText(conversation) {
      return [conversation.conversation_id, conversation.title, conversation.unique_id, conversationTitle(conversation)]
        .map((item) => String(item || '').toLowerCase())
        .join('\n');
    }

    function selectedConversationLabel() {
      const id = state.selectedConversation || '';
      const conversation = state.chatConversations.find((item) => String(item.conversation_id || '') === id);
      if (!conversation) return id || '未选择';
      const uid = conversation.unique_id ? ` / UID: ${conversation.unique_id}` : '';
      return `${conversationTitle(conversation)}${uid} / ID: ${id}`;
    }

    function restoreChatConversationsFromStatus() {
      state.chatConversations = state.status?.chat_conversations?.conversations || [];
    }

    function setOutputContent(value) {
      const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      setText('output', text);
      setText('outputMirror', text);
      setText('resultSummary', text.length > 120 ? text.slice(0, 120) + '...' : text);
      return text;
    }

    function setBusy(active, label = '处理中') {
      state.busyCount = Math.max(0, state.busyCount + (active ? 1 : -1));
      document.body.classList.toggle('is-busy', state.busyCount > 0);
      const pill = $('busyState');
      if (pill) {
        pill.textContent = state.busyCount > 0 ? label : '空闲';
        pill.className = `status-pill busy ${state.busyCount > 0 ? '' : 'hidden'}`.trim();
      }
    }

    async function withBusy(label, task) {
      setBusy(true, label);
      try { return await task(); }
      catch (err) { out(err); throw err; }
      finally { setBusy(false, label); }
    }

    function setDirty(value) {
      state.dirty = Boolean(value);
      $('dirtyBadge')?.classList.toggle('hidden', !state.dirty);
    }

    function confirmDirty(action = '继续') {
      return !state.dirty || confirm(`还有未保存的编辑，确认${action}？`);
    }

    function setFormValues(values) {
      state.suppressDirtyTracking = true;
      try {
        for (const [id, value] of Object.entries(values)) setValue(id, value);
      } finally {
        state.suppressDirtyTracking = false;
      }
    }

    function out(value) {
      const text = setOutputContent(value);
      $('outputPanel')?.classList.remove('hidden');
      return text;
    }

    function closeOutputPanel() {
      $('outputPanel')?.classList.add('hidden');
    }

    function showOutputPanel() {
      $('outputPanel')?.classList.remove('hidden');
    }

    function closeChatReader() {
      if (!confirmDirty('收起会话工作区')) return;
      $('chatReaderPanel')?.classList.add('hidden');
      $('chatWorkspaceEmpty')?.classList.remove('hidden');
    }

    function showChatReader() {
      $('chatWorkspaceEmpty')?.classList.add('hidden');
      $('chatReaderPanel')?.classList.remove('hidden');
      const body = document.querySelector('.chat-reader-body');
      if (body) body.scrollTop = 0;
    }

    function setView(name) {
      if (name !== state.active && !confirmDirty('切换页面')) return;
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
      return withBusy(name, async () => {
        const data = await api('/api/admin/tool', { name, arguments: args });
        return data.structuredContent || data;
      });
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

    async function refreshStatus(options = {}) {
      const { silent = false, force = false } = options;
      if (state.dirty && !force) return;
      if (!silent) setBusy(true, '刷新中');
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
      } finally {
        if (!silent) setBusy(false, '刷新中');
      }
    }

    function renderStatus(data) {
      const conversations = data.chat_conversations?.conversations || [];
      if (!state.chatSearch) state.chatConversations = conversations;
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
      renderChatConversations();
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
      setHtml('httpSessions', httpSessions.map((s) => `<tr><td><code>${esc(s.session_id)}</code><div class="muted">请求 ${esc(s.request_count || 0)} 次</div></td><td><span class="mini-label">最近 RPC</span><code>${esc(s.last_rpc_method || s.last_method || '')}</code><div class="time-cell">${timeCell(s.last_seen)}</div></td><td class="path-cell"><span class="mini-label">会话默认目录</span><code>${esc(s.default_cwd_display || s.default_cwd || '')}</code></td><td class="path-cell"><span class="mini-label">工作区</span><code>${esc(s.workspace || '')}</code></td><td><span class="mini-label">来源</span>${esc(s.remote_addr || '')}<div class="muted">${esc(s.user_agent || '')}</div></td><td><button class="secondary" onclick="exportTranscript('${esc(jsArg(s.session_id))}')">导出 MD</button></td></tr>`).join('') || '<tr><td colspan="6" class="muted">暂无 MCP HTTP 会话。访问 /mcp 后会显示在这里。</td></tr>');
      setHtml('execSessions', execSessions.map((s) => `<tr><td><code>${esc(s.session_id)}</code><div class="time-cell">${timeCell(s.started_at)}</div></td><td>${esc(s.status)}</td><td class="path-cell"><span class="mini-label">命令执行目录</span><code>${esc(s.workdir)}</code></td><td><span class="mini-label">命令</span>${esc(s.command)}</td><td><button class="danger" onclick="terminateSession('${esc(jsArg(s.session_id))}')">终止</button></td></tr>`).join('') || '<tr><td colspan="5" class="muted">暂无运行命令会话。</td></tr>');
    }

    function renderChatConversations(conversations = state.chatConversations) {
      const search = String(state.chatSearch || '').trim().toLowerCase();
      const kind = state.chatRecordKind || 'all';
      const visible = conversations.filter((c) => {
        const messageCount = Number(c.message_count || 0);
        const contextCount = Number(c.context_entry_count || 0);
        if (kind === 'backup' && messageCount <= 0) return false;
        if (kind === 'context' && contextCount <= 0) return false;
        if (kind === 'both' && (messageCount <= 0 || contextCount <= 0)) return false;
        return !search || conversationSearchText(c).includes(search);
      });
      const table = $('chatConversationsTable');
      if (table) table.classList.toggle('hide-source', !state.showChatSource);
      const columnCount = state.showChatSource ? 5 : 4;
      let currentDate = '';
      const rows = [];
      for (const c of visible) {
        const id = String(c.conversation_id || '');
        const date = east8DateKey(c.last_seen || c.first_seen || c.date);
        if (date !== currentDate) {
          currentDate = date;
          rows.push(`<tr class="group-row"><td colspan="${columnCount}">${esc(date)}</td></tr>`);
        }
        const title = conversationTitle(c);
        const originalTitle = c.title && localizeConversationTitle(c.title) !== c.title ? `<div class="muted">原名：${esc(c.title)}</div>` : '';
        const uid = c.unique_id ? `<div class="identity-line"><span>UID</span><code>${esc(c.unique_id)}</code></div>` : '';
        const selected = state.selectedConversation === id ? ' class="conversation-row-selected"' : '';
        rows.push(`<tr${selected}><td class="conversation-cell"><label class="checkline conversation-check"><input type="checkbox" class="chat-merge-select" value="${esc(id)}"><span><strong>${esc(title)}</strong>${originalTitle}${uid}<div class="identity-line"><span>ID</span><code>${esc(id)}</code></div><div class="identity-line"><span>开始</span><code>${esc(formatEast8(c.first_seen) || '-')}</code></div></span></label></td><td><span class="record-count backup-count"><strong>${esc(c.message_count || 0)}</strong><span>聊天</span></span> <span class="record-count context-count"><strong>${esc(c.context_entry_count || 0)}</strong><span>上下文</span></span></td><td class="time-cell">${timeCell(c.last_seen)}</td><td class="chat-source-col source-cell"><code>${esc(c.source || '')}</code></td><td class="toolbar action-stack"><button onclick="readChatConversation('${esc(jsArg(id))}')">打开</button><button class="secondary" onclick="recallChatConversation('${esc(jsArg(id))}')">恢复包</button></td></tr>`);
      }
      const empty = search || kind !== 'all'
        ? '没有符合筛选条件的聊天记录。'
        : '暂无聊天记录。agent 调用 record_chat_transcript 或 record_chat_message 后会显示在这里。';
      setHtml('chatConversations', rows.join('') || `<tr><td colspan="${columnCount}" class="muted">${empty}</td></tr>`);
    }

    async function applyChatFilter() {
      state.chatSearch = $('chatSearch')?.value.trim() || '';
      state.chatRecordKind = $('chatRecordKind')?.value || 'all';
      state.showChatSource = Boolean($('showChatSource')?.checked);
      localStorage.setItem('mcpShowChatSource', state.showChatSource ? '1' : '0');
      if (state.chatSearch) {
        try {
          const data = await callTool('mcp_chat_conversations', { limit:500, query:state.chatSearch });
          state.chatConversations = data.conversations || [];
        } catch (err) {
          out(err);
        }
      } else {
        restoreChatConversationsFromStatus();
      }
      renderChatConversations();
    }

    function clearChatFilter() {
      state.chatSearch = '';
      state.chatRecordKind = 'all';
      state.showChatSource = false;
      setValue('chatSearch', '');
      setValue('chatRecordKind', 'all');
      const showSource = $('showChatSource');
      if (showSource) showSource.checked = false;
      localStorage.removeItem('mcpShowChatSource');
      restoreChatConversationsFromStatus();
      renderChatConversations();
    }

    function renderChatMessages(data) {
      const messages = data.messages || [];
      state.chatMessages = messages;
      state.selectedConversation = data.conversation_id || state.selectedConversation || '';
      setText('chatSelectedConversation', selectedConversationLabel());
      setText('backupCount', `${messages.length} 条`);
      setHtml('chatMessages', messages.map((m) => {
        const id = Number(m.id);
        const role = String(m.role || '').toLowerCase();
        const roleClass = role.replace(/[^a-z0-9_-]/g, '') || 'unknown';
        return `<div class="chat-message role-${esc(roleClass)}"><div class="chat-message-head"><strong><span class="role-pill">${esc(roleLabel(role))}</span> #${esc(id)}</strong><span class="time-cell">${timeCell(m.timestamp)}</span><button class="danger" onclick="deleteChatMessage(${id})">删除</button></div><div class="form-grid"><div><label>角色</label><input id="chatRole-${id}" value="${esc(m.role || '')}"></div><div><label>时间</label><input id="chatTime-${id}" value="${esc(m.timestamp || '')}"></div><details class="source-detail full"><summary>来源</summary><label>来源</label><input id="chatSource-${id}" value="${esc(m.source || '')}"></details><div class="full"><label>聊天文本</label><textarea class="chat-content" id="chatContent-${id}">${esc(m.content || '')}</textarea></div></div><div class="toolbar"><button onclick="saveChatMessage(${id})">保存修改</button></div></div>`;
      }).join('') || '<div class="muted">选择一个聊天会话后，这里会显示可编辑的完整消息文本。</div>');
    }

    function renderChatContext(data) {
      const entries = data.entries || [];
      state.chatContextEntries = entries;
      state.selectedConversation = data.conversation_id || state.selectedConversation || '';
      setText('chatSelectedConversation', selectedConversationLabel());
      setText('contextCount', `${entries.length} 条`);
      setHtml('chatContextEntries', entries.map((entry) => `<div class="context-entry"><div class="context-entry-head"><strong><span class="context-pill">${esc(kindLabel(entry.kind))}</span> #${esc(entry.id)}</strong><span class="time-cell">${timeCell(entry.timestamp)}</span><div class="toolbar"><button class="secondary" onclick="editContextEntry(${Number(entry.id)})">编辑</button><button class="danger" onclick="deleteContextEntry(${Number(entry.id)})">删除</button></div></div><details class="source-detail"><summary>来源与条目 ID</summary><div class="muted">来源：${esc(entry.source || '')}${entry.entry_id ? ` / entry_id: ${esc(entry.entry_id)}` : ''}</div></details><pre>${esc(entry.content || '')}</pre></div>`).join('') || '<div class="muted">这个会话还没有恢复上下文条目。agent 调用 record-context 或兼容入口后会显示在这里。</div>');
    }

    function resetContextForm() {
      state.editingContextId = null;
      setText('contextEditorMode', '新增');
      setFormValues({
        contextConversationId: state.selectedConversation || '',
        contextKind: 'checkpoint',
        contextEntryId: `manual-${Date.now()}`,
        contextTimestamp: utcTimestamp(),
        contextSource: 'webui',
        contextContent: '',
      });
      setDirty(false);
    }

    function prefillContextFromSelected() {
      setFormValues({ contextConversationId: state.selectedConversation || $('contextConversationId')?.value || '' });
      if (!$('contextEntryId')?.value) setValue('contextEntryId', `manual-${Date.now()}`);
      if (!$('contextTimestamp')?.value) setValue('contextTimestamp', utcTimestamp());
      if (!$('contextSource')?.value) setValue('contextSource', 'webui');
    }

    function contextFormPayload() {
      const conversationId = $('contextConversationId')?.value.trim() || state.selectedConversation;
      const content = $('contextContent')?.value || '';
      if (!conversationId) return { ok:false, error:'请先填写或选择会话 ID' };
      if (!content.trim()) return { ok:false, error:'恢复上下文内容不能为空' };
      return {
        ok:true,
        payload: {
          conversation_id: conversationId,
          kind: $('contextKind')?.value || 'checkpoint',
          entry_id: $('contextEntryId')?.value.trim() || undefined,
          timestamp: $('contextTimestamp')?.value.trim() || utcTimestamp(),
          source: $('contextSource')?.value.trim() || 'webui',
          content,
        },
      };
    }

    async function saveContextEntry() {
      const form = contextFormPayload();
      if (!form.ok) { out(form); return; }
      const payload = form.payload;
      const conversationId = payload.conversation_id;
      const toolName = state.editingContextId ? 'mcp_chat_update_context' : 'mcp_chat_record_context';
      if (state.editingContextId) payload.id = state.editingContextId;
      out(await callTool(toolName, payload));
      state.editingContextId = null;
      setDirty(false);
      await refreshStatus({ force:true });
      await window.readChatConversation(conversationId);
      setChatTrack('context');
    }

    window.editContextEntry = (id) => {
      const entry = state.chatContextEntries.find((item) => Number(item.id) === Number(id));
      if (!entry) { out({ok:false, error:'没有找到上下文条目'}); return; }
      state.editingContextId = Number(id);
      setText('contextEditorMode', `编辑 #${id}`);
      setFormValues({
        contextConversationId: state.selectedConversation || entry.conversation_id || '',
        contextKind: entry.kind || 'checkpoint',
        contextEntryId: entry.entry_id || '',
        contextTimestamp: entry.timestamp || utcTimestamp(),
        contextSource: entry.source || 'webui',
        contextContent: entry.content || '',
      });
      setDirty(false);
      setView('persistence');
      $('contextContent')?.focus();
    };

    window.deleteContextEntry = async (id) => {
      if (!confirmDirty('删除上下文条目')) return;
      if (!confirm('确认删除这条恢复上下文？')) return;
      out(await callTool('mcp_chat_delete_context', { id }));
      if (state.selectedConversation) await window.readChatConversation(state.selectedConversation);
      await refreshStatus({ force:true });
    };

    function renderMcpRequests(requests) {
      setHtml('mcpRequests', requests.slice().reverse().map((r) => `<tr><td class="time-cell">${timeCell(r.timestamp)}</td><td><code>${esc(r.session_id)}</code></td><td><code>${esc(r.method)} ${esc(r.path)}</code></td><td><code>${esc(r.rpc_method || '')}</code></td><td>${esc(r.status)}</td><td><code>${esc(r.default_cwd_display || '')}</code></td></tr>`).join('') || '<tr><td colspan="6" class="muted">暂无 /mcp 访问记录。</td></tr>');
    }

    function renderCalls(calls) {
      setHtml('calls', calls.slice().reverse().map((c) => `<tr><td class="time-cell">${timeCell(c.timestamp)}</td><td><code>${esc(c.tool)}</code></td><td>${c.ok ? '成功' : '失败'}</td><td>${esc(c.duration_ms)} ms</td><td>${esc(c.error_code || '')}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">暂无调用记录。</td></tr>');
    }

    window.toggleServer = async (alias, enabled) => { out(await callTool(enabled ? 'mcp_server_enable' : 'mcp_server_disable', { alias, apply:true })); await refreshStatus(); };
    window.checkServer = async (alias) => { out(await callTool('mcp_server_health', { alias })); };
    window.serverLogs = async (alias) => { out(await callTool('mcp_server_logs', { alias, max_lines:80 })); };
    window.restartServer = async (alias) => { await callTool('mcp_server_stop', { alias }); out(await callTool('mcp_server_start', { alias })); await refreshStatus(); };
    window.removeServer = async (alias) => { if (!confirm('确认删除这个 MCP 配置？')) return; out(await callTool('mcp_server_remove', { alias, apply:true })); await refreshStatus(); };
    window.terminateSession = async (sessionId) => { if (!confirm('确认终止这个会话？')) return; out(await api('/api/admin/runtime', { terminate_session: sessionId })); await refreshStatus(); };
    window.exportTranscript = async (sessionId) => exportTranscriptPayload(sessionId);
    window.readChatConversation = async (conversationId) => {
      if (state.dirty && !confirmDirty(conversationId === state.selectedConversation ? '重新读取会话' : '切换会话')) return;
      state.selectedConversation = conversationId;
      if (!state.editingContextId) setFormValues({ contextConversationId: conversationId });
      const limit = Math.max(1, Math.min(5000, Number($('chatReadLimit')?.value || 1000)));
      const [messages, context] = await Promise.all([
        callTool('mcp_chat_messages', { conversation_id: conversationId, limit }),
        callTool('mcp_chat_context', { conversation_id: conversationId, limit }),
      ]);
      renderChatMessages(messages);
      renderChatContext(context);
      renderChatConversations();
      setChatTrack((messages.message_count || messages.messages?.length) ? 'backup' : 'context');
      setOutputContent({ ok:true, message:'已读取会话正文', conversation_id:conversationId, chat_backup_messages:messages.message_count || 0, restore_context_entries:context.entry_count || 0 });
      setDirty(false);
      showChatReader();
    };
    window.exportChatTranscript = async (conversationId) => exportChatPayload(conversationId);
    window.exportChatContext = async (conversationId) => exportChatContextPayload(conversationId);
    window.recallChatConversation = async (conversationId) => {
      const id = conversationId || state.selectedConversation || $('contextConversationId')?.value.trim();
      if (!id) { out({ok:false, error:'请先选择或填写会话 ID'}); return; }
      const data = await callTool('mcp_chat_recall', { conversation_id:id, max_messages:5000, max_context_entries:5000 });
      setOutputContent(data.context_text || data.markdown || data);
      showOutputPanel();
    };
    window.saveChatMessage = async (id) => {
      const payload = { id, role:$(`chatRole-${id}`).value, timestamp:$(`chatTime-${id}`).value, source:$(`chatSource-${id}`).value, content:$(`chatContent-${id}`).value };
      out(await callTool('mcp_chat_update_message', payload));
      setDirty(false);
      if (state.selectedConversation) await window.readChatConversation(state.selectedConversation);
      await refreshStatus({ force:true });
    };
    window.deleteChatMessage = async (id) => {
      if (!confirmDirty('删除消息')) return;
      if (!confirm('确认删除这条聊天消息？')) return;
      out(await callTool('mcp_chat_delete_message', { id }));
      if (state.selectedConversation) await window.readChatConversation(state.selectedConversation);
      await refreshStatus({ force:true });
    };
    window.deleteChatConversation = async (conversationId) => {
      if (!confirmDirty('删除会话')) return;
      if (!confirm('确认删除这个聊天会话的全部消息与上下文？')) return;
      out(await callTool('mcp_chat_delete_conversation', { conversation_id: conversationId }));
      if (state.selectedConversation === conversationId) {
        state.selectedConversation = '';
        setDirty(false);
        renderChatMessages({ conversation_id:'', messages:[] });
        renderChatContext({ conversation_id:'', entries:[] });
        closeChatReader();
      }
      await refreshStatus({ force:true });
    };

    async function clearChatRecords() {
      if (!confirmDirty('清空全部记录')) return;
      if (!confirm('确认清空全部聊天记录和恢复上下文？这个操作不可撤销。')) return;
      out(await callTool('mcp_chat_clear', {}));
      state.selectedConversation = '';
      setDirty(false);
      renderChatMessages({ conversation_id:'', messages:[] });
      renderChatContext({ conversation_id:'', entries:[] });
      closeChatReader();
      await refreshStatus({ force:true });
    }

    async function mergeChatConversations() {
      if (!confirmDirty('合并会话')) return;
      const target = $('chatMergeTarget').value.trim();
      const sources = Array.from(document.querySelectorAll('.chat-merge-select:checked')).map((el) => el.value).filter((item) => item && item !== target);
      if (!target || !sources.length) { out({ok:false, error:'请选择要合并的会话，并填写目标会话 ID'}); return; }
      out(await callTool('mcp_chat_merge', { target_conversation_id: target, source_conversation_ids: sources }));
      await refreshStatus({ force:true });
      await window.readChatConversation(target);
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

    async function copyOutput() {
      const text = $('output')?.textContent || '';
      const button = $('copyOutput');
      try {
        await navigator.clipboard.writeText(text);
        setText('resultSummary', '输出已复制');
        if (button) {
          button.textContent = '已复制';
          window.setTimeout(() => { button.textContent = '复制'; }, 1200);
        }
      } catch (err) {
        out({ok:false, error:'浏览器不允许复制，请手动选中输出内容复制', detail:String(err)});
      }
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
      const debouncedChatFilter = debounce(() => applyChatFilter(), 320);
      const dirtySelector = '.chat-content, [id^="chatRole-"], [id^="chatTime-"], [id^="chatSource-"], #contextConversationId, #contextKind, #contextEntryId, #contextTimestamp, #contextSource, #contextContent';
      const trackDirty = (event) => {
        if (!state.suppressDirtyTracking && event.target.matches(dirtySelector)) setDirty(true);
      };
      document.addEventListener('click', (event) => {
        const nav = event.target.closest('[data-nav]');
        if (nav) setView(nav.dataset.nav);
        const track = event.target.closest('[data-chat-track]');
        if (track) setChatTrack(track.dataset.chatTrack);
      });
      document.addEventListener('input', trackDirty);
      document.addEventListener('change', trackDirty);
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          closeOutputPanel();
          closeChatReader();
        }
      });
      window.addEventListener('beforeunload', (event) => {
        if (!state.dirty) return;
        event.preventDefault();
        event.returnValue = '';
      });
      $('navToggle').onclick = () => {
        const app = $('adminApp');
        if (window.matchMedia('(max-width: 780px)').matches) app.classList.toggle('nav-open');
        else app.classList.toggle('nav-collapsed');
      };
      $('navClose').onclick = () => $('adminApp').classList.remove('nav-open');
      $('navBackdrop').onclick = () => $('adminApp').classList.remove('nav-open');
      $('closeOutput').onclick = closeOutputPanel;
      $('outputBackdrop').onclick = closeOutputPanel;
      $('showOutput').onclick = showOutputPanel;
      $('closeChatReader').onclick = closeChatReader;
      const chatReaderBackdrop = $('chatReaderBackdrop');
      if (chatReaderBackdrop) chatReaderBackdrop.onclick = closeChatReader;
      $('token').value = state.token;
      populateWizard(defaultServer);
      syncJsonFromWizard();
      setValue('advancedPayload', JSON.stringify(toolTemplate, null, 2));
      state.showChatSource = localStorage.getItem('mcpShowChatSource') === '1';
      const showSource = $('showChatSource');
      if (showSource) showSource.checked = state.showChatSource;
      $('saveToken').onclick = () => { state.token = $('token').value.trim(); localStorage.setItem('mcpAdminToken', state.token); refreshStatus(); };
      $('clearToken').onclick = () => { if (!confirm('确认清除本地保存的 token？')) return; state.token = ''; localStorage.removeItem('mcpAdminToken'); setValue('token', ''); refreshStatus(); };
      $('oauthLogin').onclick = oauthLogin;
      $('refresh').onclick = () => { if (confirmDirty('刷新页面状态')) refreshStatus({ force:true }); };
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
      $('prefillContextFromSelected').onclick = prefillContextFromSelected;
      $('saveContextEntry').onclick = saveContextEntry;
      $('resetContextEntry').onclick = resetContextForm;
      $('recallSelectedContext').onclick = () => window.recallChatConversation();
      $('reloadSelectedConversation').onclick = () => state.selectedConversation ? window.readChatConversation(state.selectedConversation) : out({ok:false, error:'请先选择会话'});
      $('exportSelectedChatTranscript').onclick = () => state.selectedConversation ? exportChatPayload(state.selectedConversation) : out({ok:false, error:'请先选择会话'});
      $('exportSelectedChatContext').onclick = () => state.selectedConversation ? exportChatContextPayload(state.selectedConversation) : out({ok:false, error:'请先选择会话'});
      $('deleteSelectedConversation').onclick = () => state.selectedConversation ? window.deleteChatConversation(state.selectedConversation) : out({ok:false, error:'请先选择会话'});
      $('applyChatFilter').onclick = () => applyChatFilter();
      $('clearChatFilter').onclick = clearChatFilter;
      $('chatSearch').oninput = debouncedChatFilter;
      $('chatSearch').onkeydown = (event) => { if (event.key === 'Enter') applyChatFilter(); };
      $('chatRecordKind').onchange = () => applyChatFilter();
      $('showChatSource').onchange = () => applyChatFilter();
      $('chatReadLimit').onchange = () => { if (state.selectedConversation) window.readChatConversation(state.selectedConversation); };
      $('copyOutput').onclick = copyOutput;
      $('saveRuntimeAuth').onclick = async () => { out(await api('/api/admin/runtime', { auth_token:$('runtimeAuthToken').value, admin_token:$('runtimeAdminToken').value, oauth_password:$('runtimeOAuthPassword').value })); await refreshStatus(); };
      $('generateOAuthTokenSecret').onclick = () => { $('settingsOAuthTokenSecret').value = randomHex(32); };
      $('saveStartupSettings').onclick = async () => { out(await api('/api/admin/settings', { settings:startupSettingsPayload() })); setValue('settingsOAuthTokenSecret', ''); await refreshStatus(); };
      $('advancedRun').onclick = async () => { const payload = JSON.parse($('advancedPayload').value); out(await callTool(payload.name, payload.arguments || {})); await refreshStatus(); };
    }

    wireEvents();
    setView('overview');
    setChatTrack('backup');
    resetContextForm();
    exchangeOAuthCode().catch(out).finally(() => { refreshStatus(); setInterval(() => refreshStatus({ silent:true }), 5000); });
