# WebCodex 特性融合与 Coding Runtime 平台化：低上下文 Agent 分阶段执行计划

## 0. 文档用途

本文档用于指导多个低上下文 Agent / 多个独立会话，分阶段把 WebCodex 类产品能力和当前已经提出的增强能力融合进 `coding-tools-mcp`，同时尽量保护现有 MCP Runtime、固定工具目录、Upstream Broker、OAuth、Workspace 隔离、原子 Patch 和测试资产。

本文档不是一次性“大重构”方案，也不是要求把 WebCodex 源码整体搬入本项目。执行策略是：

> **接口先行、局部重构、稳定契约不动、数据面与控制面分离、每个 Phase 可独立验证和交接。**

执行者被假定为：

- 单次上下文较低；
- 不一定了解前序会话全部讨论；
- 可能由不同 Agent 在不同聊天窗口接力；
- 容易因为一次读取过多代码而丢失任务边界；
- 可能在 Windows、Linux、远程 Runner 等不同环境执行；
- 必须优先保护当前仓库中已经稳定的安全和协议契约。

因此本计划强制采用：

1. **一个 Agent 会话只负责一个明确 Task/Phase，但多个满足依赖条件的 Task/Phase 可以由不同 Agent 同时执行。**
2. 并行开发按固定 Workstream 和文件所有权划分，不允许多个 Agent 在不同 worktree 同时大改同一个核心文件。
3. 每个 Task/Phase 先读本计划的固定章节、当前任务、直接依赖 handoff，不通读全部历史。
4. 每个 Task/Phase 必须有独立测试、独立实现提交、独立 handoff。
5. 未满足本任务依赖和验收条件时，不得自行绕过依赖进入后续集成阶段；但不相关 Workstream 可以继续并行。
6. 所有 worktree 必须位于本项目目录内的 `.worktrees/`。
7. 所有可控临时文件、缓存、参考仓库、测试临时目录必须位于本项目目录内的 `.tmp/`。
8. 不允许为了开发方便把临时 worktree、clone、build scratch、测试输出放到系统临时目录、用户目录、兄弟目录或其他磁盘目录。
9. **已有代码优先复用。** 当前项目或 WebCodex 原项目已经有可用实现时，优先 wrap / adapt / extract / move，不重新写第二套等价实现。
10. 所有从 WebCodex 直接复用或改写的代码必须记录 source commit、source path、目标文件和授权/NOTICE 处理，不允许“看过后凭印象重写”造成不可追踪重复实现。
11. 现有 25 个 MCP 工具的外部 schema 和核心语义，除非某个专门契约任务明确批准，否则不得改变。
12. 新的 Agent/Codex/Runner/LSP/Validation/Connectivity 功能必须能够进入**现有 `webui` 控制台代码库和构建链**；不得另起第二套独立前端工程。
13. Web/Mobile 普通身份与 Admin 管理权限仍保持后端授权边界，即使两者共用现有 WebUI shell/component。
14. 不允许把 Codex App Server、Tunnel、Runner、LSP 全部直接塞回 `server.py` 或 `admin.js`。

本文档本身只定义执行计划。创建本文档不代表已经创建 integration worktree，也不代表任何功能已经实现。

---

## 1. 创建计划时已确认的仓库事实

以下事实只是本文档创建时的 baseline。后续 Agent 必须重新确认现场状态，但不得无理由推翻已经明确的架构决策。

| 项目 | 创建计划时的值 |
| --- | --- |
| 仓库根目录 | `G:\LLM\coding-tools-mcp` |
| 主分支 | `main` |
| 创建计划时 HEAD | `42d940b3ed756a7dd9a6a993c268a7f4663a094c` |
| 创建计划时工作树 | clean |
| Python 版本线 | `3.11+` |
| 当前项目版本 | `0.3.0.dev0` |
| MCP 固定本地工具目录 | 25 个 |
| Upstream Broker | 已实现，Runtime snapshot immutable，`listChanged=false` |
| HTTP MCP Session | 每 Session 独立 Runtime / Workspace / cwd / process / upstream snapshot |
| Transcript | SQLite，按 `workspace_id` 分区 |
| Codex 现有能力 | 仅有 bounded Codex session 文件发现 / 导入，不是 live App Server bridge |
| Workspace Catalog | 已实现，多 Workspace、禁止嵌套 root |
| Admin WebUI | 已实现，纯 JS + build 到 `webui_dist` |
| Desktop | PySide6，本地启动 / tunnel / profile 管理 |
| `.tmp/` | 已在 `.gitignore` 中 |
| `.worktrees/` | 项目下已存在目录；Phase 00 必须再次确认 Git ignore/exclude 状态 |

当前 `Runtime` 仍然是一个较大的 session-scoped 组合对象，直接拥有 Workspace、Process Session、Patch State、ProjectContext、UpstreamManager、Telemetry 等。后续融合不能继续把所有新能力堆入该对象。

---

## 2. 最终产品目标

### 2.1 用户可见目标

最终希望同时保留和获得以下体验：

1. **现有 MCP Coding Runtime 能力不退化**
   - 25 个本地工具保持稳定；
   - 原子 `apply_patch`、进程控制、Git、Upstream Broker 继续工作；
   - OAuth、Workspace 绑定、HTTP Session isolation 继续成立。

2. **Web / Mobile Codex 工作台**
   - 手机和桌面浏览器都可访问；
   - 可选择 Workspace / 工作目录；
   - 可创建、列出、恢复 Codex thread；
   - 支持显式 session instructions；
   - 支持流式消息和审批；
   - 页面关闭或换窗口后，可以恢复同一个 durable Agent Session；
   - 后续允许从另一台设备继续同一项目会话。

3. **真正的跨窗口执行连续性**
   - 保存的不只是聊天文本；
   - 需要保存 Workspace、Agent backend、backend thread id、conversation link、instruction metadata、repo fingerprint、last turn / approval / job 状态；
   - MCP Session、Agent Session、Shell/Job Session 明确分离。

4. **语义代码理解**
   - LSP status；
   - document symbols；
   - goto definition；
   - find references；
   - diagnostics；
   - 后续可增加 hover / workspace symbols。

5. **结构化验证**
   - Python / Node / Rust / Go 等项目使用受控 validation recipe；
   - 模型不必每次手写复杂测试命令；
   - 结果统一成机器可读状态和 diagnostics；
   - `exec_command` 继续作为 escape hatch。

6. **Inspect 执行模式**
   - 允许执行检查类命令，但限制普通 Workspace 写入；
   - Linux 上优先利用 Landlock；
   - Windows 上必须明确报告 capability unavailable，不能伪装成已隔离。

7. **Server / Runner 分离**
   - Control Plane 可放在公网；
   - Runner 主动连接 Control Plane；
   - Repo、Codex App Server、LSP、Git、Shell 仍运行在 Workspace 所在机器；
   - Control Plane 不把远程 root 当作本机 `Path`；
   - 后续支持多机器 Workspace。

8. **长任务和断线恢复**
   - Job 有稳定 ID；
   - Runner 短暂断开后可以 reconciliation；
   - 已运行任务不能因为 WebSocket 暂断就立即被判定丢失。

9. **移动端连接与隧道**
   - Desktop / Control Plane 对 tunnel provider 进行统一管理；
   - Cloudflare / FRP 等属于 Connectivity 层，不进入 MCP Runtime；
   - 手机 onboarding 能明确显示访问地址、认证方式和 Runner 状态。

10. **可选 Workflow Facade / OpenAPI**
    - 核心稳定后，可增加面向高层 coding workflow 的小工具面或 OpenAPI；
    - 不能破坏固定 MCP Core catalog；
    - 不能通过 live `tools/listChanged` 做动态 profile。

### 2.2 内部工程目标

完成后，`coding-tools-mcp` 应从“一个功能很强的 MCP Server”演进为：

> **Coding Runtime Core + Agent Session Platform + Local/Remote Workspace Data Plane + 多协议 Adapter。**

同时，MCP 仍然是第一等接口，而不是被 Web 产品层取代。

---

## 3. 明确不做的事情

除非用户后续单独改变决策，本轮融合不做以下事情：

1. 不整体 fork / 覆盖 WebCodex 架构到本仓库。
2. 不把当前 25 个本地 MCP 工具替换成 WebCodex 的 task-level surface。
3. 不删除 `apply_patch`，不新增一批功能重复的 mutation tools。
4. 不让 Remote Runner 通过“远程 `Path` 假装本地 `Path`”的方式渗透整个 Runtime。
5. 不把 Web Chat 放到 `/admin` 并复用 Admin token。
6. 不把 Agent Session 当成 MCP HTTP Session 的别名。
7. 不把所有 Agent state 塞进 transcript `metadata_json`。
8. 不在第一版 Runner 实现 QUIC；先保证 WebSocket/HTTPS 连接、认证和恢复语义。
9. 不把 dependency auto-install 作为 validation 默认行为。
10. 不把 fake readonly annotation 当作真正的安全能力。
11. 不在没有第二个实现需求前设计过度抽象的“万能 provider”。
12. 不为了移动端 UI 重写现有 Admin WebUI，也不新建第二套独立前端工程；应复用现有 `webui/src`、i18n、build/test pipeline 和控制台导航，并在需要时通过不同 route/auth capability 暴露受限视图。
13. 不复制已有 OAuth、Workspace Catalog、Transcript、Tunnel、i18n、Settings、Admin API helper、Patch、Process 或 Broker 能力来建立平行实现。

---

## 4. 目标架构

### 4.1 总体拓扑

```text
                      ┌────────────────────┐
                      │   Browser / Mobile │
                      └─────────┬──────────┘
                                │ HTTPS
                    ┌───────────▼────────────┐
                    │      Control Plane     │
                    │                        │
                    │ Auth / Principal       │
                    │ Workspace Catalog      │
                    │ AgentSessionService    │
                    │ AgentSessionStore      │
                    │ Runner Registry        │
                    │ Routing / Attachments  │
                    │ Operator Web API       │
                    │ Admin API              │
                    └───────┬─────────┬──────┘
                            │         │
                   LocalHost│         │RunnerBridge
                            │         │
            ┌───────────────▼──┐   ┌──▼────────────────┐
            │ Local Data Plane │   │ Remote Runner     │
            │                  │   │ Data Plane        │
            │ MCP Runtime      │   │                   │
            │ Codex App Server │   │ MCP Runtime       │
            │ LSP              │   │ Codex App Server  │
            │ Validation       │   │ LSP               │
            │ Job Manager      │   │ Validation        │
            │ Upstream Broker  │   │ Job Manager       │
            └──────────────────┘   │ Upstream Broker   │
                                   └───────────────────┘

MCP Client ───────► MCP Adapter ─────► LocalHost / RemoteRunnerHost
Desktop Client ───► Control Plane / TunnelProvider
```

### 4.2 最重要的边界决策

#### A. Control Plane 和 Data Plane 分离

Control Plane 负责：

- 认证；
- Workspace / Runner 注册；
- durable Agent Session；
- Web/Mobile API；
- Session attachment；
- Router；
- Admin；
- tunnel metadata / onboarding。

Data Plane 负责：

- 真正的 repo 文件访问；
- Git；
- Shell / Process；
- Patch；
- Codex App Server；
- LSP；
- Validation；
- 本地 Upstream MCP；
- 真实 Workspace instruction discovery。

#### B. Remote Runner 不代理单个 `Path`

远程 Runner 的主边界是整个 Workspace Host / Data Plane，而不是给每个文件工具做 `RemotePath`。

推荐抽象：

```text
WorkspaceHost
├─ LocalWorkspaceHost
└─ RemoteRunnerWorkspaceHost
```

这意味着：

- 当前 `Runtime` 仍然可以在 repo 所在机器用真实 `Path`；
- 远端 Workspace root 只在 Runner namespace 内有意义；
- Control Plane 存的是 `runner_id + workspace_id + runner_local_root`，不能在本机 `Path.resolve()`；
- MCP Session 如果路由到 Remote Runner，应让 Runner 创建真正的 Runtime，再转发 RPC / tool call，而不是在 Control Plane 模拟 Workspace。

#### C. `ExecutionBackend` 只作为 Data Plane 内部重构

为了减少 `Runtime` 直接依赖 OS 细节、支持 inspect mode 和测试，可以逐步抽取：

```text
ExecutionBackend
└─ LocalExecutionBackend
```

未来如确有价值，可以在 Runner 内有其他实现，但 **Remote Runner 本身不依赖把所有 `ExecutionBackend` 方法跨网络暴露**。

#### D. 三种 Session 必须独立

```text
AgentSession        durable，跨窗口 / 跨设备
McpRuntimeSession   ephemeral，绑定 MCP Session ID
ExecSession / Job   ephemeral 或可 reconciliation 的进程任务
```

不得把三者合并为一个 `session_id`。

#### E. 复用现有 WebUI 控制台，但保持后端权限边界

前端不再规划为两个独立工程。目标是一个现有 `webui` 代码库、一个共享控制台 shell、一个 build/test pipeline：

```text
webui/src
├─ shared shell / navigation / i18n / security helpers
├─ existing admin/settings/gateway/oauth panels
├─ agent workbench / thread / chat panels
├─ runner / semantic / validation panels
└─ connectivity / mobile onboarding panels
```

路由可以按授权需要提供：

```text
/admin      -> 现有完整控制台；Admin 可看到管理能力 + Agent 工作台
/app        -> 可选的受限入口；复用同一 WebUI bundle/component，只显示 Operator capability
/admin/api  -> 专用 Admin credential
/api/app    -> Principal + Workspace 授权
```

关键要求：

- 用户要求的所有新增功能都必须可以从当前 WebUI 控制台进入，不做“另一个前端项目”。
- 可以为了避免 `admin.js` 继续膨胀，把功能拆成模块，但仍属于现有 `webui/src`。
- Admin 使用者可以在现有控制台里进入 Agent 工作台、Sessions、Runners、Code Intelligence、Validation、Connectivity 等页面。
- 如果提供 `/app` 移动端受限入口，必须复用同一前端模块，不复制一套 UI。
- 普通 Web Chat 用户不能因为能聊天就获得 Gateway、OAuth Secret、Vault、Workspace Catalog 管理权限。

---

## 5. 计划中的核心接口

以下接口是目标，不要求 Phase 01 一次全部实现。每个接口只能按真实需求增量增加方法。

### 5.1 `AgentSessionBackend`

职责：对接具体 Agent 引擎。

第一实现：`CodexAppServerBackend`。

候选能力：

```python
create_thread(...)
resume_thread(...)
send_turn(...)
interrupt_turn(...)
approve(...)
list_threads(...)
close_thread(...)
stream_events(...)
```

未来可增加 OpenCode / Claude Code 等实现，但当前不得为了未来实现提前扩张协议。

### 5.2 `AgentSessionStore`

职责：保存 durable execution session state，不替代 TranscriptStore。

保存：

- session id；
- workspace id；
- principal id / owner；
- backend kind；
- backend thread id；
- conversation id；
- session status；
- explicit instruction state；
- repo fingerprint；
- last turn / approval / job summary；
- created/updated timestamps。

聊天正文仍由 TranscriptStore 管理，避免重复存储两套正文真相。

### 5.3 `WorkspaceHost`

职责：表示 Workspace 数据面位于哪里。

```text
WorkspaceHost
├─ LocalWorkspaceHost
└─ RemoteRunnerWorkspaceHost
```

它负责创建或寻址：

- MCP Runtime；
- Codex backend；
- Semantic backend；
- Validation backend；
- Job manager。

### 5.4 `ExecutionBackend`

职责：Data Plane 内部 OS / process / file mutation 边界。

第一版只做 `LocalExecutionBackend`，用于逐步把 `Runtime` 的 OS 操作抽离；不能改变工具 schema。

### 5.5 `SemanticBackend`

```text
SemanticBackend
├─ NullSemanticBackend
└─ LspSemanticBackend
```

### 5.6 `ValidationBackend`

负责：

- 项目类型检测；
- recipe 解析；
- command allowlist / manifest-aware selection；
- 结构化结果归一化。

### 5.7 `TunnelProvider`

属于 Desktop / Connectivity 层：

```text
TunnelProvider
├─ CloudflareTunnelProvider
├─ FrpExternalProvider
└─ ExternalTunnelProvider
```

不能由 MCP Runtime 直接持有。

### 5.8 `RunnerTransport`

第一版目标：authenticated WebSocket over HTTPS。

后续可增加 polling fallback。QUIC 不在第一版范围内。

---

## 6. 数据模型原则

### 6.1 Agent Session 与 Transcript 的关系

建议关系：

```text
workspace
  │
  ├─ conversation
  │    └─ messages / context
  │
  └─ agent_session
       ├─ backend_thread_id
       ├─ conversation_id ──────────┘
       ├─ repo_fingerprint
       ├─ instruction_state
       └─ current jobs / approvals summary
```

Agent Session 只引用 Conversation，不复制全部消息正文。

### 6.2 Repo Fingerprint

至少考虑：

- 当前 branch；
- HEAD；
- dirty / staged 状态摘要；
- 项目 instruction 文件 digest；
- 关键 manifest / lockfile digest；
- Workspace root identity；
- 可选 sparse/submodule/worktree identity。

Fingerprint 不是安全 hash，也不是完整内容快照。它用于恢复 thread 时判断“当前 repo 是否和上一次有效上下文明显不同”。

### 6.3 Instruction State

区分：

1. Repo 自己的 AGENTS / CLAUDE 等指令；
2. Workspace 级显式自定义 instructions；
3. Agent Session 级 instructions；
4. 当前 turn 用户消息。

不得把 repo-derived instructions 永久复制成第二份真相。推荐保存：

- 显式用户 instructions 内容；
- repo instruction 文件集合和 digest；
- 创建 / 恢复时重新发现 repo instructions；
- digest 变化时提示 session context stale。

---

## 7. Worktree 与临时文件硬约束

本节是用户明确要求，优先级高于 Agent 自己的习惯。

### 7.1 唯一允许的 worktree 根目录

所有 worktree 必须在：

```text
G:\LLM\coding-tools-mcp\.worktrees\
```

本计划默认唯一长期 integration worktree：

```text
G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform
```

对应分支：

```text
integration/webcodex-runtime-platform
```

默认采用**一个长期 integration worktree + 多会话串行 Phase**，不要每个 Phase 新建 worktree。

只有某个 Phase 明确要求隔离实验时，才允许新建：

```text
.worktrees\webcodex-pNN-<slug>
```

禁止：

- `G:\LLM\.worktrees\...`
- `%TEMP%\...`
- `C:\Users\...\worktree`
- repo 的兄弟目录；
- 其他磁盘临时目录；
- Agent 自己创建的隐藏外部 worktree。

### 7.2 唯一允许的项目临时根目录

所有 Agent 主动创建的临时内容必须放在：

```text
G:\LLM\coding-tools-mcp\.tmp\webcodex-runtime-platform\
```

建议结构：

```text
.tmp/webcodex-runtime-platform/
├─ cache/
│  ├─ uv/
│  ├─ pip/
│  ├─ npm/
│  ├─ cargo/
│  └─ playwright/
├─ reference/
│  └─ webcodex/
├─ phase-00/
├─ phase-01/
└─ ...
```

如果必须 clone WebCodex 作为参考，只允许放到：

```text
.tmp/webcodex-runtime-platform/reference/webcodex
```

不得 clone 到仓库外。

### 7.3 每个会话启动时必须重定向临时目录

Windows PowerShell 建议：

```powershell
$Repo = "G:\LLM\coding-tools-mcp"
$Phase = "phase-NN"
$TmpRoot = Join-Path $Repo ".tmp\webcodex-runtime-platform"
$PhaseTmp = Join-Path $TmpRoot $Phase
New-Item -ItemType Directory -Force $PhaseTmp | Out-Null

$env:TEMP = $PhaseTmp
$env:TMP = $PhaseTmp
$env:TMPDIR = $PhaseTmp
$env:UV_CACHE_DIR = Join-Path $TmpRoot "cache\uv"
$env:PIP_CACHE_DIR = Join-Path $TmpRoot "cache\pip"
$env:NPM_CONFIG_CACHE = Join-Path $TmpRoot "cache\npm"
$env:XDG_CACHE_HOME = Join-Path $TmpRoot "cache\xdg"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $TmpRoot "cache\playwright"
```

如果某 Phase 使用 Rust，再设置：

```powershell
$env:CARGO_HOME = Join-Path $TmpRoot "cache\cargo-home"
$env:CARGO_TARGET_DIR = Join-Path $TmpRoot "build\cargo-target"
```

如果 Agent 使用其他包管理器，应将其可配置 cache/store 同样指向 `.tmp/webcodex-runtime-platform/`。

### 7.4 临时文件违规停止条件

发现 Agent 自己创建的以下内容在 repo 外时，当前 Phase 必须停止并修正：

- clone；
- worktree；
- test scratch；
- generated fixture；
- browser download；
- package cache；
- build scratch；
- logs；
- local DB fixture；
- Runner state fixture。

操作系统或第三方进程内部、无法配置的瞬时系统行为不能被 Agent 声称为项目产物；只要工具提供可配置目录，就必须重定向到项目 `.tmp/`。

---

## 8. 多会话执行协议

### 8.1 每个新会话只读这些内容

每个新 Agent 会话默认只允许先读取：

1. 本文档第 0～9 节；
2. 当前要执行的 Phase；
3. `docs/webcodex-integration-handoffs/STATUS.md`；
4. 上一个 Phase 的 handoff；
5. 当前 Phase 明确列出的文件。

不要一次性读取全部历史 handoff、整个 `server.py`、整个 `admin.py`、全部测试。

### 8.2 每个 Phase 固定流程

1. 确认 repo root。
2. 确认 worktree 路径必须位于 `.worktrees/`。
3. 确认当前 branch / HEAD / status。
4. 设置本 Phase 的 `.tmp` 环境变量。
5. 读取 `STATUS.md` 和上一 Phase handoff。
6. 读取当前 Phase 指定文件。
7. 记录 baseline tests。
8. 只修改允许范围。
9. 运行最小测试。
10. 运行 Phase 扩展测试。
11. 运行 `git diff --check`。
12. 检查 diff，不允许无关文件。
13. 创建实现提交。
14. 写当前 Phase handoff + 更新 STATUS。
15. 创建 handoff 提交。
16. 停止，不顺手执行下一 Phase。

### 8.3 Git 禁止事项

禁止：

- `git reset --hard`
- `git clean`
- force push
- 删除未知 worktree
- 覆盖未知分支
- 在主工作树直接做长期功能开发
- `git add -A` 后不检查 staged diff
- 在未获得用户授权时 push / release / deploy
- 把 `.tmp/` 或 runtime DB 强行加入 Git

### 8.4 代码修改规则

- 文本文件使用 patch 编辑。
- 不通过临时脚本批量无审查重写核心模块。
- 不手改 `coding_tools_mcp/webui_dist`；必须从 `webui/src` 正式 build。
- 每次结构重构必须先有 characterization test。
- 任何 schema 改动必须同步 contract / golden test。
- 新 DB migration 必须支持已有 DB 启动或明确 fail-closed migration，不得静默丢数据。

---

## 9. Handoff 规范

Phase 00 创建：

```text
docs/webcodex-integration-handoffs/
├─ STATUS.md
├─ phase-00.md
├─ phase-01.md
└─ ...
```

`STATUS.md` 只保留短表，避免低上下文 Agent 加载全部历史：

```markdown
| Phase | Status | Implementation commit | Handoff | Notes |
| --- | --- | --- | --- | --- |
| 00 | complete | abc1234 | phase-00.md | baseline |
| 01 | pending | - | - | ADR |
```

每个 `phase-NN.md` 必须使用：

```markdown
# Phase NN Handoff

## Status
- Result: complete | blocked
- Branch: integration/webcodex-runtime-platform
- Worktree: G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform
- Started from: <hash>
- Implementation commit: <hash or none>
- Handoff commit: <由下一 Agent 从 git log 确认>

## Scope Completed
- ...

## Files Changed
- `<path>`：原因

## Contract / ADR Decisions Applied
- ...

## Validation Performed
| Command | Exit code | Result |
| --- | ---: | --- |
| `...` | 0 | ... |

## Temporary / Worktree Compliance
- Worktree remained under `.worktrees/`: yes/no
- Phase temp root: `.tmp/webcodex-runtime-platform/phase-NN`
- External temp artifacts intentionally created: none

## Security Check
- Secrets / tokens added to Git: no
- Real transcript / OAuth DB / runner credential committed: no

## Remaining Risks
- ...

## Next Phase Preconditions
- ...
```

如果 Phase blocked，也必须写 handoff；不要只在聊天里解释。

---

## 10. Phase 总览

| Phase | 主题 | 核心结果 |
| ---: | --- | --- |
| 00 | 现场冻结与 integration worktree | 安全工作区、STATUS、baseline |
| 01 | 产品契约与 ADR | 固化架构边界，不写功能代码 |
| 02 | Characterization tests | 给现有 Runtime / Session / Web/Admin 行为上护栏 |
| 03 | Runtime 组合边界重构 | Runtime 变 composition root，不改工具契约 |
| 04 | Local ExecutionBackend | 抽 OS/file/process 边界，仍只有 local 实现 |
| 05 | AgentSessionStore | durable Agent execution state 与 transcript 分离 |
| 06 | Codex App Server Adapter | live app-server supervisor / protocol client |
| 07 | AgentSessionService | create/resume/turn/interrupt/thread 生命周期 |
| 08 | Operator Auth + Web API | 独立于 Admin 的普通用户 API |
| 09 | Web/Mobile Workspace & Thread UI | workspace picker、thread list、新建/恢复 |
| 10 | Streaming / Approval UI | turn 流、审批、interrupt、错误恢复 |
| 11 | Cross-window Continuity | attachment、instructions、repo fingerprint、stale detection |
| 12 | Deterministic Handoff / Finish | task/session 事实摘要与下一步 evidence |
| 13 | LSP SemanticBackend | symbols / definition / references / diagnostics |
| 14 | Structured ValidationBackend | Python/Node/Rust/Go recipe |
| 15 | Inspect Mode | 可执行但限制 Workspace 写入 |
| 16 | WorkspaceHost / Data Plane 边界 | LocalHost 完整落地，为 Runner 准备 |
| 17 | Runner Enrollment + Transport | authenticated WebSocket、runner registry |
| 18 | Remote Runner 路由 | Codex/MCP/LSP/validation 在 runner 数据面执行 |
| 19 | Job Reconciliation | reconnect、job inventory、recovering 状态 |
| 20 | Tunnel / Mobile Onboarding | provider abstraction、Desktop 集成、手机入口 |
| 21 | Optional Workflow MCP / OpenAPI | 高层 facade，不改变 Core catalog |
| 22 | 全量安全、性能、迁移、发布验证 | Release candidate / 文档 / rollback |

---

# Phase 00：现场冻结、临时目录策略和 integration worktree

## 目标

- 不在主工作树直接进行长期融合开发。
- 建立唯一 canonical integration branch/worktree。
- 建立 handoff / STATUS 机制。
- 记录当前 baseline。
- 确保所有临时内容和 worktree 都在项目目录内。

## 只读取

- 本文档。
- `.gitignore`。
- `.git/info/exclude`（如果当前工具允许安全读取）。
- `pyproject.toml`。
- `docs/ci-and-tests.md`。

## 执行步骤

1. 确认 repo root 是 `G:\LLM\coding-tools-mcp`。
2. 确认主工作树 branch、HEAD、status。
3. 确认 `.tmp/` 被 ignore。
4. 确认 `.worktrees/` 被 `.gitignore` 或 `.git/info/exclude` 忽略；若两者都没有，使用最小变更补充。
5. 建立 `.tmp/webcodex-runtime-platform/phase-00` 和 cache 子目录。
6. 重定向 TEMP/TMP/TMPDIR/包缓存。
7. 检查 `integration/webcodex-runtime-platform` 是否已存在。
8. 检查 `.worktrees/webcodex-runtime-platform` 是否已存在。
9. 如果都不存在，从当前用户确认的 baseline HEAD 创建：

   ```powershell
   git worktree add ".worktrees\webcodex-runtime-platform" -b "integration/webcodex-runtime-platform" <BASELINE_HEAD>
   ```

10. 如果已经存在，不得删除重建；确认用途、分支、状态，发现未知修改则 blocked。
11. 在 canonical worktree 创建 `docs/webcodex-integration-handoffs/STATUS.md`。
12. 记录当前快速测试 baseline，不修改产品代码。
13. 写 `phase-00.md`。

## 验收

- canonical worktree 路径严格位于 repo `.worktrees/`。
- 工作分支为 `integration/webcodex-runtime-platform`。
- handoff 目录存在。
- 当前 baseline 测试结果已记录。
- 没有在 repo 外创建 Agent 控制的 temp / clone / worktree。

---

# Phase 01：固化产品契约和 ADR

## 目标

在改 Runtime 前，把后续 Agent 不应重新争论的决策写成 ADR / integration contract。

## 只读取

- `README.md` / `README.zh-CN.md`
- `docs/runtime-contract-v0.2.md`
- `docs/upstream-broker.md`
- `docs/remote-mcp.md`
- `docs/chat-persistence.md`
- `coding_tools_mcp/server.py` 中 `Runtime` 构造和 initialize/list_tools 附近片段
- `coding_tools_mcp/transport_http.py`
- `coding_tools_mcp/workspace_catalog.py`
- `coding_tools_mcp/transcript.py` schema/migration 部分

## 允许修改

- `docs/adr/*`
- 新增 `docs/webcodex-runtime-integration-contract.md`
- handoff 文件

## 必须固化的 ADR

至少包含：

1. Control Plane / Data Plane separation。
2. AgentSession != McpRuntimeSession != ExecSession/Job。
3. Operator App auth != Admin auth。
4. Remote Runner 使用 `WorkspaceHost` 粗粒度边界，不实现 `RemotePath`。
5. 当前 Core MCP tool catalog 不变。
6. Runtime snapshot / Broker immutable 语义不变。
7. Agent state 单独存储，不塞 transcript metadata。
8. Codex App Server 第一实现，但接口命名不绑定产品。
9. Runner 第一版 WebSocket，QUIC 延后。
10. 所有 worktree/temp 只在项目目录。

## 验收

- 只有文档变化。
- 后续 Phase 不需要重新决定上述 10 个问题。
- 文档明确列出哪些能力属于 core、control plane、data plane、desktop/connectivity。

---

# Phase 02：现有行为 Characterization Tests

## 目标

在结构重构前给当前行为建立防回归护栏。

## 重点覆盖

1. `Runtime.initialize()` instructions / listChanged。
2. 25 个工具名和 annotations。
3. HTTP Session 创建、独立 cwd、delete/close。
4. process session 生命周期。
5. `apply_patch` 路径与 rollback contract。
6. Workspace binding。
7. transcript workspace partition。
8. CodexSessionScanner 当前“只扫描导入、不 live resume”的 baseline。
9. Admin token 不能等价于普通 MCP auth，反之亦然。
10. Upstream Broker snapshot immutable。

## 允许修改

- 测试文件；
- 小型 test fixture；
- handoff。

不得修改 runtime 行为来“适配测试”。

## 验收

- 新测试在当前 baseline 全部通过。
- 测试足以在 Phase 03/04 发现 Runtime 重构导致的 schema/session 行为漂移。

---

# Phase 03：Runtime 组合边界重构（无功能变化）

## 目标

把 `Runtime` 从“拥有所有实现细节的超级对象”向 session-scoped composition root 演进，但不改变 MCP schema 和行为。

## 推荐拆分

根据现场代码最小化选择，不强制一次全部完成：

```text
Runtime
├─ WorkspaceContext
├─ PermissionContext
├─ ProcessManager
├─ ToolDispatcher / Registry view
├─ PatchService
├─ UpstreamManager
└─ Telemetry
```

优先抽 `ProcessManager` 和纯 session state；避免第一步移动数千行 tool handler。

## 规则

- 现有 25 个工具 schema byte-level / semantic contract 不应变化。
- `Runtime` 对外方法名可暂时保留，内部转发到新 service。
- 不同时做 Codex、LSP、Runner 功能。
- 单个 commit 不应既移动大量代码又改变逻辑。

## 测试

- Phase 02 characterization tests。
- `tests/compliance/test_mcp_contract.py`
- `tests/compliance/test_runtime_helpers.py` 中相关子集。
- tool golden。

## 验收

- `Runtime` 仍然是 MCP session composition root。
- process / state 至少有一个独立模块。
- 外部工具定义和 HTTP Session 语义不变。

---

# Phase 04：Local ExecutionBackend

## 目标

在 Data Plane 内建立 OS/file/process 执行边界，第一版只存在 local 实现。

## 设计限制

- 不做网络 RPC。
- 不引入 `RemoteExecutionBackend`。
- 不让 Workspace root 失去真实 `Path` 类型。
- 不一次重写所有 tool handler。

## 推荐顺序

1. Process spawn / poll / kill。
2. 命令 cwd / env policy。
3. 文件读 / list / search 的底层 accessor。
4. Patch service 只在必要处依赖 backend；原子 committer 语义保持。
5. Git 最后迁移。

接口只抽已经有调用者的能力。

## 验收

- `LocalExecutionBackend` 完整通过 characterization。
- 没有网络 transport。
- 没有改变 Workspace boundary。
- 后续 inspect mode 可以在该边界上实现。

---

# Phase 05：AgentSessionStore 和 durable domain model

## 目标

新增与 transcript 分离的 Agent execution state。

## 候选新模块

```text
coding_tools_mcp/agent_sessions.py
coding_tools_mcp/agent_session_store.py
```

名称可根据现有风格调整，但职责必须分开：domain/service 与 SQLite store 不混成一个大类。

## 最小 schema

建议至少包含：

```text
agent_sessions
  workspace_id
  session_id
  owner_principal_id
  backend_kind
  backend_thread_id
  conversation_id
  status
  explicit_instructions
  instruction_digest
  repo_fingerprint_json
  last_turn_id
  created_at
  updated_at
```

如 approval/job 需要多记录，使用独立表或 bounded event table，不无限增长 metadata blob。

## Migration 要求

- 旧 `transcripts.sqlite3` 可继续启动。
- 如果复用同一 DB 文件，升级必须事务化并有 `user_version` / migration test。
- 更推荐独立 `agent-sessions.sqlite3` 或清晰独立 schema；选择必须写 ADR。
- 不得读取真实用户 DB 做 fixture。

## 验收

- 可以 create/get/list/update/close Agent Session。
- 强制 `workspace_id + owner/principal` 边界。
- Conversation 仅引用，不复制消息正文。
- 有 migration / corruption / idempotency tests。

---

# Phase 06：Codex App Server Adapter

## 目标

从“扫描 Codex session 文件”升级为可 live 控制 Codex App Server，但不先做 Web UI。

## 前置研究

由于 Codex App Server 协议会变化，本 Phase 必须：

1. 确认当前目标 Codex 版本。
2. 记录 app-server protocol / method baseline。
3. 若需要参考源码或文档，reference clone 只能在 `.tmp/webcodex-runtime-platform/reference/`。
4. 在 ADR/handoff 记录精确版本或 commit，不依赖“latest”。

## 推荐模块

```text
coding_tools_mcp/agent_backends/base.py
coding_tools_mcp/agent_backends/codex_app_server.py
```

## 最小能力

- spawn / connect app-server；
- health / version；
- create thread；
- resume thread；
- send turn；
- receive structured events；
- interrupt；
- approval request / response；
- graceful close。

## 安全要求

- app-server process 必须在目标 Workspace Data Plane 上运行。
- stdout/stderr/protocol framing 有大小限制。
- 不记录 token / secret。
- app-server 崩溃要返回结构化 unavailable，而不是拖死 MCP server。

## 验收

- 用 fake protocol server 完成 deterministic unit test。
- 若本机有 Codex，可做 opt-in integration smoke；没有时 unit tests 仍可完成 Phase。
- 不修改 Web UI。

---

# Phase 07：AgentSessionService 与 Thread 生命周期

## 目标

把 Phase 05 durable store 和 Phase 06 Codex backend 组合成可测试应用服务。

## 最小用例

```text
create_session(workspace, owner, backend, instructions)
resume_session(session_id)
send_turn(session_id, message)
interrupt(session_id)
approve(session_id, approval_id, decision)
close_session(session_id)
list_sessions(workspace)
```

## 关键规则

- backend thread id 不对未授权 principal 泄露。
- Session 创建时绑定 Workspace；默认不允许运行中切换 Workspace。
- resume 时重新确认 Workspace 仍 enabled / authorized。
- AgentSession 不依赖当前 MCP Session ID。
- browser/window attachment 不持久化成 AgentSession 本体。

## 验收

- 同一个 durable session 可在 service 重启/重建后从 store 恢复元数据。
- 不要求原进程永久存在；backend unavailable 有清晰状态。
- cross-workspace / cross-principal access tests 必须 fail closed。

---

# Phase 08：Operator Auth 与 Web API

## 目标

建立面向普通 Web/Mobile 用户的 API，严格独立于 `/admin`。

## 推荐 surface

```text
/app
/api/app/workspaces
/api/app/sessions
/api/app/sessions/{id}
/api/app/sessions/{id}/turns
/api/app/sessions/{id}/interrupt
/api/app/sessions/{id}/approvals/{approval_id}
```

实际 URI 可以调整，但必须有独立 auth middleware / authorization check。

## 认证决策

优先复用已有 OAuth principal / bearer authority 的“普通调用者身份”，而不是 Admin token。

必须验证：

- principal 可访问哪些 Workspace；
- session owner；
- CSRF / Origin 策略；
- 浏览器 secret 不写 URL；
- session list 只返回 bounded summary。

## 验收

- Admin token 不自动获得普通 Agent Session owner identity，除非显式走受控 admin impersonation（本阶段默认不做）。
- MCP bearer/OAuth 也不能访问 Admin API。
- Operator API 有独立安全测试。

---

# Phase 09：Web/Mobile Workspace Picker 与 Thread UI

## 目标

先做可用 shell，不先追求复杂聊天细节。

## UI 最小功能

- 响应式移动端布局；
- Workspace picker；
- Agent Session / thread list；
- New Session；
- Resume Session；
- session metadata：Workspace、status、backend、last updated；
- explicit instructions 输入；
- offline / backend unavailable 状态。

## 文件规则

- 新 Operator App 应与 `webui/src/admin.*` 分离，避免 admin.js 继续膨胀。
- 可共享基础 i18n / security helper，但不要让 Operator App 直接依赖 Admin page state。
- build 产物统一由 script 生成。

## 测试

- model/unit test；
- DOM interaction test；
- desktop + mobile viewport smoke；
- Workspace unauthorized 不出现在 picker。

## 验收

- 手机上可以选择 Workspace、创建 session、看到历史 session、恢复 session shell。
- 尚未完成 streaming 也可以结束本 Phase。

---

# Phase 10：Streaming Turn、Approval、Interrupt UI

## 目标

把 Operator App 从“session manager”升级为真正可用 Web Codex。

## 能力

- send turn；
- server events 流式显示；
- tool / progress / assistant event 区分；
- approval prompt；
- approve / deny；
- interrupt；
- backend disconnect；
- turn failed / retryable 状态；
- bounded event history。

## Transport

浏览器到 Control Plane 可以使用 SSE 或 WebSocket；选择前写小 ADR。不要因为 Runner 后续使用 WebSocket 就强制浏览器也相同。

## 验收

- 一条完整 turn 能从手机发送到 Codex App Server 并流式返回。
- approval round-trip 可测试。
- 页面刷新不会自动重复提交最后一条 turn。

---

# Phase 11：Cross-window Continuity、Instructions、Repo Fingerprint

## 目标

完成 WebCodex 最大产品特性：关页面、换窗口、换设备后继续同一个 durable Agent Session。

## 新概念

```text
AgentSession         durable
ClientAttachment     ephemeral
```

ClientAttachment 可包含：

- browser window id；
- last observed event cursor；
- connected_at / last_seen；
- 当前 transport id。

它不能改变 AgentSession owner/workspace。

## Repo Fingerprint 流程

1. Session create 后记录 fingerprint。
2. Resume 前重新计算轻量 fingerprint。
3. 无变化：直接继续。
4. 有变化：标记 `context_changed`，显示具体变化摘要。
5. 根据 backend 能力决定继续原 thread、明确 refresh context 或新建 thread；不得静默假装无变化。

## Instructions 流程

- Repo instructions 重新发现并计算 digest。
- 用户显式 instructions 从 durable store 恢复。
- 如果 repo instruction digest 改变，UI 显示提示。
- 不把旧 repo instruction 文本无限复制到 store。

## 验收

- Browser A 创建 session，关闭。
- Browser B / 新窗口恢复同一 session。
- conversation 和 backend thread 连续。
- Repo HEAD/AGENTS 改变时能检测，不静默吞掉。

---

# Phase 12：Deterministic Handoff / Finish Evidence

## 目标

提供 Runtime 事实生成的 session handoff，而不是再让模型凭记忆总结一次。

## 输出至少包含

- Workspace；
- Agent Session id / backend；
- 当前 branch / HEAD；
- changed paths；
- git status summary；
- validation 状态；
- 最近失败；
- active / recovering jobs；
- repo fingerprint 变化；
- unresolved approval；
- 建议 next actions（由确定性状态推导，不生成自由文本计划）。

## 限制

- bounded size；
- 不包含 secret；
- 不把完整 transcript 重复进去；
- 可以引用 conversation/session id。

## 验收

- 同样的 runtime state 生成稳定 projection。
- 适合作为跨模型 / 跨窗口 handoff。

---

# Phase 13：LSP SemanticBackend

## 目标

增加语义导航，不改变已有 `search_text` / `read_file`。

## 第一版语言

根据当前目标环境优先：

- Python；
- TypeScript / JavaScript；
- Rust。

如果依赖过重，可先 Python + TypeScript，Rust 下一小提交，但 Phase handoff 必须明确。

## 第一版工具能力

```text
semantic_status
document_symbols
goto_definition
find_references
document_diagnostics
```

是否直接增加到 Core 25 工具目录必须遵守 Phase 01 ADR。推荐方式：

- Core fixed catalog 需要变更时，作为显式版本化契约升级；或
- 先作为 AgentSession / workflow capability，不偷偷改变当前 25 工具。

不得在这一 Phase 擅自使工具目录动态变化。

## 安全

- LSP process 在 Data Plane；
- 禁止自动 dependency install；
- 禁止 workspace 外任意路径访问；
- 所有 LSP output bounded。

## 验收

- fixture 项目可 deterministic definition/reference/diagnostic。
- LSP 不可用返回 capability status，不拖死 Runtime。

---

# Phase 14：Structured ValidationBackend

## 目标

提供结构化项目验证 API。

## Recipe 最小集合

```text
python: syntax | test | lint
node:   syntax | test | lint
rust:   check | test
go:     test
```

实际执行命令由 manifest / lockfile / 已安装工具决定。

## 原则

- 默认不安装 dependency；
- 不执行未知 package lifecycle script，除非 recipe 明确允许；
- command 和 cwd 必须进入现有 permission policy；
- 输出统一为结构化状态。

建议结果：

```json
{
  "status": "passed|failed|unavailable",
  "recipe": "python:test",
  "command": "...",
  "exit_code": 0,
  "diagnostics": [],
  "duration_ms": 1234
}
```

## 验收

- 每种 recipe 至少有 fixture test。
- `exec_command` 行为不改变。

---

# Phase 15：Inspect Mode

## 目标

增加与现有 `safe/trusted/dangerous` 正交的 filesystem execution mode。

推荐模型：

```text
permission_mode: safe | trusted | dangerous
execution_fs_mode: normal | inspect
```

不要新增一个混合意义的 `inspect-safe-trusted` mode。

## Inspect 语义

- 允许命令执行；
- Workspace 普通写入被隔离；
- Runtime scratch 可写；
- Linux 优先用 Landlock；
- Windows/不支持平台明确 `inspect_enforced=false`，不得虚假承诺。

## 测试

- read-only command 成功；
- 写 Workspace 失败；
- scratch 成功；
- capability report 真实。

---

# Phase 16：WorkspaceHost / Data Plane 边界

## 目标

在真正做网络 Runner 前，用 local 实现把 Control Plane 与 Workspace Data Plane 接口固化。

## 接口原则

`WorkspaceHost` 不应该暴露大量底层文件函数。它表达“在哪个 Data Plane 创建/访问能力”。

候选能力：

```text
resolve_workspace_handle()
create_mcp_runtime()
get_agent_backend()
get_semantic_backend()
get_validation_backend()
get_job_manager()
snapshot_status()
```

第一实现：`LocalWorkspaceHost`。

## 关键测试

- 所有现有本地行为通过 LocalWorkspaceHost 后仍一致。
- Control Plane 代码不直接 `Path(remote_root).resolve()`。
- WorkspaceCatalog 可为 future runner 记录 target/host identity，但旧配置可迁移。

---

# Phase 17：Runner Enrollment、Registry 与 Transport

## 目标

实现最小 authenticated Runner，不先路由全部功能。

## Runner 设计

Runner 是主动连接 Control Plane 的进程：

```text
Runner -> HTTPS/WSS -> Control Plane
```

避免要求家用机器接受公网入站端口。

## 最小协议

- enroll / authenticate；
- runner instance id；
- heartbeat；
- capability advertisement；
- workspace inventory summary；
- request / response correlation；
- event push；
- graceful disconnect。

## Credential

- Runner credential 与 MCP OAuth token / Admin token 分离；
- secret 进入 Secret Vault 或独立安全 store；
- 日志只显示 fingerprint / stable id，不显示明文 token；
- 支持 revoke / rotate。

## 第一版 transport

Authenticated WebSocket over HTTPS。

不做 QUIC。

## 验收

- fake Runner 可注册、心跳、断开。
- 未授权 Runner fail closed。
- replay / duplicate instance 基本语义有测试。

---

# Phase 18：Remote Runner 路由与远程 Workspace

## 目标

让 Agent Session 和 MCP 能力真正运行在远端 Runner 的 Data Plane。

## 推荐顺序

1. Agent backend 路由到 Runner Codex App Server。
2. Semantic / Validation 路由。
3. Job manager 路由。
4. MCP Runtime/session 路由。

不要第一天把所有东西一起透传。

## MCP 路由原则

- Runner 创建真实 `Runtime`；
- Runtime snapshot / 25 tools / Upstream Broker 仍由 Runner 本地生成；
- Control Plane 只保存路由/session mapping 和 bounded metadata；
- `Mcp-Session-Id` 仍与 authorization context 绑定；
- DELETE / close 必须路由到正确 Runner；
- Runner 不在线时返回 retryable unavailable，而不是创建错误本地 Runtime。

## Workspace root

远端 root：

```text
runner_id = home-win
root = G:\LLM\coding-tools-mcp
```

Control Plane 只把 `root` 当作 Runner namespace 数据，不调用本机 filesystem API。

## 验收

- 一个本地 Workspace 和一个 fake/真实 remote Workspace 可以共存。
- 远端 Session 无法访问另一个 Runner 的 Workspace。
- MCP tool schema 不因路由层变化而漂移。

---

# Phase 19：Job Reconciliation 与断线恢复

## 目标

Runner 短暂断线时，不立即丢失长任务状态。

## 状态模型

```text
running
recovering
completed
failed
cancelled
lost
```

## 协议

Runner reconnect 后提交 bounded job inventory：

- job id；
- pid/process identity fingerprint；
- status；
- stdout/stderr cursor；
- started_at；
- workspace id。

Control Plane 根据 runner instance identity reconciliation。

## 限制

- 不假装进程跨机器迁移；
- Runner 重启后无法证明仍是同一 process 时可以标 lost；
- cross-principal job handle 不可读。

## 验收

- 模拟 transport drop + reconnect，job 从 recovering 恢复 running/completed。
- unknown job 不被错误绑定。

---

# Phase 20：TunnelProvider、Desktop 和 Mobile Onboarding

## 目标

把“手机能用”作为正式产品入口完成，而不是靠用户手工拼 tunnel 命令。

## 设计边界

Tunnel 仍属于 Desktop / Connectivity 层。

推荐：

```text
Desktop App
├─ Runtime / Control Plane launcher
├─ TunnelProvider
├─ Runner status
└─ Mobile onboarding
```

## 第一版能力

- Cloudflare 临时 tunnel；
- Cloudflare named tunnel（已有能力继续整理）；
- FRP 外部托管模式；
- external URL mode；
- 显示 Operator App URL；
- 显示认证状态；
- 显示 Runner connected / disconnected；
- 二维码可作为后续 UI 增强，但不能把 bearer secret 明文编码进长期可分享 URL。

## 安全

- 禁止公开 noauth；
- Operator App / MCP / Admin URL 明确区分；
- Admin token 不进入 onboarding QR/link；
- tunnel 日志不得显示 secret。

## 验收

- 手机浏览器通过 tunnel 访问 Operator App。
- 能选择 Workspace、恢复 Agent Session。
- Admin surface 仍需独立凭据。

---

# Phase 21：可选 Workflow MCP Surface / OpenAPI

## 启动条件

只有 Phase 00～20 核心功能稳定、用户仍需要时才执行。

## 目标

提供更高层 coding workflow facade，例如：

```text
task_start
task_status
task_finish
run_checks
semantic_definition
```

或生成 OpenAPI / GPT Actions surface。

## 约束

- Core MCP 25-tool surface 不通过 live profile 变化。
- 如果新增 MCP surface，使用独立 endpoint / explicit startup surface，并保持 Runtime 内 snapshot immutable。
- OpenAPI 和 MCP schema 应从同一 internal contract 生成或共享定义，避免维护两套语义。
- 不替代底层工具。

## 验收

- facade 只是 application service adapter。
- 不直接访问 Runtime 私有字段。

---

# Phase 22：全量安全、性能、迁移和发布验证

## 目标

形成可发布候选，而不是“功能看起来能跑”。

## 必跑验证矩阵

### Core MCP

- MCP contract；
- tool golden；
- strict JSON；
- Runtime semantics；
- Workspace session binding；
- Upstream Broker discovery/calls/lifecycle/result budget。

### OAuth / Security

- OAuth integration / refresh / store / signing；
- Admin auth boundary；
- Operator auth boundary；
- Runner credential revoke；
- cross-workspace / cross-principal isolation；
- no-secret logging。

### Agent Session

- create/resume/close；
- backend unavailable；
- cross-window attachment；
- repo fingerprint change；
- instruction change；
- approval；
- deterministic handoff。

### Web UI

- `npm --prefix webui test` 或拆分后的各 app test；
- production build；
- desktop viewport；
- mobile viewport；
- no Admin state leakage。

### Runner

- enrollment；
- reconnect；
- remote Workspace routing；
- MCP remote session close；
- Job reconciliation；
- Runner unavailable / retryable errors。

### LSP / Validation

- fixture definitions / refs / diagnostics；
- recipe pass/fail/unavailable；
- no dependency auto-install。

### Inspect

- Linux enforced tests；
- Windows capability-report tests。

## 性能基线

至少记录：

- MCP initialize/tool list bytes；
- Agent Session create/resume latency；
- first event latency；
- WebSocket reconnect time；
- Runner RPC p50/p95；
- LSP first-start / warm query；
- transcript/session DB size growth；
- memory per active MCP Runtime / Codex backend。

不要声称“比 WebCodex 更快”或“比 Codex 更快”，除非同条件 benchmark 支持。

## Migration / Rollback

必须提供：

- DB backup instructions；
- AgentSession DB migration；
- Workspace Catalog 新字段 migration；
- Runner 配置可禁用；
- Operator App 可禁用；
- 本地 MCP-only 模式仍能启动；
- 回滚到前一版本时哪些新 DB 字段会被忽略 / 不兼容的说明。

## 最终验收

1. 旧 MCP-only 用户无需启用 Agent/Runner 也能使用。
2. Core 25 tools 和 Broker contract 无意外变化。
3. 本机 Web Codex 工作流可用。
4. 跨窗口恢复可用。
5. Remote Runner 可用。
6. 手机 tunnel 访问可用。
7. Operator/Admin 权限边界通过测试。
8. worktree/temp 项目内约束在所有 handoff 中均为 yes。

---

## 11. 跨 Phase 必须保持的稳定契约

以下内容只有专门 contract migration Phase 才能修改：

1. MCP `tools/listChanged=false` 的真实语义。
2. Runtime 初始化后固定 Workspace。
3. 本地 MCP tool namespace 保留。
4. Upstream Broker immutable snapshot。
5. `apply_patch` 是直接文件 mutation 主原语。
6. permission annotation 不是安全边界。
7. MCP OAuth access token 不等于 Admin credential。
8. Remote Runner capability 由 Runner 自身 Data Plane enforcement 控制。
9. Transcript/Agent Session/Telemetry 不记录 secret。
10. 真实用户聊天正文、Workspace path、command、secret 不进入匿名 telemetry。

---

## 12. 推荐模块布局（目标，不要求一次完成）

```text
coding_tools_mcp/
├─ server.py                     # MCP / HTTP composition，继续瘦身
├─ runtime.py                    # 若拆出 Runtime composition root
├─ execution.py                  # ExecutionBackend + local implementation
├─ process_manager.py            # process/job primitives
├─ agent_sessions.py             # Agent session domain/service types
├─ agent_session_store.py        # durable SQLite store
├─ agent_backends/
│  ├─ base.py
│  └─ codex_app_server.py
├─ semantic/
│  ├─ base.py
│  └─ lsp.py
├─ validation/
│  ├─ base.py
│  └─ recipes.py
├─ workspace_host.py             # Local / Remote Runner host abstraction
├─ runner/
│  ├─ protocol.py
│  ├─ registry.py
│  ├─ transport.py
│  └─ client.py
├─ transcript.py
├─ workspace_catalog.py
├─ upstream.py
├─ oauth.py
├─ admin.py
└─ ...

webui/
├─ src/
│  ├─ admin-*.js/css/html        # Admin Console
│  └─ app/                       # Operator Web/Mobile App
└─ tests/

apps/desktop-client/
└─ ...                           # TunnelProvider / onboarding / launcher
```

实际文件名应遵循仓库现有风格。不要为了匹配这张图而无意义移动稳定代码。

---

## 13. 失败停止条件

当前 Phase 遇到以下任意情况必须写 blocked handoff 并停止：

- canonical worktree 不在项目 `.worktrees/`；
- Agent 已在项目外创建需要保留的临时仓库 / build / fixture；
- 上一 Phase 未 complete；
- 当前 worktree 有未知修改；
- 需要改变 Phase 01 已固化 ADR；
- 为了继续必须修改当前 Phase 之外大量模块；
- 测试失败且不能证明是 baseline failure；
- 需要把 Admin token 当普通 Web Chat credential；
- 需要在 Control Plane 本机解析 remote Workspace root；
- 需要把真实用户 DB / token / transcript 提交进 Git；
- 需要 push / deploy / release 才能继续；
- Codex App Server 协议与计划假设明显不符且没有先更新 ADR；
- Runner transport 要求引入未审计的 secret handling；
- Windows/Linux 安全能力被错误描述为等价。

---

## 14. 每个会话建议提交粒度

一个 Phase 推荐两个 commit：

1. 实现提交：

   ```text
   feat(...): ...
   refactor(...): ...
   test(...): ...
   ```

2. handoff 提交：

   ```text
   docs(handoff): record phase NN <topic>
   ```

如果 Phase 只有文档/测试契约，可以只有一个实现类提交 + 一个 handoff 提交。

不要把 5 个 Phase squash 成一次“mega implementation”后才验证。

---

## 15. 新 Agent 会话启动提示词模板

用户可以把下面模板交给每个新会话，只替换 Phase 编号：

```text
你正在继续 coding-tools-mcp 的 WebCodex 特性融合计划。

仓库根目录固定为：
G:\LLM\coding-tools-mcp

所有 worktree 必须在：
G:\LLM\coding-tools-mcp\.worktrees\

所有可控临时文件、clone、cache、测试 scratch 必须在：
G:\LLM\coding-tools-mcp\.tmp\webcodex-runtime-platform\

禁止在项目外创建 worktree 或临时项目文件。

本次只执行 Phase NN，不执行后续 Phase。

先读取：
1. docs/webcodex-feature-integration-agent-execution-plan.md 的第 0～9 节；
2. Phase NN；
3. docs/webcodex-integration-handoffs/STATUS.md；
4. 上一 Phase handoff。

严格按 Phase 的只读范围、允许修改范围、测试和验收执行。
开始前确认 branch/worktree/HEAD/status，并把 TEMP/TMP/TMPDIR 和包缓存重定向到当前 Phase 的项目内 .tmp 目录。

完成实现后：
1. 运行 Phase 指定测试；
2. git diff --check；
3. 审查无关改动；
4. 创建实现提交；
5. 写 phase-NN.md 并更新 STATUS.md；
6. 创建 handoff 提交；
7. 停止，不继续 Phase NN+1。

如果遇到计划中的停止条件，写 blocked handoff，不要自行扩大范围。
```

---

## 16. Definition of Done

整个计划只有同时满足以下条件才算完成：

- [ ] Core MCP-only 模式仍能独立运行。
- [ ] 25-tool contract / Broker 没有非计划漂移。
- [ ] Runtime 已不再承载所有新功能实现细节。
- [ ] Agent Session 与 MCP Session / Process Session 分离。
- [ ] Codex App Server live backend 可用。
- [ ] Operator Web/Mobile App 可选择 Workspace。
- [ ] 可创建、恢复、继续 thread。
- [ ] 支持 explicit instructions 和 repo instruction change detection。
- [ ] 跨窗口 / 跨设备 continuity 通过测试。
- [ ] approval / interrupt / streaming 可用。
- [ ] deterministic handoff 可用。
- [ ] LSP semantic capability 可用或明确 capability unavailable。
- [ ] structured validation 可用。
- [ ] inspect mode 在支持平台真实 enforce，在不支持平台真实报告。
- [ ] LocalWorkspaceHost 和 RemoteRunnerWorkspaceHost 都通过 contract test。
- [ ] Runner enrollment / revoke / heartbeat / reconnect 通过。
- [ ] MCP / Codex / LSP / validation 可路由到 Remote Runner。
- [ ] Job reconciliation 通过断线测试。
- [ ] Desktop/Tunnel 手机入口可用。
- [ ] Operator auth 与 Admin auth 完全分离。
- [ ] 迁移 / rollback 文档完整。
- [ ] 所有 Phase handoff 都确认 worktree 位于项目 `.worktrees/`。
- [ ] 所有 Phase handoff 都确认 Agent 可控临时内容位于项目 `.tmp/`。
- [ ] 没有 secret、真实 OAuth DB、真实 transcript、Runner credential 被提交。
- [ ] 全量 CI / compliance / WebUI / Desktop / Runner / security gates 通过。

---

## 17. 最终架构判断

本次融合的核心不是“把 WebCodex 接口插进 `server.py`”，也不是“推倒重写 coding-tools-mcp”。

执行方向应始终保持：

> **70% 接口化，30% 局部重构，0% 推倒重写。**

其中：

- **必须局部重构**：Runtime / Process / Session / Workspace Host 边界；
- **适合 Adapter 插入**：Codex App Server、LSP、Validation、Tunnel；
- **必须新增独立 durable domain**：AgentSessionStore；
- **必须保持数据面本地化**：Repo、Git、Shell、Codex App Server、LSP；
- **必须保护的稳定资产**：现有 MCP tool contract、Broker、安全边界、OAuth、Workspace isolation、atomic patch、测试体系。

如果后续某 Agent 的实现方向开始要求“为一个新功能大面积改变已有 MCP 工具语义”，应优先暂停并重新检查是否违反了本计划的 Control Plane / Data Plane / Adapter 边界。
