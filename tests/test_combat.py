"""
Tests for src/engine/combat.py.

All expected values are derived from oracle fixtures in
tests/fixtures/combat_14_0_anti_armor.json and
tests/fixtures/combat_15_0_close_assault.json,
which cite specific CNA rule numbers from cna_rules.txt.

Deferred: tests that require the Close Assault CRT or Barrage CRT table
are marked xfail with reason="table not yet extracted".
"""
import json
import math
from pathlib import Path

import pytest

from src.engine.combat import (
    anti_armor_fire,
    anti_armor_lookup,
    apply_armor_damage,
    apply_close_assault_losses,
    auto_surrender_check,
    combined_arms_actual_strength,
    compute_close_assault_column,
    compute_raw_losses,
    d66_from_dice,
    dp_from_cpa_excess,
    dp_from_losses,
    is_probe,
    org_size_column_shift,
    resolve_barrage,
    resolve_close_assault,
    retreat_loss_adjustment,
    two_x_raw_bonus,
    ArmorTarget,
    BarrageInput,
    CloseAssaultInput,
)
from src.models.unit import UnitSize

FIXTURES = Path(__file__).parent / "fixtures"


# ── d66 dice helper ───────────────────────────────────────────────────────────

class TestD66:
    def test_die1_is_always_tens(self):
        # rule 12.42 / 15.73a: die1 is the designated "large" (tens) die.
        # "2 on the large die and a 5 on the small die would be a 25" (15.73a).
        # The "large" die is a designated die (different colour), NOT the one
        # showing the higher value.
        assert d66_from_dice(3, 5) == "35"  # tens=3, ones=5 even though 3 < 5
        assert d66_from_dice(5, 3) == "53"  # die1 designation, not value ordering

    def test_doubles(self):
        assert d66_from_dice(6, 6) == "66"
        assert d66_from_dice(1, 1) == "11"

    def test_all_36_combinations_valid(self):
        # CART table has all 36 rows from 11-66 (no value-sorting)
        for d1 in range(1, 7):
            for d2 in range(1, 7):
                result = d66_from_dice(d1, d2)
                assert result == f"{d1}{d2}"

    def test_invalid_dice_raises(self):
        with pytest.raises(ValueError):
            d66_from_dice(7, 1)  # 7 is not a valid d6 face


# ── Anti-Armor CART lookup ────────────────────────────────────────────────────

class TestAntiArmorLookup:
    """Rule 14.6: CART lookup returns Damage Points (not percentages)."""

    def test_example_from_rule_14_43(self):
        # rule 14.43 example: 5 AA points, roll 35 → 7 DP.
        # die1=3 (tens/"large" die), die2=5 (ones/"small" die) → d66="35"
        result = anti_armor_lookup(5, 3, 5)
        assert result == 7

    def test_dash_result_returns_zero(self):
        # rule 14.6: '-' entries mean 0 damage
        result = anti_armor_lookup(2, 1, 1)  # CART row "11", col 2 = "-"
        assert result == 0

    def test_column_0_for_zero_strength(self):
        # rule 14.35: if terrain shifts column below 1, use column 0
        result = anti_armor_lookup(0, 6, 6)  # row "66", col 0 = 2
        assert result == 2

    def test_sixteen_plus_column(self):
        # rule 14.6: 16+ column for 16 or more AA points
        result_16 = anti_armor_lookup(16, 4, 1)
        result_20 = anti_armor_lookup(20, 4, 1)
        assert result_16 == result_20  # same "16+" column

    def test_invalid_d66_raises(self):
        with pytest.raises(ValueError):
            anti_armor_lookup(5, 7, 1)  # die1=7 is not a valid d6 face


class TestAntiArmorFire:
    """Rule 14.32: terrain shifts column left (defender benefit)."""

    def test_terrain_shift_reduces_column(self):
        # rule 14.32 example: 9 AA in rough terrain → resolves at col 8
        # CART row "25", col 9 vs col 8
        dp_no_shift, _, _ = anti_armor_fire(9, 2, 5, terrain_column_shift=0)
        dp_with_shift, _, _ = anti_armor_fire(9, 2, 5, terrain_column_shift=1)
        assert dp_with_shift <= dp_no_shift

    def test_terrain_shift_cant_go_below_zero(self):
        # rule 14.35: column clamped at 0
        dp, aa_after, _ = anti_armor_fire(2, 5, 5, terrain_column_shift=10)
        assert aa_after == 0  # clamped

    def test_high_aa_strength(self):
        # smoke test: 20 AA points treated as 16+
        dp, aa_after, _ = anti_armor_fire(20, 4, 1)
        assert dp >= 0
        assert isinstance(dp, int)


# ── Armor damage application ──────────────────────────────────────────────────

class TestApplyArmorDamage:
    """Rules 14.41–14.45: damage points → TOE loss via armor protection ratings."""

    def test_rule_14_43_example(self):
        # 7 DP, M/13s (APR 3) and M/11s (APR 2) — must absorb AT LEAST 7 DP
        targets = [
            ArmorTarget("m13", toe_strength=3, armor_protection_rating=3),
            ArmorTarget("m11", toe_strength=6, armor_protection_rating=2),
        ]
        losses, remaining = apply_armor_damage(7, targets)
        total_dp_absorbed = sum(l.dp_absorbed for l in losses)
        assert total_dp_absorbed >= 7  # rule 14.43: at least as many DP absorbed

    def test_excess_dp_ignored(self):
        # rule 14.45: DP in excess of what's needed to destroy all armor are ignored.
        # 2 TOE × APR 3 = 6 DP capacity. 20 DP − 6 DP capacity = 14 DP excess.
        # The function returns remaining_dp = 14 (the excess to be ignored by caller).
        targets = [
            ArmorTarget("tank", toe_strength=2, armor_protection_rating=3),
        ]
        losses, remaining_dp = apply_armor_damage(20, targets)
        total_toe = sum(l.toe_destroyed for l in losses)
        assert total_toe == 2           # all armor destroyed (rule 14.45)
        assert remaining_dp == 14       # 14 DP were excess; caller ignores them per 14.45

    def test_zero_damage(self):
        targets = [ArmorTarget("tank", toe_strength=5, armor_protection_rating=3)]
        losses, _ = apply_armor_damage(0, targets)
        assert all(l.toe_destroyed == 0 for l in losses)

    def test_single_target_partial_destruction(self):
        # 5 DP, 10 TOE with APR 3 → ceil(5/3) = 2 TOE destroyed
        targets = [ArmorTarget("tank", toe_strength=10, armor_protection_rating=3)]
        losses, _ = apply_armor_damage(5, targets)
        assert losses[0].toe_destroyed == 2


# ── Close Assault mechanics ───────────────────────────────────────────────────

class TestProbeDetection:
    """Rule 15.25: probe if < 50% of available TOE committed."""

    def test_exactly_50_pct_is_full_assault(self):
        assert is_probe(4, 8) is False   # exactly 50%

    def test_below_50_pct_is_probe(self):
        assert is_probe(3, 7) is True    # 43% < 50%

    def test_rule_15_25_example(self):
        # rule 15.25 example: 7 TOE, 2+1=3 committed → Probe
        assert is_probe(3, 7) is True

    def test_counter_example(self):
        # rule 15.25: 3 of 7 to hex A = Probe; 4 of 7 would be full assault
        assert is_probe(4, 7) is False


class TestCombinedArms:
    """Rule 15.4: unsupported tank reduction."""

    def test_fully_supported_no_reduction(self):
        result = combined_arms_actual_strength(
            tank_toe=3, infantry_toe=3, tank_ca_rating=6
        )
        assert result == 3 * 6  # no reduction

    def test_one_to_three_unsupported_gives_minus_one(self):
        # 2 unsupported tanks: -1 Actual
        raw = 2 * 7.0
        result = combined_arms_actual_strength(tank_toe=2, infantry_toe=0, tank_ca_rating=7)
        assert result == raw - 1

    def test_max_reduction_is_four(self):
        # rule 15.4: "in no case may the reduction be more than four Actual Assault Points"
        result = combined_arms_actual_strength(tank_toe=15, infantry_toe=0, tank_ca_rating=5)
        expected_raw = 15 * 5.0
        assert result == expected_raw - 4

    def test_four_unsupported_gives_minus_two(self):
        # 4 unsupported: ceil(4/3) = 2 reductions
        raw = 4 * 5.0
        result = combined_arms_actual_strength(tank_toe=4, infantry_toe=0, tank_ca_rating=5)
        assert result == raw - 2

    def test_non_tank_raw_added(self):
        result = combined_arms_actual_strength(
            tank_toe=3, infantry_toe=3, tank_ca_rating=4, non_tank_raw=10.0
        )
        assert result == 3 * 4.0 + 10.0


class TestOrgSizeAdjustment:
    """Rule 15.52–15.53: column shift from organizational size."""

    def test_division_vs_battalion(self):
        # rule 15.53: Division vs Battalion → +4 column shift
        shift = org_size_column_shift(UnitSize.DIVISION, UnitSize.BATTALION)
        assert shift == 4

    def test_brigade_vs_battalion(self):
        # rule 15.53: Any Brigade vs Battalion → +2 column shift
        shift = org_size_column_shift(UnitSize.BRIGADE, UnitSize.BATTALION)
        assert shift == 2

    def test_battalion_vs_company(self):
        # rule 15.53: Battalion vs Company → +2 column shift
        shift = org_size_column_shift(UnitSize.BATTALION, UnitSize.COMPANY)
        assert shift == 2

    def test_brigade_vs_company(self):
        # rule 15.53: Any Brigade vs Company → +4 column shift
        shift = org_size_column_shift(UnitSize.BRIGADE, UnitSize.COMPANY)
        assert shift == 4

    def test_equal_sizes_no_shift(self):
        # rule 15.52: shift only when one side is larger
        shift = org_size_column_shift(UnitSize.BATTALION, UnitSize.BATTALION)
        assert shift == 0


class TestTwoXRawBonus:
    """Rule 15.51: ≥ 2× raw points → +2 column shift."""

    def test_example_from_rule_15_51(self):
        # 24 Raw vs 12 Raw → 2:1 → +2 shift favours attacker
        shift, favours_att = two_x_raw_bonus(24, 12)
        assert shift == 2
        assert favours_att is True

    def test_defender_has_2x(self):
        shift, favours_att = two_x_raw_bonus(10, 24)
        assert shift == 2
        assert favours_att is False

    def test_less_than_2x_no_bonus(self):
        shift, _ = two_x_raw_bonus(15, 12)
        assert shift == 0

    def test_exactly_2x_gives_bonus(self):
        shift, favours_att = two_x_raw_bonus(24, 12)
        assert shift == 2


class TestDifferentialColumn:
    """Rule 15.26–15.27: adjusted differential from terrain, org, morale."""

    def test_basic_from_rule_15_26(self):
        # 6 actual vs 8 actual → -2 basic
        diff, _ = compute_close_assault_column(-2.0)
        assert diff == -2.0

    def test_terrain_shift_reduces_attacker(self):
        # +4 basic, 3-column terrain shift → resolves at +1 (rule 15.35 example)
        diff, detail = compute_close_assault_column(4.0, terrain_hex_shift=3)
        assert diff == 1.0
        assert detail["terrain_hex"] == -3

    def test_org_size_shift_in_attacker_favour(self):
        diff, detail = compute_close_assault_column(0.0, org_size_shift=4, org_size_favours_attacker=True)
        assert diff == 4.0
        assert detail["org_size"] == 4

    def test_cumulative_terrain_hexside_and_hex(self):
        # rule 15.35: terrain effects are cumulative (both hexside and hex)
        diff, detail = compute_close_assault_column(
            6.0,
            terrain_hex_shift=2,      # 2 cols from hex terrain
            terrain_hexside_shifts=2,  # 2 cols from hexside
        )
        assert diff == 2.0  # 6 - 2 - 2 = 2


class TestLossCalculation:
    """Rules 15.82, 15.83c, 15.77: percentage → raw losses."""

    def test_attacker_rounds_up(self):
        # rule 15.83c: 35% × 87 = 30.45 → attacker rounds UP → 31
        assert compute_raw_losses(35, 87, is_attacker=True) == 31

    def test_defender_rounds_down(self):
        # rule 15.83c: 35% × 87 = 30.45 → defender rounds DOWN → 30
        assert compute_raw_losses(35, 87, is_attacker=False) == 30

    def test_overrun_defender_rounds_up(self):
        # rule 15.77: overrun → defender also rounds UP
        assert compute_raw_losses(35, 87, is_attacker=False, is_overrun=True) == 31

    def test_zero_loss_pct(self):
        assert compute_raw_losses(0, 100, is_attacker=True) == 0

    def test_retreat_penalty(self):
        # rule 15.82: 2 hexes required, 1 taken → 1 hex penalty × 10%
        penalty = retreat_loss_adjustment(2, 1, 50)
        assert penalty == math.ceil(0.10 * 50)  # 10% of 50 = 5

    def test_no_retreat_penalty_if_fully_retreated(self):
        assert retreat_loss_adjustment(3, 3, 100) == 0


class TestDisorganizationPoints:
    """Rules 6.21 and 15.87: DP from losses and CPA excess."""

    def test_30_pct_losses_gives_3_dp(self):
        assert dp_from_losses(30, 100) == 3  # exactly 30%

    def test_above_30_pct_gives_3_dp(self):
        assert dp_from_losses(31, 100) == 3

    def test_below_30_pct_gives_0_dp(self):
        assert dp_from_losses(29, 100) == 0

    def test_cpa_excess_dp(self):
        # rule 6.21: 18 CP used, CPA 15 → 3 DP
        assert dp_from_cpa_excess(18, 15) == 3

    def test_no_cpa_excess_no_dp(self):
        assert dp_from_cpa_excess(15, 15) == 0

    def test_exact_30_pct_boundary(self):
        # 30.0 raw loss / 100.0 raw points = 30.0% → 3 DP
        assert dp_from_losses(30.0, 100.0) == 3


class TestAutoSurrender:
    """Rule 15.88: cohesion ≤ -17 or out of ammo → auto-surrender."""

    def test_cohesion_minus_17_surrenders(self):
        assert auto_surrender_check(-17, False, is_assaulted=True) is True

    def test_cohesion_minus_16_does_not_surrender(self):
        assert auto_surrender_check(-16, False, is_assaulted=True) is False

    def test_out_of_ammo_surrenders(self):
        assert auto_surrender_check(0, True, is_assaulted=True) is True

    def test_not_assaulted_no_surrender(self):
        # auto-surrender only when being assaulted (rule 15.88: "that are assaulted")
        assert auto_surrender_check(-17, True, is_assaulted=False) is False


class TestResolveCloseAssault:
    """Integration tests for resolve_close_assault()."""

    def test_no_defender_auto_retreat(self):
        # rule 15.29: defender commits 0 TOE → auto 3-hex retreat + 3 DP
        inp = CloseAssaultInput(
            attacker_raw=20.0,
            attacker_actual=20.0,
            attacker_largest_size=UnitSize.BATTALION,
            attacker_committed_toe=5,
            attacker_available_toe=5,
            defender_committed_toe=0,
        )
        result = resolve_close_assault(inp)
        assert result.no_defender_auto_retreat is True
        assert result.defender_dp_earned == 3

    def test_auto_surrender_before_resolution(self):
        # rule 15.88: cohesion ≤ -17 bypasses normal resolution
        inp = CloseAssaultInput(
            attacker_raw=10.0,
            attacker_actual=10.0,
            attacker_largest_size=UnitSize.BATTALION,
            attacker_committed_toe=3,
            attacker_available_toe=5,
            defender_cohesion=-17,
            defender_committed_toe=2,
        )
        result = resolve_close_assault(inp)
        assert result.auto_surrender is True

    def test_probe_detection_in_result(self):
        # rule 15.25: < 50% of available TOE → probe flag
        inp = CloseAssaultInput(
            attacker_raw=10.0,
            attacker_actual=10.0,
            attacker_largest_size=UnitSize.BATTALION,
            attacker_committed_toe=2,   # < 50% of 5
            attacker_available_toe=5,
            defender_raw=8.0,
            defender_actual=8.0,
            defender_committed_toe=3,
            defender_largest_size=UnitSize.BATTALION,
        )
        result = resolve_close_assault(inp)
        assert result.is_probe is True

    def test_terrain_shift_reduces_differential(self):
        inp = CloseAssaultInput(
            attacker_raw=20.0,
            attacker_actual=10.0,
            attacker_largest_size=UnitSize.BATTALION,
            attacker_committed_toe=5,
            attacker_available_toe=5,
            defender_raw=5.0,
            defender_actual=5.0,
            defender_committed_toe=3,
            defender_largest_size=UnitSize.BATTALION,
            terrain_hex_shift=3,
        )
        result = resolve_close_assault(inp)
        # basic diff = 10 - 5 = +5; terrain -3; 2x raw bonus: 20 >= 2×5 → +2
        # 2× bonus: attacker_raw=20 >= 2×defender_raw=10 → yes, +2
        assert result.basic_differential == 5.0
        expected_diff = 5.0 - 3 + 2  # terrain -3, 2× raw +2
        assert result.adjusted_differential == expected_diff

    def test_loss_pct_table_stubbed(self):
        # Until Close Assault CRT is extracted, loss_pct_table_stubbed = True
        inp = CloseAssaultInput(
            attacker_raw=20.0,
            attacker_actual=15.0,
            attacker_largest_size=UnitSize.DIVISION,
            attacker_committed_toe=5,
            attacker_available_toe=5,
            defender_raw=8.0,
            defender_actual=6.0,
            defender_committed_toe=3,
            defender_largest_size=UnitSize.BATTALION,
        )
        result = resolve_close_assault(inp)
        assert result.loss_pct_table_stubbed is True

    def test_apply_losses_after_table_lookup(self):
        # Once table values are known, apply_close_assault_losses fills them in
        inp = CloseAssaultInput(
            attacker_raw=30.0,
            attacker_actual=30.0,
            attacker_largest_size=UnitSize.BATTALION,
            attacker_committed_toe=5,
            attacker_available_toe=5,
            defender_raw=30.0,
            defender_actual=20.0,
            defender_committed_toe=5,
            defender_largest_size=UnitSize.BATTALION,
        )
        result = resolve_close_assault(inp)
        # Simulate table lookup returning 35% attacker / 30% defender losses
        result = apply_close_assault_losses(
            result,
            attacker_loss_pct=35,
            defender_loss_pct=30,
            attacker_raw=30.0,
            defender_raw=30.0,
        )
        assert result.loss_pct_table_stubbed is False
        assert result.attacker_raw_losses == math.ceil(0.35 * 30)  # 11
        assert result.defender_raw_losses == math.floor(0.30 * 30)  # 9
        # 35% of 30 = 10.5 → ceil = 11. 11/30 = 36.7% → ≥ 30% → 3 DP
        assert result.attacker_dp_earned == 3
        # 30% exactly → 3 DP
        assert result.defender_dp_earned == 3


class TestResolveBarrage:
    """Rule 12.0: barrage mechanics. Table lookup is stubbed."""

    def test_barrage_returns_stub(self):
        # CRT not extracted → result always stubbed
        inp = BarrageInput(barrage_points=10, die1=4, die2=3)
        result = resolve_barrage(inp)
        assert result.table_lookup_stubbed is True
        assert result.pinned is None
        assert result.toe_destroyed is None

    def test_terrain_shift_reduces_column(self):
        # rule 12.33: terrain shifts column left
        inp = BarrageInput(barrage_points=12, die1=4, die2=2, terrain_column_shift=2)
        result = resolve_barrage(inp)
        assert result.column_after_shift == 10  # 12 - 2

    def test_sequential_roll_die1_is_tens(self):
        # rule 12.42: die1 is the designated "large" (tens) die.
        # die1=3 (tens), die2=5 (ones) → sequential="35" even though 3 < 5.
        inp = BarrageInput(barrage_points=8, die1=3, die2=5)
        result = resolve_barrage(inp)
        assert result.sequential_roll == "35"   # die1 always tens
        assert result.sum_roll == 8


# ── Oracle fixture validation ─────────────────────────────────────────────────

class TestCARTFixtureConsistency:
    """Spot-check oracle fixture values against live CART lookup."""

    def test_fixture_example_14_43(self):
        # combat_14_0_anti_armor.json: aa=5, d66="35" → 7 DP
        result = anti_armor_lookup(5, 3, 5)
        assert result == 7  # matches oracle fixture

    def test_fixture_terrain_shift(self):
        # rule 14.32 example: 9 AA in rough terrain → column shifts to 8.
        # die1=2 (tens), die2=5 (ones) → d66="25", CART row "25" col 8 = 8 DP.
        dp, aa_after, _ = anti_armor_fire(9, 2, 5, terrain_column_shift=1)
        assert aa_after == 8
        assert dp == 8   # CART row "25" col 8 = 8

    def test_fixture_16plus_column(self):
        # fixture: aa=20 uses same col as aa=16
        dp_20, _, _ = anti_armor_fire(20, 4, 1)
        dp_16, _, _ = anti_armor_fire(16, 4, 1)
        assert dp_20 == dp_16
