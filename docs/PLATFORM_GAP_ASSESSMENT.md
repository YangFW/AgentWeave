# AgentNexus 平台定位、能力评估与演进建议

更新日期：2026-08-24
评估对象：当前工作树 `/Users/yangfw/Documents/AiCoding/AgentNexus`

## 结论

AgentNexus 已经不是单一聊天页面，而是一个**单机、受控内网场景下可运行的 Agent 平台原型**。它已经形成“模型 → Agent/专家团 → Skill/MCP → 任务状态/审批/Policy → 记忆、知识与产物”的完整控制面，并覆盖了相当多的可靠性细节。

它当前还不能直接作为面向不可信用户的组织级平台上线。决定性差距不在继续增加聊天、Skill 或 MCP 页面，而在以下四项：

1. 可信身份、组织成员关系、RBAC 与资源强制隔离；
2. 租户级凭据治理（OAuth、Secret Manager/KMS、授权、轮换和撤销）；
3. 真实工作区与隔离执行环境（容器/微虚拟机、资源配额、网络策略）；
4. 面向多实例的任务调度、队列、租约、观测与成本治理。

建议把产品定位明确为：**“AgentNexus 是可扩展的 Agent 控制面；第一阶段面向受控内网单用户或可信小团队，第二阶段建设多租户执行与治理面。”**

## 目标平台与当前实现

目标平台是让不同用户经统一登录进入组织和工作区，使用被授权的 Role、Skill、MCP 和知识，安全地提交长期任务，并获得可审计的结果。

```text
目标：身份 / 组织 / RBAC
                 ↓
      任务、队列、审批、审计、配额、工作区
                 ↓
      Role、Skill、MCP、知识、凭据与策略治理
                 ↓
      Agent、专家团、工具和受隔离的执行节点
                 ↓
      任务状态、记忆、向量、业务数据与产物
```

AgentNexus 已覆盖中间的“能力治理”和大部分“Agent 控制面”，但最上层的可信身份、最下层的受隔离执行与生产级数据面仍未完成。

## 当前已具备的能力

| 能力域 | 状态 | 代码与文档证据 |
| --- | --- | --- |
| 单 Agent 任务 | 已覆盖 | `agent_runtime.py`：目标解析、Skill/MCP 匹配、计划、工具调用、输出校验；任务事件通过 SSE 输出。 |
| 专家团 | 已覆盖 | `expert_team_service.py`：成员并行子任务、主管汇总、结构化验收与失败阻断。 |
| 任务可靠性 | 已覆盖（单机） | `task_state.py`：Task、Run、Node、Checkpoint、Command、重试、恢复、取消与运行中指令。 |
| Skill 生命周期 | 部分覆盖 | 支持创建、编辑、启停、`SKILL.md`/ZIP/HTTPS/受控本地路径安装、导入导出和推荐确认；包内脚本不自动执行。 |
| MCP/HTTP 工具 | 部分覆盖 | 支持 builtin、stdio MCP、Streamable HTTP MCP 与 HTTP 工具；有白名单、脱敏、只读直调门禁、Policy 与人工审批链。 |
| Policy 与审批 | 已覆盖（单机） | 工具前后、目标、计划、产物和输出等生命周期均可评估；决策和审批记录在任务事件中。 |
| 记忆、摘要与知识库 | 部分覆盖 | `context_service.py`、`knowledge_base_service.py`：按组织/工作区/用户/Agent/会话作用域建模；知识库目前为文本分片与关键词检索。 |
| 文件与产物 | 已覆盖（基础） | 支持常见文本、Office/PDF 有界正文读取，及 DOCX、PDF、XLSX、PPTX、Markdown、HTML 产物生成和受控下载。 |
| 自动化 | 部分覆盖 | `loop_scheduler.py`：interval、Cron、一次性与签名 Webhook 触发，含重试、幂等、非重入和站内通知。 |
| 工作区数据模型 | 仅逻辑隔离 | 表和服务已存 `organization_id`、`workspace_id`、`user_id`；当前 API 由调用方自行传入，不能作为授权边界。 |

## 与“组织级 Agent 平台”的关键差距

### P0：阻止对外/多用户上线的缺口

| 缺口 | 当前状况 | 必须补齐的结果 |
| --- | --- | --- |
| 登录与身份 | 没有可信登录主体；大量 API 将 `organization_id`、`workspace_id`、`user_id` 作为查询参数或请求体字段。 | OIDC/SAML/企业 SSO；服务端 session/JWT；请求范围只能从可信主体推导。 |
| RBAC 与所有权 | 数据中存在作用域字段，但没有成员、角色、授权关系和全路径权限校验。 | Organization、Membership、Role、Permission、Resource ownership；每个读写接口和 SSE/下载都必须授权。 |
| 强制租户隔离 | Skill、MCP、模型、上传、产物等存在全局路径；SQLite 是共享单机存储。 | 所有资源带 tenant/workspace owner；仓储层强制 scope；生产数据库使用 PostgreSQL，并做租户隔离测试。 |
| 执行沙箱 | stdio MCP 在平台服务主机启动；没有文件、网络、CPU、内存、进程隔离。 | 每任务或每工作区隔离 Worker（优先容器/微虚拟机）；最小挂载、网络 allowlist、限额、清理与审计。 |
| 真实工作区 | 当前 workspace 是逻辑标识，非用户文件根，也不是 Git worktree。 | 授权工作区根、路径穿越/符号链接防护、Git snapshot/diff/回滚和生命周期管理。 |
| 秘密与 OAuth | 模型 Key 可本机加密；MCP 配置可用环境变量引用，但没有租户 Secret Manager、OAuth flow、scope、刷新、撤销与轮换。 | KMS/Vault 或云密钥服务；租户/连接器级凭据；OAuth callback、Token 轮换和撤销；日志全链路脱敏。 |

### P1：成为可靠平台的缺口

| 缺口 | 建议 |
| --- | --- |
| 分布式任务执行 | 从 SQLite 单进程调度迁至 PostgreSQL 的原子领取/租约，或接入队列；Worker 无状态化。 |
| 运行治理 | Ask / Plan / Execute 状态机；所有有副作用操作必须有计划确认、最小权限和可撤销审批。 |
| MCP 生命周期 | OAuth、租户授权、健康检查、重连、Schema 缓存、版本固定、超时/限流、可观测性。 |
| Role 与平台资产 | 现有 Agent、Skill、MCP、专家团、Policy、自动化仍分别管理；需形成带 manifest、依赖、权限预览、版本、签名、升级和回滚的“解决方案包”。 |
| 成本与观测 | Trace ID、模型/工具延迟、Token 与费用、预算硬限制、错误告警、评测集、审计导出。 |
| 知识层 | 向量/混合检索、来源页码或块级引用、同步连接器、数据保留与删除策略。 |

### P2：提高组织协作价值的能力

- 团队频道、任务指派、交接和评论；
- 企微、飞书、钉钉、Slack 等受信消息入口；
- Skill/MCP/Role 市场、审核流、发布者信任和组织级复用；
- 用户本地执行节点：设备注册、短期凭据、双向工作区授权、租约、心跳和离线恢复；
- 浏览器/Computer Use：只能在隔离浏览器、下载治理、域名策略与人工确认后引入。

## 推荐实施顺序

不要先做更多 Agent 花样。建议按安全边界自上而下建设。

### 阶段 A：可信控制面

1. 接入 OIDC/企业 SSO，建立 Organization、Membership、Role 与 Permission；
2. 设计 `RequestPrincipal`，移除所有由浏览器直接决定的 user/workspace scope；
3. 为任务、附件、产物、Skill、MCP、模型、知识库、记忆和自动化增加统一的所有权校验；
4. 将密钥存储替换为 Secret Manager 抽象，并为 MCP 设计 OAuth/授权生命周期；
5. 写跨租户 API、SSE、产物下载和连接器凭据的安全回归测试。

**阶段验收：** Alice 无法通过修改任一 HTTP 参数、任务 ID、SSE URL 或产物下载链接读取 Bob 的资源；管理员可授权/撤销团队 Skill 和 MCP。

### 阶段 B：安全执行面

1. 引入 Runner/Worker Dispatcher，任务只提交声明式执行计划；
2. 在容器或微虚拟机中运行命令、stdio MCP、文件工具和未来的浏览器；
3. 实现授权工作区挂载、只读/可写模式、网络策略、CPU/内存/时限和回收；
4. 对工具副作用使用 idempotency key、租约与 effect journal；
5. 将 Plan → Approval → Execute 变成写操作不可绕过的状态机。

**阶段验收：** 一个用户的工具无法看到其他用户文件/密钥；超时和失败的任务会回收资源；恢复不会重复执行不可幂等操作。

### 阶段 C：规模与组织资产

1. 将控制面迁至 PostgreSQL，任务以队列或原子租约交给多个 Worker；
2. 将 Role、Skill、MCP、Agent、专家团、Policy 组合成可签名、可版本化的方案包；
3. 增加组织级用量、成本、审计与告警；
4. 扩展知识连接器、消息渠道和用户本地执行节点。

## 可直接借鉴的开源产品

- **Open WebUI**：优先借鉴 SSO/SCIM、RBAC、群组、频道、模型/Agent 与 MCP 治理的产品边界；
- **Coze Studio**：借鉴 Agent、工作流、插件、知识和资源发布的低代码资产模型；
- **LibreChat**：借鉴多用户入口、MCP、Skills 和 Agent 的自托管实现；
- **OpenHands / DeepSeek Harness**：借鉴受控执行、长任务、工作区、Skill、MCP 与任务运行时；
- **TiDB/PostgreSQL**：作为任务、审计、记忆和业务数据的生产数据层；TiDB 不替代 Agent 编排和身份治理。

## 验证记录

- 已检查当前工作树的 README、架构与平台文档，以及 `app/main.py`、数据库 schema、核心服务和测试目录。
- 当前工作树存在用户已有的未提交修改；本次只新增本报告，未修改现有实现。
- 已执行前端脚本语法检查和 `python3 -m compileall -q app`，二者通过。
- 已尝试执行完整 unittest 回归；当前 shell 未安装项目依赖，因缺少 `fastapi`、`pydantic`、`httpx`、`jsonschema`、`docx` 等包，71 个已发现测试中 60 个导入失败。这不是功能测试失败结论；在按 `requirements.txt` 创建虚拟环境并安装依赖后，需要重新运行完整回归。

## 最终判断

AgentNexus 的“平台中层”已经较扎实：有任务状态、事件、专家团、Skill、MCP、Policy、审批、记忆、知识、产物和自动化；这使它比从零搭建更适合继续演进。

距离用户可登录并安全共用的组织级平台，仍差一个完整的“可信控制面 + 隔离执行面”。这个差距是架构级而非页面级工作，建议以 P0 阶段作为下一轮项目主线。
