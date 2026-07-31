# Upstream Tool Broker — v6 Stable-Catalog 分步实施任务书

> 状态：**Architecture approved / v0.2.2 contract adapted / Implementation-ready**
>
> 本任务书是 v6 Broker 方案针对当前 `v0.2.2` 固定工具目录契约的正式适配版。
> 它取代此前正文中依赖 `tool_profile`、动态 `start_server()` / `stop_server()`、
> 运行时改变 `tools/list` 的执行要求。
>
> **执行优先级：本任务书正文 > T00 handoff > 附录 A 原始技术规格。**
> 附录 A 仅用于复用 sanitizer、BM25、ResultStore 等局部算法；其中任何
> `tool_profile`、动态生命周期或 `listChanged=true` 伪代码均为失效参考，禁止照抄。

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
- mutating 调用必须先 describe，并提交匹配的 public schema digest；
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
| close | in-flight 安全，close 幂等，call-after-close retryable |
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

# 附录 A — v6 技术规格原文

> v5 (9/10) + 5 项精确修正 + 8 项补充测试

---

## 数据结构总览

### 不可变注册状态

```python
@dataclass(frozen=True)
class UpstreamRegistryState:
    """Immutable snapshot — single-reference swap guarantees atomicity."""
    all_tools: dict[str, UpstreamTool]
    direct_tool_names: frozenset[str]
    catalog: dict[str, UpstreamToolCatalogEntry]
    search_index: CatalogSearchIndex | None
```

> [!NOTE]
> `dict` 本身不是 frozen 的，但作为 frozen dataclass 的字段，一旦构建后不再被修改。
> 所有修改都是构建新 dict → 构建新 `UpstreamRegistryState` → 原子替换引用。

```python
class UpstreamManager:
    def __init__(self, ...):
        self._state = UpstreamRegistryState(
            all_tools={},
            direct_tool_names=frozenset(),
            catalog={},
            search_index=None,
        )
        self._lifecycle_lock = threading.Lock()  # 保护 _state + clients
        self.clients: dict[str, BaseUpstreamClient] = {}
        self.result_store: ResultStore = ResultStore()

    @property
    def state(self) -> UpstreamRegistryState:
        return self._state
```

所有读操作（`tool_definitions()`、`search_catalog()`、`call_tool()`）从 `self._state` 快照读取，无锁。所有写操作通过 `_replace_state()` 在 `_lifecycle_lock` 内原子替换。

### 配置

```python
EXPOSE_MODE_CHOICES = ("direct", "broker")

@dataclass(frozen=True)
class UpstreamServerConfig:
    alias: str
    transport: str
    enabled: bool = True
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    authorization_env: str | None = None
    include_tools: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    expose_mode: str = "direct"
    pinned_tools: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    tool_policy: dict[str, str] = field(default_factory=dict)
```

### 工具与目录

```python
@dataclass(frozen=True)
class UpstreamTool:
    public_name: str
    remote_name: str
    raw_definition: dict[str, Any]       # 仅服务端/admin
    public_definition: dict[str, Any]    # 递归规范化，面向模型
    effective_risk: str                  # "readonly" | "mutating"
    schema_digest: str                   # sha256 hex, 32 chars

@dataclass(frozen=True)
class UpstreamToolCatalogEntry:
    public_name: str
    remote_name: str
    server_alias: str
    title: str
    description: str                     # ≤200 chars
    effective_risk: str
    tags: tuple[str, ...]
    argument_names: tuple[str, ...]
    schema_digest: str
    schema_bytes: int
```

---

## Phase 1：Schema 与预算防御 + 注册状态迁移

> Phase 1 同时完成 `tools` → `UpstreamRegistryState` 迁移。
> 所有工具均 `direct`，为 Phase 2 降低改动量。

### 1.1 递归 Schema 规范化

#### [NEW] [upstream_sanitize.py](file:///g:/LLM/coding-tools-mcp/coding_tools_mcp/upstream_sanitize.py)

> [!NOTE]
> Schema 清洗是 **untrusted metadata containment**——限制长度、位置和攻击面。
> 不声称消除提示注入。

```python
import hashlib, json, re
from typing import Any

MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 400
MAX_DESCRIPTION_CHARS_CATALOG = 200
MAX_SCHEMA_DESCRIPTION_CHARS = 200
MAX_SCHEMA_PROPERTIES = 40
MAX_SCHEMA_ENUM_ITEMS = 50
MAX_SCHEMA_DEPTH = 8
MAX_DEFINITION_BYTES = 8_192
MAX_VALUE_DEPTH = 4
MAX_VALUE_STRING_CHARS = 500
MAX_VALUE_ARRAY_ITEMS = 20
MAX_VALUE_DICT_ITEMS = 20
MAX_VALUE_KEY_CHARS = 100

CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_ALLOWED_TOP_KEYS = {
    "name", "title", "description", "inputSchema",
    "outputSchema", "annotations",
}

# field → expected type(s) for strict type checking
_SCHEMA_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "type": (str,),
    "description": (str,),
    "title": (str,),
    "properties": (dict,),
    "required": (list,),
    "items": (dict,),
    "enum": (list,),
    "default": (str, int, float, bool, list, dict, type(None)),
    "const": (str, int, float, bool, list, dict, type(None)),
    "examples": (list,),
    "additionalProperties": (dict, bool),
    "not": (dict,),
    "oneOf": (list,),
    "anyOf": (list,),
    "allOf": (list,),
    "minimum": (int, float),
    "maximum": (int, float),
    "minLength": (int,),
    "maxLength": (int,),
    "minItems": (int,),
    "maxItems": (int,),
    "uniqueItems": (bool,),
    "pattern": (str,),
    "format": (str,),
    "$ref": (str,),
}

_ALLOWED_SCHEMA_KEYS = set(_SCHEMA_FIELD_TYPES.keys())
```

##### sanitize_text / _sanitize_value

```python
def sanitize_text(raw: str, *, max_chars: int) -> str:
    text = CONTROL_CHAR_RE.sub("", raw)
    text = re.sub(r"\s+", " ", text.strip())
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text

def _sanitize_value(value: Any, *, depth: int) -> Any:
    if depth >= MAX_VALUE_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_text(value, max_chars=MAX_VALUE_STRING_CHARS)
    if isinstance(value, list):
        return [_sanitize_value(v, depth=depth+1) for v in value[:MAX_VALUE_ARRAY_ITEMS]]
    if isinstance(value, dict):
        return {
            sanitize_text(str(k), max_chars=MAX_VALUE_KEY_CHARS): _sanitize_value(v, depth=depth+1)
            for k, v in list(value.items())[:MAX_VALUE_DICT_ITEMS]
        }
    return None
```

##### _sanitize_schema（含 $ref 降级 + 严格类型检查）

```python
_REF_DEGRADED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "description": "Schema reference omitted by gateway normalization.",
}

def _sanitize_schema(schema: dict[str, Any], depth: int) -> dict[str, Any]:
    if depth >= MAX_SCHEMA_DEPTH:
        return {"type": "object", "additionalProperties": True}

    # ── $ref detection: degrade rather than produce dangling reference ──
    if "$ref" in schema:
        return dict(_REF_DEGRADED_SCHEMA)

    result: dict[str, Any] = {}
    for key in _ALLOWED_SCHEMA_KEYS:
        if key not in schema:
            continue
        value = schema[key]

        # ── Strict type check: skip field if type doesn't match ──
        expected = _SCHEMA_FIELD_TYPES.get(key)
        if expected and not isinstance(value, expected):
            continue

        if key == "description":
            result[key] = sanitize_text(value, max_chars=MAX_SCHEMA_DESCRIPTION_CHARS)
        elif key == "title":
            result[key] = sanitize_text(value, max_chars=MAX_TITLE_CHARS)
        elif key == "properties":
            items = list(value.items())[:MAX_SCHEMA_PROPERTIES]
            result[key] = {
                k: _sanitize_schema(v, depth+1) if isinstance(v, dict) else v
                for k, v in items
            }
        elif key == "required":
            result[key] = [r for r in value if isinstance(r, str)]
        elif key == "items":
            result[key] = _sanitize_schema(value, depth+1)
        elif key in {"additionalProperties", "not"}:
            if isinstance(value, dict):
                result[key] = _sanitize_schema(value, depth+1)
            else:  # bool, already type-checked
                result[key] = value
        elif key == "enum":
            result[key] = [_sanitize_value(v, depth=0) for v in value[:MAX_SCHEMA_ENUM_ITEMS]]
        elif key in {"oneOf", "anyOf", "allOf"}:
            result[key] = [
                _sanitize_schema(v, depth+1) if isinstance(v, dict)
                else _sanitize_value(v, depth=0)
                for v in value[:10]
            ]
        elif key in {"default", "const"}:
            result[key] = _sanitize_value(value, depth=0)
        elif key == "examples":
            result[key] = [_sanitize_value(v, depth=0) for v in value[:5]]
        elif key == "pattern":
            result[key] = value[:500]
        else:
            result[key] = value

    # ── Sync required with actual properties ──
    if "required" in result and "properties" in result:
        allowed = set(result["properties"])
        result["required"] = [n for n in result["required"] if n in allowed][:MAX_SCHEMA_PROPERTIES]

    return result
```

##### sanitize_definition（4-stage 硬降级）

```python
def _serialize(d: dict[str, Any]) -> bytes:
    return json.dumps(d, ensure_ascii=False, separators=(",",":")).encode("utf-8")

def _strip_all_descriptions(schema: dict[str, Any]) -> None:
    schema.pop("description", None)
    schema.pop("title", None)
    for v in schema.get("properties", {}).values():
        if isinstance(v, dict):
            _strip_all_descriptions(v)
    for key in ("items", "additionalProperties", "not"):
        sub = schema.get(key)
        if isinstance(sub, dict):
            _strip_all_descriptions(sub)
    for key in ("oneOf", "anyOf", "allOf"):
        for v in schema.get(key, []):
            if isinstance(v, dict):
                _strip_all_descriptions(v)

def sanitize_definition(raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}

    for key in _ALLOWED_TOP_KEYS:
        if key in raw:
            result[key] = raw[key]

    if isinstance(result.get("title"), str):
        result["title"] = sanitize_text(result["title"], max_chars=MAX_TITLE_CHARS)
    if isinstance(result.get("description"), str):
        result["description"] = sanitize_text(result["description"], max_chars=MAX_DESCRIPTION_CHARS)

    # inputSchema
    schema = result.get("inputSchema")
    result["inputSchema"] = _sanitize_schema(schema, depth=0) if isinstance(schema, dict) \
        else {"type": "object", "additionalProperties": True}

    # outputSchema — recursive sanitize or drop
    output = result.get("outputSchema")
    if isinstance(output, dict):
        result["outputSchema"] = _sanitize_schema(output, depth=0)

    # annotations — known boolean hints only
    ann = result.get("annotations")
    result["annotations"] = {
        k: v for k, v in (ann if isinstance(ann, dict) else {}).items()
        if k in {"readOnlyHint","destructiveHint","idempotentHint","openWorldHint"}
        and isinstance(v, bool)
    }

    # ── 4-stage hard byte limit ──

    if len(_serialize(result)) <= MAX_DEFINITION_BYTES:
        return result

    # Stage 2: drop outputSchema
    result.pop("outputSchema", None)
    if len(_serialize(result)) <= MAX_DEFINITION_BYTES:
        return result

    # Stage 3: strip all schema descriptions + shorten top description
    _strip_all_descriptions(result.get("inputSchema", {}))
    desc = result.get("description", "")
    if isinstance(desc, str) and len(desc) > 100:
        result["description"] = desc[:99] + "…"
    if len(_serialize(result)) <= MAX_DEFINITION_BYTES:
        return result

    # Stage 4: replace with loose schema
    result["inputSchema"] = {"type": "object", "additionalProperties": True}
    result["description"] = sanitize_text(result.get("description", ""), max_chars=100)
    if len(_serialize(result)) <= MAX_DEFINITION_BYTES:
        return result

    # Stage 5: unreachable in practice
    raise UpstreamError(
        "UPSTREAM_TOOL_DEFINITION_TOO_LARGE",
        f"Tool {result.get('name','?')} exceeds {MAX_DEFINITION_BYTES}B after max degradation.",
        category="configuration",
    )


def schema_digest(definition: dict[str, Any]) -> str:
    schema = definition.get("inputSchema", {})
    canonical = json.dumps(schema, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
```

### 1.2 风险分类

```python
def classify_risk(raw_tool: dict[str, Any], tool_policy: dict[str, str], remote_name: str) -> str:
    policy = tool_policy.get(remote_name)
    if policy in ("readonly", "mutating"):
        return policy
    ann = raw_tool.get("annotations")
    if isinstance(ann, dict):
        if ann.get("readOnlyHint") is True and ann.get("destructiveHint") is not True:
            return "readonly"
    return "mutating"
```

### 1.3 结果截断（content-type-aware）

```python
RESULT_INLINE_MAX = 128_000

def truncate_result(result: dict[str, Any]) -> dict[str, Any]:
    """Content-type-aware truncation.
    
    - text blocks: safely truncated with "…[truncated]" marker
    - image/audio/blob blocks: replaced with metadata placeholder
    - resource blocks: URI preserved, embedded text truncated
    - structuredContent: recursively limited via _deep_truncate()
    """
    content = result.get("content")
    if isinstance(content, list):
        new_content = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "text")
            if btype == "text":
                text = block.get("text", "")
                if len(text) > MAX_RESULT_TEXT_CHARS:
                    new_content.append({
                        "type": "text",
                        "text": text[:MAX_RESULT_TEXT_CHARS] + "\n…[truncated]",
                    })
                else:
                    new_content.append(block)
            elif btype in ("image", "audio", "blob"):
                # Non-text binary: omit data, preserve metadata
                new_content.append({
                    "type": "text",
                    "text": f"[{btype} content omitted — {block.get('mimeType','unknown')},"
                            f" use upstream_result_fetch for full data]",
                })
            elif btype == "resource":
                # Preserve URI, truncate embedded text
                resource = block.get("resource", {})
                new_block = dict(block)
                if isinstance(resource, dict):
                    new_resource = dict(resource)
                    text = new_resource.get("text", "")
                    if isinstance(text, str) and len(text) > MAX_RESULT_TEXT_CHARS:
                        new_resource["text"] = text[:MAX_RESULT_TEXT_CHARS] + "\n…[truncated]"
                    # Remove blob data
                    new_resource.pop("blob", None)
                    new_block["resource"] = new_resource
                new_content.append(new_block)
            else:
                new_content.append(block)
        result = {**result, "content": new_content}

    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        result = {**result, "structuredContent": _deep_truncate(sc)}

    return result
```

### 1.4 注册状态迁移

Phase 1 将现有 `self.tools: dict` 替换为 `UpstreamRegistryState`。所有工具暂时均 `direct`：

```python
# Phase 1: expose_mode 字段可解析但暂时忽略，所有工具仍 direct
# Phase 2: expose_mode 生效

def _initialize_config(self, config, seen_public_names):
    # ... build client, fetch raw_tools ...
    
    new_tools: dict[str, UpstreamTool] = {}
    new_direct: set[str] = set()
    new_catalog: dict[str, UpstreamToolCatalogEntry] = {}
    
    for raw_tool in raw_tools:
        public_name = f"{config.alias}__{remote_name}"
        raw_def = namespaced_tool_definition(config.alias, public_name, raw_tool)
        public_def = sanitize_definition(raw_def)
        risk = classify_risk(raw_tool, config.tool_policy, remote_name)
        digest = schema_digest(raw_def)
        
        tool = UpstreamTool(
            public_name=public_name,
            remote_name=remote_name,
            raw_definition=raw_def,
            public_definition=public_def,
            effective_risk=risk,
            schema_digest=digest,
        )
        
        if public_name in seen_public_names:
            raise UpstreamError("UPSTREAM_TOOL_COLLISION", ...)
        seen_public_names.add(public_name)
        
        new_tools[public_name] = tool
        new_direct.add(public_name)  # Phase 1: all direct
        new_catalog[public_name] = _build_catalog_entry(tool, config)
    
    # Atomic state swap
    self._replace_alias_tools(config.alias, new_tools, new_direct, new_catalog, client)
```

### 1.5 Context Budget 报告

```python
def context_budget_report(self) -> dict[str, Any]:
    state = self._state
    direct_items = []
    for name in sorted(state.direct_tool_names):
        tool = state.all_tools.get(name)
        if tool is None:
            continue
        sz = len(json.dumps(tool.public_definition, ensure_ascii=False).encode())
        direct_items.append({"name": name, "schema_bytes": sz, "risk": tool.effective_risk})
    direct_items.sort(key=lambda t: t["schema_bytes"], reverse=True)
    direct_total = sum(t["schema_bytes"] for t in direct_items)
    catalog_count = len(state.catalog)
    broker_only = catalog_count - len(state.direct_tool_names & set(state.catalog))

    return {
        "direct_tool_count": len(state.direct_tool_names),
        "direct_definition_bytes": direct_total,
        "direct_definition_kib": round(direct_total / 1024, 1),
        "catalog_tool_count": catalog_count,
        "broker_only_tool_count": broker_only,
        "largest_direct_schemas": direct_items[:10],
    }
```

stderr 输出：

```
[coding-tools-mcp] Upstream context budget:
  Direct exposure: 18 external tools, 64.5 KiB definitions
  Catalog: 123 external tools, 105 broker-only
  This report excludes built-in and admin tool definitions.
  Largest: github__create_pull_request (12.3 KiB)
```

### 1.6 Phase 1 文件清单

| 操作 | 文件 | 变更 |
|---|---|---|
| **NEW** | [upstream_sanitize.py](file:///g:/LLM/coding-tools-mcp/coding_tools_mcp/upstream_sanitize.py) | `sanitize_definition()` (4-stage), `_sanitize_schema()` ($ref 降级 + strict type), `_sanitize_value()`, `sanitize_text()`, `schema_digest()` |
| MODIFY | [upstream.py](file:///g:/LLM/coding-tools-mcp/coding_tools_mcp/upstream.py) | `UpstreamRegistryState` + `UpstreamTool` (raw+public+risk+digest), `_replace_alias_tools()`, `classify_risk()`, `truncate_result()` (content-type-aware), `context_budget_report()`, `_visible()` → `effective_risk`, 迁移 `self.tools` → `self._state` |
| MODIFY | [server.py](file:///g:/LLM/coding-tools-mcp/coding_tools_mcp/server.py) | 启动时输出 budget report, `tool_definitions()` 从 `state.direct_tool_names` 读取（`sorted()`） |

---

## Phase 2：Broker 原子落地

### 2.1 真正原子的状态更新

```python
def _replace_alias_tools(
    self,
    alias: str,
    new_tools: dict[str, UpstreamTool],
    new_direct: set[str],
    new_catalog: dict[str, UpstreamToolCatalogEntry],
    client: BaseUpstreamClient | None = None,
) -> None:
    """Atomically replace all tools for one alias.
    
    Builds index inside lock. For hundreds of tools this is sub-millisecond.
    If profiling shows otherwise, switch to generation-based CAS outside lock.
    """
    with self._lifecycle_lock:
        old = self._state

        # Remove old entries for this alias
        next_tools = {n: t for n, t in old.all_tools.items() if not n.startswith(f"{alias}__")}
        next_tools.update(new_tools)

        next_direct = {n for n in old.direct_tool_names if not n.startswith(f"{alias}__")}
        next_direct.update(new_direct)

        next_catalog = {n: e for n, e in old.catalog.items() if not n.startswith(f"{alias}__")}
        next_catalog.update(new_catalog)

        # Build index inside lock
        index: CatalogSearchIndex | None = None
        if next_catalog:
            index = CatalogSearchIndex(synonyms=self._merged_synonyms)
            index.build(next_catalog)

        self._state = UpstreamRegistryState(
            all_tools=next_tools,
            direct_tool_names=frozenset(next_direct),
            catalog=next_catalog,
            search_index=index,
        )

        # Client lifecycle under same lock
        if client is not None:
            self.clients[alias] = client
        # (stop_server passes client=None after closing)


def stop_server(self, alias: str) -> dict[str, Any]:
    with self._lifecycle_lock:
        client = self.clients.pop(alias, None)
    if client is not None:
        client.close()
    # Remove all tools for alias
    self._replace_alias_tools(alias, {}, set(), {})
    # ...
```

读操作 — 无锁快照读：

```python
def tool_definitions(self, *, tool_profile: str) -> list[dict[str, Any]]:
    state = self._state  # atomic snapshot read
    return [
        profiled_definition(state.all_tools[name].public_definition, tool_profile)
        for name in sorted(state.direct_tool_names)  # ← 稳定排序
        if name in state.all_tools and self._visible(state.all_tools[name], tool_profile)
    ]

def call_tool(self, name, arguments, *, session_id=None, store_overflow=False):
    state = self._state
    tool = state.all_tools.get(name)
    if tool is None:
        return upstream_error_result("UPSTREAM_TOOL_NOT_FOUND", ...)
    # client lookup also under consistent view
    alias = name.partition("__")[0]
    with self._lifecycle_lock:
        client = self.clients.get(alias)
    if client is None:
        return upstream_error_result("UPSTREAM_NOT_AVAILABLE", ..., retryable=True)
    # ... proceed with call_tool_raw, normalize, store, truncate ...

def search_catalog(self, query, filters):
    state = self._state
    if state.search_index is None:
        return []
    return state.search_index.search(query, filters)
```

### 2.2 expose_mode + pinned_tools 分流

Phase 2 修改 `_initialize_config` 中的 direct 决策：

```python
# Phase 2: expose_mode 生效
if config.expose_mode == "direct":
    new_direct.add(public_name)
elif config.expose_mode == "broker":
    if remote_name in config.pinned_tools:
        new_direct.add(public_name)
# catalog: 所有模式都进入（已在 Phase 1 完成）
```

### 2.3 read-only profile Broker 过滤

```python
BROKER_READONLY_TOOL_NAMES = (
    "upstream_tool_search",
    "upstream_tool_describe",
    "upstream_tool_call",
    "upstream_result_fetch",
)
BROKER_MUTATING_TOOL_NAMES = (
    "upstream_tool_call_mutating",
)

def local_exposed_tool_names(self) -> list[str]:
    names = list(READ_ONLY_TOOL_NAMES if self.tool_profile == "read-only" else FULL_TOOL_NAMES)
    names = [n for n in names if self.enable_view_image or n != "view_image"]
    if self.upstream_gateway_enabled:
        names.extend(BROKER_READONLY_TOOL_NAMES)
        if self.tool_profile != "read-only":
            names.extend(BROKER_MUTATING_TOOL_NAMES)
    return names

def _visible(self, tool: UpstreamTool, tool_profile: str) -> bool:
    if tool_profile == "read-only":
        return tool.effective_risk == "readonly"
    return True
```

`upstream_tool_search` 在 read-only profile 强制 `read_only=True`：

```python
def upstream_tool_search(self, args):
    read_only = args.get("read_only")
    if self.tool_profile == "read-only":
        read_only = True
    # ...
```

### 2.4 调用链：call_tool_raw → 唯一 normalize + budget + store

```python
class BaseUpstreamClient:
    def call_tool_raw(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Raw tools/call response. No normalization."""
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(response, dict):
            raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "result not an object")
        return response

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Backward-compat: raw + normalize."""
        return normalize_tool_result(self.call_tool_raw(name, arguments))


class UpstreamManager:
    def call_tool(
        self, name: str, arguments: dict[str, Any], *,
        session_id: str | None = None,
        store_overflow: bool = False,
    ) -> dict[str, Any]:
        state = self._state
        tool = state.all_tools.get(name)
        if tool is None:
            return upstream_error_result("UPSTREAM_TOOL_NOT_FOUND", ...)

        alias = name.partition("__")[0]
        with self._lifecycle_lock:
            client = self.clients.get(alias)
        if client is None:
            return upstream_error_result("UPSTREAM_NOT_AVAILABLE", ...)

        try:
            # 1. Raw from upstream
            raw_result = client.call_tool_raw(tool.remote_name, arguments or {})

            # 2. Normalize (唯一责任方)
            normalized = normalize_tool_result(raw_result)

            # 3. Size check
            serialized = json.dumps(
                normalized, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")

            if len(serialized) <= RESULT_INLINE_MAX:
                return normalized

            # 4. Store original BEFORE truncation
            handle = None
            if store_overflow and session_id:
                handle = self.result_store.store(
                    serialized, session_id=session_id, server_alias=alias,
                )

            # 5. Content-type-aware truncation
            truncated = truncate_result(normalized)

            # 6. Attach metadata
            sc = truncated.get("structuredContent")
            if isinstance(sc, dict):
                sc["_truncated"] = True
                sc["_original_bytes"] = len(serialized)
                if handle:
                    sc["_result_handle"] = handle

            return truncated

        except UpstreamError as exc:
            return upstream_error_result(exc.code, exc.message, ...)
```

### 2.5 Broker Passthrough Dispatch

```python
_BROKER_PASSTHROUGH = frozenset({"upstream_tool_call", "upstream_tool_call_mutating"})

def _call_tool(self, name, arguments, *, admin=False):
    started_at = time.time()
    args = arguments or {}

    handler = self._tool_handlers.get(name) if name in self.local_exposed_tool_names() else None
    if handler is None:
        # Direct upstream
        if self.upstream_manager.has_tool(name, tool_profile=self.tool_profile):
            result = self.upstream_manager.call_tool(name, args)
            self.emit_tool_trace(name, args, result.get("structuredContent", result), started_at)
            return result
        raise JsonRpcError(-32602, f"Unknown tool: {name}")

    # Broker passthrough
    if name in _BROKER_PASSTHROUGH:
        validate_arguments(name, args)
        result = handler(args)
        self.emit_tool_trace(name, args, result.get("structuredContent", result), started_at)
        return result

    # Normal local handler
    validate_arguments(name, args)
    payload = handler(args)
    payload.setdefault("ok", True)
    self.emit_tool_trace(name, args, payload, started_at)
    return tool_result(payload, is_error=payload.get("ok") is False)
```

### 2.6 Session Owner — 无 `__default__`

```python
def current_result_owner(self) -> str | None:
    """Derive result store owner. Returns None if no real session."""
    session_id = getattr(self._tool_context, "session_id", None)
    if not session_id:
        return None
    # v6: OAuth principal isolation is not yet implemented.
    # Current isolation unit is MCP session ID only.
    # Future: f"{principal_id}:{session_id}" when principal_id available.
    return session_id
```

**stdio**：启动时生成进程级 owner：

```python
# server.py startup for stdio:
self._stdio_owner = f"stdio:{secrets.token_urlsafe(24)}"
# Then in _tool_context setup:
self._tool_context.session_id = self._stdio_owner
```

**无 owner 行为**：

```python
def upstream_tool_call(self, args):
    # ...
    owner = self.current_result_owner()
    result = self.upstream_manager.call_tool(
        name, args.get("arguments", {}),
        session_id=owner,
        store_overflow=owner is not None,  # 无 owner 不存储
    )
    return result
```

> [!NOTE]
> v6 实际隔离单位是 MCP session ID。OAuth principal 复合隔离（`principal_id:session_id`）已设计但暂未实现，
> 因为当前 `_tool_context` 不传递 OAuth claims。后续需要在 HTTP handler 解码 token 后
> 传入稳定身份（如 `client_id + grant_id`，不使用会随 token 刷新变化的 `jti`）。

### 2.7 Result Store（FIFO + TTL）

```python
RESULT_INLINE_MAX = 128_000
RESULT_STORED_MAX = 8_388_608
RESULT_PER_SESSION_HANDLES = 16
RESULT_PER_SESSION_BYTES = 16_777_216
RESULT_GLOBAL_BYTES = 67_108_864
RESULT_TTL_SECONDS = 300
RESULT_FETCH_LIMIT_MAX = 32_000


class ResultStore:
    """Session-isolated, TTL-bound, FIFO-evicting result store.
    
    Not LRU — fetch does not update access time. FIFO is simpler
    and sufficient for short-lived upstream result caching.
    """
    # ... (same as v5, omitted for brevity) ...
```

### 2.8 Result Fetch — 硬限制

Schema：

```python
"upstream_result_fetch": object_schema(
    {
        "handle": {"type": "string", "description": "Handle from a truncated result."},
        "offset": {
            "type": "integer", "minimum": 0, "default": 0,
            "description": "Character offset (Unicode codepoints, not bytes) into stored result.",
        },
        "limit": {
            "type": "integer", "minimum": 1, "maximum": 32000, "default": 32000,
            "description": "Max characters to return. Clamped to 32000.",
        },
    },
    required=["handle"],
),
```

Handler 双重约束：

```python
def upstream_result_fetch(self, args):
    handle = args["handle"]
    owner = self.current_result_owner()
    if owner is None:
        return {"ok": False, "error": {"code": "NO_SESSION", "message": "No active session."}}

    # Runtime clamp (defense-in-depth beyond schema validation)
    raw_offset = args.get("offset", 0)
    raw_limit = args.get("limit", 32000)
    safe_offset = max(0, int(raw_offset))
    safe_limit = max(1, min(int(raw_limit), RESULT_FETCH_LIMIT_MAX))

    result = self.upstream_manager.result_store.fetch(
        handle, session_id=owner,
        offset=safe_offset, limit=safe_limit,
    )
    if result is None:
        return {"ok": False, "error": {
            "code": "RESULT_NOT_FOUND",
            "message": "Handle not found, expired, or belongs to another session.",
        }}
    return result
```

### 2.9 搜索引擎 — 实例级词典

#### [NEW] [upstream_search.py](file:///g:/LLM/coding-tools-mcp/coding_tools_mcp/upstream_search.py)

```python
# ── Built-in synonyms (module constant) ──
BUILTIN_SYNONYMS: dict[str, list[str]] = {
    "搜索": ["search", "find", "query", "lookup"],
    "查找": ["search", "find", "lookup"],
    "读取": ["read", "get", "fetch", "retrieve"],
    "获取": ["get", "fetch", "retrieve"],
    "创建": ["create", "add", "insert", "new"],
    "新建": ["create", "add", "new"],
    "删除": ["delete", "remove", "drop"],
    "修改": ["update", "edit", "patch", "modify"],
    "写入": ["write", "put", "save", "store"],
    "列出": ["list", "enumerate", "show"],
    "查询": ["query", "search", "find"],
    "检索": ["search", "retrieve", "fetch"],
    "下载": ["download", "fetch", "get"],
    "文件": ["file", "document"],
    "目录": ["directory", "folder", "path"],
    "提交": ["commit", "push"],
    "分支": ["branch", "checkout"],
    "合并": ["merge", "rebase"],
    "文献": ["library", "paper", "article", "reference", "literature"],
    "论文": ["paper", "article", "publication"],
    "批注": ["annotation", "highlight", "note", "comment"],
    "引用": ["citation", "reference", "cite"],
    "收藏": ["collection", "library", "bookmark"],
    "浏览器": ["browser", "web", "page"],
    "截图": ["screenshot", "capture", "snapshot"],
    "导航": ["navigate", "goto", "open"],
    "点击": ["click", "tap", "press"],
    "数据库": ["database", "db", "sql"],
    "表": ["table", "schema"],
    "部署": ["deploy", "publish", "release"],
    "服务器": ["server", "host", "instance"],
    "容器": ["container", "docker"],
}

MAX_EXPANSIONS_PER_TERM = 10


def merge_synonyms(
    base: dict[str, list[str]],
    custom: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    merged = {k: list(v) for k, v in base.items()}
    if custom:
        for key, values in custom.items():
            existing = merged.get(key, [])
            combined = list(dict.fromkeys(existing + values))[:MAX_EXPANSIONS_PER_TERM]
            merged[key] = combined
    return merged


class ToolTokenizer:
    """Instance-level tokenizer with synonym-aware CJK phrase extraction."""

    def __init__(self, synonyms: dict[str, list[str]]) -> None:
        self.synonyms = synonyms
        self.known_phrases: frozenset[str] = frozenset(synonyms.keys())
        self.reverse_synonyms: dict[str, list[str]] = self._build_reverse(synonyms)

    @staticmethod
    def _build_reverse(synonyms: dict[str, list[str]]) -> dict[str, list[str]]:
        rev: dict[str, set[str]] = {}
        for _cn, en_list in synonyms.items():
            for en in en_list:
                rev.setdefault(en, set()).update(en_list)
        # Stable sort for deterministic output
        return {k: sorted(v) for k, v in rev.items()}

    def tokenize(self, text: str) -> list[str]:
        """NFKC → separators → camelCase BEFORE lower → CJK phrase+bigram → ASCII words → lowercase."""
        normalized = unicodedata.normalize("NFKC", text)
        parts = _SEPARATOR_RE.split(normalized)
        tokens: list[str] = []
        for part in parts:
            if not part:
                continue
            cjk_runs, ascii_runs = _split_cjk_ascii(part)
            # ASCII: camelCase split BEFORE lowercasing
            for ascii_run in ascii_runs:
                for cp in _CAMEL_RE.split(ascii_run):
                    if cp:
                        tokens.append(cp.lower())
            # CJK: phrase extraction + bigrams
            for cjk_run in cjk_runs:
                tokens.extend(self._tokenize_cjk(cjk_run))
        return tokens

    def _tokenize_cjk(self, text: str) -> list[str]:
        """Known phrases (greedy longest) + bigrams for uncovered positions."""
        tokens: list[str] = []
        covered = [False] * len(text)
        max_len = max((len(p) for p in self.known_phrases), default=2)
        # Greedy longest match
        for length in range(max_len, 1, -1):
            j = 0
            while j <= len(text) - length:
                candidate = text[j:j+length]
                if candidate in self.known_phrases and not any(covered[j:j+length]):
                    tokens.append(candidate)
                    for k in range(j, j+length):
                        covered[k] = True
                    j += length
                else:
                    j += 1
        # Bigrams for uncovered
        for j in range(len(text) - 1):
            if not (covered[j] and covered[j+1]):
                tokens.append(text[j:j+2])
        # Remaining uncovered single chars
        for j in range(len(text)):
            if not covered[j]:
                tokens.append(text[j])
        return tokens

    def expand_query(self, tokens: list[str]) -> list[str]:
        expanded: list[str] = []
        for token in tokens:
            expanded.append(token)
            if token in self.synonyms:
                expanded.extend(self.synonyms[token])
            if token in self.reverse_synonyms:
                for syn in self.reverse_synonyms[token]:
                    if syn != token:
                        expanded.append(syn)
        return list(dict.fromkeys(expanded))  # deduplicate, preserve order
```

`CatalogSearchIndex` 接收实例级 tokenizer：

```python
class CatalogSearchIndex:
    def __init__(self, synonyms: dict[str, list[str]]) -> None:
        self._tokenizer = ToolTokenizer(synonyms)
        self._entries: dict[str, UpstreamToolCatalogEntry] = {}
        self._fields: dict[str, BM25Field] = {}
        self._built = False

    def build(self, entries: Mapping[str, UpstreamToolCatalogEntry]) -> None:
        self._entries = dict(entries)
        self._fields = {n: BM25Field(name=n, weight=w) for n, w in FIELD_WEIGHTS.items()}
        tok = self._tokenizer.tokenize
        for doc_id, entry in self._entries.items():
            self._fields["name_tokens"].add_document(doc_id, tok(entry.remote_name))
            self._fields["title"].add_document(doc_id, tok(entry.title))
            self._fields["tags"].add_document(doc_id, [
                t for tag in entry.tags for t in tok(tag)
            ])
            self._fields["alias"].add_document(doc_id, tok(entry.server_alias))
            self._fields["argument_names"].add_document(doc_id, [
                t for arg in entry.argument_names for t in tok(arg)
            ])
            self._fields["description"].add_document(doc_id, tok(entry.description))
        for f in self._fields.values():
            f.finalize()
        self._built = True

    def search(self, query: str, filters: ToolSearchFilters) -> list[ToolSearchResult]:
        # ... structural filtering ...
        # ... exact match ...
        raw_tokens = self._tokenizer.tokenize(query)
        expanded = self._tokenizer.expand_query(raw_tokens)
        # ... BM25 scoring ...
        scored.sort(key=lambda x: (-x[1], x[0]))  # deterministic
        # ...
```

`UpstreamManager` 合并词典后传入：

```python
class UpstreamManager:
    def __init__(self, ..., custom_synonyms=None):
        self._merged_synonyms = merge_synonyms(BUILTIN_SYNONYMS, custom_synonyms)
        # ...

    def _replace_alias_tools(self, alias, new_tools, new_direct, new_catalog, client=None):
        with self._lifecycle_lock:
            # ...
            if next_catalog:
                index = CatalogSearchIndex(synonyms=self._merged_synonyms)
                index.build(next_catalog)
            # ...
```

### 2.10 custom_synonyms 全局配置

```json
{
  "tool_search": {
    "custom_synonyms": {
      "核磁": ["nmr", "spectrum", "spectroscopy"],
      "结构": ["structure", "molecule", "conformation"]
    }
  }
}
```

在 `parse_gateway_config()` 中解析 → 传入 `UpstreamManager`。

### 2.11 Broker Tool Call + schema_digest 校验

```python
"upstream_tool_call": object_schema(
    {
        "name": {"type": "string", "description": "Read-only brokered tool name."},
        "arguments": {"type": "object", "additionalProperties": True},
        "schema_digest": {
            "type": "string",
            "description": "Optional. 32-char hex digest from upstream_tool_describe. "
                           "If provided and mismatched, call rejected with UPSTREAM_SCHEMA_CHANGED.",
        },
    },
    required=["name"],
),
```

Handler 校验：

```python
def upstream_tool_call(self, args):
    name = args["name"]
    state = self.upstream_manager.state
    tool = state.all_tools.get(name)
    if tool is None:
        return upstream_error_result("TOOL_NOT_FOUND", ...)
    if tool.effective_risk != "readonly":
        return upstream_error_result("TOOL_NOT_READONLY", ...)
    digest = args.get("schema_digest")
    if digest and digest != tool.schema_digest:
        return upstream_error_result(
            "UPSTREAM_SCHEMA_CHANGED",
            f"Schema changed. Expected {digest}, current {tool.schema_digest}. "
            f"Call upstream_tool_describe again.",
        )
    owner = self.current_result_owner()
    return self.upstream_manager.call_tool(
        name, args.get("arguments", {}),
        session_id=owner, store_overflow=owner is not None,
    )
```

`upstream_tool_call_mutating` 同理但检查 `effective_risk != "mutating"`。

### 2.12 固定 Instructions + 固定注册

```python
_BROKER_INSTRUCTIONS = (
    "Additional external tools are available through the upstream broker. "
    "Use upstream_tool_search to find a tool, upstream_tool_describe to inspect "
    "its schema, then call it via upstream_tool_call (read-only) or "
    "upstream_tool_call_mutating. "
    "Unknown risk classification defaults to mutating."
)
```

Broker 工具固定注册（upstream_gateway_enabled 时），catalog 为空时返回空结果。

### 2.13 Phase 2 文件清单

| 操作 | 文件 | 变更 |
|---|---|---|
| **NEW** | [upstream_search.py](file:///g:/LLM/coding-tools-mcp/coding_tools_mcp/upstream_search.py) | `ToolTokenizer` (instance-level), `BUILTIN_SYNONYMS`, `merge_synonyms()`, `BM25Field`, `CatalogSearchIndex`, `SearchBackend` Protocol, filters/results |
| MODIFY | [upstream.py](file:///g:/LLM/coding-tools-mcp/coding_tools_mcp/upstream.py) | expose_mode/pinned_tools/tags/tool_policy on Config, `_replace_alias_tools()` 原子更新, `call_tool_raw()` on BaseUpstreamClient, `call_tool()` 唯一 normalize+store+truncate, `ResultStore`, direct 分流逻辑, `_merged_synonyms` |
| MODIFY | [server.py](file:///g:/LLM/coding-tools-mcp/coding_tools_mcp/server.py) | BROKER_READONLY/MUTATING split, `_BROKER_PASSTHROUGH` dispatch, 5 Broker ToolSpec+schemas+handlers, `current_result_owner()` (无 `__default__`), stdio owner, read-only profile 过滤, 固定 instructions, schema_digest 校验, fetch 硬限制 |

---

## Phase 3：Broker Profile（仅搜索层）

```json
{
  "broker_profiles": {
    "coding": {"default_servers": ["github", "browser"], "search_boost": {"github": 2.0}}
  }
}
```

不改变 `tools/list`。只影响搜索过滤和 BM25 boost。

---

## Phase 4：动态激活

`activate_toolset` + `listChanged: true` + `notifications/tools/list_changed`。

---

## Open Questions — 最终答案

| # | 答案 |
|---|---|
| Q1 expose_mode | 旧缺字段 → `"direct"`。新建 → `"broker"` |
| Q2 同义词 | 内置 + 全局 `tool_search.custom_synonyms`。实例级 ToolTokenizer。每词最多 10 扩展 |
| Q3 Result store | session 绑定 + TTL 5min + per-session FIFO 16/16MiB + global 64MiB。无 owner 不存储 |
| Q4 readonly 校验 | 始终硬校验。unknown → mutating。local tool_policy 覆盖 |
| Q5 Digest | SHA-256 hex 32 chars。call 接受可选 schema_digest |
| Q6 Store 语义 | TTL + FIFO，非 LRU |
| Q7 Atomicity | `UpstreamRegistryState` frozen snapshot。`_lifecycle_lock` 保护 state + clients 一致性 |
| Q8 $ref | 检测到 → 降级为 loose schema + 描述说明。不做 JSON Schema resolver |
| Q9 OAuth | 当前隔离单位是 session ID。principal 复合隔离已设计未实现 |

---

## 完整测试清单 (56 项)

### upstream_sanitize.py (19)

| # | 测试 |
|---|---|
| 1 | `sanitize_text()` 截断 + "…" |
| 2 | `sanitize_text()` 去除控制字符 |
| 3 | `sanitize_text()` 合并空白 |
| 4 | `sanitize_definition()` 剥离未知顶层字段 |
| 5 | `sanitize_definition()` 限制顶层 description |
| 6 | `_sanitize_schema()` 递归限制每层 description |
| 7 | `_sanitize_schema()` cap property + 同步 required |
| 8 | `_sanitize_schema()` cap enum |
| 9 | `_sanitize_schema()` 超深度 → loose schema |
| 10 | `_sanitize_schema()` 递归 additionalProperties / not |
| 11 | `_sanitize_value()` 限制 default/const 深度和长度 |
| 12 | 4-stage 硬降级链全路径 |
| 13 | property description 恶意内容被截断 |
| 14 | `schema_digest()` 相同 schema → 相同 32-hex digest |
| 15 | `classify_risk()` local policy > annotation > default |
| 16 | `$ref` + `$defs` schema → 降级为 loose schema（不产生悬空引用） |
| 17 | 非法 Schema 字段类型不进入 public definition（strict type check） |
| 18 | `outputSchema` 被递归清洗 |
| 19 | oversized definition 经过全部降级阶段仍低于 MAX_DEFINITION_BYTES |

### upstream_search.py (19)

| # | 测试 |
|---|---|
| 20 | `tokenize("searchLibrary")` → `["search", "library"]` |
| 21 | `tokenize("搜索文献")` → 包含 `"搜索"`, `"文献"` |
| 22 | `tokenize("snake_case_name")` → `["snake", "case", "name"]` |
| 23 | `expand_query()` 中文→英文同义词 |
| 24 | `expand_query()` 英文→英文互扩 |
| 25 | `expand_query()` 去重（`dict.fromkeys`） |
| 26 | `BM25Field.score()` 不因重复 token 加倍 |
| 27 | 字段权重：name > title > description |
| 28 | 精确匹配：public_name 立即返回 |
| 29 | 精确匹配：alias/remote_name |
| 30 | remote_name 仅在候选中唯一时返回 |
| 31 | 两个 server 同名 → 不快速返回 |
| 32 | 结构化过滤：server + read_only + tags + name_prefix（含 NFKC） |
| 33 | min_score 阈值 |
| 34 | limit 截断 |
| 35 | 相同分数按 tool_id 字母序 |
| 36 | 中文 "搜索文献" 匹配英文 "search_library" |
| 37 | custom synonym "核磁" 匹配 title/description 中的 "nmr" |
| 38 | ToolTokenizer 实例持有 custom phrases 不污染全局 |

### Broker 集成测试 (18)

| # | 测试 |
|---|---|
| 39 | expose_mode=broker 工具不在 tool_definitions() |
| 40 | broker + pinned_tools 的 pinned 在 tool_definitions() |
| 41 | read-only profile 不暴露 upstream_tool_call_mutating |
| 42 | read-only profile search 强制 read_only=true |
| 43 | passthrough 不被 tool_result() 二次包裹 |
| 44 | upstream isError=true 正确透传 |
| 45 | upstream_tool_call 拒绝 mutating |
| 46 | upstream_tool_call_mutating 拒绝 readonly |
| 47 | 大结果先存储后截断、handle 正确 |
| 48 | upstream_result_fetch 分段读取 |
| 49 | handle 跨 session 被拒绝 |
| 50 | 无 owner → 不生成 handle |
| 51 | handle TTL 过期 → null |
| 52 | schema_digest 不匹配 → UPSTREAM_SCHEMA_CHANGED |
| 53 | start/stop 并发期间搜索结果始终可调用（atomic state） |
| 54 | Broker 工具在 catalog 为空时仍在 tools/list |
| 55 | direct tools 输出顺序固定（sorted） |
| 56 | oversized image/resource content block 不被截断成无效数据 |
