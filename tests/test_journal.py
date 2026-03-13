"""
Tests for src/agents/journal.py

All tests are deterministic (no Claude API calls).
The _call_claude() method is monkeypatched to return a fixed stub narrative.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from src.agents.journal import JournalAgent, _PRIORITY_TYPES


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_state(turn: int = 57, date: str = "1941-11-18") -> dict:
    return {
        "turn": turn,
        "current_date": date,
        "weather": "normal",
        "initiative": "commonwealth",
        "scenario": "crusader",
        "units": {
            "cw-7rtk-001": {
                "id": "cw-7rtk-001", "name": "7th RTK", "side": "commonwealth",
                "hex_id": "B0407", "status": "active", "supply_status": "in_supply",
            },
            "cw-4armd-001": {
                "id": "cw-4armd-001", "name": "4th Armoured Bde", "side": "commonwealth",
                "hex_id": "B0508", "status": "active", "supply_status": "out_of_supply",
            },
            "ax-dak-001": {
                "id": "ax-dak-001", "name": "DAK HQ", "side": "axis",
                "hex_id": "B0310", "status": "active", "supply_status": "in_supply",
            },
        },
        "supply_dumps": {},
        "events": [],
    }


def _make_events(turn: int = 57) -> list:
    return [
        {
            "turn": turn, "opstage": 1, "type": "weather_roll",
            "description": "Weather roll 43 (fall): normal weather.",
            "unit_id": None, "hex_from": None, "hex_to": None, "data": {},
        },
        {
            "turn": turn, "opstage": 1, "type": "movement",
            "description": "7th RTK moved B0407→B0408.",
            "unit_id": "cw-7rtk-001", "hex_from": "B0407", "hex_to": "B0408",
            "data": {"cp_cost": 4.0},
        },
        {
            "turn": turn, "opstage": 2, "type": "combat",
            "description": "Close assault: 4th Armoured vs DAK HQ — attacker +1 column.",
            "unit_id": "cw-4armd-001", "hex_from": None, "hex_to": None,
            "data": {"combat_type": "close_assault", "attacker_losses": 0, "defender_losses": 1},
        },
        {
            "turn": turn, "opstage": 2, "type": "supply",
            "description": "4th Armoured Bde is now out of supply.",
            "unit_id": "cw-4armd-001", "hex_from": None, "hex_to": None,
            "data": {"new_status": "out_of_supply"},
        },
        {
            "turn": turn, "opstage": 3, "type": "arbiter_rejection",
            "description": "Proposed move cw-7rtk-001 to B0210 rejected: path crosses enemy ZOC (rule 10.31).",
            "unit_id": "cw-7rtk-001", "hex_from": None, "hex_to": None,
            "data": {"rule_ref": "10.31"},
        },
    ]


@pytest.fixture
def tmp_journal(tmp_path):
    """Returns (JournalAgent, turns_dir, journal_dir) with pre-written turn files."""
    turns_dir   = tmp_path / "turns"
    journal_dir = tmp_path / "journal"
    turns_dir.mkdir()

    state  = _make_state()
    events = _make_events()

    (turns_dir / "turn_057_state.json").write_text(json.dumps(state))
    (turns_dir / "turn_057_events.json").write_text(json.dumps(events))

    agent = JournalAgent(journal_dir=journal_dir, turns_dir=turns_dir)
    return agent, turns_dir, journal_dir


# ── _load_turn_files ──────────────────────────────────────────────────────────

class TestLoadTurnFiles:
    def test_loads_valid_files(self, tmp_journal):
        agent, turns_dir, _ = tmp_journal
        state, events = agent._load_turn_files(57)
        assert state["turn"] == 57
        assert len(events) == 5

    def test_missing_state_raises(self, tmp_path):
        agent = JournalAgent(turns_dir=tmp_path / "turns", journal_dir=tmp_path / "journal")
        with pytest.raises(FileNotFoundError, match="State file"):
            agent._load_turn_files(99)

    def test_missing_events_raises(self, tmp_path):
        turns_dir = tmp_path / "turns"
        turns_dir.mkdir()
        (turns_dir / "turn_099_state.json").write_text("{}")
        agent = JournalAgent(turns_dir=turns_dir, journal_dir=tmp_path / "journal")
        with pytest.raises(FileNotFoundError, match="Events file"):
            agent._load_turn_files(99)


# ── _state_summary ────────────────────────────────────────────────────────────

class TestStateSummary:
    def test_counts_active_units(self, tmp_journal):
        agent, _, _ = tmp_journal
        summary = agent._state_summary(_make_state())
        assert "Commonwealth: 2" in summary
        assert "Axis: 1" in summary

    def test_flags_oos(self, tmp_journal):
        agent, _, _ = tmp_journal
        summary = agent._state_summary(_make_state())
        assert "out-of-supply" in summary
        assert "4th Armoured Bde" in summary

    def test_no_oos_when_all_supplied(self, tmp_journal):
        agent, _, _ = tmp_journal
        state = _make_state()
        for u in state["units"].values():
            u["supply_status"] = "in_supply"
        summary = agent._state_summary(state)
        assert "supplied" in summary
        assert "out-of-supply" not in summary


# ── _format_events ────────────────────────────────────────────────────────────

class TestFormatEvents:
    def test_groups_by_opstage(self, tmp_journal):
        agent, _, _ = tmp_journal
        text = agent._format_events(_make_events())
        assert "### OpStage 1" in text
        assert "### OpStage 2" in text
        assert "### OpStage 3" in text

    def test_priority_types_bolded(self, tmp_journal):
        agent, _, _ = tmp_journal
        text = agent._format_events(_make_events())
        assert "**combat**" in text
        assert "**supply**" in text
        assert "**weather_roll**" in text
        assert "**arbiter_rejection**" in text

    def test_empty_events(self, tmp_journal):
        agent, _, _ = tmp_journal
        text = agent._format_events([])
        assert "no events" in text

    def test_minor_event_cap(self, tmp_journal):
        agent, _, _ = tmp_journal
        # 12 minor events → should cap at _MINOR_EVENT_CAP and show overflow note
        minor_events = [
            {"turn": 57, "opstage": 1, "type": "movement",
             "description": f"Move {i}", "unit_id": None,
             "hex_from": None, "hex_to": None, "data": {}}
            for i in range(12)
        ]
        text = agent._format_events(minor_events)
        assert "minor events" in text


# ── _build_user_message ───────────────────────────────────────────────────────

class TestBuildUserMessage:
    def test_includes_turn_header(self, tmp_journal):
        agent, _, _ = tmp_journal
        msg = agent._build_user_message(_make_state(), _make_events())
        assert "Turn 57" in msg
        assert "1941-11-18" in msg

    def test_includes_rejected_orders_section(self, tmp_journal):
        agent, _, _ = tmp_journal
        msg = agent._build_user_message(_make_state(), _make_events())
        assert "Rejected Orders" in msg
        assert "rule 10.31" in msg

    def test_no_rejection_section_when_none(self, tmp_journal):
        agent, _, _ = tmp_journal
        events = [e for e in _make_events() if e["type"] != "arbiter_rejection"]
        msg = agent._build_user_message(_make_state(), events)
        assert "Rejected Orders" not in msg


# ── _write_output ─────────────────────────────────────────────────────────────

class TestWriteOutput:
    def test_creates_file_with_front_matter(self, tmp_journal):
        agent, _, journal_dir = tmp_journal
        narrative = "We advanced on Sidi Rezegh at first light."
        path = agent._write_output(_make_state(), narrative)

        assert path.exists()
        assert path.name == "turn_057_1941-11-18.md"
        content = path.read_text()
        assert content.startswith("---\n")
        assert "turn: 57" in content
        assert 'date: "1941-11-18"' in content
        assert "weather: normal" in content
        assert "initiative: commonwealth" in content
        assert narrative in content

    def test_creates_journal_dir(self, tmp_path):
        agent = JournalAgent(
            journal_dir=tmp_path / "new_journal",
            turns_dir=tmp_path / "turns",
        )
        agent._write_output(_make_state(), "test narrative")
        assert (tmp_path / "new_journal").exists()


# ── write_turn_journal (integration, Claude monkeypatched) ────────────────────

class TestWriteTurnJournal:
    def test_full_pipeline(self, tmp_journal, monkeypatch):
        agent, _, journal_dir = tmp_journal

        stub_narrative = textwrap.dedent("""\
            Dawn, 18 November 1941. Eighth Army moved forward.

            We advanced on Sidi Rezegh under grey skies.
        """).strip()

        monkeypatch.setattr(agent, "_call_claude", lambda _: stub_narrative)

        output = agent.write_turn_journal(57)
        assert output.exists()
        content = output.read_text()
        assert "turn: 57" in content
        assert stub_narrative in content

    def test_rejection_narrative_included(self, tmp_journal, monkeypatch):
        """write_turn_journal passes rejections to Claude's user message."""
        agent, _, _ = tmp_journal
        captured: list = []

        def capture_and_stub(user_msg: str) -> str:
            captured.append(user_msg)
            return "stub narrative"

        monkeypatch.setattr(agent, "_call_claude", capture_and_stub)
        agent.write_turn_journal(57)

        assert captured, "expected _call_claude to be called"
        assert "Rejected Orders" in captured[0]
        assert "10.31" in captured[0]
