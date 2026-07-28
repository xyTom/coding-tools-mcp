# OAuth Agent 与 Workspace 升级说明

服务器设置现在位于稳定的用户级配置目录（Windows 为 `%APPDATA%\coding-tools-mcp`；其他系统为 `$XDG_CONFIG_HOME/coding-tools-mcp` 或 `~/.config/coding-tools-mcp`），不再跟随业务 Workspace。

首次 HTTP 启动会从旧的 `<workspace>/.coding-tools-mcp/server-settings.json` 一次性导入设置与上游配置。导入成功后请备份新目录中的 `server-settings.json`、`oauth.sqlite3` 和 `oauth-secrets.json`；不要把这些文件中的密钥材料提交到版本库。

## 推荐部署

设置独立的 `CODING_TOOLS_MCP_SECRETS_KEY`。启用后，OAuth signing key 和 refresh-token pepper 会保存到加密 Secret Vault，普通 settings 仅保留 reference。未启用 Vault 时，旧版明文 signing secret 兼容路径仍可读取；已运行的 OAuth 服务拒绝通过 WebUI 写入新的明文 signing secret。

OAuth access token 现在带有 `kid`、`client_id`、`grant_id` 和 `jti`。`oauth.sqlite3` 仅保存标识、状态和 refresh-token 哈希，不保存 bearer token 原文。正常轮换 signing key 时，旧 key 退役但仍验证未过期 token；紧急撤销会立即拒绝相关 token。

## 兼容模式

对不支持 refresh token 的旧 Agent，可在 Authentication Settings 打开 compatibility profile，或设置 `CODING_TOOLS_MCP_OAUTH_COMPATIBILITY_MODE=1`。该模式签发较长期但可追踪、可单独撤销的 access token；不要把它视为永不过期凭据。

## 回滚与恢复

不要让旧版本覆盖新的 settings 或 SQLite schema。若需回滚应用版本，保留 OAuth DB、Secret Vault 和当前/退役 signing key，直到旧 token 全部过期。紧急撤销 key、Client、Grant 或单个 token 前，应先确认会影响的远程 Agent。
