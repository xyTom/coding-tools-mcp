# WebUI 非开发者友好化实施计划

> 状态：待实施  
> 目标读者：实现 Agent、集成 Agent、测试 Agent、代码审查者  
> 最后更新：2026-07-28  
> 计划范围：工作区表单、全部设置界面、相关 WebUI 一致性改造  
> 实施原则：正确性先于视觉重排；JSON 继续作为内部存储格式；普通用户不需要理解 JSON、内部 ID 或配置枚举

## 1. 执行摘要

当前多工作区配置仍通过 Workspace Catalog JSON 文本框编辑。其他启动设置虽然已经使用 input、select 和 textarea，但仍直接暴露 Host、Allowed origins、profile、Shell 环境继承、Compatibility profile 等开发者术语。

本计划按以下顺序实施：

1. 修正设置保存、校验和状态模型。
2. 把 Workspace Catalog JSON 改成工作区卡片表单。
3. 按工作区、安全、连接、凭据和高级设置重组整个设置页面。
4. 把同一套非开发者语言和渐进式披露原则扩展到总览、MCP 添加、会话、OAuth 和高级 JSON 页面。
5. 完成行为测试、可访问性、响应式和安全回归。

底层 server-settings.json 和 mcp-servers.json 继续使用 JSON。改造目标是缩小用户必须理解的 Interface，而不是更换持久化格式。

## 2. 当前事实与问题

### 2.1 当前实现

- webui/src/admin.html 中的 settingsWorkspaceCatalog 是 JSON textarea。
- webui/src/admin.js 的 startupSettingsPayload 会执行 JSON.parse。
- renderStatus 会把 runtime.workspace_catalog 再 JSON.stringify 回 textarea。
- coding_tools_mcp/workspace_catalog.py 已提供目录存在、目录唯一、禁止嵌套和默认工作区启用等校验。
- coding_tools_mcp/server.py 的 save_startup_settings 会合并并原子保存启动设置。
- coding_tools_mcp/settings_store.py 保留 schema version，并对 secret 响应做脱敏。
- WebUI source 位于 webui/src，构建产物位于 coding_tools_mcp/webui_dist。

### 2.2 必须先修复的正确性问题

1. 保存失败可能仍显示成功。
   - 管理端点可能以 HTTP 200 返回 ok=false。
   - 当前保存按钮没有检查业务结果，随后仍清空 OAuth secret 并显示“设置已保存”。

2. 当前生效值与已保存待重启值混淆。
   - 保存后立即 refreshStatus。
   - 工作区、Host、Port、权限模式、工具 profile 和 Shell 环境等字段主要从 runtime 或 server 读取。
   - 新值尚未重启时，表单会重新显示旧的运行值。

3. 保存时校验不完整。
   - Workspace Catalog 会校验。
   - permission_mode、tool_profile、shell_env_inherit、Port 等值缺少统一的保存前校验。
   - 错误值可能先落盘，到下一次启动才失败。

4. 默认工作区存在两个来源。
   - 每个条目的 default 标记。
   - 顶层 default_workspace_id。
   - 两者可能漂移，UI 可能在下一次保存时意外切回另一默认项。

5. 设置页面没有独立 dirty state。
   - 当前 state.dirty 主要保护聊天编辑。
   - 设置修改后刷新、导航或状态轮询可能覆盖草稿。

6. 测试没有覆盖真实设置交互。
   - 现有 WebUI 测试主要检查 HTML、CSS、JavaScript 中是否包含某些字符串。
   - 没有覆盖 hydrate、serialize、字段错误、保存失败、待重启和未保存保护。

## 3. Goals

### 3.1 用户目标

- 添加或修改工作区不需要写 JSON。
- 用户只需要理解名称、文件夹、启用状态和默认工作区。
- 安全设置使用用户任务语言，而不是底层枚举。
- 设置页明确区分当前生效、已保存待重启和尚未保存。
- 每个错误显示在对应字段或工作区卡片旁边。
- 危险模式、兼容模式和原始 JSON 不干扰普通流程。
- 所有重要操作都有明确结果，不以原始 JSON 弹窗作为主要反馈。

### 3.2 工程目标

- 设置规则集中在一个 deep Settings Definition Module。
- 前端草稿、标准化、dirty state 和 pending restart 逻辑具有 locality。
- 后端和前端不再分别维护相互矛盾的选项和约束。
- source 与 webui_dist 始终由构建流程同步。
- 纯设置模型可以使用 Node 内置测试运行器测试。
- HTTP 管理端点具有稳定、可测试、脱敏的 Interface。

## 4. Non-Goals

- 不改变 server-settings.json 和 mcp-servers.json 的 JSON 存储格式。
- 不迁移 React、Vue 或其他前端框架。
- 不重写整个管理台视觉品牌。
- 不改变 Workspace Runtime 的会话隔离语义。
- 不自动重启服务作为第一版必需功能。
- 不通过 WebUI 显示任何已保存 secret 原文。
- 不在第一版开放任意服务器文件系统浏览。
- 不删除高级 JSON；只把它移动到渐进式高级入口。

## 5. 目标信息架构

设置页顶部显示三个状态之一：

- 所有设置已生效。
- 有未保存修改。
- 设置已保存，重启后生效。

设置页按以下顺序分组：

1. 工作区
2. 安全与工具权限
3. 连接与远程访问
4. 凭据与密钥
5. 高级设置

### 5.1 工作区

每个工作区使用卡片或可编辑列表，字段为：

- 显示名称
- 文件夹路径
- 启用开关
- 默认工作区单选
- 编辑、删除

普通模式隐藏：

- 稳定 Workspace ID
- legacy workspace 字段
- 每项 default 原始布尔值
- 原始 JSON

新增工作区时：

- 生成稳定且不重复的内部 ID。
- 默认启用。
- 如果这是第一项，自动设为默认。
- 路径失焦或点击“检查目录”时执行服务端校验。

删除或停用默认项时：

- 要求先选择新的默认项。
- 不允许保存一个没有启用默认项的目录。

### 5.2 安全与工具权限

普通模式提供四个预设：

| 用户选项 | permission_mode | tool_profile | 说明 |
| --- | --- | --- | --- |
| 仅查看 | safe | read-only | 只开放检查和读取工具 |
| 安全编辑（推荐） | safe | full | 可编辑工作区，但限制网络和高风险命令 |
| 可信本地开发 | trusted | full | 允许网络、Shell 展开和内联脚本，仍保留敏感变量过滤 |
| 隔离环境完全权限 | dangerous | full | 仅限容器或虚拟机，关闭主要命令权限门 |

高级模式才显示：

- permission_mode 原始枚举
- tool_profile 原始枚举
- compat-readonly-all

选择 dangerous 或 compat-readonly-all 时：

- 显示不可忽略的风险说明。
- dangerous 要求二次确认。
- 文案必须说明 compat-readonly-all 不是真正只读。

### 5.3 连接与远程访问

Host 改为：

- 仅本机访问，映射 127.0.0.1
- 局域网或容器访问，映射 0.0.0.0
- 自定义地址，高级选项

Port：

- 使用 number input。
- 校验整数且范围为 1 到 65535。
- 显示预计访问地址。

Allowed origins：

- 使用可增删的地址列表或 tag editor。
- 每项单独校验。
- 自动去重和规范化。
- 帮助文字解释它控制哪些浏览器来源可以访问。

OAuth：

- 仅在 OAuth 已启用或用户展开远程认证时显示。
- OAuth server URL 使用中文标签“公开访问地址”。
- OAuth compatibility 放入高级区域。
- Secret 输入保持 write-only，空白表示不修改。

### 5.4 凭据与密钥

密钥管理使用已有 mcp_secret_list、mcp_secret_set 和 mcp_secret_delete：

- 显示已保存密钥名称列表。
- 不显示密钥值。
- 每项提供更新和删除。
- 新增密钥使用对话框或独立表单。
- 删除必须确认。

运行时认证分开显示：

- MCP 访问凭据
- 管理台访问凭据
- OAuth 授权密码

每项显示：

- 已配置或未配置
- 更新操作
- 立即生效标记
- 空白不修改说明

### 5.5 高级设置

高级区域包含：

- Shell 环境继承
- compat-readonly-all
- OAuth compatibility
- 自定义 Host
- Workspace ID
- 原始 JSON 预览或专家编辑
- 配置路径和诊断信息

原始 JSON 默认折叠。普通保存流程不要求用户在表单和 JSON 之间手动同步。

## 6. 核心设置状态模型

前端必须同时维护三份状态：

1. active
   - 当前 Runtime 正在使用的值。

2. persisted
   - 已保存到 server-settings.json、下次启动会使用的值。

3. draft
   - 用户当前正在编辑、尚未保存的值。

派生状态：

- dirty：draft 与 persisted 不同。
- pending_restart：persisted 与 active 在重启字段上不同。
- validation_errors：字段路径到中文错误的映射。
- warnings：不阻止保存的风险和兼容性提示。

规则：

- 状态刷新只能更新 active 和 persisted。
- dirty 为 true 时不得覆盖 draft。
- 保存成功后 persisted 更新为返回的标准化值，draft 同步到 persisted。
- 保存失败时 draft 和 secret 输入保持不变。
- 只有保存成功才清空 write-only secret 输入。
- pending restart 必须持续显示到 Runtime 真正加载新值。

## 7. 后端 Settings Definition Module

建议新增：

- coding_tools_mcp/settings_definition.py

该 Module 负责：

- 支持字段集合。
- 类型转换。
- 选项枚举。
- 默认值。
- 标准化。
- 字段级校验。
- 跨字段校验。
- 哪些字段需要重启。
- 哪些字段是敏感值。
- active、persisted 和 pending restart 差异。

不要把中文 UI 文案全部放进后端。后端返回稳定字段名、约束、枚举、风险代码和生效方式；前端负责中文标题与解释。

### 7.1 统一校验范围

至少覆盖：

- host
- port
- workspace_catalog
- default_workspace_id
- allowed_origins
- oauth_server_url
- oauth_compatibility_mode
- permission_mode
- tool_profile
- shell_env_inherit
- OAuth secret 写入规则

跨字段校验至少覆盖：

- 默认工作区存在且已启用。
- Workspace ID 唯一。
- Workspace 路径存在、是目录、唯一且不嵌套。
- 非本机 Host 与认证配置组合符合现有安全规则。
- Port 合法。
- 枚举值受支持。
- OAuth URL 格式合法。

### 7.2 默认工作区规范化

保存时只接受一个权威来源：

- default_workspace_id 为唯一权威值。
- 每项 default 由后端根据 default_workspace_id 重新生成，或在持久化格式中删除冗余 default。
- legacy workspace 自动同步为默认工作区 root，仅用于兼容旧版本。

### 7.3 管理端点契约

建议增加专用读取端点：

- GET /api/admin/settings

返回：

- active
- persisted
- pending_restart
- pending_fields
- schema 或 options
- source 信息
- sanitized secret configured 状态

建议增加校验端点：

- POST /api/admin/settings/validate

输入：

- draft settings

成功返回：

- ok=true
- normalized
- warnings
- restart_fields

失败返回：

- ok=false
- field_errors
- form_errors
- warnings

保存端点继续使用：

- POST /api/admin/settings

保存成功返回：

- ok=true
- persisted
- pending_restart
- pending_fields
- requires_restart
- restart_command

保存失败：

- 使用明确的非 2xx 状态，或确保前端统一检查 ok。
- 返回字段级错误。
- 不包含 secret 原文。
- 不改变已保存文件。

## 8. 前端 Module 规划

当前 admin.js 体积较大，多个 Agent 同时编辑会高冲突。先建立以下内部 Seam：

- webui/src/settings-model.js
  - hydrateSettings
  - normalizeDraftForCompare
  - serializeSettings
  - computeDirty
  - computePendingRestart
  - applyValidationErrors

- webui/src/workspace-editor.js
  - 工作区卡片渲染
  - 新增、编辑、删除、启用和设默认
  - 目录校验反馈
  - 隐藏内部 ID

- webui/src/settings-page.js
  - settings 页面总协调
  - active、persisted、draft 生命周期
  - 保存、重置和重启提示
  - 分区展开状态

- webui/src/settings-copy.js
  - 中文标签、帮助文字、风险文字和枚举显示名

admin.js 只保留导航、公共请求和页面启动接线。

### 8.1 构建与静态资源

因为现有构建只复制 admin.js、admin.css 和 admin.html，拆分前端 Module 时必须同步修改：

- webui/scripts/build.mjs
- coding_tools_mcp/webui.py
- webui/src/admin.html
- tests/compliance/test_mcp_admin.py

要求：

- dist HTML 继续使用 module script。
- 所有导入的 JavaScript 都进入 coding_tools_mcp/webui_dist。
- 静态资源响应使用显式 allowlist，禁止路径穿越。
- 不直接手工编辑 webui_dist；构建生成。
- 增加 source 与 dist 同步检查。

## 9. 分阶段实施

## Phase 0：基线与工作树保护

### 目标

在其他 Agent 开始前，先明确当前未提交 WebUI 改动的所有权，避免覆盖现有登录页、OAuth secret 显示/复制等工作。

### 工作项

- 记录当前分支、HEAD 和 git status。
- 确认 webui/src 与 webui_dist 中现有未提交改动的负责人。
- 将现有改动先提交到独立基线提交，或由唯一集成 Agent 持有。
- 若使用 worktree，只放在仓库内的 .worktrees 或 .claude/worktrees。
- 所有 Agent 从同一明确提交开始。

### 验收

- 不存在来源不明的未提交 WebUI 改动。
- 每个 Agent 知道自己的文件所有权。
- 集成 Agent 拥有最终合并和构建产物生成权。

## Phase 1：设置正确性与后端契约

### 目标

先让设置能够可靠校验、保存和表达 pending restart，再调整 UI。

### 工作项

1. 新增 Settings Definition Module。
2. 把 Runtime 构造时的枚举校验提取为可复用保存前校验。
3. 集中 Workspace Catalog 规范化。
4. 增加 active、persisted 和 pending restart payload。
5. 增加 settings validate 端点。
6. 保存失败返回字段级错误。
7. 保存成功只返回 sanitized settings。
8. 修复前端保存按钮：
   - 检查 HTTP 和 ok。
   - 失败不清空 secret。
   - 失败不刷新覆盖 draft。
   - 成功后显示 pending restart。
9. 给设置页面建立独立 dirty state。

### 主要文件

- coding_tools_mcp/settings_definition.py
- coding_tools_mcp/settings_store.py
- coding_tools_mcp/workspace_catalog.py
- coding_tools_mcp/server.py
- webui/src/admin.js
- tests/compliance/test_mcp_admin.py
- tests/compliance/test_oauth_persistence.py

### 验收

- 非法 Port、权限模式、工具 profile 和 Shell 环境不能落盘。
- 非法 Workspace 显示字段级错误。
- 保存失败时输入内容仍保留。
- 保存成功后明确显示需要重启的字段。
- refreshStatus 不覆盖 dirty draft。
- 响应、日志和测试输出不包含 secret canary。

## Phase 2：前端 Module 与构建支持

### 目标

降低 admin.js 冲突，为后续并行实现建立真实 Seam。

### 工作项

1. 添加 settings-model.js。
2. 添加 settings-page.js。
3. 添加 settings-copy.js。
4. 保持视觉和行为不变地迁移现有设置逻辑。
5. 更新 build.mjs 和静态资源 allowlist。
6. 使用 node --test 添加 settings-model 纯函数测试。

### 验收

- npm --prefix webui run build 成功。
- source 和 webui_dist 同步。
- 管理台仍能登录、读取和保存原设置。
- 纯模型测试不依赖真实 DOM。
- admin.js 不再直接包含 Workspace JSON parse/stringify 业务规则。

## Phase 3：工作区卡片表单

### 目标

完全移除普通流程中的 Workspace Catalog JSON 编辑要求。

### 工作项

1. 新增 workspace-editor.js。
2. 把 runtime/persisted catalog hydrate 为卡片草稿。
3. 实现新增工作区。
4. 实现名称和路径编辑。
5. 实现启用开关和默认项单选。
6. 实现删除。
7. 隐藏 ID，保留稳定值。
8. 在保存前调用统一校验端点。
9. 将错误定位到具体卡片和字段。
10. 把 legacy Workspace 输入从普通 UI 删除。
11. 在高级区域保留只读 JSON 预览。

### 关于“选择文件夹”

第一版默认使用：

- 路径输入框。
- 当前工作区和最近路径建议。
- “检查目录”按钮。
- Windows 中提示可以从资源管理器复制文件夹路径。

不要直接使用浏览器 showDirectoryPicker 作为服务器目录选择器，因为浏览器通常不会提供服务器需要的绝对路径。

如确需服务器目录浏览，另开安全设计任务，至少满足：

- 仅管理员认证。
- 只返回目录，不返回文件。
- 严格路径规范化。
- 无路径穿越。
- 明确允许的起始根。
- 默认只在 loopback 管理台启用。
- 独立安全测试和审计。

### 验收

- 普通用户不接触 JSON 和 Workspace ID。
- 添加、编辑、删除、停用、设默认均可完成。
- 第一项自动成为默认项。
- 不能删除或停用唯一启用的默认项。
- 重复、嵌套、不存在和非目录路径均有卡片内错误。
- 保存后的 persisted 值与服务端规范化结果一致。

## Phase 4：其余设置表单友好化

### 目标

把技术枚举重组为用户任务，同时保留高级控制能力。

### 工作项

1. 实现安全预设卡片。
2. 把 dangerous 与 compat-readonly-all 放入高级设置。
3. Host 改为本机、局域网、自定义。
4. Port 使用 number input 和范围校验。
5. Allowed origins 改为列表编辑器。
6. OAuth 设置条件显示。
7. 把 Shell 环境继承移入高级区域，并提供中文解释。
8. 使用 mcp_secret_list 渲染密钥列表。
9. 把运行时认证与启动设置分开。
10. 给每个分区显示“立即生效”或“重启后生效”。
11. 增加 sticky 保存栏：
    - 保存更改
    - 放弃更改
    - 查看待重启项目
12. 保存完成后在页面内显示成功状态，不强制弹出原始 JSON 输出。

### 验收

- 默认流程不显示原始枚举值。
- 每个预设准确映射到底层配置。
- dangerous 有二次确认和持续风险标记。
- Secret 列表不显示值。
- 更新 secret 失败时输入不丢失。
- pending restart 横跨刷新持续存在。

## Phase 5：整个 WebUI 的非开发者一致性

### 目标

将设置页采用的语言、反馈和渐进式披露扩展到其他主要页面。

### 导航建议

| 当前名称 | 建议名称 |
| --- | --- |
| MCP 管理 | 工具连接 |
| 添加 MCP | 添加工具连接 |
| 聊天持久化 | 聊天记录与恢复 |
| 目录与会话 | 活动会话 |
| OAuth Agents | 已授权客户端 |
| Signing Keys | 登录签名密钥 |
| 实时使用 | 活动日志 |
| 认证与设置 | 设置 |
| 高级 JSON | 开发者工具 |

### 页面工作项

1. 总览
   - 用状态卡片展示配置路径、监听地址和健康状态。
   - 原始 JSON 移入“复制诊断信息”。
   - 给出可操作的异常说明。

2. 添加工具连接
   - 表单成为唯一主编辑面。
   - JSON 默认折叠在高级区域。
   - 移除“同步到 JSON”和“从 JSON 读取”作为普通流程。
   - args、env、headers、include_tools 和 exclude_tools 改为可增删行或 tag editor。
   - timeout_ms 显示为“超时时间（毫秒）”，并提供合理说明。

3. 工具连接列表
   - transport 显示“本地命令”或“远程 HTTP”。
   - 状态错误给出下一步操作。
   - 删除、停用和重载说明影响范围。

4. 活动会话
   - 技术 ID 保留为次要可复制信息。
   - 主要显示工作区、活动时间、来源和状态。
   - 危险终止操作二次确认。

5. 已授权客户端与登录签名密钥
   - 中文名称为主，OAuth、Grant、Key ID 等术语作为辅助说明。
   - 不隐藏安全含义。
   - 撤销和紧急密钥操作显示影响范围。

6. 开发者工具
   - 从主流程视觉降级。
   - 保留原始工具调用、JSON 和输出镜像。
   - 加入明确“适合调试和高级操作”的说明。

### 验收

- 普通导航不要求用户理解 OAuth Agent、Signing Key 或 JSON。
- 技术细节仍可在详情和高级区域找到并复制。
- 所有危险操作使用一致确认样式。
- 所有保存操作使用一致的成功、错误和忙碌反馈。

## Phase 6：测试、可访问性与发布门禁

### 后端测试

- 每个设置字段合法和非法输入。
- Workspace default 规范化。
- Workspace 路径不存在、重复和嵌套。
- active、persisted 和 pending restart 差异。
- 保存失败不写文件。
- Secret canary 不进入响应和日志。
- 非本机监听与认证组合。

### 前端纯模型测试

- hydrate active/persisted/draft。
- serialize Workspace Catalog。
- 新工作区 ID 稳定。
- 默认项唯一。
- dirty 比较忽略无意义顺序或格式差异。
- pending restart 计算。
- 字段错误映射。
- Secret 空白保持不变。
- 安全预设映射。

### WebUI 行为测试

最低覆盖：

- 设置加载。
- 添加工作区。
- 非法目录显示错误。
- 保存失败保留草稿。
- 保存成功显示待重启。
- 页面刷新后仍显示 persisted 值。
- dangerous 二次确认。
- Secret 更新失败不清空。
- 高级 JSON 不阻塞普通表单。

如果 CI 暂不引入浏览器运行时：

- 先用 Node 测试纯模型。
- 保留 Python HTTP 集成测试。
- 提供人工浏览器检查清单。
- 后续单独评估 Playwright。

### 可访问性

- 工作区卡片使用 fieldset 和 legend 或等价语义。
- 每个字段有 label。
- 字段错误通过 aria-describedby 关联。
- 保存结果使用 aria-live。
- 键盘可以完成新增、设默认、删除和保存。
- 对话框进入时聚焦，关闭后恢复触发按钮焦点。
- 不能只依赖颜色表达危险或错误。

### 响应式

验证宽度：

- 1440px 桌面。
- 1024px 小型桌面或平板横屏。
- 768px 平板。
- 390px 手机。

工作区卡片、sticky 保存栏、Origins 列表和密钥列表不得产生不可操作的横向滚动。

### 发布门禁

- Python 相关测试通过。
- WebUI Node 测试通过。
- npm --prefix webui run check 通过。
- source 与 webui_dist 无漂移。
- secret canary 扫描通过。
- 人工完成设置保存、待重启、重启后生效和失败保留草稿流程。
- git diff 中没有意外覆盖用户原有改动。

## 10. 建议的 Agent 分工

| Agent | 工作包 | 依赖 | 主要文件所有权 |
| --- | --- | --- | --- |
| 集成 Agent | Phase 0、契约冻结、最终合并和构建产物 | 无 | 全局，只处理集成 |
| Agent A：后端设置 | Phase 1 后端部分 | Phase 0 | settings_definition.py、settings_store.py、workspace_catalog.py、server.py |
| Agent B：前端基础 | Phase 2、Phase 1 前端保存状态 | Phase 0，可与 A 并行但按冻结契约开发 | settings-model.js、settings-page.js、build.mjs、webui.py |
| Agent C：工作区表单 | Phase 3 | A、B | workspace-editor.js、工作区 HTML/CSS |
| Agent D：设置友好化 | Phase 4 | A、B | settings-copy.js、安全、连接、凭据相关 Module |
| Agent E：全局 WebUI | Phase 5 | C、D 基本稳定 | 总览、MCP、会话、导航与高级工具 |
| Agent F：测试与审查 | Phase 1 起持续参与 | 各阶段产物 | 测试文件、QA 清单，不直接改生产逻辑 |

### 10.1 可以并行的部分

- Agent A 后端契约与 Agent B 前端 Module/构建准备可以并行。
- Agent C 和 Agent D 可以在 Module Seam 建立后并行。
- Agent F 可以从 Phase 1 开始同步补测试。
- 文案审查和可访问性审查可以与 Phase 4、5 并行。

### 10.2 不应并行编辑的部分

- 不让两个 Agent 同时大范围修改 admin.js。
- 不让多个 Agent 手工修改 webui_dist。
- 不让 Agent C、D 同时重排 admin.html 的同一设置 section。
- 不让多个 Agent 同时修改 server.py 的路由分发；由 Agent A 或集成 Agent 统一处理。

### 10.3 集成顺序

1. Phase 0 基线。
2. Agent A 的设置契约和校验。
3. Agent B 的前端 Module 与保存状态。
4. Agent C 的工作区表单。
5. Agent D 的其余设置。
6. Agent F 的阶段性测试门禁。
7. Agent E 的全局 WebUI 一致性。
8. 最终构建、完整回归和人工验收。

## 11. 建议的微提交顺序

1. 增加设置验证现状测试，不改生产行为。
2. 修正保存失败仍显示成功。
3. 引入 Settings Definition Module。
4. 完成所有启动设置保存前校验。
5. 统一 default_workspace_id。
6. 返回 active、persisted 和 pending restart。
7. 增加 settings validate 端点。
8. 前端建立 settings-model。
9. 前端建立独立 dirty state。
10. 构建支持多个前端 Module。
11. 工作区 JSON hydrate 为卡片草稿。
12. 新增、编辑、删除、停用和设默认。
13. 字段级 Workspace 校验。
14. 移除普通 UI 的 Workspace JSON。
15. 增加安全预设。
16. 重做 Host、Port 和 Origins。
17. 重做凭据和 secret 列表。
18. 添加 sticky 保存与待重启提示。
19. 重命名导航与全局术语。
20. 总览和 MCP 添加页移除 JSON 主流程。
21. 完成可访问性和响应式。
22. 更新 build artifact、文档和完整回归。

每个提交必须：

- 只包含一个可描述行为变化。
- 包含对应测试。
- 不手工编辑构建产物。
- 运行最小相关测试。
- 记录尚未解决的风险。

## 12. 关键验收场景

### 场景 A：首次添加第二个工作区

1. 用户点击“添加工作区”。
2. 输入名称和文件夹路径。
3. 页面检查目录有效。
4. 用户选择是否设为默认。
5. 保存成功。
6. 页面显示“已保存，重启后生效”。
7. 刷新页面仍显示新工作区，不回退到当前 Runtime Catalog。
8. 重启后 pending 提示消失。

### 场景 B：目录无效

1. 用户输入不存在目录。
2. 错误显示在该工作区路径字段下。
3. 保存被阻止。
4. 其他工作区草稿不丢失。
5. 不写 server-settings.json。

### 场景 C：安全模式

1. 用户选择“安全编辑”。
2. 页面显示它允许和限制的能力。
3. 保存映射到 safe + full。
4. 用户切换 dangerous。
5. 页面要求二次确认并持续显示隔离环境警告。

### 场景 D：保存失败

1. 后端返回业务错误。
2. 页面显示字段或表单错误。
3. draft 保留。
4. secret 输入保留。
5. 不显示“已保存”。
6. 不执行覆盖 draft 的自动刷新。

### 场景 E：环境或 CLI 覆盖

1. persisted 设置与 active 值不同。
2. 页面显示哪个值当前生效。
3. 页面说明可能被 CLI、环境变量或重启状态覆盖。
4. 用户不会误以为保存无效或设置丢失。

## 13. 风险与缓解

### 风险：多个 Agent 冲突覆盖

- 先拆 Module，再并行。
- 指定文件所有权。
- 构建产物只由集成 Agent生成。

### 风险：界面改了但后端仍允许坏配置

- Phase 1 先集中后端校验。
- 前端校验只用于即时反馈，后端始终是最终权威。

### 风险：保存后连接中断

- 第一版不自动重启。
- Host 或 Port 改动只显示重启命令和影响。
- 重启前提示管理台地址可能变化。

### 风险：目录选择扩大文件系统暴露

- 第一版不增加任意目录浏览端点。
- 若后续实现，单独做安全设计与测试。

### 风险：安全预设隐藏重要细节

- 普通模式显示能力摘要。
- 高级模式保留原始枚举。
- dangerous 和兼容模式永远显示风险。

### 风险：active 与 persisted 比较误报

- 比较前统一标准化类型、路径、列表顺序和默认值。
- Secret 只比较 configured 状态，不比较原文。

### 风险：source 与 build artifact 漂移

- 构建生成 dist。
- CI 检查构建前后 git diff。

## 14. 交付物清单

- Settings Definition Module。
- settings 读取、校验和保存端点。
- active、persisted、draft 和 pending restart 状态模型。
- 工作区卡片表单。
- 安全预设。
- 连接与远程访问表单。
- 密钥名称列表和 write-only 更新流程。
- 高级设置区。
- 全局 WebUI 术语和 JSON 渐进披露。
- 后端测试。
- 前端纯模型测试。
- WebUI 行为检查清单或浏览器测试。
- 更新后的 WebUI 构建产物。
- 用户文档与迁移说明。

## 15. 每个 Agent 的交接模板

每个 Agent 完成工作后必须报告：

- 完成的 Phase 和工作项。
- 修改的文件。
- 新增或改变的 Interface。
- 运行的测试及实际结果。
- 未运行的测试和原因。
- 仍存在的风险。
- 是否修改了 webui_dist。
- 是否发现并保留了进入任务前的未提交改动。
- 推荐下一位 Agent 从哪个提交继续。

## 16. 最终完成定义

只有以下条件全部满足，才视为完成：

1. 普通设置流程不要求编辑 Workspace JSON。
2. 工作区可以通过表单完整管理。
3. 所有启动设置保存前经过统一后端校验。
4. 页面可靠区分 active、persisted 和 draft。
5. 保存失败不会显示成功或清空用户输入。
6. 保存后刷新不会丢失待重启设置。
7. 默认工作区只有一个权威来源。
8. 安全模式使用非开发者语言，并保留高级控制。
9. Secret 永不回显，失败时不丢失新输入。
10. 原始 JSON 只存在于高级或诊断入口。
11. 总览、MCP 添加、会话和 OAuth 页面使用一致的任务语言。
12. 相关自动测试、构建检查和人工验收全部通过。
