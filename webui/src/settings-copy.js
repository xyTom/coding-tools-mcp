export const SAFETY_PRESETS = {
  view: { permission_mode: 'safe', tool_profile: 'read-only', label: '仅查看', help: '只开放检查和读取工具。' },
  safe_edit: { permission_mode: 'safe', tool_profile: 'full', label: '安全编辑（推荐）', help: '可编辑工作区，同时限制网络和高风险命令。' },
  trusted: { permission_mode: 'trusted', tool_profile: 'full', label: '可信本地开发', help: '允许网络、Shell 展开和内联脚本。' },
  dangerous: { permission_mode: 'dangerous', tool_profile: 'full', label: '隔离环境完全权限', help: '仅适用于容器或虚拟机，会关闭主要命令权限门。' },
};

export function presetFor(settings = {}) {
  return Object.entries(SAFETY_PRESETS).find(([, preset]) => preset.permission_mode === settings.permission_mode && preset.tool_profile === settings.tool_profile)?.[0] || 'custom';
}

export function hostMode(host = '') {
  if (host === '127.0.0.1' || host === 'localhost') return 'local';
  if (host === '0.0.0.0' || host === '::') return 'lan';
  return 'custom';
}
