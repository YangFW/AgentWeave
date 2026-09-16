# AgentNexus 架构说明

AgentNexus（智枢）由 FastAPI 服务、浏览器界面及可选 Redis/Worker 组成。未配置 REDIS_URL 时保留进程内开发模式；配置后，API 保存任务与投递记录，Worker 负责执行。模型调用、工具权限、文件处理和持久化均在服务端完成。

## 主要组件

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| Web 工作台 | `web/` | 对话、配置管理、任务进度、文件预览与下载 |
| HTTP API | `app/main.py` | 路由、输入校验、公共响应和 SSE 事件流 |
| Agent Runtime | `app/services/agent_runtime.py` | 上下文还原、Skill 匹配、计划、工具调用和输出校验 |
| Model Gateway | `app/services/model_gateway.py` | 离线适配器及 OpenAI Chat Completions 兼容调用 |
| Skill Registry | `app/services/skill_registry.py` | Skill 包加载、安装、编辑、匹配和导出 |
| MCP Gateway | `app/services/mcp_gateway.py` | 内置工具、stdio MCP、Streamable HTTP MCP 和 HTTP 工具 |
| Task State | `app/services/task_state.py` | 运行尝试、节点、指令和检查点 |
| Expert Team Service | `app/services/expert_team_service.py` | 团队选择、成员并行执行、主管汇总和验收 |
| Context Service | `app/services/context_service.py` | 分层记忆和有效上下文 |
| Loop Scheduler | `app/services/loop_scheduler.py` | 间隔、Cron、单次和 Webhook 自动化 |
| Policy Engine | `app/services/policy_engine.py` | 在任务和工具生命周期应用拒绝、审批或上下文规则 |
| SQLite | `app/db.py` | 配置、任务、事件、记忆和文件索引的单机持久化 |
| 身份与权限 | `app/services/auth_service.py` | 密码哈希、持久会话、角色、成员关系、限流与审计 |
| Worker 与队列 | `app/worker.py`、`app/services/task_queue.py` | 事务投递、消息确认、运行租约、恢复与用户配额 |
| 工作区路径 | `app/services/workspace_path_manager.py` | 按组织/用户/项目生成隔离目录，供容器挂载 |
| 容器执行引擎 | `app/services/container_agent_runner.py`、`docker/runner.Dockerfile` | 为 Codex/Claude Code/自定义命令启动独立容器，流式日志，回收容器 |

| 实时通知 | `app/services/event_notifications.py` | Redis 传递游标通知，SSE 从 SQLite 补读正文 |

## 普通任务流程

```text
浏览器提交消息和附件
        ↓
创建 Task 与 Run
        ↓
恢复对话上下文和有效记忆
        ↓
确认当前目标与必要参数
        ↓
匹配 Skill，生成执行计划
        ↓
向模型提供本次允许的 MCP 工具
        ↓
生成回答或文件
        ↓
按计划检查格式、产物和来源一致性
        ↓
保存结果，并通过 SSE 更新页面
```

执行过程采用两层节点。上层是目标理解、准备、执行、文件生成和校验等阶段；下层记录本次实际使用的 Skill、模型、MCP Server 和工具。节点展示的是运行状态和可核验摘要，不包含模型隐藏推理。

每个任务可以有多个 Run。取消、重试、检查点恢复和服务重启恢复都会保留原有运行记录，而不是覆盖历史结果。检查点保存的是 JSON 状态及允许复用的工具结果，不是进程快照。

## 专家任务流程

专家模式不会临时拼装任意成员。运行时从当前作用域内已启用的团队中选择一个团队，或使用用户手工指定的团队。成员分别创建子任务并行执行，完成后由主管读取成员交付并生成汇总结果。

成员上下文相互独立。团队、成员和 Agent 的权限逐层收紧；失败成员可以单独重试。主管汇总完成后还要通过团队配置的结构化验收规则，父任务才会结束。

## Skill 与工具

Skill 是以 `SKILL.md` 为入口的文本流程包。内置 Skill 位于 `app/builtin_skills/`，安装包保存在数据库中。运行时读取 Skill 指令及包内可读文本作为上下文；包中的 Python、Shell 或 JavaScript 文件不会被自动执行。

MCP Gateway 按服务类型分发调用：

- `builtin`：平台维护的天气、搜索、表格和报告工具。
- `mcp_stdio`：通过官方 MCP SDK 连接服务端本地进程。
- `mcp_http`：通过官方 MCP SDK 连接远程 Streamable HTTP 服务。
- `http`：按预先声明的路径和输入 Schema 调用普通 HTTP API。

平台自身的出站请求先受 `APP_ALLOW_OUTBOUND_NETWORK` 总开关控制，再应用模型、搜索、远程 MCP、HTTP 工具、下载链接安装和远程 Policy 的细分开关与白名单。stdio 命令仍应使用白名单限制；其子进程不受 Python 应用总开关约束，需要部署层网络策略。工具在进入模型上下文前还要经过 Agent 权限和当前计划过滤，调用前后可以继续应用 Policy。

## 模型与流式输出

Model Gateway 当前实现 OpenAI Chat Completions 兼容协议。真实模型请求优先使用上游 SSE 流式接口，并把文字增量写入任务事件。如果兼容服务忽略 `stream=true` 并返回普通 Chat Completions JSON，网关会解析该响应并一次性发布文字，同时保留工具调用；这种兼容模式可以完成任务，但没有逐段实时输出。

浏览器保存最后收到的事件游标。连接中断时会有限次数退避重连，服务端从游标之后继续发送事件，避免重复展示；任务结束或进入等待确认状态后关闭连接。

内置离线适配器用于无密钥启动和流程检查，不承担通用内容生成。模型配置保存在 SQLite；环境变量模式只保存变量名，直接密钥模式保存本机加密后的密文。

## 文件存储

默认运行目录如下：

```text
data/platform.db       SQLite 数据库
data/.secret_key       本机密钥（未显式配置 APP_SECRET_KEY 时生成）
data/uploads/          用户上传文件
data/artifacts/        任务生成文件
```

公共任务和事件响应不会返回服务器绝对路径或数据库内部 JSON 字段。产物通过 ID 定位，并由受控接口预览或下载；路径解析会拒绝绝对路径、目录穿越和指向产物目录外部的符号链接。

这些目录仍然是单机共享存储。启用认证时，上传登记所有者，任务及产物接口检查用户和工作区权限。公共响应脱敏和接口权限不等于独立文件系统或企业级租户隔离；可信部署与权限验收仍是前提。

## 第三方引擎沙箱

`execution_engine=builtin` 时仍由本机 `AgentRuntime` 调用模型和 MCP。`codex` / `claude` / `container` 时：

```text
创建任务 (execution_engine)
        ↓
AgentRuntime._run_container_task
        ↓
确保 data/workspaces/{org}/{user}/{workspace}/code 存在
        ↓
docker run --rm agentnexus-runner:latest
  挂载 code -> /workspace
  挂载 state/.codex -> /home/node/.codex
        ↓
容器内 pip/npm 安装写入 /workspace（宿主机项目目录）
        ↓
任务结束 / 超时 / 取消 -> 容器销毁，文件保留
```

项目新增依赖不要装进镜像。入口脚本把 venv、user site 和 npm prefix 固定在 `/workspace`。容器默认 `--rm`，不会因为“时间久了”而堆积。

## 部署边界

现有多人模式使用单台主机、SQLite、Redis Streams 和独立 Worker。Compose 已设置非 root、只读根文件系统及进程/CPU/内存限制；用户配额和运行锁在 Redis 中维护。第三方引擎任务使用每任务容器；内置 MCP/stdio 仍在平台进程中。它不提供跨节点高可用或企业级多租户隔离。外部线程/子进程的副作用不能仅靠取消 Python 协程撤销。

任务/事件契约见 `TASK_EVENT_CONTRACT.md`，投递和恢复语义见 `QUEUE_PROTOCOL.md`，部署与备份见 `SECOND_RELEASE_RUNBOOK.md` 和 `BACKUP_RESTORE.md`。不要用单元测试通过替代逐项上线验收。

PPTX 生成默认由平台内置的 Python `python-pptx` 生成器完成，和主服务在同一 Python 运行环境中执行，不需要 Docker、Node.js 或 npm。若部署方有受支持的外部 Artifact Tool，也可以显式配置 `APP_ARTIFACT_TOOL_ENTRYPOINT` 切换到 Node.js 子进程链路；该路径不是默认依赖。

平台已有基础知识库服务：支持按作用域创建知识库、从上传文件建立文本分片索引、关键词检索，并在任务上下文中注入命中片段和来源摘要。它仍不等同于完整企业级 RAG：当前没有向量索引、混合检索、文档同步连接器、页级引用系统或可信登录主体下的强制租户隔离。附件上下文、长期记忆和对话摘要仍是独立能力。
