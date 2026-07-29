# 上游 v0.2.2 集成：低上下文 Agent 分阶段执行计划

## 0. 文档用途

本文档用于把 `xyTom/coding-tools-mcp` 上游 `v0.2.2` 的新特性和修复集成到当前本地定制版本中。

执行者被假定为：

- 推理能力有限；
- 单次上下文有限；
- 可能在不同会话中由不同 Agent 接力；
- 容易在大范围 merge、长命令和隐含假设中犯错。

因此，本计划强制采用以下模式：

1. 一次会话只执行一个 Phase。
2. 每个 Phase 只读取列出的文件。
3. 每个 Phase 必须独立测试、提交、生成 handoff。
4. 未满足验收条件时不得进入下一 Phase。
5. 不允许在当前脏 `main` 上直接执行 `git merge origin/main`。
6. 不允许把本地大型 `server.py` 整体覆盖到上游。

本文档本身只是计划；创建本文档不代表已经执行任何集成步骤。

---

## 1. 已确认的仓库事实

执行 Agent 不得重新猜测以下事实；如果现场状态不同，必须停止并记录差异。

| 项目 | 已确认值 |
| --- | --- |
| 仓库根目录 | `G:\LLM\coding-tools-mcp` |
| 当前本地分支 | `main` |
| 当前本地提交 | `717dacf9b1e9d5a86704d8629d0a0b7fea2eaabf` |
| 上游 remote | `origin` |
| 上游仓库 | `https://github.com/xyTom/coding-tools-mcp.git` |
| 上游目标提交 | `311c1f2529d0f047ad2a8b68db6bf92dbb93d6bc` |
| 上游目标标签 | `v0.2.2` |
| 共同祖先 | `d1766a3033fc8ad55018cdf2005c89e64747ac53` |
| 本地独有提交 | 8 个 |
| 上游独有提交 | 51 个 |
| 用户 fork remote | `onestao` |
| 推荐集成分支 | `integration/upstream-v0.2.2` |
| 推荐集成 worktree | `<repo>\.worktrees\upstream-v0.2.2-integration` |

当前本地工作树存在未提交修改和未跟踪文件。不得假定工作树干净。

已发现 `onestao` remote URL 曾包含明文 GitHub PAT。该 PAT 必须由用户在 GitHub 撤销或轮换；Agent 不得打印、测试、复制或记录该 PAT。

---

## 2. 最终目标架构

集成完成后，应以上游 v0.2.2 运行时为核心，本地能力作为分离模块接入：

```text
coding_tools_mcp/
├─ server.py                 # 上游组合入口；避免再次膨胀为全部实现
├─ protocol.py               # 上游 MCP 2025-11-25 / 2025-06-18 兼容
├─ patching.py               # 上游原子 patch 与回滚
├─ processes.py              # 上游进程和保留输出生命周期
├─ tool_results.py           # 上游 structuredContent/result contract
├─ transport_http.py         # 上游 HTTP transport helper
├─ transport_stdio.py        # 上游 stdio transport helper
├─ project_context.py        # 上游 AGENTS/CLAUDE 指令加载
├─ oauth.py                  # 上游 OAuth 协议行为 + 本地持久化适配接口
├─ oauth_store.py            # 本地 OAuth 持久化
├─ secret_vault.py           # 本地密钥存储
├─ settings_store.py         # 本地服务器设置持久化
├─ settings_definition.py    # 本地统一校验和规范化
├─ workspace_catalog.py      # 本地多工作区目录
├─ upstream.py               # 本地上游 MCP gateway
├─ admin.py                  # 本地 Admin 服务
├─ transcript.py             # 本地 transcript/chat 数据
├─ codex_sessions.py         # 本地 Codex session 发现与读取
├─ chat_cli.py               # 本地聊天 CLI 适配
└─ webui.py                  # 本地 WebUI 静态资源和入口
```

### 2.1 必须保留的上游行为

- MCP 主协议 `2025-11-25`，兼容 `2025-06-18`。
- 每个 `Mcp-Session-Id` 独立 runtime。
- `DELETE /mcp` 会话终止。
- 标准 cancellation、严格协议头、JSON-RPC batch 拒绝。
- `apply_patch` 原子写入、baseline 检查、回滚和换行/BOM/权限保持。
- `structuredContent` 作为稳定机器接口。
- 有界输出、`next_action`、进程数量/大小/TTL 限制。
- 自动加载根目录项目指令。
- OAuth redirect/resource/client/auth method 精确绑定。
- v0.2.2 DCR metadata narrowing 修复。
- OAuth 默认 access token TTL 为 24 小时，最大 604800 秒。
- 上游桌面客户端、npm launcher、Cloudflare control plane、release workflow。

### 2.2 必须保留的本地能力

- Admin WebUI。
- 设置持久化和旧配置迁移。
- 多 Workspace Catalog。
- OAuth Client、Grant、Access Token、Refresh Token、Signing Key 持久化。
- Refresh Token 轮换和 reuse detection。
- Secret Vault。
- OAuth key active/retired/revoked 生命周期。
- Agent 与 Workspace 绑定。
- upstream MCP gateway。
- transcript、Codex session、聊天持久化和管理。
- 当前新的 WebUI 设置模型、工作区编辑器和纯 JS 测试。

### 2.3 明确采用的兼容策略

除非用户明确修改决策，否则执行 Agent 必须采用以下策略：

1. 跟随上游固定工具目录，不恢复旧的 `full/read-only/compat-readonly-all` 工具隐藏机制。
2. `--dangerously-fake-readonly-annotations` 只作为上游兼容开关，绝不作为安全模式。
3. 旧 persisted `tool_profile` 可以读取并迁移，但不再控制 `tools/list`。
4. WebUI 原“仅查看”预设改名或移除，不得把“安全模式”错误描述成真正只读。
5. 保留本地持久化 OAuth 和 refresh token 功能。
6. 只有 token endpoint 已真实实现的 grant type 才能进入 advertised constants。
7. 集成期间保留上游 telemetry 默认行为；是否改为默认关闭必须作为后续独立产品决策，不混入集成提交。
8. 上游 desktop profile 存储与服务器 Admin settings 暂时保持分离，不在本次集成中统一两个密钥存储系统。
9. 集成期间版本暂时保持 `0.2.2`；最终 fork 版本在发布 Phase 单独决定。

---

## 3. 全局执行协议

### 3.1 每次会话只能执行一个 Phase

每次新 Agent 会话只允许读取：

1. 本文档的“第 1～3 节”；
2. 当前要执行的 Phase；
3. 上一个 Phase 的 handoff；
4. 当前 Phase 明确列出的源文件和目标文件。

不要一次性加载所有历史 handoff、全部 `server.py` 和全部测试。

### 3.2 每个 Phase 的固定流程

每个 Phase 必须严格按以下顺序：

1. 确认仓库根目录。
2. 确认当前 worktree、分支、HEAD、状态。
3. 读取上一阶段 handoff。
4. 读取当前 Phase 指定文件。
5. 只修改允许文件。
6. 先运行最小测试。
7. 再运行当前 Phase 的扩展测试。
8. 检查 `git diff --check`。
9. 检查 diff，确认没有无关文件。
10. 创建实现提交。
11. 写当前 Phase handoff，记录实现提交 hash。
12. 创建 handoff 文档提交。
13. 停止，不进入下一 Phase。

### 3.3 PowerShell 命令规则

- 使用原生 PowerShell。
- 不使用 `grep`、`sed`、`awk`、`tail`、`rm -rf`。
- 文本搜索优先使用 `rg`；输出筛选使用 `Select-String`。
- 每个重要命令单独执行。
- 每个 native command 后检查 `$LASTEXITCODE`。
- 任一关键命令失败后立即停止，不继续后续验证。
- 不使用一条长命令串联多个测试。

### 3.4 Git 禁止事项

执行期间禁止：

- `git reset --hard`
- `git clean -fd` 或任何 `git clean`
- `git checkout -- <path>`
- 在脏 `main` 上 `git merge origin/main`
- 整体 cherry-pick 本地 8 个功能提交
- force push
- 删除 backup/WIP 分支
- 删除或覆盖未知 worktree
- 在得到用户明确授权前 push

### 3.5 文件修改规则

- 使用 patch 编辑机制修改文本文件。
- 不用 shell 重定向、`Set-Content`、内联 Python 批量改写源文件。
- 格式化器和正式构建脚本生成的产物除外。
- `webui/src` 是 WebUI 源代码真相。
- `coding_tools_mcp/webui_dist` 必须由正式 build 生成，不手工独立修改。

### 3.6 失败停止条件

出现以下任意情况时，当前 Phase 必须写 blocked handoff 并停止：

- 仓库根目录或目标提交与本文不符；
- 当前 worktree 存在不属于当前 Phase 的未知修改；
- 上一阶段没有通过验收；
- 需要改变第 2.3 节中的产品决策；
- 测试失败且无法明确证明是已记录的上游 baseline failure；
- 发现 secret、token、OAuth DB 或真实用户数据可能被提交；
- 需要删除、覆盖或移动用户现有文件；
- 需要 push、发布包、创建 Release 或部署 Cloudflare；
- 一个文件需要同时采用两个互相矛盾的协议语义；
- 为了继续必须修改当前 Phase 允许范围外的大量文件。

---

## 4. Handoff 文件规范

Phase 00 先在 WIP 分支创建 `phase-00.md`。从 Phase 01 开始，把它和后续 handoff 一起维护在集成分支：

```text
docs/integration-handoffs/
├─ STATUS.md
├─ phase-01.md
├─ phase-02.md
└─ ...
```

每个 `phase-NN.md` 必须使用以下模板：

```markdown
# Phase NN Handoff

## Status

- Result: complete | blocked
- Integration branch: integration/upstream-v0.2.2
- Implementation commit: <hash or none>
- Handoff commit: filled by next agent from git log
- Started from: <hash>

## Scope Completed

- <完成项>

## Files Changed

- `<path>`：<变化原因>

## Decisions Applied

- <只记录实际采用的决策，不写推测>

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `<exact command>` | 0 | <结果摘要> |

## Known Baseline Failures

- None

## Remaining Risks

- <未解决风险；无则写 None>

## Next Phase Preconditions

- <下一阶段必须确认的条件>

## Secret Check

- No credentials, OAuth databases, bearer tokens, signing secrets, or vault files were added.
```

`STATUS.md` 只保留一张短表，避免后续 Agent 加载全部历史：

```markdown
| Phase | Status | Implementation commit | Handoff |
| --- | --- | --- | --- |
| 01 | complete | abc1234 | phase-01.md |
| 02 | pending | - | - |
```

---

## Phase 00：凭据处理、现场冻结与本地 WIP 保护

### 目标

- 不丢失当前未提交工作。
- 不让泄露的 GitHub PAT 继续存在于 remote URL。
- 让本文档拥有单独提交，便于带入集成分支。
- 创建明确的 backup 和 WIP 分支。

### 本阶段允许修改

- Git remote URL。
- Git branch/ref。
- 本文档的单独提交。
- 当前 WIP 的安全 checkpoint 提交。
- `.git/info/exclude` 中的 `.worktrees/` 忽略项。

### 必须先由用户完成

- 在 GitHub 中撤销或轮换已经暴露的 PAT。

Agent 不得声称已经撤销 PAT，除非用户明确确认。

### 执行步骤

1. 确认仓库：

   ```powershell
   git rev-parse --show-toplevel
   ```

   预期：`G:/LLM/coding-tools-mcp`。

2. 检查状态、分支和 worktree：

   ```powershell
   git status --short --branch
   ```

   ```powershell
   git worktree list --porcelain
   ```

3. 已知旧 `onestao` URL 含凭据，因此不要先运行会显示旧 URL 的命令。直接将它改为无凭据 HTTPS 地址：

   ```powershell
   git remote set-url onestao https://github.com/onestao/coding-tools-mcp.git
   ```

4. 修改后才检查 remote：

   ```powershell
   git remote -v
   ```

   确认所有 URL 不含 username、token、query string。不得把旧 PAT 或任何凭据复制到 handoff。

5. 检查 backup 分支是否已存在：

   ```powershell
   git branch --list backup/local-before-v0.2.2
   ```

   - 不存在：创建它，指向 `717dacf`。
   - 已存在：确认它指向 `717dacf`，否则停止。

   ```powershell
   git branch backup/local-before-v0.2.2 717dacf9b1e9d5a86704d8629d0a0b7fea2eaabf
   ```

6. 在暂存本文档之前，先确认 index 没有既有 staged 内容：

   ```powershell
   git diff --cached --name-only
   ```

   预期没有输出。若有输出，停止并报告；不得为了继续而重置或取消不属于自己的 staged 内容。

7. 仅暂存本文档并创建独立文档提交：

   ```powershell
   git add docs/upstream-v0.2.2-integration-agent-execution-plan.md
   ```

   ```powershell
   git diff --cached --stat
   ```

   预期：只包含本文档。若包含其他文件，停止并报告；不得自行取消或重置不属于自己的 staged 内容。

   ```powershell
   git commit -m "docs: add upstream v0.2.2 integration execution plan"
   ```

8. 创建或切换到 WIP 分支：

   ```powershell
   git branch --list wip/pre-upstream-v0.2.2
   ```

   - 不存在时：

     ```powershell
     git switch -c wip/pre-upstream-v0.2.2
     ```

   - 已存在时不得直接切换；先检查其 HEAD、状态和用途，确认不会覆盖当前工作。

9. 列出未提交文件：

   ```powershell
   git diff --name-only
   ```

   ```powershell
   git ls-files --others --exclude-standard
   ```

10. 检查以下高风险文件没有准备提交：

    - `*.sqlite`
    - `*.sqlite3`
    - `*.db`
    - `.env*`
    - `oauth-secrets.json`
    - `server-settings.json`
    - vault 数据文件
    - 包含真实 bearer token/signing secret 的 fixture

11. 运行：

    ```powershell
    git diff --check
    ```

12. 先审查 tracked diff，再选择性暂存。新文件逐个确认后显式添加。禁止无检查运行 `git add -A`。

13. 检查 staged diff：

    ```powershell
    git diff --cached --stat
    ```

    ```powershell
    git diff --cached --check
    ```

14. 创建 WIP checkpoint：

    ```powershell
    git commit -m "chore: checkpoint local work before upstream v0.2.2 integration"
    ```

15. 确认工作树干净。若仍有文件，必须说明是故意保留还是漏提交；不得继续创建 integration worktree，直到状态明确。

16. 确认 `.worktrees/` 已在 `.git/info/exclude` 中。若没有，使用 patch 编辑机制添加一行：

    ```text
    .worktrees/
    ```

17. 创建 `docs/integration-handoffs/phase-00.md`，至少记录：

    - plan commit hash；
    - WIP checkpoint hash；
    - backup branch hash；
    - WIP branch 名称；
    - repo root；
    - remote 已脱敏；
    - 用户是否确认 PAT 已撤销；
    - secret 检查结果；
    - Phase 01 前置条件。

18. 仅提交 Phase 00 handoff：

    ```text
    docs(handoff): record pre-integration checkpoint
    ```

19. 记录该 handoff commit hash，供 Phase 01 只 cherry-pick 文档使用。

### 验收

- `onestao` remote URL 不含凭据。
- 用户已确认 PAT 已撤销或轮换；若未确认，handoff 必须标 blocked。
- `backup/local-before-v0.2.2` 指向原本地 HEAD。
- 本文档有独立 commit。
- 本地 WIP 在 `wip/pre-upstream-v0.2.2` 有 checkpoint。
- 没有 DB、token、secret、vault 数据进入 Git。
- 当前主工作树状态明确。

### 本阶段结束

确认 `phase-00.md` 已记录以下值供 Phase 01 使用：

- plan commit hash；
- WIP checkpoint hash；
- backup branch hash；
- 当前 repo root；
- PAT 是否由用户确认撤销；
- Phase 00 handoff commit hash。

停止，不创建集成 worktree；由下一会话执行 Phase 01。

---

## Phase 01：创建干净集成 worktree 并验证纯上游基线

### 目标

- 创建基于 `origin/main@311c1f2` 的独立集成分支。
- 把本文档带入集成分支。
- 在未移植任何本地代码前记录上游测试基线。

### 只读取

- WIP 分支中的 `docs/integration-handoffs/phase-00.md`。
- 上游 `pyproject.toml`。
- 上游 `docs/ci-and-tests.md`。
- 上游 `Makefile`，仅用于了解测试门，不直接依赖其 POSIX 命令。

### 执行步骤

1. 在主工作树确认 repo root 和 worktree 列表。

2. 获取上游：

   ```powershell
   git fetch origin --prune --tags
   ```

3. 验证目标引用：

   ```powershell
   git rev-parse origin/main
   ```

   ```powershell
   git rev-parse v0.2.2
   ```

   两者预期都是 `311c1f2529d0f047ad2a8b68db6bf92dbb93d6bc`。不同则停止。

4. 确认分支和 worktree 目标均不存在：

   ```powershell
   git branch --list integration/upstream-v0.2.2
   ```

   ```powershell
   git worktree list --porcelain
   ```

5. 创建 worktree：

   ```powershell
   git worktree add ".worktrees\upstream-v0.2.2-integration" -b "integration/upstream-v0.2.2" origin/main
   ```

6. 进入 worktree 并确认：

   ```powershell
   Set-Location ".worktrees\upstream-v0.2.2-integration"
   ```

   ```powershell
   git status --short --branch
   ```

   预期：干净，分支为 `integration/upstream-v0.2.2`。

7. 从 WIP 分支读取 Phase 00 handoff，不加载其他 WIP diff：

   ```powershell
   git show wip/pre-upstream-v0.2.2:docs/integration-handoffs/phase-00.md
   ```

8. 从 Phase 00 handoff 取得 plan commit hash，只 cherry-pick该计划文档提交：

   ```powershell
   git cherry-pick <PLAN_COMMIT_HASH>
   ```

9. 从 Phase 00 handoff 取得 handoff commit hash，只 cherry-pick该 handoff 文档提交：

   ```powershell
   git cherry-pick <PHASE_00_HANDOFF_COMMIT_HASH>
   ```

   Phase 01 只允许 cherry-pick以上两个纯文档提交。不得 cherry-pick WIP checkpoint 或任何本地功能提交。

10. 创建 `docs/integration-handoffs/STATUS.md`，列出 Phase 00～14；Phase 00 为 complete，Phase 01 为 in progress，其余 pending。

11. 安装上游开发依赖：

   ```powershell
   uv sync --extra dev
   ```

12. 运行核心 unittest：

    ```powershell
    uv run python -m unittest discover -s tests -p "test_*.py"
    ```

13. 运行 lint：

    ```powershell
    uv run python -m ruff check --exclude benchmarks/dogfood --ignore=E501 coding_tools_mcp apps/desktop-client/mcp_desktop_client tests benchmarks
    ```

14. 运行核心协议测试：

    ```powershell
    uv run python -m tests.compliance.runner --suite mcp-contract
    ```

15. 运行工具 golden：

    ```powershell
    uv run python -m tests.compliance.runner --suite tool-golden
    ```

16. 在不改变当前目录的情况下运行 npm launcher 测试：

   ```powershell
   npm --prefix npm/coding-tools-mcp test
   ```

17. 记录所有命令、exit code、跳过项和失败项。不得为了让 baseline 变绿而修改上游代码。

18. 创建 Phase 01 handoff 和 STATUS 更新。

19. 由于本阶段除 plan/handoff 外不应有实现改动，只创建 handoff 文档提交：

    ```text
    docs(handoff): record upstream v0.2.2 baseline
    ```

### 验收

- worktree 位于仓库控制目录内。
- 分支基于精确的 `311c1f2`。
- 上游代码未被修改。
- baseline 测试结果完整记录。
- 若 baseline 失败，必须标记 blocked，下一阶段不得开始。

---

## Phase 02：建立集成契约和迁移决策测试

### 目标

- 把第 2.3 节的产品决策固化为短文档和失败优先的契约测试。
- 不移植业务实现。

### 只读取

- `docs/runtime-contract-v0.2.md`
- `coding_tools_mcp/protocol.py`
- `coding_tools_mcp/oauth.py`
- `coding_tools_mcp/server.py` 中 CLI parser、tool registry、OAuth handler 附近的定义
- WIP 分支中的 `docs/profile-v0.1.md`
- WIP 分支中的 `docs/oauth-agent-workspace-migration.md`
- WIP 分支中的 tool profile 相关测试

不要通读本地完整 `server.py`。

### 允许修改

- 新增 `docs/integration-contract-v0.2.2.md`
- 新增小型 migration/compatibility 测试文件
- `docs/integration-handoffs/*`

### 必须固化的契约

1. 协议版本目标。
2. 固定工具目录策略。
3. 旧 `tool_profile` 的迁移行为。
4. fake readonly 不是安全策略。
5. OAuth supported grant/response types 的单一常量来源。
6. OAuth store 必须可持久化并支持旧数据迁移。
7. Agent 到 Workspace 的绑定点。
8. telemetry 在集成期间不改变默认值。
9. desktop 和 Admin 设置暂不共享 secret store。

### 测试要求

新增测试应先表达迁移输入/输出，不要求本阶段全部通过实现测试。若采用 test-first：

- 只允许预期失败的测试存在于工作分支短时间内；
- 本阶段结束前，纯文档契约测试必须通过；
- 需要后续实现的测试可标记为明确 skip，并写出解除 skip 的 Phase 编号；
- 不允许留下无说明的 failing test。

### 提交

```text
docs(integration): define v0.2.2 extension contract
```

然后写 Phase 02 handoff 并单独提交。

### 验收

- 后续 Agent 不需要重新决定 tool profile、OAuth、telemetry 和 desktop 策略。
- 文档没有把 fake readonly 描述成安全功能。
- 没有修改运行时代码。

---

## Phase 03：移植 Settings Store、Settings Definition、Workspace Catalog、Secret Vault

### 目标

先移植与 MCP transport/OAuth handler 解耦的持久化基础模块。

### 源文件（从 WIP 分支读取）

- `coding_tools_mcp/settings_store.py`
- `coding_tools_mcp/settings_definition.py`
- `coding_tools_mcp/workspace_catalog.py`
- `coding_tools_mcp/secret_vault.py`
- `docs/oauth-agent-workspace-migration.md`
- 与这些模块直接相关的测试段落

使用 `git show wip/pre-upstream-v0.2.2:<path>` 读取已提交版本。不得从脏工作树猜测内容。

### 目标侧只读取

- `coding_tools_mcp/envutils.py`
- `coding_tools_mcp/project_context.py`
- `pyproject.toml`
- `tests/compliance/test_runtime_helpers.py` 中配置和 workspace helper 测试

### 允许修改

- 上述四个新模块
- 对应的新测试文件
- 必要的 package import
- 集成契约文档中的已实现标记
- handoff

### 禁止修改

- OAuth HTTP handler
- `Runtime` 工具调用逻辑
- Admin/WebUI
- tool registry
- desktop client

### 实现步骤

1. 先移植数据类、异常类型和纯函数。
2. 统一配置目录解析，复用上游 `envutils.py`；不要复制第二套 env 解析。
3. Settings Store 必须使用原子替换，并保留 schema version/migration 入口。
4. Secret Vault 必须只保存 secret material，普通 settings 只保存 reference。
5. Workspace Catalog 必须验证：ID 唯一、root 可规范化、default 唯一、禁用项行为明确。
6. `settings_definition.py` 删除 `TOOL_PROFILE_CHOICES` 作为新运行时设置；旧值只进入 migration warning。
7. 不在本阶段把 Workspace Catalog 注入 Runtime。

### 最小测试

- settings JSON round-trip
- active/persisted 比较
- 原子写入失败不破坏旧文件
- schema migration
- workspace ID/root/default 规范化
- duplicate ID
- 无效路径
- vault 加密/解密和错误 key
- settings 响应不包含 secret material

### 验证命令

先运行新模块测试，再运行：

```powershell
uv run python -m unittest tests.compliance.test_runtime_helpers
```

```powershell
uv run python -m tests.compliance.runner --suite security
```

### 提交

建议拆成两个实现提交：

```text
feat(config): port settings and workspace catalog
feat(secrets): port encrypted secret vault
```

每个提交前都运行相关测试。完成后写 Phase 03 handoff。

### 验收

- 四个模块可以独立测试。
- 没有改动 `server.py` 大段逻辑。
- 不再把 tool profile 当作有效新设置。
- 没有真实用户配置或 secret fixture。

---

## Phase 04：移植 OAuth Store，但暂不接入 HTTP

### 目标

移植并验证持久化数据层，不改变上游 OAuth 网络行为。

### 只读取

- WIP `coding_tools_mcp/oauth_store.py`
- WIP `tests/compliance/test_oauth_persistence.py` 中 store-only 测试
- 已移植的 `secret_vault.py`
- 上游 `coding_tools_mcp/oauth.py` 的 dataclass 和 registry 接口
- WIP `docs/oauth-agent-workspace-management-plan.md` 的数据表章节；通过 `git show wip/pre-upstream-v0.2.2:<path>` 读取

### 允许修改

- `coding_tools_mcp/oauth_store.py`
- 新的 `tests/test_oauth_store.py` 或等价聚焦测试
- 必要的小型 migration helper
- handoff

### 禁止修改

- `server.py` OAuth endpoints
- `oauth.py` 协议行为
- Admin API
- WebUI

### 数据表必须覆盖

- clients
- grants
- access tokens
- refresh token families
- refresh tokens
- signing keys
- audit events

### 安全要求

- 不保存 bearer token 原文。
- refresh token 只保存带 pepper 的摘要。
- signing secret 只通过 vault reference 关联。
- revoke 操作幂等。
- refresh rotation 必须能检测旧 token 重用。
- migration 可重复运行。
- SQLite transaction 失败不能留下半迁移 schema。

### 测试

- 创建/查询/禁用 Client
- Grant 创建和撤销
- Access Token jti 查询和撤销
- Signing key active→retired→revoked
- Refresh Token 正常轮换
- 旧 Refresh Token 重用导致 family revoke
- DB reopen 后数据仍在
- migration 重跑
- 并发或事务回滚的最小覆盖

### 提交

```text
feat(oauth): port persistent authorization store
```

完成后写 Phase 04 handoff。

### 验收

- OAuth Store 测试独立通过。
- 上游 OAuth endpoint 行为完全未变。
- `git diff` 中没有 WebUI 或 transport 文件。

---

## Phase 05：将持久化 OAuth 接入上游 OAuth 协议实现

### 目标

把 Phase 04 store 接入上游 `oauth.py` 和 HTTP handler，同时保留全部 v0.2.2 修复。

### 只读取

- `coding_tools_mcp/oauth.py`
- `coding_tools_mcp/oauth_store.py`
- `coding_tools_mcp/secret_vault.py`
- `coding_tools_mcp/server.py` 中以下符号附近：
  - OAuth config 构造
  - registration endpoint
  - authorization endpoint
  - token endpoint
  - bearer validation
- WIP `server.py` 中 OAuth token/create/validate/refresh 相关符号，不通读全文件
- 上游 OAuth 测试
- WIP OAuth persistence 测试

### 允许修改

- `coding_tools_mcp/oauth.py`
- `coding_tools_mcp/server.py` 中 OAuth composition/handler 的小范围代码
- OAuth tests
- OAuth docs
- handoff

### 必须采用的设计

1. `OAuthClientRegistry` 通过 adapter 或 store-backed implementation 持久化，不在 handler 中散落 SQL。
2. `OAUTH_GRANT_TYPES_SUPPORTED` 是 metadata 和 DCR narrowing 的共同来源。
3. 只有 refresh endpoint 分支完成且测试通过后，才加入 `refresh_token`。
4. access token 必须包含并验证：`iss`、`aud`、`client_id`、`iat`、`exp`、`jti`、`kid`。
5. Store 不可用时必须 fail closed，不能静默退回无状态宽松验证。
6. 旧 token 兼容范围必须由 migration 文档明确，不得无限兼容无 `jti` token。
7. v0.2.2 的 24 小时默认 TTL 和最大值保持不变。

### 必测场景

- DCR 请求 `authorization_code + refresh_token` 时不会因额外请求值失败。
- 尚未实现 refresh 时，响应只 advertised 已实现类型。
- 实现 refresh 后，metadata、DCR response、token endpoint 三者一致。
- redirect URI exact match。
- PKCE S256 成功/失败。
- confidential/public client auth method。
- server restart 后 client/grant/key 可恢复。
- signing key rotation 后旧未过期 token 可按策略验证。
- revoked key/token/client/grant 被拒绝。
- refresh reuse detection。
- token secret/vault 缺失时启动失败且错误清晰。

### 验证命令

先运行聚焦 OAuth 测试，然后：

```powershell
uv run python -m tests.compliance.runner --suite mcp-contract
```

```powershell
uv run python -m tests.compliance.runner --suite security
```

```powershell
uv run python -m unittest tests.compliance.test_oauth_persistence
```

### 提交拆分

```text
feat(oauth): back dynamic registration with persistent clients
feat(oauth): persist grants and access-token revocation state
feat(oauth): restore refresh-token rotation
feat(oauth): restore signing-key lifecycle
```

每个提交都必须保持 OAuth 聚焦测试通过。

### 验收

- 不回归 v0.2.2 DCR 修复。
- 协议 advertised 能力与 endpoint 实现一致。
- restart persistence 有测试证据。
- 无 secret 出现在日志、响应和 Git fixture。

---

## Phase 06：多 Workspace 与每 Session Runtime 绑定

### 目标

将 Workspace Catalog 接入上游独立 HTTP runtime，不破坏会话隔离。

### 只读取

- `workspace_catalog.py`
- `project_context.py`
- `server.py` 中 Runtime 构造、HTTP initialize/session registry、workspace/path adapter
- WIP `server.py` 中 Workspace Catalog 使用符号
- WIP workspace/session 测试
- 上游 session、path security、runtime semantics 测试

### 允许修改

- Runtime 构造和 session 初始化的聚焦代码
- Workspace selection adapter
- workspace/session tests
- 相关契约文档
- handoff

### 设计要求

1. 一个 HTTP Session 一旦绑定 Workspace，普通工具调用不得无审计地切换到其他 root。
2. stdio 仍有一个明确 default Workspace。
3. OAuth Agent/Client 到 Workspace ID 的映射必须在 initialize 或授权结果中解析。
4. path confinement、symlink escape、cwd、exec workdir 都使用当前 Session 的 Workspace adapter。
5. 不同 Session 不共享 cwd、process、retained output。
6. Workspace 禁用后：新 Session 拒绝；旧 Session 行为必须写入契约并测试。
7. 项目指令从绑定 Workspace 加载，而不是服务器启动目录。

### 必测场景

- 两个 Session 绑定两个 Workspace，读取同名文件得到不同内容。
- Session A 无法通过绝对路径、`..`、symlink 访问 B。
- `set_default_cwd` 只影响当前 Session。
- 进程 session 不跨 MCP Session。
- retained output 不跨 MCP Session。
- 项目指令按 Workspace 独立加载。
- Agent 没有有效 Workspace 映射时 fail closed。
- Workspace 被禁用后的新连接行为。

### 验证

```powershell
uv run python -m tests.compliance.runner --suite runtime-semantics
```

```powershell
uv run python -m tests.compliance.runner --suite security
```

```powershell
uv run python -m tests.compliance.runner --suite e2e
```

### 提交

```text
feat(workspace): bind MCP sessions to catalog workspaces
```

完成后写 Phase 06 handoff。

---

## Phase 07：移植 upstream MCP Gateway

### 目标

在上游固定工具目录语义下恢复嵌套 MCP server/gateway 能力。

### 只读取

- WIP `coding_tools_mcp/upstream.py`
- WIP `tests/compliance/test_upstream_gateway.py`
- 上游 `server.py` 中 tool registry、`tools/list`、`tools/call`
- 上游 `tool_results.py`
- 集成契约的工具目录章节

### 允许修改

- `coding_tools_mcp/upstream.py`
- tool registry/composition 的小范围代码
- upstream gateway tests
- docs
- handoff

### 必须删除或重构的旧逻辑

- `tool_profile` 参数传播
- read-only profile 隐藏工具
- compat-readonly-all annotations 改写

### 新逻辑要求

1. upstream tool 使用稳定 namespace。
2. 本地工具优先级和重名拒绝规则明确。
3. upstream schema 原样或可证明等价地暴露。
4. `structuredContent` 优先保留。
5. `content` 只做边界规范化，不假定它是 JSON。
6. upstream timeout、disconnect、invalid response 变为结构化错误。
7. upstream allowlist/enable 状态与 tool visibility 规则写入文档。
8. gateway 不得突破当前 Workspace 和 permission policy。

### 必测场景

- 初始化并导入 upstream tools。
- namespace collision。
- schema 保留。
- structured success/error 转发。
- upstream 断开和 timeout。
- tool enable/disable。
- Session 隔离。
- 不存在任何 `tool_profile` 控制路径。

### 验证

```powershell
uv run python -m unittest tests.compliance.test_upstream_gateway
```

```powershell
uv run python -m tests.compliance.runner --suite mcp-contract
```

```powershell
uv run python -m tests.compliance.runner --suite tool-golden
```

### 提交

```text
feat(gateway): port upstream MCP composition
```

完成后写 Phase 07 handoff。

---

## Phase 08：移植 Admin 后端和管理 API

### 目标

恢复管理能力，但暂不移植前端页面。

### 只读取

- WIP `coding_tools_mcp/admin.py`
- WIP `coding_tools_mcp/webui.py` 中路由入口，不读取前端大文件
- WIP `tests/compliance/test_mcp_admin.py`
- 已集成的 settings/workspace/oauth/gateway 公共接口
- 上游 HTTP transport 和 auth boundary

### 允许修改

- `admin.py`
- `webui.py` 的后端入口骨架
- `server.py` 的 Admin composition 小范围代码
- Admin tests
- Admin API docs
- handoff

### API 要求

- 所有管理写操作要求明确 Admin auth。
- 设置响应脱敏。
- OAuth key/client/grant/token 操作使用 ID，不接受 secret 回显。
- destructive action 返回影响对象数量和审计事件 ID。
- active settings 与 persisted settings 分开返回。
- restart-required fields 明确列出。
- Workspace 检查不允许任意未授权文件读取。
- CORS/allowed origins 使用统一 settings validation。

### 必测场景

- 未认证访问拒绝。
- 只读状态 API。
- settings validate/save/restart pending。
- secret 字段不回显。
- OAuth client/grant/token/key 管理。
- Workspace add/disable/default/check。
- upstream server 配置读取和错误状态。
- 并发保存或 stale update 行为。

### 验证

```powershell
uv run python -m unittest tests.compliance.test_mcp_admin
```

```powershell
uv run python -m tests.compliance.runner --suite security
```

```powershell
uv run python -m tests.compliance.runner --suite mcp-contract
```

### 提交

```text
feat(admin): port authenticated management API
```

完成后写 Phase 08 handoff。

---

## Phase 09：移植 transcript、Codex session 和聊天持久化

### 目标

恢复聊天与会话数据能力，并与 Admin API 解耦。

### 只读取

- WIP `transcript.py`
- WIP `codex_sessions.py`
- WIP `chat_cli.py`
- Admin tests 中仅聊天/session 相关测试
- 当前 Admin service 接口

### 允许修改

- 上述三个模块
- Admin 的相关 service wiring
- 聚焦测试
- docs
- handoff

### 实现要求

- 文件扫描有边界、数量和大小限制。
- 解析损坏 session 时返回单项错误，不拖垮整个列表。
- 删除/清理操作必须精确到 ID，并需要 Admin auth。
- 不把完整聊天内容写入普通日志或 telemetry。
- Workspace identity 进入查询和缓存键。
- Windows 路径和编码有测试。

### 测试

- session discovery
- transcript parsing
- malformed/partial JSONL
- pagination
- Workspace 隔离
- chat project/context/message CRUD
- delete/clear 权限
- 大文件和无效编码限制

### 提交

```text
feat(chat): port transcript and session persistence
```

完成后写 Phase 09 handoff。

---

## Phase 10：移植 WebUI 源码并重新构建静态产物

### 目标

恢复 Admin WebUI，并适配上游固定工具目录和新设置契约。

### 源文件

- WIP `webui/src/admin.html`
- WIP `webui/src/admin.css`
- WIP `webui/src/admin.js`
- WIP `webui/src/settings-copy.js`
- WIP `webui/src/settings-model.js`
- WIP `webui/src/settings-page.js`
- WIP `webui/src/workspace-editor.js`
- WIP `webui/scripts/build.mjs`
- WIP `webui/package.json`
- WIP `webui/tests/*`

### 目标侧只读取

- Phase 08 Admin API 文档和 tests
- 上游 telemetry 文档
- 上游 desktop client README，仅了解职责边界

### 允许修改

- `webui/**`
- `coding_tools_mcp/webui_dist/**`，但只能由 build 生成
- `coding_tools_mcp/webui.py` 的静态资源映射
- WebUI docs/tests
- handoff

### UI 必须调整

1. 删除 `TOOL_PROFILE_CHOICES` 和 tool profile 下拉框。
2. “仅查看”不得继续承诺隐藏 mutation tools。
3. permission mode 文案与上游实际行为一致。
4. fake readonly 放在高级危险兼容区，并展示明确警告。
5. telemetry 展示 enabled/off/debug 状态和关闭方法。
6. active/persisted/pending restart 状态可见。
7. Workspace editor 保持单一 default。
8. OAuth secrets 永不回显。
9. destructive OAuth/Admin 操作二次确认并显示影响范围。
10. 前端只消费 Phase 08 的稳定 API，不直接拼接内部数据库字段。

### 构建顺序

1. 先运行纯模型测试：

   ```powershell
   npm --prefix webui test
   ```

2. 再运行 build：

   ```powershell
   npm --prefix webui run build
   ```

3. 确认当前目录仍是仓库根目录，并确认 `webui_dist` 只包含构建预期变化。

4. 再次运行 `npm --prefix webui test`，防止 build script 修改源文件。

5. 运行 Admin Python tests。

### 禁止事项

- 不直接手改 `webui_dist` 修复 build 后差异。
- 不使用 `latest` 依赖升级解决无关问题；依赖版本策略单独记录。
- 不把桌面客户端和 WebUI 合并成同一应用。

### 提交

```text
feat(webui): port modular administration interface
build(webui): regenerate packaged assets
```

如果 build 产物和源文件必须原子一致，可以放在同一提交，但 handoff 必须说明。

---

## Phase 11：合并 packaging、desktop、npm、telemetry 和远程 sandbox 配置

### 目标

确保本地扩展不会破坏上游产品化能力。

### 只读取

- 当前 `pyproject.toml`
- 上游 `apps/desktop-client/**`
- 上游 `npm/coding-tools-mcp/**`
- 上游 `coding_tools_mcp/telemetry.py`
- 上游 `cloudflare/sandbox-control/**`
- 上游 release workflows
- 当前 WebUI package/build 配置

### 允许修改

- `pyproject.toml`
- `coding_tools_mcp/__init__.py`
- package-data 配置
- 必要的 packaging tests
- telemetry/Admin 状态接线
- docs
- handoff

### `pyproject.toml` 合并要求

必须同时保留：

- 上游 `desktop` optional dependencies。
- 上游 `coding-tools-mcp-desktop` entry point。
- `where = [".", "apps/desktop-client"]`。
- `coding_tools_mcp*` 和 `mcp_desktop_client*` package discovery。
- desktop locale package data。
- 本地 `coding_tools_mcp = ["webui_dist/*"]` package data。
- 上游 dev/image extras。

### Telemetry 要求

- 不在本阶段改变上游默认开关。
- Admin 状态中只显示模式和文档入口。
- 不增加 path、Workspace ID、Agent ID、Client ID、command、arguments、file content 等字段。
- 运行上游 telemetry privacy tests。

### Desktop 要求

- desktop 仍可独立启动。
- desktop 自有 profile/secret storage 不被 Admin settings 覆盖。
- server CLI 参数变化不破坏 desktop runtime 启动。

### npm launcher 要求

- 参数和 stdio 原样转发。
- server 版本 pin 行为保持。
- fatal signal 行为保持。

### Cloudflare/release 要求

- 不部署。
- 不修改 secrets。
- 只运行 dispatch contract 和 release metadata 检查。

### 验证

```powershell
uv sync --extra dev --extra desktop
```

```powershell
uv run python -m unittest tests.test_desktop_client
```

```powershell
uv run python -m unittest tests.test_telemetry
```

```powershell
uv run python scripts/check_dispatch_inputs.py
```

在仓库根目录运行 npm launcher 检查：

```powershell
npm --prefix npm/coding-tools-mcp test
```

```powershell
npm --prefix npm/coding-tools-mcp pack --dry-run --json
```

### 提交

```text
build(packaging): include admin webui with upstream desktop client
feat(admin): expose privacy-safe telemetry status
```

仅在确有代码变化时创建对应提交。不要为了匹配计划而制造空提交。

---

## Phase 12：文档、迁移说明和 schema drift 对齐

### 目标

让文档描述最终行为，删除或更新已经失效的 v0.1/tool profile 内容。

### 只读取

- `README.md`
- `README.zh-CN.md`
- `CHANGELOG.md`
- `docs/runtime-contract-v0.2.md`
- `docs/remote-mcp.md`
- `docs/tools-and-schemas.md`
- `docs/mcp-client-config.md`
- `docs/telemetry.md`
- WIP 分支中的本地 OAuth/Workspace migration 文档；通过 `git show wip/pre-upstream-v0.2.2:<path>` 读取
- schema drift/docs required tests

### 允许修改

- 文档和文档测试
- `CHANGELOG.md` 的 `Unreleased` 部分
- handoff

### 文档必须说明

- MCP 版本和 session semantics。
- 固定工具目录。
- permission mode 与 fake readonly 的真实含义。
- OAuth persistence、refresh、key rotation。
- DCR narrowing 行为。
- Workspace/Agent 绑定。
- Admin WebUI。
- desktop 与 WebUI 的职责差异。
- telemetry 默认行为和关闭方法。
- 从旧 `tool_profile` 和旧 Workspace settings 迁移。
- 回滚时如何保留 OAuth DB、vault 和 signing keys。

### 删除/替换策略

- 上游已经删除 `docs/profile-v0.1.md`；不要简单恢复为主契约。
- 有用历史内容迁入 migration/history 文档。
- 当前运行契约只以 v0.2 文档为准。

### 验证

```powershell
uv run python -m tests.compliance.runner --suite docs-required
```

```powershell
uv run python -m tests.compliance.runner --suite schema-drift
```

### 提交

```text
docs: document upstream v0.2.2 integration and migration
```

完成后写 Phase 12 handoff。

---

## Phase 13：完整回归、构建产物和安全审计

### 目标

不再增加功能，只验证最终集成候选。

### 允许修改

- 由测试明确发现的问题修复。
- 测试报告。
- handoff。

任何修复必须使用独立 commit，不能混入一个笼统“fix tests”大提交。

### 执行前检查

```powershell
git status --short --branch
```

预期干净。

```powershell
git log --oneline --decorate -20
```

确认所有 Phase 实现提交存在。

### Python 全量门禁

依次单独运行：

```powershell
uv run python -m ruff check --exclude benchmarks/dogfood --ignore=E501 coding_tools_mcp apps/desktop-client/mcp_desktop_client tests benchmarks
```

```powershell
uv run python -m mypy --python-version 3.11 --disable-error-code union-attr --disable-error-code assignment --disable-error-code arg-type --disable-error-code no-untyped-def coding_tools_mcp benchmarks/mcp_http.py benchmarks/runtime_latency.py benchmarks/real_workloads.py
```

```powershell
uv run python -m unittest discover -s tests -p "test_*.py"
```

```powershell
uv run python -m tests.compliance.runner --suite mcp-contract
```

```powershell
uv run python -m tests.compliance.runner --suite tool-golden
```

```powershell
uv run python -m tests.compliance.runner --suite security
```

```powershell
uv run python -m tests.compliance.runner --suite runtime-semantics
```

```powershell
uv run python -m tests.compliance.runner --suite e2e
```

```powershell
uv run python -m tests.compliance.runner --suite dogfood
```

```powershell
uv run python -m tests.compliance.runner --suite docs-required
```

```powershell
uv run python -m tests.compliance.runner --suite schema-drift
```

### JavaScript 门禁

在仓库根目录运行 WebUI 门禁：

```powershell
npm --prefix webui test
```

```powershell
npm --prefix webui run build
```

在仓库根目录运行 npm launcher 门禁：

```powershell
npm --prefix npm/coding-tools-mcp test
```

```powershell
npm --prefix npm/coding-tools-mcp pack --dry-run --json
```

### 最终 Git 检查

```powershell
git diff --check
```

```powershell
git status --short
```

```powershell
git diff origin/main...HEAD --stat
```

```powershell
git diff origin/main...HEAD --name-status
```

### 人工验收场景

必须至少记录：

1. stdio initialize 和一个只读工具调用。
2. HTTP initialize、Session ID、后续工具调用、DELETE session。
3. 两 Workspace 两 Session 隔离。
4. DCR + PKCE 登录。
5. 重启后 Client/Grant/Signing Key 保留。
6. Refresh Token 轮换和重用拒绝。
7. Admin 登录、settings 保存、pending restart。
8. WebUI Workspace 编辑。
9. upstream gateway 工具调用。
10. desktop client 能启动并创建 profile；不得在测试中使用真实生产 secret。

### 安全审计

- Git diff 不含 token/secret/DB。
- telemetry payload 不含路径、Workspace ID、Agent ID、命令或文件内容。
- remote URL 不含凭据。
- public HTTP 无 auth 时仍遵守上游安全限制。
- fake readonly 警告仍存在。
- Admin destructive APIs 需要认证。

### 验收

- 所有门禁通过，或只有 Phase 01 已记录且确认仍存在的 baseline skip。
- worktree 干净。
- 无未解释生成文件。
- Phase 13 handoff 包含完整命令和 exit code。

---

## Phase 14：版本、推送与切换准备

### 目标

准备交付，但任何外部写操作必须得到用户明确授权。

### 前置条件

- Phase 13 complete。
- 用户确认 fork 版本号。
- 用户确认 telemetry 产品策略是否保持上游默认。
- 用户确认是否推送到 `onestao`。

### 版本建议

不要继续使用 `0.2.2` 作为含大量本地扩展的发布版本。可选：

- `0.3.0.dev0`：尚未正式发布的集成版本；推荐。
- `0.2.2.post1`：只适用于极小下游补丁，不适合当前大量功能。
- 自定义 fork 版本：必须符合 PEP 440，并同步 `pyproject.toml`、`__init__.py`、CHANGELOG。

默认推荐 `0.3.0.dev0`，但 Agent 不得未经用户确认直接修改版本。

### 用户授权后才可执行

```powershell
git push -u onestao integration/upstream-v0.2.2
```

不得 push 到 `origin`。

建议先在用户 fork 创建 PR：

```text
integration/upstream-v0.2.2 -> onestao/main
```

### 不执行的操作

- 不自动替换本地主 `main`。
- 不删除 `wip/pre-upstream-v0.2.2`。
- 不删除 `backup/local-before-v0.2.2`。
- 不发布 PyPI/npm。
- 不创建 tag。
- 不部署 Cloudflare Worker。

这些操作必须分别得到用户授权。

### 最终交付报告必须包含

- 最终 branch/HEAD。
- 相对 `origin/main` 的提交列表。
- 功能保留矩阵。
- 所有验证命令和结果。
- 未解决风险。
- 版本决策。
- push/PR 状态。
- backup/WIP 分支位置。
- 回滚步骤。

---

## 5. 功能保留矩阵

每个相关 Phase 完成后更新此表的状态。状态只允许：`pending`、`implemented`、`verified`、`blocked`。

| 能力 | 来源 | 实现 Phase | 验证 Phase | 状态 |
| --- | --- | ---: | ---: | --- |
| MCP 2025-11-25 + 2025-06-18 | 上游 | 01 | 13 | pending |
| 独立 HTTP Session runtime | 上游 | 01/06 | 13 | pending |
| 原子 apply_patch | 上游 | 01 | 13 | pending |
| 有界工具结果和 next_action | 上游 | 01 | 13 | pending |
| 自动项目指令加载 | 上游 | 01/06 | 13 | pending |
| OAuth DCR/PKCE | 上游 | 01/05 | 13 | pending |
| DCR metadata narrowing 修复 | 上游 v0.2.2 | 01/05 | 13 | pending |
| OAuth 24h TTL | 上游 v0.2.2 | 01/05 | 13 | pending |
| OAuth 持久化 | 本地 | 04/05 | 13 | pending |
| Refresh Token rotation | 本地 | 04/05 | 13 | pending |
| Signing key lifecycle | 本地 | 04/05 | 13 | pending |
| Secret Vault | 本地 | 03 | 13 | pending |
| Workspace Catalog | 本地 | 03/06 | 13 | pending |
| Agent→Workspace 绑定 | 本地 | 06 | 13 | pending |
| upstream MCP gateway | 本地 | 07 | 13 | pending |
| Admin API | 本地 | 08 | 13 | pending |
| transcript/chat/Codex sessions | 本地 | 09 | 13 | pending |
| Admin WebUI | 本地 | 10 | 13 | pending |
| Desktop client | 上游 | 01/11 | 13 | pending |
| npm launcher | 上游 | 01/11 | 13 | pending |
| telemetry | 上游 | 01/11 | 13 | pending |
| Cloudflare sandbox control | 上游 | 01/11 | 13 | pending |
| release workflow | 上游 | 01/11 | 13 | pending |

---

## 6. Phase 间依赖关系

```text
00 保护现场
 ↓
01 上游基线
 ↓
02 集成契约
 ↓
03 Settings / Workspace / Vault
 ↓
04 OAuth Store
 ↓
05 OAuth Runtime
 ↓
06 Session / Workspace
 ↓
07 Upstream Gateway
 ↓
08 Admin API
 ↓
09 Chat / Transcript
 ↓
10 WebUI
 ↓
11 Packaging / Desktop / npm / Telemetry / Cloudflare
 ↓
12 Docs / Schema Drift
 ↓
13 Full Validation
 ↓
14 Version / Push Preparation
```

不得跳过 00～06。Phase 07 和 Phase 09 在代码层面可以相对独立，但由于执行 Agent 上下文有限，本计划仍要求串行执行，避免同时修改 `server.py` 和 Admin composition。

---

## 7. 每个 Agent 的最小启动提示词

后续调度 Agent 时，使用以下模板，不要把整段历史对话塞入上下文：

```text
你正在执行 coding-tools-mcp 上游 v0.2.2 集成计划的 Phase NN。

仓库：G:\LLM\coding-tools-mcp
集成 worktree：G:\LLM\coding-tools-mcp\.worktrees\upstream-v0.2.2-integration
分支：integration/upstream-v0.2.2

必须读取：
1. docs/upstream-v0.2.2-integration-agent-execution-plan.md 的第 1～3 节；
2. 同文档的 Phase NN；
3. docs/integration-handoffs/phase-(NN-1).md；
4. Phase NN 明确列出的文件。

一次只执行 Phase NN。不要继续下一 Phase。
遵守 PowerShell、worktree、secret、Git 禁止事项。
修改后运行 Phase 指定测试，创建实现 commit，再创建 handoff commit。
若前置条件、测试或安全检查不满足，写 blocked handoff 并停止。
```

---

## 8. 最终 Definition of Done

只有同时满足以下条件，才允许宣布集成完成：

- 集成分支基于精确的上游 v0.2.2。
- 上游核心 runtime 没有被旧 `server.py` 整体覆盖。
- 上游协议、patch、process、result、session、安全修复均保留。
- 本地 OAuth 持久化、refresh、key lifecycle、Workspace、Admin、Gateway、Chat、WebUI 均有测试。
- 旧 tool profile 不再控制工具目录。
- fake readonly 没有被描述成安全功能。
- telemetry 没有扩展收集敏感字段。
- desktop、npm launcher、Cloudflare contract、release metadata 检查通过。
- Python、JavaScript、协议、安全、runtime、e2e、docs/schema 测试通过。
- 没有真实 token、secret、OAuth DB、vault 或用户数据进入提交。
- integration worktree 干净。
- 每个 Phase 都有 handoff。
- 用户明确批准最终版本和 push 目标。
- backup 和 WIP 分支仍然存在，可用于回滚。

任何一项缺失，都只能报告“集成未完成”，不能用“基本完成”替代。
