# Upstream Tool Broker — v6 Stable-Catalog 分步实施任务书

> 状态：**Implementation and validation complete through T12 / not merge-ready until commit-chain review and Git clean**
>
> 本任务书是 v6 Broker 方案针对当前 `v0.2.2` 固定工具目录契约的正式适配版。
> 它取代此前正文中依赖 `tool_profile`、动态 `start_server()` / `stop_server()`、
> 运行时改变 `tools/list` 的执行要求。
>
> **执行优先级：本任务书正文 > T00 handoff > 历史归档。**
> 原附录 A 已移至 `docs/archive/upstream-tool-broker-v6-appendix-a-historical.md`；其中任何 `tool_profile`、动态生命周期或 `listChanged=true` 伪代码均为失效历史材料，禁止作为执行规格。

---

## 0. 契约修订决定

### 0.1 决定来源

T00 在当前目标工作树验证到：

- `docs/integration-contract-v0.2.2.md` 将本地工具目录定义为固定目录；
- legacy `full`、`read-only`、`compat-readonly-all` 仅是设置迁移输入；
- `tool_profile` 不得控制 `tools/list`、Gateway 可见性、路由或 annotations；
- 每个 `Runtime` 在初始化时发现 upstream，并冻结定义与路由直至该 Runtime 关闭；
- Admin Gateway 写入只持久化配置并返回 `restart_required`；
- 不存在运行时 start、stop、reload、profile 或 list-changed 路径；
- `tools.listChanged=false` 必须持续真实。

因此本任务书选择 T00 handoff 推荐的：

> **Stable-catalog adaptation**

不恢复已移除的 profile 产品行为，不修改 v0.2.2 的固定目录与 Runtime 冻结契约。

### 0.2 适配后的总目标

将“所有外部 MCP 工具都直接进入 `tools/list`”改造为：

- 五个本地 Broker 工具永久登记在 `TOOL_REGISTRY`；
- upstream 配置、catalog、direct/broker 分流在 Runtime 构造前解析；
- 每个 Runtime 初始化时完成一次 upstream discovery；
- 该 Runtime 的 direct definitions、catalog、路由与 clients 随后保持冻结；
- broker-only 工具的完整 Schema 不进入 `tools/list`；
- Agent 通过 search → describe → readonly/mutating call 按需调用；
- Runtime 运行期间不改变工具目录，不发送 `tools/list_changed`；
- Admin 配置更新只影响之后新建的 Runtime 或服务重启后的 Runtime；
- 旧 Runtime 继续使用自己冻结的 catalog 和 clients，直到关闭。

### 0.3 固定本地 Broker 工具

以下五个工具最终必须无条件存在于本地 `TOOL_REGISTRY`，不得根据：

- upstream 配置是否为空；
- catalog 当前是否为空；
- legacy profile；
- HTTP session；
- workspace；

进行隐藏或动态注册：

```text
upstream_tool_search
upstream_tool_describe
upstream_tool_call
upstream_tool_call_mutating
upstream_result_fetch
```

catalog 为空时：

- search 返回空结果；
- describe/call 返回结构化 not-found；
- result_fetch 返回结构化 not-found/no-session；
- `tools/list` 仍保持固定。

### 0.4 mutating 工具的安全语义

当前 Runtime 不存在真正的 read-only profile，因此：

- `upstream_tool_call_mutating` **始终在固定目录中可见**；
- 它的真实 annotations 必须是 `readOnlyHint=false`、`destructiveHint=true`、
  `openWorldHint=true`；
- `upstream_tool_call` 只执行 effective risk 为 readonly 的目标；
- `upstream_tool_call_mutating` 只执行 effective risk 为 mutating/unknown 的目标；
- unknown annotations 默认 mutating；
- 推荐 mutating 调用前执行 describe；调用必须提交由 search 或 describe 返回的匹配 public schema digest。当前 digest 校验是无状态 Schema 一致性检查，不证明本 session 曾执行 describe；
- Broker 在网关侧验证公开 `inputSchema`；
- 不得把 fake-readonly annotation override 当作安全边界；
- 不得声称“工具未出现在目录中所以安全”；
- 不在本任务中发明新的 `tool_profile` 或产品级审批系统。

现有 HTTP authentication、Runtime permission policy、客户端基于真实 annotations 的确认流程
继续生效，但 Gateway 文档不得把这些描述为远端副作用的绝对安全边界。

### 0.5 不在本轮实现

- 动态 `tools/listChanged`；
- Runtime 内 start/stop/reload upstream；
- workspace 切换时改变 direct 工具；
- live profile filtering；
- 向量检索；
- 完整 `$ref` resolver；
- OAuth principal + session 复合 ResultStore owner；
- ResultStore 持久化；
- `expose_mode=auto`。

---

## 1. 短上下文执行协议

### 1.1 一次只执行一个任务卡

每个 Agent/session 只执行一个 `Txx`：

1. 确认当前独立工作树、分支、HEAD 和 clean 状态；
2. 读取当前任务卡；
3. 读取上一任务 handoff；
4. 只读取任务卡指定的代码文件或局部；
5. 不提前实现下一任务；
6. 运行本任务定点测试；
7. 一个任务一个提交；
8. 写 handoff 后停止。

禁止把整份附录 A 全量塞入 Agent 上下文。

### 1.2 每次 Agent 的最小输入

```text
1. 当前任务卡全文；
2. 上一任务 handoff；
3. git status + HEAD；
4. 任务卡指定的 2–5 个文件或局部；
5. 本任务失败测试输出；
6. 必要时只读取附录 A 指定算法章节。
```

### 1.3 强制停止条件

遇到以下任一情况必须停在当前任务：

- 需要恢复 `tool_profile`；
- 需要添加动态 `start_server()` / `stop_server()`；
- 需要把 `listChanged` 改为 true；
- 需要让 Admin 配置写入立即改变现有 Runtime；
- 需要改变本地 `TOOL_REGISTRY` 的固定目录原则；
- 需要破坏 v0.2.2 规范测试才能继续；
- 发现上一任务的数据结构无法满足本任务且无法局部兼容。

### 1.4 提交规则

- 开始时 worktree clean；
- 不混入其他分支或原目录修改；
- 不恢复用户 stash；
- 不推送远程，除非用户明确要求；
- 测试失败不能标记 complete；
- 文档与代码在对应任务中同步；
- 不用大范围顺手重构掩盖本任务 diff。

### 1.5 Handoff 模板

保存到：

```text
docs/upstream-broker-handoffs/Txx-<slug>.md
```

模板：

```markdown
# Txx Handoff — <title>

Status: complete | blocked | partial
Parent HEAD: <sha before task>
Branch: feat/upstream-tool-broker-v6
Worktree: clean | dirty

Implemented:
- ...

Files changed:
- ...

Tests:
- command: ...
  result: PASS | FAIL

Stable-catalog compatibility:
- no tool_profile control path: yes/no
- no dynamic start/stop/reload: yes/no
- listChanged remains false: yes/no
- existing Runtime snapshot remains immutable: yes/no

Known limitations:
- ...

Next task prerequisites:
- ...

Do not redo:
- ...
```

---

## 2. 全局强制架构约束

### 2.1 Runtime 快照只在构造期发布一次

采用：

```python
@dataclass(frozen=True)
class UpstreamRegistryState:
    all_tools: Mapping[str, UpstreamTool]
    direct_tool_names: tuple[str, ...]
    catalog: Mapping[str, UpstreamToolCatalogEntry]
    search_index: CatalogSearchIndex | None
    clients: Mapping[str, BaseUpstreamClient]
```

要求：

- `all_tools`、direct names、catalog、index、clients 来自同一次 discovery；
- 使用 `MappingProxyType` 或等价只读映射；
- 构造阶段先在局部初始化全部 enabled clients 与 tools；
- 任一配置/名称碰撞/初始化错误按当前 tolerant snapshot 规则记录；
- 完成后一次赋值给 `self._state`；
- 正常 Runtime 生命周期不再替换 `_state`；
- `close()` 只负责阻止新调用并关闭该 Runtime 自己的 clients；
- 不提供 public start/stop/restart API；
- Admin 配置写入不持有或修改现有 Runtime 的 `_state`。

由于不存在 live swap，本轮不需要 generation CAS 或动态 index replacement。

### 2.2 client 与关闭并发

每个 Runtime 独占自己的 upstream clients。

最低要求：

- `call_tool_raw()` 与 `close()` 不能造成中途破坏或数据竞争；
- 已进入调用临界区的调用可安全完成；
- close 后的新调用返回 retryable `UPSTREAM_NOT_AVAILABLE`；
- close 幂等；
- HTTP 与 stdio transport 保持当前协议行为；
- 不通过新增动态 manager lifecycle 来解决并发。

可采用 client lifecycle lock/lease，但必须最小改动，并与 transport 已有 request lock 顺序一致。

### 2.3 Schema 与公开元数据

- raw definition 仅服务端诊断使用；
- public definition 面向 `tools/list` 和 describe；
- sanitizer 是 untrusted metadata containment，不宣称消灭提示注入；
- 顶层显式构建；
- Schema 递归类型检查、深度/属性/enum/value 预算；
- `$ref` 节点降级，不留下悬空引用；
- `type` 支持字符串和字符串数组；
- 单个 public definition 最终不超过 8 KiB；
- catalog description 不超过 200 字符；
- raw description 不进入普通 Broker 输出。

### 2.4 风险分类与 Digest

```text
local tool_policy override
  > explicit readOnlyHint=true and destructiveHint!=true
  > default mutating
```

数据结构：

```python
public_schema_digest: str
raw_schema_digest: str | None
```

- search/describe/call 使用 public digest；
- mutating call digest 必填；
- readonly call digest可选；
- raw-only 变化不应使 public digest漂移；
- fake-readonly override 不改变 effective risk。

### 2.5 Broker 参数验证

转发前验证：

```python
tool.public_definition["inputSchema"]
```

首版支持：

- object、array、string、integer、number、boolean、null；
- type 数组；
- properties、required、additionalProperties；
- enum、const；
- minimum、maximum；
- minLength、maxLength；
- minItems、maxItems；
- oneOf、anyOf、allOf；
- items。

失败返回：

```text
UPSTREAM_ARGUMENTS_INVALID
category=validation
```

### 2.6 结果最终硬预算

保证：

```text
len(final MCP result envelope) <= RESULT_INLINE_MAX
```

管线：

```text
raw result
→ normalize
→ serialize original
→ Broker: 有 owner 时存储原文
→ content-aware truncate
→ 再次 serialize
→ 超限则最小 envelope
```

要求：

- 多 text blocks 累计受限；
- image/audio/blob 整块省略，不裁坏 base64；
- resource URI 可保留，内嵌 text 截断，blob 省略；
- unknown block 在超限路径转占位符；
- structuredContent 递归预算；
- 原结果无 structuredContent 时创建 metadata；
- 保留原 `isError`；
- direct upstream 默认不存 overflow；
- Broker 才产生 result handle。

### 2.7 ResultStore

```text
single result <= 8 MiB
per owner <= 16 handles / 16 MiB
global <= 64 MiB
TTL = 5 min
FIFO
fetch limit <= 32000 Unicode codepoints
```

owner：

- HTTP：当前 MCP session ID；
- stdio：Runtime/进程级随机 owner；
- 无 owner：不存储；
- 禁止 `__default__`；
- OAuth principal 复合隔离明确为未实现。

### 2.8 固定 catalog 与配置生效边界

配置字段：

```text
expose_mode: direct | broker
pinned_tools: remote_name[]
tags: string[]
tool_policy: remote_name -> readonly | mutating
tool_search.custom_synonyms: global map
```

生效规则：

- 配置在 Runtime 创建前加载；
- 旧配置缺 `expose_mode` → direct；
- direct：过滤后的工具全部进入 direct + catalog；
- broker：过滤后的工具全部进入 catalog，只有 pinned 进入 direct；
- include/exclude 先执行；
- 当前 Runtime 的结果随后冻结；
- Admin 修改返回 `restart_required=true`；
- 旧 Runtime 不变；
- 新 Runtime/服务重启后使用新配置；
- `listChanged=false`。

---

## 3. 任务依赖图

```text
T00  基线冻结与契约冲突发现（已完成）
  ↓
T00R Stable-catalog 任务书修订（本次文档任务）
  ↓
T01  固定 Registry 快照迁移（行为不变）
  ↓
T02  Schema sanitizer、风险与 public digest
  ↓
T03  结果硬预算（inline，无 handle）
  ↓
T04  独立 BM25 Catalog 搜索模块
  ↓
T05  启动配置、Catalog 与 direct/broker 冻结分流
  ↓
T06  固定 Broker search/describe 工具
  ↓
T07  固定 readonly/mutating call、参数验证、passthrough
  ↓
T08  Session ResultStore 与 result_fetch
  ↓
T09  Runtime close/client 并发与多 Runtime 隔离
  ↓
T10  Admin restart-only 配置、文档和运维报告
  ↓
T11  全量验证与发布交接
```

T01–T04 不改变当前 direct upstream 工具可见行为。
T05 开始，`expose_mode=broker` 只在新 Runtime 初始化时生效。

---

# 4. 分步任务卡

## T00 — 基线冻结与契约冲突发现

状态：**complete / blocked as designed**

权威 handoff：

```text
docs/upstream-broker-handoffs/T00-baseline.md
```

不要重复基线测试，除非 T01 开始前 HEAD 或依赖已变化。

---

## T00R — Stable-catalog 任务书修订

### 目标

解决 T00 发现的产品契约冲突，仅修订任务书，不改生产代码。

### 必须确认

- 删除所有 live `tool_profile` 分支要求；
- 删除动态 start/stop/restart 要求；
- Broker 五工具改为固定本地目录；
- direct/broker 只在 Runtime 初始化时决定；
- Admin 写入仅 restart-required；
- T11 smoke 不再要求切换 profile 或实时 start/stop；
- 附录旧伪代码明确降级为非规范参考。

### 测试

```text
python -m pytest tests/compliance/test_upstream_gateway.py -q
uv run --frozen python -m unittest discover -s tests -p "test_*.py"
```

文档任务允许使用 T00 已通过的全量基线；至少运行 upstream contract 定点测试和文档静态搜索：

```text
任务书正文不得要求：
- tool_profile 控制目录
- UpstreamManager.start_server/stop_server
- listChanged=true
```

### 建议提交

```text
docs(broker): adapt taskbook to stable catalog contract
```

---

## T01 — 固定 Registry 快照迁移（行为不变）

### 目标

把当前 mutable `_tools + _tool_order + clients` 迁移为一次构造、只读发布的
`UpstreamRegistryState`，保持现有 direct exposure、错误和 annotations 行为不变。

### 只读取

- `coding_tools_mcp/upstream.py`：clients、UpstreamManager、initialize、close；
- `coding_tools_mcp/server.py`：Runtime 构造、list_tools、direct dispatch；
- `tests/compliance/test_upstream_gateway.py`；
- T00 与 T00R handoff。

### 实现

1. 新增 frozen `UpstreamRegistryState`：
   - all_tools；
   - stable direct name tuple；
   - empty/future catalog slot；
   - empty/future search index slot；
   - clients。
2. 使用 `MappingProxyType` 防外部修改。
3. `_initialize_configs()` 在局部构建完整 snapshot，一次发布。
4. 保持当前 tolerant config snapshot 语义和 status payload。
5. `tool_definitions()`、`tool_names()`、`has_tool()`、`call_tool()` 每次只读取一个局部 state。
6. `close()` 幂等关闭 state 中 clients，不添加 start/stop/reload API。
7. 所有 upstream 工具仍 direct。
8. 不引入 catalog 搜索、expose_mode 生效、sanitizer 或 result budget。

### 验收

- 原工具名称与顺序不变；
- 两个 Runtime 不共享 clients/session；
- state mapping 不可修改；
- `tool_profile` 字符串未进入 `upstream.py`；
- `UpstreamManager` 仍无 start_server/stop_server；
- `listChanged=false`；
- 原 Gateway 定点测试通过。

### 建议提交

```text
refactor(upstream): freeze registry in immutable runtime state
```

---

## T02 — Schema sanitizer、风险分类与 public Digest

### 目标

建立 raw/public definition 双轨，限制外部 MCP 元数据进入模型上下文。

### 只读取

- `coding_tools_mcp/upstream.py` 的 `UpstreamTool`、definition 构建与 tool definitions；
- 新建 `coding_tools_mcp/upstream_sanitize.py`；
- T01 handoff；
- 附录 A sanitizer 局部章节。

### 实现

1. 顶层 public definition 显式构建；
2. title/description/control chars/Schema 递归清洗；
3. property、enum、深度、default/const/examples 限额；
4. `$ref` 节点降级；
5. outputSchema 递归清洗；
6. `type` 支持 str/list；
7. 四阶段硬降级，最终 `< 8192 bytes`；
8. `UpstreamTool` 保存：raw/public/effective risk/public digest/raw digest；
9. 风险规则：local policy > real annotation > unknown mutating；
10. `tool_definitions()` 只返回 public definition；
11. fake-readonly compatibility override 不改变 effective risk。

### 禁止

- 不实现 Broker；
- 不让 expose_mode 生效；
- 不把 raw definition 返回模型；
- 不修改固定目录契约。

### 测试

覆盖：

- 非法顶层类型丢弃；
- 非 dict property 降级；
- `$ref` 不悬空；
- `type=["string","null"]`；
- hard byte budget；
- raw-only 变化不影响 public digest；
- unknown risk=mutating；
- 原 Gateway annotations 合规测试更新后通过。

### 建议提交

```text
feat(upstream): contain untrusted tool metadata
```

---

## T03 — 结果预算与内容感知截断（无 handle）

### 目标

给现有 direct upstream 结果加入最终 envelope 硬预算，暂不实现 ResultStore。

### 只读取

- `coding_tools_mcp/upstream.py` 的 client call、normalize、result error；
- MCP content/result shape 测试；
- T02 handoff；
- 附录 A result truncation 局部章节。

### 实现

1. client raw call 与 Manager normalize/budget 责任分离；
2. text 按累计 envelope 预算；
3. image/audio/blob 整块省略；
4. resource 保留 URI，裁 text，移除 blob；
5. unknown large block 转占位符；
6. structuredContent 递归限制；
7. 无 structuredContent 时创建 metadata；
8. 最终 serialize 再检查；
9. 仍超限返回最小 envelope；
10. 保留 `isError`；
11. Phase 1 仅 `_truncated + _original_bytes`，无 handle。

### 验收

- 小结果结构保持；
- missing content 仍按 v0.2.2 契约规范化为空数组，不复制 structured text；
- 多 text block 不绕过；
- binary 不损坏；
- final envelope 不超过上限；
- direct 调用不产生 handle。

### 建议提交

```text
fix(upstream): enforce hard result envelope budgets
```

---

## T04 — 独立 BM25 Catalog 搜索模块

### 目标

实现纯模块、可单测的字段加权搜索，不接入 Runtime Broker。

### 只读取

- 新建 `coding_tools_mcp/upstream_search.py`；
- T03 handoff；
- 附录 A tokenizer/BM25 局部章节。

### 实现

- instance-level `ToolTokenizer`；
- NFKC、separator、camelCase；
- CJK known phrase + bigram + fallback；
- built-in + global custom synonyms；
- 单词扩展最多 10，去重、稳定；
- 字段权重 name 5/title 4/tags 4/alias 3/arguments 2/description 1；
- server/risk/tags/name_prefix filters；
- exact public、alias/remote、唯一 remote、唯一 prefix 快速路径；
- 默认 5、最大 20；
- 结果不含完整 Schema；
- deterministic sort；
- SearchBackend Protocol 与实现一致。

### 测试

重点：

- `searchLibrary`；
- `snake_case`；
- “搜索文献”；
- custom “核磁” → nmr；
- 两 server 同 remote name；
- query token 不重复加分；
- tokenizer 实例互不污染；
- 50、200、500 工具 fixture 均正确返回且结果受限。

### 建议提交

```text
feat(upstream): add field-weighted broker search index
```

---

## T05 — 启动配置、Catalog 与 direct/broker 冻结分流

### 目标

在 Runtime 初始化快照中接入 catalog、搜索索引和 exposure 配置；
不提供任何 live mutation API。

### 只读取

- `coding_tools_mcp/upstream.py` 配置 snapshot 与 Manager 初始化；
- Gateway Admin 配置 parser/validator；
- T04 handoff；
- v0.2.2 integration contract 的 Upstream/Admin 章节。

### 实现

配置：

```text
expose_mode: direct | broker
pinned_tools
tags
tool_policy
tool_search.custom_synonyms
```

规则：

1. include/exclude 先过滤；
2. 旧配置缺 expose_mode → direct；
3. direct：全部进入 direct + catalog；
4. broker：全部进入 catalog，只有 pinned remote names 进入 direct；
5. collision 基于全部 all_tools 和本地 `TOOL_REGISTRY` reserved names；
6. catalog 只保存紧凑 public metadata；
7. index 在构造期建立并随 state 冻结；
8. Manager 不新增 start/stop/reload；
9. Admin 写配置只 validate/persist/revision/restart_required；
10. 当前 Runtime 不读取更新后的配置。

### 迁移安全

T05 完成后 broker-only 工具会从新 Runtime 的 direct list 消失，但 T06 尚未提供发现工具。
因此：

- T05 与 T06 应连续实施；
- 文档/UI 在 T06 完成前不得推荐用户切到 broker；
- 默认仍 direct，现有配置行为不变。

### 测试

- legacy config direct；
- broker-only 不在 direct definitions；
- pinned 在 direct；
- catalog/index 与 definitions 来自同一 Runtime snapshot；
- Admin 更新不影响已创建 Runtime；
- 新 Runtime 读取新配置；
- no dynamic methods；
- listChanged=false。

### 建议提交

```text
feat(upstream): freeze catalog and exposure at runtime startup
```

---

## T06 — 固定 Broker Search 与 Describe

### 目标

把发现能力作为固定本地工具加入 `TOOL_REGISTRY`。

### 只读取

- `coding_tools_mcp/server.py` 的 `ToolSpec`、`TOOL_REGISTRY`、schemas、handlers；
- `coding_tools_mcp/upstream.py` 的 frozen catalog/search；
- T05 handoff。

### 实现

固定注册：

```text
upstream_tool_search
upstream_tool_describe
```

要求：

- 两者 `read_only=True`、`idempotent=True`；
- 不根据 gateway_enabled/catalog/profile 条件隐藏；
- search 支持 query、server、read_only、tags、name_prefix、limit；
- `read_only` 仅是调用参数过滤器，不是 Runtime profile；
- search 返回紧凑 metadata + public digest，不返回 Schema；
- describe 返回 public definition + public digest；
- raw definition 永不返回；
- catalog 空时稳定返回空/not-found；
- instructions 使用固定短文本，不列动态 server/tool 数量；
- `listChanged=false`。

### 测试

- 两工具始终在本地固定目录；
- 两个 Runtime catalog 不同但本地工具目录名称一致；
- empty catalog；
- readonly filter；
- describe 不泄漏 raw；
- deterministic definitions/order；
- legacy profile 输入不改变结果。

### 建议提交

```text
feat(broker): add fixed upstream discovery tools
```

---

## T07 — 固定 Broker Calls、参数验证与 passthrough

### 目标

增加两个固定调用工具，严格区分 readonly 与 mutating，避免 envelope 二次包裹。

### 只读取

- `server.py` 的 ToolSpec、call dispatch、argument validation；
- `upstream.py` 的 raw/normalized call path；
- T06 handoff。

### 实现

固定注册：

```text
upstream_tool_call
upstream_tool_call_mutating
```

ToolSpec：

```text
upstream_tool_call:
  read_only=true
  idempotent=false  # 目标虽声明只读，也不假定所有远端实现幂等
  open_world=true

upstream_tool_call_mutating:
  read_only=false
  destructive=true
  open_world=true
```

调用要求：

1. search/describe 只查当前 Runtime frozen catalog；
2. readonly route 只允许 effective risk=readonly；
3. mutating route 只允许 mutating/unknown；
4. readonly digest 可选；
5. mutating public digest 必填且为 32-char lower hex；
6. public digest 不匹配 → `UPSTREAM_SCHEMA_CHANGED`；
7. 使用 public inputSchema 验证参数；
8. 失败 → `UPSTREAM_ARGUMENTS_INVALID`；
9. handler 返回完整 MCP envelope，dispatch 直接 passthrough；
10. 不进入 `make_tool_result()` 二次包裹；
11. upstream `isError` 保留；
12. 不添加 profile visibility 分支；
13. fake-readonly override 只影响向客户端展示本地 annotation 的现有兼容路径，不改变 handler 风险校验。

### 测试

- 两调用工具永久存在；
- real annotations 正确；
- passthrough 无嵌套；
- readonly/mutating 互拒；
- mutating 无 digest 拒绝；
- digest stale 拒绝；
- required/type/enum/const/bounds/oneOf 验证；
- loose schema 兼容；
- upstream isError=true 保留；
- legacy profile 不隐藏 mutating call。

### 建议提交

```text
feat(broker): validate and dispatch fixed upstream calls
```

---

## T08 — Session ResultStore 与固定 Result Fetch

### 目标

为 Broker 超大结果提供 session 隔离的短期原文存储与分页读取。

### 只读取

- `upstream.py` Manager call/budget；
- `server.py` request/session context、stdio startup；
- T07 handoff。

### 实现

固定注册：

```text
upstream_result_fetch
```

ToolSpec：read_only=true、idempotent=true。

ResultStore：

```text
single <= 8 MiB
per owner <= 16 handles / 16 MiB
global <= 64 MiB
TTL 5 min
FIFO
```

owner：

- HTTP MCP session ID；
- stdio Runtime/进程级随机 ID；
- 无 owner不存；
- 禁止共享 default；
- OAuth principal 未实现要如实记录。

fetch：

- Unicode codepoint offset；
- 1..32000 limit；
- schema + runtime clamp；
- cross-session、expired、unknown 使用统一 not-found；
- 不泄露其他 session 是否存在 handle。

### 测试

- raw 先存后截；
- 无 structuredContent 仍有 metadata/handle；
- direct upstream 不存；
- Broker 有 owner 才存；
- cross-session；
- TTL/FIFO/byte caps；
- fetch cap；
- stdio owner；
- fixed local directory 现在包含五个 Broker 工具。

### 建议提交

```text
feat(broker): add session-scoped upstream result paging
```

---

## T09 — Runtime Close、Client Lease 与多 Runtime 隔离

### 目标

在“Runtime snapshot 永不动态替换”的前提下，加固 close/call 并发和 Runtime 隔离。

### 只读取

- `upstream.py` 所有 client class、Manager close/call；
- Runtime close 路径；
- T08 handoff。

### 实现

1. client 增加最小 lifecycle lock/closed 状态；
2. `call_tool_raw()` 与 `close()` 锁顺序明确；
3. 已开始调用安全完成，close 等待或采用明确 lease；
4. close 后新调用 retryable；
5. Manager close 一次截断后续调用并关闭所有本 Runtime clients；
6. close 幂等；
7. 两个 Runtime 不共享：client、HTTP upstream session、catalog、ResultStore owner 数据；
8. Admin 配置写入不会替换现有 Runtime state；
9. 不实现 restart/stop manager API；
10. “重启”测试通过销毁旧 Runtime、构造新 Runtime 表达。

### 允许结果

- 与 close 竞争且尚未取得 lease 的调用可 retryable fail；
- 已取得 lease 的调用完成；
- 不允许死锁、半关闭 transport、跨 Runtime client 使用。

### 测试

- close waits for in-flight call；
- close idempotent；
- call-after-close；
- HTTP/stdio；
- 两 Runtime session independence；
- old Runtime 保持旧 catalog；
- new Runtime 使用更新配置；
- 50–100 次并发 call/close 无死锁。

### 建议提交

```text
fix(upstream): make frozen runtime client shutdown safe
```

---

## T10 — Admin Restart-Only 配置、兼容迁移与文档

### 目标

让新增配置安全 round-trip，并准确说明 fixed-catalog 生效边界。

### 只读取

- Gateway/Admin config parser、revision、persistence；
- README、browser client、integration contract；
- T09 handoff。

### 实现

配置验证：

- expose_mode enum；
- pinned/include/exclude 字符串数组；
- tool_policy readonly/mutating；
- tags/custom synonyms 数量与长度；
- credentials/redaction 不变；
- legacy missing expose_mode=direct。

Admin：

- validate/persist only；
- revision conflict 保留；
- `restart_required=true`；
- 不触碰现有 Runtime；
- UI 新建 upstream 默认 broker；
- direct → broker 预览显示 direct/pinned/broker-only 差异；
- 提示“新 MCP session/Runtime 或服务重启后生效”；
- 不声称发送 list_changed。

运维：

```text
Upstream direct exposure: count/bytes
Upstream catalog: total/broker-only
Largest public definitions
Excludes built-in/admin definitions
```

文档：

- fixed Broker workflow；
- mutating route 始终可见但带真实风险 annotations；
- digest 和参数验证；
- ResultStore 隔离；
- Runtime freeze/restart-only；
- legacy tool_profile ignored；
- Phase 3/4 未实现。

必要时更新 `docs/integration-contract-v0.2.2.md`：

- 只新增 Broker 作为固定目录扩展的兼容决策；
- 不推翻 legacy profile migration；
- 不引入 live catalog mutation。

### 测试

- config round-trip；
- old config；
- invalid config；
- revision conflict；
- redaction；
- old Runtime unchanged/new Runtime changed；
- WebUI source/build checks；
- docs contract machine-readable block（若修改）。

### 建议提交

```text
docs(config): document restart-only upstream broker exposure
```

---

## T11 — 全量验证、性能与发布交接

### 目标

不新增功能，冻结 stable-catalog Broker 实施结果。

### 只读取

- T00–T10 handoff；
- 最终 diff；
- 失败测试涉及文件；
- 附录算法测试清单。

### 必做验证

1. 定点：sanitizer、search、catalog、Broker calls、ResultStore、close concurrency、Admin config；
2. authoritative full unittest discovery；
3. fixed catalog contract tests；
4. smoke A：legacy direct config 构造 Runtime A；
5. smoke B：broker config 构造 Runtime B；
6. 验证 Runtime A 在配置写入后保持不变；
7. 验证 Runtime B direct definitions 减少、catalog 完整；
8. search → describe → readonly call；
9. search → describe → mutating digest call；
10. large result → fetch；
11. close old Runtime → new Runtime 读取新配置；
12. 不执行 live profile 切换；
13. 不执行 manager start/stop；
14. `listChanged=false`；
15. Context 对比 direct count/schema bytes/catalog count；
16. 至少 30 个中英文 search fixture，记录 top-1/top-5；
17. index build/search p50/p95；
18. final envelope <= budget；
19. raw definition 不泄漏；
20. cross-session handle 不可读；
21. Git clean、提交链完整、release handoff。

### 完成标准

```text
Stable-catalog adaptation: complete
Phase 1 defensive controls: complete
Phase 2 Broker: complete
Dynamic listChanged/profile activation: explicitly not implemented
```

### 建议提交

```text
docs(handoff): validate stable-catalog upstream broker
```

---

## T12 — 独立复审修复与最终合并门禁

### 目标

将 T11 之后独立终审确认的协议、Unicode、Schema、结果预算、生命周期、Admin/WebUI 契约和跨平台测试缺口作为独立 remediation 收口，不把这些新增产品行为归入 T11 的“只验证、不新增功能”。

### 范围

- strict JSON：UTF-8 only、4,300 位整数、有限浮点、深度失败归一化，且输入解析、Schema 错误、canonical JSON、结果预算和协议输出均独立于 Python 全局整数位数限制；
- MCP stdio：raw pipe UTF-8、非法 UTF-8 后继续、surrogate 输出回退；
- upstream stdio response：单帧原始响应上限为 1 MiB；超限帧必须有界排空并返回 `UPSTREAM_RESPONSE_TOO_LARGE`，且下一帧仍可读取；
- Schema assertion containment：必须覆盖真实 discovery/Manager 生命周期；sanitizer 前不得执行无界递归复制，深层但可解析 Schema 不得中止 Runtime 初始化；
- upstream tools/call result：`structuredContent` 可缺省，存在时必须为 object；超出受支持结构深度的合法 JSON 必须转换为非重试 `UPSTREAM_PROTOCOL_ERROR`，不得从 Manager 逸出；
- JSON 容器深度：根 dict/list 为第 1 层，每进入一个子 dict/list 层数 +1，scalar 不增加层数；最多允许 64 个容器层，第 65 层必须拒绝；该定义统一适用于 upstream result、`structuredContent`、JSON-RPC error tree 和 error details；
- upstream JSON-RPC envelope/error/details：响应 `id` 必须是与请求 ID 精确相等的整数，禁止布尔、浮点、字符串或缺失值；`error.code` 必须存在且为精确整数；错误对象和 Gateway/status 映射不得执行无界递归复制；64 层限制按每个不可信顶层 detail value 计算，不计 Gateway 与 status 包装层；status 必须保留安全的 code/message/category/retryable，仅重新约束 details；合法边界值不得被提前省略；超深结构、NaN/Infinity 和超过 4,300 位的整数必须在严格输出前转换为非重试协议错误或有界 omission；单个 detail value 内的循环或共享容器必须省略，不同顶层 detail values 之间的 Python 身份共享按值独立规范化；
- upstream result：未配对 surrogate 保留与预算；
- JSON Schema：大整数指纹独立于 Python 全局限制；
- mutating digest：可直接来自 `upstream_tool_search` 或 `upstream_tool_describe`，仅用于无状态 Schema 一致性校验；
- Runtime snapshot：search index 单次 build、tokenizer 同义词映射不可变；definition 树使用非 `dict`/非 `list` 的只读 Mapping/Sequence 包装，常规及 `dict`/`list` 基类变异 API 均不可修改，raw/public snapshot 的 `deepcopy()` 必须通过迭代 thaw 导出任意受支持深度的普通可变 JSON；
- lifecycle：call-after-close 统一为 retryable `UPSTREAM_NOT_AVAILABLE`；
- Admin/WebUI：`exposure_report.servers[]` 只使用聚合计数；
- 测试客户端：stdio 文本包装显式 `encoding="utf-8", errors="strict"`；
- 文档、ADR、机器契约和追踪矩阵。

### 明确不实现

- session-bound describe token；
- live profile、start/stop/reload；
- `listChanged=true`；
- T00–T11 已延期的动态产品能力。

### 最终门禁

1. `make compliance`；
2. `npm --prefix webui test` 与 `npm --prefix webui run build`；
3. authoritative full unittest discovery；
4. Ruff、compileall、项目 typecheck；
5. release validation report 重新生成并为 pass；
6. `git diff --check`；
7. 需求 → 代码 → 测试 → 证据追踪矩阵完整；
8. 所有 T12 文件进入提交，提交链完整；
9. `git status --short` 为空后，才允许标记 merge-ready。

### 建议提交

```text
fix(upstream): close post-review broker blockers
```

---

## 5. 全局验收矩阵

| 维度 | 必须满足 |
|---|---|
| 固定目录 | 五个 Broker 工具永久存在于本地 TOOL_REGISTRY |
| legacy profile | 仅迁移输入，不控制目录、路由、搜索或调用 |
| Runtime | upstream discovery/catalog/clients 构造后冻结 |
| Admin | 配置写入 restart-only，不修改现有 Runtime |
| listChanged | 始终 false |
| 上下文 | broker-only 完整 Schema 不进入 tools/list |
| 搜索 | >50 工具使用字段加权 BM25，默认只返前 5 |
| Schema | public definition 递归 containment + 8 KiB 硬上限 |
| 风险 | unknown=mutating；local policy 可覆盖 |
| readonly route | 只调用 effective readonly |
| mutating route | 始终可见、真实 destructive/open-world annotations、digest 必填 |
| 参数 | 网关验证 public inputSchema |
| 结果 | final MCP envelope 有硬预算 |
| 大结果 | session owner + TTL/FIFO + fetch cap |
| 隔离 | 不同 Runtime 不共享 upstream client/session/catalog store |
| close | in-flight 安全，close 幂等，call-after-close retryable `UPSTREAM_NOT_AVAILABLE`；真实传输断开使用 `UPSTREAM_DISCONNECTED` |
| 兼容 | 旧配置缺 expose_mode=direct |

---

## 6. 禁止实现清单

以下内容出现于代码 diff 即视为任务偏离，除非用户另行批准产品契约变更：

```text
tool_profile in coding_tools_mcp/upstream.py
Runtime(... tool_profile=...)
UpstreamManager.tool_names(tool_profile=...)
UpstreamManager.start_server(...)
UpstreamManager.stop_server(...)
Runtime 内 reload upstream config
capabilities.tools.listChanged = true
notifications/tools/list_changed
根据 profile 隐藏 upstream_tool_call_mutating
Admin 保存后直接修改现有 Runtime catalog
```

允许 legacy `tool_profile` 只存在于设置迁移/警告代码和相应测试中，不得进入 Gateway 运行逻辑。

---

## 7. Agent 启动提示模板

### T01

```text
进入 G:\LLM\coding-tools-mcp-broker-v6。
读取：
1. docs/upstream-tool-broker-v6-execution-taskbook.md 中 T01；
2. docs/upstream-broker-handoffs/T00-baseline.md；
3. docs/upstream-broker-handoffs/T00R-taskbook-adaptation.md；
4. coding_tools_mcp/upstream.py 的 client/manager 部分；
5. tests/compliance/test_upstream_gateway.py。

只执行 T01：固定 Registry 快照迁移，行为不变。
禁止引入 tool_profile、start_server、stop_server、reload 或 listChanged。
完成定点测试、提交和 T01 handoff 后停止，不进入 T02。
```

### 通用 Txx

```text
进入 G:\LLM\coding-tools-mcp-broker-v6。
确认分支 feat/upstream-tool-broker-v6、工作树 clean。
只读取任务书 Txx、上一 handoff 和任务卡指定文件。
只执行 Txx；测试、提交、写 handoff 后停止。
不得恢复 tool_profile，不得增加动态 upstream lifecycle，不得改变 listChanged=false。
```

---

# 附录 A — 历史材料归档

原始附录已移至 `docs/archive/upstream-tool-broker-v6-appendix-a-historical.md`。该文件仅用于历史追溯，不属于执行规格。
