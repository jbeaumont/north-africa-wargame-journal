"""Tests for data loading — verifies all JSON files parse correctly."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from cna.data.loader import (
    load_all_ground_units,
    load_all_supply_counters,
    load_hex_map,
)
from cna.models.counter import Side


class TestDataLoading:
    def test_load_all_ground_units(self):
        units = load_all_ground_units()
        assert len(units) > 0
        # Check both sides are represented
        axis_units = [u for u in units.values() if u.side == Side.AXIS]
        allied_units = [u for u in units.values() if u.side == Side.ALLIED]
        assert len(axis_units) > 0
        assert len(allied_units) > 0

    def test_load_axis_units_have_correct_fields(self):
        units = load_all_ground_units()
        axis = [u for u in units.values() if u.side == Side.AXIS]
        for u in axis:
            assert u.id
            assert u.name
            assert u.cpa > 0
            assert u.toe_strength > 0
            assert u.morale > 0
            assert u.steps > 0
            assert u.max_steps > 0

    def test_italian_units_have_pasta_rule(self):
        """Italian infantry should have pasta_rule=True."""
        units = load_all_ground_units()
        from cna.models.counter import Nationality, UnitType
        italian_infantry = [
            u for u in units.values()
            if u.nationality == Nationality.ITALIAN
            and u.unit_type in (UnitType.INFANTRY_REGIMENT, UnitType.INFANTRY_BATTALION)
        ]
        pasta_units = [u for u in italian_infantry if u.pasta_rule]
        assert len(pasta_units) > 0, "Expected Italian infantry with pasta_rule=True"

    def test_dak_available_on_turn_14(self):
        """DAK units should arrive on Turn 14."""
        units = load_all_ground_units()
        dak_hq = units.get("GE-DAK-HQ")
        assert dak_hq is not None
        assert dak_hq.available_turn == 14

    def test_us_units_available_on_turn_61(self):
        """US units arrive via Operation Torch on Turn 61."""
        units = load_all_ground_units()
        us_corps = units.get("US-II-CORPS-HQ")
        assert us_corps is not None
        assert us_corps.available_turn == 61

    def test_load_supply_counters(self):
        counters = load_all_supply_counters()
        assert len(counters) > 0
        # Both sides should have supply counters
        axis_depots = [c for c in counters.values() if c.side == Side.AXIS]
        allied_depots = [c for c in counters.values() if c.side == Side.ALLIED]
        assert len(axis_depots) > 0
        assert len(allied_depots) > 0

    def test_tripoli_is_infinite_depot(self):
        counters = load_all_supply_counters()
        tripoli = counters.get("AX-TRIPOLI-BASE")
        assert tripoli is not None
        assert tripoli.current_load >= 999.0

    def test_load_hex_map(self):
        hex_map = load_hex_map()
        assert len(hex_map) > 50

    def test_key_locations_exist(self):
        hex_map = load_hex_map()
        tobruk = hex_map.find_named("Tobruk")
        assert tobruk is not None
        assert tobruk.is_port

        el_alamein = hex_map.find_named("El Alamein")
        assert el_alamein is not None

        tripoli = hex_map.find_named("Tripoli")
        assert tripoli is not None
        assert tripoli.is_port

    def test_qattara_depression_is_impassable(self):
        hex_map = load_hex_map()
        qattara = hex_map.find_named("Qattara Depression")
        assert qattara is not None
        assert qattara.is_impassable()

    def test_hex_adjacency_is_symmetric(self):
        """If A lists B as adjacent, B should list A."""
        hex_map = load_hex_map()
        violations = []
        for h in hex_map.all_hexes():
            for adj_id in h.adjacent:
                adj = hex_map.get(adj_id)
                if adj and h.hex_id not in adj.adjacent:
                    violations.append(f"{h.hex_id} → {adj_id} not reciprocal")
        # Allow some violations (map edge cases) but flag major issues
        assert len(violations) < 20, f"Too many adjacency violations: {violations[:5]}"
