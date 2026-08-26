# Tool Effect Journal 设计与接线说明

## 目标与边界

当前安全检查点在真实工具调用前写入，而工具结果在调用返回后才进入缓存。如果进程在远端系统已经提交操作、但本地结果尚未落盘时退出，恢复 Run 会再次调用工具。对于发送消息、写远端系统或生成 Artifact，这会产生重复副作用。

`ToolEffectJournal` 为一个逻辑工具操作建立跨 Run 的持久记录，提供：

- 稳定的 `effect_key` 和 `Idempotency-Key`；
- 带租约令牌的 CAS 执行权；
- 已成功结果跨 Run 复用；
- 重启后把未决执行标记为 `unknown`；
- 只读/明确幂等操作可安全重试，非幂等操作必须人工对账；
- 每次状态变化与主状态同行事务提交的审计轨迹。

它不能让不支持幂等的远端系统凭空获得 exactly-once 语义。对任意非幂等写操作，进程丢失后的正确默认行为是停止并核对，而不是盲目重试。

实现位于 `app/services/tool_effect_journal.py`，独立测试位于 `tests/test_tool_effect_journal.py`。

## 状态机

```text
 prepared ──claim──> executing ──已确认成功──> succeeded
    ▲                    │                         │
    │                    │                         └── 后续 Run 直接复用
    │                    │ 重启、租约失效、网络结果不确定
    │                    ▼
    │                 unknown ──人工确认成功──────> succeeded
    │                    │
    │                    │ 只读/幂等自动重试，或人工允许重试
    │                    ▼
    └──────────────── prepared
    ▲
    └── executing 在本地派发前失败，且可证明 gateway 未调用
```

规则：

1. `prepared` 只表示调用意图已持久化，外部副作用尚未被确认。
2. `executing` 必须持有 `lease_token`；心跳、成功和未知结果写入都以该 Token 为栅栏。
3. 平台单进程启动恢复时，将遗留的 `executing` 统一转为 `unknown`。未来多 Worker 部署只能回收已确认死亡 Worker 或过期租约，不能把其他存活 Worker 的执行权清掉。
4. 已取得租约、但仍在本地预算检查/检查点阶段失败时，以同一租约执行 `executing → prepared`；该路径必须能证明 gateway 尚未派发。
5. `unknown + read/idempotent_write` 可转回 `prepared` 后重试。
6. `unknown + non_idempotent_write/artifact_write` 返回 `reconcile`，必须由用户或管理员确认“已成功”或“允许重试”。
7. `succeeded` 是终态；任何新 Run 遇到它都复用 `result_json` 和 Artifact 引用，不再调用外部工具。

`tool_effects.attempt_count` 统计的是成功取得 fenced lease 的 claim 次数，不是 gateway 的真实调用次数。派发前本地失败会保留这次 claim 审计，但真实调用计数仍为 0；运行权限中的 `tool_calls_used` 才表示真实派发预算。

## 稳定身份

稳定键使用以下字段生成，不包含 `run_id`：

```text
task_id
GoalSpec spec_hash
persisted operation_key
server_id + tool_name
Policy 修改后的最终 arguments
```

`operation_key` 必须来自已经持久化的计划调用槽，例如：

```text
<plan_id>:<node_key>:<tool_binding_id>:<call_ordinal>
```

不能只使用“工具名 + 参数”。同一任务可能有意以相同参数调用同一工具两次，这两次必须拥有不同的 `operation_key`。恢复 Run 则必须复用原计划槽，因而得到同一个 `effect_key`。

## 数据库迁移请求

正式接线时，在 `app/db.py` 新增一个未使用的迁移版本，并执行 `TOOL_EFFECT_JOURNAL_SCHEMA_SQL`，创建：

- `tool_effects`：当前状态、稳定身份、租约、结果、Artifact 和外部引用；
- `tool_effect_transitions`：append-only 状态变化记录。

Artifact 还需要与副作用记录建立唯一关系：

```sql
ALTER TABLE artifacts ADD COLUMN tool_effect_id TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_tool_effect
ON artifacts(tool_effect_id)
WHERE TRIM(tool_effect_id) <> '';
```

`tool_effect_id` 存储 Journal 的 `effect_key`；这是 Artifact 侧的外键语义命名，不再另建第二个 `effect_key` 列。

迁移函数应使用项目现有 `_ensure_column()`，不要直接无条件执行 `ALTER TABLE`。如果未来允许一个工具调用生成多个文件，则改为 `tool_effect_artifacts(effect_key, artifact_id)` 关联表，并对 `artifact_id` 建唯一约束。

## 运行时接线点

### 1. AgentRuntime 最后的真实调用边界

在 `_tool()` 中完成以下步骤之后再创建 Effect：

1. GoalSpec、计划、Schema 和 Agent 权限检查；
2. `tool.before` Policy 对参数的最终修改；
3. 最后一次取消和 steering 检查。

在调用 `mcp_gateway.invoke_tool()` 之前：

1. 根据持久化计划槽生成 `operation_key`；
2. `prepare_effect(...)`；
3. `acquire_effect(...)`；
4. 按 Decision 处理：
   - `dispatch`：携带租约 Token 调用工具；
   - `reuse`：物化缓存结果，不再调用工具；
   - `wait`：暂停/重新排队，等待当前执行者；
   - `reconcile`：Run 进入人工对账等待状态。

真实调用成功后，应先将 Journal 标记为 `succeeded`，再创建普通安全检查点。这样即使进程在两者之间退出，新 Run 也能从 Journal 复用结果。

超时、断网和连接中断不能证明远端没有提交，应调用 `mark_unknown()`。若租约已经取得，但剩余时限、调用次数或调用前检查点在 gateway coroutine 创建前失败，应以租约调用 `release_before_dispatch()` 回到 `prepared`，不得制造需要人工对账的 `unknown`。

### 2. HTTP 与 MCP

- 普通 HTTP 工具：请求 Header 强制加入 Journal 的 `Idempotency-Key`，不能沿用每个 Run 随机生成的值。
- Streamable HTTP MCP：传输 Header 同样加入 `Idempotency-Key`，作为服务端可能支持的 best-effort 信号。
- MCP 工具参数：只有输入 Schema 明确声明 `idempotency_key`、`idempotencyKey` 或管理员配置的等价字段时才注入；不得向 `additionalProperties=false` 的未知 Schema 硬塞字段。
- stdio MCP：没有通用传输 Header，只能使用上述 Schema 字段；未声明时不能假设远端具备幂等能力。
- 只有可信工具定义中的 `readOnlyHint=true` 或 `idempotentHint=true` 可以自动重试；缺少标记时一律按非幂等处理。

### 3. Artifact 原子登记

文档工具需要进一步收口：

1. 文件先写入 Effect 专属的确定性 staging 目录；
2. Artifact 行携带 `effect_key`；
3. Artifact 登记和 Journal `succeeded` 在同一个 SQLite 事务中提交，可通过 `journal.using_connection(conn)` 参加外层事务；
4. 发布前继续走现有 Verification/Artifact publication fence；
5. 若启动时发现 `unknown` Effect 已有关联 Artifact，核验文件大小和 SHA-256 后可自动对账为 `succeeded`；没有关联记录则保持人工对账，不能重新生成第二份文件。

文件系统本身不受 SQLite 事务控制。建议使用“确定性 staging 路径 + fsync + 原子 rename + 唯一 effect_key”的组合；失败清理只能删除尚未登记且确认不再被执行者使用的 staging 文件。

### 4. 启动恢复与用户交互

- 单进程版本在恢复普通 Run 之前调用 `recover_interrupted_executions()`。
- 非幂等 `unknown` 不应把整个服务启动失败，而应让对应 Task/Run 进入 `waiting_approval` 或新的 `waiting_reconciliation` 状态。
- 页面展示工具、参数脱敏摘要、首次 Run、未知原因、已有外部引用，并提供：
  - “已在外部系统确认成功”；
  - “确认尚未执行，允许重试”；
  - “继续等待核对”。
- 对账决定必须记录操作者、时间、说明和原 Effect/GoalSpec Hash。当前独立服务保存说明；可信 actor 应由后续认证/RBAC 层注入。

## 故障注入验收

现有独立测试覆盖两个恢复闭环：

1. **幂等 HTTP 写操作**：远端提交后、本地 `mark_succeeded()` 前模拟进程丢失；重启后使用同一个 `Idempotency-Key` 重试，远端副作用计数仍为 1，随后所有 Run 复用成功结果。
2. **文档 Artifact**：文件生成后、本地成功登记前模拟进程丢失；重启后 Effect 为 `unknown` 并返回人工对账，不再次调用生成器；确认已有文件后，后续 Run 复用同一 Artifact，文件和调用计数均为 1。

此外还覆盖活动租约等待、CAS 成功栅栏、MCP Schema 安全透传、不同 GoalSpec Hash 的 Effect 隔离、非幂等未知效果拒绝后的重启，以及“主状态更新成功但 transition 写入失败”时整个事务回滚。剩余时限、调用次数和调用前检查点的本地失败还必须证明 gateway 调用数为 0、Effect 回到 `prepared`、启动恢复不会生成 `unknown`。

主线接入后还需把这两个场景升级为 `AgentRuntime + MCPGateway + TaskState + Artifact` 的真实 E2E，故障点分别放在 gateway 返回之后、Journal/Artifact 同事务提交之前。
