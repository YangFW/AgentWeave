# 队列协议

Redis 队列采用 Streams 消费组，键为 `${APP_TASK_QUEUE}:stream:v1`，组名 `agent-workers`。消息包含 `version=1`、`task_id`、`run_id`，以及可选 `attempt`。消费者使用 `XREADGROUP` 获取消息，使用 `XAUTOCLAIM` 领取闲置未确认消息，执行结果持久化后 `XACK` 并 `XDEL`。超过重试上限的消息留在 `:dead` 流供排查，不静默丢弃。

当前发布器统一补充 `attempt=1`、`priority=50` 和 `workspace_id`；正式 outbox 投递的工作区来自数据库资源记录。当前任务均使用默认优先级，Streams 按投递顺序消费，没有开放多优先级调度。

曾使用 Redis List 的开发原型不会自动迁移或删除旧队列。从该原型升级时须停机核对其数据库和消息；当前 outbox 恢复仅针对本协议的持久记录，不能据此宣称任意旧原型队列均可自动迁移。

普通运行锁按 run_id 建立，默认 60 秒有效，按租约时长的三分之一续租。`APP_WORKER_LEASE_MS` 可用于测试调整（1000～600000 毫秒），生产建议保留默认值。续租及释放必须匹配所有者随机令牌。租约丢失会取消运行协程；这不保证已经启动的线程、子进程或外部请求立即终止，仍需结合任务状态和工具副作用日志实现执行隔离。

普通任务、重试和恢复在创建 Run 的同一 SQLite 事务内保存 `dispatch_outbox`。API 后台每两秒尝试投递未完成记录，Redis Lua 按 run_id 原子去重，投递成功后更新数据库标记。进程在 Redis 接收后退出时，下一轮重投不会创建第二条消息。去重标记目前保留在 Redis，后续需要结合运行保留策略清理。

API 启动恢复按持久化 `dispatch_backend=redis` 排除 Worker 运行和对应工具执行记录。Worker 重领运行并取得锁后，仅将该旧运行中的未确定工具调用转为 unknown，再调用原子检查点恢复流程。新恢复 Run 与 outbox 一起提交，重复恢复复用同一子运行。真实进程测试已覆盖原 Worker 终止后的恢复，断言两个 Run、一个最终回答；各版本证据见实施记录。

API 的事件 relay 读取数据库新事件并发布每个任务的最新游标；SSE 订阅对应 Redis channel 后按数据库游标读取正文，每次等待超时也补读数据库。通知丢失不会删除历史，重复通知不会重复返回已消费事件。

审批命令与 `approval_dispatch` 在同一事务提交，投递键为审批命令 ID，Worker 读取数据库中的决定后续跑。不同审批不复用初始运行的去重标记。Policy 等待若已失去原消费者，由取得运行锁的 Worker 提交审批证明后创建恢复尝试，证明随恢复元数据保留。API 重启不会消费 Worker 的待审批记录。真实进程测试已覆盖 output.before 等待时终止所属 Worker、审批后由其他 Worker 完成并保留证明。

自动化触发通过 `automation_dispatch` 投递，单个自动化最多存在一条 queued/running 记录。Worker 按自动化 ID 加锁后运行完整轮次，包括重试、计数和通知。检测到已运行过但未完成的投递时暂停并标记中断，避免盲目重放外部副作用。真实进程测试覆盖单轮执行及等待 Skill 审批时重建全部 API/Worker，审批续跑后轮次、触发事件与通知同步且不重复。任务结束后由数据库终态同步父自动化，不依赖内存协程。

Worker 对 Redis 连接故障和运行锁丢失执行退避重连，其他编程错误不静默重试。API 检查已投递但尚未开始的工作：queued 的普通/团队 Run、Run 仍在 waiting_approval 且命令仍为 queued 的审批，以及 queued 的自动化和成员重试投递。Redis 去重标记丢失时重新投递，使用 dispatch_id（没有时使用 run_id）原子去重。该重建不接管 running 执行，也不重放已结束的记录。

普通运行因 Worker 丢失触发的自动恢复以 `worker_recovery_count` 持久计数，达到 `APP_TASK_MAX_RETRIES` 后提交失败，不无限创建恢复 Run。用户主动重试创建新的恢复计数链；模型调用的任务累计预算仍独立生效。

用户并发配额使用带到期时间的 Redis sorted set。普通运行及审批的配额租约跟随 `APP_WORKER_LEASE_MS`，自动化和成员重试跟随其固定 60 秒运行锁；领取时原子清除已过期占用。短租约续期不得缩短整个用户配额键的保留时间，避免提前丢失该用户其他长任务的有效占用。进程退出未释放的配额在对应租约到期后可回收。

普通任务创建已将 Task、Run 和 outbox 合入同一 SQLite 写事务，任一步插入失败均回滚，不再留下部分提交。

专家单成员重试已有持久投递、Worker 消费和超时收敛测试，排队取消已有消费测试。Redis 全量丢失时，未开始消息的补投覆盖上述各类投递；活动执行的恢复不能由补投测试证明，仍需分别核对租约、检查点及外部副作用边界。

## 验证

使用独立临时 Redis，禁止指定业务 Redis。测试仅修改随机命名的测试键：

```bash
APP_TEST_REDIS_URL=redis://127.0.0.1:测试端口/0 .venv/bin/python -m unittest tests.test_task_queue -v
```

实际验证覆盖消息未确认后的重新领取、确认后的队列清理、死信保留，以及旧持有者无法释放或续期新锁。Worker 进程故障验收由 `tests.test_worker_process` 执行，具体运行版本及结果见 `SECOND_RELEASE_PROGRESS.md`，不能仅以本协议单元测试代替。

2026-09-09：独立 Redis 7 临时容器上运行 `tests.test_task_queue` 和 `tests.test_dispatch_outbox`，20 项全部通过。新增测试使用临时 SQLite 和随机 Redis 命名空间，覆盖审批、自动化和成员重试的未开始消息重建、重复扫描去重，以及执行中/终态不重放。此结果只证明消息恢复与事务行为，不代表三类业务执行的完整故障演练。

参考：Redis 官方 [XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/) 文档。
