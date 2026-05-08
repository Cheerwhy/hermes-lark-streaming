# Hermes Lark Streaming

[![PyPI](https://img.shields.io/badge/python-%E2%89%A53.11-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[Hermes](https://github.com/NousResearch/hermes-agent) Gateway 飞书流式卡片插件 — 基于 CardKit v2.0 的进程内流式消息卡片。

灵感来源于 [openclaw-lark](https://github.com/larksuite/openclaw-lark) 和 [hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card)。

[English](README.md)

---

## 功能

- **流式输出** — AI 回复实时显示在交互卡片中，打字机效果
- **思考过程** — 显示模型的推理/思考内容（可折叠面板）
- **工具调用** — 实时展示工具调用状态和进度，含标准图标和结果/错误块
- **CardKit v2.0** — 优先使用飞书 CardKit 流式 API，自动降级到 IM PATCH
- **终态卡片** — 完成后展示完整结果，含 token 用量、耗时、上下文信息
- **消息保护** — 消息被删除/撤回后自动终止更新，避免无效 API 调用
- **图片解析** — 自动识别 markdown 图片引用，下载上传后替换为飞书 img_key
- **中断处理** — 处理 `/stop` 命令，展示中断状态卡片

---

## 运行要求

- Hermes Gateway 已安装并配置飞书平台
- `Python >= 3.11`
- `lark-oapi >= 1.4.0` — 飞书/Lark 官方 Python SDK
- `PyYAML >= 6.0` — YAML 解析库
- 飞书应用权限：消息卡片（CardKit）读写、消息发送与回复、图片上传

---

## 安装

> **注意：** Hermes 运行在独立的 Python 虚拟环境中，请使用 Hermes 的 Python 安装插件，否则 gateway 启动后会无法加载。

```bash
git clone https://github.com/Cheerwhy/hermes-lark-streaming.git
cd hermes-lark-streaming

# 安装到 Hermes 的 venv 中，确保 gateway 能加载插件
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
$HERMES_PYTHON -m pip install -e .
$HERMES_PYTHON -m hermes_lark_streaming verify   # 验证兼容性
$HERMES_PYTHON -m hermes_lark_streaming install   # 注入 hook
hermes gateway restart
```

---

## 配置

在 `~/.hermes/config.yaml` 中添加：

```yaml
streaming:
  enabled: true
```

### 凭据

凭据按以下顺序解析：

| 优先级 | 来源 | 变量 |
|--------|------|------|
| 1 | 环境变量 | `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（或 `LARK_APP_ID` / `LARK_APP_SECRET`） |
| 2 | 配置文件 | `~/.hermes/config.yaml` 中的 `feishu` 或 `lark` 区段 |

```env
FEISHU_APP_ID=cli_xxxxx
FEISHU_APP_SECRET=xxxxx
```

### 页脚

通过 `streaming.footer` 自定义完成态卡片的页脚：

```yaml
streaming:
  enabled: true
  footer:
    fields:
      - [status, elapsed, model]
      - [tokens, context]
    show_label: true
```

**字段**（`footer.fields`）：二维数组，每个子数组为一行，字段间用 `·` 连接。

| 字段 | 说明 | 有标签 | 无标签 |
|------|------|--------|--------|
| `status` | 完成状态 | `✅ Completed` | `✅ Completed` |
| `elapsed` | 耗时 | `Elapsed 12.3s` | `12.3s` |
| `model` | 模型名称 | `deepseek-v4-flash` | `deepseek-v4-flash` |
| `tokens` | Token 用量 | `↑ 1.2K ↓ 500` | `↑ 1.2K ↓ 500` |
| `context` | 上下文窗口用量 | `Context 50K/200K (25%)` | `50K/200K (25%)` |

**显示标签**（`footer.show_label`）：是否展示字段标签（如 "Elapsed"、"Context"）。默认：`true`。

未配置时的默认值：`fields: [[status, elapsed, model], [tokens, context]]`，`show_label: true`。

---

## 工作原理

插件通过 AST 注入在 `gateway/run.py` 的 **6 个位置**插入 hook 调用，所有业务逻辑在 `hermes_lark_streaming` 包内完成：

| Hook | 注入位置 | 说明 |
|------|----------|------|
| `on_message_started` | `_handle_message_with_agent` 函数体开头 | 创建卡片会话，发送占位卡片 |
| `on_tool_updated` | `progress_callback` 内部 | 实时展示工具调用状态 |
| `on_answer_delta` | `_stream_delta_cb` 内部 | 流式更新回答文本 |
| `on_thinking_delta` | `_interim_assistant_cb` 内部 | 显示思考/推理过程 |
| `on_message_aborted` | stale `return None` 之前 | 处理 `/stop` 中断 |
| `on_message_completed` | `return response` 之前 | 发送终态卡片 |

**消息处理流程：**

```
用户发送消息
  → 创建卡片会话
  → 流式更新（reasoning、工具状态、文本增量 — 节流调度）
  → 图片 URL 异步解析替换
  → 终态卡片（token/耗时/上下文）
```

若消息被删除/撤回，UnavailableGuard 自动终止后续更新。

---

## 降级策略

| 策略 | 间隔 | 触发条件 |
|------|------|----------|
| CardKit 流式（优先） | 100ms | 默认 |
| IM PATCH（降级） | 1.5s | CardKit 创建失败、表格超限等 |
| 速率限制 | — | 跳过当前帧，不降级通道 |
| 终态卡片失败 | — | Gateway 回退到默认文本回复 |

---

## CLI 命令

```bash
python -m hermes_lark_streaming status     # 查看状态
python -m hermes_lark_streaming verify     # 验证兼容性（不修改文件）
python -m hermes_lark_streaming install    # 注入 hook
python -m hermes_lark_streaming uninstall  # 移除 hook
python -m hermes_lark_streaming restore    # 从备份恢复原始 run.py
```

---

## 文件结构

```
hermes_lark_streaming/
├── __init__.py            # 包入口 + register() 插件 API
├── __main__.py            # CLI: install / uninstall / status / verify / restore
├── cardkit.py             # CardKit v2.0 卡片模板构建
├── config.py              # 配置读取（config.yaml + .env）
├── controller.py          # 流式卡片主控制器（单例）
├── feishu.py              # 飞书 API 客户端（lark-oapi SDK）
├── flush.py               # 节流调度器（异常安全 + wait_for_flush）
├── image.py               # 图片 URL 解析（同步 strip + 异步上传 + 回调）
├── text.py                # 流式文本累积器（reasoning 提取 + 增量追踪）
├── tooluse.py             # 工具调用追踪（图标映射 + 结果块格式化）
├── unavailable_guard.py   # 消息不可用保护（删除/撤回检测 + 终止 pipeline）
├── patch.py               # 6 个 hook 函数（被注入代码调用）
└── patcher.py             # AST 注入器（修改 run.py）
```

---

## 卸载

```bash
python -m hermes_lark_streaming uninstall
pip uninstall hermes-lark-streaming
```

---

## 注意事项

- `install` 会修改 `~/.hermes/hermes-agent/gateway/run.py`，自动创建 `.hermes_lark.bak` 备份
- Hermes 更新后需重新运行 `verify` + `install`
- 插件与 Hermes 内置飞书适配器互补工作：插件负责流式卡片，内置适配器负责消息收发
- 仅对飞书平台生效，其他平台不受影响

## 许可证

[MIT](LICENSE)
