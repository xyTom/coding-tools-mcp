# 多工作区与 OAuth Agent/Token 持久化管理实施计划

> 状态：待实施  
> 目标读者：接手实现的开发 Agent、代码审查者、测试 Agent  
> 最后更新：2026-07-28  
> 实施原则：小步提交、每个提交保持系统可运行、默认安全、兼容已有部署

## 1. 执行摘要

本计划解决三个相互关联的问题：

1. 服务器目前只绑定一个 Workspace，无法由 WebUI 配置和管理多个 Workspace。
2. OAuth signing secret 与服务器设置默认跟随 Workspace 路径，重启或切换 Workspace 后可能重新生成，导致远程 Agent 已保存的 access token 立即失效并要求重新认证。
3. 当前 access token 是完全无状态的 JWT，没有 Agent 身份、grant 标识、`jti` 或持久化撤销状态，因此 WebUI 无法回答“哪些 Agent 已连接”，也无法对单个 Agent 或单个 access token 进行管理。

目标架构包含三个深模块：

- **Server Settings Store**：提供固定配置路径、schema、迁移、原子写、脱敏和 secret 引用。
- **Workspace Catalog**：管理多个具名 Workspace，并为每个 MCP session 保存 active Workspace 和 default cwd。
- **OAuth Authorization Store**：持久化 OAuth Client、Authorization Grant、access-token 元数据、refresh-token 哈希、signing-key 元数据和审计事件。

完成后，远程 Agent 首次人工授权后可以跨服务器重启持续连接。支持 refresh token 的 Agent 自动续期；不支持 refresh token 的 Agent 可使用受追踪、可撤销的长期 access token 兼容模式。WebUI 可以查看、禁用、撤销和审计 Agent 连接，但不会显示原始 token 或 secret。

## 2. Problem Statement

### 2.1 用户问题

- 远程 Agent 已完成 OAuth 认证并保存 access token。
- 服务器重启后，OAuth signing secret 或 issuer 发生变化。
- 原 access token 的 HS256 签名、issuer 或 audience 校验失败。
- Agent 被迫重新执行浏览器授权流程。
- 管理员无法在 WebUI 中识别哪些 Agent 已经获得授权，也无法有选择地撤销某个 Agent 或某个 token。

### 2.2 当前实现限制

- OAuth access token 只包含 issuer、audience、签发时间、过期时间和 scope。
- access token 没有 `client_id`、grant ID、subject 或 `jti`。
- OAuth Client 配置是单值；未配置固定 client ID 时，任意非空 client ID 都会被接受。
- Authorization Code 只存在内存中，兑换后没有留下持久化授权记录。
- token endpoint 只支持 Authorization Code grant，不支持 Refresh Token grant。
- signing secret 使用单个原始值，没有 `kid` 或密钥轮换状态。
- WebUI 中展示的 HTTP session 是运行时连接状态，不等同于持久化 OAuth Agent 授权。
- 启动设置默认存储在当前 Workspace 内，配置路径与 Workspace 互相依赖。
- 保存启动设置的响应可能包含刚输入的 secret 原文。
- 单 Workspace 假设存在于路径解析、会话 cwd、exec、Landlock、patch checkpoint、trace 和状态输出中。

## 3. Goals

### 3.1 OAuth 目标

- OAuth signing key、issuer 和授权数据库跨重启保持稳定。
- 首次授权后，在授权未撤销且凭据未过期的情况下，不要求用户重复人工认证。
- 每个 Agent 都有独立 OAuth Client 记录和 Authorization Grant。
- 每个新 access token 都可归属到 Agent、Grant 和唯一 `jti`。
- WebUI 可以查看 Agent、Grant、token 状态、最近使用时间和审计事件。
- WebUI 可以撤销单个 access token、整个 Grant 或整个 Agent。
- 支持 refresh-token 自动续期、轮换和重用检测。
- 为不支持 refresh token 的现有 Agent 提供长期 access-token 兼容模式。
- 保留旧 token 的平滑迁移期，避免升级本身立即使所有远程 Agent 下线。

### 3.2 Workspace 目标

- WebUI 可以添加、编辑、禁用、删除和选择默认 Workspace。
- 每个 Workspace 具有稳定 ID、显示名称和根目录。
- 每个 MCP session 维护独立的 active Workspace 和 default cwd。
- 文件、Git、patch、exec 等工具继续只访问当前 session 的 active Workspace。
- 不允许工具通过绝对路径、路径遍历、符号链接或命令 cwd 跨越 active Workspace。
- Workspace 身份进入 trace、session、patch checkpoint 和审计数据，避免跨 Workspace 的相对路径碰撞。

### 3.3 配置目标

- 服务器设置路径不再依赖业务 Workspace。
- 配置保存是原子的，并有可验证的 schema 和迁移版本。
- Secret 不出现在状态响应、保存响应、日志、审计记录或异常详情中。
- WebUI 能清楚显示配置是否被 CLI 或环境变量覆盖。

## 4. Non-Goals

- 不在首版实现完整的第三方身份提供商、OIDC 登录或多用户账户系统。
- 不在首版实现完整 RFC 7591 Dynamic Client Registration endpoint；首版可在首次成功授权时受控地创建 Client 记录。
- 不把 WebUI 变成通用 OAuth Provider 管理平台。
- 不让每个文件工具新增 Workspace 参数并自行路由多个根目录。
- 不允许一个 MCP 工具调用同时读取多个 Workspace。
- 不在设置文件或数据库中保存原始 access token、refresh token 或 client secret。
- 不承诺 access token 永不过期；“长期免重新认证”由持久化 Grant 和 refresh token 实现。
- 不在本次工作中替换全部认证方式；现有静态 bearer token 可保持兼容。
- 不在首版实现每个 Client 一把 JWT signing key。每 Agent 独立的是 Client credential、Grant 和 token；服务器 signing key 使用可轮换 key ring。

## 5. Architecture Decisions

### 5.1 Server Settings Store

- Server Settings Store 使用固定、持久化且与业务 Workspace 解耦的配置目录。
- 显式配置目录继续拥有最高可预测性；默认目录应是稳定的用户级应用配置目录。
- 启动时只解析一次最终 settings 路径，不再因 settings 中的 Workspace 值重新定位 settings 文件。
- 设置文档增加 schema version。
- 旧的单 Workspace 设置迁移为 Workspace Catalog 中的单项。
- 写入采用同目录临时文件、flush、必要时 fsync、权限收紧和原子 replace。
- 读取到损坏 JSON 时不得静默退化为空配置；应保留原文件、报告明确错误并拒绝覆盖。
- 保存接口返回 sanitized settings；所有 secret 只返回 `configured`、来源和非敏感指纹。
- CLI、环境变量、持久化设置、默认值的优先级保持明确，并在状态中显示 effective source。

### 5.2 Secret 存储

- signing-key material、client secret、OAuth authorize password 等写入 Secret Vault 或平台受保护存储。
- 普通 settings 只保存 secret reference、active key ID 和配置状态。
- 使用现有 Secret Vault 时，部署必须提供独立 master key；不得把 master key 与密文保存在同一文件中。
- 若受保护存储未启用，默认拒绝通过 WebUI 持久化新的 OAuth secret。
- 可提供明确标注的个人本地明文兼容模式，但必须二次确认并在状态页持续显示风险。
- WebUI secret 输入框为 write-only；空白表示保持不变。

### 5.3 OAuth Authorization Store

- 使用独立 SQLite 数据库，而不是 JSON 设置文件。
- 数据库位于固定配置目录，并通过 schema migration 管理版本。
- 启用 WAL、foreign keys 和合理的 busy timeout。
- 数据库只保存 token 哈希、标识和元数据，不保存 bearer token 原文。
- 对数据库不可用、迁移失败或完整性失败采取 fail-closed 策略：已追踪 token 不得绕过状态检查。
- 高频 MCP 请求的 `last_seen_at` 更新需要节流，避免每个请求都触发磁盘写入。

### 5.4 OAuth Client

每个远程 Agent 对应一个 OAuth Client 记录，至少包含：

- 稳定 client ID
- 可编辑显示名称
- Client 类型：public PKCE 或 confidential
- 精确 redirect URI 列表
- 允许的 scopes
- 创建时间、首次授权时间、最后使用时间
- enabled、revoked 和 revoked-at 状态
- 可选的 client-secret reference
- 可选的最近 User-Agent 和来源地址摘要

首次授权的 Client 处理规则：

- client ID 必须非空且满足长度、字符集限制。
- redirect URI 必须是绝对 URI，并经过严格规范化。
- 对 localhost/native callback 制定单独规则。
- 未登记 Client 第一次请求时必须经过明确授权页面，页面展示 client ID 和 redirect URI。
- 成功授权后才创建或批准 Client 记录。
- 后续请求的 redirect URI 必须与登记值精确匹配。

### 5.5 Authorization Grant

每次用户批准一个 Agent 连接时创建或更新 Authorization Grant，至少包含：

- grant ID
- client ID
- granted scopes
- 创建和更新时间
- 最后使用时间
- enabled、revoked 和 revoked-at 状态
- 撤销原因
- 授权来源和审计关联

撤销 Grant 会立即使其所有 access token 和 refresh token 失效。

### 5.6 Access Token

- 继续使用 JWT 以降低迁移成本。
- JWT header 增加 `kid`。
- 新 token claims 至少包含：issuer、audience、issued-at、expiry、scope、client ID、grant ID、subject 和 `jti`。
- subject 使用稳定 Grant 或 Agent 身份，不使用显示名称。
- 每个签发的 access token 在数据库记录 `jti`、grant ID、client ID、签发时间、到期时间、scope、撤销状态和最后使用时间。
- 数据库不保存 JWT 原文。
- 请求校验顺序为：解析 header、选择 signing key、验证签名、验证 issuer/audience/time、读取 Grant、读取 token 状态、验证 Client 状态、验证 scope。
- 任一 Client、Grant、token 或 key 被禁用时拒绝请求。
- 管理员撤销单个 `jti` 后立即生效。

### 5.7 Refresh Token

- token endpoint 增加 Refresh Token grant。
- refresh token 使用高熵随机 opaque value。
- 数据库只保存带服务器 pepper 的 token 哈希。
- refresh token 与 Grant、Client、scope 和 token family 关联。
- 每次成功刷新后旋转 refresh token，并撤销旧 token。
- 检测已旋转 token 被再次使用时，视为可能泄漏：撤销整个 token family，并记录高优先级审计事件。
- refresh token 可以长期有效，但仍需绝对过期时间和可选 inactivity timeout。
- Client 或 Grant 撤销后，相关 refresh token 立即失效。

### 5.8 不支持 Refresh Token 的兼容模式

- 管理设置提供明确的 compatibility profile。
- 兼容模式签发较长期 access token，但仍必须包含 `jti` 并登记在数据库。
- 长期 access token 可由 WebUI 单独撤销。
- WebUI 明确标记该 Agent 没有 refresh 能力及其风险。
- 不允许生成真正无过期时间的 JWT。
- 默认优先使用 refresh-token 模式；兼容模式必须显式启用。

### 5.9 Signing Key Ring

- 服务器拥有持久化 signing-key ring，而不是每次启动生成随机 secret。
- 每个 key 包含稳定 `kid`、算法、创建时间、激活时间、退役时间、撤销状态和 secret reference。
- 同一时间只有一个 active signing key 用于签发。
- retired key 可继续验证旧 token，直到对应 token 全部过期。
- compromised key 可立即 revoked，使相关 token 全部失效。
- WebUI 只显示 key ID、状态、创建时间、指纹和预计安全删除时间，不显示 key material。
- signing-key rotation 不应要求正常 Agent 重新人工认证，只要其 refresh token family 仍有效。

### 5.10 Workspace Catalog

- 保留现有单根 Workspace 路径安全模块作为 Adapter。
- Workspace Catalog 位于其上方，负责多个具名根目录、默认项、启用状态和 session 选择。
- 每个配置项至少包含稳定 ID、显示名称、根目录、enabled 和 default 标记。
- 启动时拒绝不存在、非目录、危险根目录、重复根目录和不允许的嵌套根目录。
- Workspace ID 不直接依赖显示名称；根目录变更需被视为显式管理操作。
- relative path 永远相对于当前 session 的 active Workspace/default cwd。
- absolute path 必须位于当前 active Workspace；不得扫描其他 Workspace 自动匹配。
- 切换 active Workspace 时重置 session default cwd，并只影响后续工具调用。
- stdio 模式没有独立 HTTP session 时，使用进程级 active Workspace，并明确禁止并发切换造成上下文竞争。
- 删除或禁用 Workspace 前检查活跃 exec/session；默认阻止删除并要求先终止或迁移。

### 5.11 OS 级执行约束

- 命令 cwd 必须属于 active Workspace。
- Linux Landlock 每次只授予 active Workspace 和必要运行目录，不授予 Catalog 中所有根目录。
- patch baseline、checkpoint、exec session、trace 和文件版本键必须包含 Workspace identity。
- Windows/macOS 等缺少同等内核约束的平台继续明确报告外部沙箱风险。

## 6. Persistent Data Model

建议的逻辑表如下；实现者可以调整列名，但不得改变安全语义。

### 6.1 `oauth_clients`

- client ID，主键
- display name
- client type
- redirect URI document
- allowed scopes
- client-secret reference
- enabled/revoked 状态
- created/updated/first-authorized/last-seen timestamps
- optional metadata：User-Agent 摘要、来源地址摘要

### 6.2 `oauth_grants`

- grant ID，主键
- client ID，外键
- scopes
- enabled/revoked 状态和原因
- created/updated/last-used timestamps

### 6.3 `oauth_access_tokens`

- `jti`，主键
- grant ID 和 client ID，外键
- signing `kid`
- scopes
- issued/expires/last-used timestamps
- revoked 状态和原因
- token mode：standard 或 compatibility

### 6.4 `oauth_refresh_token_families`

- family ID，主键
- grant ID 和 client ID
- created/expires/last-used timestamps
- revoked 状态和原因

### 6.5 `oauth_refresh_tokens`

- token ID，主键
- family ID，外键
- token hash，唯一
- issued/expires/used/revoked timestamps
- replacement token ID
- reuse-detected 状态

### 6.6 `oauth_signing_keys`

- `kid`，主键
- algorithm
- secret reference
- fingerprint
- active/retired/revoked 状态
- created/activated/retired/revoked timestamps

### 6.7 `oauth_audit_events`

- event ID
- timestamp
- event type
- client/grant/token/key references
- actor kind
- redacted details document

审计事件至少覆盖：Client 首次批准、Grant 创建、token 签发、refresh、refresh reuse、单 token 撤销、Grant 撤销、Client 禁用、key 轮换和 key 紧急撤销。

## 7. OAuth Interface Contracts

### 7.1 Authorization endpoint

- 保持 Authorization Code + PKCE S256。
- 请求中的 client ID、redirect URI、scope 和 PKCE 参数必须被完整绑定到一次性 Authorization Code。
- Authorization Code 仍然短期、一次性；可保留内存实现，也可存入数据库以支持多进程部署。
- 授权页面展示 Agent 名称或 client ID、redirect URI 和请求 scopes。
- 已批准 Agent 再次授权时仍应显示足够信息，避免静默授权未知 redirect URI。

### 7.2 Token endpoint

支持：

- Authorization Code grant
- Refresh Token grant

Authorization Code 成功响应包含：

- access token
- bearer token type
- access-token expiry
- granted scope
- 在标准模式下返回 refresh token

Refresh 成功响应：

- 返回新的 access token
- 返回轮换后的 refresh token
- 保持或收窄 scope，不允许扩大原 Grant scope

### 7.3 MCP bearer 验证

- 所有 MCP 和 Admin OAuth bearer 验证共享同一验证模块。
- Admin scope 仍需显式存在，普通 MCP token 不得访问管理接口。
- 验证结果生成安全上下文，包含 client ID、grant ID、`jti`、scope 和 token mode。
- HTTP session、request trace 和 tool trace 记录这些非敏感标识。

### 7.4 Admin management endpoints

至少提供：

- Agent 列表与详情
- Agent 启用、禁用和撤销
- Grant 列表与撤销
- 某 Agent 的 active/expired/revoked token 列表
- 单 access token 撤销
- refresh-token family 撤销
- signing-key 列表、创建、激活、退役和紧急撤销
- OAuth 审计事件查询

所有写操作必须：

- 要求 Admin scope 或 admin bearer token
- 使用明确 POST/DELETE 语义
- 进行 CSRF/Origin 防护和审计
- 不在响应中包含原始 secret/token
- 对危险操作要求 WebUI 二次确认

## 8. WebUI Plan

### 8.1 导航结构

新增或拆分为：

- Workspaces
- OAuth Agents
- OAuth Tokens
- Signing Keys
- Authentication Settings
- Audit

不要继续把浏览器登录 token、MCP bearer token、Admin bearer token、OAuth authorize password 和 signing secret 混在同一组输入框中。

### 8.2 OAuth Agents 页面

列表显示：

- Agent 名称和 client ID
- client type
- scopes
- redirect URI 摘要
- 首次授权和最近访问时间
- active access-token 数
- active refresh family 数
- token mode
- enabled/revoked 状态

详情页显示：

- 完整登记 redirect URI
- Grant 列表
- token 元数据列表
- refresh family 状态
- 最近审计事件
- 可选来源/User-Agent 摘要

操作：重命名、禁用、重新启用、撤销 Grant、撤销单 token、撤销 refresh family、强制重新认证。

### 8.3 Signing Keys 页面

- 显示 key ID、状态、算法、指纹、创建/激活/退役时间。
- 支持生成新 key、激活、正常退役和紧急撤销。
- 正常轮换需要显示旧 token 最晚到期时间。
- 紧急撤销必须提示会立即中断相关 Agent。
- 任何页面都不能读取或复制 key material。

### 8.4 Workspace 页面

- 表格或卡片形式管理多个 Workspace。
- 支持添加、编辑名称、修改路径、设置默认、禁用和删除。
- 保存前显示规范化后的路径和校验结果。
- 显示使用该 Workspace 的 active MCP/exec session 数。
- 删除或禁用有活跃使用者的 Workspace 时给出阻止原因和处理入口。

### 8.5 设置编辑行为

- 前端 hydrate、serialize、validation、dirty-state 和 secret write-only 逻辑应从大型单文件脚本中抽离为可测试模块。
- 保存后显示哪些值立即生效、哪些需要重启。
- 显示 effective source：CLI、environment、persistent settings 或 default。
- 环境变量覆盖持久化值时显示不可忽略的提示。
- 保存响应只展示 sanitized 结果。

## 9. Migration and Compatibility

### 9.1 Settings 路径迁移

- 首次启动新版本时检查固定 settings 目录。
- 如果固定目录不存在而旧 Workspace 内存在 settings，则执行一次性导入。
- 导入前验证旧配置并创建备份。
- 导入后记录 migration marker，避免重复覆盖新配置。
- 明确记录原配置位置和新配置位置，但不记录 secret。

### 9.2 单 Workspace 迁移

- 旧 `workspace` 字段转换成 Catalog 单项。
- 生成稳定 Workspace ID。
- 将其设为默认 active Workspace。
- 兼容读取旧字段至少一个发布周期，但所有新写入只写新 schema。

### 9.3 旧 signing secret 迁移

- 现有 `oauth_token_secret` 导入 key ring，成为初始 active key。
- 生成稳定 `kid` 和非敏感 fingerprint。
- 导入后 secret material 进入 Secret Vault；settings 只保留 reference。
- 删除旧明文前必须确认新存储可正常读取并完成备份策略。

### 9.4 旧 JWT 兼容

旧 JWT 没有 `kid`、client ID、grant ID 或 `jti`。升级时：

- 对没有 `kid` 的 token，允许使用 legacy signing key 验证。
- 仅在有限迁移窗口内接受 legacy token，最长不超过旧 token 最大 TTL。
- legacy token 不能被单独列出或撤销，因为没有 `jti`；WebUI 应明确标记“存在不可追踪 legacy token 的迁移窗口”。
- 管理员仍可通过撤销 legacy key 立即使全部旧 token 失效。
- 迁移窗口结束后拒绝无 `kid`/`jti` token。

### 9.5 Agent refresh 能力

- 实施前记录目标远程 Agent 是否支持 Refresh Token grant。
- 支持 refresh 的 Agent 使用标准模式。
- 不支持的 Agent使用显式 compatibility profile。
- 不因某一个旧 Agent 缺少 refresh 能力而把所有 Agent 降级到长期 access token。

## 10. Detailed Tiny-Commit Plan

每个提交都必须保持现有测试可运行，并尽量只引入一个可观察行为变化。

### Commit 1：锁定当前 OAuth 行为

- 增加现状测试：固定 signing secret 和 issuer 时，重建 Runtime 后旧 token 仍能验证。
- 增加现状测试：secret、issuer、audience 或 expiry 改变时验证失败。
- 增加现状测试：token 当前不含 client ID、grant ID、`jti` 和 `kid`。
- 不修改生产行为。

### Commit 2：脱敏启动设置保存响应

- 让 status 和 save response 共用同一个 settings sanitizer。
- 对 auth token、admin token、OAuth password、signing secret 和 future secret refs 只返回 configured 状态。
- 增加 HTTP 级测试，确保响应和日志不出现输入的 secret。
- 保持现有设置文件格式不变。

### Commit 3：让 Settings Store 原子写入

- 抽出 Settings Store 模块。
- 保留当前读取接口的兼容 Adapter。
- 改用临时文件和原子 replace。
- 损坏 JSON 时返回明确错误并禁止覆盖。
- 增加写入失败、损坏文件和并发读取测试。

### Commit 4：固定最终配置目录

- 启动时确定一次 settings/config 目录。
- 删除 Workspace 改变后重新定位 settings 的行为。
- 保留 CLI 和环境变量优先级。
- 增加“WebUI 修改 Workspace 后重启仍读取同一 settings”的测试。

### Commit 5：为 settings 增加 schema version 和迁移框架

- 增加 version 字段和 migration runner。
- 迁移过程支持备份、幂等和失败回滚。
- 尚不改变 Workspace 或 OAuth schema。

### Commit 6：引入 OAuth Store 空 schema

- 创建独立 SQLite Store。
- 增加 schema migration、WAL、foreign keys、busy timeout 和 health/status。
- 建立 Client、Grant、Access Token、Refresh Family、Refresh Token、Signing Key 和 Audit 表。
- 暂不接入请求流。

### Commit 7：导入现有 signing secret 到 key ring

- 将现有 secret 作为初始 active key。
- 生成 `kid` 和 fingerprint。
- 保持 token 签发格式不变，验证仍使用同一 key material。
- 增加跨重启 key ID/fingerprint 稳定测试。

### Commit 8：新 JWT 加入 `kid`

- 新签发 token header 写入 active `kid`。
- 验证根据 `kid` 选择 key。
- 无 `kid` 的旧 token 走 legacy key fallback。
- 增加未知、retired 和 revoked key 测试。

### Commit 9：持久化 OAuth Client

- 成功授权时创建或更新 Client 记录。
- 严格校验 client ID 和 redirect URI。
- 后续请求要求 redirect URI 精确匹配。
- 增加未知 Client、redirect mismatch 和 disabled Client 测试。

### Commit 10：持久化 Authorization Grant

- 用户批准时创建 Grant。
- Authorization Code 绑定 grant ID。
- 撤销状态尚不接入 bearer 验证。
- 增加 Client 多次授权、scope 收窄/扩大和 revoked Grant 测试基线。

### Commit 11：扩展 JWT claims

- 新 token 加入 client ID、grant ID、subject 和 `jti`。
- 保留现有 issuer、audience、scope 和时间 claims。
- 将 `jti` 元数据写入 access-token 表。
- 旧 token 继续走迁移兼容路径。

### Commit 12：bearer 验证接入 OAuth Store

- 签名和标准 claims 验证成功后查询 Client、Grant 和 access-token 状态。
- disabled/revoked/expired 任一状态都拒绝。
- 生成包含 OAuth identity 的请求安全上下文。
- 增加单 token、Grant、Client 三级撤销测试。

### Commit 13：更新最近使用信息和审计

- MCP 请求记录 client ID、grant ID 和 `jti`。
- 节流更新 Agent、Grant 和 token 的 last-seen。
- 增加授权、签发、撤销的审计事件。
- 确保事件 details 自动脱敏。

### Commit 14：实现 Refresh Token grant

- Authorization Code 标准模式返回 refresh token。
- token endpoint 支持 refresh-token 兑换。
- 保存带 pepper 的 token hash。
- 验证 Client/Grant/scope/expiry。
- 增加跨服务器重启 refresh 成功测试。

### Commit 15：实现 Refresh Token 轮换与重用检测

- 每次刷新创建 replacement token 并撤销旧 token。
- 再次使用旧 token 时撤销整个 family。
- 记录 security audit event。
- 增加并发刷新和重放测试。

### Commit 16：增加长期 AT 兼容模式

- 增加 server/Client profile 配置。
- compatibility token 仍包含 `kid`、client ID、grant ID 和 `jti`。
- 默认不启用。
- 增加单 token 撤销和模式隔离测试。

### Commit 17：增加 OAuth Agent 只读管理接口

- 实现 Agent、Grant、token、key 和 audit 列表/详情。
- 所有响应只含元数据和指纹。
- 加入分页、过滤和稳定排序。
- 增加 Admin scope 和 bearer 权限测试。

### Commit 18：增加 OAuth 管理写接口

- 实现 Client enable/disable、Grant revoke、token revoke 和 refresh-family revoke。
- 实现 key 创建、激活、退役和紧急撤销。
- 每个操作记录审计事件。
- 增加幂等、冲突和危险操作测试。

### Commit 19：WebUI 增加 OAuth Agents 页面

- 增加 Agent 列表、详情、状态徽标和最近使用时间。
- 增加 Grant/token/refresh family 查看。
- 不加入写操作。
- 增加前端数据转换和空状态测试。

### Commit 20：WebUI 增加撤销和禁用操作

- 增加二次确认对话框。
- 显示操作影响范围。
- 操作后刷新 Agent 和 token 状态。
- 验证错误信息不泄露 token/secret。

### Commit 21：WebUI 增加 Signing Keys 页面

- 展示 key metadata 和 fingerprint。
- 增加正常轮换、退役和紧急撤销流程。
- 明确展示可能中断的 Agent/token 数量。

### Commit 22：引入 Workspace Catalog schema

- settings 中增加 Workspace Catalog 和默认 Workspace ID。
- 迁移旧单 Workspace 字段。
- 增加路径、重复、嵌套、危险根目录校验。
- Runtime 暂时仍使用默认 Workspace。

### Commit 23：Runtime 接入默认 Workspace Adapter

- Runtime 从 Catalog 取得默认 Workspace。
- 保持所有现有工具行为不变。
- 状态输出增加 Catalog 摘要和 active Workspace identity。

### Commit 24：HTTP session 增加 active Workspace

- session 状态保存 active Workspace ID 和 cwd。
- 新 session 使用默认 Workspace。
- 切换 Workspace 会重置该 session cwd。
- 增加两个 session 使用不同 Workspace 的隔离测试。

### Commit 25：路径与工具调用使用 session active Workspace

- 文件、Git 和 patch 路径解析取得当前 session Workspace Adapter。
- 相对路径和绝对路径都限制在 active Workspace。
- 增加跨 root 绝对路径、遍历和 symlink escape 测试。

### Commit 26：exec 与 OS 约束使用 active Workspace

- command cwd、path policy、runtime dir 和 Landlock 绑定 active Workspace。
- 不把其他 Catalog roots 暴露给当前命令。
- 增加并发 exec 和 active-root 安全测试。

### Commit 27：Workspace identity 进入版本与 checkpoint 键

- patch baseline、checkpoint、file version、trace 和 session payload 加入 Workspace identity。
- 增加两个 Workspace 中相同相对路径不碰撞测试。

### Commit 28：增加 Workspace 管理接口

- 实现 Catalog 列表、添加、编辑、设默认、禁用和删除。
- 有 active session/exec 时默认阻止危险变更。
- 增加迁移、冲突和状态一致性测试。

### Commit 29：WebUI 增加 Workspace 管理页面

- 列表式管理 Workspace。
- 保存前路径校验和影响预览。
- 展示 active session/exec 使用数。
- 增加 hydrate、serialize、validation 和 dirty-state 测试。

### Commit 30：完善 OAuth 与 Workspace 交叉审计

- 每次 MCP 请求同时记录 Agent identity 和 Workspace identity。
- WebUI Agent 详情可查看其最近使用的 Workspace，但不泄露不必要的文件路径。
- 增加审计权限和数据最小化测试。

### Commit 31：更新构建产物、契约和文档

- 更新 WebUI 构建产物。
- 更新 OAuth metadata、工具 schema、profile、安全说明、远程部署和迁移说明。
- 更新 schema drift/golden checks。
- 明确 legacy token 迁移窗口和回滚方法。

### Commit 32：完整回归与发布门禁

- 运行 OAuth、Admin、Runtime、Workspace、安全、schema 和 WebUI 测试。
- 运行完整 Python 测试套件。
- 运行 WebUI check 和 build。
- 对重启、撤销、refresh rotation、多 Workspace 并发执行进行端到端验证。
- 生成升级检查清单和已知限制。

## 11. Testing Decisions

### 11.1 测试原则

- 测试外部行为和安全不变量，不绑定内部函数拆分方式。
- 每个迁移必须有旧数据 fixture、幂等测试和失败回滚测试。
- 每个撤销操作必须验证“立即拒绝”，不能只检查数据库字段。
- Secret 测试使用明显的 canary 值，并检查 HTTP 响应、日志、状态、审计和异常中均不存在该值。
- 时间相关测试使用可控时钟，避免真实 sleep。
- 并发测试覆盖 token refresh、settings 写入和跨 session Workspace 隔离。

### 11.2 OAuth 单元测试

- key ring 选择、轮换、退役和撤销
- JWT claims、issuer、audience、expiry、scope 和 `kid`
- Client/Grant/token 状态机
- refresh-token hash、rotation 和 reuse detection
- sanitizer 和 secret fingerprint
- redirect URI 和 client ID 校验

### 11.3 OAuth 集成测试

- 完整 PKCE Authorization Code 流
- 服务器重启后旧 access token 有效
- 服务器重启后 refresh token 可续期
- 单 token 撤销立即生效
- Grant 撤销使全部关联 token 失效
- Client 禁用使全部关联 Grant 失效
- 正常 key rotation 不打断可刷新 Agent
- 紧急 key revoke 立即拒绝相关 token
- legacy token 在迁移窗口内可用、窗口后失效
- Admin scope 与 MCP scope 隔离

### 11.4 Workspace 测试

- 旧单 Workspace schema 迁移
- 默认 Workspace 选择
- 两个 session 使用不同 active Workspace
- session 切换时 cwd 重置
- 跨 root absolute path、`..` 和 symlink escape 拒绝
- patch checkpoint/version key 不碰撞
- exec cwd 和 Landlock 只开放 active root
- Workspace 禁用/删除与活跃 session 冲突
- stdio active Workspace 行为

### 11.5 WebUI 测试

- OAuth Agent 列表和详情 hydrate
- token/grant/key 状态渲染
- 撤销确认和错误提示
- Workspace Catalog hydrate/serialize/validation
- secret 空白保持不变
- save response 脱敏
- environment override 提示
- restart-required 提示
- build artifact 与 source 同步

### 11.6 安全测试

- 原始 access/refresh token 不落库
- secret 不进入任何管理响应或日志
- refresh token replay 撤销 family
- redirect URI 不允许前缀或模糊匹配
- revoked token 不因缓存继续生效
- 并发撤销和请求的顺序安全
- 数据库损坏或不可用时 fail closed
- 非 Admin token 无法调用管理写接口

## 12. Acceptance Criteria

以下条件全部满足才视为完成：

1. 固定 issuer、settings、OAuth DB 和 key store 后，服务器重启不要求已授权 Agent 重新人工认证。
2. 支持 refresh 的 Agent 可以跨 access-token 过期自动续期。
3. 不支持 refresh 的兼容 Agent 使用受追踪的长期 AT，且可由 WebUI 单独撤销。
4. WebUI 能列出每个 Agent、其 Grant、active token 数、最近使用时间和状态。
5. 撤销单 token、Grant 或 Client 后，下一次 MCP 请求立即失败。
6. signing-key 正常轮换不打断仍有有效 refresh grant 的 Agent。
7. settings/status/save/log/audit 中不出现任何原始 secret 或 bearer token。
8. 多个 Workspace 可由 WebUI 管理。
9. 不同 MCP session 可同时使用不同 active Workspace，并保持路径、exec 和 patch 隔离。
10. 旧单 Workspace 设置和旧 signing secret 可以自动迁移。
11. 旧 JWT 在明确迁移窗口内继续工作，不因升级立即全部下线。
12. 完整测试、WebUI check/build 和文档契约检查通过。

## 13. Rollout Plan

### Phase A：安全基础

- Settings Store 固定路径、原子写和脱敏。
- OAuth DB 与 signing-key migration。
- 此阶段不改变 Agent token 行为。

### Phase B：可追踪 OAuth

- Client、Grant、`jti` 和状态检查。
- Admin 只读页面先上线。
- 保留 legacy-token fallback。

### Phase C：可撤销与自动续期

- 撤销操作、refresh token、rotation 和 compatibility profile。
- 启用 WebUI 管理写操作。

### Phase D：多 Workspace

- Catalog、session active Workspace、工具/exec 隔离和 WebUI。

### Phase E：收紧兼容

- 迁移窗口结束后拒绝 legacy token。
- 根据 Agent 能力逐步减少长期 AT compatibility profile。

## 14. Rollback Strategy

- 每次数据库 schema migration 前创建可识别版本的备份。
- 新代码能够在只读模式下识别旧 settings，并给出明确降级提示。
- legacy signing key 在迁移窗口结束前不得删除。
- 回滚应用版本时，不得让旧版本覆盖新 schema；检测到未来 schema version 时应拒绝启动。
- OAuth Store 接入初期可提供验证-only shadow mode：记录本应拒绝的状态但仍由旧验证路径决定结果，用于观察误判；正式启用后不得自动降级到不检查撤销状态。
- Workspace Catalog 初期仅让 Runtime 使用默认项；session 切换功能单独启用，便于回滚。

## 15. Operational and Observability Requirements

- 启动日志记录 settings 路径、OAuth DB 路径、active key ID 和 fingerprint，但不记录 secret。
- Admin health 显示 DB schema version、migration 状态、active key ID、Client/Grant/token 数和 legacy migration 状态。
- 记录 token 验证失败类别，但不记录完整 token。
- 对 refresh reuse、key emergency revoke、异常大量失败认证输出高优先级事件。
- 为 audit 和 token 表制定保留策略，定期清理已过期且超过保留期的元数据。
- last-seen 更新节流参数可配置，并有默认值。

## 16. Risks and Mitigations

### 风险：目标 Agent 不支持 refresh token

- 缓解：按 Client 启用长期 AT compatibility profile；仍保留 `jti` 和撤销能力。

### 风险：JWT 状态查询增加请求开销

- 缓解：SQLite 索引、只读连接、短时安全缓存和节流写入；撤销时必须使缓存失效。

### 风险：旧 token 无 `jti`，无法单独撤销

- 缓解：有限迁移窗口；必要时撤销 legacy signing key，使旧 token 全部失效。

### 风险：redirect URI 管理不严导致授权码泄漏

- 缓解：精确匹配、首次明确授权、拒绝模糊匹配和未知 URI。

### 风险：Refresh Token 被盗

- 缓解：只存哈希、一次性轮换、family reuse detection、Client/Grant 撤销和审计。

### 风险：多个 Workspace 扩大命令访问面

- 缓解：每个 session 只激活一个 Workspace，命令和 Landlock 只开放 active root。

### 风险：设置迁移导致服务无法启动

- 缓解：迁移前备份、幂等迁移、明确错误、禁止静默空配置和分阶段 rollout。

## 17. Executor Checklist

开始前：

- 阅读仓库 AGENTS 指令和安全文档。
- 确认当前 Git 根目录、分支、dirty state 和已有工作树改动。
- 使用 CodeGraph 获取 OAuth、Runtime、Workspace、Admin 和 WebUI 的最新结构上下文。
- 确认目标远程 Agent 是否支持 Refresh Token grant。
- 记录当前 settings/config 路径、issuer 和 signing-secret fingerprint；不得复制 secret 原文到计划、日志或 issue。

每个提交：

- 只完成一个可描述的行为变化。
- 更新或增加对应外部行为测试。
- 运行最小相关测试，再运行受影响测试集合。
- 确认 secret canary 未出现在输出。
- 确认 WebUI source 和 build artifact 没有漂移。

提交前：

- 运行完整测试门禁。
- 检查 schema migration 的向前升级和失败回滚。
- 人工验证重启、refresh、撤销和两个 session/Workspace 的核心流程。
- 更新远程部署、备份、恢复、轮换和紧急撤销说明。

## 18. Suggested Skills for the Next Agent

- `tdd`：按红—绿—重构完成 OAuth Store、撤销和 Workspace 隔离。
- `diagnose`：若远程 Agent 仍在重启后掉线，按 secret、issuer、audience、expiry、Grant/token 状态逐层定位。
- `review`：每个阶段完成后审查安全不变量、迁移兼容和跨模块影响。
- `improve-codebase-architecture`：确保 Settings Store、OAuth Authorization Store 和 Workspace Catalog 保持深模块与清晰 seam。
- `handoff`：在跨 Agent 或跨会话继续实施时记录已完成提交、迁移状态、失败测试和剩余步骤。

## 19. Final Implementation Guidance

- 优先完成 OAuth 持久化和 Agent 管理，再完成多 Workspace；用户当前最痛的问题是重启后远程 Agent 掉线。
- 不要用“每 Client 一把 signing secret”替代 Client/Grant/token 状态模型。每 Client 独立凭据可以存在，但服务器签名密钥应由 key ring 管理。
- 不要为了 WebUI 可管理而保存原始 bearer token；`jti`、Grant 和 token hash 已足够完成识别、撤销和审计。
- 不要用永不过期 token 实现“无需重新认证”；优先使用长期 Grant、可轮换 refresh token 和短期 access token。
- 不要把多个 Workspace root 同时暴露给一次工具调用或一个 exec sandbox。
- 所有安全状态变更都必须持久化、可审计，并在服务器重启后保持一致。
