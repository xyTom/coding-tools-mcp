(() => {
  'use strict';

  const DEFAULT_LOCALE = 'zh-CN';
  const STORAGE_KEY = 'showcase-bwa-locale';
  const SUPPORTED = new Set(['zh-CN', 'en']);
  const originalText = new WeakMap();
  const originalAttributes = new WeakMap();
  let observer = null;
  let localePreference = document.documentElement.dataset.localePreference || 'system';
  let currentLocale = document.documentElement.dataset.locale || DEFAULT_LOCALE;

  const EN = Object.freeze({
    'Coding Tools MCP · 管理控制台设计': 'Coding Tools MCP · Admin Console Design',
    '跳到主要内容': 'Skip to main content',
    'Coding Tools MCP 管理控制台首页': 'Coding Tools MCP admin console home',
    '管理控制台': 'Admin console',
    '主要导航': 'Primary navigation',
    '概览': 'Overview',
    '工具连接': 'Tool connections',
    '工作区': 'Workspaces',
    '聊天会话': 'Conversations',
    '服务器设置': 'Server settings',
    '授权管理': 'OAuth management',
    '凭据保险箱': 'Secret Vault',
    '系统状态': 'System status',
    '当前暴露策略': 'Current exposure strategy',
    '代理模式': 'Broker mode',
    '只直接暴露固定工具与置顶工具，其余能力通过目录搜索按需调用。': 'Only fixed and pinned tools are exposed directly. Other capabilities are discovered on demand through catalog search.',
    '查看模式说明': 'View mode details',
    'MCP 管理接口': 'MCP Admin API',
    '设计展示 · 不修改正式界面': 'Design showcase · does not modify the production UI',
    '关闭导航': 'Close navigation',
    '打开导航': 'Open navigation',
    '运行状态与运维提醒': 'Runtime status and operational alerts',
    '界面语言': 'Interface language',
    '跟随系统': 'Follow system',
    '中文': 'Chinese',
    '界面主题': 'Interface theme',
    '日间模式': 'Light mode',
    '夜间模式': 'Dark mode',
    '演示数据': 'Demo data',
    '刷新当前页面': 'Refresh current page',
    '刷新': 'Refresh',
    '连接管理接口': 'Connect Admin API',
    '管理员': 'Admin',
    '专用权限': 'Dedicated privileges',
    '欢迎回来': 'Welcome back',
    '先处理影响运行的事项，再进入具体管理页面。': 'Address runtime-impacting items first, then open a management area.',
    '服务摘要': 'Service summary',
    '服务状态': 'Service status',
    '运行中': 'Running',
    '权限模式': 'Permission mode',
    '管理接口': 'Admin API',
    '运维提醒': 'Operational alerts',
    '汇总需要管理员关注的配置、权限和运行状态。': 'Summarizes configuration, permission, and runtime conditions that need administrator attention.',
    '查看全部提醒': 'View all alerts',
    '工具连接状态': 'Tool connection status',
    '持久化配置与下一次运行时的启用状态。': 'Persisted configuration and enablement for the next runtime.',
    '查看全部': 'View all',
    '最近会话': 'Recent conversations',
    '只展示摘要；选择后才读取正文。': 'Only summaries are shown; message bodies load after selection.',
    '管理接口、工具网关、凭据保险箱与配置版本。': 'Admin API, tool gateway, Secret Vault, and configuration version.',
    '查看详情': 'View details',
    '快速操作': 'Quick actions',
    '直接进入最常用的管理流程。': 'Open the most common management workflows.',
    '添加工具连接': 'Add tool connection',
    '配置本地命令或远程 MCP': 'Configure a local command or remote MCP server',
    '添加工作区': 'Add workspace',
    '明确文件访问边界': 'Define file-access boundaries',
    '查看聊天记录': 'View chat records',
    '按需读取会话详情': 'Load conversation details on demand',
    '配置授权': 'Configure OAuth',
    '客户端、授权记录与工作区': 'Clients, grants, and workspaces',
    '带版本校验的保存': 'Revision-aware saving',
    '管理上游 MCP 配置。启用、禁用和删除只影响下一次运行时或服务重启。': 'Manage upstream MCP configuration. Enable, disable, and delete actions affect only the next runtime or service restart.',
    '新增连接': 'Add connection',
    '存在等待生效的工具网关配置': 'Tool gateway changes are waiting to take effect',
    '当前运行时的工具快照不会热重载；重启服务或新建运行时后生效。': 'The current runtime tool snapshot does not hot reload. Changes apply after a service restart or a new runtime.',
    '查看影响': 'View impact',
    '持久化连接': 'Persisted connections',
    '当前运行时': 'Current runtime',
    '公开工具定义快照': 'Public tool-definition snapshot',
    '暴露策略': 'Exposure strategy',
    '代理模式适合控制上下文占用': 'Broker mode helps control context usage',
    '搜索别名、连接方式或标签': 'Search aliases, transports, or tags',
    '连接筛选': 'Connection filters',
    '全部': 'All',
    '启用': 'Enabled',
    '停用': 'Disabled',
    '直连模式': 'Direct mode',
    '代理模式与直连模式': 'Broker and Direct modes',
    '暴露方式影响客户端看到的工具目录，不改变上游能力本身。': 'Exposure mode changes the tool catalog visible to clients without changing upstream capabilities.',
    '代理模式（推荐）': 'Broker mode (recommended)',
    '只直接暴露固定工具和置顶工具，其余工具保留在可搜索目录中。': 'Expose only fixed and pinned tools directly; keep the rest in a searchable catalog.',
    '直接暴露过滤后的完整工具集合，适合工具规模较小或客户端明确需要完整列表时。': 'Expose the complete filtered tool set directly when the catalog is small or a client explicitly needs the full list.',
    '凭据策略': 'Credential policy',
    '管理台读取时遮蔽敏感值，表单编辑会保留隐藏字段。': 'Sensitive values are masked when read, and hidden fields are preserved during form editing.',
    '策略': 'Policy',
    '本地 MCP 模式': 'Local MCP mode',
    '严格安全模式': 'Strict security mode',
    '支持直接值、': 'Supports direct values, ',
    ' 和 ': ' and ',
    '；展示页不会回显凭据值。': '; the showcase never reveals credential values.',
    '保存策略': 'Save policy',
    '每个 HTTP 会话在初始化时绑定一个已校验工作区，生命周期内不可切换。': 'Each HTTP session binds to one validated workspace at initialization and cannot switch during its lifetime.',
    '检查全部': 'Check all',
    '新增工作区': 'Add workspace',
    '工作区是文件与进程隔离边界': 'A workspace is the file and process isolation boundary',
    '当前目录、保留输出、进程表、项目指令和上游工具快照都按会话独立。': 'Working directory, retained output, process table, project instructions, and upstream tool snapshots are isolated per session.',
    '列表仅加载摘要；消息与 durable context 只在明确选择会话后按页读取。': 'The list loads summaries only; messages and durable context are paged only after a conversation is explicitly selected.',
    '刷新摘要': 'Refresh summaries',
    '搜索摘要': 'Search summaries',
    '上一页': 'Previous page',
    '下一页': 'Next page',
    '选择一条会话': 'Select a conversation',
    '只有选择摘要后，才会加载消息正文和持久化上下文。': 'Message bodies and durable context load only after a summary is selected.',
    '保存必须携带最新的持久化版本；发生 409 冲突时保留草稿并提示重新比较。': 'Saves must include the latest persisted revision. On a 409 conflict, keep the draft and prompt the user to compare again.',
    '重新读取': 'Reload',
    '保存草稿': 'Save draft',
    '检测到版本冲突': 'Revision conflict detected',
    '已保留当前草稿，请重新读取服务器设置后比较差异。': 'The current draft was preserved. Reload server settings and compare the differences.',
    '网络与运行': 'Network and runtime',
    '监听地址、端口和 OAuth 公开地址修改后需要重启。': 'Changes to the listen address, port, and public OAuth URL require a restart.',
    '监听地址': 'Listen address',
    '端口': 'Port',
    '安全模式': 'Safe mode',
    '可信模式': 'Trusted mode',
    '危险模式': 'Dangerous mode',
    '命令行环境继承': 'Shell environment inheritance',
    '核心变量': 'Core variables',
    '全部变量': 'All variables',
    '不继承': 'Do not inherit',
    'OAuth 服务地址': 'OAuth server URL',
    '允许的浏览器来源（每行一个）': 'Allowed browser origins (one per line)',
    'OAuth 兼容模式（旧客户端使用受追踪的长效访问令牌）': 'OAuth compatibility mode (legacy clients use tracked long-lived access tokens)',
    '高级危险兼容设置': 'Advanced dangerous compatibility settings',
    '伪只读标注': 'Fake read-only annotations',
    '这不会隐藏工具、阻止修改操作、改变处理程序或建立安全边界，仅用于兼容错误依赖工具标注的客户端。': 'This does not hide tools, block mutations, change handlers, or create a security boundary. It only supports clients that incorrectly depend on tool annotations.',
    '下次启动启用兼容覆盖': 'Enable the compatibility override on next startup',
    '配置版本': 'Configuration version',
    '当前生效、已持久化与等待重启相互分离。': 'Active, persisted, and pending-restart states are separate.',
    '版本号': 'Revision',
    '当前生效': 'Active',
    '当前启动快照': 'Current startup snapshot',
    '已持久化': 'Persisted',
    '最新保存配置': 'Latest saved configuration',
    '等待重启': 'Pending restart',
    '这些字段已保存，但当前进程仍使用旧值。': 'These fields are saved, but the current process still uses the old values.',
    '已更新': 'Updated',
    '只显示脱敏元数据。客户端密码和工作区允许列表更新可立即生效，不修改已有授权记录或令牌的工作区。': 'Only redacted metadata is shown. Client password and workspace allowlist updates take effect immediately without changing the workspace on existing grants or tokens.',
    '客户端': 'Clients',
    '授权记录': 'Grants',
    '访问令牌': 'Access tokens',
    '刷新令牌族': 'Refresh families',
    '签名密钥': 'Signing keys',
    '审计日志': 'Audit log',
    '页面只列出凭据名称和用途，不读取、显示或复制已保存的值。': 'The page lists credential names and purposes only. Saved values are never read, displayed, or copied.',
    '已配置名称': 'Configured names',
    'OAuth、工具网关和签名材料均通过名称引用。': 'OAuth, tool gateway, and signing material are referenced by name.',
    '设置或替换': 'Set or replace',
    '提交后输入立即清空，值不会出现在响应中。': 'The input is cleared immediately after submission, and the value never appears in the response.',
    '凭据名称': 'Credential name',
    '例如 oauth/authorization-password': 'For example: oauth/authorization-password',
    '新值': 'New value',
    '显示或隐藏新值': 'Show or hide the new value',
    ' 保存后会立即替换运行中的授权页面密码。': ' replaces the active authorization-page password immediately after saving.',
    '集中查看运行时、工具网关、凭据保险箱、遥测、执行环境和重启影响。': 'View runtime, tool gateway, Secret Vault, telemetry, execution environment, and restart impact in one place.',
    '查看运行日志': 'View runtime logs',
    '刷新状态': 'Refresh status',
    '运行日志': 'Runtime logs',
    '默认不加载日志正文；需要排障时再打开查看器并筛选范围。': 'Log bodies are not loaded by default. Open the viewer and filter the scope only when troubleshooting.',
    '打开日志查看器': 'Open log viewer',
    '最近 24 小时': 'Last 24 hours',
    '条展示日志': 'demo log entries',
    '错误': 'Errors',
    '需要优先处理': 'Requires priority attention',
    '警告': 'Warnings',
    '可能影响运行': 'May affect runtime',
    '最后更新': 'Last updated',
    '点击查看器后按需读取': 'Loaded on demand after opening the viewer',
    '当前项目的管理接口尚未提供日志端点；此页面先展示交互与筛选设计，正式接入时应使用只读、分页、脱敏的日志接口。': 'The project Admin API does not yet expose a log endpoint. This page demonstrates the interaction and filtering design; production integration should use a read-only, paginated, redacted log API.',
    '遥测': 'Telemetry',
    '只报告开启、关闭或调试模式，不展示事件、路径、命令、参数或文件内容。': 'Reports only on, off, or debug mode without exposing events, paths, commands, arguments, or file contents.',
    '当前模式': 'Current mode',
    '关闭方式': 'How to disable',
    '兼容设置': 'Compatibility setting',
    '持续集成环境': 'CI environment',
    '自动关闭': 'Disabled automatically',
    '重启影响': 'Restart impact',
    '工具网关和部分服务器设置只在新建运行时或服务重启后生效。': 'Tool gateway and some server settings take effect only in a new runtime or after a service restart.',
    '工具网关连接启用状态、服务端口、允许的浏览器来源。': 'Tool gateway connection state, service port, and allowed browser origins.',
    '查看配置差异': 'View configuration differences',
    '移动端主要导航': 'Mobile primary navigation',
    '连接': 'Connections',
    '会话': 'Conversations',
    '更多': 'More',
    '连接真实管理接口': 'Connect a real Admin API',
    '只读取状态、配置摘要、工作区和会话；展示页不执行写入或删除。': 'Reads status, configuration summaries, workspaces, and conversations only. The showcase performs no writes or deletes.',
    '管理接口地址': 'Admin API URL',
    '专用管理令牌': 'Dedicated admin token',
    '显示或隐藏令牌': 'Show or hide token',
    '只保存在当前页面内存中，不写入网址、浏览器存储或日志。': 'Stored only in current page memory and never written to the URL, browser storage, or logs.',
    '取消': 'Cancel',
    '连接并读取': 'Connect and read',
    '新增 MCP 工具连接': 'Add MCP tool connection',
    '表单对应真实工具网关配置语义，保存仅在展示页本地模拟。': 'The form follows real tool-gateway configuration semantics; saving is simulated locally in the showcase.',
    '连接别名': 'Connection alias',
    '连接方式': 'Transport',
    '本地命令（标准输入输出）': 'Local command (stdio)',
    '远程网址（流式 HTTP）': 'Remote URL (Streamable HTTP)',
    '启动命令': 'Launch command',
    'npx 或 uvx': 'npx or uvx',
    '启动参数（每行一个）': 'Launch arguments (one per line)',
    '服务地址': 'Service URL',
    '工具暴露方式': 'Tool exposure mode',
    '等待时间（毫秒）': 'Timeout (milliseconds)',
    '代理模式置顶工具': 'Pinned Broker tools',
    '搜索标签': 'Search tags',
    '下次启动或新建运行时时启用': 'Enable on next startup or new runtime',
    '保存到展示草稿': 'Save to demo draft',
    '新增工作区': 'Add workspace',
    '工作区决定 HTTP 会话的文件与进程边界。': 'A workspace defines the file and process boundary for an HTTP session.',
    '标识': 'ID',
    '名称': 'Name',
    '项目代码': 'Project code',
    '根目录': 'Root',
    '设为唯一默认工作区': 'Set as the only default workspace',
    '添加到展示数据': 'Add to demo data',
    '运行日志查看器': 'Runtime log viewer',
    '日志默认关闭；打开后可按时间、级别、组件和关键词筛选。': 'Logs are closed by default. After opening, filter by time, level, component, and keyword.',
    '时间范围': 'Time range',
    '最近 15 分钟': 'Last 15 minutes',
    '最近 1 小时': 'Last hour',
    '全部展示数据': 'All demo data',
    '级别': 'Level',
    '全部级别': 'All levels',
    '组件': 'Component',
    '全部组件': 'All components',
    '关键词': 'Keyword',
    '搜索事件、消息或上下文': 'Search events, messages, or context',
    '刷新展示日志': 'Refresh demo logs',
    '筛选后的运行日志': 'Filtered runtime logs',
    '选择一条日志': 'Select a log entry',
    '点击左侧记录后查看完整消息和脱敏上下文。': 'Select an entry on the left to view the full message and redacted context.',
    '展示数据不会读取本机日志文件，也不会发送到服务器。': 'Demo data does not read local log files or send anything to the server.',
    '关闭': 'Close',
    '确认操作': 'Confirm action',
    '请确认影响范围。': 'Confirm the scope of impact.',
    '确认': 'Confirm',
    '正常': 'Healthy',
    '异常': 'Error',
    '不可用': 'Unavailable',
    '已启用': 'Enabled',
    '未启用': 'Disabled',
    '注意': 'Attention',
    '可用': 'Available',
    '需检查': 'Needs review',
    '已停用': 'Disabled',
    '真实数据 · 只读': 'Live data · read-only',
    '连接中…': 'Connecting…',
    '刚刚': 'Just now',
    '今天': 'Today',
    '昨天': 'Yesterday',
    '周二': 'Tuesday',
    '未使用': 'Never used',
    '真实配置': 'Live configuration',
    '尚未启动': 'Not started',
    '尚未检查': 'Not checked yet',
    '无摘要': 'No summary',
    '用户': 'User',
    '完整消息': 'Full message',
    '脱敏上下文': 'Redacted context',
    '时间': 'Time',
    '事件与摘要': 'Event and summary',
    '没有匹配的连接': 'No matching connections',
    '调整搜索词或筛选条件。': 'Adjust the search term or filters.',
    '没有会话摘要': 'No conversation summaries',
    '当前工作区或搜索条件没有结果。': 'No results for the current workspace or search criteria.',
    '本页没有消息正文': 'No message bodies on this page',
    '摘要存在，但当前详情页为空。': 'A summary exists, but the current detail page is empty.',
    '没有匹配的日志': 'No matching logs',
    '调整时间范围、级别、组件或关键词。': 'Adjust the time range, level, component, or keyword.',
    '没有等待重启的字段': 'No fields pending restart',
    '已同步': 'Synchronized',
    '已保存，等待重启': 'Saved, pending restart',
    '当前默认': 'Current default',
    '设为默认': 'Set as default',
    '检查路径': 'Check path',
    '位置': 'Location',
    '工作区标识': 'Workspace ID',
    '最近检查': 'Last check',
    '默认工作区': 'Default workspace',
    '复制恢复信息': 'Copy recovery reference',
    '按需详情': 'On-demand details',
    '创建时间': 'Created',
    '全局密码': 'Global password',
    '客户端专属密码': 'Client-specific password',
    '未分配工作区': 'No workspace assigned',
    '轮换专属密码': 'Rotate client password',
    '设置专属密码': 'Set client password',
    '编辑工作区权限': 'Edit workspace access',
    '活动中': 'Active',
    '删除': 'Delete',
  });

  const PATTERNS = [
    [/^第 (\d+) 页$/, (_, n) => `Page ${n}`],
    [/^(\d+) 个启用$/, (_, n) => `${n} enabled`],
    [/^(\d+) 个连接$/, (_, n) => `${n} connections`],
    [/^(\d+) 个目录$/, (_, n) => `${n} directories`],
    [/^(\d+) 个会话$/, (_, n) => `${n} conversations`],
    [/^(\d+) 个公开定义$/, (_, n) => `${n} public definitions`],
    [/^(\d+) 个公开工具定义$/, (_, n) => `${n} public tool definitions`],
    [/^(\d+) 条消息$/, (_, n) => `${n} messages`],
    [/^(\d+) 条结果$/, (_, n) => `${n} results`],
    [/^(\d+) 项等待生效$/, (_, n) => `${n} items pending`],
    [/^当前显示 (\d+) 项需要管理员关注的提醒。$/, (_, n) => `${n} operational alerts currently require administrator attention.`],
    [/^已保存 (\d+) 个会话摘要，正文可按需读取。$/, (_, n) => `${n} conversation summaries are saved; message bodies are available on demand.`],
    [/^发现 (\d+) 个会话摘要，可按需读取正文。$/, (_, n) => `Found ${n} conversation summaries; message bodies can be loaded on demand.`],
    [/^(\d+) 个启用客户端缺少工作区允许列表。$/, (_, n) => `${n} enabled clients are missing a workspace allowlist.`],
    [/^存在：(是|否) · 文件夹：(是|否)$/, (_, exists, folder) => `Exists: ${exists === '是' ? 'yes' : 'no'} · Directory: ${folder === '是' ? 'yes' : 'no'}`],
    [/^(\d+) 分钟前$/, (_, n) => `${n} minutes ago`],
    [/^(\d+) 小时前$/, (_, n) => `${n} hours ago`],
    [/^(\d+) 天前$/, (_, n) => `${n} days ago`],
    [/^今天 (.+)$/, (_, time) => `Today ${time}`],
    [/^昨天 (.+)$/, (_, time) => `Yesterday ${time}`],
    [/^周二 (.+)$/, (_, time) => `Tuesday ${time}`],
    [/^(.+) · (\d+) 条消息 · (.+)$/, (_, workspace, count, time) => `${workspace} · ${count} messages · ${time}`],
    [/^编辑 (.+)$/, (_, name) => `Edit ${name}`],
    [/^删除 (.+)$/, (_, name) => `Delete ${name}`],
    [/^下次启动禁用 (.+)$/, (_, name) => `Disable ${name} on next startup`],
    [/^下次启动启用 (.+)$/, (_, name) => `Enable ${name} on next startup`],
    [/^(.+) 已设为下次启动(启用|禁用)$/, (_, name, state) => `${name} will be ${state === '启用' ? 'enabled' : 'disabled'} on next startup`],
    [/^(.+) 检查通过$/, (_, name) => `${name} check passed`],
    [/^(.+) 检查完成$/, (_, name) => `${name} check completed`],
    [/^(.+)(已启用|已停用)$/, (_, name, state) => `${name} ${state === '已启用' ? 'enabled' : 'disabled'}`],
    [/^已允许 (.+) 访问 (.+)。$/, (_, client, workspace) => `${client} may now access ${workspace}.`],
  ];

  const FRAGMENTS = [
    ['工作区允许列表', 'workspace allowlist'],
    ['凭据保险箱', 'Secret Vault'],
    ['工具网关', 'tool gateway'],
    ['管理接口', 'Admin API'],
    ['运行时', 'runtime'],
    ['工作区', 'workspace'],
    ['授权记录', 'grant'],
    ['访问令牌', 'access token'],
    ['刷新令牌', 'refresh token'],
    ['签名密钥', 'signing key'],
    ['客户端', 'client'],
    ['会话', 'session'],
    ['凭据', 'credential'],
    ['目录', 'directory'],
    ['工具', 'tool'],
    ['服务', 'service'],
    ['配置', 'configuration'],
    ['路径', 'path'],
    ['状态', 'status'],
    ['当前', 'current'],
    ['默认', 'default'],
    ['启用', 'enable'],
    ['禁用', 'disable'],
    ['停用', 'disabled'],
    ['删除', 'delete'],
    ['保存', 'save'],
    ['读取', 'read'],
    ['检查', 'check'],
    ['刷新', 'refresh'],
    ['重启', 'restart'],
    ['等待生效', 'pending'],
    ['已更新', 'updated'],
    ['已连接', 'connected'],
    ['真实', 'live'],
    ['只读', 'read-only'],
    ['展示', 'demo'],
    ['消息', 'message'],
    ['摘要', 'summary'],
    ['错误', 'error'],
    ['警告', 'warning'],
    ['正常', 'healthy'],
    ['异常', 'error'],
    ['可用', 'available'],
    ['不可用', 'unavailable'],
    ['最近', 'recent'],
    ['时间', 'time'],
    ['创建', 'created'],
    ['名称', 'name'],
    ['操作', 'actions'],
    ['级别', 'level'],
    ['组件', 'component'],
    ['事件', 'event'],
    ['详情', 'details'],
    ['编辑', 'edit'],
    ['复制', 'copy'],
    ['关闭', 'close'],
    ['取消', 'cancel'],
    ['确认', 'confirm'],
    ['是', 'yes'],
    ['否', 'no'],
    ['条', ' entries'],
    ['个', ' '],
    ['项', ' items'],
  ];

  function normalizeLocale(locale) {
    const value = String(locale || '').toLowerCase();
    if (value.startsWith('en')) return 'en';
    if (value.startsWith('zh')) return 'zh-CN';
    return DEFAULT_LOCALE;
  }

  function detectLocale() {
    const languages = Array.isArray(navigator.languages) && navigator.languages.length
      ? navigator.languages
      : [navigator.language || DEFAULT_LOCALE];
    for (const language of languages) {
      const value = String(language).toLowerCase();
      if (value.startsWith('zh')) return 'zh-CN';
      if (value.startsWith('en')) return 'en';
    }
    return DEFAULT_LOCALE;
  }

  function translate(source, locale = currentLocale) {
    if (locale !== 'en' || !source) return source;
    const match = String(source).match(/^(\s*)([\s\S]*?)(\s*)$/);
    const leading = match?.[1] || '';
    const body = match?.[2] || '';
    const trailing = match?.[3] || '';
    if (!body) return source;
    let translated = EN[body];
    if (!translated) {
      for (const [pattern, formatter] of PATTERNS) {
        if (pattern.test(body)) {
          translated = body.replace(pattern, formatter);
          break;
        }
      }
    }
    if (!translated && /[\u3400-\u9fff]/.test(body)) {
      translated = body;
      for (const [from, to] of FRAGMENTS) translated = translated.replaceAll(from, to);
    }
    return leading + (translated || body) + trailing;
  }

  function shouldSkip(element) {
    return !element || Boolean(element.closest('script, style, pre, textarea, [data-i18n-skip], [contenteditable="true"]'));
  }

  function translateTextNode(textNode) {
    const parent = textNode.parentElement;
    if (shouldSkip(parent)) return;
    const current = textNode.nodeValue || '';
    if (currentLocale === 'zh-CN') {
      const source = originalText.get(textNode);
      if (source !== undefined && current !== source) textNode.nodeValue = source;
      return;
    }
    if (/[\u3400-\u9fff]/.test(current)) originalText.set(textNode, current);
    const source = originalText.get(textNode) ?? current;
    const next = translate(source, currentLocale);
    if (current !== next) textNode.nodeValue = next;
  }

  function translateAttributes(element) {
    if (!element || element.closest('[data-i18n-skip]')) return;
    const names = ['placeholder', 'title', 'aria-label'];
    let stored = originalAttributes.get(element);
    if (!stored) {
      stored = new Map();
      originalAttributes.set(element, stored);
    }
    for (const name of names) {
      if (!element.hasAttribute(name)) continue;
      const current = element.getAttribute(name) || '';
      if (currentLocale === 'zh-CN') {
        if (stored.has(name) && current !== stored.get(name)) element.setAttribute(name, stored.get(name));
        continue;
      }
      if (/[\u3400-\u9fff]/.test(current)) stored.set(name, current);
      const source = stored.get(name) ?? current;
      const next = translate(source, currentLocale);
      if (current !== next) element.setAttribute(name, next);
    }
  }

  function applyTranslations(root = document) {
    if (root.nodeType === Node.TEXT_NODE) {
      translateTextNode(root);
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
    if (root.nodeType === Node.ELEMENT_NODE) translateAttributes(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let textNode = walker.nextNode();
    while (textNode) {
      translateTextNode(textNode);
      textNode = walker.nextNode();
    }
    const elementRoot = root.nodeType === Node.DOCUMENT_NODE ? root.documentElement : root;
    elementRoot.querySelectorAll?.('[placeholder], [title], [aria-label]').forEach(translateAttributes);
  }

  function updatePicker() {
    const picker = document.getElementById('localePicker');
    const select = document.getElementById('localeSelect');
    if (select && select.value !== localePreference) select.value = localePreference;
    const label = currentLocale === 'en' ? 'Interface language' : '界面语言';
    if (picker && picker.title !== label) picker.title = label;
    if (select && select.getAttribute('aria-label') !== label) select.setAttribute('aria-label', label);
  }

  function setLocale(locale) {
    currentLocale = normalizeLocale(locale);
    document.documentElement.lang = currentLocale;
    document.documentElement.dataset.locale = currentLocale;
    applyTranslations(document);
    updatePicker();
    document.dispatchEvent(new CustomEvent('localechange', {
      detail: { locale: currentLocale, preference: localePreference },
    }));
    return currentLocale;
  }

  function resolvePreference(preference) {
    return preference === 'system' ? detectLocale() : normalizeLocale(preference);
  }

  function setPreference(preference, persist = true) {
    localePreference = ['system', 'zh-CN', 'en'].includes(preference) ? preference : 'system';
    document.documentElement.dataset.localePreference = localePreference;
    if (persist) {
      try { localStorage.setItem(STORAGE_KEY, localePreference); } catch { /* no-op */ }
    }
    return setLocale(resolvePreference(localePreference));
  }

  function init() {
    const select = document.getElementById('localeSelect');
    select?.addEventListener('change', (event) => setPreference(event.target.value));
    window.addEventListener('languagechange', () => {
      if (localePreference === 'system') setLocale(detectLocale());
    });
    if (!observer) {
      observer = new MutationObserver((mutations) => {
        for (const mutation of mutations) {
          if (mutation.type === 'characterData') translateTextNode(mutation.target);
          mutation.addedNodes.forEach((node) => applyTranslations(node));
          if (mutation.type === 'attributes') translateAttributes(mutation.target);
        }
        updatePicker();
      });
      observer.observe(document.documentElement, {
        childList: true,
        characterData: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['placeholder', 'title', 'aria-label'],
      });
    }
    return setPreference(localePreference, false);
  }

  window.ShowcaseI18n = Object.freeze({
    translate,
    getLocale: () => currentLocale,
    getPreference: () => localePreference,
    setLocale,
    setPreference,
    detectLocale,
  });

  init();
})();
