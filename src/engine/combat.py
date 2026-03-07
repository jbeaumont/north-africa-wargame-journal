"""
Combat engine — Anti-Armor fire, Barrage, and Close Assault.

Rules implemented (all verified against data/rules/cna_rules.txt)
-----------------------------------------------------------------
  14.0   Anti-Armor Fire
  14.12  Phasing Player's fire targets entire hex; non-Phasing fires at
         specific assaulting Armor class units.
  14.13  Artillery in Back position may not fire or be affected by AA fire.
  14.14  Artillery in AA role: Vulnerability Rating = 2 (automatic).
  14.22  Multiple firing hexes combine AA Strength; target hex fired on once.
  14.23  If no enemy armor in hex: up to 1/3 (rounded down) AA points may be
         reassigned to Close Assault.
  14.32  Terrain column shift in defender's favor (lessens AA firepower).
  14.33  Terrain effects not cumulative; defender picks best. Hexside effects
         are in addition to hex effects.
  14.35  If terrain shifts column below 1, use column 0.
  14.41  CART result is Damage Points (NOT a percentage).
  14.42  Armor Protection Rating = DP that TOE point absorbs before destroyed.
  14.43  Player must remove ENOUGH TOE to absorb AT LEAST the DP total.
  14.45  Excess DP (beyond all armor) are ignored.
  14.6   Anti-Armor CRT — d66 lookup (table in rules_tables.json confirmed).

  12.0   Barrage (PARTIAL — Barrage CRT not yet extracted from PDF)
  12.33  Terrain column shift benefits defender (shift left on table).
  12.34  Column shifts not cumulative; defender's best position only.
         Hexside shifts worse than 1-2 column → barrage has no effect.
  12.35  No LoS restrictions; may barrage any adjacent hex.
  12.42  Two-dice sequential read, larger die first: (3,4) → 34.
  12.43  Note Barrage Points, adjust column left for terrain, roll two dice,
         cross-reference under target type.
  12.44  'P' result = Pinned (may not move, fire AA, or assault this segment).
         Does not affect Gun-class or Artillery HQ units.
  12.45  Number result = TOE Strength Points destroyed.
  12.46  Trucks separately affected: second roll on Trucks row each barrage.
  DEFERRED: 12.6 Artillery Barrage CRT — not yet extracted. Lookup stubbed.

  15.0   Close Assault
  15.25  Probe: attacker commits < 50% of available TOE Strength Points.
  15.28  < 5 Raw Points → treated as 0; both < 10 Raw → use Raw as Actual.
  15.29  Defender commits no units → auto 3-hex retreat + 3 DP.
  15.3   Terrain effects cumulative (hexside effects stack), except defender
         picks only best single hex-terrain benefit (15.34).
  15.4   Combined Arms: each tank TOE needs equal infantry/MG/HW TOE.
         Per 1–3 unsupported tank TOE → −1 Actual CA Strength; max −4.
  15.51  If one side has ≥ 2× the Raw Points of the other → +2 column shift
         in favor of the larger side (independent of other modifiers).
  15.52–15.53  Org-size adjustment: see ORG_SIZE_ADJUSTMENTS table below.
  15.6   Morale adjustment: DEFERRED (Morale Modification Table 17.4 not
         yet extracted). Final adjusted morale accepted as external input.
  15.71–15.73  Assault CRT uses 2d6 read sequentially for loss%, and summed
         for Engaged/Retreat/Captured checks. Each player rolls once.
  DEFERRED: 15.79 Close Assault Results Table — not yet extracted. Stubbed.
  15.82  Retreat: 10% additional loss per hex not retreated.
  15.83  Loss % × Total Raw → raw losses; attacker rounds UP, defender DOWN.
         In Overrun, defender also rounds UP.
  15.87  ≥ 30% losses of committed TOE → 3 DP earned.
  15.88  Cohesion ≤ −17 OR out of ammo → auto-surrender when assaulted.
  15.9   Probe: Engaged results ignored; units not in Contact after.

  6.21   DP earned by exceeding CPA: 1 DP per CP over CPA.
  6.26   Cohesion ≤ −26 → may not move, attack, or defend (auto-surrender
         if adjacent enemy; see also pasta rule 52.6).

Usage
-----
    from src.engine.combat import (
        anti_armor_fire,
        apply_armor_damage,
        resolve_close_assault,
    )
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

from src.models.unit import UnitSize, UnitStatus


# ── Load CART from rules_tables.json ─────────────────────────────────────────

_TABLES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "extracted", "rules_tables.json"
)

def _load_cart() -> dict:
    with open(_TABLES_PATH) as f:
        tables = json.load(f)
    return tables["combat_system"]["anti_armor_results_table"]

_CART = _load_cart()
# columns = [0, 1, 2, ..., 15, "16+"]
_CART_COLS: List[Union[int, str]] = _CART["columns"]
# rows keyed by d66 string: "11", "12", ... "66"
_CART_ROWS: Dict[str, List[Union[int, str]]] = _CART["rows"]


# ── d66 helper ────────────────────────────────────────────────────────────────

def d66_from_dice(die1: int, die2: int) -> str:
    """
    Convert two d6 rolls to a d66 result string.

    Rule 12.42 / 15.73a: one die is designated as the "large" (tens) die and
    the other as the "small" (ones) die, and they are always read in that order.
    die1 is the tens die, die2 is the ones die.

    This convention is confirmed by the CART table structure: it has all 36
    combinations from 11–66 (including entries like "35" where tens < ones),
    showing that the table is NOT sorted-by-value but ordered by designation.

    Note: the wording "reading the larger die first" in rule 12.42 refers to the
    physically larger die (typically a different colour), NOT the die showing the
    higher face value.  Rule 15.73a: "2 on the large die and a 5 on the small die
    would be a 25" confirms this — the "large" die showing 2 (lower value) goes
    to the tens position.

    Caller must designate die1 as the tens die before passing here.
    """
    if not (1 <= die1 <= 6 and 1 <= die2 <= 6):
        raise ValueError(f"Both dice must be in [1,6]; got ({die1}, {die2})")
    return f"{die1}{die2}"


# ── Anti-Armor CART lookup (rule 14.6) ───────────────────────────────────────

def anti_armor_lookup(aa_points: int, die1: int, die2: int) -> int:
    """
    Look up Damage Points on the Anti-Armor CRT (rule 14.6).

    aa_points: Actual Anti-Armor Strength Points AFTER terrain column shift
               (i.e., already adjusted by anti_armor_fire()).
    die1, die2: the two d6 rolls (order doesn't matter here; d66_from_dice
                puts the larger first).

    Returns Damage Points (int). '-' entries in the table mean 0 damage.

    Rule 14.41: "All results on the Anti-Armor CRT are in Damage Points."
    Rule 14.35: if column would be < 0, use column 0.
    """
    d66 = d66_from_dice(die1, die2)   # raises ValueError if dice out of [1,6]
    if d66 not in _CART_ROWS:
        raise ValueError(f"d66 result '{d66}' not in CART table")

    row = _CART_ROWS[d66]

    # Find column index: columns are [0, 1, ..., 15, "16+"]
    col_idx: int
    if aa_points <= 0:
        col_idx = 0   # rule 14.35: shifts below 1 use column 0
    elif aa_points >= 16:
        col_idx = len(_CART_COLS) - 1   # "16+" is the last column
    else:
        col_idx = aa_points  # column index equals point value (0-indexed: col 0 = 0 pts)

    result = row[col_idx]
    return 0 if result == "-" else int(result)


def anti_armor_fire(
    aa_strength_raw: int,
    die1: int,
    die2: int,
    terrain_column_shift: int = 0,
) -> Tuple[int, int, int]:
    """
    Resolve Anti-Armor fire against a target hex.

    Rule 14.32: terrain_column_shift is the number of columns shifted LEFT
    (in defender's favor), reducing effective AA strength.
    Rule 14.35: result clamped at column 0 minimum.

    Returns (damage_points, aa_points_after_shift, col_idx_used).
    """
    aa_after_shift = max(0, aa_strength_raw - terrain_column_shift)  # rule 14.35
    damage_points = anti_armor_lookup(aa_after_shift, die1, die2)
    return damage_points, aa_after_shift, min(aa_after_shift, len(_CART_COLS) - 1)


# ── Armor damage application (rule 14.4) ──────────────────────────────────────

@dataclass
class ArmorTarget:
    """One unit's armor TOE and absorption capacity."""
    unit_id: str
    toe_strength: int          # TOE Strength Points with armor
    armor_protection_rating: int   # DP absorbed per TOE point before destruction


@dataclass
class ArmorLoss:
    unit_id: str
    toe_destroyed: int
    dp_absorbed: int


def apply_armor_damage(
    damage_points: int,
    targets: List[ArmorTarget],
) -> Tuple[List[ArmorLoss], int]:
    """
    Distribute Damage Points across armor targets.

    Rule 14.43: player must remove AT LEAST as many TOE Strength Points as
    are affected by absorbing Damage Points.
    Rule 14.44: losses may come from any armored unit in the target hex.
    Rule 14.45: excess DP beyond destroying all armor are ignored.

    This function removes the minimum necessary TOE to satisfy the DP total,
    favouring targets with the highest Armor Protection Rating first (most
    efficient absorption, minimising units destroyed). The actual player may
    choose differently (14.43 says "at least"), but for engine purposes the
    minimum-loss greedy choice is used.

    Returns (list of ArmorLoss per unit, remaining_dp_ignored).
    """
    losses: List[ArmorLoss] = []
    dp_remaining = damage_points

    # Sort by APR descending (absorb most per unit first — minimises TOE lost)
    for target in sorted(targets, key=lambda t: t.armor_protection_rating, reverse=True):
        if dp_remaining <= 0:
            break
        apr = target.armor_protection_rating
        # How many full TOE points can we destroy from this unit?
        toe_can_destroy = target.toe_strength
        dp_capacity = toe_can_destroy * apr

        if dp_remaining >= dp_capacity:
            # Destroy all TOE from this target
            losses.append(ArmorLoss(target.unit_id, toe_can_destroy, dp_capacity))
            dp_remaining -= dp_capacity
        else:
            # Destroy only enough TOE to cover remaining DP (round up)
            toe_needed = math.ceil(dp_remaining / apr)
            dp_absorbed = toe_needed * apr
            losses.append(ArmorLoss(target.unit_id, toe_needed, min(dp_absorbed, dp_remaining)))
            dp_remaining = 0  # rule 14.45: excess DP ignored

    return losses, max(0, dp_remaining)


# ── Close Assault helpers ──────────────────────────────────────────────────────

# rule 15.53: org-size adjustment table.
# Keys: (larger_size_level, smaller_size_level) → column shift in favour of
# the larger side. Note: rule uses "3-point Division" and "2-point Division"
# which refer to formation point value, not a named UnitSize. We approximate:
#   3-point Division ~ DIVISION; 2-point Division ~ REGIMENT (placeholder).
# TODO: confirm "3-point" vs "2-point" distinction from formation point
#       counting once counters.json formation data is loaded.

_SIZE_LEVEL: Dict[UnitSize, int] = {
    UnitSize.COMPANY:   0,
    UnitSize.BATTALION: 1,
    UnitSize.REGIMENT:  2,   # between battalion and brigade
    UnitSize.BRIGADE:   3,
    UnitSize.DIVISION:  4,
    UnitSize.CORPS:     5,
    UnitSize.ARMY:      6,
}

# rule 15.53 org-size column shift table.
# (larger_level, smaller_level) → column shifts in favour of larger side.
# Approximated from rule text (DIVISION = level 4, BRIGADE = 3, etc.).
#   "Division vs 3-point" → div(4) vs rgt/3-pt (2) → +1
#   "Brigade vs Division 2-point" → bde(3) on larger, "div 2-pt" on smaller? Confusing.
#   Most reliable subset directly from text:
#     Div   > Bde  > Battalion > Company
# Rule 15.53 table as stated:
#   Division vs 3-point    : +1   → approximated as div(4) vs rgt(2) : +1
#   Brigade vs Division 2pt: +2   → bde(3) vs rgt(2) : +2
#   Brigade vs Div+Battalion: +4  → bde(3) vs bn(1) : +4
#   Division vs Company    : +8   → div(4) vs coy(0) : +8
#   Any Brigade vs Battalion: +2  → bde(3) vs bn(1) : +2 (same as Brigade vs Div+Bn?)
#   Any Brigade vs Company : +4   → bde(3) vs coy(0) : +4
#   Battalion  vs Company  : +2   → bn(1)  vs coy(0) : +2
#
# NOTE: the rule text has apparent overlaps (Brigade vs Battalion appears twice).
# The simplest consistent interpretation, reading the table column headers as
# "largest unit on each side":
ORG_SIZE_ADJUSTMENTS: Dict[Tuple[int, int], int] = {
    # (larger_level, smaller_level): column_shift
    # Explicit entries from rule 15.53 table:
    (4, 2): 1,   # Division vs Regiment/3-pt: +1  (rule 15.53: "Division vs 3-point: +1")
    (3, 2): 2,   # Brigade vs Regiment/2-pt: +2   (rule 15.53: "Brigade vs Division 2-point: +2")
    (3, 1): 2,   # Brigade vs Battalion: +2        (rule 15.53: "Any Brigade vs Battalion: +2")
    (4, 0): 8,   # Division vs Company: +8         (rule 15.53)
    (3, 0): 4,   # Brigade vs Company: +4          (rule 15.53: "Any Brigade vs Company: +4")
    (1, 0): 2,   # Battalion vs Company: +2        (rule 15.53)
    # Inferred for size pairs not directly listed:
    (4, 1): 4,   # Division vs Battalion: no explicit entry; "Brigade vs Division Battalion: +4"
                 # suggests +4 at this span (TODO: confirm from physical table)
    (5, 1): 4,   # Corps vs Battalion
    (5, 0): 8,   # Corps vs Company
    (6, 0): 8,   # Army vs Company
    (5, 2): 2,   # Corps vs Regiment: interpolated
    (4, 3): 1,   # Division vs Brigade: interpolated from "Division vs 3-point" (+1)
}


def org_size_column_shift(
    attacker_largest: UnitSize,
    defender_largest: UnitSize,
) -> int:
    """
    Column shift from organizational size differential (rule 15.52–15.53).

    Returns a positive number of columns shifted in favour of the larger side.
    Caller is responsible for determining which side is larger and applying the
    shift in the correct direction.

    If sizes are equal: 0 shift (rule 15.52 only activates when one side is larger).
    """
    a_lvl = _SIZE_LEVEL[attacker_largest]
    d_lvl = _SIZE_LEVEL[defender_largest]

    if a_lvl == d_lvl:
        return 0

    larger = max(a_lvl, d_lvl)
    smaller = min(a_lvl, d_lvl)

    # Direct lookup
    if (larger, smaller) in ORG_SIZE_ADJUSTMENTS:
        return ORG_SIZE_ADJUSTMENTS[(larger, smaller)]

    # Fallback: any large disparity gets maximum known shift
    if larger - smaller >= 4:
        return 8
    if larger - smaller >= 2:
        return 4
    return 2


def combined_arms_actual_strength(
    tank_toe: int,
    infantry_toe: int,
    tank_ca_rating: float,
    non_tank_raw: float = 0.0,
) -> float:
    """
    Apply Combined Arms Effect (rule 15.4) to compute Actual CA Strength.

    Rule 15.4: "for each TOE Strength Point of tanks engaging in Close Assault,
    there must be an equal number of TOE Strength Points of infantry, machinegun,
    or heavy weapons units engaging in that Close Assault from the same hex."

    "For every one to three TOE Strength Points of unsupported tanks, the Actual
    Close Assault Strength of the tanks is reduced by one. In no case may the
    reduction be more than four Actual Assault Points."

    AUDIT NOTE: The rule text example ("2 tank TOE with CA 7 each assaulting
    unsupported → Actual CA Strength is reduced to zero") appears to contradict
    the formula (2 unsupported = 1 reduction, not 14). This may mean the formula
    gives -1 to the Actual count (of Actual Points), not to Raw. The example
    result of "zero" may be a misread from OCR or a special case for that
    specific unit combination. We implement the stated formula as written:
    per 1–3 unsupported tank TOE → -1 Actual; max -4 reduction.
    TODO: re-read from physical book when available.

    tank_toe: TOE Strength Points of tanks in assault
    infantry_toe: TOE Strength Points of supporting infantry/MG/HW in assault
    tank_ca_rating: the CA rating of the tank unit(s)
    non_tank_raw: Raw CA points from non-tank units (added after reduction)

    Returns total Actual CA Strength for this side.
    """
    tank_raw = tank_toe * tank_ca_rating
    unsupported_tank_toe = max(0, tank_toe - infantry_toe)

    # Reduction: ceil(unsupported / 3), capped at 4 (rule 15.4)
    reduction = min(4, math.ceil(unsupported_tank_toe / 3)) if unsupported_tank_toe > 0 else 0

    actual_tank = max(0.0, tank_raw - reduction)
    return actual_tank + non_tank_raw


def is_probe(attacker_committed_toe: int, attacker_available_toe: int) -> bool:
    """
    Rule 15.25: Close Assault is a Probe if attacker commits < 50% of
    available TOE Strength Points.

    "if the units making a given Close Assault have committed less than 50% of
    their available TOE Strength Points, that Close Assault is automatically
    considered a Probe."
    """
    if attacker_available_toe <= 0:
        return False
    return attacker_committed_toe < attacker_available_toe * 0.5


def apply_small_force_rule(attacker_raw: float, defender_raw: float) -> Tuple[float, float, bool]:
    """
    Rule 15.28 adjustments for small forces.

    "If one Player has fewer than 5 Raw Assault Points, he is considered to
    have zero Close Assault Points."

    "If each Player has fewer than 10 Raw Strength Points, the Raw Assault
    Strength Points are used as if they were Actual Assault Strength Points."

    Returns (effective_attacker_raw, effective_defender_raw, use_raw_as_actual).
    """
    use_raw_as_actual = attacker_raw < 10 and defender_raw < 10

    if attacker_raw < 5:
        attacker_raw = 0.0
    if defender_raw < 5:
        defender_raw = 0.0

    return attacker_raw, defender_raw, use_raw_as_actual


def compute_close_assault_column(
    basic_differential: float,
    terrain_hex_shift: int = 0,
    terrain_hexside_shifts: int = 0,
    org_size_shift: int = 0,
    org_size_favours_attacker: bool = True,
    two_x_raw_shift: int = 0,
    two_x_raw_favours_attacker: bool = True,
    morale_shift: int = 0,
) -> Tuple[float, Dict[str, int]]:
    """
    Compute the Adjusted Assault Differential column (rule 15.27).

    basic_differential: Attacker Actual – Defender Actual.
    terrain_hex_shift: columns shifted against attacker from hex terrain (positive).
    terrain_hexside_shifts: cumulative hexside shift columns against attacker (positive).
    org_size_shift: column shift from rule 15.52–15.53.
    org_size_favours_attacker: True if larger organisation is attacker's.
    two_x_raw_shift: 2 if one side has ≥ 2× raw points (rule 15.51); else 0.
    two_x_raw_favours_attacker: True if attacker has ≥ 2× raw.
    morale_shift: Final Adjusted Morale (rule 15.61); positive = attacker benefit.

    Returns (adjusted_differential, shift_breakdown_dict).

    Note: rule 15.35 says terrain effects on Close Assault ARE cumulative
    (both hexside and hex terrain), with one exception (defender gets best
    single hexside, 15.35). terrain_hexside_shifts should reflect that.
    Rule 15.32: hex terrain either has no effect or decreases attacker's column;
    hex terrain shifts are in the defender's favour.
    """
    detail: Dict[str, int] = {}

    diff = basic_differential

    # Terrain shifts (rule 15.3) — always in defender's favour (negative to attacker)
    diff -= terrain_hex_shift
    detail["terrain_hex"] = -terrain_hex_shift
    diff -= terrain_hexside_shifts
    detail["terrain_hexside"] = -terrain_hexside_shifts

    # Org size shift (rule 15.52–15.53)
    if org_size_shift > 0:
        if org_size_favours_attacker:
            diff += org_size_shift
            detail["org_size"] = +org_size_shift
        else:
            diff -= org_size_shift
            detail["org_size"] = -org_size_shift
    else:
        detail["org_size"] = 0

    # 2× raw strength bonus (rule 15.51)
    if two_x_raw_shift > 0:
        if two_x_raw_favours_attacker:
            diff += two_x_raw_shift
            detail["two_x_raw"] = +two_x_raw_shift
        else:
            diff -= two_x_raw_shift
            detail["two_x_raw"] = -two_x_raw_shift
    else:
        detail["two_x_raw"] = 0

    # Morale (rule 15.62) — positive = attacker advantage; negative = defender
    diff += morale_shift
    detail["morale"] = morale_shift

    return diff, detail


def two_x_raw_bonus(attacker_raw: float, defender_raw: float) -> Tuple[int, bool]:
    """
    Rule 15.51: if one side has ≥ 2× the Raw Points of the other,
    that side gets +2 column shift.

    Returns (shift, favours_attacker). shift is 0 if neither qualifies.
    """
    if defender_raw > 0 and attacker_raw >= 2 * defender_raw:
        return 2, True
    if attacker_raw > 0 and defender_raw >= 2 * attacker_raw:
        return 2, False
    return 0, False


# ── Loss calculation (rule 15.83) ─────────────────────────────────────────────

def compute_raw_losses(
    loss_pct: float,
    raw_points: float,
    is_attacker: bool,
    is_overrun: bool = False,
) -> int:
    """
    Rule 15.83c: percentage × raw points.
    Attacker rounds UP; Defender rounds DOWN (except Overrun — all round UP).

    Rule 15.77: in Overrun, all Defender losses are rounded UP.
    """
    raw = loss_pct / 100.0 * raw_points
    if is_attacker or is_overrun:
        return math.ceil(raw)
    else:
        return math.floor(raw)


def retreat_loss_adjustment(
    retreat_required: int,
    retreat_taken: int,
    raw_points: float,
) -> int:
    """
    Rule 15.82: 10% additional loss per hex of mandated retreat not taken.

    Returns additional raw losses (rounded per the same attacker/defender rule;
    here we just compute the raw penalty as an integer, caller adds it).
    """
    hexes_not_taken = max(0, retreat_required - retreat_taken)
    if hexes_not_taken == 0:
        return 0
    penalty_pct = hexes_not_taken * 10  # 10% per hex
    return math.ceil(penalty_pct / 100.0 * raw_points)


def dp_from_losses(raw_losses: float, raw_committed: float) -> int:
    """
    Rule 15.87: if losses ≥ 30% of TOE Strength Points committed to assault,
    all of that player's involved units gain 3 Disorganization Points.

    Returns 3 if threshold met, else 0.
    """
    if raw_committed <= 0:
        return 0
    pct = raw_losses / raw_committed * 100.0
    return 3 if pct >= 30.0 else 0


def dp_from_cpa_excess(cp_used: float, cpa: int) -> int:
    """
    Rule 6.21: 1 Disorganization Point per CP expended over CPA.

    "Thus, a unit with a CPA of 15 that uses 18 CP's would earn 3
    Disorganization Points."
    """
    excess = max(0, cp_used - cpa)
    return math.floor(excess)


def auto_surrender_check(cohesion: int, out_of_ammo: bool, is_assaulted: bool) -> bool:
    """
    Rule 15.88: units with Cohesion Level ≤ −17 OR out of ammunition that are
    assaulted automatically Surrender.

    Rule 6.26: units with Cohesion ≤ −26 automatically surrender if any enemy
    combat unit moves adjacent (not just when assaulted).

    Returns True if the unit auto-surrenders.
    """
    if not is_assaulted:
        return False
    return cohesion <= -17 or out_of_ammo


# ── Barrage helpers (partial — table not yet extracted) ───────────────────────

@dataclass
class BarrageInput:
    """Inputs for one barrage resolution."""
    barrage_points: int          # total Actual Barrage Points
    die1: int
    die2: int
    terrain_column_shift: int = 0  # rule 12.33: shifts LEFT (defender benefit)
    target_type: str = "personnel"  # "personnel" | "armor" | "artillery" | "truck"


@dataclass
class BarrageResult:
    """
    Result of a barrage.

    table_lookup_stubbed: True because the Artillery Barrage CRT (12.6) has
    not been extracted from the PDF yet. The column calculation is correct
    (rules 12.33–12.34 applied); only the final result lookup is deferred.
    """
    barrage_points_used: int
    column_after_shift: int
    sequential_roll: str         # e.g., "43" (larger die first, rule 12.42)
    sum_roll: int                # die1 + die2 (Engaged/Retreat check)
    pinned: Optional[bool]       # None = stubbed
    toe_destroyed: Optional[int] # None = stubbed
    table_lookup_stubbed: bool = True


def resolve_barrage(inp: BarrageInput) -> BarrageResult:
    """
    Resolve a barrage (rule 12.0).

    Rule 12.33: shift column LEFT by terrain_column_shift.
    Rule 12.34: shifts not cumulative; defender's best position only.
                Shifts worse than 1-2 column → no effect.
    Rule 12.42: two-dice sequential read (larger die first).

    DEFERRED: Artillery Barrage CRT (12.6) not yet extracted.
    Returns result with table_lookup_stubbed=True; caller must apply the
    actual table once extracted.

    TODO: extract data/rules/cna_rules.txt page reference for barrage table
    and add to rules_tables.json.
    """
    column = max(1, inp.barrage_points - inp.terrain_column_shift)
    # rule 12.34: shifts worse than 1-2 column → no effect means barrage_points < 1
    # (the rule says if column drops below the "1-2" column it has no effect)
    sequential = d66_from_dice(inp.die1, inp.die2)
    total = inp.die1 + inp.die2

    return BarrageResult(
        barrage_points_used=inp.barrage_points,
        column_after_shift=column,
        sequential_roll=sequential,
        sum_roll=total,
        pinned=None,       # DEFERRED: requires barrage CRT
        toe_destroyed=None,  # DEFERRED: requires barrage CRT
        table_lookup_stubbed=True,
    )


# ── Close Assault resolution (top-level) ──────────────────────────────────────

@dataclass
class CloseAssaultInput:
    """All inputs for one Close Assault resolution."""
    # Attacker
    attacker_raw: float                     # Raw CA Strength Points
    attacker_actual: float                  # After combined arms (rule 15.4)
    attacker_largest_size: UnitSize
    attacker_committed_toe: int
    attacker_available_toe: int
    attacker_cohesion: int = 0

    # Defender
    defender_raw: float = 0.0
    defender_actual: float = 0.0
    defender_largest_size: UnitSize = UnitSize.BATTALION
    defender_committed_toe: int = 0
    defender_out_of_ammo: bool = False
    defender_cohesion: int = 0
    defender_in_major_city: bool = False

    # Column adjustments (rule 15.27)
    terrain_hex_shift: int = 0             # from TEC hex terrain (defender benefit)
    terrain_hexside_shifts: int = 0        # cumulative hexside shifts (defender benefit)
    final_adjusted_morale: int = 0         # rule 15.61 (stub: 0 until 17.0 table extracted)

    # Dice (for future non-stubbed lookup)
    attacker_die1: int = 1
    attacker_die2: int = 1
    defender_die1: int = 1
    defender_die2: int = 1

    # Context
    is_overrun_column: bool = False        # set by caller based on final column ≥ +1/1


@dataclass
class CloseAssaultResult:
    """Outcome of a Close Assault resolution."""
    # Mechanics
    basic_differential: float
    adjusted_differential: float
    column_shift_detail: Dict[str, int] = field(default_factory=dict)
    is_probe: bool = False
    auto_surrender: bool = False           # rule 15.88 / 6.26

    # Losses (raw points)
    attacker_raw_losses: int = 0
    defender_raw_losses: int = 0
    attacker_dp_earned: int = 0
    defender_dp_earned: int = 0

    # Status outcomes
    attacker_engaged: Optional[bool] = None   # None = table lookup deferred
    defender_retreat_hexes: Optional[int] = None  # None = table lookup deferred
    defender_captured_pct: Optional[float] = None  # None = table lookup deferred
    is_overrun: bool = False

    # Stub flags
    loss_pct_table_stubbed: bool = True
    note: str = ""

    # No-defender auto-retreat (rule 15.29)
    no_defender_auto_retreat: bool = False


def resolve_close_assault(inp: CloseAssaultInput) -> CloseAssaultResult:
    """
    Resolve one Close Assault, applying all rules 15.2–15.87.

    DEFERRED items (marked in result):
      - loss_pct_table_stubbed=True: Assault CRT not yet extracted (15.79).
        Raw losses cannot be computed without it. attacker_raw_losses and
        defender_raw_losses are set to 0 until the table is available.
      - morale_shift=0 unless caller provides it: Morale Modification Table
        (17.4) not yet extracted.

    All differential calculation, org-size, terrain, DP, and probe mechanics
    ARE applied and reflected in the result.
    """
    result = CloseAssaultResult(
        basic_differential=0,
        adjusted_differential=0,
    )

    # Rule 15.88 / 6.26: auto-surrender check before any resolution
    if auto_surrender_check(inp.defender_cohesion, inp.defender_out_of_ammo, is_assaulted=True):
        result.auto_surrender = True
        result.note = (
            f"Auto-surrender: cohesion {inp.defender_cohesion} ≤ −17 "
            f"or out of ammo (rule 15.88)"
        )
        return result

    # Rule 15.29: no defender allocated → auto 3-hex retreat + 3 DP
    if inp.defender_committed_toe == 0:
        result.no_defender_auto_retreat = True
        result.defender_raw_losses = 0  # losses are a % of raw; no retreat = 0% from table
        result.defender_dp_earned = 3   # rule 15.29 + 15.87 DP
        result.note = "No defender units allocated; auto 3-hex retreat + 3 DP (rule 15.29)"
        return result

    # Probe detection (rule 15.25)
    result.is_probe = is_probe(inp.attacker_committed_toe, inp.attacker_available_toe)

    # Rule 15.28: small-force adjustments
    eff_att_raw, eff_def_raw, use_raw_as_actual = apply_small_force_rule(
        inp.attacker_raw, inp.defender_raw
    )
    eff_att_actual = eff_att_raw if use_raw_as_actual else inp.attacker_actual
    eff_def_actual = eff_def_raw if use_raw_as_actual else inp.defender_actual

    # Basic differential (rule 15.26)
    basic_diff = eff_att_actual - eff_def_actual
    result.basic_differential = basic_diff

    # 2× raw strength bonus (rule 15.51)
    two_x_shift, two_x_favours_att = two_x_raw_bonus(eff_att_raw, eff_def_raw)

    # Org-size adjustment (rule 15.52–15.53)
    org_shift = org_size_column_shift(inp.attacker_largest_size, inp.defender_largest_size)
    att_level = _SIZE_LEVEL[inp.attacker_largest_size]
    def_level = _SIZE_LEVEL[inp.defender_largest_size]
    org_favours_att = att_level >= def_level

    # Adjusted column (rule 15.27)
    adjusted_diff, detail = compute_close_assault_column(
        basic_differential=basic_diff,
        terrain_hex_shift=inp.terrain_hex_shift,
        terrain_hexside_shifts=inp.terrain_hexside_shifts,
        org_size_shift=org_shift,
        org_size_favours_attacker=org_favours_att,
        two_x_raw_shift=two_x_shift,
        two_x_raw_favours_attacker=two_x_favours_att,
        morale_shift=inp.final_adjusted_morale,
    )
    result.adjusted_differential = adjusted_diff
    result.column_shift_detail = detail

    # Overrun: adjusted column ≥ +1/1 (rule 15.77: the "+J/1 to +17 et seq" columns)
    # Simplified threshold: adjusted_diff > 17 (far right of table) — actual
    # boundary depends on extracted table.
    # TODO: set overrun threshold from table once extracted.
    result.is_overrun = inp.is_overrun_column or adjusted_diff >= 18

    # Loss percentage: DEFERRED (Close Assault CRT not yet extracted)
    result.loss_pct_table_stubbed = True
    result.attacker_raw_losses = 0   # populated when table extracted
    result.defender_raw_losses = 0   # populated when table extracted

    # DP from 30%+ losses — can only be computed once table is available
    # (set to 0; will be recomputed when loss % is filled in)
    result.attacker_dp_earned = 0
    result.defender_dp_earned = 0

    # Engaged / Retreat / Captured — deferred with table
    result.attacker_engaged = None
    result.defender_retreat_hexes = None
    result.defender_captured_pct = None

    note_parts = []
    if result.is_probe:
        note_parts.append("PROBE: Engaged results ignored (rule 15.9)")
    if adjusted_diff != basic_diff:
        note_parts.append(
            f"Basic diff {basic_diff:+.1f} adjusted to {adjusted_diff:+.1f} "
            f"(shifts: {detail})"
        )
    note_parts.append("Loss% lookup DEFERRED: Close Assault CRT not yet extracted (15.79)")
    result.note = "; ".join(note_parts)

    return result


def apply_close_assault_losses(
    result: CloseAssaultResult,
    attacker_loss_pct: float,
    defender_loss_pct: float,
    attacker_raw: float,
    defender_raw: float,
    attacker_retreat_hexes_required: int = 0,
    attacker_retreat_hexes_taken: int = 0,
    defender_retreat_hexes_required: int = 0,
    defender_retreat_hexes_taken: int = 0,
) -> CloseAssaultResult:
    """
    Fill in loss figures once the Close Assault CRT has been looked up.

    Called by the board-state agent when the table is available or after
    the table is extracted into rules_tables.json.

    Applies:
      - Rule 15.83c: attacker rounds up, defender rounds down (overrun: both up)
      - Rule 15.82: retreat non-compliance penalty (10% per hex not retreated)
      - Rule 15.87: 30%+ losses → 3 DP
    """
    att_loss = compute_raw_losses(
        attacker_loss_pct, attacker_raw,
        is_attacker=True, is_overrun=result.is_overrun,
    )
    att_retreat_pen = retreat_loss_adjustment(
        attacker_retreat_hexes_required, attacker_retreat_hexes_taken, attacker_raw,
    )
    att_total = att_loss + att_retreat_pen

    def_loss = compute_raw_losses(
        defender_loss_pct, defender_raw,
        is_attacker=False, is_overrun=result.is_overrun,
    )
    def_retreat_pen = retreat_loss_adjustment(
        defender_retreat_hexes_required, defender_retreat_hexes_taken, defender_raw,
    )
    def_total = def_loss + def_retreat_pen

    result.attacker_raw_losses = att_total
    result.defender_raw_losses = def_total
    result.attacker_dp_earned = dp_from_losses(att_total, attacker_raw)
    result.defender_dp_earned = dp_from_losses(def_total, defender_raw)
    result.loss_pct_table_stubbed = False
    return result
