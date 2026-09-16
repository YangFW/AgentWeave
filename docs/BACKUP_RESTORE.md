# 多人版备份与恢复

当前脚本仅支持默认布局：一个数据目录下的 `platform.db`、`uploads/`、`artifacts/`，以及可选 `.secret_key` 和 `.env.runtime`。备份包含敏感数据，目录权限为 0700，文件权限为 0600。快照校验用于检测损坏，不提供防篡改签名或加密。

## 备份

可用 `sh scripts/backup-maintenance.sh --maintenance-window-approved` 执行 Compose 维护窗口备份：记录当前运行的 API/Worker 容器、停止它们、生成快照，再恢复原先运行的容器。失败或中断时也尝试恢复服务。脚本不修改 Redis 和其他项目服务，不自动安装调度；`examples/backup.cron.example` 仅为可审查的每日调度示例。实际启用前必须确认停机时间、路径、运行用户权限和外部备份存储，不应同时运行多个维护备份。

脚本使用本机 `flock` 防止维护任务重叠；默认锁文件为 `data/.backup-maintenance.lock`。确需指定其他位置时可使用 `AGENTNEXUS_MAINTENANCE_LOCK`，同一部署的所有维护入口必须使用同一个锁。获取锁失败或缺少 flock 时，在任何容器启停前退出。

先停止 API、Worker 及其他可能写入此数据目录的进程。数据库快照使用 Python 标准库 SQLite backup API，能够包含 WAL 中已提交的内容；文件一致性依赖停机，脚本不能自动证明所有写入者都已停止。

```bash
scripts/backup-data.sh --quiesced
```

脚本默认在 `backups/` 下创建新目录，输出其路径。可用 `AGENTNEXUS_DATA_DIR` 指定数据目录，`AGENTNEXUS_BACKUP_DIR` 指定备份存放目录。已有快照不会被覆盖或自动清理。

```bash
python3 scripts/data_snapshot.py verify backups/实际快照目录
```

缺少清单、版本不支持、文件缺失或额外文件、SHA-256 不匹配、符号链接和 SQLite 完整性检查失败都会中止操作。复制中断的目录不算有效快照，需重新创建新的快照。

数据、快照和恢复目标的父目录也不能通过符号链接间接指向其他位置；使用实际目录的规范路径。挂载点可以使用，符号链接别名会被明确拒绝。

## 恢复到暂存目录

```bash
scripts/restore-data.sh backups/实际快照目录 restore-staging/本次恢复
```

第二个参数必须是不存在的新目录。脚本先校验快照，再写入暂存目录，不删除或覆盖现有 `data/`。检查恢复数据库记录与文件后，停机保留原数据目录，再由维护者将暂存目录切换到原数据目录位置。此切换需要操作确认，脚本不会自动执行。

数据库中可能记录文件绝对路径。恢复实例应使用相同容器路径（本部署为 `/app/data`），或先完成经验证的路径迁移；不能假定任意新路径都兼容。2026-09-09 实际部署时已将历史旧机器路径校验后转换为相对项目路径，详见部署记录。恢复后的用户会话仍可能有效，涉及安全事件时应另行撤销会话。

## 自定义配置

若 `APP_DB_PATH`、`APP_UPLOAD_DIR`、`APP_ARTIFACT_DIR` 或 `APP_SECRET_KEY_FILE` 位于上述布局之外，不要把此快照当作完整备份。必须另行备份这些路径并记录恢复位置。通过 `APP_SECRET_KEY` 或其他外部系统提供的密钥，以及环境变量、MCP 凭证、Redis 队列不包含在快照内，需要独立保管与恢复。

恢复数据库到较早时间点后，旧 Redis 消息可能不再对应有效运行记录；可靠队列恢复协议尚未完成，目前不能以这些脚本单独证明第二次上线的完整灾难恢复能力。

## 已验证范围

`python3 -m unittest tests.test_data_snapshot -v` 在临时数据目录验证 WAL 数据、附件、产物、本机密钥恢复以及损坏文件、缺失文件、符号链接和目标覆盖拒绝。尚未替代真实部署上的备份恢复演练。
