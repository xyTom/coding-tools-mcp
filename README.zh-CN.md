# Coding Tools MCP

[English](README.md) | **简体中文**

> 为任何支持 MCP 的 AI 客户端提供一双有边界、可审计的“代码之手”。

[![PyPI](https://img.shields.io/pypi/v/coding-tools-mcp)](https://pypi.org/project/coding-tools-mcp/)
[![npm](https://img.shields.io/npm/v/coding-tools-mcp)](https://www.npmjs.com/package/coding-tools-mcp)
[![Python](https://img.shields.io/pypi/pyversions/coding-tools-mcp)](https://pypi.org/project/coding-tools-mcp/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Coding Tools MCP 是一个模型无关的编程运行时，通过 Model Context Protocol
提供受限文件读取与搜索、原子多文件补丁、命令执行、交互式进程、Git 检查、
可选上游 MCP 组合、持久化 OAuth、Workspace 绑定的 HTTP Session，以及需要
专用认证的 Admin WebUI。

默认本地目录包含 20 个真实标注的工具。权限模式只改变命令策略，不改变
`tools/list`。可选上游工具在 Runtime 初始化时形成不可变快照，并通过稳定
namespace 暴露；远端能力由上游服务器自己的安全边界控制，不应误认为受本地
Workspace 文件边界保护。

## 主要特点

- **跨客户端统一契约**：Claude Desktop、Claude Code、Codex、Cursor、Cline
  与自建 Agent 使用同一套 MCP schema 和结果 envelope。
- **不可变 Workspace 绑定**：每个 HTTP MCP Session 在 `initialize` 时绑定一个
  已校验 Workspace，Session 存续期间不能切换；cwd、进程、输出和项目指令互不共享。
- **固定工具目录**：旧版 `tool_profile` 仅作为迁移输入被删除，不能隐藏工具、
  改写目录或控制 annotation。
- **持久化 OAuth**：Client、Grant、access-token 元数据、refresh-token family
  与 signing-key 状态跨重启保留，并可按稳定 ID 精确撤销。
- **不泄露 Secret 的管理面**：`/admin` 使用专用 Admin token，支持 Settings、
  Workspace、Gateway、OAuth、Secret Vault 和聊天/session 管理。
- **适合上下文窗口**：文本结果摘要化、分页化，完整机器结果保存在
  `structuredContent`。

## 快速开始

Python 包要求 Python 3.11 或更高版本。npm 包只是一个启动器，通过 `uvx` 或
`pipx` 启动固定版本的 Python 服务。

```bash
uvx coding-tools-mcp --stdio --workspace /path/to/repo
npx coding-tools-mcp --stdio --workspace /path/to/repo
```

客户端配置示例：

```json
{
  "mcpServers": {
    "coding-tools": {
      "command": "uvx",
      "args": ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

去掉 `--stdio` 即可使用 Streamable HTTP，默认地址为
`http://127.0.0.1:8765/mcp`。主协议版本为 MCP `2025-11-25`，并明确兼容
`2025-06-18`。

## 集成后的 v0.2.2 架构

### Session 与 Workspace

每次成功的 HTTP `initialize` 都会创建独立 Runtime，拥有独立 Workspace、cwd、
进程表、保留输出、项目指令和上游工具快照。后续 POST/DELETE 请求必须匹配初始化
时的认证上下文。stdio 只使用明确的默认 Workspace，不伪造 OAuth Agent。

### 持久化 OAuth

OAuth 支持 Authorization Code + PKCE S256、RFC 7591 动态注册、
`authorization_code` 与 `refresh_token` grant、refresh rotation、旧 refresh
重放后的 family 撤销、access-token `jti` 撤销，以及 active/retired/revoked
signing key 生命周期。

Client secret 只保存 digest；refresh token 只保存带 pepper 的 hash；signing
material 只保存在加密 Secret Vault。OAuth Store 或 Vault 不可用时 fail closed，
不会退化到内存实现。详见 [远程 MCP](docs/remote-mcp.md) 和
[升级/回滚指南](docs/migration-v0.1-to-v0.2.2.md)。

### Gateway

Gateway 配置、启用状态和 allowlist 在 Runtime 初始化前固定。每个 Runtime 获得
不可变的 namespaced 工具快照，因此 `listChanged: false` 是真实语义。本地工具名
永久保留，namespace 冲突时 fail closed。Admin 保存 Gateway 后只标记
`restart_required`，不支持热 reload/start/stop。

### Admin WebUI

配置专用 Admin token 后访问 `/admin`。普通 MCP bearer 和 OAuth access token
不会自动获得管理员权限。Settings 写入使用 revision；遇到 stale HTTP 409 时，
页面保留草稿、刷新 persisted revision 并提示冲突，不会静默覆盖。

OAuth、Gateway 和 Secret Vault 页面不会显示 secret、digest、hash、token material
或内部 reference。Conversation 列表只显示摘要，正文通过明确的分页详情请求按需加载。

## 本地工具目录

| 类别 | 工具 |
| --- | --- |
| 文件与搜索 | `read_file`、`list_dir`、`list_files`、`search_text`、`apply_patch`、`view_image` |
| 执行 | `exec_command`、`write_stdin`、`read_output`、`kill_session`、`request_permissions` |
| Git | `git_status`、`git_diff`、`git_log`、`git_show`、`git_blame` |
| Runtime | `server_info`、`check_exec_environment`、`get_default_cwd`、`set_default_cwd` |

`apply_patch` 是唯一的本地直接文件修改原语。根目录的 `AGENTS.md`/`CLAUDE.md`
会在初始化时加载。`content` 是简洁文本，`structuredContent` 是稳定完整的机器结果。

## 安全边界

| 模式 | 场景 | 实际行为 |
| --- | --- | --- |
| `safe` | 日常 Agent 工作 | 网络命令、shell 展开、内联脚本和破坏性命令需要授权 |
| `trusted` | 正常本地开发 | 开放网络、展开和内联脚本，仍保留 secret 与破坏性命令检查 |
| `dangerous` | 隔离容器/虚拟机 | 关闭命令权限门；本地直接路径工具仍受 Workspace 限制 |

权限模式不会隐藏 mutation tools。“高级危险兼容设置”
`--dangerously-fake-readonly-annotations` 只改写 `tools/list` 的暴露提示，
不会阻止真实执行或修改，也不是安全边界。

在不受 Landlock 保护的平台上，本服务不是完整 OS sandbox。处理不可信仓库时请使用
Docker、虚拟机或其他外部沙箱。上游 MCP 工具是远端能力，应按远端服务器自身权限评估。

## 远程访问与管理

HTTP 建议保持 loopback 绑定，再通过带认证的 HTTPS tunnel 发布。固定本地目录包含
修改和命令执行能力，绝不能把 `noauth` 公开到网络。详见
[Remote MCP](docs/remote-mcp.md)。

管理文档：

- [Admin API](docs/admin-api.md)
- [Admin WebUI](docs/admin-webui.md)
- [聊天与 Codex Session 持久化](docs/chat-persistence.md)

## Desktop 与 Admin WebUI 的职责区别

Desktop 是本地启动、tunnel 和 profile 管理器：

```bash
python -m pip install "coding-tools-mcp[desktop]"
coding-tools-mcp-desktop
```

Desktop 的 profile/secret 存储与服务器 Settings、Secret Vault 相互独立。Admin
WebUI 只通过专用 Admin API 管理已经运行的 HTTP 服务器，不是 Desktop 应用，也不
负责动态启动或停止 Gateway/Runtime。

## 遥测

上游默认启用匿名使用遥测，只包含封闭 schema 的计数、枚举、耗时和版本/平台维度；
不包含路径、Workspace/Agent/Client ID、命令、参数、文件内容、聊天正文或 transcript
摘要。可用 `CODING_TOOLS_MCP_TELEMETRY=off` 或 `DO_NOT_TRACK=1` 关闭；CI
自动关闭。`CODING_TOOLS_MCP_TELEMETRY=debug` 只把事件写到 stderr。详见
[docs/telemetry.md](docs/telemetry.md)。

## 文档

| 主题 | 文档 |
| --- | --- |
| 入门 | [快速开始](docs/quickstart.md)、[客户端配置](docs/mcp-client-config.md)、[故障排查](docs/troubleshooting.md) |
| Runtime | [工具与 schema](docs/tools-and-schemas.md)、[Runtime 契约](docs/runtime-contract-v0.2.md)、[权限模式](docs/permission-modes.md) |
| 远程与 OAuth | [Remote MCP](docs/remote-mcp.md)、[升级与回滚](docs/migration-v0.1-to-v0.2.2.md) |
| 管理 | [Admin API](docs/admin-api.md)、[Admin WebUI](docs/admin-webui.md)、[聊天持久化](docs/chat-persistence.md) |
| 安全与沙箱 | [安全策略](SECURITY.md)、[安全边界](docs/security-boundary.md)、[Docker](docs/docker.md) |
| 客户端与打包 | [Desktop README](apps/desktop-client/README.md)、[npm 启动器](npm/coding-tools-mcp/README.md) |
| 质量 | [CI 与测试](docs/ci-and-tests.md)、[Dogfood](docs/dogfood.md)、[SWE-bench](docs/swe-bench.md) |

## 开发

```bash
python -m pip install -e ".[dev]"
make ci
```

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。重新分发大量代码或文档时，请保留版权、
许可证、[NOTICE](NOTICE) 和来源说明。
