# Upstream Tool Broker — v6 分步实施任务书

> 状态：**Architecture approved / Implementation-ready**
>
> 本任务书将 v6 技术规格改造成适合短上下文模型逐步执行的工作包，并补入生命周期一致性、结果硬预算和 Broker 参数验证三项强制约束。
>
> **执行优先级：本任务书正文 > 附录 A 技术规格。**  
> 附录中与正文冲突的伪代码、字段名或并发语义，以正文为准。

---

## 0. 目标与边界

### 0.1 总目标

将 `coding-tools-mcp` 从“全量透传所有外部 MCP 工具”的聚合器，升级为：

- 少量高频工具直接进入 `tools/list`；
- 其他外部工具进入服务端 catalog；
- Agent 通过搜索、描述、调用 Broker 按需使用；
- read-only 与 mutating 调用严格分离；
- 工具定义、结果大小和搜索返回均有硬预算；
- 多客户端、start/stop/restart 并发时不存在跨代工具/连接混用；
- 保持旧配置缺少 `expose_mode` 时的行为不变。

### 0.2 不在本轮实现

以下内容只保留接口与后续扩展位，不在 Phase 1–2 强行实现：

- 向量检索或 embedding；
- 完整 JSON Schema `$ref` resolver；
- OAuth principal + MCP session 复合隔离；
- 动态 `tools/listChanged`；
- workspace 切换时动态改变 direct 工具集；
- 自动 `expose_mode=auto`；
- 长期持久化 ResultStore。

### 0.3 最终可见工具

upstream gateway 启用时，Broker 工具固定存在：

```text
upstream_tool_search
upstream_tool_describe
upstream_tool_call
upstream_tool_call_mutating
upstream_result_fetch
```

`tool_profile=read-only` 时不暴露：

```text
upstream_tool_call_mutating
```

---

## 1. 短上下文执行协议

### 1.1 一次只执行一个任务卡

每个 Agent/session 只能执行一个 `Txx` 任务卡：

1. 只读取该任务卡列出的文件和附录章节；
2. 读取上一任务的 handoff；
3. 不提前实现下一任务；
4. 完成本任务的定点测试；
5. 创建一个独立提交；
6. 输出 handoff 后停止。

禁止把整份附录 A 全量塞入每个 Agent 的上下文。

### 1.2 每次新 Agent 的输入组成

建议只提供：

```text
1. 当前任务卡全文；
2. 上一任务 handoff；
3. git status / HEAD；
4. 当前任务卡指定的 2–5 个代码文件或局部行；
5. 本任务失败测试输出。
```

### 1.3 每个任务的提交规则

- 工作树开始时必须 clean；
- 不混入无关重构；
- 一项任务一个提交；
- 提交消息使用任务卡给出的建议；
- 测试失败不能标记 complete；
- 若发现设计冲突，停止在当前任务并写入 handoff，不跨任务修补。

### 1.4 Handoff 模板

每个任务结束必须输出并保存到提交说明或 `docs/handoff`：

```markdown
## Txx Handoff

Status: complete | blocked | partial
HEAD: <full sha>
Commit: <short sha> <subject>
Worktree: clean | dirty

Implemented:
- ...

Files changed:
- ...

Tests:
- command: ...
  result: PASS | FAIL

Compatibility:
- ...

Known limitations:
- ...

Next task prerequisites:
- ...

Do not redo:
- ...
```

---

## 2. 强制架构约束

以下约束覆盖附录中的旧伪代码。

### 2.1 Registry 与 client 必须属于同一代快照

最终注册状态必须包含：

```python
@dataclass(frozen=True)
class UpstreamRegistryState:
    all_tools: Mapping[str, UpstreamTool]
    direct_tool_names: frozenset[str]
    catalog: Mapping[str, UpstreamToolCatalogEntry]
    search_index: CatalogSearchIndex | None
    clients: Mapping[str, BaseUpstreamClient]
    generation: int
```

要求：

- `all_tools`、`catalog`、`clients` 必须来自同一个 generation；
- `call_tool()` 只能从同一个局部 `state = self._state` 中同时取得 tool 和 client；
- 禁止“旧 state 的 tool + 新 clients 字典中的 client”；
- 使用 `MappingProxyType` 或等价只读映射，避免 frozen dataclass 内部字典被修改；
- start/restart：新 client 完成 initialize + tools/list 后才发布新 state；
- stop：先原子发布移除 alias 的新 state，再关闭旧 client；
- 旧 client 的关闭必须等待已进入调用临界区的请求完成；
- 与 stop/restart 竞争但尚未取得 client lease 的调用，允许返回 retryable not-available，不要求强行成功；
- 不允许死锁、跨代执行、使用已关闭 client 或目录/工具状态撕裂。

建议 client 生命周期接口：

```python
class BaseUpstreamClient:
    def __init__(...):
        self._lifecycle_lock = threading.RLock()
        self._closed = False

    def call_tool_raw(...):
        with self._lifecycle_lock:
            if self._closed:
                raise UpstreamError(
                    "UPSTREAM_NOT_AVAILABLE",
                    "Upstream client is closed.",
                    retryable=True,
                )
            return self._call_tool_raw_locked(...)

    def close(self):
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._close_locked()
```

不要直接把现有 transport 的全部 `request()` 强制改造成同一个私有函数；可以采用最小改动的 wrapper，但必须保证 `call_tool_raw()` 与 `close()` 互斥。

### 2.2 Broker 结果必须有最终 envelope 硬预算

内容感知截断完成后必须再次序列化检查。

保证：

```text
len(final MCP tool result envelope) <= RESULT_INLINE_MAX
```

若第一次截断后仍超限，返回最小降级 envelope：

```python
{
    "content": [{
        "type": "text",
        "text": "Upstream result exceeded the inline budget. "
                "Use upstream_result_fetch with the returned handle."
    }],
    "structuredContent": {
        "_truncated": True,
        "_original_bytes": original_bytes,
        "_result_handle": handle,  # 有 handle 时
    },
    "isError": original_is_error,
}
```

要求：

- 多个 text block 的总和不能绕过预算；
- 未知 content type 在超限路径中不得原样保留；
- image/audio/blob 不能裁成损坏 base64，必须整块省略；
- resource URI 可保留，嵌入 text 可截断，blob 必须省略；
- 即使原结果没有 `structuredContent`，也必须创建 metadata dict；
- 没有真实 session owner 时不生成 handle，但仍返回小型截断结果；
- direct upstream 调用默认不存 overflow，Broker 调用才按 session 存储。

### 2.3 Broker 调用必须验证公开 Schema

Broker 转发前必须验证：

```python
tool.public_definition["inputSchema"]
```

不能只依赖上游 MCP 自己校验。

首版支持的验证子集：

- `type`：object、array、string、integer、number、boolean、null；
- type 数组，例如 `["string", "null"]`；
- `properties`；
- `required`；
- `additionalProperties`；
- `enum`、`const`；
- `minimum`、`maximum`；
- `minLength`、`maxLength`；
- `minItems`、`maxItems`；
- `oneOf`、`anyOf`、`allOf`；
- 数组 `items`。

降级为 loose schema 时自然允许任意对象。

错误格式：

```text
UPSTREAM_ARGUMENTS_INVALID
category=validation
```

### 2.4 Digest 以公开 Schema 为准

数据结构采用：

```python
public_schema_digest: str
raw_schema_digest: str | None
```

规则：

- Broker search/describe/call 使用 `public_schema_digest`；
- raw digest 仅用于 admin/诊断；
- 附录中所有普通 `schema_digest` 均解释为 public digest；
- readonly call 的 digest 可选；
- mutating call 的 digest **必填**；
- mutating call 未 describe 或 digest 过期时拒绝；
- public Schema 未变化时，不因 raw 中被 sanitizer 删除的字段改变而使 public digest 漂移。

### 2.5 Schema sanitizer 额外约束

- 顶层 definition 必须显式构建，不应先复制再删；
- `title`、`description`、`inputSchema`、`outputSchema` 类型不合法时不得进入 public definition；
- property value 不是 dict 时降级为 loose schema，不原样透传；
- JSON Schema `type` 同时支持字符串和字符串数组；
- `$ref` 节点降级，不保留悬空引用；
- 单工具公开定义最终必须满足 `MAX_DEFINITION_BYTES`；
- sanitizer 只称为 untrusted metadata containment，不称为彻底防提示注入。

### 2.6 Result fetch 硬限制

- offset 单位：Unicode codepoint；
- `offset >= 0`；
- `1 <= limit <= 32000`；
- Schema 与运行时双重 clamp；
- fetch 不允许一次重新取回 8 MiB 全文；
- ResultStore 是 TTL + FIFO，不称为 LRU。

### 2.7 当前隔离边界

本轮 ResultStore owner：

```text
MCP session ID
```

stdio 使用进程级随机 owner。

OAuth principal 复合隔离仅记录为后续工作；不得伪称已完成。

---

## 3. 任务依赖图

```text
T00 基线冻结
  ↓
T01 Registry 快照迁移
  ↓
T02 Schema sanitizer + 风险 + digest
  ↓
T03 结果预算与截断
  ↓
T04 BM25 搜索模块
  ↓
T05 配置、catalog、direct/broker 分流
  ↓
T06 Broker 搜索与描述工具
  ↓
T07 Broker 调用、参数验证、passthrough
  ↓
T08 ResultStore 与 result_fetch
  ↓
T09 client/state 生命周期并发加固
  ↓
T10 配置管理、文档与兼容迁移
  ↓
T11 全量验证、性能与发布交接
```

T01–T03 对现有 direct 工具行为保持兼容。  
只有 T05 以后才允许已有配置显式切换到 broker。

---

# 4. 分步任务卡

## T00 — 基线冻结与测试地图

### 目标

建立实施基线，不修改运行逻辑。

### 只读取

- `coding_tools_mcp/upstream.py`
- `coding_tools_mcp/server.py`
- `tests/compliance/test_upstream_gateway.py`
- upstream/admin 配置加载相关测试
- 附录 A 的“数据结构总览”和测试清单

### 工作内容

1. 记录：
   - HEAD；
   - 当前 upstream 工具数；
   - `tool_profile=full/read-only` 下 tools/list；
   - 当前 Zotero 等 upstream 状态；
   - 全量测试结果。
2. 找出：
   - `UpstreamTool` 所有使用点；
   - `self.tools` 所有读写点；
   - `BaseUpstreamClient.call_tool()` 所有调用点；
   - HTTP/stdio session_id 注入路径；
   - tool definition/schema validation 现有公共函数。
3. 新建实施 handoff 文档，列出每个任务的目标文件与现有测试入口。
4. 不改生产行为。

### 验收

- 工作树 clean；
- 全量测试基线明确；
- 所有迁移触点有清单；
- 没有功能提交混入。

### 建议提交

```text
docs(broker): freeze upstream broker implementation baseline
```

---

## T01 — Registry 快照迁移（行为不变）

### 目标

将当前 `self.tools + self.clients` 迁移为同代只读 Registry snapshot，但仍保持全部 upstream 工具 direct。

### 只读取

- `upstream.py`：Base clients、UpstreamManager、start/stop/init
- `server.py`：list_tools、direct upstream dispatch
- T00 handoff
- 附录 A 的“不可变注册状态”

### 实现

1. 新增：
   - `UpstreamRegistryState`
   - `generation`
   - `clients` 映射进入 state
   - `MappingProxyType`
2. 保持：
   - 所有工具 direct；
   - 原工具名称；
   - 原 annotations 行为；
   - 原配置语义。
3. 所有读方法只读一次：
   ```python
   state = self._state
   ```
4. start/restart：
   - 在局部构建 client 和工具；
   - 完成 initialize/list_tools；
   - 一次发布新 generation。
5. stop：
   - 一次发布移除 alias 的 state；
   - 发布后再关闭旧 client。
6. 暂不加入 catalog 搜索和 Broker。
7. `state` 属性不允许调用者修改内部映射。

### 并发语义

本任务先保证：

- 不出现旧工具和新 client 混用；
- stop 后的新读取看不到已删除工具；
- 已取得旧 state 的调用要么安全完成，要么返回 retryable closed/not-available；
- 不要求引入最终 client lease 测试，T09 再加固。

### 测试

- 原 upstream gateway 测试全部通过；
- tools/list 顺序稳定；
- state mapping 无法外部修改；
- restart 后 generation 增加；
- 缺少 alias 时结构化错误保持一致。

### 建议提交

```text
refactor(upstream): migrate registry to atomic snapshots
```

---

## T02 — Schema sanitizer、风险分类与公开 Digest

### 目标

建立 raw/public definition 双轨，并限制外部元数据进入模型上下文。

### 只读取

- `upstream.py`：namespaced definition、profiled definition
- 新建 `upstream_sanitize.py`
- schema/tool definition 测试
- T01 handoff
- 附录 A Phase 1 sanitizer

### 实现

1. 新建 `upstream_sanitize.py`：
   - 文本控制字符清除；
   - 递归 Schema 类型检查；
   - property/enum/depth/value 限额；
   - `$ref` 降级；
   - outputSchema 递归处理；
   - 最终 8 KiB 硬预算；
   - `type` 支持 str/list；
   - 顶层显式构建。
2. `UpstreamTool` 改为：
   ```python
   raw_definition
   public_definition
   effective_risk
   public_schema_digest
   raw_schema_digest
   ```
3. `tool_definitions()` 只使用 public definition。
4. 风险：
   - local policy > annotation > unknown=mutating；
   - `compat-readonly-all` 只改变对客户端的兼容展示，不改变 effective risk。
5. Digest：
   - public digest 由 public inputSchema 生成；
   - raw digest 可选，仅诊断。

### 禁止

- 不实现 Broker；
- 不切换 expose_mode；
- 不把 raw description 暴露给普通 describe；
- 不声称 sanitizer 消除提示注入。

### 测试

至少覆盖附录 sanitizer 测试，并增加：

- 顶层非法 title/outputSchema 类型被丢弃；
- 非 dict property schema 降级；
- `type=["string","null"]` 保留；
- raw 改变但 public 不变时 public digest 不变；
- hard fallback 后仍严格低于上限。

### 建议提交

```text
feat(upstream): contain untrusted tool metadata
```

---

## T03 — 结果预算与内容感知截断（无 handle）

### 目标

为现有 direct upstream 结果增加安全的 inline 硬预算，暂不引入 ResultStore。

### 只读取

- `upstream.py`：normalize_tool_result、client call path
- MCP content block 相关测试
- T02 handoff
- 附录 A “结果截断”

### 实现

1. `normalize_tool_result()` 与 budget/truncate 责任分离。
2. 新增 content-type-aware truncation：
   - text：按累计 envelope 预算截断；
   - image/audio/blob：整块省略；
   - resource：保留 URI、截断 text、移除 blob；
   - unknown：超限时转占位符；
   - structuredContent：递归裁剪。
3. 截断后再次序列化。
4. 若仍超过 `RESULT_INLINE_MAX`，返回最小 envelope。
5. 创建 metadata：
   ```text
   _truncated
   _original_bytes
   ```
6. Phase 1 不生成 handle。
7. 保留原 `isError`。

### 关键验收

- 最终 envelope 始终不超过预算；
- 多 text block 不能叠加绕过；
- image/base64 不返回损坏数据；
- 没有 structuredContent 时仍有 metadata；
- 小结果保持原样；
- direct 调用行为除超大结果外不变。

### 建议提交

```text
fix(upstream): enforce hard result envelope budgets
```

---

## T04 — 独立 BM25 Catalog 搜索模块

### 目标

在不接入 Runtime Broker 的情况下完成可单测的搜索引擎。

### 只读取

- 新建 `upstream_search.py`
- T03 handoff
- 附录 A 搜索引擎章节
- 不需要加载整个 `server.py`

### 实现

1. `ToolTokenizer` 实例级：
   - NFKC；
   - separator；
   - camelCase；
   - CJK 已知短语、bigram、必要 unigram；
   - custom synonyms 不污染全局。
2. `merge_synonyms()`：
   - built-in + 全局 custom；
   - 每词最多 10 个；
   - 去重且顺序稳定。
3. 字段 BM25：
   - name 5；
   - title 4；
   - tags 4；
   - alias 3；
   - argument names 2；
   - description 1。
4. 结构化过滤：
   - server；
   - readonly；
   - tags；
   - name_prefix。
5. 快速路径：
   - public name；
   - alias/remote；
   - 唯一 remote；
   - 唯一 prefix。
6. 结果：
   - 默认 5；
   - 最大 20；
   - 不返回 Schema；
   - 稳定排序。
7. SearchBackend Protocol 与实现签名保持一致。

### 测试

执行附录 A 搜索测试全部项目，尤其：

- `searchLibrary`；
- “搜索文献”；
- custom “核磁” → nmr；
- 两 server 同 remote_name；
- 同义词不重复加分；
- 不同 tokenizer 实例互不污染。

### 建议提交

```text
feat(upstream): add field-weighted broker search index
```

---

## T05 — 配置、Catalog 与 direct/broker 分流

### 目标

接入 catalog 和暴露模式，但暂不提供 Broker 调用工具。

### 依赖警告

本任务与 T06 必须连续完成后才能让用户实际切换已有 MCP 到 broker。  
T05 提交可存在，但管理台/UI 不应在 T06 前主动引导用户启用 broker。

### 只读取

- `upstream.py` 配置解析、manager 初始化
- upstream config/admin 读写代码
- `mcp-servers.json` 示例
- T04 handoff
- 附录 A expose_mode 章节

### 实现

配置字段：

```text
expose_mode: direct | broker
pinned_tools
tags
tool_policy
tool_search.custom_synonyms（全局）
```

规则：

- 旧配置缺字段 → direct；
- direct：全部进入 direct + catalog；
- broker：只有 pinned 进入 direct，全部进入 catalog；
- include/exclude 先过滤；
- collision 基于全部 all_tools；
- pinned 使用 remote_name；
- catalog 保存紧凑 public metadata；
- search index 与 state 同代发布；
- context budget 只报告 upstream direct exposure，并注明不含 built-in/admin。

### 测试

- 旧配置行为完全不变；
- broker-only 不在 tools/list；
- pinned 在 tools/list；
- direct/catalog 统计正确；
- start/stop/restart 后 state 内部一致；
- custom synonyms 配置进入 tokenizer。

### 建议提交

```text
feat(upstream): add catalog and exposure modes
```

---

## T06 — Broker 搜索、描述与固定注册

### 目标

提供只读发现能力，让 broker-only 工具真正可发现，但暂不执行外部调用。

### 只读取

- `server.py`：tool registry、schemas、handlers、instructions
- `upstream.py`：state/search_catalog
- T05 handoff
- 附录 A Broker search/describe

### 实现

固定注册：

```text
upstream_tool_search
upstream_tool_describe
```

并同时预注册后续工具定义名称，但尚未完成的 call/fetch 不得暴露；或者在本任务只注册 search/describe，在 T07/T08 完成时再固定扩展。最终状态必须固定五个。

search：

- read-only profile 强制 `read_only=True`；
- 默认 limit=5，max=20；
- 返回紧凑结果和 public digest；
- 不返回完整 Schema。

describe：

- 只返回 `public_definition`；
- 返回 `public_schema_digest`；
- 不返回 raw definition；
- 对不存在工具返回结构化 not-found。

instructions：

- 固定短文本；
- 不列动态 server/tool 数量；
- 不引入 `listChanged=true`。

### 测试

- catalog 空时 search 返回空；
- read-only 搜不到 mutating；
- describe 不泄漏 raw；
- instructions 为常量长度；
- 工具定义顺序稳定。

### 建议提交

```text
feat(broker): expose upstream search and describe
```

---

## T07 — Broker 调用、参数验证与 passthrough

### 目标

实现 readonly/mutating Broker 执行路径，确保 envelope 不二次包裹。

### 只读取

- `server.py`：`_call_tool()`、local handlers、argument validation
- `upstream.py`：client call path
- T06 handoff
- 附录 A Broker passthrough
- 当前项目已有 schema validation helper

### 实现

1. 新增：
   ```text
   upstream_tool_call
   upstream_tool_call_mutating
   ```
2. passthrough：
   - handler 返回完整 MCP envelope；
   - 不再进入 `tool_result()`；
   - `isError` 原样传递。
3. 风险硬校验：
   - readonly 只允许 effective_risk=readonly；
   - mutating 只允许 mutating/unknown；
   - read-only profile 不暴露 mutating call。
4. 参数验证：
   - 使用 public inputSchema；
   - 实现任务书 §2.3 的 Schema 子集；
   - 错误返回 `UPSTREAM_ARGUMENTS_INVALID`。
5. Digest：
   - readonly 可选；
   - mutating 必填；
   - 必须是 32-char lower hex；
   - 不匹配返回 `UPSTREAM_SCHEMA_CHANGED`。
6. `BaseUpstreamClient.call_tool_raw()`：
   - 返回 raw tools/call result；
   - Manager 成为唯一 normalize/budget 责任方；
   - 原 `call_tool()` 可保留兼容 wrapper，但新 Manager 路径只调用 raw。
7. 本任务仍不生成 result handle；超大 Broker 结果按 T03 最小 envelope 返回。

### 测试

- passthrough 无嵌套；
- upstream isError=true 保留；
- readonly/mutating 互相拒绝；
- mutating 无 digest 拒绝；
- 参数 required/type/bounds/extra fields 验证；
- loose schema 保持兼容；
- public digest 变化检测；
- raw-only 变化不影响 public digest。

### 建议提交

```text
feat(broker): validate and dispatch upstream calls
```

---

## T08 — Session ResultStore 与分段读取

### 目标

为 Broker 超大结果增加 session 隔离的短期存储和受限 fetch。

### 只读取

- `upstream.py`：Manager call/budget
- `server.py`：tool context/session path
- HTTP/stdio transport 启动代码
- T07 handoff
- 附录 A ResultStore

### 实现

ResultStore：

```text
single result <= 8 MiB
per session <= 16 handles / 16 MiB
global <= 64 MiB
TTL = 5 min
FIFO
```

owner：

- HTTP：真实 MCP session ID；
- stdio：进程级随机 owner；
- 无 owner：不存储；
- 不使用 `__default__`；
- OAuth principal 复合隔离不在本任务。

fetch：

- offset=Unicode codepoint；
- limit 最大 32000；
- Schema + runtime clamp；
- 跨 session、过期、未知统一返回 not found；
- 不暴露 owner 是否存在，避免信息泄漏。

调用管线：

```text
raw result
→ normalize
→ serialize original
→ store original（有 owner）
→ content-aware truncate
→ final hard envelope check
→ attach handle metadata
```

### 测试

- 先存后截；
- 无 structuredContent 仍有 handle；
- 跨 session 拒绝；
- 无 owner 无 handle；
- TTL/FIFO/bytes；
- fetch limit 无法绕过；
- stdio owner 稳定到进程生命周期；
- direct upstream 调用不生成 handle。

### 建议提交

```text
feat(broker): add session-scoped result paging
```

---

## T09 — Lifecycle 并发、client lease 与 restart/stop 加固

### 目标

完成任务书 §2.1 的最终并发语义，消除跨 generation client/tool 混用。

### 只读取

- `upstream.py`：所有 client class、state replacement、start/stop/close
- T08 handoff
- 并发测试文件
- 不需要重新加载搜索实现细节

### 实现

1. state 内 clients 与 tool/catalog/index 同代。
2. Base client 增加 lifecycle lock + closed 状态。
3. call_tool_raw 与 close 互斥：
   - 已进入 call 的请求安全完成；
   - close 等待；
   - close 后新 call 返回 retryable error。
4. restart：
   - 新 client 在发布前完全 initialize；
   - 一次 state swap；
   - 发布后关闭 old client。
5. stop：
   - 一次 state swap 移除工具+client；
   - 再关闭 old client。
6. Manager close：
   - 原子发布空 state；
   - 再关闭全部旧 client。
7. status 更新不得制造“status initialized 但 state 没工具”的长期状态。
8. 不在生命周期锁中执行长时间网络 initialize/list_tools；只在发布时加锁。
9. 索引构建：
   - 正确性优先；
   - 记录耗时；
   - 不声称必然 sub-ms；
   - 若实测过慢，记录后续 CAS 优化，不在本任务过度设计。

### 允许的竞争结果

- 已取得 client lease 的调用完成；
- 未取得 lease 且与 stop/restart 竞争的调用可返回 retryable；
- 搜索结果可能在下一次 call 前过期并返回 TOOL_NOT_FOUND；
- 禁止跨代调用、死锁、损坏结果或进程中途被无保护关闭。

### 测试

- old tool 不会用 new client；
- stop 不产生 client 已移除但新 state 仍暴露工具的发布状态；
- 已开始 call 时 stop 等待；
- close 幂等；
- 50–100 次并发 restart/call/search 无死锁；
- generation 单调递增；
- state mapping 只读。

### 建议提交

```text
fix(upstream): make client lifecycle generation-safe
```

---

## T10 — 配置管理、兼容性、文档与运维报告

### 目标

让新增配置可被管理端安全读写，并明确迁移行为。

### 只读取

- upstream/admin config parsing
- 管理页面或设置模型相关代码
- README / browser-client docs
- T09 handoff
- 附录 A Open Questions

### 实现

配置验证：

- expose_mode enum；
- pinned/include/exclude 只接受字符串数组；
- tool_policy 只接受 readonly/mutating；
- custom synonym key/value 限长与数量；
- tags 规范化；
- 旧配置缺 expose_mode → direct。

产品默认：

- 解析旧配置默认 direct；
- 管理台新建 upstream 默认 broker；
- direct → broker 保存时提示：
  - 哪些工具从 tools/list 消失；
  - 哪些 pinned 保留；
  - 当前 `listChanged=false`，客户端需要重新连接。

运维：

- context budget 报告：
  - upstream direct count/bytes；
  - catalog/broker-only；
  - top schemas；
  - 明确不含 built-in/admin；
- server_info/status 显示 direct/catalog 统计；
- 不输出敏感 env/header/token。

文档：

- Broker 工作流；
- read-only/mutating；
- result paging；
- digest；
- session 隔离边界；
- compatibility/migration；
- Phase 3/4 未实现项。

### 测试

- 配置 round-trip；
- 旧配置兼容；
- 非法字段拒绝；
- admin 输出脱敏；
- UI/设置默认值正确；
- 文档示例可解析。

### 建议提交

```text
docs(config): document broker exposure and migration
```

---

## T11 — 全量验证、性能门槛与发布交接

### 目标

不新增功能，只验证并冻结实施结果。

### 只读取

- 所有 T00–T10 handoff
- 测试失败涉及的文件
- 最终 diff
- 附录 A 测试清单

### 必做验证

1. 定点测试：
   - sanitizer；
   - search；
   - Broker；
   - ResultStore；
   - lifecycle/concurrency；
   - config compatibility。
2. 全量测试。
3. 手工 smoke：
   - direct upstream；
   - broker-only Zotero；
   - readonly profile；
   - mutating digest；
   - large result fetch；
   - start/stop/restart。
4. Context 对比：
   - 改造前 direct tools/schema bytes；
   - broker 后 direct tools/schema bytes；
   - catalog 总量。
5. 搜索质量：
   - 至少 30 个中英文 query fixture；
   - top-1 / top-5 命中；
   - 记录失败案例，不临时堆同义词掩盖架构问题。
6. 性能：
   - index build；
   - search p50/p95；
   - state swap；
   - ResultStore；
   - 记录数据，不硬编码不可靠的 sub-ms 声称。
7. 安全：
   - raw definition 不进入普通工具输出；
   - mutating 无 digest 不执行；
   - cross-session handle 不可读；
   - unknown annotations=mutating；
   - final envelope 不超预算。
8. Git：
   - clean worktree；
   - 每任务提交完整；
   - release notes/handoff。

### 完成标准

```text
Phase 1: complete
Phase 2: complete
Phase 3: pending
Phase 4: pending
```

### 建议提交

```text
docs(handoff): record upstream broker validation
```

---

## 5. 全局验收矩阵

| 维度 | 必须满足 |
|---|---|
| 上下文 | broker-only 工具完整 Schema 不进入 tools/list |
| 搜索 | >50 工具使用 BM25 字段加权，不返回全部摘要 |
| Schema | public definition 有递归清洗与硬字节上限 |
| 风险 | unknown 默认 mutating；local policy 可覆盖 |
| 只读 | read-only profile 无 mutating direct/call |
| 写操作 | mutating call 必须有匹配的 public digest |
| 参数 | Broker 在网关侧验证 public inputSchema |
| 结果 | 最终 MCP envelope 有硬预算 |
| 大结果 | session handle + TTL/FIFO + fetch cap |
| 并发 | tool/catalog/client/index 同 generation |
| 生命周期 | stop/restart 不跨代，不中途无保护关闭 client |
| 兼容 | 旧配置缺 expose_mode 仍为 direct |
| 动态工具 | Phase 2 仍为 listChanged=false |
| 隔离 | 当前 owner 为 MCP session；不伪称 principal 隔离 |
| 可观测 | budget 报告不含敏感信息，不误称总上下文 |

---

## 6. Agent 启动提示模板

每次开新 Agent，可使用：

```text
定位到 G:\LLM\coding-tools-mcp。

执行《Upstream Tool Broker — v6 分步实施任务书》的 Txx。
只执行 Txx，不进入下一任务。

开始前：
1. 确认上一任务 handoff 和 HEAD。
2. 确认 worktree clean。
3. 只读取 Txx 指定文件与必要局部代码。
4. 保持任务书“强制架构约束”。

完成后：
1. 运行 Txx 定点测试。
2. 创建一个本地提交。
3. 输出 Txx Handoff。
4. 停止，不继续下一任务。
```

---

## 7. 计划状态

```text
Architecture: approved
Task decomposition: complete
Phase 1 implementation: pending
Phase 2 implementation: pending
Phase 3 profile: deferred
Phase 4 listChanged: deferred
```

---

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
