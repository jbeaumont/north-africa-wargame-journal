"""
Movement engine for The Campaign for North Africa simulation.

CNA movement rules:
  - Each unit has a CPA (Capability Point Allowance) per Operations Stage.
  - Terrain costs 0.5 CP (road) to 6 CP (mountains) per hex.
  - Fuel is consumed proportional to distance moved relative to CPA.
  - Each Operations Stage of vehicle movement has a d6 breakdown chance.
  - Units out of fuel cannot move (or are severely restricted).
  - Unsupplied units cannot activate.
  - Disorganized units move at 50% CPA.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..models.counter import GroundUnit, UnitType, UnitStatus, Nationality
from ..models.hex_map import HexMap, Hex
from ..models.game_state import GameState, Event


# Fuel consumed per CPA unit spent in movement.
# Formula: fuel_consumed = hexes_moved × (fuel_capacity / cpa) × FUEL_BURN_RATE
FUEL_BURN_RATE = 0.4  # Tuned to ~30% fuel per full-CPA OpStage


@dataclass
class MovementOrder:
    """An instruction to move a unit along a path of hex IDs."""
    unit_id: str
    path: list[str]  # hex_id sequence; path[0] must be unit's current hex


@dataclass
class MovementResult:
    unit_id: str
    start_hex: str
    end_hex: str
    hexes_moved: int
    cp_spent: float
    fuel_consumed: float
    broke_down: bool
    aborted_reason: str | None = None


def _vehicle_unit(unit: GroundUnit) -> bool:
    """Return True if this unit has vehicles that can break down."""
    return unit.unit_type in {
        UnitType.ARMORED_BATTALION,
        UnitType.ARMORED_REGIMENT,
        UnitType.ARMORED_DIVISION_HQ,
        UnitType.MOTORIZED_INFANTRY,
        UnitType.MECHANIZED_INFANTRY,
        UnitType.RECON_UNIT,
        UnitType.SUPPLY_COLUMN,
        UnitType.HEADQUARTERS,
    }


def _breakdown_roll(unit: GroundUnit) -> bool:
    """
    Vehicle breakdown check per Operations Stage of movement.
    Roll d6: on a 1, the unit suffers a breakdown (lose 1 step).
    German vehicles are more reliable: breakdown only on a 1 out of 6.
    Italian vehicles: slightly less reliable.
    """
    if not _vehicle_unit(unit):
        return False
    roll = random.randint(1, 6)
    threshold = 1  # breakdown on 1
    if unit.nationality == Nationality.ITALIAN:
        threshold = 1  # same odds but flavor differs
    return roll <= threshold


def attempt_movement(
    unit: GroundUnit,
    order: MovementOrder,
    hex_map: HexMap,
    state: GameState,
) -> MovementResult:
    """
    Attempt to move a unit along the given path.

    Validates:
      - Unit is not eliminated
      - Unit is not completely out of fuel (if motorized)
      - Each hex is passable and adjacent
      - CP budget not exceeded

    Consumes fuel proportional to distance moved.
    Returns a MovementResult describing what actually happened.
    """
    if unit.status == UnitStatus.ELIMINATED:
        return MovementResult(
            unit_id=unit.id, start_hex=unit.hex_id or "?",
            end_hex=unit.hex_id or "?", hexes_moved=0,
            cp_spent=0, fuel_consumed=0, broke_down=False,
            aborted_reason="unit eliminated",
        )

    if unit.available_turn > state.turn:
        return MovementResult(
            unit_id=unit.id, start_hex=unit.hex_id or "?",
            end_hex=unit.hex_id or "?", hexes_moved=0,
            cp_spent=0, fuel_consumed=0, broke_down=False,
            aborted_reason="not yet available",
        )

    effective_cpa = unit.movement_factor

    # Motorized units need fuel to move
    if _vehicle_unit(unit) and unit.supply.is_critically_low_on_fuel():
        state.log_event(
            "movement",
            f"{unit.name} cannot move — fuel critically low "
            f"({unit.supply.fuel:.1f} points remaining)",
            unit_ids=[unit.id],
            severity="notable",
        )
        return MovementResult(
            unit_id=unit.id, start_hex=unit.hex_id or "?",
            end_hex=unit.hex_id or "?", hexes_moved=0,
            cp_spent=0, fuel_consumed=0, broke_down=False,
            aborted_reason="out of fuel",
        )

    path = order.path
    if len(path) < 2:
        return MovementResult(
            unit_id=unit.id, start_hex=unit.hex_id or "?",
            end_hex=unit.hex_id or "?", hexes_moved=0,
            cp_spent=0, fuel_consumed=0, broke_down=False,
            aborted_reason="trivial path",
        )

    cp_spent = 0.0
    fuel_consumed = 0.0
    last_good_hex = path[0]

    for i in range(1, len(path)):
        from_hex_id = path[i - 1]
        to_hex_id = path[i]

        from_hex = hex_map.get(from_hex_id)
        to_hex = hex_map.get(to_hex_id)

        if not from_hex or not to_hex:
            break
        if to_hex.is_impassable():
            break
        if to_hex_id not in from_hex.adjacent:
            break  # Non-adjacent hex in path

        step_cp = to_hex.movement_cost(from_hex)

        if cp_spent + step_cp > effective_cpa + 0.001:
            break  # Would exceed movement allowance

        cp_spent += step_cp
        last_good_hex = to_hex_id

        # Fuel consumption for this step
        if _vehicle_unit(unit) and unit.fuel_capacity > 0:
            fuel_per_cp = unit.fuel_capacity * FUEL_BURN_RATE / max(unit.cpa, 1)
            step_fuel = fuel_per_cp * step_cp
            fuel_consumed += step_fuel
            unit.supply.fuel = max(0.0, round(unit.supply.fuel - step_fuel, 3))

    hexes_moved = path.index(last_good_hex) if last_good_hex in path else 0

    # Vehicle breakdown check (per OpStage, not per hex)
    broke_down = False
    if hexes_moved > 0 and _vehicle_unit(unit):
        broke_down = _breakdown_roll(unit)
        if broke_down:
            unit.apply_step_loss(1)
            state.log_event(
                "movement",
                f"{unit.name} suffered a mechanical breakdown moving to {last_good_hex}! "
                f"({unit.steps}/{unit.max_steps} steps remaining)",
                unit_ids=[unit.id],
                hex_ids=[last_good_hex],
                severity="notable",
            )

    # Actually move the unit
    unit.hex_id = last_good_hex

    return MovementResult(
        unit_id=unit.id,
        start_hex=path[0],
        end_hex=last_good_hex,
        hexes_moved=hexes_moved,
        cp_spent=round(cp_spent, 2),
        fuel_consumed=round(fuel_consumed, 3),
        broke_down=broke_down,
    )


def generate_ai_movement_orders(state: GameState, side_str: str) -> list[MovementOrder]:
    """
    Simple AI movement logic — generates plausible movement orders for a side.

    Strategy:
      - Axis: push east toward Allied positions / supply choke points
      - Allied: maintain defensive positions or push west when stronger

    This is deliberately simple — the journal narrative will provide the
    strategic reasoning. The simulation just needs plausible movement.
    """
    from ..models.counter import Side
    side = Side(side_str)
    orders: list[MovementOrder] = []

    units = state.active_units_for_side(side)

    for unit in units:
        if not unit.hex_id:
            continue
        if unit.status != UnitStatus.UNAFFECTED:
            continue
        if unit.supply.is_critically_low_on_fuel() and _vehicle_unit(unit):
            continue  # Can't move

        h = state.map.get(unit.hex_id)
        if not h:
            continue

        # Choose adjacent hex toward the front
        candidates = state.map.adjacent_hexes(unit.hex_id)
        if not candidates:
            continue

        # Axis moves east (toward higher column numbers); Allied west
        if side == Side.AXIS:
            # Sort by hex_id to prefer eastward movement (higher column)
            candidates.sort(key=lambda x: x.hex_id, reverse=True)
        else:
            candidates.sort(key=lambda x: x.hex_id)

        # Filter: don't walk into enemy-held hex (simplified)
        enemy_side_str = "allied" if side == Side.AXIS else "axis"
        valid = [
            c for c in candidates
            if state.hex_control.get(c.hex_id) != enemy_side_str
        ]
        if not valid:
            continue

        target = valid[0]
        # Only move if it's meaningful (not same column = no progress)
        if target.hex_id != unit.hex_id:
            orders.append(MovementOrder(
                unit_id=unit.id,
                path=[unit.hex_id, target.hex_id],
            ))

    return orders
