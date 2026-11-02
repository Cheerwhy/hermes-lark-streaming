"""Feishu client transient-error behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_lark_streaming.feishu import FeishuAPIError, FeishuClient


class _Resp:
    def __init__(self, *, ok: bool, code: int = 0, msg: str = "", data: object | None = None) -> None:
        self._ok = ok
        self.code = code
        self.msg = msg
        self.data = data

    def success(self) -> bool:
        return self._ok


def _client_with(**methods: AsyncMock) -> FeishuClient:
    client = FeishuClient.__new__(FeishuClient)
    client._client = SimpleNamespace(  # type: ignore[attr-defined]
        cardkit=SimpleNamespace(
            v1=SimpleNamespace(
                card=SimpleNamespace(
                    acreate=methods.get("card_create", AsyncMock()),
                    aupdate=methods.get("card_update", AsyncMock()),
                    abatch_update=methods.get("batch_update", AsyncMock()),
                    asettings=methods.get("settings", AsyncMock()),
                ),
                card_element=SimpleNamespace(content=methods.get("card_element_content", AsyncMock())),
            ),
        ),
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(
                    acreate=methods.get("create_message", AsyncMock()),
                    areply=methods.get("reply", AsyncMock()),
                    apatch=methods.get("patch", AsyncMock()),
                ),
            ),
        ),
    )
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "message"),
    [
        (2200, "Gateway timeout. Please try again later."),
        (300000, "Server Internal Error"),
    ],
    ids=["gateway-timeout", "server-internal-error"],
)
async def test_cardkit_create_retries_transient_errors_once(code: int, message: str) -> None:
    create = AsyncMock(
        side_effect=[
            _Resp(ok=False, code=code, msg=message),
            _Resp(ok=True, data=SimpleNamespace(card_id="card-ok")),
        ]
    )
    client = _client_with(card_create=create)

    assert await client.cardkit_create({"schema": "2.0"}) == "card-ok"
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_cardkit_batch_update_retries_internal_error() -> None:
    batch_update = AsyncMock(
        side_effect=[
            _Resp(ok=False, code=1663, msg="internal error"),
            _Resp(ok=True),
        ]
    )
    client = _client_with(batch_update=batch_update)

    await client.cardkit_batch_update("card", [{"action": "add"}], sequence=7)

    assert batch_update.await_count == 2
    first_request = batch_update.await_args_list[0].args[0]
    second_request = batch_update.await_args_list[1].args[0]
    assert second_request.card_id == first_request.card_id
    assert second_request.request_body.sequence == first_request.request_body.sequence


@pytest.mark.asyncio
async def test_reply_card_by_id_retries_gateway_timeout_once() -> None:
    reply = AsyncMock(
        side_effect=[
            _Resp(ok=False, code=2200, msg="Gateway timeout. Please try again later."),
            _Resp(ok=True, data=SimpleNamespace(message_id="msg-ok")),
        ]
    )
    client = _client_with(reply=reply)

    assert await client.reply_card_by_id("anchor", "card") == "msg-ok"
    assert reply.await_count == 2
    first_request = reply.await_args_list[0].args[0]
    second_request = reply.await_args_list[1].args[0]
    assert first_request.request_body.uuid
    assert second_request.request_body.uuid == first_request.request_body.uuid


@pytest.mark.asyncio
async def test_send_card_to_chat_reuses_uuid_across_retries() -> None:
    create = AsyncMock(
        side_effect=[
            _Resp(ok=False, code=2200, msg="Gateway timeout. Please try again later."),
            _Resp(ok=True, data=SimpleNamespace(message_id="msg-ok")),
        ]
    )
    client = _client_with(create_message=create)

    assert await client.send_card_to_chat("chat", {"schema": "2.0"}) == "msg-ok"
    assert create.await_count == 2
    first_request = create.await_args_list[0].args[0]
    second_request = create.await_args_list[1].args[0]
    assert first_request.request_body.uuid
    assert second_request.request_body.uuid == first_request.request_body.uuid


@pytest.mark.asyncio
async def test_cardkit_create_does_not_retry_non_transient_error() -> None:
    create = AsyncMock(side_effect=[_Resp(ok=False, code=230099, msg="content failed")])
    client = _client_with(card_create=create)

    with pytest.raises(FeishuAPIError):
        await client.cardkit_create({"schema": "2.0"})

    assert create.await_count == 1


@pytest.mark.asyncio
async def test_card_message_update_uses_patch_api() -> None:
    patch_message = AsyncMock(return_value=_Resp(ok=True))
    client = _client_with(patch=patch_message)
    await client.update_card_message("message", {"schema": "2.0", "body": {"elements": []}})
    request = patch_message.call_args.args[0]
    assert request.message_id == "message"
    assert json.loads(request.request_body.content)["schema"] == "2.0"


@pytest.mark.asyncio
async def test_batch_retry_after_ambiguous_success_keeps_uuid_and_does_not_duplicate(tmp_path, monkeypatch):
    """首请求已执行但响应超时；同序号冲突后仍用同一幂等 ID 恢复."""
    import asyncio

    from hermes_lark_streaming.config import Config
    from hermes_lark_streaming.controller import StreamCardController
    from hermes_lark_streaming.streaming.session import CardSession

    applied = set()
    requests = []

    async def server(request):
        body = request.request_body
        requests.append((body.sequence, body.uuid, body.actions))
        if len(requests) == 1:
            applied.add(body.uuid)
            return _Resp(ok=False, code=2200)
        if len(requests) == 2:
            return _Resp(ok=False, code=300317)
        applied.add(body.uuid)
        return _Resp(ok=True)

    monkeypatch.setattr("hermes_lark_streaming.feishu.asyncio.sleep", AsyncMock())
    client = _client_with(batch_update=AsyncMock(side_effect=server))
    controller = StreamCardController(Config(tmp_path), client)
    session = CardSession("in", "chat", asyncio.get_running_loop())
    actions = [{"type": "add_elements", "params": {"elements": [{"tag": "markdown", "element_id": "a"}]}}]
    await controller._cardkit_call(session, client.cardkit_batch_update, "card", actions)
    assert [r[0] for r in requests] == [2, 2, 3]
    assert len(applied) == 1 and next(iter(applied))
    assert len({r[2] for r in requests}) == 1
    assert session.sequence == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("method,sdk_method,args", [
    ("cardkit_update", "card_update", ("card", {})),
    ("cardkit_close_streaming", "settings", ("card",)),
    ("cardkit_batch_update", "batch_update", ("card", [])),
])
async def test_cardkit_uuid_survives_transient_retries(method, sdk_method, args, monkeypatch):
    monkeypatch.setattr("hermes_lark_streaming.feishu.asyncio.sleep", AsyncMock())
    sdk = AsyncMock(side_effect=[_Resp(ok=False, code=2200), _Resp(ok=True)])
    client = _client_with(**{sdk_method: sdk})
    await getattr(client, method)(*args, sequence=5, request_id="same-logical-operation")
    bodies = [c.args[0].request_body for c in sdk.await_args_list]
    assert [b.sequence for b in bodies] == [5, 5]
    assert [b.uuid for b in bodies] == ["same-logical-operation"] * 2


@pytest.mark.asyncio
async def test_stream_uuid_survives_sync_sdk_transient_retries(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr("hermes_lark_streaming.feishu.asyncio.sleep", AsyncMock())
    sdk = Mock(side_effect=[_Resp(ok=False, code=2200), _Resp(ok=True)])
    client = _client_with(card_element_content=sdk)
    await client.cardkit_stream_element("card", "answer", "text", sequence=5, request_id="stream-operation")
    bodies = [c.args[0].request_body for c in sdk.call_args_list]
    assert [b.sequence for b in bodies] == [5, 5]
    assert [b.uuid for b in bodies] == ["stream-operation"] * 2
