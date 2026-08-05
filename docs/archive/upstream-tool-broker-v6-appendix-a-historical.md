# Upstream Tool Broker v6 — Historical Appendix A

> Archived from the execution taskbook during T12. This file is non-normative and contains superseded `tool_profile`, dynamic lifecycle, and `listChanged=true` examples.


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
