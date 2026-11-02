# Hermes Lark Streaming

[![Python](https://img.shields.io/badge/python-%E2%89%A53.11-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.en.md)

![Hermes Lark Streaming](assets/cover.jpg)

Hermes 飞书 / Lark CardKit 2.0 流式卡片插件。0.14.0 起通过 Hermes 原生平台接口接入，在 Gateway 进程内运行。

- 保留内置飞书的连接、用户权限、配对、群策略、附件、话题和审批
- AI 回复持续更新到卡片，保留思考、工具状态、图片解析和长内容拆卡
- 在线与独立 Cron、后台任务结果使用卡片投递
- 卡片失败时交回内置回复链路
- 不修改 Hermes 源码，不使用 AST 注入、monkey-patch 或 sidecar

## 要求

- Hermes **>= 0.22.0**，已配置内置飞书渠道
- Python >= 3.11；Hermes 的运行环境由 Hermes 自己管理
- `lark-oapi >= 1.6.4`、`PyYAML >= 6.0`
- 飞书应用具备消息发送/回复、CardKit 读写和所用媒体权限

旧注入版用户必须先阅读 [升级说明](MIGRATION.md)，用旧版卸载注入后再安装新版。

## 安装

```bash
hermes plugins install https://github.com/Cheerwhy/hermes-lark-streaming
hermes plugins enable hermes-lark-streaming
hermes gateway restart
```

本地开发可将仓库符号链接到 `~/.hermes/plugins/hermes-lark-streaming`，再执行上述启用和重启命令。修改源码后重启 Gateway 生效。

插件使用正式平台注册接口替换 `feishu` 的展示适配器。保留现有 `platforms.feishu`、凭据、会话和 Cron 目标，不另建渠道。

## 配置

编辑当前 Hermes Profile 的 `config.yaml`（默认 `~/.hermes/config.yaml`），合并以下配置，保留已有的飞书连接和权限设置。

### 最小配置

```yaml
streaming:
  enabled: true

platforms:
  feishu:
    enabled: true
    extra:
      card:
        enabled: true

display:
  platforms:
    feishu:
      tool_progress: 'off'
      show_tool_use: true
      show_reasoning: false
```

`streaming.enabled` 是 Hermes 的流式总开关，默认 `false`，必须开启；同时不要将 `streaming.transport` 设为 `off`，或将 `display.platforms.feishu.streaming` 设为 `false`。

### 卡片参数

以下键均位于 `platforms.feishu.extra.card`，未设置时采用表中默认值。外观选项用于流式卡片及其完成态；独立 Cron、后台结果等静态卡片使用简洁固定样式。

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `enabled` | `true` | 启用原生流式卡片；`false` 仅关闭流式接管，停用整个插件见下文 |
| `width_mode` | `default` | 卡片宽度：`default`、`compact`、`fill`；无效值回退为 `default` |
| `panel_expanded` | `false` | 完成后保持思考、工具面板展开；`false` 表示完成后折叠 |
| `header.enabled` | `false` | 显示顶部状态标题 |
| `body.text_size` | `normal_v2` | 正文 Markdown 字号，使用 CardKit 支持的字号标记 |
| `footer.enabled` | `true` | 显示完成态页脚 |
| `footer.text_size` | `notation` | 页脚字号，使用 CardKit 支持的字号标记 |
| `footer.fields` | `[[status, elapsed, context, model]]` | 页脚字段及排列，详见下节 |
| `footer.show_label` | `false` | 为耗时和上下文增加文字标签 |

例如调整卡片外观，在已有 `card` 下合并：

```yaml
width_mode: fill
panel_expanded: true
header:
  enabled: true
body:
  text_size: normal_v2
```

### 页脚布局

`footer.fields` 每个内层数组对应一行，行内用 `·` 分隔。支持以下五个字段；没有数据的字段自动省略。

| 字段 | 内容 |
| --- | --- |
| `status` | 完成、出错或已停止 |
| `elapsed` | 卡片耗时 |
| `context` | 最近一次 API 请求的完整输入量 / 宿主上下文上限及占比，包含缓存输入 |
| `tokens` | 本回合 API 用量累计，`↑` 非缓存输入、`↓` 输出；宿主仅提供完整输入时以完整输入计 |
| `model` | 模型名称 |

例如在 `platforms.feishu.extra.card` 下设置两行页脚：

```yaml
footer:
  enabled: true
  text_size: notation
  show_label: true
  fields:
    - [status, elapsed]
    - [tokens, model]
```

一维数组如 `[status, elapsed]` 表示单行；空数组恢复默认布局，隐藏页脚请用 `footer.enabled: false`。`context` 缺少完整输入量或窗口上限时省略；默认布局为单行 `[status, elapsed, context, model]`，`tokens` 可按需加入。

### 思考与工具面板

| 配置路径 | 默认值 | 作用 |
| --- | --- | --- |
| `display.platforms.feishu.show_tool_use` | `true` | 显示卡片工具面板：名称、简短参数、状态和耗时；不展示完整成功结果 |
| `display.platforms.feishu.show_reasoning` | `false` | 显示模型提供的思考内容，还需开启下方事件开关 |
| `plugins.stream_reasoning_deltas` | `false` | 允许 Hermes 向插件提供流式思考事件 |
| `display.platforms.feishu.tool_progress` | Hermes 决定 | 建议设为 `'off'`，避免宿主工具进度文本与卡片工具面板重复 |

`show_tool_use`、`show_reasoning` 的读取顺序为飞书专属设置 → `display` 下的同名全局设置 → 表中默认值。例如展示思考：

```yaml
plugins:
  stream_reasoning_deltas: true
display:
  platforms:
    feishu:
      show_reasoning: true
```

展示开关不限制工具权限，也不会让模型产生原本未提供的思考内容。

### 生效与渠道设置

修改卡片参数或宿主流式设置后执行 `hermes gateway restart`。`show_tool_use`、`show_reasoning` 会在运行中重新读取，作用于后续渲染，不主动刷新已结束的历史卡片。

凭据与渠道设置继续使用 Hermes 内置飞书配置：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_DOMAIN`，以及 `platforms.feishu.extra` 下的连接、群策略等选项，不放入 `card`。插件不新增渠道权限配置。

旧版卡片参数从 `streaming.*` 移至 `platforms.feishu.extra.card`，全局 `streaming.enabled` 保留原位；插件不会自动迁移旧配置，详见 [升级说明](MIGRATION.md)。

命令审批后的答案遵循 Hermes 0.22.0 的协议，一次性发送终态卡；澄清后可恢复流式。澄清选项沿用内置文本回复流程，审批按钮沿用内置卡片回调。

## 卡片展示

回答持续更新到卡片，支持思考和工具面板，长内容自动拆卡。

![流式卡片](assets/streaming.jpg)

使用 `/stop` 停止生成：

![停止生成后的卡片](assets/abort.jpg)

新消息打断当前回复：

![消息打断后的卡片](assets/interrupt.jpg)

以上为既有渲染效果截图，实际展示取决于卡片配置与宿主版本。0.14.0 原生接入已通过自动化测试，真实飞书端验收尚未执行。

## 停用与诊断

```bash
hermes plugins disable hermes-lark-streaming
hermes gateway restart
```

停用后恢复内置飞书展示。`python -m hermes_lark_streaming verify` 只读检查宿主接口和遗留注入，不会发消息或改配置。旧 `install`、`uninstall`、`restore` 补丁命令已移除。

## 开发与验证

在隔离开发环境安装本插件与固定的 Hermes 0.22.0 源码，然后执行：

```bash
python -m pip install -e '.[dev]'
python -m ruff check hermes_lark_streaming tests scripts __init__.py
python -m mypy hermes_lark_streaming scripts
python -m pytest tests -q
```

插件复用内置飞书适配器，并读取宿主的消息上下文；这些依赖包含私有接口，升级 Hermes 后应重新验证兼容性。

CI 固定测试 Hermes 0.22.0 和 0.22.5。API 测试使用模拟飞书服务，集成测试使用实际 Hermes 注册器和流式消费者；真实飞书收发需单独验收。

真实出站验收：设置 `FEISHU_E2E_APP_ID`、`FEISHU_E2E_APP_SECRET` 后，执行 `python scripts/feishu_smoke.py --chat-id <测试群ID> --scenario all`。脚本发送固定测试卡片，覆盖流式、序号恢复、拆卡、停止和静态更新；可加 `--reply-to <消息ID>` 验证引用，不调用模型或真实工具。入站权限、澄清和 Cron 调度仍需人工验收。

## 贡献者

感谢以下贡献者的 Issue 和 PR：

<a href="https://github.com/Mxin-9527"><img src="https://avatars.githubusercontent.com/u/178271393?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/gitteeee"><img src="https://avatars.githubusercontent.com/u/128769493?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/Bandersnatch0x"><img src="https://avatars.githubusercontent.com/u/13325067?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/runfali"><img src="https://avatars.githubusercontent.com/u/39327978?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/thunderfight127-svg"><img src="https://avatars.githubusercontent.com/u/275854191?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/willggy"><img src="https://avatars.githubusercontent.com/u/74762604?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/atomperson"><img src="https://avatars.githubusercontent.com/u/14934637?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/linjunxin01"><img src="https://avatars.githubusercontent.com/u/63715504?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/mouxangithub"><img src="https://avatars.githubusercontent.com/u/48978046?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/numuly"><img src="https://avatars.githubusercontent.com/u/137970054?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/wzgrx"><img src="https://avatars.githubusercontent.com/u/39661556?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/zhaomingcheng01"><img src="https://avatars.githubusercontent.com/u/46734892?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>

灵感来源于 [openclaw-lark](https://github.com/larksuite/openclaw-lark) 和 [hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card)。

## 许可证

[MIT](LICENSE)
