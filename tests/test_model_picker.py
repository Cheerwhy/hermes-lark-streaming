"""Unit tests for the model_picker module — core paths only."""

from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_lark_streaming import model_picker


def _providers() -> list[dict]:
    return [
        {"slug": "openai", "models": [{"id": "gpt-4"}, {"id": "gpt-3.5"}]},
        {"slug": "anthropic", "models": [{"id": "claude-3"}]},
    ]


def _fake_adapter(authorized: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        _is_interactive_operator_authorized=lambda open_id: authorized,
        _hfc_model_picker_state={},
    )


def _fake_event(open_id: str = "ou_test") -> SimpleNamespace:
    return SimpleNamespace(
        operator=SimpleNamespace(open_id=open_id),
        context=SimpleNamespace(open_chat_id="oc_test"),
        action=SimpleNamespace(option="", tag="select_static"),
    )


# ---------------------------------------------------------------------------
# _model_picker_options
# ---------------------------------------------------------------------------
class TestModelPickerOptions:
    def test_normal_providers(self) -> None:
        opts = model_picker._model_picker_options(_providers())
        assert len(opts) == 3
        assert opts[0]["value"]

    def test_max_options_truncation(self) -> None:
        opts = model_picker._model_picker_options(_providers(), max_options=2)
        assert len(opts) == 2

    def test_current_model_marked(self) -> None:
        opts = model_picker._model_picker_options(_providers(), current_model="gpt-4")
        # current model should be first
        assert "gpt-4" in opts[0]["text"]["content"]

    def test_empty_providers(self) -> None:
        assert model_picker._model_picker_options([]) == []


# ---------------------------------------------------------------------------
# build_model_picker_card
# ---------------------------------------------------------------------------
class TestBuildModelPickerCard:
    def test_returns_none_when_no_options(self) -> None:
        assert model_picker.build_model_picker_card(
            providers=[], current_model="", current_provider="", picker_id="pid"
        ) is None

    def test_card_has_select_static(self) -> None:
        card = model_picker.build_model_picker_card(
            providers=_providers(), current_model="", current_provider="", picker_id="pid"
        )
        assert card is not None
        # v1 card: select_static is inside a tag:action element
        actions = [e for e in card["elements"] if e.get("tag") == "action"]
        assert len(actions) == 1
        sel = [a for a in actions[0]["actions"] if a.get("tag") == "select_static"]
        assert len(sel) == 1
        assert len(sel[0]["options"]) == 3

    def test_json_serializable(self) -> None:
        card = model_picker.build_model_picker_card(
            providers=_providers(), current_model="gpt-4", current_provider="openai", picker_id="pid"
        )
        json.loads(json.dumps(card, ensure_ascii=False))


# ---------------------------------------------------------------------------
# build_model_picker_resolved_card
# ---------------------------------------------------------------------------
class TestBuildModelPickerResolvedCard:
    def test_success_green(self) -> None:
        card = model_picker.build_model_picker_resolved_card(result_text="ok")
        assert card["header"]["template"] == "green"
        assert "ok" in card["elements"][0]["content"]

    def test_failure_red(self) -> None:
        card = model_picker.build_model_picker_resolved_card(result_text="err", success=False)
        assert card["header"]["template"] == "red"


# ---------------------------------------------------------------------------
# handle_model_picker_action
# ---------------------------------------------------------------------------
class TestHandleModelPickerAction:
    def test_non_dict_action_value_safe(self) -> None:
        resp = model_picker.handle_model_picker_action(
            _fake_adapter(), event=_fake_event(), action_value=None, loop=None
        )
        assert resp is not None

    def test_unauthorized_dropped(self) -> None:
        adapter = _fake_adapter(authorized=False)
        value = {"hfc_action": "model_picker", "hfc_model_picker_id": "p1"}
        resp = model_picker.handle_model_picker_action(
            adapter, event=_fake_event("ou_evil"), action_value=value, loop=None
        )
        assert resp is not None

    def test_missing_picker_id_dropped(self) -> None:
        value = {"hfc_action": "model_picker"}
        resp = model_picker.handle_model_picker_action(
            _fake_adapter(), event=_fake_event(), action_value=value, loop=None
        )
        assert resp is not None

    def test_valid_choice_no_callback(self) -> None:
        adapter = _fake_adapter()
        adapter._hfc_model_picker_state = {
            "p1": SimpleNamespace(chat_id="oc_test", callback=None)
        }
        event = _fake_event()
        event.action.option = '{"provider":"openai","model":"gpt-4"}'
        value = {"hfc_action": "model_picker", "hfc_model_picker_id": "p1"}
        resp = model_picker.handle_model_picker_action(
            adapter, event=event, action_value=value, loop=None
        )
        assert resp is not None
