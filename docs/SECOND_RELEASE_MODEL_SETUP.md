# 真实模型接入记录

## 最新执行状态

用户随后明确要求修复“当前部署已关闭出站网络”错误。已备份 `.env.release` 到 `backups/network-config-20260909T122216Z/`，开启 `APP_ALLOW_OUTBOUND_NETWORK=true`，并在保留 `api.openai.com` 的基础上将 `ai.yujiangrubber.cn` 加入模型白名单。API 与两个 Worker 已重新创建，运行环境均验证生效，readiness 正常。

首次验证请求返回 HTTP 400，但不再是网络关闭错误；后续核对数据库的最新连接测试记录为 `pass`，回复 `OK`，时间 `2026-09-09T12:24:04.473579+00:00`。这是系统最新测试结果，不将首次请求记成成功。未修改或输出已有模型密钥。

用户指定 OpenAI / gpt-5.5 / https://ai.yujiangrubber.cn，新密钥暂未准备。

## 初次核对状态（历史）

- 系统已有模型配置 ID `gpt-5.5`，模型名 `gpt-5.5`，适配器为 `openai_compatible`。
- 已有基础地址 `https://ai.yujiangrubber.cn/v1`，配置处于 enabled 状态，并存在加密保存的密钥。本次未新增、覆盖或解密输出模型密钥。
- 无密钥探测 `/v1/models` 返回 401 JSON，`/models` 返回 HTML。该结果支持使用 `/v1` 作为候选 API 基础路径，不能证明密钥有效或 gpt-5.5 可调用。
- 当前部署出站网络关闭，尚未发送带密钥的真实模型请求。

## 原接入方案

1. 保存现有 `.env.release` 的受保护副本。
2. 设置 `APP_ALLOW_OUTBOUND_NETWORK=true`，将 `ai.yujiangrubber.cn` 追加到 `APP_MODEL_HOST_ALLOWLIST`，保留已有白名单项。
3. 在没有活动任务时重建 API/Worker 容器，使环境配置生效，继续使用已验证镜像和原数据卷。
4. 使用既有加密密钥执行模型连接测试，记录成功或公开错误类型，不输出密钥。
5. 连接有效后再进行真实模型任务验收；无效时由用户在模型管理页面更新密钥。

网络开关、域名白名单和服务重建已按后续明确请求执行，不需要再次申请同一授权。连接测试通过不替代完整真实模型任务与用户试运行验收。
