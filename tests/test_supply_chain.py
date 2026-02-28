"""Tests for the supply chain engine."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from cna.models.counter import GroundUnit, Nationality, Side, UnitType, SupplyState
from cna.models.hex_map import HexMap, Hex, Terrain
from cna.models.game_state import GameState
from cna.models.supply import SupplyReport
from cna.engine.supply_chain import (
    calculate_supply_lines,
    apply_fuel_evaporation,
    compile_supply_report,
)
from cna.models.counter import SupplyCounter


def _make_simple_map() -> HexMap:
    """Build a linear 7-hex map: 0101-0102-0103-0104-0105-0106-0107."""
    hex_map = HexMap()
    prev = None
    for col in range(1, 8):
        hex_id = f"01{col:02d}"
        adjacent = []
        if prev:
            adjacent.append(prev)
        if col < 7:
            adjacent.append(f"01{col+1:02d}")
        h = Hex(
            hex_id=hex_id,
            terrain=Terrain.FLAT_DESERT_ROCKY,
            adjacent=adjacent,
        )
        hex_map.add_hex(h)
        prev = hex_id
    return hex_map


def _make_state(unit_hex: str, depot_hex: str) -> GameState:
    hex_map = _make_simple_map()
    state = GameState(turn=1, map=hex_map)

    unit = GroundUnit(
        id="TEST-UNIT-1",
        name="Test Battalion",
        nationality=Nationality.BRITISH,
        side=Side.ALLIED,
        unit_type=UnitType.INFANTRY_BATTALION,
        hex_id=unit_hex,
        available_turn=1,
    )
    state.ground_units["TEST-UNIT-1"] = unit

    depot = SupplyCounter(
        id="TEST-DEPOT-1",
        name="Test Depot",
        nationality=Nationality.BRITISH,
        side=Side.ALLIED,
        unit_type=UnitType.SUPPLY_DEPOT,
        hex_id=depot_hex,
        capacity=100.0,
        current_load=100.0,
        supply_type="general",
    )
    state.supply_counters["TEST-DEPOT-1"] = depot

    # Control all hexes for allied
    for h in hex_map.all_hexes():
        state.hex_control[h.hex_id] = "allied"

    return state


class TestSupplyLineCalculation:
    def test_unit_adjacent_to_depot_is_in_supply(self):
        state = _make_state("0101", "0102")
        lines = calculate_supply_lines(state)
        assert "TEST-UNIT-1" in lines
        assert lines["TEST-UNIT-1"].in_supply

    def test_unit_at_depot_hex_is_in_supply(self):
        state = _make_state("0101", "0101")
        lines = calculate_supply_lines(state)
        assert lines["TEST-UNIT-1"].in_supply

    def test_unit_5_hexes_away_is_in_supply(self):
        state = _make_state("0101", "0106")
        lines = calculate_supply_lines(state)
        assert lines["TEST-UNIT-1"].in_supply
        assert lines["TEST-UNIT-1"].hex_distance == 5

    def test_unit_6_hexes_away_is_out_of_supply(self):
        state = _make_state("0101", "0107")
        lines = calculate_supply_lines(state)
        assert not lines["TEST-UNIT-1"].in_supply

    def test_supply_blocked_by_enemy_control(self):
        state = _make_state("0101", "0103")
        # Enemy controls the intermediate hex
        state.hex_control["0102"] = "axis"
        lines = calculate_supply_lines(state)
        # Can't reach depot through enemy hex
        assert not lines["TEST-UNIT-1"].in_supply


class TestFuelEvaporation:
    def test_british_units_lose_7_percent(self):
        state = GameState(turn=1, map=HexMap())
        unit = GroundUnit(
            id="GB-1",
            name="British Tank",
            nationality=Nationality.BRITISH,
            side=Side.ALLIED,
            unit_type=UnitType.ARMORED_BATTALION,
        )
        unit.supply.fuel = 100.0
        state.ground_units["GB-1"] = unit

        report = SupplyReport(turn=1)
        apply_fuel_evaporation(state, report)

        assert abs(unit.supply.fuel - 93.0) < 0.01
        assert report.fuel_evaporated > 0

    def test_german_units_lose_3_percent(self):
        state = GameState(turn=1, map=HexMap())
        unit = GroundUnit(
            id="GE-1",
            name="German Panzer",
            nationality=Nationality.GERMAN,
            side=Side.AXIS,
            unit_type=UnitType.ARMORED_BATTALION,
        )
        unit.supply.fuel = 100.0
        state.ground_units["GE-1"] = unit

        report = SupplyReport(turn=1)
        apply_fuel_evaporation(state, report)

        assert abs(unit.supply.fuel - 97.0) < 0.01

    def test_italian_units_lose_3_percent(self):
        state = GameState(turn=1, map=HexMap())
        unit = GroundUnit(
            id="IT-1",
            name="Italian Infantry",
            nationality=Nationality.ITALIAN,
            side=Side.AXIS,
            unit_type=UnitType.INFANTRY_BATTALION,
        )
        unit.supply.fuel = 50.0
        state.ground_units["IT-1"] = unit

        report = SupplyReport(turn=1)
        apply_fuel_evaporation(state, report)

        assert abs(unit.supply.fuel - 48.5) < 0.01
