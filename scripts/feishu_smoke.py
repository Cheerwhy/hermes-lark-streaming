"""真实 CardKit 出站验收；只发送固定测试内容，不调用模型或执行工具."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import tempfile
import uuid
from pathlib import Path

from hermes_lark_streaming.cardkit.builder import build_static_card, build_streaming_card_v2
from hermes_lark_streaming.config import DEFAULT_DOMAIN, LARK_DOMAIN, Config
from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.feishu import FeishuAPIError, FeishuClient, FeishuClientConfig

SCENARIOS = ("stream", "sequence", "split", "stop", "static")
FRAME_DELAY = 0.4


async def run_scenario(
    cards: StreamCardController, chat_id: str, reply_to: str | None, scenario: str,
) -> str:
    """每个场景使用独立回合；返回消息 ID，供人工检查最终展示."""
    client = cards._client
    assert client is not None
    title = f"[插件验收 · {scenario}]"
    if scenario == "static":
        message_id = await client.send_card_to_chat(
            chat_id, build_static_card(f"{title} 静态卡片"), reply_to_message_id=reply_to,
        )
        await client.update_card_message(message_id, build_static_card(f"{title} 静态更新完成 ✅"))
        return message_id

    key = uuid.uuid4().hex

    async def frame(text: str, *, finalize: bool = False) -> None:
        if not await cards.frame(key, chat_id, text, reply_to=reply_to, source_id=key, finalize=finalize):
            raise RuntimeError(f"{scenario}: card delivery failed")

    await frame("")
    session = cards.turns[key].session
    if scenario == "sequence":
        # 仅在本脚本新建的测试卡上推进服务端序号，验证本地下一次更新能从冲突恢复。
        await client.cardkit_update(session.card_id or "", build_streaming_card_v2(), session.sequence + 1)

    async def flush() -> None:
        await session.flush.flush_now(lambda: cards._do_flush(session))
        if session.state.is_terminal or session.segment_state.has_dirty:
            raise RuntimeError(f"{scenario}: card flush failed")

    await frame(f"{title} 正在流式输出…")
    await flush()
    if scenario == "sequence" and session.sequence < 3:
        raise RuntimeError("sequence: conflict recovery did not advance the local sequence")
    await asyncio.sleep(FRAME_DELAY)
    for index in range(40 if scenario == "split" else 1):
        payload = {"tool_name": "web_search", "tool_call_id": str(index), "args": {"query": "仅展示 / 未执行工具"}}
        await cards.observe(key, "tool_start", payload)
        await cards.observe(key, "tool_end", {**payload, "duration_ms": 100})
    await flush()
    if scenario == "split" and session.split_index == 0:
        raise RuntimeError("split: expected an additional card")

    if scenario == "stop":
        await cards.close_source(key)
        if key in cards.turns or not cards._finished.get(key, (False, False))[0]:
            raise RuntimeError("stop: card completion failed")
    else:
        await frame(f"{title} 流式、工具状态及收尾完成 ✅", finalize=True)
    return session.card_msg_id or ""


async def run(args: argparse.Namespace, app_id: str, app_secret: str) -> None:
    domain = LARK_DOMAIN if args.domain == "lark" else DEFAULT_DOMAIN
    # 隔离配置，避免本机外观/显示开关影响验收；不改用户配置，也不启动 Gateway。
    with tempfile.TemporaryDirectory(prefix="lark-smoke-") as directory:
        client = FeishuClient(FeishuClientConfig(app_id, app_secret, domain))
        cards = StreamCardController(Config(Path(directory)), client)
        try:
            for scenario in SCENARIOS if args.scenario == "all" else (args.scenario,):
                print(f"RUN {scenario}", flush=True)
                message_id = await asyncio.wait_for(run_scenario(cards, args.chat_id, args.reply_to, scenario), 60)
                print(f"PASS {scenario}: {message_id}")
        finally:
            await asyncio.wait_for(cards.close(), 15)
    print("请在飞书检查打字机效果、工具终态、拆卡、停止状态; 传入 --reply-to 时同时检查引用")


def main() -> int:
    parser = argparse.ArgumentParser(description="向指定测试聊天发送真实验收卡片; 不调用模型、不执行工具、不修改配置")
    parser.add_argument(
        "--chat-id", default=os.environ.get("FEISHU_E2E_CHAT_ID"), help="测试聊天 ID (或 FEISHU_E2E_CHAT_ID)",
    )
    parser.add_argument("--reply-to", help="可选: 同一测试聊天内的消息 ID / 用于验收引用回复")
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="stream")
    parser.add_argument("--domain", choices=("feishu", "lark"), default="feishu")
    args = parser.parse_args()
    app_id = os.environ.get("FEISHU_E2E_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_E2E_APP_SECRET", "").strip()
    if not args.chat_id or not app_id or not app_secret:
        parser.error("请设置 FEISHU_E2E_APP_ID、FEISHU_E2E_APP_SECRET / 并指定 --chat-id 或 FEISHU_E2E_CHAT_ID")
    # SDK/底层 HTTP 日志可能含鉴权信息；命令行仅输出场景结果或 API 错误码。
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(args, app_id, app_secret))
    except FeishuAPIError as exc:
        print(f"FAIL Feishu API code={exc.code}")
        return 1
    except (Exception, KeyboardInterrupt) as exc:
        print(f"FAIL {type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
