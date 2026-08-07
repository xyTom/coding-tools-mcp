(() => {
  'use strict';

  let currentLocale = document.documentElement.dataset.locale || 'zh-CN';

  const EXACT = Object.freeze({
    '需要restartservice': 'Service restart required',
    'tool gateway或service器设置已save，但currentruntime仍使用旧快照。': 'Tool gateway or server settings were saved, but the current runtime still uses the old snapshot.',
    '复核workspacepath': 'Review workspace path',
    '查看workspace': 'View workspaces',
    'session存储运行healthy': 'Conversation storage is healthy',
    '查看session': 'View conversations',
    'OAuth clientconfiguration': 'OAuth client configuration',
    'tool gatewayservice': 'Tool gateway service',
    '连接healthy': 'Connected',
    'tool gatewayconfiguration、暴露策略与restart语义': 'Tool gateway configuration, exposure strategy, and restart semantics',
    '暴露方式': 'Exposure mode',
    'TOOL数量': 'TOOL COUNT',
    'RECENT使用': 'LAST USED',
    'Broker and Direct modes': 'Broker and Direct modes',
    'session的文件与进程边界': 'File and process boundaries for sessions',
    'summary列表与按需正文': 'Summary list and on-demand message bodies',
    '带版本校验的configuration管理': 'Revision-aware configuration management',
    'client、grant、令牌与signing key': 'Clients, grants, tokens, and signing keys',
    'client密钥、summary、令牌材料和内部保险箱引用均不会显示。': 'Client secrets, digests, token material, and internal Vault references are never displayed.',
    '授权登录': 'Authorization login',
    '只显示name的credential管理': 'Name-only credential management',
    'OAuth 授权全局密码': 'OAuth authorization global password',
    'github 连接credential': 'github connection credential',
    'OAuth 签名材料': 'OAuth signing material',
    'oauth/authorization-password save后会立即替换运行中的授权页面密码。': 'oauth/authorization-password replaces the active authorization-page password immediately after saving.',
    'runtime、tool gateway、Secret Vault与遥测': 'Runtime, tool gateway, Secret Vault, and telemetry',
    'Admin API · 专用管理令牌权限面': 'Admin API · dedicated admin-token privilege surface',
    'tool gatewayruntime · 48 公开tool定义': 'Tool gateway runtime · 48 public tool definitions',
    'Secret Vault · 值不会通过Admin API返回': 'Secret Vault · values are never returned by the Admin API',
    '权限模式 · 权限模式不改变固定tooldirectory': 'Permission mode · does not change the fixed tool catalog',
    'tool gateway与部分service器设置需要新建runtime或servicerestart。': 'The tool gateway and some server settings require a new runtime or service restart.',
    '本地命令': 'Local command',
    '流式 HTTP': 'Streamable HTTP',
    '标准输入输出 · uvx': 'stdio · uvx',
    '标准输入输出 · npx': 'stdio · npx',
    '下次启动enable': 'Enabled on next startup',
    '研究资料': 'Research materials',
    '导出文件': 'Export files',
    '优化用户认证流程': 'Improve user authentication flow',
    '讨论如何简化 OAuth 授权页面和clientworkspace选择。': 'Discuss simplifying the OAuth authorization page and client workspace selection.',
    '数据库查询性能分析': 'Database query performance analysis',
    '分析慢查询日志和索引优化方案。': 'Analyze slow-query logs and index optimization options.',
    'API 接口设计讨论': 'API design discussion',
    '设计新的 RESTful API 接口和 revision 冲突处理。': 'Design new RESTful API endpoints and revision-conflict handling.',
    '整理 MCP 安全边界资料': 'Organize MCP security-boundary material',
    '汇总workspace、OAuth、Secret Vault与外部沙箱的职责。': 'Summarize the responsibilities of workspaces, OAuth, the Secret Vault, and external sandboxes.',
    '工具网关运行时已完成不可变工具快照初始化。': 'The tool gateway runtime completed immutable tool-snapshot initialization.',
    '新的 HTTP MCP 会话已绑定默认工作区。': 'A new HTTP MCP session was bound to the default workspace.',
    '启用的 OAuth 客户端没有可授权的工作区允许列表。': 'The enabled OAuth client has no authorized workspace allowlist.',
    '管理员读取了当前生效、已持久化和等待重启的配置摘要。': 'An administrator read the active, persisted, and pending-restart configuration summary.',
    '工具网关凭据引用已从凭据保险箱安全解析。': 'The tool gateway credential reference was securely resolved from the Secret Vault.',
    '持久化工具网关配置已变化，当前运行时仍使用旧工具快照。': 'Persisted tool gateway configuration changed while the current runtime still uses the old tool snapshot.',
    '一个受管命令以非零退出码结束；输出已保留供后续读取。': 'A managed command exited with a non-zero code; its output was retained for later reading.',
    '管理员按稳定 jti 撤销了一个访问令牌元数据记录。': 'An administrator revoked an access-token metadata record by stable jti.',
    '工作区目录路径检查完成。': 'Workspace catalog path check completed.',
    '代理目录搜索返回匹配的上游工具定义。': 'Broker catalog search returned matching upstream tool definitions.',
    '当前 OAuth 签名密钥即将进入建议轮换窗口。': 'The current OAuth signing key is approaching the recommended rotation window.',
    'Coding Tools MCP HTTP 服务启动完成。': 'Coding Tools MCP HTTP service started successfully.',
    '管理员刷新了展示日志查看器。': 'An administrator refreshed the demo log viewer.',
  });

  const REPLACEMENTS = [
    ['session初始化后绑定该directory；currentdirectory、进程、输出与 items目指令均在此边界内独立。', 'The session binds to this directory at initialization; working directory, processes, output, and project instructions remain isolated within this boundary.'],
    ['导出文件 的recentcheck未通过。', 'The latest check for Export files failed.'],
    ['Supports direct values, secret:保险箱name 和 env:变量名; the showcase never reveals credential values.', 'Supports direct values, secret:VaultName, and env:VARIABLE_NAME; the showcase never reveals credential values.'],
    ['专用管理令牌权限面', 'dedicated admin-token privilege surface'],
    ['已完成不可变tool快照初始化', 'completed immutable tool-snapshot initialization'],
    ['已绑定defaultworkspace', 'was bound to the default workspace'],
    ['没有可授权的workspace allowlist', 'has no authorized workspace allowlist'],
    ['管理员read了current生效、已持久化和等待restart的configuration summary', 'An administrator read the active, persisted, and pending-restart configuration summary'],
    ['credential引用已从Secret Vault安全解析', 'credential reference was securely resolved from the Secret Vault'],
    ['持久化tool gatewayconfiguration已变化，currentruntime仍使用旧tool快照', 'Persisted tool gateway configuration changed while the current runtime still uses the old tool snapshot'],
    ['受管命令以非零退出码结束', 'managed command exited with a non-zero code'],
    ['输出已保留供后续read', 'output was retained for later reading'],
    ['管理员按稳定', 'an administrator used stable'],
    ['撤销了一', 'revoked one'],
    ['元数据记录', 'metadata record'],
    ['搜索返回匹配的上游tool定义', 'search returned matching upstream tool definitions'],
    ['即将进入建议轮换窗口', 'is approaching the recommended rotation window'],
    ['公开tool定义', 'public tool definitions'],
    ['公开定义', 'public definitions'],
    ['权限模式', 'Permission mode'],
    ['代理模式', 'Broker mode'],
    ['连接healthy', 'Connected'],
    ['流式', 'Streamable'],
    ['标准输入输出', 'stdio'],
    ['授权页面和', 'authorization page and '],
    ['接口设计讨论', 'API design discussion'],
    ['接口和', 'endpoints and '],
    ['冲突处理', 'conflict handling'],
    ['安全边界资料', 'security-boundary material'],
    ['与外部沙箱的职责', 'and external sandbox responsibilities'],
    ['带版本校验的', 'revision-aware '],
    ['令牌材料和内部保险箱引用均不会显示', 'token material and internal Vault references are never displayed'],
    ['授权全局密码', 'authorization global password'],
    ['签名材料', 'signing material'],
    ['后会立即替换运行中的授权页面密码', 'replaces the active authorization-page password immediately after saving'],
    ['与遥测', 'and telemetry'],
    ['值不会通过', 'values are never returned by '],
    ['器设置需要新建', ' settings require a new '],
    ['仍使用旧快照', 'still uses the old snapshot'],
    ['未通过', 'failed'],
    ['复核', 'Review '],
    ['查看', 'View '],
    ['存储运行', 'storage is '],
    ['可恢复', 'recoverable'],
    ['总', 'total '],
    ['数量', 'count'],
    ['使用', 'used'],
    ['下次启动', 'on next startup'],
    ['保险箱', 'Vault'],
    ['变量名', 'VARIABLE_NAME'],
    ['研究资料', 'Research materials'],
    ['导出文件', 'Export files'],
  ];

  const PATTERNS = [
    [/^(\d+) enable中$/, (_, n) => `${n} enabled`],
    [/^(\d+) 代理模式$/, (_, n) => `${n} Broker`],
    [/^(\d+) 需check$/, (_, n) => `${n} need review`],
    [/^(\d+) 可恢复$/, (_, n) => `${n} recoverable`],
    [/^(\d+) 总message数$/, (_, n) => `${n} total messages`],
    [/^(\d+) 公开定义$/, (_, n) => `${n} public definitions`],
    [/^(\d+) 代理 \/ (\d+) 直连$/, (_, broker, direct) => `${broker} Broker / ${direct} Direct`],
    [/^(.+) 的recentcheck未通过。$/, (_, name) => `The latest check for ${name} failed.`],
  ];

  function translateText(value) {
    if (currentLocale !== 'en' || !/[\u3400-\u9fff]/.test(value)) return value;
    if (EXACT[value]) return EXACT[value];
    for (const [pattern, formatter] of PATTERNS) {
      if (pattern.test(value)) return value.replace(pattern, formatter);
    }
    let next = value;
    for (const [from, to] of REPLACEMENTS) next = next.replaceAll(from, to);
    return next;
  }

  function shouldSkip(element) {
    return !element || Boolean(element.closest('script, style, pre, textarea, [data-i18n-skip], [contenteditable="true"]'));
  }

  function translateNode(textNode) {
    if (currentLocale !== 'en' || shouldSkip(textNode.parentElement)) return;
    const current = textNode.nodeValue || '';
    const next = translateText(current);
    if (next !== current) textNode.nodeValue = next;
  }

  function translateAttributes(element) {
    if (currentLocale !== 'en' || !element || element.closest('[data-i18n-skip]')) return;
    for (const name of ['placeholder', 'title', 'aria-label']) {
      if (!element.hasAttribute(name)) continue;
      const current = element.getAttribute(name) || '';
      const next = translateText(current);
      if (next !== current) element.setAttribute(name, next);
    }
  }

  function apply(root = document) {
    if (currentLocale !== 'en') return;
    if (root.nodeType === Node.TEXT_NODE) return translateNode(root);
    if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
    if (root.nodeType === Node.ELEMENT_NODE) translateAttributes(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let textNode = walker.nextNode();
    while (textNode) {
      translateNode(textNode);
      textNode = walker.nextNode();
    }
    const elementRoot = root.nodeType === Node.DOCUMENT_NODE ? root.documentElement : root;
    elementRoot.querySelectorAll?.('[placeholder], [title], [aria-label]').forEach(translateAttributes);
  }

  document.addEventListener('localechange', (event) => {
    currentLocale = event.detail?.locale || document.documentElement.dataset.locale || 'zh-CN';
    if (currentLocale === 'en') queueMicrotask(() => apply(document));
  });

  const observer = new MutationObserver((mutations) => {
    if (currentLocale !== 'en') return;
    for (const mutation of mutations) {
      if (mutation.type === 'characterData') translateNode(mutation.target);
      mutation.addedNodes.forEach((node) => apply(node));
      if (mutation.type === 'attributes') translateAttributes(mutation.target);
    }
  });
  observer.observe(document.documentElement, {
    childList: true,
    characterData: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['placeholder', 'title', 'aria-label'],
  });

  apply(document);
})();
