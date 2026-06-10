# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.10.5] - 2026-06-10

### 新增

- Cron 推送卡片支持 header，显示任务名称和运行时间。修复 #57. (#59)

### 修复

- 自动发现 pip 安装的 Hermes 模块路径，不再依赖 git clone 标准目录布局。修复 #55. (#58)

### Added

- Cron delivery cards now support an optional header showing task name and run time. Fixes #57. (#59)

### Fixed

- Auto-discover Hermes module paths for pip-installed scenarios, no longer requiring the standard git-clone directory layout. Fixes #55. (#58)

---

## [0.10.3] - 2026-06-09

### 新增

- Queued follow-up hooks：支持 Hermes queue 模式下连续消息的卡片生命周期管理，正确传递 completion ID。(#53)

### 修复

- 重试飞书 CardKit 瞬态服务端错误（1663、300000），扩展 gateway timeout 2200 的重试范围至所有 CardKit 操作。(#52)
- 去重 cron 卡片推送到同一 chat_id 的情况。

### Added

- Queued follow-up hooks for streaming card lifecycle under Hermes queue busy mode, propagating completion ID correctly. (#53)

### Fixed

- Retry transient Feishu CardKit server errors (1663, 300000) in addition to gateway timeout 2200 across all CardKit operations. (#52)
- Deduplicate cron card delivery to the same chat_id.

---

## [0.10.0] - 2026-06-05

### 新增

- 卡片外观配置：Header/Footer 开关、正文/Footer 文字大小，流式阶段和完成态均生效。(#18, #47)

  ```yaml
  streaming:
    header:
      enabled: true      # 卡片 header，默认 false
    body:
      text_size: heading  # 正文文字大小，默认 normal_v2
    footer:
      enabled: true       # 卡片 footer，默认 true
      text_size: notation # Footer 文字大小，默认 notation
  ```

  文字大小有效值见[飞书文档](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/content-components/plain-text)。

- Batch update 诊断日志，用于排查 300313 错误。(#49)

### Added

- Card style configuration: `streaming.header.enabled`, `streaming.footer.enabled`, `streaming.body.text_size`, `streaming.footer.text_size`, applied in both streaming and completion phases. (#18, #47)

  ```yaml
  streaming:
    header:
      enabled: true      # card header, default false
    body:
      text_size: heading  # answer body text size, default normal_v2
    footer:
      enabled: true       # card footer, default true
      text_size: notation # footer text size, default notation
  ```

  See [Feishu docs](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/content-components/plain-text?lang=en-US) for valid `text_size` values.

- Diagnostic logging for CardKit batch update failures to aid 300313 troubleshooting. (#49)

---

## [0.9.5] - 2026-06-03

### 修复

- 修复多个 dirty tool segment 同时增长时跨段阈值溢出导致卡片超限的问题。修复 #45. (#46)
- 消除纯 answer 流式阶段冗余的 batch_update 调用：用 tool step 内容快照比较替代 tool_end_offset > 0 条件，正确清理 open tool segment 的 dirty 标志。

### Fixed

- Fix multiple dirty tool segments growing simultaneously causing element threshold overflow. Fixes #45. (#46)
- Eliminate redundant batch_update calls during pure answer streaming by replacing tool_end_offset > 0 guard with content-level snapshot comparison for open tool segment dirty clearing.

---

## [0.9.3] - 2026-06-02

### 修复

- 修复卡片拆分后新卡片中段元素未正确创建的问题。(#44)

### Fixed

- Fix segment elements not being created in the new card after card split. (#44)

---

## [0.9.2] - 2026-06-01

### 修复

- CardKit 创建失败时正确回退纯文本回复，瞬态网关超时自动重试。(#40)
- 运行时加固：原子写入防止崩溃损坏文件、`on_feishu_normalize` 容错、`get_controller` 线程安全单例。(#42)

### Fixed

- Properly fall back to plain text when CardKit creation fails; retry transient gateway timeouts. (#40)
- Runtime hardening: atomic writes prevent crash corruption, `on_feishu_normalize` error handling, thread-safe `get_controller` singleton. (#42)

---

## [0.9.0] - 2026-05-29

### Highlights

- 后台任务卡片推送：`/background`（`/btw`）任务完成后以 CardKit v2.0 卡片形式推送，支持话题内回复。修复 #38. (#39)
- 升级：
  ```bash
  cd hermes-lark-streaming
  git pull
  HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
  $HERMES_PYTHON -m hermes_lark_streaming uninstall
  $HERMES_PYTHON -m hermes_lark_streaming install
  hermes gateway restart
  ```

### Highlights

- Background task card delivery: `/background` (`/btw`) task results delivered as CardKit v2.0 cards with topic-aware reply. Fixes #38. (#39)
- Upgrade:
  ```bash
  cd hermes-lark-streaming
  git pull
  HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
  $HERMES_PYTHON -m hermes_lark_streaming uninstall
  $HERMES_PYTHON -m hermes_lark_streaming install
  hermes gateway restart
  ```

---

## [0.8.4] - 2026-05-28

### 变更

- 内部模块重构：重组子包结构、引入 StrEnum/TypedDict、移除旧版 hook
- 卡片内 markdown 表格渲染上限从 3 提升至 5

### Changed

- Internal refactor: reorganize sub-packages, introduce StrEnum/TypedDict, remove legacy hook
- Raise markdown table rendering limit in cards from 3 to 5

---

## [0.8.0] - 2026-05-27

### 变更

- 移除非线性模式，简化插件逻辑
- 移除 `streaming.linear` 配置项，所有会话统一走 CardKit 流式路径
- CardKit 创建失败时直接交回 gateway 默认回复，不再降级到 IM PATCH

### Changed

- Remove non-linear mode to simplify plugin logic
- Remove `streaming.linear` config option; all sessions now use CardKit streaming path
- CardKit creation failure now yields to gateway default reply instead of falling back to IM PATCH

---

## [0.7.3] - 2026-05-26

### 修复

- 修复短回复场景下 CardKit 卡片未正确收尾的异步竞态问题。修复 #32. (#34)
- 修复 Clarify 工具面板重复显示问题文本为原始代码块。修复 #33. (#35)

### Fixed

- Fix CardKit completion race condition in short-reply scenarios where streaming card may not finalize correctly. Fixes #32. (#34)
- Fix Clarify tool panel rendering question text as redundant raw code block. Fixes #33. (#35)

## [0.7.1] - 2026-05-26

### 新增

- 支持 `HERMES_HOME` 环境变量自定义安装路径，与 Hermes 主程序保持一致。修复 #30. (#31)

### Added

- Support `HERMES_HOME` environment variable for custom installation path, aligning with Hermes's own `hermes_constants.get_hermes_home()`. Fixes #30. (#31)

## [0.7.0] - 2026-05-22

### Highlights

- Cron 卡片推送：定时任务结果以飞书 CardKit v2.0 卡片形式发送，保留 Markdown 渲染。修复 #15. (#20)

### 新增

- 完成态面板折叠配置：`streaming.panel_expanded` 控制推理面板和工具面板在完成态卡片中的展开/折叠，默认折叠。修复 #28. (#29)

### 变更

- 线性模式完成态卡片中工具面板和推理面板默认折叠
- 非线性模式完成态卡片中推理面板默认折叠（此前为展开）

### Highlights

- Cron card delivery: scheduled job results sent as Feishu CardKit v2.0 cards, preserving Markdown rendering. Fixes #15. (#20)

### Added

- `streaming.panel_expanded` config option to control reasoning and tool panel state in completion cards, collapsed by default. Fixes #28. (#29)

### Changed

- Tool and reasoning panels in linear completion card now collapsed by default
- Reasoning panel in non-linear completion card now collapsed by default (previously expanded)

---

## [0.6.8] - 2026-05-22

### 修复

- 修复飞书引用消息的虚假 thread_id 导致卡片回复到错误消息的问题
- 新增 NORMALIZE hook，在消息处理前修正飞书引用消息的 thread_id
- 引入 anchor_id 机制，分离会话标识和卡片投递目标

### Fixed

- Fix card replying to the quoted message instead of the user's new message on Feishu quote
- Add NORMALIZE hook to clear false thread_id on Feishu quoted messages before processing
- Introduce anchor_id mechanism to separate session identity from card delivery target

---

## [0.6.7] - 2026-05-22

### 变更

- 线性模式默认开启（`streaming.linear` 默认值改为 `true`）

### 修复

- 修复飞书群聊中引用消息时，卡片仅显示 Done. 的问题。修复 #24. (#25)
- 需要重新安装插件：
  ```bash
  HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
  $HERMES_PYTHON -m pip install -e .
  $HERMES_PYTHON -m hermes_lark_streaming uninstall
  $HERMES_PYTHON -m hermes_lark_streaming install
  ```

### Changed

- Linear mode now enabled by default (`streaming.linear` defaults to `true`)

### Fixed

- Fix Feishu quoted messages showing only Done. in the card. Fixes #24. (#25)
- Requires reinstall:
  ```bash
  HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
  $HERMES_PYTHON -m pip install -e .
  $HERMES_PYTHON -m hermes_lark_streaming uninstall
  $HERMES_PYTHON -m hermes_lark_streaming install
  ```

---

## [0.6.5] - 2026-05-22

### Highlights

- 线性模式多卡拆分：长对话自动在飞书卡片元素接近 200 上限时拆分为多张卡片，数据完整不丢失
- 超长工具调用拆分：单个工具面板步骤过多时按 step 边界拆分到多张卡片

### 修复

- 修复长对话导致飞书卡片元素超限（300305）的问题。修复 #21. (#23)
- 修复长输出底部 markdown 表格失效的问题。修复 #14. (#23)

### Highlights

- Linear mode multi-card split: automatically splits into multiple cards when approaching Feishu's 200-element limit, preserving all data
- Oversized tool call split: splits tool panels with too many steps across cards at step boundaries

### Fixed

- Fix long conversations exceeding Feishu card element limit (300305). Fixes #21. (#23)
- Fix markdown tables at bottom of long outputs not rendering. Fixes #14. (#23)

---

## [0.6.2] - 2026-05-21

### 修复

- 延迟自我进化消息（background review）到卡片完成后再发送，避免流式卡片被新消息打断。需要重新安装插件：
  ```bash
  HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
  $HERMES_PYTHON -m pip install -e .
  $HERMES_PYTHON -m hermes_lark_streaming uninstall
  $HERMES_PYTHON -m hermes_lark_streaming install
  ```

### 变更

- 线性模式 flush 从 3 步（step1 reasoning/answer → step2 text → step3 tool）合并为 2 步（step1 按 segment 顺序处理所有结构性变更 → step2 text），减少 1 次 API 调用。
- `print_frequency_ms` 从 35 调整为 15，提升打字机渲染流畅度。

### Fixed

- Defer self-evolution messages (background review) until card completion, preventing streaming card from being interrupted by new messages. Requires reinstall:
  ```bash
  HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
  $HERMES_PYTHON -m pip install -e .
  $HERMES_PYTHON -m hermes_lark_streaming uninstall
  $HERMES_PYTHON -m hermes_lark_streaming install
  ```

### Changed

- Linear mode flush merged from 3 steps into 2, process structural changes in segment order, reducing API calls.
- `print_frequency_ms` adjusted from 35 to 15 for smoother typewriter rendering.

---

## [0.6.0] - 2026-05-20

### Highlights

- 线性模式：按事件顺序动态渲染思考、工具调用、回答内容（推理、工具调用不再收纳置顶）
  ![linear](assets/linear.jpg)
- 开启方式：在 `~/.hermes/config.yaml` 中添加：
  ```yaml
  streaming:
    enabled: true
    linear: true
  ```
- 注意：线性模式将在稳定后作为默认模式

### 新增

- `LinearState` 扁平段管理器，按事件顺序管理 reasoning / answer / tool 段。
- `_do_linear_flush` 三步流水线：batch 创建元素 → stream 文本 → batch 更新 tool 面板。
- `build_linear_complete_card` 按段顺序渲染完成态卡片。

### Highlights

- Linear single-card mode: dynamically renders reasoning / answer / tool elements within one CardKit v2.0 card in event arrival order, supporting multi-round conversations with typewriter effect throughout.
  ![linear](assets/linear.jpg)
- Enable by adding to `~/.hermes/config.yaml`:
  ```yaml
  streaming:
    enabled: true
    linear: true
  ```
- Note: Linear mode will become the default mode once stabilized

### Added

- `LinearState` flat segment manager for reasoning / answer / tool segments in event arrival order.
- `_do_linear_flush` three-step pipeline: batch create elements → stream text → batch update tool panels.
- `build_linear_complete_card` renders completion card in segment order.

---

## [0.5.2] - 2026-05-15

### 变更

- 拆分 `cardkit.py`（646 行）为 `cardkit.py` + `cardkit_md.py` + `cardkit_i18n.py`，按职责分离。
- 拆分 `controller.py`（797 行）为 `controller.py` + `controller_mixin.py`，异步卡片操作提取为 `ControllerMixin`。

### 修复

- `show_reasoning` 配置项改为每次访问时重新读取配置文件，支持运行时热更新，无需重启。

### Changed

- Split `cardkit.py` (646 lines) into `cardkit.py` + `cardkit_md.py` + `cardkit_i18n.py` by responsibility.
- Split `controller.py` (797 lines) into `controller.py` + `controller_mixin.py`, extracting async card operations into `ControllerMixin`.

### Fixed

- `show_reasoning` config now re-reads the config file on every access, allowing runtime hot-reload without restart.

---

## [0.5.0] - 2026-05-15

### Highlights

- 原生推理流式展示：实时展示模型原生推理过程，打字机效果逐字输出。
  ![reasoning](assets/reasoning.jpg)

  开启方式（二选一）：
  - 在 `~/.hermes/config.yaml` 中配置 `display.platforms.feishu.show_reasoning: true`
  - 在对话中发送 `/reasoning on` 即可开启

### 新增

- 新增第 8 个 hook `on_reasoning_delta`，注入 `agent.reasoning_callback`，接收模型原生推理增量。
- `Config.show_reasoning` 配置项，支持平台级（`display.platforms.feishu.show_reasoning`）和全局（`display.show_reasoning`）两级配置。
- `build_streaming_card_v2` 新增 `show_reasoning` 参数，启用时预置空 reasoning 面板。
- reasoning 面板标题：空内容时显示"Thinking/思考中"，有内容后切换为"Thought/思考"。

### 变更

- 统一三处卡片元素顺序为 reasoning → tool → answer。
- `_build_reasoning_panel` 新增 `expanded`、`element_id` 参数，标题改为 `plain_text` + `text_color: grey` + `text_size: notation`，与工具面板风格一致。
- IM fallback 路径 reasoning 展示条件从 `if reasoning_text and not text` 改为 `if reasoning_text`，始终展示推理内容。

### Highlights

- Native reasoning streaming: display model's native reasoning process in real-time with typewriter effect.
  ![reasoning](assets/reasoning.jpg)

  Enable (either option):
  - Set `display.platforms.feishu.show_reasoning: true` in `~/.hermes/config.yaml`
  - Send `/reasoning on` in the conversation to enable

### Added

- Add 8th hook `on_reasoning_delta` that injects `agent.reasoning_callback` to receive native reasoning deltas.
- `Config.show_reasoning` property with platform-level (`display.platforms.feishu.show_reasoning`) and global (`display.show_reasoning`) fallback.
- `build_streaming_card_v2` gains `show_reasoning` param — when enabled, pre-adds an empty reasoning panel.
- Reasoning panel title shows "Thinking" when empty, switches to "Thought" when content arrives.

### Changed

- Unify element order to reasoning → tool → answer across all card builders.
- `_build_reasoning_panel` gains `expanded` and `element_id` params; title changed to `plain_text` + `text_color: grey` + `text_size: notation` to match tool panel style.
- IM fallback reasoning display condition changed from `if reasoning_text and not text` to `if reasoning_text` — always show reasoning content.

---

## [0.4.5] - 2026-05-12

### 修复

- 修复 `_do_update_card` 在流式模式关闭后仍调用 `cardkit_stream_element`，产生大量 300309 错误刷屏日志的问题。修复 #7. (#9) 感谢 @Mxin-9527.
- 修复 `_do_complete` 重试全失败后会话状态被错误设为 `COMPLETED`，应为 `FAILED`。修复 #7. (#9)
- 修复 markdown 表格降级未应用到所有渲染路径的问题。 (#8) 感谢 @Bandersnatch0x.

### 变更

- 新增 ruff lint/format 和 mypy 类型检查，统一代码风格。 (#6)

### Fixes

- Fix `_do_update_card` calling `cardkit_stream_element` after streaming mode closed, causing excessive 300309 errors in logs. Fixes #7. (#9) Thanks @Mxin-9527.
- Fix `_do_complete` incorrectly setting session state to `COMPLETED` after all retries failed — should be `FAILED`. Fixes #7. (#9)
- Fix markdown table downgrade not applied to all render paths. (#8) Thanks @Bandersnatch0x.

### Changed

- Add ruff lint/format and mypy type checking for consistent code style. (#6)

---

## [0.4.3] - 2026-05-12

### 修复

- 修复 `message_id` 为 `None` 时 `on_message_started` 崩溃（`TypeError: 'NoneType' object is not subscriptable`），导致后续所有流式卡片失效直到重启。修复 #4. (#5) 感谢 @gitteeee.
- 修复 `_prune_stale_sessions` 遇到 `None` 键时崩溃的问题。修复 #4. (#5)

### Fixed

- Fix `on_message_started` crash when `message_id` is `None` (`TypeError: 'NoneType' object is not subscriptable`), which broke all subsequent streaming cards until gateway restart. Fixes #4. (#5) Thanks @gitteeee.
- Fix `_prune_stale_sessions` crash when encountering `None` keys in session map. Fixes #4. (#5)

---

## [0.4.2] - 2026-05-11

### 变更

- 优化流式卡片打字机渲染频率（35ms/字），减少文字积压导致的突然上屏。
- 清理流式卡片构建函数中的冗余代码。

### Changes

- Optimized streaming card typewriter rendering frequency (35ms/char) to reduce sudden text appearance.
- Cleaned up redundant code in streaming card builder.

## [0.4.1] - 2026-05-11

### 变更

- Footer 默认布局改为单行紧凑模式（`[[status, elapsed, context, model]]`），`show_label` 默认改为 `false`。
- 重构 README 章节，调整顺序并合并降级策略到工作原理，新增更新章节。

### 新增

- 新增 GitHub Actions release workflow，推送 `v*` tag 时自动从 CHANGELOG.md 提取内容创建 release。

### Changed

- Footer default layout changed to single-row compact mode (`[[status, elapsed, context, model]]`), `show_label` default changed to `false`.
- Restructured README sections, merged degradation strategy into How It Works, added Update section.

### Added

- Add GitHub Actions release workflow that auto-creates releases from CHANGELOG.md on `v*` tag push.

---

## [0.4.0] - 2026-05-10

### 重要修复

- 修复工具调用后卡片输出丢失流式效果：长空闲恢复时节流定时器被反复重设，导致文字累积但从未推送到卡片。
- 修复完成态卡片丢失工具调用前的文字：多轮对话中完成态只保留了最后一轮内容，工具调用前的文字被丢弃。

### 新增

- 新增 `update_card`、`tool_update`、`do_complete` 的 info 级别日志，方便排查流式输出问题。

### Fixes

- Fix card losing streaming effect after tool calls: throttle timer was endlessly rescheduled during long-gap recovery, causing text to accumulate but never flush to the card.
- Fix completion card losing text before tool calls: in multi-turn conversations, only the last turn's content was kept, discarding earlier text.

### Added

- Add info-level logs for `update_card`, `tool_update`, and `do_complete` events to aid streaming output debugging.

---

## [0.3.0] - 2026-05-09

### Highlights

- 消息打断处理：用户发送新消息可中断正在生成的回复，支持嵌套中断（A→B→C）
  ![interrupt](assets/interrupt.jpg)

### 新增

- 新增第 7 个 hook `on_message_interrupted`，处理用户发送新消息打断正在处理的回复。
- `_interrupt_map` 机制：中断时映射旧消息 ID → 新消息 ID，`on_completed` 通过重定向将旧消息的完成结果传递给新会话。
- 支持嵌套中断（A→B→C），自动更新映射链。

### 修复

- 修复消息打断时旧卡片未终止、新卡片未创建的问题。

### Highlights

- Message interrupt handling: send a new message to interrupt the ongoing reply, with nested interrupt support (A→B→C)
  ![interrupt](assets/interrupt.jpg)

### Added

- Add 7th hook `on_message_interrupted` for handling message interrupts when user sends a new message while agent is still processing.
- `_interrupt_map` mechanism: maps old message ID → new message ID on interrupt, `on_completed` redirects the old message's completion to the new session.
- Support nested interrupts (A→B→C) with automatic chain update.

### Fixed

- Fix old card not terminated and new card not created on message interrupt.

---

## [0.2.0] - 2026-05-09

### 新增

- 使用 CardKit batch_update API 延迟渲染工具面板，首次工具调用时插入，后续事件仅局部更新，避免重建整个卡片。
- 新增 patcher 测试，基于 Hermes 环境中的真实 run.py 执行注入/移除/幂等性/备份恢复测试。

### 修复

- 修复 `tool_panel_added` 标志在 API 调用前被设置，导致失败后无法正确重试的问题。
- 修复模型在同次响应中先输出文本再调用工具时，工具面板不更新的问题。
- 修复卡片创建失败时未正确让出给 gateway 默认回复的问题。

### Added

- Use CardKit batch_update API to lazy-render tool panel — insert on first tool event, then update element locally, avoiding full card rebuilds.
- Add patcher tests using real run.py from Hermes environment for inject/remove/idempotency/backup-restore coverage.

### Fixed

- Fix `tool_panel_added` flag being set before API call, preventing correct retry on failure.
- Fix tool panel not updating when model outputs text before tool calls in the same streaming response.
- Fix card creation failure not yielding to gateway default reply.

---

## [0.1.1] - 2026-05-08

### 新增

- 新增 `AGENTS.md`，包含架构概览与开发指南。

### 变更

- 精简 `optimize_markdown_style`，移除不必要的 `<br>` 间距逻辑（连续标题、表格、代码块前后填充）。空行压缩已足够适配 CardKit 渲染。
- 移除 5 个模块中的冗余代码。

### Added

- Add `AGENTS.md` with architecture overview and development guide.

### Changed

- Simplify `optimize_markdown_style` by removing unnecessary `<br>` spacing logic (consecutive headers, tables, code-block padding). Blank-line compression is sufficient for CardKit rendering.
- Remove redundant code across 5 modules.

---

## [0.1.0] - 2026-05-08

### 新增

- `hermes-lark-streaming` 初始版本 — 基于飞书 CardKit v2.0 的 Hermes Gateway 实时流式卡片插件。
- 通过 CardKit `streaming_mode` 实现打字机效果的流式输出。
- 展示推理/思考内容。
- 实时工具调用状态追踪，含图标、结果块和错误块。
- CardKit 流式失败或频控时自动降级到 IM PATCH。
- 完成态卡片，页脚展示元数据（耗时、模型、token 用量、上下文窗口）。
- `UnavailableGuard` — 源消息被删除或撤回时自动终止后续更新。
- `ImageResolver` — 异步识别 markdown 图片 URL，下载并上传为飞书 `img_key`。
- AST 注入 6 个 hook 到 `gateway/run.py`（`on_message_started`、`on_answer_delta`、`on_thinking_delta`、`on_tool_updated`、`on_message_completed`、`on_message_aborted`）。
- CLI 命令：`install`、`uninstall`、`verify`、`status`、`restore`。

### 变更

- 在 README 中明确说明插件必须安装到 Hermes 自身的 Python 虚拟环境中（`~/.hermes/hermes-agent/venv/bin/python3`），而非系统 Python。避免 gateway 启动后因找不到插件而失败。

### 修复

- 移除 `strip_reasoning_tags()` 末尾的 `.strip()`，保留换行符以支持 CardKit 流式渲染。Markdown 格式（加粗、代码块、表格、列表）现在在流式阶段即可正确渲染，不再仅在全量更新后正常显示。

### Added

- Initial release of `hermes-lark-streaming` — a real-time streaming card plugin for Hermes Gateway via Feishu/Lark CardKit v2.0.
- Streaming output with typewriter effect via CardKit `streaming_mode`.
- Display reasoning/thinking content.
- Live tool-use status tracking with icons, result blocks, and error blocks.
- Auto fallback from CardKit streaming to IM PATCH on creation failure or rate limiting.
- Completion card with footer metadata (duration, model, tokens, context usage).
- `UnavailableGuard` — auto-terminates updates when the source message is deleted or recalled.
- `ImageResolver` — asynchronously detects markdown image URLs, downloads, uploads to Feishu, and replaces with `img_key`.
- AST injection of 6 hooks into `gateway/run.py` (`on_message_started`, `on_answer_delta`, `on_thinking_delta`, `on_tool_updated`, `on_message_completed`, `on_message_aborted`).
- CLI commands: `install`, `uninstall`, `verify`, `status`, `restore`.

### Changed

- Clarify in README that the plugin must be installed into Hermes's own Python venv (`~/.hermes/hermes-agent/venv/bin/python3`), not the system Python. This prevents the gateway from failing to load the plugin at runtime.

### Fixed

- Remove trailing `.strip()` in `strip_reasoning_tags()` to preserve newlines for CardKit streaming. Markdown formatting (bold, code blocks, tables, lists) now renders correctly during the streaming phase, not just after completion.
