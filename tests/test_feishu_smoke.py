"""验收脚本本身用 mock 验证；测试期间绝不连接真实飞书."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from hermes_lark_streaming.config import Config
from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.feishu import FeishuAPIError, FeishuClient


@pytest.fixture
def smoke(monkeypatch):
    spec = importlib.util.spec_from_file_location("feishu_smoke", Path(__file__).parents[1] / "scripts/feishu_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "FRAME_DELAY", 0)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["stream", "sequence", "split", "stop", "static"])
@pytest.mark.parametrize("reply_to", [None, "quoted-message"])
async def test_smoke_scenarios_keep_target_and_close_cards(smoke, tmp_path, scenario, reply_to):
    client = create_autospec(FeishuClient, instance=True)
    client.cardkit_create.side_effect = ["card-a", "card-b", "card-c"]
    client.reply_card_by_id.return_value = "reply-message"
    client.send_card_to_chat.return_value = "direct-message"
    if scenario == "sequence":
        client.cardkit_batch_update.side_effect = [FeishuAPIError("sequence conflict", code=300317), None, None]
    cards = StreamCardController(Config(tmp_path), client)
    try:
        message_id = await smoke.run_scenario(cards, "explicit-chat", reply_to, scenario)
        assert message_id == ("reply-message" if reply_to and scenario != "static" else "direct-message")
        assert not cards.turns
        if scenario == "static":
            client.send_card_to_chat.assert_awaited_once()
            assert client.send_card_to_chat.call_args.args[0] == "explicit-chat"
            assert client.send_card_to_chat.call_args.kwargs["reply_to_message_id"] == reply_to
            client.update_card_message.assert_awaited_once()
        elif reply_to:
            assert {c.args[0] for c in client.reply_card_by_id.call_args_list} == {reply_to}
            client.send_card_to_chat.assert_not_called()
        else:
            assert {c.kwargs["chat_id"] for c in client.send_card_to_chat.call_args_list} == {"explicit-chat"}
            client.reply_card_by_id.assert_not_called()
        if scenario == "split":
            assert client.cardkit_create.call_count == 2
    finally:
        await cards.close()


def test_smoke_requires_explicit_test_credentials_before_client_creation(smoke, monkeypatch):
    for name in ("FEISHU_E2E_APP_ID", "FEISHU_E2E_APP_SECRET", "FEISHU_E2E_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "argv", ["feishu_smoke.py"])
    client = create_autospec(FeishuClient)
    monkeypatch.setattr(smoke, "FeishuClient", client)
    with pytest.raises(SystemExit) as exc:
        smoke.main()
    assert exc.value.code == 2
    client.assert_not_called()


@pytest.mark.asyncio
async def test_smoke_does_not_report_success_after_failed_card(smoke, tmp_path):
    client = create_autospec(FeishuClient, instance=True)
    client.cardkit_create.side_effect = FeishuAPIError("permission denied", code=99991672)
    cards = StreamCardController(Config(tmp_path), client)
    with pytest.raises(RuntimeError, match="card delivery failed"):
        await smoke.run_scenario(cards, "explicit-chat", None, "stream")
    assert not cards.turns


@pytest.mark.asyncio
async def test_smoke_stop_does_not_report_success_after_failed_completion(smoke, tmp_path):
    client = create_autospec(FeishuClient, instance=True)
    client.cardkit_create.return_value = "card"
    client.send_card_to_chat.return_value = "message"
    client.cardkit_close_streaming.side_effect = FeishuAPIError("sequence conflict", code=300317)
    cards = StreamCardController(Config(tmp_path), client)
    with pytest.raises(RuntimeError, match="card completion failed"):
        await smoke.run_scenario(cards, "explicit-chat", None, "stop")
    assert not cards.turns
