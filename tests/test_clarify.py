"""Unit tests for the clarify-as-card module (v2 CardKit format).

Pure unit tests — no Hermes runtime required.  ``tools.clarify_gateway`` is
stubbed via ``sys.modules`` because it is a lazy in-function import that only
resolves inside the Hermes process.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

from hermes_lark_streaming import clarify


# ---------------------------------------------------------------------------
# Fake clarify_gateway module (lazy import target)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_clarify_gateway(monkeypatch):
    mod = types.ModuleType("tools.clarify_gateway")
    mod.resolve_gateway_clarify = lambda cid, resp: setattr(mod, "_last_resolve", (cid, resp)) or True
    mod.mark_awaiting_text = lambda cid: setattr(mod, "_last_mark", cid) or True
    # tools package must exist too
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []  # mark as package
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", mod)
    return mod


def _elements(card: dict) -> list[dict]:
    """Helper: extract body.elements from a v2 card."""
    return card["body"]["elements"]


# ---------------------------------------------------------------------------
# build_clarify_card
# ---------------------------------------------------------------------------
class TestBuildClarifyCard:
    def test_card_is_v2_schema(self) -> None:
        card = clarify.build_clarify_card(
            question="q", choices=["a"], clarify_id="x"
        )
        assert card["schema"] == "2.0"

    def test_multiple_choice_buttons(self) -> None:
        card = clarify.build_clarify_card(
            question="Which deployment target?",
            choices=["staging", "prod"],
            clarify_id="abc123",
        )
        assert card["header"]["template"] == "orange"
        elems = _elements(card)
        # first element is the question markdown
        assert elems[0] == {
            "tag": "markdown",
            "content": "Which deployment target?",
        }
        # next: 2 choice buttons (directly in elements, no tag:action wrapper)
        btns = [e for e in elems if e["tag"] == "button"]
        assert len(btns) == 2
        # first choice is primary
        assert btns[0]["type"] == "primary"
        assert btns[1]["type"] == "default"
        # values carry clarify_id + choice
        assert btns[0]["value"] == {
            "hermes_clarify": "choose",
            "clarify_id": "abc123",
            "choice": "staging",
        }

    def test_first_choice_primary_rest_default(self) -> None:
        card = clarify.build_clarify_card(
            question="q", choices=["a", "b", "c"], clarify_id="x"
        )
        btns = [e for e in _elements(card) if e["tag"] == "button"]
        assert [b["type"] for b in btns[:3]] == ["primary", "default", "default"]

    def test_more_than_four_choices_truncated(self) -> None:
        card = clarify.build_clarify_card(
            question="q", choices=["a", "b", "c", "d", "e", "f"], clarify_id="x"
        )
        btns = [e for e in _elements(card) if e["tag"] == "button"]
        # only 4 choice buttons (5th/6th dropped), no "Other" button
        assert len(btns) == 4
        choice_labels = [b["value"]["choice"] for b in btns]
        assert choice_labels == ["a", "b", "c", "d"]

    def test_input_element_with_behaviors(self) -> None:
        card = clarify.build_clarify_card(
            question="q", choices=["a"], clarify_id="cid"
        )
        inputs = [e for e in _elements(card) if e["tag"] == "input"]
        assert len(inputs) == 1
        inp = inputs[0]
        assert inp["name"] == "clarify_text"
        assert inp["behaviors"][0]["type"] == "callback"
        assert inp["behaviors"][0]["value"]["hermes_clarify"] == "other_submit"
        assert inp["behaviors"][0]["value"]["clarify_id"] == "cid"

    def test_no_tag_action_wrapper(self) -> None:
        """v2 must not use tag:action (im API rejects it)."""
        card = clarify.build_clarify_card(
            question="q", choices=["a"], clarify_id="x"
        )
        assert not any(e.get("tag") == "action" for e in _elements(card))

    def test_open_ended_has_no_buttons_but_has_input(self) -> None:
        card = clarify.build_clarify_card(question="What now?", choices=None, clarify_id="x")
        elems = _elements(card)
        # markdown + input, no buttons
        assert elems[0]["tag"] == "markdown"
        btns = [e for e in elems if e["tag"] == "button"]
        assert len(btns) == 0
        inputs = [e for e in elems if e["tag"] == "input"]
        assert len(inputs) == 1

    def test_empty_choices_list_treated_as_open_ended(self) -> None:
        card = clarify.build_clarify_card(question="q", choices=[], clarify_id="x")
        btns = [e for e in _elements(card) if e["tag"] == "button"]
        assert len(btns) == 0

    def test_empty_question_falls_back(self) -> None:
        card = clarify.build_clarify_card(question="", choices=["a"], clarify_id="x")
        assert _elements(card)[0]["content"] == "请选择"

    def test_blank_choices_skipped(self) -> None:
        card = clarify.build_clarify_card(question="q", choices=["a", "", "  ", "b"], clarify_id="x")
        btns = [e for e in _elements(card) if e["tag"] == "button"]
        assert len(btns) == 2
        assert [b["value"]["choice"] for b in btns] == ["a", "b"]

    def test_card_is_json_serialisable(self) -> None:
        card = clarify.build_clarify_card(question="q", choices=["a"], clarify_id="x")
        json.loads(json.dumps(card, ensure_ascii=False))


# ---------------------------------------------------------------------------
# build_clarify_resolved_card
# ---------------------------------------------------------------------------
class TestBuildClarifyResolvedCard:
    def test_chosen_is_green(self) -> None:
        card = clarify.build_clarify_resolved_card(question="q?", chosen="prod")
        assert card["header"]["template"] == "green"
        assert "prod" in _elements(card)[0]["content"]
        assert "已选择" in card["header"]["title"]["content"]

    def test_awaiting_text_is_blue(self) -> None:
        card = clarify.build_clarify_resolved_card(question="q?", awaiting_text=True)
        assert card["header"]["template"] == "blue"
        assert "输入" in _elements(card)[0]["content"]


# ---------------------------------------------------------------------------
# handle_clarify_card_action
# ---------------------------------------------------------------------------
def _fake_adapter(authorized: bool = True) -> SimpleNamespace:
    def _submit_on_loop(loop, coro):
        asyncio.run(coro)
        return True
    return SimpleNamespace(
        _is_interactive_operator_authorized=lambda open_id: authorized,
        _submit_on_loop=_submit_on_loop,
    )


def _fake_event(open_id: str = "ou_test", chat_id: str = "oc_test") -> SimpleNamespace:
    return SimpleNamespace(
        operator=SimpleNamespace(open_id=open_id),
        context=SimpleNamespace(open_chat_id=chat_id),
    )


class TestHandleClarifyCardAction:
    def test_choose_resolves(self, fake_clarify_gateway) -> None:
        adapter = _fake_adapter()
        value = {
            "hermes_clarify": "choose",
            "clarify_id": "c1",
            "choice": "staging",
        }
        resp = clarify.handle_clarify_card_action(
            adapter, event=_fake_event(), action_value=value, loop=None
        )
        # resolve_gateway_clarify was called with the choice
        assert fake_clarify_gateway._last_resolve == ("c1", "staging")
        # a response object is returned
        assert resp is not None

    def test_resolved_card_includes_question(self, fake_clarify_gateway) -> None:
        """Question stored in send_clarify_card is passed to resolved card."""
        clarify._clarify_questions["c_q1"] = "Which deploy target?"
        adapter = _fake_adapter()
        value = {
            "hermes_clarify": "choose",
            "clarify_id": "c_q1",
            "choice": "prod",
        }
        resp = clarify.handle_clarify_card_action(
            adapter, event=_fake_event(), action_value=value, loop=None
        )
        assert resp is not None
        # Check the resolved card data includes the question
        card_data = getattr(getattr(resp, "card", None), "data", None)
        if card_data:
            elements = card_data.get("body", {}).get("elements", [])
            if elements:
                assert "Which deploy target?" in elements[0].get("content", "")
        # Question was popped from the store
        assert "c_q1" not in clarify._clarify_questions

    def test_other_submit_extracts_input_value(self, fake_clarify_gateway) -> None:
        """input's behaviors callback: action.value has hermes_clarify,
        action.input_value has the user's text."""
        adapter = _fake_adapter()
        value = {
            "hermes_clarify": "other_submit",
            "clarify_id": "c2",
        }
        event = _fake_event()
        event.action = SimpleNamespace(input_value="custom answer")
        clarify.handle_clarify_card_action(
            adapter, event=event, action_value=value, loop=None
        )
        assert fake_clarify_gateway._last_resolve == ("c2", "custom answer")

    def test_missing_clarify_id_dropped(self, fake_clarify_gateway) -> None:
        adapter = _fake_adapter()
        value = {"hermes_clarify": "choose", "choice": "x"}
        clarify.handle_clarify_card_action(
            adapter, event=_fake_event(), action_value=value, loop=None
        )
        assert not hasattr(fake_clarify_gateway, "_last_resolve")

    def test_choose_without_choice_dropped(self, fake_clarify_gateway) -> None:
        adapter = _fake_adapter()
        value = {"hermes_clarify": "choose", "clarify_id": "c3"}
        clarify.handle_clarify_card_action(
            adapter, event=_fake_event(), action_value=value, loop=None
        )
        assert not hasattr(fake_clarify_gateway, "_last_resolve")

    def test_unauthorized_operator_dropped(self, fake_clarify_gateway, monkeypatch) -> None:
        monkeypatch.delenv("FEISHU_ALLOW_ALL_USERS", raising=False)
        adapter = _fake_adapter(authorized=False)
        value = {
            "hermes_clarify": "choose",
            "clarify_id": "c4",
            "choice": "prod",
        }
        clarify.handle_clarify_card_action(
            adapter, event=_fake_event("ou_evil"), action_value=value, loop=None
        )
        assert not hasattr(fake_clarify_gateway, "_last_resolve")

    def test_allow_all_users_bypasses_auth(self, fake_clarify_gateway, monkeypatch) -> None:
        """FEISHU_ALLOW_ALL_USERS=true lets non-admin users click clarify."""
        monkeypatch.setenv("FEISHU_ALLOW_ALL_USERS", "true")
        adapter = _fake_adapter(authorized=False)
        value = {
            "hermes_clarify": "choose",
            "clarify_id": "c5",
            "choice": "prod",
        }
        clarify.handle_clarify_card_action(
            adapter, event=_fake_event("ou_guest"), action_value=value, loop=None
        )
        assert hasattr(fake_clarify_gateway, "_last_resolve")

    def test_unknown_action_dropped(self, fake_clarify_gateway) -> None:
        adapter = _fake_adapter()
        value = {"hermes_clarify": "bogus", "clarify_id": "c5"}
        clarify.handle_clarify_card_action(
            adapter, event=_fake_event(), action_value=value, loop=None
        )
        assert not hasattr(fake_clarify_gateway, "_last_resolve")
        assert not hasattr(fake_clarify_gateway, "_last_mark")

    def test_non_dict_action_value_safe(self, fake_clarify_gateway) -> None:
        # a malformed callback must not crash the handler
        resp = clarify.handle_clarify_card_action(
            _fake_adapter(), event=_fake_event(), action_value=None, loop=None
        )
        assert resp is not None


# ---------------------------------------------------------------------------
# send_clarify_card
# ---------------------------------------------------------------------------
class TestSendClarifyCard:
    def test_falls_back_to_im_api_when_no_controller(self) -> None:
        """Without a CardKit controller, falls back to im API (input won't render
        but the card is still sendable)."""
        calls = {}

        class _FakeAdapter:
            async def _feishu_send_with_retry(self, **kw):
                calls["send"] = kw
                return "RAW_RESPONSE"

            def _finalize_send_result(self, response, msg):
                calls["finalize"] = (response, msg)
                return SimpleNamespace(success=True, message_id="om_123")

        async def _run():
            return await clarify.send_clarify_card(
                _FakeAdapter(),
                chat_id="oc_chat",
                question="Which?",
                choices=["a", "b"],
                clarify_id="cid",
                session_key="sk",
                metadata={"m": 1},
            )

        result = asyncio.run(_run())
        assert result.success is True
        assert result.message_id == "om_123"
        sent = calls["send"]
        assert sent["msg_type"] == "interactive"
        assert sent["chat_id"] == "oc_chat"
        assert sent["metadata"] == {"m": 1}
        # payload is a json string of the v2 clarify card
        card = json.loads(sent["payload"])
        assert card["schema"] == "2.0"
        assert card["header"]["template"] == "orange"
        assert not any(e.get("tag") == "action" for e in card["body"]["elements"])
        assert calls["finalize"] == ("RAW_RESPONSE", "send_clarify_card failed")
