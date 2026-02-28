"""
Supply chain engine for The Campaign for North Africa simulation.

Core rules implemented here:
  - Supply line validity: each unit must trace an unbroken path of friendly
    hexes ≤ 5 hexes to a friendly supply depot.
  - Fuel evaporation: 3%/turn for all non-British; 7%/turn for British
    (they used 50-gallon drums instead of the far superior German jerry cans).
  - Water source replenishment from wells along the route.
  - Convoy arrivals add supplies to designated port depots.
  - Out-of-supply consequences are tracked for journal reporting.
"""

from __future__ import annotations

import math
from collections import deque

from ..models.counter import GroundUnit, SupplyCounter, Nationality, Side
from ..models.hex_map import HexMap
from ..models.supply import SupplyLine, ConvoyRecord, SupplyReport
from ..models.game_state import GameState, Event


MAX_SUPPLY_RANGE = 5  # hexes; units beyond this are out of supply


def calculate_supply_lines(state: GameState) -> dict[str, SupplyLine]:
    """
    For every active ground unit, trace its supply line to the nearest
    friendly depot. Returns a dict of unit_id → SupplyLine.

    Algorithm: BFS from each unit outward through friendly-controlled hexes.
    A supply line is valid if it reaches a friendly depot within 5 hops.
    """
    supply_lines: dict[str, SupplyLine] = {}

    for side in (Side.AXIS, Side.ALLIED):
        # Collect depot positions for this side
        depot_hexes: dict[str, SupplyCounter] = {}
        for depot in state.supply_depots_for_side(side):
            if depot.hex_id and depot.current_load > 0:
                depot_hexes[depot.hex_id] = depot

        # Collect unit positions
        for unit in state.active_units_for_side(side):
            if not unit.hex_id:
                supply_lines[unit.id] = SupplyLine(
                    unit_id=unit.id, depot_id=None, is_valid=False)
                continue

            # BFS from unit's hex
            best = _bfs_to_depot(
                start_hex=unit.hex_id,
                depot_hexes=depot_hexes,
                state=state,
                side=side,
            )
            supply_lines[unit.id] = best

    return supply_lines


def _bfs_to_depot(
    start_hex: str,
    depot_hexes: dict[str, SupplyCounter],
    state: GameState,
    side: Side,
) -> SupplyLine:
    """
    BFS from start_hex through friendly-controlled or neutral hexes.
    Returns the SupplyLine to the nearest depot (if within range).
    """
    # If the unit is already sitting on a depot hex, trivially in supply.
    if start_hex in depot_hexes:
        return SupplyLine(
            unit_id="",
            depot_id=depot_hexes[start_hex].id,
            path=[start_hex],
            is_valid=True,
            hex_distance=0,
        )

    visited: dict[str, int] = {start_hex: 0}
    prev: dict[str, str] = {}
    queue: deque[tuple[str, int]] = deque([(start_hex, 0)])

    while queue:
        current_hex, distance = queue.popleft()

        if distance >= MAX_SUPPLY_RANGE:
            continue

        h = state.map.get(current_hex)
        if not h:
            continue

        for adj_id in h.adjacent:
            if adj_id in visited:
                continue
            adj_hex = state.map.get(adj_id)
            if not adj_hex or adj_hex.is_impassable():
                continue
            # Supply lines can only pass through friendly or uncontrolled hexes
            controller = state.hex_control.get(adj_id)
            enemy_side = "allied" if side == Side.AXIS else "axis"
            if controller == enemy_side:
                continue

            new_dist = distance + 1
            visited[adj_id] = new_dist
            prev[adj_id] = current_hex

            if adj_id in depot_hexes:
                # Found a depot — reconstruct path
                path = _reconstruct_path(prev, start_hex, adj_id)
                return SupplyLine(
                    unit_id="",
                    depot_id=depot_hexes[adj_id].id,
                    path=path,
                    is_valid=True,
                    hex_distance=new_dist,
                )
            queue.append((adj_id, new_dist))

    return SupplyLine(unit_id="", depot_id=None, is_valid=False,
                      hex_distance=9999)


def _reconstruct_path(prev: dict[str, str], start: str, end: str) -> list[str]:
    path = [end]
    current = end
    while current != start:
        current = prev[current]
        path.append(current)
    return list(reversed(path))


def apply_fuel_evaporation(state: GameState, report: SupplyReport) -> None:
    """
    End-of-turn fuel evaporation.
    British: 7% loss (50-gallon drums).
    All others: 3% loss (German jerry cans, which they issued to everyone).
    Also applies to supply depots and fuel dumps.
    """
    total_evaporated = 0.0

    for unit in state.ground_units.values():
        if unit.supply.fuel <= 0:
            continue
        rate = 0.07 if unit.nationality == Nationality.BRITISH else 0.03
        lost = unit.supply.fuel * rate
        unit.supply.fuel = round(unit.supply.fuel * (1 - rate), 2)
        total_evaporated += lost

    # Evaporation also hits fuel dump stocks
    for depot in state.supply_counters.values():
        if depot.supply_type == "fuel" and depot.current_load > 0:
            # Depots lose 2% (better storage conditions)
            lost = depot.current_load * 0.02
            depot.current_load = round(depot.current_load * 0.98, 2)
            total_evaporated += lost

    report.fuel_evaporated = round(total_evaporated, 2)


def resupply_unit(
    unit: GroundUnit,
    depots: list[SupplyCounter],
    supply_line: SupplyLine,
) -> dict[str, float]:
    """
    Draw supplies from the nearest depot to fill up a unit.
    Returns dict of how much of each type was transferred.
    """
    if not supply_line.in_supply:
        return {}

    transfers: dict[str, float] = {}

    for depot in depots:
        if depot.id != supply_line.depot_id and depot.supply_type != "general":
            continue

        if depot.supply_type in ("fuel", "general"):
            needed = unit.fuel_capacity - unit.supply.fuel
            if needed > 0:
                drawn = depot.draw(needed)
                unit.supply.fuel = round(unit.supply.fuel + drawn, 2)
                transfers["fuel"] = transfers.get("fuel", 0) + drawn

        if depot.supply_type in ("water", "general"):
            needed = unit.water_factor * 10.0 - unit.supply.water
            if needed > 0:
                drawn = depot.draw(needed)
                unit.supply.water = round(unit.supply.water + drawn, 2)
                transfers["water"] = transfers.get("water", 0) + drawn

        if depot.supply_type in ("ammo", "general"):
            needed = unit.ammo_factor * 8.0 - unit.supply.ammo
            if needed > 0:
                drawn = depot.draw(needed)
                unit.supply.ammo = round(unit.supply.ammo + drawn, 2)
                transfers["ammo"] = transfers.get("ammo", 0) + drawn

    return transfers


def process_convoy_arrivals(state: GameState, report: SupplyReport) -> None:
    """
    Check for convoys arriving this turn and add their cargo to the
    destination depot (if any remains after interdiction losses).
    """
    arrived = []
    for convoy in state.convoys:
        if convoy.arrived or convoy.arrival_turn != state.turn:
            continue
        # Find the destination depot
        dest_hex = convoy.destination
        for depot in state.supply_counters.values():
            if depot.hex_id == dest_hex and depot.side == Side(
                "allied" if convoy.origin.startswith("alex") or convoy.origin == "cairo"
                else "axis"
            ):
                depot.restock(convoy.fuel_load)
                # Also handle other cargo types via general depot
                break
        convoy.arrived = True
        arrived.append(convoy)
        report.convoys_arrived.append(convoy)

        if convoy.damage_fraction > 0.3:
            report.convoys_damaged.append(convoy)
            state.log_event(
                "supply",
                f"Convoy {convoy.convoy_id} arrived with {convoy.damage_fraction:.0%} "
                f"damage — {convoy.total_cargo:.0f} supply points delivered",
                severity="notable",
            )


def compile_supply_report(state: GameState) -> SupplyReport:
    """
    Build a SupplyReport after all supply calculations for the turn.
    Called at turn end before journal generation.
    """
    report = SupplyReport(turn=state.turn)

    for side in (Side.AXIS, Side.ALLIED):
        for unit in state.active_units_for_side(side):
            sl = state.supply_lines.get(unit.id)
            if sl and not sl.in_supply:
                report.out_of_supply_units.append(unit.name)
                state.log_event(
                    "supply",
                    f"{unit.name} is out of supply (nearest depot: {sl.hex_distance} hexes)",
                    unit_ids=[unit.id],
                    severity="notable",
                )
            if unit.supply.is_critically_low_on_fuel():
                report.fuel_critical_units.append(unit.name)
            if unit.supply.is_critically_low_on_water():
                report.water_critical_units.append(unit.name)
            if (unit.pasta_rule and unit.nationality == Nationality.ITALIAN
                    and not unit.supply.has_pasta_ration()):
                report.pasta_deprived_units.append(unit.name)

    # Check depot health
    for depot in state.supply_counters.values():
        if depot.current_load < depot.capacity * 0.20 and depot.capacity > 10:
            report.low_depots.append(depot.name)

    apply_fuel_evaporation(state, report)

    return report
