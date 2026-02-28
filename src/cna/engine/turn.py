"""
Turn/Operations Stage orchestrator for The Campaign for North Africa simulation.

CNA turn structure:
  Turn start:
    1. Reinforcement entry (per scenario schedule)
    2. Command Point allocation

  For each of 3 Operations Stages (OpStages):
    a. Phasing player movement impulses (alternating AXIS/ALLIED)
    b. Non-phasing player reaction moves
    c. Combat declaration and resolution
    d. Supply consumption (fuel, water, ammo for movement)

  Turn end:
    5. Supply phase (evaporation, convoy arrivals, resupply)
    6. Rally phase (disorganized units attempt to recover)
    7. Air operations (maintenance, replacements)
    8. Victory check
    9. Journal entry generation
"""

from __future__ import annotations

from ..models.counter import Side, UnitStatus
from ..models.game_state import GameState
from ..models.supply import SupplyReport
from .movement import generate_ai_movement_orders, attempt_movement
from .supply_chain import (
    calculate_supply_lines, compile_supply_report, process_convoy_arrivals
)
from .logistics import (
    apply_water_consumption, apply_ammo_consumption,
    apply_stores_consumption, prioritize_resupply, apply_resupply_allocations
)
from .combat import generate_ai_combat_declarations, resolve_combat
from .air_ops import generate_ai_air_orders, execute_air_mission


def process_turn(state: GameState) -> SupplyReport:
    """
    Execute one full CNA turn (1 week).
    Mutates state in place. Returns the SupplyReport for journal generation.
    """
    turn = state.turn

    # --- REINFORCEMENTS ---
    _process_reinforcements(state)

    # --- 3 OPERATIONS STAGES ---
    for opstage in range(1, 4):
        state.opstage = opstage
        _process_opstage(state, opstage)

    state.opstage = 0  # Back to turn-level

    # --- SUPPLY PHASE ---
    state.supply_lines = calculate_supply_lines(state)
    supply_report = compile_supply_report(state)
    state.supply_report = supply_report
    process_convoy_arrivals(state, supply_report)

    # Resupply in-supply units
    _execute_resupply(state)

    apply_stores_consumption(state)
    apply_ammo_consumption(state, combat_occurred=bool(
        [e for e in state.events if e.category == "combat"]
    ))

    # --- RALLY PHASE ---
    _rally_disorganized_units(state)

    # --- AIR MAINTENANCE ---
    _air_maintenance(state)

    # --- HEX CONTROL UPDATE ---
    _update_hex_control(state)

    return supply_report


def _process_opstage(state: GameState, opstage: int) -> None:
    """Run one Operations Stage with alternating impulses."""
    # Phasing order alternates; AXIS phases first on odd-numbered turns
    if state.turn % 2 == 1:
        phasing_first = Side.AXIS
        phasing_second = Side.ALLIED
    else:
        phasing_first = Side.ALLIED
        phasing_second = Side.AXIS

    # Two movement impulses (phasing then non-phasing)
    for phasing in (phasing_first, phasing_second):
        _execute_movement_impulse(state, phasing)

    # Water consumption per OpStage
    apply_water_consumption(state, opstage)

    # Combat (phasing side attacks)
    _execute_combat_phase(state, phasing_first)


def _execute_movement_impulse(state: GameState, side: Side) -> None:
    """Generate and execute movement orders for one side."""
    orders = generate_ai_movement_orders(state, side.value)
    for order in orders:
        unit = state.ground_units.get(order.unit_id)
        if unit:
            attempt_movement(unit, order, state.map, state)


def _execute_combat_phase(state: GameState, phasing_side: Side) -> None:
    """Declare and resolve all combats for the phasing side."""
    declarations = generate_ai_combat_declarations(state, phasing_side.value)
    for decl in declarations:
        attacker = state.ground_units.get(decl.attacker_id)
        defender = state.ground_units.get(decl.defender_id)
        if attacker and defender:
            resolve_combat(attacker, defender, decl, state.map, state)


def _process_reinforcements(state: GameState) -> None:
    """Make units available that enter play this turn."""
    for unit in state.ground_units.values():
        if unit.available_turn == state.turn and unit.status == UnitStatus.ELIMINATED:
            # Don't revive eliminated units
            pass
        elif unit.available_turn == state.turn:
            state.log_event(
                "reinforcement",
                f"{unit.name} arrives on the map at {unit.hex_id or 'unknown'}",
                unit_ids=[unit.id],
                hex_ids=[unit.hex_id] if unit.hex_id else [],
                severity="notable",
            )


def _execute_resupply(state: GameState) -> None:
    """Distribute available supplies to in-supply units."""
    for side in (Side.AXIS, Side.ALLIED):
        units = state.active_units_for_side(side)
        depots = state.supply_depots_for_side(side)

        # Aggregate available supply
        available_fuel = sum(
            d.current_load for d in depots
            if d.supply_type in ("fuel", "general") and d.current_load > 0
        )
        available_water = sum(
            d.current_load for d in depots
            if d.supply_type in ("water", "general") and d.current_load > 0
        )
        available_ammo = sum(
            d.current_load for d in depots
            if d.supply_type in ("ammo", "general") and d.current_load > 0
        )

        # Only resupply in-supply units
        in_supply_units = [
            u for u in units
            if u.id in state.supply_lines and state.supply_lines[u.id].in_supply
        ]

        if not in_supply_units:
            continue

        allocations = prioritize_resupply(
            in_supply_units,
            min(available_fuel, 50.0),    # Limit per turn throughput
            min(available_water, 40.0),
            min(available_ammo, 30.0),
        )
        apply_resupply_allocations(in_supply_units, allocations)

        # Deduct from depots
        total_fuel_drawn = sum(a.get("fuel", 0) for a in allocations.values())
        total_water_drawn = sum(a.get("water", 0) for a in allocations.values())
        total_ammo_drawn = sum(a.get("ammo", 0) for a in allocations.values())

        for depot in depots:
            if total_fuel_drawn > 0 and depot.supply_type in ("fuel", "general"):
                drawn = depot.draw(min(total_fuel_drawn, depot.current_load))
                total_fuel_drawn -= drawn


def _rally_disorganized_units(state: GameState) -> None:
    """
    Disorganized units attempt to rally.
    Success chance depends on morale and whether they are in supply.
    """
    import random
    for unit in state.ground_units.values():
        if unit.status != UnitStatus.DISORGANIZED:
            continue
        sl = state.supply_lines.get(unit.id)
        in_supply = sl and sl.in_supply
        rally_target = unit.morale + (2 if in_supply else -2)
        roll = random.randint(1, 10)
        if roll <= rally_target:
            unit.status = UnitStatus.UNAFFECTED
            unit.cohesion = min(0, unit.cohesion + 3)
            state.log_event(
                "command",
                f"{unit.name} has rallied and returned to effective status",
                unit_ids=[unit.id],
                severity="normal",
            )


def _air_maintenance(state: GameState) -> None:
    """
    End-of-turn aircraft maintenance — condition partially recovers.
    Represents ground crews working overnight.
    """
    for air_unit in state.air_units.values():
        if air_unit.status == UnitStatus.ELIMINATED:
            continue
        # Maintenance restores 5–10 airframe condition points per turn
        import random
        recovery = random.randint(5, 10)
        air_unit.airframe_condition = min(100, air_unit.airframe_condition + recovery)


def _update_hex_control(state: GameState) -> None:
    """
    Update which side controls each hex based on unit presence.
    A hex is controlled by a side if it has units there and no enemy units.
    """
    axis_hexes: set[str] = set()
    allied_hexes: set[str] = set()

    for unit in state.active_units_for_side(Side.AXIS):
        if unit.hex_id:
            axis_hexes.add(unit.hex_id)
    for unit in state.active_units_for_side(Side.ALLIED):
        if unit.hex_id:
            allied_hexes.add(unit.hex_id)

    # Contested hexes — keep previous control
    contested = axis_hexes & allied_hexes

    for hex_id in axis_hexes - contested:
        state.hex_control[hex_id] = "axis"
    for hex_id in allied_hexes - contested:
        state.hex_control[hex_id] = "allied"
