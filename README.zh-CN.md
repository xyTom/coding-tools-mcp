# Coding Tools MCP 中文版

Coding Tools MCP 是一个面向 coding agent 的 MCP 服务。当前版本已经不只是本地代码工具集合，而是一个带上游 MCP 网关、管理工具、secret 管理和同端口 Web 管理台的 MCP 管理平台。

核心链路如下：

```text
远端 MCP Client
  -> coding-tools-mcp
      -> 本地代码工具
      -> 已注册的本地/远端 MCP 工具
      -> 管理工具，仅管理员可见
      -> Web 管理台，HTTP 模式默认在同端口 /admin 启动
```

## 当前可操作内容

### 本地代码工具

默认可用的本地工具包括：

- `server_info`：查看服务、workspace、权限模式、上游 MCP 状态。
- `get_default_cwd` / `set_default_cwd`：查看或设置当前会话的默认目录。
- `read_file` / `list_dir` / `list_files` / `search_text`：读取、列目录、按 glob 列文件、全文搜索。
- `apply_patch`：以结构化 patch 修改文件。
- `exec_command` / `write_stdin` / `kill_session`：运行命令、向长进程写入 stdin、结束会话。
- `git_status` / `git_diff` / `git_log` / `git_show` / `git_blame`：查看 Git 状态和历史。
- `view_image`：查看 workspace 内图片。
- `request_permissions`：为受控命令能力请求权限。

这些工具仍然受 workspace 边界保护：路径默认相对 workspace，禁止 `..` 逃逸、绝对路径逃逸和不安全符号链接逃逸。

### MCP 网关

可以把其他 MCP server 注册为上游，然后由 coding-tools-mcp 统一暴露。上游工具会自动加命名空间：

```text
github__search_repositories
filesystem__read_file
browser__open
```

支持两类上游：

- `streamable_http`：远端或本机 HTTP MCP endpoint。
- `stdio`：本地 stdio MCP 进程。

每个上游支持 `include_tools` 和 `exclude_tools` 工具白名单/黑名单，建议远端场景优先用白名单。

### MCP 管理工具

管理工具默认不出现在普通 `tools/list` 中，只有管理员 token 或 OAuth admin scope 可见。

当前管理工具：

- `mcp_catalog_list`：查看已配置的上游 MCP 和运行状态。
- `mcp_server_plan`：生成安装/更新计划，只 dry-run，不写配置。
- `mcp_server_install`：安装上游 MCP，`apply=true` 时才写配置。
- `mcp_server_update`：更新上游 MCP 配置。
- `mcp_server_enable` / `mcp_server_disable`：启用或禁用上游 MCP。
- `mcp_server_remove`：删除上游 MCP 配置。
- `mcp_server_reload`：重新加载上游 MCP。
- `mcp_server_logs`：查看上游 MCP 进程日志。
- `mcp_secret_set` / `mcp_secret_list` / `mcp_secret_delete`：管理本地 secret vault。

### Secret 管理

配置里可以引用环境变量，也可以引用本地加密 secret vault：

```json
{
  "env": {
    "TOKEN": {"secret_ref": "github_token"},
    "CACHE_DIR": {"env_ref": "CACHE_DIR"}
  }
}
```

如果要保存真实 token，必须设置服务端 master key：

```bash
export CODING_TOOLS_MCP_SECRETS_KEY="replace-with-a-long-random-key"
```

没有这个 key 时，不允许通过 Web UI 或管理工具保存真实 secret；此时只能在配置中使用 `authorization_env` 或 `env_ref` 这类环境变量引用。

### Web 管理台

HTTP 模式默认启用中文 Web 管理台，并和 MCP 接口共用同一个端口：

```bash
python -m coding_tools_mcp \
  --workspace G:/LLM \
  --host 127.0.0.1 \
  --port 8765
```

访问：

```text
MCP 接口：http://127.0.0.1:8765/mcp
管理台：http://127.0.0.1:8765/admin
OAuth 页面：http://127.0.0.1:8765/oauth/authorize
```

个人远程使用建议开启 OAuth：

```bash
uvx coding-tools-mcp --host 0.0.0.0 --port 8765 --workspace G:/LLM --oauth-mode
```

管理台登录会申请 `scope=admin`。普通 `scope=mcp` 的 OAuth token 只能访问 `/mcp`，不能调用 `/api/admin/*`。如果只想关闭管理台，使用 `--no-admin-ui` 或 `CODING_TOOLS_MCP_ADMIN_UI=0`。旧的 `--admin-ui --admin-port 8766` 独立管理台仍保留兼容，但不再是默认方式。

管理台包含总览、MCP 管理、添加 MCP、认证设置、目录与会话、实时使用和高级 JSON。添加 MCP 支持常用模板、向导表单和高级 JSON 双模式；当前不会安装 skills，skills 安装属于后续高危能力，需要单独设计。

### 多会话默认目录隔离

HTTP MCP 会话会分配独立 `Mcp-Session-Id`。同一个服务面对多个 agent 或多个会话时，每个会话都可以设置自己的默认目录：

```text
会话 A: set_default_cwd -> project-a
会话 B: set_default_cwd -> project-b
```

之后两个会话都调用 `read_file` 的相对路径时，会分别从自己的默认目录解析，避免一个 agent 的 cwd 状态影响另一个 agent。

浏览器或自定义 HTTP 客户端也可以传：

```text
X-Coding-Tools-Session: your-session-id
```

## 快速启动

### 从当前仓库启动 HTTP MCP

```bash
python -m coding_tools_mcp --workspace G:/LLM --host 127.0.0.1 --port 8765
```

MCP endpoint：

```text
http://127.0.0.1:8765/mcp
```

Web 管理台同端口默认可用：

```text
http://127.0.0.1:8765/admin
```

如果要按个人远程管理方式启动，并使用 OAuth 登录管理台：

```bash
uvx coding-tools-mcp --host 0.0.0.0 --port 8765 --workspace G:/LLM --oauth-mode
```

### 使用 make 启动

```bash
make start MCP_WORKSPACE=G:/LLM MCP_PORT=8765
```

### 使用 stdio 模式

适合 Claude Code、Cursor 等本地 MCP 客户端：

```bash
python -m coding_tools_mcp --stdio --workspace G:/LLM
```

通用 MCP client 配置示例：

```json
{
  "mcpServers": {
    "coding-tools": {
      "command": "python",
      "args": ["-m", "coding_tools_mcp", "--stdio", "--workspace", "G:/LLM"]
    }
  }
}
```

### 远端访问建议

远端 MCP client 访问时，不建议直接公开无鉴权服务。至少使用 bearer token 或 OAuth：

```bash
python -m coding_tools_mcp \
  --workspace G:/LLM \
  --host 127.0.0.1 \
  --port 8765 \
  --auth-token "replace-with-user-token"
```

远端只读场景建议启用只读 profile：

```bash
CODING_TOOLS_MCP_TOOL_PROFILE=read-only \
python -m coding_tools_mcp --workspace G:/LLM --host 127.0.0.1 --port 8765
```

## 持久化、Token 与隧道兼容性

和原版本相比，当前版本仍然保留安装脚本生成 token 和 cloudflared 隧道能力，同时新增了 MCP 管理配置持久化。

会持久化的内容：

- 通过 `scripts/install.sh` 安装的 `coding-tools-mcp` 命令会持久安装到用户环境，具体由 `uv tool install` 或 `pip install --user` 完成。
- 上游 MCP 配置写入 `mcp-servers.json`。路径优先级是 `--upstream-config`、`CODING_TOOLS_MCP_UPSTREAM_CONFIG`、`--config-dir/mcp-servers.json`、`CODING_TOOLS_MCP_CONFIG_DIR/mcp-servers.json`、`<workspace>/.coding-tools-mcp/mcp-servers.json`。
- 启动级设置写入同一配置目录的 `server-settings.json`，包括 host、port、workspace、OAuth issuer/server URL、OAuth token secret、权限模式和 shell 环境继承策略。
- 管理操作会写入同目录审计日志，例如 `mcp-servers.json.audit.jsonl`。
- 设置 `CODING_TOOLS_MCP_SECRETS_KEY` 后，`mcp_secret_set` 会把 secret 加密写入同目录 vault，例如 `mcp-servers.json.secrets.json`。

不会自动持久化的内容：

- 脚本自动生成的 bearer token 默认只在本次启动/隧道进程中打印和使用，不会自动保存到磁盘。管理台里手动保存到 `server-settings.json` 的 token 是明文，适合个人本机使用，必须把该文件当作敏感文件。
- `cloudflared tunnel --url` 生成的是临时 URL，重启后通常会变化。
- OAuth 模式会在首次启动时把自动生成的 `oauth_token_secret` 保存到 `server-settings.json`，也可以用 `CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET` 手动指定。OAuth token 同时绑定 server URL；如果 Cloudflare 临时隧道地址变了，旧 token 仍会失效。要跨隧道重启复用授权，请使用固定域名并设置 `CODING_TOOLS_MCP_SERVER_URL` 或管理台里的 OAuth server URL。

在线立即生效：`auth-token`、`admin-token`、OAuth 授权密码、默认目录、终止运行会话、secret 管理、MCP 配置保存和上游 reload。需要重启：host、port、workspace 根目录、OAuth issuer/server URL、OAuth token secret、权限模式和 shell 环境继承策略。

如果希望 token 跨重启稳定，自己固定配置：

```bash
export CODING_TOOLS_MCP_AUTH_TOKEN="replace-with-stable-token"
export CODING_TOOLS_MCP_ADMIN_TOKEN="replace-with-stable-admin-token"
```

如果希望 OAuth token 跨重启稳定，固定签名 key 和稳定公网地址：

```bash
export CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET="hex-encoded-32-bytes"
export CODING_TOOLS_MCP_SERVER_URL="https://mcp.example.com"
```

cloudflared 隧道仍然可用：

```bash
scripts/tunnel.sh cloudflared G:/LLM
```

或一条命令安装并启动 cloudflared 隧道：

```bash
scripts/install.sh --tunnel cloudflared --auto-install-tunnel --workspace G:/LLM
```

隧道模式默认：

- `CODING_TOOLS_MCP_TOOL_PROFILE=read-only`
- `CODING_TOOLS_MCP_AUTH_MODE=bearer`
- 如果没有传 `CODING_TOOLS_MCP_AUTH_TOKEN`，脚本会自动生成 bearer token 并打印。

如果想在隧道模式下同时启用 MCP 管理平台持久化，可以用环境变量传入管理配置和 admin token：

```bash
export CODING_TOOLS_MCP_UPSTREAM_CONFIG="G:/LLM/coding-tools-mcp/mcp-servers.json"
export CODING_TOOLS_MCP_ADMIN_TOKEN="replace-with-admin-token"
export CODING_TOOLS_MCP_SECRETS_KEY="replace-with-a-long-random-key"

scripts/tunnel.sh cloudflared G:/LLM
```

隧道会暴露同一 HTTP origin，因此 `/admin` 也会跟随 `/mcp` 暴露。公网隧道场景请使用 bearer token 或 OAuth，并优先使用 `read-only` profile；如果不需要管理台，设置 `CODING_TOOLS_MCP_ADMIN_UI=0` 或传 `--no-admin-ui` 关闭。

## 上游 MCP 配置教程

### 1. 准备配置文件

默认会使用 `G:/LLM/.coding-tools-mcp/mcp-servers.json`。也可以用 `--config-dir` 或 `--upstream-config` 改位置。例如：

```json
{
  "servers": {
    "filesystem": {
      "transport": "stdio",
      "command": "uvx",
      "args": ["mcp-server-filesystem", "G:/LLM"],
      "env": {
        "TOKEN": {"secret_ref": "github_token"}
      },
      "include_tools": ["read_file", "list_directory"],
      "enabled": true
    },
    "browser": {
      "transport": "streamable_http",
      "url": "http://127.0.0.1:9000/mcp",
      "authorization_env": "BROWSER_MCP_TOKEN",
      "include_tools": ["open", "screenshot"],
      "enabled": true
    }
  }
}
```

启动时加载：

```bash
python -m coding_tools_mcp \
  --workspace G:/LLM \
  --host 127.0.0.1 \
  --port 8765 \
  --config-dir G:/LLM/.coding-tools-mcp
```

### 2. 调用上游工具

如果上游 alias 是 `filesystem`，它的 `read_file` 会暴露为：

```text
filesystem__read_file
```

普通 MCP `tools/call` 示例：

```json
{
  "name": "filesystem__read_file",
  "arguments": {
    "path": "README.md"
  }
}
```

### 3. 结构化安装，不接受任意 shell

本地 stdio MCP 安装必须使用结构化字段：

```json
{
  "alias": "filesystem",
  "transport": "stdio",
  "command": "uvx",
  "args": ["mcp-server-filesystem", "G:/LLM"],
  "env": {
    "TOKEN": {"secret_ref": "github_token"}
  },
  "include_tools": ["read_file"]
}
```

安全限制：

- `command` 和 `args` 必须分开。
- 禁止 `cmd`、`powershell`、`bash`、`sh` 等 shell 作为 stdio command。
- 禁止管道、重定向、`;`、反引号、`$()`、`${}` 等 shell 片段。
- 安装、更新、删除默认都是 dry-run；只有 `apply=true` 才会写配置。

## 管理工具使用教程

### 1. 启动带 admin token 的服务

```bash
python -m coding_tools_mcp \
  --workspace G:/LLM \
  --host 127.0.0.1 \
  --port 8765 \
  --auth-token "replace-with-user-token" \
  --admin-token "replace-with-admin-token" \
  --upstream-config G:/LLM/coding-tools-mcp/mcp-servers.json
```

普通 token 只能看到普通工具；admin token 才能看到 `mcp_*` 管理工具。

### 2. 先生成安装计划

调用 `mcp_server_plan`：

```json
{
  "config": {
    "alias": "filesystem",
    "transport": "stdio",
    "command": "uvx",
    "args": ["mcp-server-filesystem", "G:/LLM"],
    "include_tools": ["read_file"]
  }
}
```

返回里会包含：

- `dry_run: true`
- `apply_required: true`
- `changes`

### 3. 确认无误后安装

调用 `mcp_server_install`，加上 `apply=true`：

```json
{
  "config": {
    "alias": "filesystem",
    "transport": "stdio",
    "command": "uvx",
    "args": ["mcp-server-filesystem", "G:/LLM"],
    "include_tools": ["read_file"]
  },
  "apply": true
}
```

### 4. 重新加载上游 MCP

```json
{
  "name": "mcp_server_reload",
  "arguments": {}
}
```

重新加载后，普通工具列表里会出现带命名空间的上游工具。

### 5. 禁用、启用、删除

禁用：

```json
{"alias": "filesystem", "apply": true}
```

对应工具名：

- `mcp_server_disable`
- `mcp_server_enable`
- `mcp_server_remove`

### 6. 查看日志

```json
{
  "alias": "filesystem",
  "max_lines": 100
}
```

对应工具名：

```text
mcp_server_logs
```

## Secret 使用教程

### 1. 开启 secret vault

```bash
export CODING_TOOLS_MCP_SECRETS_KEY="replace-with-a-long-random-key"
```

Windows PowerShell：

```powershell
$env:CODING_TOOLS_MCP_SECRETS_KEY = "replace-with-a-long-random-key"
```

### 2. 保存 secret

调用 `mcp_secret_set`：

```json
{
  "name": "github_token",
  "value": "ghp_xxx"
}
```

### 3. 在 MCP 配置里引用

```json
{
  "env": {
    "GITHUB_TOKEN": {"secret_ref": "github_token"}
  }
}
```

### 4. 查看和删除

```text
mcp_secret_list
mcp_secret_delete
```

配置、审计日志、secret vault 会落在 `--upstream-config` 对应文件旁边：

```text
mcp-servers.json
mcp-servers.json.audit.jsonl
mcp-servers.json.secrets.json
```

审计日志会脱敏敏感字段。

## Web 管理台教程

启动：

```bash
python -m coding_tools_mcp \
  --workspace G:/LLM \
  --host 127.0.0.1 \
  --port 8765 \
  --admin-token "replace-with-admin-token"
```

打开：

```text
http://127.0.0.1:8765/admin
```

页面可做的事：

- 输入 admin bearer token。
- 查看已管理的 MCP server。
- 使用模板和向导表单添加 stdio 或 HTTP MCP，也可以切到高级 JSON。
- 先“预览变更”，再“保存并重载”。
- 启用、禁用、删除 server。
- 重新加载上游。
- 设置或删除 secrets。
- 在线替换 token、切换默认目录、查看或终止运行会话。

默认配置文件是 `G:/LLM/.coding-tools-mcp/mcp-servers.json`，启动设置文件是同目录的 `server-settings.json`。通过 `--config-dir` 可以把两者放到自定义目录；通过 `--upstream-config` 可以只指定 MCP server 配置文件。

Web 管理台适合个人管理。非 loopback 绑定必须配置 bearer token 或 OAuth；远程打开建议使用 `--oauth-mode`，管理台会申请 `scope=admin`。

## 聊天会话同步

客户端可以通过 `record_chat_transcript` 或 `record_chat_message` 把 Codex / ChatGPT 会话文本同步到本地 transcript 库。写入时应使用稳定的 `conversation_id` 和 `message_id`，这样重复上传会去重。之后可用 `list_chat_projects` 和 `list_chat_conversations` 查找已保存的项目与会话，再用 `recall_chat_context` 或 `recall_project_context` 把消息和 Markdown 上下文取回到新的客户端会话里。

这个机制是“客户端主动提交的会话库”，不会自动读取外部账号或 Codex 原生私有会话文件。

## 权限和安全边界

默认安全策略：

- HTTP 模式默认启用 `/admin`；stdio 模式不启动 Web UI。
- 普通 token 不能安装 MCP。
- admin token 独立于普通 token。
- 管理工具不出现在普通 `tools/list`。
- 安装必须 dry-run 生成计划，`apply=true` 才生效。
- 本地 stdio MCP 使用结构化 `command` / `args`，不接受任意 shell 字符串。
- 每个 MCP 可以配置工具白名单。
- token 和 secret 在 catalog、日志、审计里脱敏。
- 非 loopback 绑定必须配置 bearer token 或 OAuth，避免无鉴权公开管理台。

权限模式：

- `safe`：默认模式，限制网络、shell 展开、inline script、危险命令等。
- `trusted`：适合本地开发，会放开部分开发便利能力，但仍保留敏感环境变量过滤和危险命令检查。
- `dangerous`：关闭 `exec_command` 的权限门，仅建议在隔离容器或 VM 里使用。

## 当前不包含的内容

当前版本不直接安装远端客户端自己的 Skills 系统。可以后续做两类扩展：

- 管理本服务自己的 skill packs，并暴露成 MCP resources、prompts 或 tools。
- 为本机 Codex 环境安装 skills，但这应作为高危管理员能力单独设计，要求来源白名单、版本锁定和审计日志。

## 测试

常用测试：

```bash
python -m unittest tests.compliance.test_mcp_admin
python -m unittest tests.compliance.test_upstream_gateway
python -m unittest tests.compliance.test_schema_drift tests.compliance.test_mcp_contract
```

本次 MCP 管理平台相关回归建议：

```bash
python -m unittest \
  tests.compliance.test_schema_drift \
  tests.compliance.test_mcp_contract \
  tests.compliance.test_upstream_gateway \
  tests.compliance.test_mcp_admin \
  tests.compliance.test_runtime_helpers.RuntimeHelperTests.test_default_cwd_is_isolated_per_tool_session
```

语法检查：

```bash
python -m py_compile \
  coding_tools_mcp/server.py \
  coding_tools_mcp/upstream.py \
  coding_tools_mcp/admin.py \
  coding_tools_mcp/secret_vault.py
```

## 相关英文文档

- [Quickstart](docs/quickstart.md)
- [MCP client configuration](docs/mcp-client-config.md)
- [Browser chat clients](docs/browser-clients.md)
- [Remote MCP](docs/remote-mcp.md)
- [Tools and schemas](docs/tools-and-schemas.md)
- [Permission modes](docs/permission-modes.md)
- [Security policy](SECURITY.md)
- [Security boundary](docs/security-boundary.md)
- [CI and test commands](docs/ci-and-tests.md)
