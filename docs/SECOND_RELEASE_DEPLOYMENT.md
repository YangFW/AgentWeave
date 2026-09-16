# 第二次上线实际部署记录

日期：2026-09-09（Asia/Shanghai）。用户明确回复“确认授权”后，按交接清单完成本机部署及每日维护备份安装。未提交 Git，未操作其他项目容器。

## 当前运行状态

- 入口：`http://192.168.0.230:8000/`。部署初始仅本机监听；用户提供内网 IP 并要求修改后，已通过 `APP_BIND_HOST=192.168.0.230` 绑定该网卡地址，仅重建 API 容器。
- Compose 项目：`agentnexus`；API 服务 `agentnexus`、Redis 服务 `redis`、两个 `worker` 副本。
- 当前 API 与 Worker 镜像为 `sha256:f6e62c59b05ee20f542dcb9bdbddb6d43038a7e68bb5e999b0ac9853bf915588`。初次部署镜像 `sha256:3f8399f6a13af467a8e48eb3cc9e6a828a9a49456cc73b2fa5867ad8fb48dfc3` 已保留为 `before-context-hotfix-20260910` 回滚标签。
- 认证已开启，每用户一个并发执行。用户后续要求修复模型联网错误后，出站网络已开启，模型白名单为 `api.openai.com,ai.yujiangrubber.cn`；远程/stdio MCP 仍关闭。模型接入详情见 `SECOND_RELEASE_MODEL_SETUP.md`。
- 使用既有 admin 账号及已修复密码，没有再次重置或创建测试用户。
- `.env.release` 和 `data/.env.runtime` 权限为 0600，均被 Git 忽略。保留原本的 PPTX 生成器选择及 `data/.secret_key`。
- 最终 readiness：数据库正常、Redis 正常、在线 Worker 数为 2。API 和 Redis 容器健康，两 Worker 运行中。

## 备份与迁移

部署前没有活动 API/Worker，8000 端口未占用。数据库 quick_check 通过，所有既有任务与运行均为终态。

部署前完整快照：

`/home/rickie/AgentNexus/backups/release-deploy-20260909T113743Z`

校验通过，共 829 文件。对应旧配置、原启动脚本、Compose 配置、Git HEAD 归档及部署回执保存在同级 `release-deploy-20260909T113743Z-configuration/`；敏感备份文件权限为 0600，目录为 0700。

历史上传记录引用旧机器绝对路径，但全部文件仍在当前数据目录。校验文件大小、快照 SHA-256 及产物哈希后，在一个 SQLite 事务中将 486 条上传路径改为 `data/uploads/...`、327 条产物路径改为 `data/artifacts/...`，原有产物 relative_path 不变。未改文件内容，外键检查无异常。相对项目路径在本机项目根目录及容器 `/app` 工作目录均可解析。

维护备份实测快照：

`/home/rickie/AgentNexus/backups/agentnexus-20260909T114752Z-3492689`

校验通过，共 831 文件，包含部署后的 `.env.runtime` 和新增验收产物。维护脚本实际停止 API/Worker、生成快照，再恢复原先运行的三个容器；Redis 保持运行。恢复后再次验证登录和 readiness。

## 已安装的每日备份

当前主机时区为 Asia/Shanghai，cron 服务 active。在 rickie 的 crontab 追加每日 03:00 维护备份，原有条目原样保留，并已另存 `crontab.before`。

```cron
0 3 * * * AGENTNEXUS_BACKUP_DIR=/home/rickie/AgentNexus/backups /bin/sh /home/rickie/AgentNexus/scripts/backup-maintenance.sh --maintenance-window-approved >> /home/rickie/AgentNexus/backups/maintenance.log 2>&1
```

日志文件权限为 0600。该任务会短暂停止 API/Worker，维护窗口已获授权；旧快照不自动删除。本机备份不覆盖整盘故障，外部备份目的地仍需另行确定。

## 实际服务验证

- 未登录访问任务 API 返回 401；既有 admin 登录成功，验收会话已登出。
- 容器内全部 486 份历史上传、327 份原有产物可读；抽查历史产物下载 SHA-256 与数据库记录一致。
- 创建一条明确标记为第二次上线验收的 Markdown 任务，ID `task_0a473794e68d`，状态 completed，产物可下载。
- 维护备份恢复后，管理员登录、模型/工作区/能力页面 API 均返回 200。
- 最终记录数：用户 1、任务 837、上传 486、产物 328。与部署前相比只新增一条验收任务、一份产物。

## 2026-09-10 定时执行复核

首个每日 03:00 任务已由 cron 实际触发，生成 `backups/agentnexus-20260909T190004Z-3672844`。清单时间为 UTC 2026-09-09 19:00:09（北京时间次日 03:00:09），831 文件重新校验通过。维护日志记录三个容器停止及恢复，09:10 复核 readiness 正常、Worker 数为 2，cron 服务 active。

截至本次只读检查，实际运行库有一名启用的管理员；上线后有一条确定性验收任务和一条 gpt-5.5 任务完成。该统计不代表已经开展两名用户的多人试运行。

## 2026-09-10 附件与超时提示补丁

真实模型补验发现专家目标识别及子任务没有收到附件上下文，以及总超时错误消息为空。修复通过相关单元、上下文恢复及新镜像 Compose 回归后，在线上没有活动任务时发布。升级前快照为 `backups/context-hotfix-20260910T015404Z`，831 文件校验通过，配置与镜像回执位于同名 `-configuration/` 目录。

发布后核对 API 和两个 Worker 均使用本页当前镜像，readiness 正常、页面返回 200、未登录访问任务返回 401。访问地址、网络白名单、每日备份和生产模型 90 秒超时配置保持原值。真实长文档在隔离的 180 秒配置下通过；真实专家首轮一名成员被语义验收拒绝，单独重试后完成主管汇总，详细证据见实施记录。

## 后续事项

内网入口及模型联网已按用户后续要求完成，真实模型补验证据和限制见上文。是否将生产模型超时改为 180 秒等待用户选择；独立备份存储和原计划的真实用户试运行仍需安排。
