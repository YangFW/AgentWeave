# 第二次上线部署操作说明

2026-09-09 已完成用户授权的本机部署及每日维护备份安装，实际记录见 `SECOND_RELEASE_DEPLOYMENT.md`。本手册用于后续启动和维护；扩大到真实多人使用前，继续按原计划完成模型验收和用户试运行。

## 准备配置

复制 `.env.release.example` 为 `.env.release`，首次启动填写非空的 `APP_ADMIN_PASSWORD`。不要提交 `.env.release`，也不要把真实配置直接粘贴到日志或问题报告。

API 与 Worker 共用 Compose 中的环境配置。模型和工具的白名单、密钥环境变量、上传和产物目录必须在两者中一致。Redis 仅在 Compose 网络内访问，不映射宿主机端口。

如果管理员已存在，启动时环境变量不会覆盖其密码；需要由管理员用户管理接口重置。

在“项目”中选择已有项目，管理员或项目所有者可在“项目成员”区域按用户名添加成员、切换只读权限或移除成员。账号必须先在“用户管理”创建；普通成员不能修改成员关系。

登录限制为每个账号每分钟 10 次、每个来源每分钟 60 次尝试，包含成功尝试。计数保存在 SQLite，重启不会绕过限制；超过限制返回 HTTP 429 和 Retry-After。HTTPS 登录会设置 Secure Cookie。反向代理须正确传递协议并限制可信代理来源，否则来源识别和 Origin 校验可能不符合部署预期。

## 配置检查和启动

`APP_BIND_HOST` 默认为 `127.0.0.1`。本机实际部署已按用户要求在 `.env.release` 设置为 `192.168.0.230`，访问和检查地址使用 `http://192.168.0.230:8000`；下方通用命令中的回环地址用于默认配置。

以下命令会构建镜像并启动服务，执行前应确认现有服务和数据备份：

```bash
docker compose --env-file .env.release config --quiet
docker compose --env-file .env.release up --build -d --scale worker=2
```

浏览器访问 `http://127.0.0.1:8000/`，登录后通过“用户管理”为成员创建独立账号。默认仅监听回环地址；内网入口和 HTTPS 反向代理需由部署者另行配置，不能将关闭认证的配置公开访问。

API 健康检查通过后启动 Worker。`/api/health` 用于启动顺序；`/api/readiness` 另行检查数据库、Redis 和当前队列的 Worker 心跳。Redis 模式下没有在线 Worker 时返回 HTTP 503，至少一个 Worker 心跳有效时返回 200。心跳有效期 30 秒，不等于外部模型或全部工具可用。

## 停止、日志和升级

计划中的逻辑名称与实际配置对应如下，避免混用示例默认值：

| 计划项 | 实际实现及部署值 |
| --- | --- |
| API 服务 | Python 入口 `app.main:app`，Compose 服务名 `agentnexus` |
| 独立 Worker | Python 入口 `app.worker`，Compose 服务名 `worker` |
| Worker 数量 | 代码/Compose 默认 1；多人示例设为 2，可用 `--scale worker=2` 指定 |
| 单用户并发 | 代码默认 2；多人示例设为 1，配合两个 Worker 使用 |
| SQLite | Compose 的 `APP_DB_PATH=/app/data/platform.db`，宿主机挂载 `./data` |
| Redis | Compose 内固定使用 `redis://redis:6379/0`，不公开 Redis 端口；本地未配置 REDIS_URL 时使用进程内调度 |
| 成功与重试状态 | 公共成功状态保留 `completed`，对应计划中的 succeeded；重试创建新 Run，其初始状态为 queued，详见任务契约 |
| 健康检查 | `/api/health` 用于启动检查，`/api/readiness` 验证数据库、Redis 和 Worker 心跳，避免 Worker 启动依赖自身就绪造成循环 |

公共 Task/事件使用 `schema_version=1`，内部队列与备份清单分别使用 `version=1`。队列的 `priority=50` 是当前固定值，尚无多优先级调度；`workspace_id` 来自数据库，消费者重新加载资源及权限，不凭消息中的工作区字段授权。

Compose 中 API 的服务名是 `agentnexus`，独立执行服务是 `worker`，队列服务是 `redis`。以下命令均从项目根目录执行；实际部署启停须先安排维护窗口。

```bash
# 查看状态及最近日志
docker compose --env-file .env.release ps
docker compose --env-file .env.release logs --tail 100 agentnexus worker redis

# 停止 API 和 Worker，保留数据库目录及 Redis 数据卷
docker compose --env-file .env.release stop -t 30 agentnexus worker
```

升级顺序如下。先记录当前代码版本、镜像 ID 和数据路径，通知用户保存输入并停止提交任务；执行中的任务应完成或确认允许中断。停止后才生成一致快照，快照完成之前不要重启服务。

```bash
docker compose --env-file .env.release stop -t 30 agentnexus worker
sh scripts/backup-data.sh --quiesced
# 记录上一步输出的快照路径，并核验清单
python3 scripts/data_snapshot.py verify backups/实际快照目录

# 在已核对的新版代码目录中构建并启动
docker compose --env-file .env.release up --build -d --scale worker=2
curl --fail http://127.0.0.1:8000/api/readiness
```

启动后检查管理员登录、工作区访问、普通任务、文件下载和 Worker 日志，再开放用户提交。`readiness` 通过只说明数据库、Redis 与 Worker 心跳就绪，不能代替业务验证。

若升级失败，先停止 API/Worker，保留失败现场；用 `restore-data.sh` 将升级前快照恢复到新目录并校验，再按已记录的旧代码/镜像和路径切换运行目录。目录切换必须明确确认，不自动覆盖 `data/`。当前没有任意数据库版本的自动降级工具，回滚依据是升级前快照及匹配的代码版本。恢复到旧时间点会失去快照之后的新写入，恢复后的旧会话也可能仍有效，处理步骤见 `BACKUP_RESTORE.md`。

## 数据与备份

外部调用的公共配置由 `call_limits.py` 读取：资源的 `config.timeout`、`config.max_retries`、`config.retry_backoff` 优先于对应环境默认值。API 和 Worker 共用 Compose 配置，修改环境变量后需重启相应进程；无需新增依赖。

| 调用类型 | 默认超时 | 默认额外重试次数 | 环境变量 |
| --- | --- | --- | --- |
| 模型摘要/流式工具循环 | 90 秒 | 3 | `APP_MODEL_TIMEOUT_SECONDS`、`APP_MODEL_MAX_RETRIES`、`APP_MODEL_RETRY_BACKOFF_SECONDS` |
| HTTP 工具 | 30 秒 | 0 | `APP_HTTP_TOOL_TIMEOUT_SECONDS`、`APP_HTTP_TOOL_MAX_RETRIES` |
| 远程/stdio MCP | 60 秒 | 0 | `APP_MCP_TIMEOUT_SECONDS`、`APP_MCP_MAX_RETRIES` |

超时必须为有限数字，范围 0.1～600 秒；stdio 为兼容现有配置继续使用 5～300 秒范围及 `startup_timeout` 备用字段。模型额外重试上限为 0～5，退避基数 `retry_backoff` 为 0～10 秒，默认 0.6，按重试次数指数退避。模型仅自动重试 HTTP 429/5xx；超时、传输错误、其他 HTTP 错误直接交给任务失败处理。每次请求均占模型调用预算。

模型流式请求最后一次重试可切换为非流式兼容请求，计入同一个重试上限；默认最多三次流式尝试加一次非流式请求。`max_retries=0` 时只发一次请求，不再发送备用请求。每次网络请求有总时限，整个 Worker 任务仍受 `APP_TASK_TIMEOUT_SECONDS` 约束。

HTTP/MCP 调用可能产生不可撤销副作用，`max_retries` 当前只接受 0，配置正数会明确报错；调用超时后不自动重发。需要先查看任务、工具副作用记录和外部结果，再由用户显式重试任务。远程 MCP 的初始化、发现/调用和会话退出共同受一次调用总时限约束；这不保证远端已执行操作能撤销。

`APP_MAX_TOOL_CALLS` 默认 32，作为运行权限快照的工具调用上限；智能体配置更小的上限会被保留，不会因平台默认值放宽。实际计数和恢复沿用现有工具执行状态与检查点；缓存结果复用不算新工具执行。

`APP_MAX_MODEL_CALLS` 默认 32，限制任务累计模型请求尝试次数。计数保存在 SQLite，任务恢复不会清零；Worker 中的专家成员继承父任务预算。超过上限时在下一次请求前拒绝执行；测试连接等不属于任务的请求不使用该预算。

容器默认使用 UID/GID 1000、只读根文件系统，数据卷和 `/tmp` 可写。`APP_RUNTIME_UID/GID` 应与宿主机数据目录所有者匹配；本配置不自动改变现有数据目录权限。页面生成器配置保存到数据卷内 `.env.runtime`，应用环境变量仍优先于文件配置；跨进程配置变更后应重启 API/Worker 使其一致。

每个 API/Worker 默认限制 1 CPU、1GB 内存和 128 个进程，可通过示例中的资源配置调整。根文件系统只读时，stdio MCP 依赖应预装在镜像中；不要依靠运行时向系统目录安装组件。

`APP_MAX_CONCURRENT_TASKS_PER_USER` 限制每用户同时执行的顶层任务数，默认 2，适用于普通任务、专家团父任务和自动化。配额在 Redis 中原子领取、续租和释放；容量不足时消息保留并稍后重领。它不预留专属 Worker；若仅有两个 Worker 且希望单用户最多占用一个，应配置为 1。

多人版 `.env.release.example` 已采用两个 Worker、每用户一个并发执行的组合；代码层默认值保留兼容性，可按实际容量调整。

单任务最多 10 个不同附件，附件总大小默认 40MB，通过 `APP_MAX_TASK_ATTACHMENTS_MB` 调整。超量、重复或失效附件会在创建任务前明确拒绝，不再静默截断。单文件上传限制仍由 `APP_MAX_UPLOAD_MB` 控制。

Webhook 使用签名授权，不需要浏览器 Cookie。只有 `POST /api/loops/{loop_id}/webhook` 采用此入口；仍要求有效时间戳、HMAC 签名和 Idempotency-Key，并检查所属用户及工作区执行权限。其他自动化接口仍要求登录。停用用户或移除执行权限后，即使签名正确也不能触发任务。

Worker 每次取得执行配额后，从数据库加载启用的 Policy 规则及 `APP_POLICY_RULES_JSON`。API 中的规则新增、修改和停用在下一次 Worker 执行时生效；当前正在执行的任务不保证实时切换规则。规则配置变更不替代取消任务，账号和工作区权限撤销仍由执行期间的权限复查处理。

`APP_TASK_TIMEOUT_SECONDS` 控制 Worker 单次执行上限，默认 1800 秒，允许范围大于 0 且不超过 86400。超时写入失败终态，由用户缩小任务范围或调整配置后重试。此上限取消 Python 协程，不等于每个外部子进程或线程均能立即终止。

`./data` 同时挂载给 API 和 Worker；Redis 使用独立数据卷。不要启动多个 API 实例，因为调度器及部分专家/审批逻辑尚未全部迁到 Worker。

备份和暂存恢复见 `BACKUP_RESTORE.md`。快照目录和恢复暂存目录已从 Git 与 Docker 构建上下文排除。

## 验收记录

- Compose 展开配置测试通过：API/Worker 环境和挂载一致，Worker 无固定容器名，Redis 未公开端口。
- 独立进程测试通过：离线任务在排队期间重启 API 后，由 Worker 完成，未出现重复运行或重复最终回答。
- 已构建本 Compose 镜像并在独立实例验证非 root、只读根文件系统、登录、上传、普通任务和 Markdown 交付；外部模型、MCP 及完整故障恢复仍需继续验证。
