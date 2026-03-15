"""
Movement engine — CP expenditure, fuel consumption, and breakdown tracking.

Rules implemented (all verified against data/rules/cna_rules.txt)
-----------------------------------------------------------------
  6.11   Gun units with CPA '0' are treated as CPA 10 for all non-movement
         purposes.  For movement: CPA 0 units cannot move.
  6.13   CPA = max CPs usable in one OpStage without earning DPs.
  6.15   Formation CPA = LOWEST CPA of its constituent units (upward flow).
         WARNING: current Formation.cpa in game_state.py flows the wrong
         direction (downward, unit inherits from formation). This is a known
         placeholder — see GameState.formation_cpa() TODO comment. This
         module resolves unit CPA via GameState.formation_cpa() until the
         counter loader is built.
  6.16   Unused CPs do NOT carry over to the next OpStage.
  6.21   Exceeding CPA: 1 DP per CP over CPA.  Applied immediately (rule 6.22).
  8.11   Voluntary movement only during Movement Segments or Retreat Before
         Assault Step.
  8.12   Involuntary movement (Retreat, Reaction) also costs CPs and triggers
         Breakdown checks (rule 21.0).
  8.13   Unit may never enter a hex containing an enemy unit (rule 27.4 aside).
         Movement must be consecutive; units may not skip hexes.
  8.14   Unit entering enemy ZOC must stop immediately.
  8.15   Exiting ZOC from Contact: 2 CP.  From Engaged: 4 CP (added before move).
  8.16   Units may exceed CPA; excess earns DPs (rule 6.21).
  8.17   Non-motorized units (CPA ≤ 10) may never voluntarily spend > 150% of
         base CPA: max = base_cpa * 1.5.  Does NOT apply to Reaction/Retreat.

  21.21  Each terrain hex has a Breakdown Point (BD) Value (TEC 8.37).
         Hexsides also have BD values (Wadi, Escarpment, etc.).
  21.22  Combat does not cause BD; all movement (including Retreat/Reaction) does.
  21.23  All vehicles in a stack/formation accumulate BD for terrain moved through.
  21.24  Breakdown check when unit CEASES movement.
  21.25  BD is cumulative across the whole OpStage (both players' portions).
  21.26  Second check required only if BD pushes into a HIGHER table column.
  21.27  No check until BD > 3 (strictly greater than 3).
  21.31  Breakdown Table columns: 4-10, 11-20, 21-30, 31-40, 41-50, 51-60,
         61-70, 71+.  (See also 21.33: 71+ is the ceiling column.)
  DEFERRED: 21.38 Breakdown Table — not yet extracted from PDF. The dice roll
         is stubbed; BD column and check-needed logic are fully implemented.

  49.12  Every vehicle consumes fuel when it moves (exceptions: desert raiders).
  49.13  Fuel consumed = fuel_rate × ceil(cp_spent_movement / 5).
         Fuel is NOT consumed for combat or non-movement actions.
  49.14  Fuel capacity = CPA × (1/5) × fuel_rate.  A unit has exactly enough
         capacity to expend its full CPA on movement.
  49.16  Unit draws fuel from the hex in which it begins movement for that Segment.

Usage
-----
    from src.engine.movement import (
        execute_move,
        validate_move_path,
        compute_fuel_consumption,
        fuel_capacity,
        non_motorized_cp_cap,
        bd_column,
        needs_breakdown_check,
        dp_from_cp_excess,
    )

    result = execute_move(unit, path, game_state, hex_map, context="voluntary")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

from src.engine.hex_map import HexMap
from src.models.event import Event
from src.models.game_state import GameState
from src.models.unit import Unit, UnitStatus


# ── ZOC exit costs (rule 8.15) ────────────────────────────────────────────────

class ContactStatus(str, Enum):
    """
    Whether a unit beginning its segment in enemy ZOC is in Contact or Engaged.

    Rule 8.62 (Contact): units are adjacent with no attack this segment yet.
    Rule 8.63 (Engaged): units have already exchanged fire / committed to assault.
    """
    NONE    = "none"      # not in enemy ZOC
    CONTACT = "contact"   # 2 CP to exit (rule 8.15.2)
    ENGAGED = "engaged"   # 4 CP to exit (rule 8.15.3)


# rule 8.15: CP cost to exit ZOC at start of movement
ZOC_EXIT_COST: Dict[ContactStatus, float] = {
    ContactStatus.NONE:    0.0,
    ContactStatus.CONTACT: 2.0,
    ContactStatus.ENGAGED: 4.0,
}


# ── Breakdown table columns (rule 21.31) ──────────────────────────────────────

# Each entry: (min_bd_exclusive, max_bd_inclusive, column_label)
# "exclusive" means BD must be *strictly greater than* the previous threshold.
# Rule 21.27: no check until BD > 3 (strictly).
# Rule 21.33: 71+ is the ceiling column.
_BD_COLUMN_THRESHOLDS: List[Tuple[int, Optional[int], str]] = [
    (3,  10, "4-10"),
    (10, 20, "11-20"),
    (20, 30, "21-30"),
    (30, 40, "31-40"),
    (40, 50, "41-50"),
    (50, 60, "51-60"),
    (60, 70, "61-70"),
    (70, None, "71+"),
]


def bd_column(bd_total: float) -> Optional[str]:
    """
    Return the Breakdown Table column label for a given BD total (rule 21.31).

    Returns None if BD ≤ 3 (no breakdown check required — rule 21.27).

    Rule 21.31: "All fractions are rounded up: 20.5 = 21."
    """
    bd_int = math.ceil(bd_total)
    for lo, hi, label in _BD_COLUMN_THRESHOLDS:
        if bd_int > lo and (hi is None or bd_int <= hi):
            return label
    return None   # bd_int ≤ 3


def needs_breakdown_check(
    bd_before_move: float,
    bd_after_move: float,
    had_previous_check: bool,
) -> bool:
    """
    Determine if a breakdown check is required when a unit stops (rules 21.24–21.26).

    Rule 21.24: check when unit CEASES movement.
    Rule 21.27: no check if BD ≤ 3.
    Rule 21.26: if a check was already made this OpStage, another check is
    required only if the BD total has pushed into a HIGHER column.

    bd_before_move: BD accumulated before this movement segment began.
    bd_after_move:  BD accumulated after this movement segment.
    had_previous_check: True if the unit already had at least one breakdown
                        check this OpStage.
    """
    col_after = bd_column(bd_after_move)
    if col_after is None:
        return False  # rule 21.27: BD ≤ 3, no check

    if not had_previous_check:
        return True   # first movement this OpStage with BD > 3

    col_before = bd_column(bd_before_move)
    if col_before is None:
        return True   # crossed from "no column" into a real column

    # rule 21.26: only check again if pushed into a HIGHER column
    return col_after != col_before


# ── Fuel consumption (rule 49.13) ─────────────────────────────────────────────

def compute_fuel_consumption(cp_spent_movement: float, fuel_rate: float) -> float:
    """
    Fuel Points consumed for a given movement CP expenditure (rule 49.13).

    "The fuel consumption factor is the number of Fuel Points consumed for every
    five Capability Points (or fraction thereof) expended by the TOE Strength
    Point for movement (or any type)."

    Fuel is NOT consumed for combat or non-movement CP expenditure (49.13).

    fuel_rate: from unit's counter (Fuel Points per 5 CP of movement).
    cp_spent_movement: CP spent on actual hex-to-hex movement this segment.

    Rule 49.13 example: fuel_rate=4, 12 CP → 3 groups of 5 → 12 Fuel Points.
    """
    if fuel_rate <= 0 or cp_spent_movement <= 0:
        return 0.0
    groups = math.ceil(cp_spent_movement / 5.0)
    return groups * fuel_rate


def fuel_capacity(cpa: int, fuel_rate: float) -> float:
    """
    Fuel capacity rating for a unit (rule 49.14).

    "Fuel capacity rating = CPA × (1/5) × fuel consumption rate"

    "Players will note that a TOE Strength Point always has a fuel capacity
    rating exactly sufficient to allow all its CPA to be expended on movement."
    """
    return cpa * (1.0 / 5.0) * fuel_rate


# ── CP cap for non-motorized (rule 8.17) ──────────────────────────────────────

def non_motorized_cp_cap(base_cpa: int) -> Optional[float]:
    """
    Maximum CPs a non-motorized unit may voluntarily spend in its own portion
    of an OpStage (rule 8.17).

    Rule 8.17: "Non-motorized units — those units with CPA's of ten or less —
    may never voluntarily expend CP's greater than 50% of their base CPA during
    their portion of the Operations Stage (An '8' could not go higher than '12',
    a '10' no higher than '15')."

    Returns the cap, or None if the unit is motorized (CPA > 10) — no cap applies.

    Does NOT apply to Reaction or Retreat Before Assault (rule 8.17 explicitly
    excludes these: "they occur in the other player's portion of the Operations Stage").
    """
    if base_cpa > 10:
        return None   # motorized units have no 50% cap
    return base_cpa * 1.5


# ── DP from CPA excess (rule 6.21) ────────────────────────────────────────────

def dp_from_cp_excess(cp_used: float, cpa: int) -> int:
    """
    Disorganization Points earned by exceeding CPA (rule 6.21).

    "For each Capability Point that a unit uses over its CPA it earns one
    Disorganization Point."

    DPs are applied immediately (rule 6.22), not at end of OpStage.
    """
    excess = max(0.0, cp_used - cpa)
    return math.floor(excess)


# ── Move step and result dataclasses ──────────────────────────────────────────

@dataclass
class MoveStep:
    """One hex transition within a move path."""
    from_id: str
    to_id: str
    cp_cost: float          # hex_map.entry_cost() result
    bd_cost: float          # hex_map.entry_bd() result
    enters_enemy_zoc: bool  # this step ends movement (rule 8.14)


@dataclass
class ValidationIssue:
    """A problem found during move path validation."""
    step_index: int          # which step (0-indexed); -1 = pre-move check
    hex_id: str
    reason: str
    is_fatal: bool           # if True the move cannot proceed at all


@dataclass
class MoveResult:
    """Outcome of an executed move."""
    unit_id: str
    context: str             # "voluntary" | "reaction" | "retreat"

    # Path
    path_intended: List[str]
    path_taken: List[str]    # may be shorter if stopped by ZOC or prohibition
    stopped_reason: Optional[str] = None

    # CP accounting
    zoc_exit_cp: float = 0.0       # rule 8.15 pre-move cost
    cp_spent_movement: float = 0.0  # CP for hex entry costs
    cp_spent_total: float = 0.0     # zoc_exit_cp + cp_spent_movement
    dp_earned: int = 0             # rule 6.21

    # BD
    bd_before: float = 0.0
    bd_accumulated: float = 0.0
    bd_after: float = 0.0
    breakdown_check_needed: bool = False
    bd_column_label: Optional[str] = None   # None = no check needed

    # Fuel
    fuel_consumed: float = 0.0     # rule 49.13 (0.0 for non-motorized)

    # Events emitted
    events: List[Event] = field(default_factory=list)


# ── Validation ────────────────────────────────────────────────────────────────

def validate_move_path(
    unit: Unit,
    path: List[str],
    game_state: GameState,
    hex_map: HexMap,
    context: str = "voluntary",
) -> List[ValidationIssue]:
    """
    Validate that a unit can move along path (list of hex_ids, starting hex first).

    Does NOT execute the move.  Returns a list of ValidationIssue objects.
    An empty list means the path is valid.

    Checks:
      - Unit must be active and not BROKEN_DOWN (8.11)
      - Unit at cohesion ≤ −26 may not move (6.26)
      - Path is consecutive (8.13)
      - No hex in path contains an enemy unit (8.13)
      - No prohibited terrain for this unit type
      - ZOC stop: movement ends on entering enemy ZOC (8.14)
      - Non-motorized voluntary CP cap (8.17)
    """
    issues: List[ValidationIssue] = []

    # Pre-move checks
    if unit.is_eliminated():
        issues.append(ValidationIssue(-1, unit.hex_id or "", "unit is eliminated", True))
        return issues

    if unit.status == UnitStatus.BROKEN_DOWN:
        issues.append(ValidationIssue(-1, unit.hex_id or "", "unit is broken down and cannot move this OpStage", True))
        return issues

    if unit.status == UnitStatus.DISORGANIZED:
        issues.append(ValidationIssue(-1, unit.hex_id or "", "unit cohesion ≤ −26; may not move (rule 6.26)", True))
        return issues

    if len(path) < 2:
        issues.append(ValidationIssue(-1, path[0] if path else "", "path must contain at least 2 hexes (start and one destination)", True))
        return issues

    if path[0] != unit.hex_id:
        issues.append(ValidationIssue(-1, path[0], f"path start '{path[0]}' does not match unit hex '{unit.hex_id}'", True))
        return issues

    all_units = list(game_state.units.values())
    cp_accumulated = 0.0
    cpa = game_state.formation_cpa(unit)
    cap = non_motorized_cp_cap(cpa) if context == "voluntary" else None

    for i in range(1, len(path)):
        from_id = path[i - 1]
        to_id   = path[i]

        # Adjacency check (rule 8.13: consecutive movement)
        if hex_map.direction_to(from_id, to_id) is None:
            issues.append(ValidationIssue(i, to_id, f"hexes {from_id}→{to_id} are not adjacent (rule 8.13)", True))
            continue

        # Enemy unit in destination (rule 8.13)
        for u in all_units:
            if u.hex_id == to_id and u.side != unit.side and not u.is_eliminated():
                issues.append(ValidationIssue(i, to_id, "hex contains an enemy unit (rule 8.13)", True))
                break

        # Entry cost
        cost = hex_map.entry_cost(unit, from_id, to_id, game_state.weather)
        if cost == "P":
            issues.append(ValidationIssue(i, to_id, "terrain is prohibited for this unit (rule 8.3/8.4)", True))
            continue

        cp_accumulated += float(cost)

        # Non-motorized voluntary CP cap (rule 8.17)
        if cap is not None and cp_accumulated > cap:
            issues.append(ValidationIssue(
                i, to_id,
                f"voluntary CP cap exceeded: {cp_accumulated:.1f} > {cap:.1f} "
                f"(rule 8.17: non-mot may not exceed 150% of CPA {cpa})",
                True,
            ))

        # ZOC stop (rule 8.14)
        if hex_map.in_enemy_zoc(to_id, unit.side, all_units):
            if not hex_map.zoc_cancelled(to_id, unit.side, all_units):
                if i < len(path) - 1:
                    # Not the last step — ZOC forced stop earlier than intended
                    issues.append(ValidationIssue(
                        i, to_id,
                        f"unit must stop on entering enemy ZOC at {to_id} (rule 8.14); path continues beyond this hex",
                        False,  # not fatal — we just truncate
                    ))
                break  # movement ends here

    return issues


# ── Execution ─────────────────────────────────────────────────────────────────

def execute_move(
    unit: Unit,
    path: List[str],
    game_state: GameState,
    hex_map: HexMap,
    context: str = "voluntary",
    zoc_contact_status: ContactStatus = ContactStatus.NONE,
    had_previous_bd_check: bool = False,
    fuel_rate: float = 0.0,
) -> MoveResult:
    """
    Execute a move and return the result, updating unit state in-place.

    path: list of hex_ids, starting hex first (e.g. ["A1234", "A1235", "A1236"]).
    context: "voluntary" | "reaction" | "retreat"
    zoc_contact_status: Contact or Engaged status if unit starts in enemy ZOC.
    had_previous_bd_check: True if the unit already passed a BD check this OpStage.
    fuel_rate: Fuel Points per 5 CP for this unit (0.0 = non-motorized / no fuel).

    Rule 8.17 voluntary cap is enforced when context == "voluntary".
    ZOC exit cost (rule 8.15) is charged at the START before any movement.

    Updates in-place:
      unit.hex_id       → final hex after move
      unit.cp_remaining → decremented by total CP spent
      unit.breakdown_points → incremented by BD accumulated
    """
    result = MoveResult(
        unit_id=unit.id,
        context=context,
        path_intended=list(path),
        path_taken=[path[0]],
    )

    if unit.is_eliminated() or unit.status == UnitStatus.DISORGANIZED:
        result.stopped_reason = "unit cannot move"
        return result

    cpa = game_state.formation_cpa(unit)
    all_units = list(game_state.units.values())

    # ── Pre-move ZOC exit cost (rule 8.15) ────────────────────────────────────
    zoc_exit = ZOC_EXIT_COST[zoc_contact_status]
    result.zoc_exit_cp = zoc_exit

    cp_spent = 0.0
    bd_before = unit.breakdown_points
    bd_accumulated = 0.0

    # ── Move hex by hex ───────────────────────────────────────────────────────
    for i in range(1, len(path)):
        from_id = path[i - 1]
        to_id   = path[i]

        # Adjacency sanity check
        if hex_map.direction_to(from_id, to_id) is None:
            result.stopped_reason = f"non-adjacent hexes {from_id}→{to_id} (rule 8.13)"
            break

        # Prohibited terrain
        cost = hex_map.entry_cost(unit, from_id, to_id, game_state.weather)
        if cost == "P":
            result.stopped_reason = f"terrain prohibited at {to_id}"
            break

        step_cp = float(cost)

        # Non-motorized voluntary CP cap (rule 8.17)
        if context == "voluntary":
            cap = non_motorized_cp_cap(cpa)
            if cap is not None and (cp_spent + zoc_exit + step_cp) > cap:
                result.stopped_reason = (
                    f"non-mot voluntary CP cap {cap:.1f} would be exceeded "
                    f"(rule 8.17)"
                )
                break

        # BD accumulation (rule 21.21)
        step_bd = hex_map.entry_bd(unit, from_id, to_id)
        bd_accumulated += step_bd

        cp_spent += step_cp
        result.path_taken.append(to_id)

        # ZOC stop (rule 8.14): unit must stop immediately on entering enemy ZOC
        if hex_map.in_enemy_zoc(to_id, unit.side, all_units):
            if not hex_map.zoc_cancelled(to_id, unit.side, all_units):
                result.stopped_reason = f"entered enemy ZOC at {to_id}; must stop (rule 8.14)"
                break

    # ── Totals ────────────────────────────────────────────────────────────────

    total_cp = zoc_exit + cp_spent
    result.cp_spent_movement = cp_spent
    result.cp_spent_total = total_cp

    result.bd_before    = bd_before
    result.bd_accumulated = bd_accumulated
    result.bd_after     = bd_before + bd_accumulated

    # DP from CPA excess (rule 6.21)
    cp_already_used = cpa - unit.cp_remaining   # CP used before this move
    cp_after_move   = cp_already_used + total_cp
    result.dp_earned = dp_from_cp_excess(cp_after_move, cpa)

    # Breakdown check (rules 21.24–21.27)
    result.breakdown_check_needed = needs_breakdown_check(
        bd_before, result.bd_after, had_previous_bd_check,
    )
    result.bd_column_label = bd_column(result.bd_after)

    # Fuel consumption (rule 49.13) — movement CPs only
    result.fuel_consumed = compute_fuel_consumption(cp_spent, fuel_rate) if unit.motorized else 0.0

    # ── Update unit state ─────────────────────────────────────────────────────
    if result.path_taken:
        unit.hex_id = result.path_taken[-1]

    # Deduct CP from remaining (cp_remaining tracks what's left this OpStage)
    unit.cp_remaining = max(0.0, unit.cp_remaining - total_cp)
    unit.breakdown_points += bd_accumulated

    # ── Emit events ───────────────────────────────────────────────────────────
    if result.dp_earned > 0:
        result.events.append(Event(
            turn=game_state.turn,
            opstage=game_state.opstage,
            type="dp",
            unit_id=unit.id,
            description=(
                f"{unit.name} earned {result.dp_earned} DP "
                f"(CPA {cpa}, total CP used {cp_after_move:.1f}) — rule 6.21"
            ),
            data={
                "dp": result.dp_earned,
                "cp_over_cpa": cp_after_move - cpa,
                "cpa": cpa,
            },
        ))

    if result.breakdown_check_needed:
        result.events.append(Event(
            turn=game_state.turn,
            opstage=game_state.opstage,
            type="breakdown_check",
            unit_id=unit.id,
            description=(
                f"{unit.name} requires Breakdown check: "
                f"{result.bd_after:.1f} BD ({result.bd_column_label}) — rule 21.24"
            ),
            data={
                "bd_total": result.bd_after,
                "bd_column": result.bd_column_label,
                "table_lookup_stubbed": True,
                "note": "Breakdown Table (21.38) not yet extracted; roll result is deferred",
            },
        ))

    if result.fuel_consumed > 0.0:
        result.events.append(Event(
            turn=game_state.turn,
            opstage=game_state.opstage,
            type="fuel",
            unit_id=unit.id,
            description=(
                f"{unit.name} consumed {result.fuel_consumed:.1f} fuel "
                f"({cp_spent:.1f} CP movement, rate {fuel_rate}) — rule 49.13"
            ),
            data={
                "fuel_consumed": result.fuel_consumed,
                "cp_movement": cp_spent,
                "fuel_rate": fuel_rate,
            },
        ))

    return result


# ── Formation movement helper ──────────────────────────────────────────────────

def execute_formation_move(
    unit_ids: List[str],
    path: List[str],
    game_state: GameState,
    hex_map: HexMap,
    context: str = "voluntary",
    fuel_rates: Optional[Dict[str, float]] = None,
    had_previous_bd_check: bool = False,
) -> List[MoveResult]:
    """
    Move multiple units along the same path (formation movement).

    Rule 6.15: the formation's effective CPA is the lowest CPA of its units.
    All units share the same BD accumulation for the path (rule 21.23).

    Each unit is moved independently (separate MoveResult) but they all receive
    the same BD from the path. The caller is responsible for ensuring the path
    is valid for all units before calling this function.

    fuel_rates: {unit_id → fuel_rate}; defaults to 0.0 for any missing unit.
    """
    results: List[MoveResult] = []
    if fuel_rates is None:
        fuel_rates = {}

    # Determine the shared stopping point: the shortest path any unit can take.
    # For simplicity, execute each unit independently; they all follow the same
    # path until any one is blocked.  Formation movement is an area for future
    # refinement when board_state.py manages formation cohesion.
    for uid in unit_ids:
        unit = game_state.units.get(uid)
        if unit is None:
            continue
        r = execute_move(
            unit, path, game_state, hex_map,
            context=context,
            had_previous_bd_check=had_previous_bd_check,
            fuel_rate=fuel_rates.get(uid, 0.0),
        )
        results.append(r)

    return results
