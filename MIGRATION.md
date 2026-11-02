# 从注入版迁移到 0.14.0 原生插件

## 从旧注入版升级

1. 等待当前任务结束，停止 Gateway，记录现有插件版本并备份配置
2. 在旧发行包仍可用时，使用旧版所在 Python 执行 `python -m hermes_lark_streaming uninstall`，移除 AST 注入
3. 确认旧注入已清除。不要用历史整文件备份覆盖带有其他修改的 Hermes 源码
4. 卸载旧包/旧插件，通过 Hermes 插件管理器安装新版和依赖，避免手工向托管环境额外 pip 安装
5. 保留 `platforms.feishu` 及原有凭据、Home Channel、display、会话和 Cron 目标
6. 把旧 `streaming` 下的卡片样式参数移动到 `platforms.feishu.extra.card`，保留全局 `streaming.enabled: true`
7. 设置 `display.platforms.feishu.tool_progress: 'off'`；卡片工具面板由原生 Hook 提供
8. 启用插件、运行只读 `verify`、重启 Gateway
9. 在测试聊天中验证问答、工具、引用、停止、审批、后台和在线/离线 Cron

`card_ttl_sec` 已移除，旧配置可删除；活动卡片跟随宿主结束/中断信号收尾，不再因卡龄或静默时间强制结束。

新版不包含旧 AST 清理器。如果先升级导致旧卸载命令不可用，先用隔离环境安装旧发行包完成卸载，再启用新版。

页脚默认单行展示 `status`、`elapsed`、`context`、`model`，原有 `context` 配置可保留。上下文使用原生 Hook 提供的最近一次 `prompt_tokens`（含缓存）和 `context_length`，不跨请求累加。

常规安装与启用步骤见 [README](README.md#安装)。新版保留 `feishu` 平台身份，沿用内置连接与权限实现，无需新建机器人或迁移会话和 Cron 目标。

## 回退

停用插件并重启 Gateway 即恢复内置飞书：

```bash
hermes plugins disable hermes-lark-streaming
hermes gateway restart
```

需要恢复旧版卡片时，另行安装旧发行包并按旧文档操作。新版不会自动重装注入。

## 验收清单

- 单聊/群聊、白名单、配对与 @ 规则正常
- 一轮回答持续更新，最终正文不重复
- 引用和话题定位正常，同群多人不串卡
- /stop、异常、关闭和连续打断后旧卡不持续加载
- 审批/澄清后正常续答，图片和文件不丢失
- Gateway 在线与离线的 Cron 均能发送
- 停用插件能恢复内置展示，Hermes 核心源码不变

命令审批后按 Hermes 0.22.0 协议改为终态卡投递，澄清后可以恢复流式。接口检查与模拟 API 测试不能代替真实飞书端验收。
