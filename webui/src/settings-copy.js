const PERMISSION_COPY = Object.freeze({
  safe: Object.freeze({
    label: '安全模式',
    description: '保留完整固定工具目录，同时限制网络、Shell 展开和高风险命令。它不会隐藏或禁用 mutation tools。',
  }),
  trusted: Object.freeze({
    label: '可信本地模式',
    description: '保留完整固定工具目录，并允许更多本机网络与 Shell 能力。仅用于受信任的本地环境。',
  }),
  dangerous: Object.freeze({
    label: '危险模式',
    description: '关闭主要命令权限门。只应在隔离容器或虚拟机中使用。',
  }),
});

const FAKE_READONLY_COPY = Object.freeze({
  label: '伪只读 annotations 兼容覆盖',
  warning: '此高级兼容选项只会向客户端伪报 readOnlyHint。它不会隐藏工具、阻止 mutation、改变 handler，或形成安全边界；并且只能配合 dangerous 模式启动。',
  enable: '通过 --dangerously-fake-readonly-annotations 或 CODING_TOOLS_MCP_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS=1 启用，然后重启服务。',
});

function permissionPresentation(mode) {
  return PERMISSION_COPY[mode] || { label: String(mode || '未知'), description: '后端返回了未知 permission mode。' };
}

function telemetryPresentation(value) {
  const mode = typeof value === 'string' ? value : value?.mode;
  if (mode === 'off' || mode === 'disabled') {
    return { mode: 'off', label: '关闭', detail: '不会发送匿名 telemetry。' };
  }
  if (mode === 'debug') {
    return { mode: 'debug', label: '调试', detail: '事件仅写入 stderr，不发送到远端。' };
  }
  if (mode === 'enabled' || mode === 'on') {
    return { mode: 'enabled', label: '启用', detail: '使用上游 v0.2.2 默认匿名 telemetry 策略。' };
  }
  return { mode: 'unknown', label: '未报告', detail: '当前 Admin 后端尚未报告 telemetry 运行模式。' };
}

const TELEMETRY_DISABLE_HELP = '设置 CODING_TOOLS_MCP_TELEMETRY=off、DO_NOT_TRACK=1，或在 CI 环境中启动；修改后重启服务。';

globalThis.McpSettingsCopy = {
  PERMISSION_COPY,
  FAKE_READONLY_COPY,
  TELEMETRY_DISABLE_HELP,
  permissionPresentation,
  telemetryPresentation,
};

export { PERMISSION_COPY, FAKE_READONLY_COPY, TELEMETRY_DISABLE_HELP, permissionPresentation, telemetryPresentation };
