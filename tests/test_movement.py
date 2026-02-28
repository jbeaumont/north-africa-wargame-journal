"""Tests for the movement engine."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from cna.models.counter import GroundUnit, Nationality, Side, UnitType, SupplyState
from cna.models.hex_map import HexMap, Hex, Terrain
from cna.models.game_state import GameState
from cna.engine.movement import MovementOrder, attempt_movement


def _linear_map(n: int = 6) -> HexMap:
    """Build a linear map of n hexes: 0101 → 01{n+1}."""
    hex_map = HexMap()
    for i in range(1, n + 1):
        hex_id = f"01{i:02d}"
        adjacent = []
        if i > 1:
            adjacent.append(f"01{i-1:02d}")
        if i < n:
            adjacent.append(f"01{i+1:02d}")
        h = Hex(
            hex_id=hex_id,
            terrain=Terrain.FLAT_DESERT_ROCKY,
            adjacent=adjacent,
            road_hexes=[f"01{i+1:02d}"] if i < n else [],
        )
        hex_map.add_hex(h)
    return hex_map


def _road_map() -> HexMap:
    """Build a 4-hex map with road connections."""
    hex_map = HexMap()
    ids = ["0101", "0102", "0103", "0104"]
    for i, hex_id in enumerate(ids):
        adjacent = []
        if i > 0:
            adjacent.append(ids[i - 1])
        if i < len(ids) - 1:
            adjacent.append(ids[i + 1])
        h = Hex(
            hex_id=hex_id,
            terrain=Terrain.FLAT_DESERT_ROCKY,
            adjacent=adjacent,
            road_hexes=adjacent,  # All connections are roads
        )
        hex_map.add_hex(h)
    return hex_map


def _unit(cpa: int = 20, fuel: float = 50.0, nationality=Nationality.GERMAN,
          unit_type=UnitType.ARMORED_BATTALION, available_turn=1) -> GroundUnit:
    u = GroundUnit(
        id="TEST-UNIT",
        name="Test Unit",
        nationality=nationality,
        side=Side.AXIS,
        unit_type=unit_type,
        cpa=cpa,
        fuel_capacity=10.0,
    )
    u.supply.fuel = fuel
    u.hex_id = "0101"
    u.available_turn = available_turn
    return u


def _state(hex_map: HexMap, turn: int = 1) -> GameState:
    s = GameState(turn=turn, map=hex_map)
    return s


class TestMovementCosts:
    def test_single_hex_costs_2_cp_on_clear_terrain(self):
        m = _linear_map()
        u = _unit(cpa=10)
        u.hex_id = "0101"
        state = _state(m)
        state.ground_units["TEST-UNIT"] = u
        order = MovementOrder(unit_id="TEST-UNIT", path=["0101", "0102"])
        result = attempt_movement(u, order, m, state)
        assert result.hexes_moved == 1
        assert abs(result.cp_spent - 2.0) < 0.01

    def test_road_movement_costs_half_cp(self):
        m = _road_map()
        u = _unit(cpa=10)
        u.hex_id = "0101"
        state = _state(m)
        state.ground_units["TEST-UNIT"] = u
        order = MovementOrder(unit_id="TEST-UNIT", path=["0101", "0102"])
        result = attempt_movement(u, order, m, state)
        assert result.hexes_moved == 1
        assert abs(result.cp_spent - 0.5) < 0.01

    def test_unit_cannot_exceed_cpa(self):
        m = _linear_map(6)
        u = _unit(cpa=4)
        u.hex_id = "0101"
        state = _state(m)
        state.ground_units["TEST-UNIT"] = u
        # Try to move 4 hexes (each costs 2 CP = 8 CP total, CPA is 4)
        order = MovementOrder(unit_id="TEST-UNIT",
                              path=["0101", "0102", "0103", "0104", "0105"])
        result = attempt_movement(u, order, m, state)
        # Can only move 2 hexes (2 × 2 CP = 4 CP)
        assert result.hexes_moved == 2
        assert u.hex_id == "0103"

    def test_fuel_consumed_on_movement(self):
        m = _linear_map()
        u = _unit(cpa=20, fuel=50.0, unit_type=UnitType.ARMORED_BATTALION)
        u.hex_id = "0101"
        state = _state(m)
        state.ground_units["TEST-UNIT"] = u
        order = MovementOrder(unit_id="TEST-UNIT", path=["0101", "0102"])
        result = attempt_movement(u, order, m, state)
        assert result.fuel_consumed > 0
        assert u.supply.fuel < 50.0

    def test_infantry_no_fuel_consumption(self):
        m = _linear_map()
        u = _unit(cpa=12, fuel=0.0, unit_type=UnitType.INFANTRY_BATTALION)
        u.hex_id = "0101"
        state = _state(m)
        state.ground_units["TEST-UNIT"] = u
        order = MovementOrder(unit_id="TEST-UNIT", path=["0101", "0102"])
        result = attempt_movement(u, order, m, state)
        # Infantry don't consume fuel (no vehicle_unit check)
        # But they also shouldn't be blocked by fuel checks
        # Infantry CAN move even with 0 fuel
        assert result.aborted_reason != "out of fuel"

    def test_armored_unit_blocked_by_no_fuel(self):
        m = _linear_map()
        u = _unit(cpa=20, fuel=0.0, unit_type=UnitType.ARMORED_BATTALION)
        u.hex_id = "0101"
        state = _state(m)
        state.ground_units["TEST-UNIT"] = u
        order = MovementOrder(unit_id="TEST-UNIT", path=["0101", "0102"])
        result = attempt_movement(u, order, m, state)
        assert result.aborted_reason == "out of fuel"
        assert result.hexes_moved == 0

    def test_unit_not_yet_available(self):
        m = _linear_map()
        u = _unit(available_turn=5)
        u.hex_id = "0101"
        state = _state(m, turn=1)
        state.ground_units["TEST-UNIT"] = u
        order = MovementOrder(unit_id="TEST-UNIT", path=["0101", "0102"])
        result = attempt_movement(u, order, m, state)
        assert result.aborted_reason == "not yet available"
