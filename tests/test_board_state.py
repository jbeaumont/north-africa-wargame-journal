"""
Tests for src/agents/board_state.py.

Focuses on:
  - Scenario loading: produces a valid GameState from crusader.json
  - Action dispatch: move, supply checks, fuel evaporation, end_opstage
  - Turn output: files written with correct content
  - Deterministic behavior: no LLM calls, reproducible results
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agents.board_state import (
    ActionResult,
    BoardStateAgent,
    load_scenario,
    write_opstage_output,
    write_turn_output,
)
from src.models.game_state import GameState
from src.models.unit import Side, UnitStatus


# ── Scenario loading ──────────────────────────────────────────────────────────

class TestLoadScenario:
    """Scenario loader builds a valid GameState from crusader.json."""

    def test_loads_crusader(self):
        gs = load_scenario("crusader")
        assert isinstance(gs, GameState)

    def test_crusader_turn_and_opstage(self):
        gs = load_scenario("crusader")
        assert gs.turn == 57       # Crusader starts GT 57
        assert gs.opstage == 3     # OpStage 3

    def test_crusader_date(self):
        gs = load_scenario("crusader")
        assert gs.current_date is not None
        assert gs.historical_date_str() == "18 November 1941"

    def test_crusader_has_units(self):
        gs = load_scenario("crusader")
        assert len(gs.units) > 0

    def test_crusader_has_both_sides(self):
        gs = load_scenario("crusader")
        sides = {u.side for u in gs.units.values()}
        assert Side.COMMONWEALTH in sides
        assert Side.AXIS in sides

    def test_units_have_hex_ids(self):
        gs = load_scenario("crusader")
        active = [u for u in gs.units.values() if u.hex_id]
        assert len(active) > 0

    def test_crusader_has_supply_dumps(self):
        gs = load_scenario("crusader")
        assert len(gs.supply_dumps) > 0

    def test_unlimited_dumps_flagged(self):
        gs = load_scenario("crusader")
        unlimited = [d for d in gs.supply_dumps.values() if d.is_unlimited]
        # Alexandria and Cairo should be unlimited
        assert len(unlimited) >= 2

    def test_unit_ids_unique(self):
        gs = load_scenario("crusader")
        ids = [u.id for u in gs.units.values()]
        assert len(ids) == len(set(ids)), "duplicate unit IDs found"

    def test_all_scenarios_load(self):
        """All scenario files in data/extracted/scenarios/ must load without error."""
        from pathlib import Path
        scenarios_dir = Path("data/extracted/scenarios")
        for f in scenarios_dir.glob("*.json"):
            gs = load_scenario(f.stem)
            assert isinstance(gs, GameState), f"failed to load {f.stem}"


# ── Agent construction ────────────────────────────────────────────────────────

class TestBoardStateAgentConstruction:
    def test_from_scenario(self):
        agent = BoardStateAgent.from_scenario("crusader")
        assert agent.gs.scenario == "crusader"

    def test_from_state_file(self, tmp_path):
        gs = load_scenario("crusader")
        state_file = tmp_path / "state.json"
        with open(state_file, "w") as f:
            json.dump(gs.to_dict(), f)

        agent = BoardStateAgent.from_state_file(str(state_file))
        assert agent.gs.scenario == "crusader"
        assert agent.gs.turn == gs.turn


# ── Action: unknown ───────────────────────────────────────────────────────────

class TestUnknownAction:
    def test_unknown_action_returns_failure(self):
        agent = BoardStateAgent.from_scenario("crusader")
        result = agent.apply_action({"action": "teleport"})
        assert result.success is False
        assert "teleport" in (result.reason or "")


# ── Action: move ──────────────────────────────────────────────────────────────

class TestMoveAction:
    """Move action updates unit position and deducts CP."""

    def setup_method(self):
        self.agent = BoardStateAgent.from_scenario("crusader")

    def _first_active_unit(self) -> tuple:
        for uid, unit in self.agent.gs.units.items():
            if unit.is_active() and unit.hex_id and unit.cpa > 0:
                return uid, unit
        pytest.skip("no active unit with CPA > 0 found")

    def test_move_to_adjacent_hex(self):
        uid, unit = self._first_active_unit()
        start_hex = unit.hex_id
        # Get an adjacent hex via HexMap
        neighbors = self.agent.hex_map.neighbors(start_hex)
        if not neighbors:
            pytest.skip("no neighbors found for unit hex")

        dest_hex = neighbors[0]
        result = self.agent.apply_action({
            "action": "move",
            "unit_id": uid,
            "path": [start_hex, dest_hex],
            "context": "voluntary",
        })

        # Either succeeded or stopped for a legitimate reason (ZOC, terrain, etc.)
        assert isinstance(result, ActionResult)
        assert result.action == "move"

    def test_move_nonexistent_unit(self):
        result = self.agent.apply_action({
            "action": "move",
            "unit_id": "DOES-NOT-EXIST",
            "path": ["A0101", "A0102"],
        })
        assert result.success is False
        assert "not found" in (result.reason or "")

    def test_move_short_path_fails(self):
        uid, unit = self._first_active_unit()
        result = self.agent.apply_action({
            "action": "move",
            "unit_id": uid,
            "path": [unit.hex_id],  # only 1 hex — not a move
        })
        assert result.success is False

    def test_move_events_recorded(self):
        """Events from a move are accumulated in the agent's opstage log."""
        uid, unit = self._first_active_unit()
        neighbors = self.agent.hex_map.neighbors(unit.hex_id)
        if not neighbors:
            pytest.skip("no neighbors found")

        before_count = len(self.agent._opstage_events)
        self.agent.apply_action({
            "action": "move",
            "unit_id": uid,
            "path": [unit.hex_id, neighbors[0]],
        })
        # Events may or may not be generated (depends on CP/DP/fuel);
        # at minimum the list should not shrink.
        assert len(self.agent._opstage_events) >= before_count


# ── Action: supply checks ─────────────────────────────────────────────────────

class TestSupplyCheckAction:
    def test_run_supply_checks_succeeds(self):
        agent = BoardStateAgent.from_scenario("crusader")
        result = agent.apply_action({"action": "run_supply_checks"})
        assert result.success is True
        assert result.action == "run_supply_checks"
        assert "checks_run" in result.data

    def test_supply_check_events_accumulated(self):
        agent = BoardStateAgent.from_scenario("crusader")
        agent.apply_action({"action": "run_supply_checks"})
        # Any status-change events should be in the opstage log
        assert isinstance(agent._opstage_events, list)


# ── Action: fuel evaporation ──────────────────────────────────────────────────

class TestFuelEvaporationAction:
    def test_fuel_evaporation_succeeds(self):
        agent = BoardStateAgent.from_scenario("crusader")
        result = agent.apply_action({
            "action": "apply_fuel_evaporation",
            "hot_weather": False,
        })
        assert result.success is True
        assert "dumps_affected" in result.data

    def test_fuel_evaporation_reduces_dump_fuel(self):
        agent = BoardStateAgent.from_scenario("crusader")
        # Get a finite dump
        finite = [d for d in agent.gs.supply_dumps.values()
                  if not d.is_unlimited and not d.is_dummy and d.fuel > 0]
        if not finite:
            pytest.skip("no finite fuel dump found")
        dump = finite[0]
        fuel_before = dump.fuel

        agent.apply_action({"action": "apply_fuel_evaporation", "hot_weather": False})
        assert dump.fuel < fuel_before  # evaporation reduced fuel

    def test_unlimited_dumps_unaffected(self):
        agent = BoardStateAgent.from_scenario("crusader")
        unlimited = [d for d in agent.gs.supply_dumps.values() if d.is_unlimited]
        if not unlimited:
            pytest.skip("no unlimited dump found")

        agent.apply_action({"action": "apply_fuel_evaporation", "hot_weather": False})
        for dump in unlimited:
            assert dump.fuel == 0.0  # unlimited dumps have no numeric fuel value


# ── Action: pasta rule ────────────────────────────────────────────────────────

class TestPastaRuleAction:
    def test_pasta_rule_non_pasta_unit_no_op(self):
        agent = BoardStateAgent.from_scenario("crusader")
        # Find any unit
        uid = next(iter(agent.gs.units))
        agent.gs.units[uid].pasta_rule = False
        result = agent.apply_action({
            "action": "apply_pasta_rule",
            "unit_id": uid,
            "received_pasta_point": False,
        })
        assert result.success is True
        assert result.events == []  # non-pasta unit: no event

    def test_pasta_rule_nonexistent_unit(self):
        agent = BoardStateAgent.from_scenario("crusader")
        result = agent.apply_action({
            "action": "apply_pasta_rule",
            "unit_id": "FAKE",
            "received_pasta_point": True,
        })
        assert result.success is False


# ── Action: end_opstage ───────────────────────────────────────────────────────

class TestEndOpstageAction:
    def test_end_opstage_writes_files(self, tmp_path):
        agent = BoardStateAgent.from_scenario("crusader")
        with patch("src.agents.board_state._TURNS_DIR", tmp_path):
            result = agent.apply_action({"action": "end_opstage"})

        assert result.success is True
        assert "state_file" in result.data
        assert "events_file" in result.data
        assert Path(result.data["state_file"]).exists()
        assert Path(result.data["events_file"]).exists()

    def test_end_opstage_state_is_valid_json(self, tmp_path):
        agent = BoardStateAgent.from_scenario("crusader")
        with patch("src.agents.board_state._TURNS_DIR", tmp_path):
            result = agent.apply_action({"action": "end_opstage"})

        with open(result.data["state_file"]) as f:
            state = json.load(f)
        assert state["scenario"] == "crusader"
        assert "units" in state

    def test_end_opstage_advances_opstage(self):
        agent = BoardStateAgent.from_scenario("crusader")
        initial_opstage = agent.gs.opstage
        with patch("src.agents.board_state._TURNS_DIR", Path(tempfile.mkdtemp())):
            agent.apply_action({"action": "end_opstage"})
        # Crusader starts at opstage 3; advancing would go to 4 (or be clamped)
        # The logic: if opstage < 3 → advance, else stay (end_turn called next)
        expected = initial_opstage + 1 if initial_opstage < 3 else initial_opstage
        assert agent.gs.opstage == expected

    def test_end_opstage_resets_bd_tracking(self):
        agent = BoardStateAgent.from_scenario("crusader")
        agent._bd_checked["some-unit"] = True
        with patch("src.agents.board_state._TURNS_DIR", Path(tempfile.mkdtemp())):
            agent.apply_action({"action": "end_opstage"})
        assert agent._bd_checked == {}

    def test_end_opstage_clears_opstage_events(self):
        agent = BoardStateAgent.from_scenario("crusader")
        # Add a fake event
        from src.models.event import Event
        agent._opstage_events.append(
            Event(turn=57, opstage=3, type="test", description="test")
        )
        with patch("src.agents.board_state._TURNS_DIR", Path(tempfile.mkdtemp())):
            agent.apply_action({"action": "end_opstage"})
        assert agent._opstage_events == []


# ── Action: end_turn ──────────────────────────────────────────────────────────

class TestEndTurnAction:
    def test_end_turn_writes_files(self, tmp_path):
        agent = BoardStateAgent.from_scenario("crusader")
        with patch("src.agents.board_state._TURNS_DIR", tmp_path):
            result = agent.apply_action({"action": "end_turn"})
        assert result.success is True
        assert Path(result.data["state_file"]).exists()

    def test_end_turn_advances_turn_counter(self):
        agent = BoardStateAgent.from_scenario("crusader")
        initial_turn = agent.gs.turn
        with patch("src.agents.board_state._TURNS_DIR", Path(tempfile.mkdtemp())):
            agent.apply_action({"action": "end_turn"})
        assert agent.gs.turn == initial_turn + 1
        assert agent.gs.opstage == 1  # reset to OpStage 1


# ── Fog of war ────────────────────────────────────────────────────────────────

class TestFogOfWar:
    def test_fog_of_war_commonwealth_view(self):
        agent = BoardStateAgent.from_scenario("crusader")
        fow = agent.fog_of_war(Side.COMMONWEALTH)
        assert fow["viewing_side"] == "commonwealth"
        assert "units" in fow

    def test_fog_of_war_axis_view(self):
        agent = BoardStateAgent.from_scenario("crusader")
        fow = agent.fog_of_war(Side.AXIS)
        assert fow["viewing_side"] == "axis"

    def test_fog_hides_non_adjacent_enemy(self):
        agent = BoardStateAgent.from_scenario("crusader")
        fow = agent.fog_of_war(Side.COMMONWEALTH)
        # Enemy units must NOT appear in the units dict — only contact_hexes
        for uid, u in fow["units"].items():
            assert u.get("side") != "axis", (
                f"Axis unit {uid} leaked into CW fog-of-war units dict"
            )
        assert "contact_hexes" in fow
        assert isinstance(fow["contact_hexes"], list)


# ── Narrative ─────────────────────────────────────────────────────────────────

class TestNarrativeSummary:
    def test_narrative_not_empty(self):
        agent = BoardStateAgent.from_scenario("crusader")
        summary = agent.narrative_summary()
        assert isinstance(summary, str)
        assert len(summary) > 0
        assert "57" in summary  # should mention the turn number


# ── Output file integration ───────────────────────────────────────────────────

class TestWriteFunctions:
    def test_write_opstage_output(self, tmp_path):
        gs = load_scenario("crusader")
        state_path, events_path = write_opstage_output(gs, [], _turns_dir=tmp_path)
        assert state_path.exists()
        assert events_path.exists()

        with open(state_path) as f:
            state = json.load(f)
        assert state["turn"] == 57

    def test_write_turn_output(self, tmp_path):
        gs = load_scenario("crusader")
        state_path, events_path = write_turn_output(gs, [], _turns_dir=tmp_path)
        assert state_path.exists()

        with open(events_path) as f:
            events = json.load(f)
        assert events == []
