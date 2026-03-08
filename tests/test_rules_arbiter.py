"""
Tests for src/agents/rules_arbiter.py.

All Claude API calls are mocked — no real API calls are made.
"""

import json
import types
import unittest
from unittest.mock import MagicMock, patch

from src.agents.rules_arbiter import (
    _parse_verdict,
    extract_rule_text,
    validate_action,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _mock_client(response_text: str) -> MagicMock:
    """Return a mock anthropic.Anthropic client that yields `response_text`."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = response_text

    final_message = MagicMock()
    final_message.content = [text_block]

    stream_ctx = MagicMock()
    stream_ctx.__enter__ = MagicMock(return_value=stream_ctx)
    stream_ctx.__exit__ = MagicMock(return_value=False)
    stream_ctx.get_final_message = MagicMock(return_value=final_message)

    client = MagicMock()
    client.messages.stream.return_value = stream_ctx
    return client


def _valid_move_action() -> dict:
    return {
        "action": "move",
        "unit_id": "cw-7th-armoured-001",
        "path": ["A0101", "A0102", "A0103"],
    }


def _valid_move_context() -> dict:
    return {
        "unit": {
            "id": "cw-7th-armoured-001",
            "name": "7th Armoured Div",
            "cpa": 30,
            "cp_remaining": 30,
            "side": "commonwealth",
            "hex_id": "A0101",
            "supply_status": "in_supply",
            "breakdown_points": 0,
            "is_motorized": True,
            "zoc_status": "none",
        },
        "path": ["A0101", "A0102", "A0103"],
        "path_hex_costs": {"A0102": 4, "A0103": 4},
        "total_cp_cost": 8,
        "zoc_hexes": [],
        "enemy_occupied_hexes": [],
        "stacking_in_destination": 3,
        "stacking_limit": 20,
        "weather": "normal",
        "context": "voluntary",
    }


# ── _parse_verdict ─────────────────────────────────────────────────────────────

class TestParseVerdict(unittest.TestCase):
    def test_direct_valid_json(self):
        result = _parse_verdict('{"valid": true}')
        self.assertTrue(result["valid"])

    def test_direct_invalid_json(self):
        result = _parse_verdict('{"valid": false, "reason": "ZOC", "rule_ref": "8.14"}')
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_ref"], "8.14")

    def test_code_block_wrapped(self):
        result = _parse_verdict('```json\n{"valid": true}\n```')
        self.assertTrue(result["valid"])

    def test_gibberish_returns_fallback(self):
        result = _parse_verdict("I cannot validate this.")
        self.assertFalse(result["valid"])

    def test_json_buried_in_text(self):
        result = _parse_verdict(
            'After careful review: {"valid": false, "reason": "exceeds CPA", "rule_ref": "6.13"}'
        )
        self.assertFalse(result["valid"])
        self.assertIn("rule_ref", result)


# ── extract_rule_text ──────────────────────────────────────────────────────────

class TestExtractRuleText(unittest.TestCase):
    def test_known_rule_returns_text(self):
        text = extract_rule_text("8.14")
        # Should find something about ZOC
        self.assertGreater(len(text), 50)

    def test_nonexistent_rule_returns_empty(self):
        text = extract_rule_text("99.99")
        self.assertEqual(text, "")

    def test_ocr_artifacts_collapsed(self):
        text = extract_rule_text("8.15")
        # Should not have excessive spaces from line-break collapsing
        self.assertNotIn("  ", text)


# ── validate_action ────────────────────────────────────────────────────────────

class TestValidateActionMove(unittest.TestCase):
    def test_valid_move_passes(self):
        client = _mock_client('{"valid": true}')
        result = validate_action(_valid_move_action(), _valid_move_context(), client=client)
        self.assertTrue(result["valid"])

    def test_cpa_exceeded_fails(self):
        client = _mock_client(
            '{"valid": false, "reason": "Total CP cost 32 exceeds cp_remaining 30.", "rule_ref": "6.13"}'
        )
        context = _valid_move_context()
        context["total_cp_cost"] = 32  # exceeds cp_remaining=30
        result = validate_action(_valid_move_action(), context, client=client)
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_ref"], "6.13")
        self.assertIn("reason", result)

    def test_zoc_stop_fails(self):
        client = _mock_client(
            '{"valid": false, "reason": "Unit entered enemy ZOC and must stop.", "rule_ref": "8.14"}'
        )
        context = _valid_move_context()
        context["zoc_hexes"] = ["A0102"]  # first step hits ZOC
        # Path continues past A0102 — engine would catch but arbiter also should
        result = validate_action(_valid_move_action(), context, client=client)
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_ref"], "8.14")

    def test_enemy_occupied_hex_fails(self):
        client = _mock_client(
            '{"valid": false, "reason": "Path includes enemy-occupied hex A0102.", "rule_ref": "8.13"}'
        )
        context = _valid_move_context()
        context["enemy_occupied_hexes"] = ["A0102"]
        result = validate_action(_valid_move_action(), context, client=client)
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_ref"], "8.13")

    def test_client_api_error_returns_failure(self):
        import anthropic as anthropic_module
        client = MagicMock()
        client.messages.stream.side_effect = anthropic_module.APIConnectionError(request=MagicMock())
        result = validate_action(_valid_move_action(), _valid_move_context(), client=client)
        self.assertFalse(result["valid"])
        self.assertIn("reason", result)

    def test_api_call_includes_action_and_context(self):
        """The user message passed to the API must contain the action and context."""
        client = _mock_client('{"valid": true}')
        validate_action(_valid_move_action(), _valid_move_context(), client=client)
        call_kwargs = client.messages.stream.call_args.kwargs
        user_msg = call_kwargs["messages"][0]["content"]
        self.assertIn("A0101", user_msg)
        self.assertIn("cp_remaining", user_msg)

    def test_system_prompt_cached(self):
        """System prompt block must have cache_control set."""
        client = _mock_client('{"valid": true}')
        validate_action(_valid_move_action(), _valid_move_context(), client=client)
        call_kwargs = client.messages.stream.call_args.kwargs
        system = call_kwargs["system"]
        self.assertIsInstance(system, list)
        self.assertIn("cache_control", system[0])


class TestValidateActionCombat(unittest.TestCase):
    def _combat_action(self):
        return {"action": "combat", "combat_type": "close_assault", "attacker_id": "cw-001", "defender_id": "ax-001"}

    def _combat_context(self):
        return {
            "attacker": {
                "id": "cw-001", "name": "7th Armoured", "cpa": 30, "cp_remaining": 20,
                "side": "commonwealth", "hex_id": "A0101", "supply_status": "in_supply",
                "is_motorized": True, "zoc_status": "none",
            },
            "defender": {"id": "ax-001", "hex_id": "A0102", "supply_status": "in_supply"},
            "combat_type": "close_assault",
            "attacker_cp_cost": 10,
            "attacker_cp_remaining": 20,
            "adjacent": True,
            "terrain": "open",
            "defender_in_supply": True,
            "weather": "normal",
        }

    def test_valid_combat_passes(self):
        client = _mock_client('{"valid": true}')
        result = validate_action(self._combat_action(), self._combat_context(), client=client)
        self.assertTrue(result["valid"])

    def test_not_adjacent_fails(self):
        client = _mock_client(
            '{"valid": false, "reason": "Attacker not adjacent to defender.", "rule_ref": "15.1"}'
        )
        ctx = self._combat_context()
        ctx["adjacent"] = False
        result = validate_action(self._combat_action(), ctx, client=client)
        self.assertFalse(result["valid"])


class TestValidateActionUnknown(unittest.TestCase):
    def test_unknown_action_type_returns_failure(self):
        client = _mock_client('{"valid": true}')  # mock would return valid but type check fires first
        result = validate_action({"action": "supply"}, {}, client=client)
        self.assertFalse(result["valid"])
        self.assertIn("Unknown action type", result["reason"])
        # Should not call the API
        client.messages.stream.assert_not_called()

    def test_missing_action_key_returns_failure(self):
        client = _mock_client('{"valid": true}')
        result = validate_action({}, {}, client=client)
        self.assertFalse(result["valid"])
        client.messages.stream.assert_not_called()


class TestValidateActionModelConfig(unittest.TestCase):
    def test_uses_opus_model(self):
        client = _mock_client('{"valid": true}')
        validate_action(_valid_move_action(), _valid_move_context(), client=client)
        call_kwargs = client.messages.stream.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "claude-opus-4-6")

    def test_uses_adaptive_thinking(self):
        client = _mock_client('{"valid": true}')
        validate_action(_valid_move_action(), _valid_move_context(), client=client)
        call_kwargs = client.messages.stream.call_args.kwargs
        self.assertEqual(call_kwargs["thinking"], {"type": "adaptive"})


if __name__ == "__main__":
    unittest.main()
