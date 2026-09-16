# 任务与事件契约 v1

`GET /api/tasks`、任务详情和任务提交响应中的公共任务对象使用 `schema_version: 1`。HTTP 事件查询及 SSE `task_event` 中的公共事件对象也使用 `schema_version: 1`。新增字段不改变现有 `status`、游标或事件类型。

## 任务状态

| 现有状态值 | 含义 |
| --- | --- |
| queued | 已持久化，等待执行或恢复 |
| running | 当前运行正在执行 |
| waiting_approval | 等待审批决定 |
| paused | 暂停的运行状态 |
| completed | 已成功结束，对应计划中的 succeeded |
| failed | 失败结束 |
| cancelled | 取消结束 |

保留 `completed` 是为了兼容现有浏览器和调用方，不直接将其替换为 `succeeded`。重试创建新的 Run，旧 Run 保留终态；新 Run 从 queued 开始，metadata.trigger 表示 retry/resume，不通过覆盖旧历史表示 retrying。

## 公共字段

- Task：`id`、`status`、`agent_id`、`executor_type`、`executor_id`、`workspace`、`organization_id`、`user_id`、时间戳、公开结果、附件及产物引用。
- 附件：`id`、`name`、`content_type`、`size`、`created_at` 和 `context_status`。不公开服务端路径。
- 事件：`id`、`task_id`、`ts`、`type`、`title`、`content`、按事件类型筛选后的 `data` 和 `schema_version`。未知内部字段不直接返回。

## SSE 游标

事件 ID 是数据库自增游标。重连时传 `cursor`、`after_id` 或 `Last-Event-ID`，服务端仅返回其后的公共事件。Redis 通知只包含最新游标，正文和历史以数据库为准。任务终态或等待审批时会结束当前连接。

队列协议是内部协议，见 `QUEUE_PROTOCOL.md`；不应把内部消息体当作公共任务响应。
