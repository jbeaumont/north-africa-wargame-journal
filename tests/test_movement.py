"""
Tests for src/engine/movement.py.

All expected values are derived from oracle fixtures in
tests/fixtures/movement_rules.json, citing specific rule numbers from
cna_rules.txt.

Deferred: tests that require the Breakdown Table (21.38) dice roll are
marked xfail with reason="table not yet extracted".
"""

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.engine.movement import (
    ContactStatus,
    MoveResult,
    ZOC_EXIT_COST,
    bd_column,
    compute_fuel_consumption,
    dp_from_cp_excess,
    execute_move,
    fuel_capacity,
    needs_breakdown_check,
    non_motorized_cp_cap,
    validate_move_path,
)
from src.models.unit import (
    Nationality,
    Side,
    Unit,
    UnitSize,
    UnitStatus,
    UnitType,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_unit(
    uid: str = "test-unit",
    motorized: bool = True,
    hex_id: str = "A0101",
    cpa: int = 20,
    cp_remaining: float = None,
    status: UnitStatus = UnitStatus.ACTIVE,
    cohesion: int = 0,
    bd: float = 0.0,
    size: UnitSize = UnitSize.BATTALION,
) -> Unit:
    if cp_remaining is None:
        cp_remaining = float(cpa)
    return Unit(
        id=uid, name=uid,
        nationality=Nationality.BRITISH,
        side=Side.COMMONWEALTH,
        type=UnitType.INFANTRY if not motorized else UnitType.ARMOR,
        size=size,
        motorized=motorized,
        hex_id=hex_id,
        cpa=cpa,
        cp_remaining=cp_remaining,
        status=status,
        cohesion=cohesion,
        breakdown_points=bd,
    )


def make_game_state(unit: Unit = None):
    """Minimal GameState mock."""
    gs = MagicMock()
    gs.turn = 1
    gs.opstage = 1
    gs.weather = "clear"
    gs.units = {unit.id: unit} if unit else {}
    gs.formations = {}
    if unit:
        gs.formation_cpa.return_value = unit.cpa
    return gs


def make_hex_map(cp_cost: float = 1.0, bd_cost: float = 0.0, in_zoc: bool = False):
    """Minimal HexMap mock."""
    hm = MagicMock()
    # direction_to returns a direction string for adjacent hexes
    hm.direction_to.return_value = "N"
    hm.entry_cost.return_value = cp_cost
    hm.entry_bd.return_value = bd_cost
    hm.in_enemy_zoc.return_value = in_zoc
    hm.zoc_cancelled.return_value = False
    hm.neighbors.return_value = []
    return hm


# ── Rule 6.21: DP from CPA excess ────────────────────────────────────────────

class TestDpFromCpExcess:
    """Rule 6.21: 1 DP per CP over CPA."""

    def test_fixture_example_cpa15_uses18(self):
        # oracle fixture: CPA 15, uses 18 CP → 3 DPs
        assert dp_from_cp_excess(18, 15) == 3

    def test_fixture_rnf_example(self):
        # oracle fixture: CPA 8, uses 10 CP → 2 DPs (rule 6.22 example)
        assert dp_from_cp_excess(10, 8) == 2

    def test_exactly_at_cpa_no_dp(self):
        assert dp_from_cp_excess(20, 20) == 0

    def test_one_cp_over(self):
        assert dp_from_cp_excess(11, 10) == 1

    def test_fractional_excess_floors(self):
        # 10.7 CP used vs CPA 10 → 0.7 excess → floor → 0 DP
        assert dp_from_cp_excess(10.7, 10) == 0

    def test_zero_used(self):
        assert dp_from_cp_excess(0, 15) == 0


# ── Rule 8.17: non-motorized CP cap ───────────────────────────────────────────

class TestNonMotorizedCpCap:
    """Rule 8.17: CPA ≤ 10 → max voluntary = CPA × 1.5."""

    def test_fixture_cpa8_cap_is_12(self):
        # oracle fixture: CPA 8 → cap 12
        assert non_motorized_cp_cap(8) == 12.0

    def test_fixture_cpa10_cap_is_15(self):
        # oracle fixture: CPA 10 → cap 15
        assert non_motorized_cp_cap(10) == 15.0

    def test_motorized_no_cap(self):
        # CPA > 10 → no cap (motorized)
        assert non_motorized_cp_cap(20) is None
        assert non_motorized_cp_cap(25) is None
        assert non_motorized_cp_cap(11) is None

    def test_cpa1_minimum(self):
        assert non_motorized_cp_cap(1) == 1.5


# ── Rule 49.13: fuel consumption ──────────────────────────────────────────────

class TestFuelConsumption:
    """Rule 49.13: fuel = rate × ceil(cp / 5)."""

    def test_fixture_rate4_cp12(self):
        # oracle fixture: rate=4, 12 CP → ceil(12/5)=3 groups → 12 fuel
        assert compute_fuel_consumption(12, 4) == 12.0

    def test_fixture_rate4_cp1_fraction(self):
        # oracle fixture: 1 CP still costs 1 group (fraction thereof)
        assert compute_fuel_consumption(1, 4) == 4.0

    def test_fixture_rate3_cp6(self):
        # oracle fixture: rate=3, 6 CP → ceil(6/5)=2 → 6 fuel
        assert compute_fuel_consumption(6, 3) == 6.0

    def test_fixture_rate4_exactly5cp(self):
        # oracle fixture: exactly 5 CP → 1 group (no fraction)
        assert compute_fuel_consumption(5, 4) == 4.0

    def test_zero_cp_no_fuel(self):
        assert compute_fuel_consumption(0, 4) == 0.0

    def test_zero_rate_no_fuel(self):
        assert compute_fuel_consumption(12, 0) == 0.0

    def test_ceil_rounding(self):
        # 11 CP → ceil(11/5) = 3 groups
        assert compute_fuel_consumption(11, 2) == 6.0


class TestFuelCapacity:
    """Rule 49.14: capacity = CPA × (1/5) × fuel_rate."""

    def test_fixture_cpa25_rate4(self):
        # oracle fixture: 25 × 0.2 × 4 = 20
        assert fuel_capacity(25, 4) == 20.0

    def test_capacity_equals_full_move_fuel(self):
        # "always has exactly enough capacity to allow all CPA on movement"
        cpa = 20
        rate = 3.0
        cap = fuel_capacity(cpa, rate)
        fuel_full_move = compute_fuel_consumption(float(cpa), rate)
        assert cap == fuel_full_move


# ── Rule 21.27/21.31: Breakdown column ───────────────────────────────────────

class TestBdColumn:
    """Rules 21.27, 21.31: breakdown column detection."""

    def test_bd_3_no_check(self):
        # rule 21.27: strictly greater than 3 needed
        assert bd_column(3.0) is None
        assert bd_column(3) is None

    def test_bd_4_first_column(self):
        # rule 21.31: 4-10 is the first column
        assert bd_column(4) == "4-10"

    def test_bd_10_first_column(self):
        assert bd_column(10) == "4-10"

    def test_bd_11_second_column(self):
        assert bd_column(11) == "11-20"

    def test_bd_20_second_column(self):
        assert bd_column(20) == "11-20"

    def test_bd_21_third_column(self):
        assert bd_column(21) == "21-30"

    def test_bd_71_plus(self):
        assert bd_column(71) == "71+"
        assert bd_column(100) == "71+"

    def test_fractions_rounded_up(self):
        # rule 21.31: "all fractions are rounded up: 20.5 = 21"
        assert bd_column(20.5) == "21-30"
        assert bd_column(10.1) == "11-20"


class TestNeedsBreakdownCheck:
    """Rules 21.24-21.27: when a breakdown check is required."""

    def test_no_check_if_bd_le_3(self):
        # rule 21.27: no check until BD > 3
        assert needs_breakdown_check(0, 3.0, False) is False

    def test_first_check_bd_gt_3(self):
        # rule 21.24: check when unit ceases movement, if BD > 3
        assert needs_breakdown_check(0, 4.0, False) is True

    def test_fixture_same_column_no_second_check(self):
        # oracle fixture: 15 BD → first check; more movement → 17 BD; still 11-20 → no check
        assert needs_breakdown_check(15, 17, had_previous_check=True) is False

    def test_fixture_new_column_requires_check(self):
        # oracle fixture: 15 BD (11-20) → retreat → 27 BD (21-30) → check required
        assert needs_breakdown_check(15, 27, had_previous_check=True) is True

    def test_no_previous_check_always_checks(self):
        # first movement of OpStage: if BD > 3, always check
        assert needs_breakdown_check(0, 15, had_previous_check=False) is True

    def test_crossing_from_none_to_column(self):
        # BD was 0, now 5 — crosses from "no column" into 4-10 column
        assert needs_breakdown_check(0, 5, had_previous_check=True) is True


# ── Rule 8.15: ZOC exit costs ─────────────────────────────────────────────────

class TestZocExitCosts:
    """Rule 8.15: CP cost to exit ZOC at start of movement."""

    def test_no_zoc_no_cost(self):
        assert ZOC_EXIT_COST[ContactStatus.NONE] == 0.0

    def test_contact_costs_2(self):
        # rule 8.15.2: Contact → 2 CP
        assert ZOC_EXIT_COST[ContactStatus.CONTACT] == 2.0

    def test_engaged_costs_4(self):
        # rule 8.15.3: Engaged → 4 CP
        assert ZOC_EXIT_COST[ContactStatus.ENGAGED] == 4.0


# ── execute_move integration ──────────────────────────────────────────────────

class TestExecuteMove:
    """Integration tests for execute_move()."""

    def test_simple_two_hex_move(self):
        unit = make_unit(cpa=20, cp_remaining=20.0)
        gs = make_game_state(unit)
        hm = make_hex_map(cp_cost=1.0, bd_cost=0.0)

        result = execute_move(unit, ["A0101", "A0102"], gs, hm)

        assert result.path_taken == ["A0101", "A0102"]
        assert result.cp_spent_movement == 1.0
        assert unit.hex_id == "A0102"
        assert result.stopped_reason is None

    def test_cp_deducted_from_unit(self):
        unit = make_unit(cpa=20, cp_remaining=20.0)
        gs = make_game_state(unit)
        hm = make_hex_map(cp_cost=3.0)

        execute_move(unit, ["A0101", "A0102", "A0103"], gs, hm)
        # 2 steps × 3 CP = 6 CP spent; cp_remaining = 20 - 6 = 14
        assert unit.cp_remaining == 14.0

    def test_bd_accumulated_on_unit(self):
        unit = make_unit(cpa=20, cp_remaining=20.0, bd=0.0)
        gs = make_game_state(unit)
        hm = make_hex_map(cp_cost=1.0, bd_cost=4.0)

        result = execute_move(unit, ["A0101", "A0102"], gs, hm)

        assert result.bd_accumulated == 4.0
        assert unit.breakdown_points == 4.0

    def test_breakdown_check_needed_when_bd_gt_3(self):
        unit = make_unit(cpa=20, cp_remaining=20.0, bd=0.0)
        gs = make_game_state(unit)
        hm = make_hex_map(cp_cost=1.0, bd_cost=4.0)

        result = execute_move(unit, ["A0101", "A0102"], gs, hm)

        # bd_after = 4 > 3 → check needed (rule 21.27)
        assert result.breakdown_check_needed is True
        assert result.bd_column_label == "4-10"

    def test_no_breakdown_check_when_bd_le_3(self):
        unit = make_unit(cpa=20, cp_remaining=20.0, bd=0.0)
        gs = make_game_state(unit)
        hm = make_hex_map(cp_cost=1.0, bd_cost=0.0)  # no BD terrain

        result = execute_move(unit, ["A0101", "A0102"], gs, hm)

        assert result.breakdown_check_needed is False
        assert result.bd_column_label is None

    def test_zoc_stop(self):
        """Rule 8.14: unit stops immediately on entering enemy ZOC."""
        unit = make_unit(cpa=20, cp_remaining=20.0)
        gs = make_game_state(unit)

        # ZOC in second destination hex (index 2 in path), not the first
        hm = make_hex_map(cp_cost=1.0)
        call_count = [0]
        def mock_in_zoc(hex_id, side, units):
            call_count[0] += 1
            return hex_id == "A0103"  # ZOC only at A0103
        hm.in_enemy_zoc.side_effect = mock_in_zoc

        result = execute_move(unit, ["A0101", "A0102", "A0103", "A0104"], gs, hm)

        assert "A0103" in result.path_taken
        assert "A0104" not in result.path_taken
        assert result.stopped_reason is not None
        assert "ZOC" in result.stopped_reason

    def test_zoc_exit_cost_charged(self):
        """Rule 8.15: ZOC exit cost added at start."""
        unit = make_unit(cpa=20, cp_remaining=20.0)
        gs = make_game_state(unit)
        hm = make_hex_map(cp_cost=1.0)

        result = execute_move(
            unit, ["A0101", "A0102"], gs, hm,
            zoc_contact_status=ContactStatus.CONTACT,
        )

        assert result.zoc_exit_cp == 2.0
        assert result.cp_spent_total == 3.0  # 2 (ZOC exit) + 1 (terrain)

    def test_fuel_consumed_for_motorized(self):
        """Rule 49.13: motorized unit with fuel_rate=4 consumes fuel."""
        unit = make_unit(motorized=True, cpa=20, cp_remaining=20.0)
        gs = make_game_state(unit)
        # 3 steps of 4 CP each = 12 CP; ceil(12/5)=3 × 4 = 12 fuel
        hm = make_hex_map(cp_cost=4.0)

        result = execute_move(
            unit, ["A0101", "A0102", "A0103", "A0104"], gs, hm,
            fuel_rate=4.0,
        )

        assert result.cp_spent_movement == 12.0
        assert result.fuel_consumed == 12.0  # fixture: rate=4, 12 CP → 12 fuel

    def test_no_fuel_for_non_motorized(self):
        """Rule 49.12: non-motorized units don't consume fuel."""
        unit = make_unit(motorized=False, cpa=8, cp_remaining=8.0)
        gs = make_game_state(unit)
        hm = make_hex_map(cp_cost=1.0)

        result = execute_move(unit, ["A0101", "A0102"], gs, hm, fuel_rate=4.0)

        assert result.fuel_consumed == 0.0

    def test_dp_earned_from_cpa_excess(self):
        """Rule 6.21: 1 DP per CP over CPA."""
        unit = make_unit(cpa=10, cp_remaining=10.0)
        gs = make_game_state(unit)
        gs.formation_cpa.return_value = 10
        # Move costs 13 CP → 3 over CPA → 3 DPs
        hm = make_hex_map(cp_cost=13.0)

        result = execute_move(unit, ["A0101", "A0102"], gs, hm)

        assert result.dp_earned == 3

    def test_disorganized_unit_cannot_move(self):
        """Rule 6.26: cohesion ≤ -26 → DISORGANIZED → cannot move."""
        unit = make_unit(status=UnitStatus.DISORGANIZED)
        gs = make_game_state(unit)
        hm = make_hex_map()

        result = execute_move(unit, ["A0101", "A0102"], gs, hm)

        assert result.path_taken == ["A0101"]  # didn't move
        assert result.stopped_reason is not None

    def test_non_motorized_voluntary_cap_enforced(self):
        """Rule 8.17: CPA 8 non-mot capped at 12 CP voluntary movement."""
        unit = make_unit(motorized=False, cpa=8, cp_remaining=8.0)
        gs = make_game_state(unit)
        gs.formation_cpa.return_value = 8
        # Each hex costs 3 CP; 5 hexes would be 15 CP > cap of 12
        hm = make_hex_map(cp_cost=3.0)

        result = execute_move(
            unit,
            ["A0101", "A0102", "A0103", "A0104", "A0105", "A0106"],
            gs, hm,
            context="voluntary",
        )

        # Should stop at 4 hexes (3+3+3=9 OK; next step 3 more = 12, at cap)
        # Actually: 3+3+3+3=12 CP = cap, so 4 steps allowed, 5th would exceed
        # The cap is 12 (8 × 1.5), so spending 12 is OK but 15 is not.
        assert result.cp_spent_movement <= 12.0

    def test_non_motorized_cap_not_applied_to_retreat(self):
        """Rule 8.17: cap does NOT apply to Retreat."""
        unit = make_unit(motorized=False, cpa=8, cp_remaining=8.0)
        gs = make_game_state(unit)
        gs.formation_cpa.return_value = 8
        hm = make_hex_map(cp_cost=3.0)

        # context="retreat" → no cap enforcement
        result = execute_move(
            unit,
            ["A0101", "A0102", "A0103", "A0104", "A0105", "A0106"],
            gs, hm,
            context="retreat",
        )

        # Without the cap, can spend up to 5×3=15 CP
        assert result.cp_spent_movement > 12.0


# ── validate_move_path ────────────────────────────────────────────────────────

class TestValidateMovePath:
    """validate_move_path() returns issues without mutating unit state."""

    def test_valid_path_no_issues(self):
        unit = make_unit(cpa=20, cp_remaining=20.0)
        gs = make_game_state(unit)
        hm = make_hex_map()

        issues = validate_move_path(unit, ["A0101", "A0102"], gs, hm)
        assert issues == []

    def test_eliminated_unit_fatal_issue(self):
        unit = make_unit(status=UnitStatus.ELIMINATED)
        gs = make_game_state(unit)
        hm = make_hex_map()

        issues = validate_move_path(unit, ["A0101", "A0102"], gs, hm)
        assert any(i.is_fatal for i in issues)

    def test_broken_down_unit_fatal_issue(self):
        unit = make_unit(status=UnitStatus.BROKEN_DOWN)
        gs = make_game_state(unit)
        hm = make_hex_map()

        issues = validate_move_path(unit, ["A0101", "A0102"], gs, hm)
        assert any(i.is_fatal for i in issues)

    def test_prohibited_terrain_fatal_issue(self):
        unit = make_unit()
        gs = make_game_state(unit)
        hm = make_hex_map(cp_cost="P")  # prohibited

        issues = validate_move_path(unit, ["A0101", "A0102"], gs, hm)
        assert any(i.is_fatal for i in issues)

    def test_wrong_start_hex_fatal(self):
        unit = make_unit(hex_id="A0101")
        gs = make_game_state(unit)
        hm = make_hex_map()

        issues = validate_move_path(unit, ["B0101", "B0102"], gs, hm)
        assert any(i.is_fatal for i in issues)

    def test_zoc_stop_non_fatal_issue(self):
        """Rule 8.14: ZOC stop in middle of path is non-fatal (path gets truncated)."""
        unit = make_unit(cpa=20, cp_remaining=20.0)
        gs = make_game_state(unit)
        hm = make_hex_map()

        def in_zoc(hex_id, side, units):
            return hex_id == "A0102"  # ZOC at second hex
        hm.in_enemy_zoc.side_effect = in_zoc

        issues = validate_move_path(unit, ["A0101", "A0102", "A0103"], gs, hm)
        # Non-fatal: path continues beyond ZOC hex, but it's not a fatal blocker
        zoc_issues = [i for i in issues if "ZOC" in i.reason]
        assert zoc_issues
        assert not any(i.is_fatal for i in zoc_issues)


# ── Oracle fixture spot-checks ────────────────────────────────────────────────

class TestFixtureConsistency:
    """Verify key oracle fixture values against live implementation."""

    def test_fixture_cpa15_dp3(self):
        # movement_rules.json: CPA 15, uses 18 CP → 3 DPs
        assert dp_from_cp_excess(18, 15) == 3

    def test_fixture_fuel_rate4_12cp(self):
        # movement_rules.json: rate=4, 12 CP → 12 fuel
        assert compute_fuel_consumption(12, 4) == 12.0

    def test_fixture_bd_column_21_30(self):
        # movement_rules.json: 27 BD → 21-30 column
        assert bd_column(27) == "21-30"

    def test_fixture_needs_check_column_change(self):
        # movement_rules.json: 15 BD (11-20) → 27 BD (21-30) → check required
        assert needs_breakdown_check(15, 27, had_previous_check=True) is True

    def test_fixture_no_check_same_column(self):
        # movement_rules.json: 15 BD → 17 BD, same column (11-20) → no check
        assert needs_breakdown_check(15, 17, had_previous_check=True) is False
