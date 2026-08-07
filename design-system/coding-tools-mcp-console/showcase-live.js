(() => {
  'use strict';

  const state = {
    baseUrl: '',
    token: '',
    connected: false,
    loading: false,
    payloads: {},
    conversations: [],
  };

  const icon = (name, className = 'icon') => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', className);
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  };

  const el = (tag, options = {}, children = []) => {
    const node = document.createElement(tag);
    if (options.className) node.className = options.className;
    if (options.text !== undefined) node.textContent = String(options.text);
    if (options.type) node.type = options.type;
    if (options.id) node.id = options.id;
    if (options.title) node.title = options.title;
    for (const [name, value] of Object.entries(options.attrs || {})) {
      node.setAttribute(name, String(value));
    }
    node.append(...children);
    return node;
  };

  function defaultApiBase() {
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      return `${location.origin}/admin/api`;
    }
    return 'http://127.0.0.1:8000/admin/api';
  }

  function normalizeBase(value) {
    return String(value || '').trim().replace(/\/+$/, '');
  }

  function describeError(error) {
    if (error instanceof TypeError) {
      return '无法连接服务。请确认地址正确、服务已启动，并允许当前页面来源访问 Admin API。直接用 file:// 打开时，浏览器也可能阻止跨域请求。';
    }
    return error?.message || '连接失败。';
  }

  async function apiRequest(path) {
    const response = await fetch(`${state.baseUrl}${path}`, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${state.token}`,
        Accept: 'application/json',
      },
      cache: 'no-store',
      credentials: 'omit',
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const message = payload?.error?.message || payload?.detail || `${response.status} ${response.statusText}`;
      throw new Error(message);
    }
    return payload || {};
  }

  function showLiveToast(title, detail) {
    if (typeof window.showToast === 'function') {
      window.showToast(title, detail);
      return;
    }
    const region = document.getElementById('toastRegion');
    const toast = el('div', { className: 'toast' });
    const badge = el('span', { className: 'card-icon' }, [icon('check')]);
    const copy = el('span');
    copy.append(el('strong', { text: title }), el('span', { text: detail || '' }));
    toast.append(badge, copy);
    region?.append(toast);
    window.setTimeout(() => toast.remove(), 3600);
  }

  function createLiveDialog() {
    const dialog = el('dialog', { id: 'liveConnectDialog', attrs: { 'aria-labelledby': 'live-connect-title' } });
    const form = el('form', { className: 'dialog-shell live-connect-form', id: 'liveConnectForm' });
    const header = el('header', { className: 'dialog-header' });
    const titleCopy = el('div');
    titleCopy.append(
      el('h2', { id: 'live-connect-title', text: '连接真实 Admin API' }),
      el('p', { text: '展示页以只读方式读取状态、连接、工作区和会话，不执行保存、删除或轮换操作。' }),
    );
    const close = el('button', { className: 'icon-button', type: 'button', attrs: { 'aria-label': '关闭' } }, [icon('close')]);
    close.addEventListener('click', () => dialog.close());
    header.append(titleCopy, close);

    const body = el('div', { className: 'dialog-body' });
    const baseField = el('div', { className: 'field' });
    const baseInput = el('input', { id: 'liveApiBase', attrs: { type: 'url', required: '', spellcheck: 'false' } });
    baseInput.value = defaultApiBase();
    baseField.append(
      el('label', { text: 'Admin API 地址', attrs: { for: 'liveApiBase' } }),
      baseInput,
      el('small', { text: '通常为 http://127.0.0.1:8000/admin/api，或同源的 /admin/api。' }),
    );

    const tokenField = el('div', { className: 'field' });
    const tokenInput = el('input', { id: 'liveAdminToken', attrs: { type: 'password', required: '', autocomplete: 'off' } });
    tokenField.append(
      el('label', { text: '专用 Admin token', attrs: { for: 'liveAdminToken' } }),
      tokenInput,
      el('small', { text: 'Token 只保存在当前页面内存中，不写入 URL、localStorage 或日志。' }),
    );

    const note = el('div', { className: 'live-connect-note' });
    note.append(
      el('strong', { text: '真实任务范围：' }),
      document.createTextNode('读取服务健康、检查默认 Workspace、识别待重启配置、列出工具连接、读取会话摘要与详情。'),
    );
    const error = el('div', { className: 'live-connect-error', id: 'liveConnectError', attrs: { hidden: '' } });
    body.append(baseField, tokenField, note, error);

    const footer = el('footer', { className: 'dialog-footer' });
    const cancel = el('button', { className: 'button button-secondary', type: 'button', text: '取消' });
    cancel.addEventListener('click', () => dialog.close());
    const submit = el('button', { className: 'button button-primary', type: 'submit', text: '连接并读取任务' });
    footer.append(cancel, submit);
    form.append(header, body, footer);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      error.hidden = true;
      state.baseUrl = normalizeBase(baseInput.value);
      state.token = tokenInput.value;
      submit.disabled = true;
      submit.textContent = '正在连接…';
      try {
        await loadLiveData();
        tokenInput.value = '';
        dialog.close();
        showLiveToast('已连接真实服务', '展示页已切换为只读实时数据模式。');
      } catch (connectError) {
        error.textContent = describeError(connectError);
        error.hidden = false;
      } finally {
        submit.disabled = false;
        submit.textContent = '连接并读取任务';
      }
    });

    dialog.append(form);
    document.body.append(dialog);
    return dialog;
  }

  function enhanceConceptStrip(openDialog) {
    const strip = document.querySelector('.concept-strip');
    if (!strip) return;
    strip.classList.add('live-enabled');
    const copy = el('span', { className: 'live-strip-copy' });
    copy.append(icon('info'), el('span', { id: 'liveStripText', text: '独立效果展示 · 可连接真实 Admin API · 默认只读' }));
    const button = el('button', { className: 'live-strip-button', type: 'button', text: '连接真实服务' });
    button.addEventListener('click', () => openDialog.showModal());
    strip.replaceChildren(copy, button);
  }

  function createLivePanel(openDialog) {
    const hero = document.querySelector('#page-overview .hero');
    if (!hero) return;
    const panel = el('section', { className: 'live-data-panel', attrs: { 'aria-label': '真实数据连接状态' } });
    const copy = el('div', { className: 'live-data-copy' });
    const stateIcon = el('span');
    stateIcon.append(el('span', { className: 'live-state-dot', id: 'liveStateDot' }));
    const text = el('div');
    text.append(
      el('strong', { id: 'liveStateTitle', text: '当前使用演示数据' }),
      el('small', { id: 'liveStateDetail', text: '连接 Admin API 后，页面会读取真实状态并生成任务。' }),
    );
    copy.append(stateIcon, text);

    const actions = el('div', { className: 'live-panel-actions' });
    const connect = el('button', { className: 'button button-primary button-sm', type: 'button', text: '连接真实服务', id: 'liveConnectButton' });
    connect.addEventListener('click', () => openDialog.showModal());
    const refresh = el('button', { className: 'button button-secondary button-sm', type: 'button', text: '刷新真实数据', id: 'liveRefreshButton', attrs: { hidden: '' } });
    refresh.addEventListener('click', () => loadLiveData().catch((error) => showLiveToast('刷新失败', describeError(error))));
    const disconnect = el('button', { className: 'button button-quiet button-sm', type: 'button', text: '断开', id: 'liveDisconnectButton', attrs: { hidden: '' } });
    disconnect.addEventListener('click', () => {
      state.token = '';
      state.connected = false;
      location.reload();
    });
    actions.append(connect, refresh, disconnect);
    panel.append(copy, actions);
    hero.insertAdjacentElement('afterend', panel);
  }

  function setupTaskCard(openDialog) {
    const card = document.querySelector('#page-overview .bento-stack > article.card');
    if (!card) return;
    card.classList.add('live-task-card');
    const header = el('div', { className: 'card-header' });
    const copy = el('div');
    copy.append(
      el('h2', { text: '真实任务中心' }),
      el('p', { text: '根据当前服务状态自动整理下一步，不再只展示抽象指标。' }),
    );
    header.append(copy, el('span', { className: 'live-task-summary', id: 'liveTaskSummary', text: '未连接' }));
    const list = el('div', { className: 'live-task-list', id: 'liveTaskList' });
    const empty = el('div', { className: 'live-empty' });
    empty.append(document.createTextNode('连接真实 Admin API 后，将显示待重启配置、异常连接、Workspace 检查、OAuth 授权和会话恢复任务。'));
    const button = el('button', { className: 'button button-primary button-sm', type: 'button', text: '连接并读取任务' });
    button.style.marginTop = '.7rem';
    button.addEventListener('click', () => openDialog.showModal());
    empty.append(button);
    list.append(empty);
    card.replaceChildren(header, list);
  }

  function setConnectionState(kind, title, detail) {
    const dot = document.getElementById('liveStateDot');
    const titleNode = document.getElementById('liveStateTitle');
    const detailNode = document.getElementById('liveStateDetail');
    if (dot) dot.className = `live-state-dot ${kind || ''}`.trim();
    if (titleNode) titleNode.textContent = title;
    if (detailNode) detailNode.textContent = detail;
    const connect = document.getElementById('liveConnectButton');
    const refresh = document.getElementById('liveRefreshButton');
    const disconnect = document.getElementById('liveDisconnectButton');
    if (connect) connect.hidden = state.connected;
    if (refresh) refresh.hidden = !state.connected;
    if (disconnect) disconnect.hidden = !state.connected;
  }

  function setMetric(index, value, label) {
    const cards = [...document.querySelectorAll('#page-overview .metric-card')];
    const card = cards[index];
    if (!card) return;
    const strong = card.querySelector('.metric-copy strong');
    const span = card.querySelector('.metric-copy span');
    if (strong) strong.textContent = String(value);
    if (span && label) span.textContent = label;
  }

  function setHealth(items) {
    const side = document.querySelector('#page-overview .hero-side');
    if (!side) return;
    const score = items.filter((item) => item.ok).length;
    const percent = Math.round((score / Math.max(items.length, 1)) * 100);
    const scoreNode = side.querySelector('.health-score');
    const list = side.querySelector('.health-list');
    const time = side.querySelector('.muted-text');
    if (scoreNode) scoreNode.textContent = String(percent);
    if (time) time.textContent = `最近检查：${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
    if (!list) return;
    list.replaceChildren();
    for (const item of items) {
      const row = el('li', { className: 'health-item' });
      const result = el('span');
      result.append(icon(item.ok ? 'check' : 'info'), document.createTextNode(item.value));
      row.append(el('span', { text: item.label }), result);
      list.append(row);
    }
  }

  function buildTasks(payloads, defaultCheck) {
    const tasks = [];
    const servers = payloads.gateway?.persisted?.servers || {};
    const workspaces = payloads.workspaces?.workspace_catalog || [];
    const clients = payloads.clients?.items || [];
    const pending = payloads.settings?.pending_restart || [];
    const enabledServers = Object.entries(servers).filter(([, item]) => item?.enabled !== false);
    const disabledServers = Object.entries(servers).filter(([, item]) => item?.enabled === false);
    const defaultWorkspace = workspaces.find((item) => item.default);

    if (!Object.keys(servers).length) {
      tasks.push({ level: 'warning', icon: 'plug', title: '添加第一个工具连接', detail: '当前持久化 Gateway 配置中没有任何上游工具。', page: 'connections', action: '查看连接入口' });
    }
    if (payloads.gateway?.restart_required || pending.length) {
      const detail = pending.length ? `有 ${pending.length} 项服务器设置等待重启生效。` : '工具连接配置已保存，但当前 Runtime 仍使用旧快照。';
      tasks.push({ level: 'warning', icon: 'refresh', title: '安排一次服务重启', detail, page: 'security', action: '查看待生效项' });
    }
    if (disabledServers.length) {
      tasks.push({ level: 'warning', icon: 'plug', title: `复核 ${disabledServers.length} 个已停用连接`, detail: disabledServers.map(([alias]) => alias).slice(0, 3).join('、'), page: 'connections', action: '查看连接' });
    }
    if (!workspaces.length) {
      tasks.push({ level: 'danger', icon: 'folder', title: '添加可访问的工作文件夹', detail: '没有 Workspace 时，文件类任务无法获得明确的访问边界。', page: 'workspaces', action: '查看工作文件夹' });
    } else if (!defaultWorkspace || defaultWorkspace.enabled === false) {
      tasks.push({ level: 'danger', icon: 'folder', title: '设置一个可用的默认 Workspace', detail: '新会话需要一个已启用的默认工作目录。', page: 'workspaces', action: '查看工作文件夹' });
    }
    if (defaultWorkspace && defaultCheck && (!defaultCheck.exists || !defaultCheck.is_directory)) {
      tasks.push({ level: 'danger', icon: 'folder', title: '修复默认 Workspace 路径', detail: `${defaultWorkspace.name || defaultWorkspace.id} 的目录不存在或不是文件夹。`, page: 'workspaces', action: '查看检查结果' });
    }
    if (!payloads.status?.vault?.enabled) {
      tasks.push({ level: 'warning', icon: 'lock', title: '启用凭据保险箱', detail: 'Secret Vault 未启用，在线工具凭据无法获得推荐的加密保护。', page: 'security', action: '查看安全建议' });
    }
    const unboundClients = clients.filter((client) => {
      const access = client.workspace_access || {};
      const ids = access.workspace_ids || client.workspace_ids || [];
      return access.configured && ids.length === 0;
    });
    if (unboundClients.length) {
      tasks.push({ level: 'danger', icon: 'key', title: `为 ${unboundClients.length} 个 OAuth Client 分配 Workspace`, detail: '这些客户端已注册，但当前没有可授权的工作区。', page: 'security', action: '查看客户端权限' });
    }
    if ((payloads.conversations?.total || 0) > 0) {
      tasks.push({ level: 'success', icon: 'message', title: '继续最近一次工作', detail: `已找到 ${payloads.conversations.total} 个会话摘要，可读取详情并恢复上下文。`, page: 'records', action: '打开聊天记录' });
    }
    if (!tasks.length) {
      tasks.push({ level: 'success', icon: 'check', title: '当前没有阻塞任务', detail: `${enabledServers.length} 个工具连接和 ${workspaces.length} 个 Workspace 均已就绪。`, page: 'overview', action: '已就绪' });
    }
    return tasks;
  }

  function renderTasks(tasks) {
    const list = document.getElementById('liveTaskList');
    const summary = document.getElementById('liveTaskSummary');
    if (!list || !summary) return;
    const blocking = tasks.filter((task) => task.level === 'danger' || task.level === 'warning').length;
    summary.textContent = blocking ? `${blocking} 项待处理` : '状态良好';
    list.replaceChildren();
    for (const task of tasks.slice(0, 6)) {
      const row = el('article', { className: `live-task ${task.level}` });
      const badge = el('span', { className: 'live-task-icon' }, [icon(task.icon, 'icon icon-sm')]);
      const copy = el('span', { className: 'live-task-copy' });
      copy.append(el('strong', { text: task.title }), el('span', { text: task.detail }));
      const button = el('button', { className: 'button button-secondary button-sm', type: 'button', text: task.action });
      button.disabled = task.action === '已就绪';
      button.addEventListener('click', () => {
        document.querySelector(`[data-page="${task.page}"]`)?.click();
        showLiveToast(task.title, task.detail);
      });
      row.append(badge, copy, button);
      list.append(row);
    }
    list.append(el('p', { className: 'live-source-line', text: `任务依据：${state.baseUrl} · ${new Date().toLocaleString()} · 只读分析` }));
  }

  function renderConnections(gateway) {
    const grid = document.querySelector('#page-connections .connections-grid');
    if (!grid) return;
    const servers = gateway?.persisted?.servers || {};
    grid.replaceChildren();
    for (const [alias, config] of Object.entries(servers)) {
      const enabled = config?.enabled !== false;
      const card = el('article', { className: 'connection-card' });
      const top = el('div', { className: 'connection-top' });
      const heading = el('div', { className: 'connection-heading' });
      const headingCopy = el('span');
      headingCopy.append(el('strong', { text: alias }), el('span', { text: config.transport === 'streamable_http' ? '远程 MCP 服务' : '本机命令工具' }));
      heading.append(el('span', { className: 'connection-icon' }, [icon(config.transport === 'streamable_http' ? 'globe' : 'code')]), headingCopy);
      top.append(heading, el('span', { className: `badge ${enabled ? 'success' : 'info'}`, text: enabled ? '下次启动启用' : '已停用' }));

      const meta = el('div', { className: 'connection-meta' });
      for (const [label, value] of [
        ['连接方式', config.transport || '未知'],
        ['暴露方式', config.expose_mode || 'direct'],
        ['置顶工具', `${(config.pinned_tools || []).length} 个`],
      ]) {
        const line = el('div', { className: 'meta-line' });
        line.append(el('span', { text: label }), el('strong', { text: value }));
        meta.append(line);
      }

      const actions = el('div', { className: 'connection-actions' });
      const inspect = el('button', { className: 'button button-secondary button-sm', type: 'button', text: '查看任务说明' });
      inspect.addEventListener('click', () => showLiveToast(alias, '展示页只读；编辑、启停和删除仍应在正式管理台中完成。'));
      actions.append(inspect, el('span', { className: 'live-readonly-badge', text: '只读真实数据' }));
      card.append(
        top,
        el('p', { className: 'connection-description', text: config.expose_mode === 'broker' ? '按需搜索和调用工具，减少上下文占用。' : '直接向客户端展示过滤后的全部工具。' }),
        meta,
        actions,
      );
      grid.append(card);
    }
    if (!Object.keys(servers).length) grid.append(el('div', { className: 'live-empty', text: '真实 Gateway 配置中尚无工具连接。' }));
  }

  function renderWorkspaces(workspacesPayload) {
    const grid = document.querySelector('#page-workspaces .workspaces-grid');
    if (!grid) return;
    const items = workspacesPayload?.workspace_catalog || [];
    grid.replaceChildren();
    for (const workspace of items) {
      const card = el('article', { className: 'workspace-card' });
      const top = el('div', { className: 'workspace-top' });
      const heading = el('div', { className: 'workspace-heading' });
      const headingCopy = el('span');
      headingCopy.append(el('strong', { text: workspace.name || workspace.id }), el('span', { text: workspace.default ? '默认工作文件夹' : workspace.id }));
      heading.append(el('span', { className: 'workspace-icon' }, [icon('folder')]), headingCopy);
      top.append(heading, el('span', { className: `badge ${workspace.enabled !== false ? 'success' : 'info'}`, text: workspace.enabled !== false ? '已启用' : '已停用' }));

      const meta = el('div', { className: 'workspace-meta' });
      for (const [label, value] of [['位置', workspace.root || '—'], ['ID', workspace.id], ['默认', workspace.default ? '是' : '否']]) {
        const line = el('div', { className: 'meta-line' });
        line.append(el('span', { text: label }), el('strong', { text: value }));
        meta.append(line);
      }

      const actions = el('div', { className: 'workspace-actions' });
      const check = el('button', { className: 'button button-primary button-sm', type: 'button', text: '检查真实路径' });
      check.addEventListener('click', async () => {
        check.disabled = true;
        check.textContent = '检查中…';
        try {
          const result = await apiRequest(`/workspaces/${encodeURIComponent(workspace.id)}/check`);
          const info = result.check || result;
          showLiveToast(`${workspace.name || workspace.id} 检查完成`, `存在：${Boolean(info.exists)} · 文件夹：${Boolean(info.is_directory)}`);
        } catch (error) {
          showLiveToast('检查失败', describeError(error));
        } finally {
          check.disabled = false;
          check.textContent = '检查真实路径';
        }
      });
      actions.append(check, el('span', { className: 'live-readonly-badge', text: '真实 GET 任务' }));
      card.append(
        top,
        el('p', { className: 'workspace-description', text: '该卡片直接来自 Workspace Catalog；检查按钮调用只读路径验证接口。' }),
        meta,
        actions,
      );
      grid.append(card);
    }
    if (!items.length) grid.append(el('div', { className: 'live-empty', text: '真实 Workspace Catalog 为空。' }));
  }

  function renderConversationDetail(payload) {
    const detail = document.querySelector('#page-records .record-detail');
    if (!detail) return;
    detail.replaceChildren();
    const conversation = payload?.conversation || {};
    const header = el('header', { className: 'record-toolbar' });
    const heading = el('div');
    heading.append(
      el('span', { className: 'badge info', text: conversation.workspace_id || 'Workspace' }),
      el('h2', { text: conversation.title || conversation.conversation_id || '会话详情' }),
      el('p', { text: `真实详情 · ${(payload.messages || []).length} 条本页消息 · ${(payload.contexts || []).length} 条本页上下文` }),
    );
    header.append(heading, el('span', { className: 'live-readonly-badge', text: '按需读取正文' }));
    const stack = el('div', { className: 'message-stack', attrs: { 'aria-label': '真实会话内容' } });
    for (const message of (payload.messages || []).slice(0, 20)) {
      const article = el('article', { className: `message ${message.role === 'user' ? 'user' : ''}` });
      const messageHeader = el('header');
      messageHeader.append(el('strong', { text: message.role === 'user' ? '用户' : (message.role || 'Assistant') }), el('time', { text: message.created_at || '' }));
      article.append(messageHeader, el('p', { text: message.content || '' }));
      stack.append(article);
    }
    if (!(payload.messages || []).length) stack.append(el('div', { className: 'live-empty', text: '该页没有消息正文。' }));
    detail.append(header, stack);
  }

  function renderRecords(conversationsPayload) {
    const list = document.querySelector('#page-records .record-list');
    if (!list) return;
    state.conversations = conversationsPayload?.items || [];
    list.replaceChildren();
    for (const item of state.conversations) {
      const li = el('li');
      const button = el('button', { className: 'record-button', type: 'button' });
      button.append(
        el('strong', { text: item.title || item.conversation_id }),
        el('p', { text: item.preview || '无摘要' }),
        el('span', { className: 'record-meta' }, [
          el('span', { text: item.updated_at || item.created_at || '时间未知' }),
          el('span', { text: `${item.message_count || 0} 条消息` }),
        ]),
      );
      button.addEventListener('click', async () => {
        [...list.querySelectorAll('.record-button')].forEach((node) => node.removeAttribute('aria-current'));
        button.setAttribute('aria-current', 'true');
        try {
          const detail = await apiRequest(`/chat/conversations/${encodeURIComponent(item.workspace_id)}/${encodeURIComponent(item.conversation_id)}?message_page=1&message_page_size=50&context_page=1&context_page_size=20`);
          renderConversationDetail(detail);
        } catch (error) {
          showLiveToast('读取会话失败', describeError(error));
        }
      });
      li.append(button);
      list.append(li);
    }
    if (!state.conversations.length) list.append(el('li', {}, [el('div', { className: 'live-empty', text: '当前 Workspace 没有会话摘要。' })]));
  }

  function updateSecurity(payloads) {
    const blocks = [...document.querySelectorAll('#page-security .technical-block')];
    const values = {
      '监听地址': payloads.settings?.active?.host || '未报告',
      '服务端口': payloads.settings?.active?.port || '未报告',
      '权限模式': payloads.settings?.active?.permission_mode || '未报告',
      '工具暴露模式': Object.values(payloads.gateway?.persisted?.servers || {})[0]?.expose_mode || '未配置',
      'OAuth 公开地址': payloads.settings?.active?.oauth_server_url || '未配置',
      '配置版本': payloads.settings?.persisted_revision || payloads.gateway?.persisted_revision || '未报告',
    };
    for (const block of blocks) {
      const label = block.querySelector('span')?.textContent;
      const code = block.querySelector('code');
      if (code && Object.hasOwn(values, label)) code.textContent = String(values[label]);
    }
    const cards = [...document.querySelectorAll('#page-security .security-card')];
    const vaultCard = cards.find((card) => card.textContent.includes('登录与凭据'));
    if (vaultCard) {
      const badge = vaultCard.querySelector('.badge');
      if (badge) {
        badge.textContent = payloads.status?.vault?.enabled ? '已保护' : '未启用 Vault';
        badge.className = `badge ${payloads.status?.vault?.enabled ? 'success' : 'warning'}`;
      }
    }
  }

  async function loadLiveData() {
    if (state.loading) return;
    state.loading = true;
    setConnectionState('loading', '正在读取真实服务', state.baseUrl || defaultApiBase());
    try {
      const [status, settings, gateway, workspaces, secrets, clients] = await Promise.all([
        apiRequest('/status'),
        apiRequest('/settings'),
        apiRequest('/gateway'),
        apiRequest('/workspaces'),
        apiRequest('/secrets'),
        apiRequest('/oauth/clients'),
      ]);
      const workspaceItems = workspaces.workspace_catalog || [];
      const defaultWorkspace = workspaceItems.find((item) => item.default && item.enabled !== false) || workspaceItems.find((item) => item.enabled !== false);
      let defaultCheck = null;
      let conversations = { items: [], total: 0 };
      if (defaultWorkspace) {
        [defaultCheck, conversations] = await Promise.all([
          apiRequest(`/workspaces/${encodeURIComponent(defaultWorkspace.id)}/check`).then((value) => value.check || value).catch(() => null),
          apiRequest(`/chat/conversations?workspace_id=${encodeURIComponent(defaultWorkspace.id)}&page=1&page_size=20`).catch(() => ({ items: [], total: 0 })),
        ]);
      }

      state.payloads = { status, settings, gateway, workspaces, secrets, clients, conversations };
      state.connected = true;
      const serverCount = Object.keys(gateway.persisted?.servers || {}).length;
      const workspaceCount = workspaceItems.length;
      const exposure = gateway.active_status?.exposure_report;
      setMetric(0, serverCount, '真实工具连接');
      setMetric(1, workspaceCount, '真实工作文件夹');
      setMetric(2, conversations.total || 0, '真实会话摘要');
      setMetric(3, exposure?.catalog?.count || exposure?.direct?.count || 0, 'Runtime 可用工具');
      setHealth([
        { label: '管理服务', value: status.admin_api === false ? '不可用' : '正常', ok: status.admin_api !== false },
        { label: '工具运行时', value: status.gateway?.available ? '可用' : '未配置', ok: Boolean(status.gateway?.available) },
        { label: '凭据保护', value: status.vault?.enabled ? '已启用' : '未启用', ok: Boolean(status.vault?.enabled) },
        { label: '默认工作文件夹', value: defaultCheck?.exists && defaultCheck?.is_directory ? '可用' : '需检查', ok: Boolean(defaultCheck?.exists && defaultCheck?.is_directory) },
      ]);
      renderTasks(buildTasks(state.payloads, defaultCheck));
      renderConnections(gateway);
      renderWorkspaces(workspaces);
      renderRecords(conversations);
      updateSecurity(state.payloads);
      setConnectionState('connected', '已连接真实服务（只读）', `${state.baseUrl} · ${serverCount} 个连接 · ${workspaceCount} 个 Workspace`);
      const stripText = document.getElementById('liveStripText');
      if (stripText) stripText.textContent = '真实 Admin API 已连接 · 展示页只读 · Token 仅在内存中';
    } catch (error) {
      state.connected = false;
      setConnectionState('error', '真实服务连接失败', describeError(error));
      throw error;
    } finally {
      state.loading = false;
    }
  }

  const liveDialog = createLiveDialog();
  enhanceConceptStrip(liveDialog);
  createLivePanel(liveDialog);
  setupTaskCard(liveDialog);

  const queryApi = new URLSearchParams(location.search).get('api');
  if (queryApi) document.getElementById('liveApiBase').value = queryApi;
})();
