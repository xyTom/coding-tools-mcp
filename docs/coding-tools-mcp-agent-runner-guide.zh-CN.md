# Coding Tools MCP Agent 工作台与远程 Runner 用户指南

> 需要可点击跳转的页面时，请先启动 HTTP 服务，再打开
> `http://127.0.0.1:8765/wiki`。本 Markdown 仅作为仓库内的备用源文档。

这份指南面向需要通过浏览器操作 Agent、管理多个 Workspace，或把执行放到远程机器上的用户。如果只想把 Coding Tools MCP 接入 Claude、Codex、Cursor 等 MCP 客户端，请直接阅读[快速开始](quickstart.md)。

## 先认识三个入口

| 入口 | 用途 | 使用哪种凭据 |
| --- | --- | --- |
| `/mcp` | ChatGPT、Codex、Claude、Cursor 等 MCP 客户端入口 | 普通 bearer 或 OAuth token |
| `/admin` | 会话中心以及 Workspace、OAuth、Gateway、Secret 和 Runner 管理 | 独立的 Admin token |

普通 bearer/OAuth token 不能调用 Admin API。请为两种角色使用不同的 token。

### 会话中心边界

- `/admin` 是唯一 WebUI 管理入口。
- 执行主机必须安装 Codex CLI，并已完成 Codex 登录；Runner Workspace 要在对应 Runner 主机完成。
- Agent Session 元数据由 Coding Tools MCP SQLite 保存；模型 thread 与对话连续性由 Codex thread store 保存，两者共同提供恢复能力。

## 本地启动

要求 Python 3.11 或更高版本。安装后，为普通用户和管理员分别配置 token，再启动 HTTP 服务。下面的示例固定使用端口 `8765`：

```powershell
$env:CODING_TOOLS_MCP_AUTH_TOKEN = "<operator-token>"
$env:CODING_TOOLS_MCP_ADMIN_TOKEN = "<different-admin-token>"
coding-tools-mcp --workspace "G:\path\to\repo" --host 127.0.0.1 --port 8765
```

在 macOS 或 Linux 上使用对应 shell 的环境变量语法，并把 Workspace 换成实际路径。不要把真实 token 写入仓库、URL、二维码或聊天记录。

启动后可打开：

- Admin WebUI：`http://127.0.0.1:8765/admin`
- MCP endpoint：`http://127.0.0.1:8765/mcp`

如果只在本机使用，保持 loopback 绑定即可。需要从手机或另一台电脑访问时，请通过有认证的 HTTPS tunnel 发布；不要把 `noauth` 服务直接暴露到公网。

## 使用会话中心

1. 打开 `/admin`，使用专用 Admin token 认证。
2. 选择 Workspace，进入 **会话中心**。
3. 创建 Conversation，启动 Agent execution 并发送 turn。
4. Agent 请求权限时，检查具体命令和影响范围，再批准或拒绝。
5. 需要确认仓库状态时运行结构化 Validation。
6. 交接前查看 continuation 或 handoff 证据。

Conversation 和执行元数据都是持久化对象。刷新或关闭浏览器不会自动删除它们；重新打开 execution 会尝试恢复后端。MCP 身份丢失时使用显式 `conversation_list` 和 `conversation_resume`，不要重复提交上一条可能产生写入的消息。

## 管理 Workspace

本地 Workspace 可以在 `/admin` 的“工作区”页面添加。每个 Session 创建后会固定绑定一个 Workspace，不能在 Session 中途换根目录。

Runner Workspace 在服务设置中使用下列字段；`root` 是 Runner 本机理解的路径或命名空间，Control Plane 不会在自己的文件系统上解析它：

```json
{
  "id": "chem-project",
  "name": "Chem project on lab workstation",
  "root": "G:\\Research\\chem-project",
  "target": "runner",
  "runner_id": "lab-win-01",
  "enabled": true,
  "default": false
}
```

`id` 必须与 Runner 启动命令中的 Workspace ID 相同，`runner_id` 必须与已签发 credential 的 Runner ID 相同。远程 Workspace 不可用时系统会返回可重试错误，不会悄悄改用 Control Plane 的本地目录。

## 连接远程 Runner

Runner 适合以下情况：代码只存在于实验室工作站、需要使用该机器上的编译器/仪器配套软件，或不希望 Control Plane 直接挂载远程目录。

1. 使用 Admin API `POST /admin/api/runners/{runner_id}/credential` 签发或轮换 Runner credential。明文只在这次响应中返回一次。
2. 把 credential 放入 Runner 机器的 `CODING_TOOLS_MCP_RUNNER_CREDENTIAL`，或写入权限受限的文件并使用 `--credential-file`。
3. 在 Control Plane 的 Workspace catalog 中添加与 Runner 广播一致的 `id`、`runner_id` 和 `root`。
4. 在 Runner 机器上启动：

```powershell
$env:CODING_TOOLS_MCP_RUNNER_CREDENTIAL = "<issued-once-credential>"
coding-tools-mcp-runner `
  --server "wss://control.example/runner/ws" `
  --runner-id "lab-win-01" `
  --workspace "chem-project=G:\Research\chem-project"
```

生产环境必须使用 `wss://`。只有本机开发或 tunnel 的 loopback hop 才能显式使用：

```powershell
coding-tools-mcp-runner --allow-insecure-ws --server "ws://127.0.0.1:8765/runner/ws" ...
```

即使带 `--allow-insecure-ws`，非 loopback 的 `ws://` 也会被拒绝。Runner 需要自己的 upstream MCP 时使用 `--upstream-config <path>`；该配置在 Runner 本地加载，每个 Runtime 的已发布工具目录仍保持不可变。

## 常见状态与处理

| 状态或现象 | 含义 | 建议操作 |
| --- | --- | --- |
| `http_session_capacity` | 全局 Session 容量已满 | 关闭确定不用的 Session，按 `Retry-After` 重试新的 initialize |
| `http_session_identity_quota` | 当前身份达到配额 | 清理该身份的旧 Session；不要跨身份复用 Session ID |
| `http_session_initialization_limit` | 同时初始化过多 | 等待并只重试新的 initialize |
| `UPSTREAM_BACKING_OFF` | upstream 发生 5xx、timeout 或断线 | 等待退避窗口；修复 upstream，不要自动重放可能写入的 tool call |
| 404/410 stale session | 远端 Session 已失效 | 客户端应清除旧 Session ID，再初始化新 Session |
| Runner unavailable | 原 Runner 离线 | 恢复同一个 Runner；不会本地 fallback |
| `close_pending` | 离线期间已记录关闭意图 | 让原 Runner 重连并完成 reconciliation，不要反复发送关闭请求 |
| Validation unavailable | Runner/本机缺少 recipe 所需工具链 | 安装或配置工具链后重试；Validation 不会自动安装 |

更完整的诊断步骤见 [Runner 与 Session 故障排查](webcodex-runner-troubleshooting.md)。

## 安全与运维清单

- MCP principal、Admin、Runner 三类 credential 必须分开。
- token 不放进 URL、二维码、命令行参数、Git 或截图；优先使用环境变量、Secret Vault 或受保护的 credential file。
- 公网只使用 HTTPS/WSS，并在 tunnel 或反向代理处保留认证。
- 不要把 `permission_mode=dangerous` 当作沙箱；仅在外部隔离的容器或 VM 中使用。
- 升级前停止或静默服务，并备份 Settings、Vault、OAuth、Agent Session 与 Transcript 数据库。
- SQLite 正在写入时使用 SQLite-aware backup，不要直接复制活动数据库文件。

升级、回滚和数据备份步骤见 [Coding Tools MCP Runtime Platform Migration and Rollback](webcodex-migration-rollback.md)。Admin 的详细接口见 [Admin API](admin-api.md)，MCP 客户端配置见 [Client configuration](mcp-client-config.md)。
