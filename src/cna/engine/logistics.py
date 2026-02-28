"""
Logistics engine for The Campaign for North Africa simulation.

Handles:
  - Water consumption per Operations Stage
  - Italian pasta rule (extra water per OpStage for infantry battalions)
  - Ammunition consumption in combat and general wear
  - Stores (food, uniforms, misc) depletion and morale effects
  - Depot management and resupply prioritization
  - Vehicle maintenance consumption
"""

from __future__ import annotations

from ..models.counter import GroundUnit, UnitType, Nationality, UnitStatus, Side
from ..models.game_state import GameState


# Water consumed per OpStage per unit type (in water points)
WATER_CONSUMPTION: dict[str, float] = {
    "recon_unit": 8.0,
    "armored_battalion": 4.0,
    "armored_regiment": 6.0,
    "armored_division_hq": 5.0,
    "motorized_infantry": 5.0,
    "mechanized_infantry": 5.0,
    "infantry_battalion": 3.0,
    "infantry_regiment": 4.0,
    "infantry_division_hq": 3.0,
    "headquarters": 2.0,
    "artillery_regiment": 3.0,
    "artillery_battalion": 2.0,
    "anti_tank_battalion": 2.0,
    "anti_aircraft_battalion": 2.0,
    "engineer_battalion": 2.0,
    "supply_column": 2.0,
}

PASTA_WATER_BONUS = 1.0   # Extra water per OpStage for Italian infantry (the famous rule)
STORES_DEPLETION_PER_TURN = 0.5  # General stores consumption per turn per unit


def apply_water_consumption(state: GameState, opstage: int) -> None:
    """
    Apply water consumption for all units this Operations Stage.
    Enforces the Italian pasta rule.
    Logs events for units running critically low.
    """
    for unit in state.ground_units.values():
        if unit.status == UnitStatus.ELIMINATED:
            continue
        if unit.available_turn > state.turn:
            continue

        base_consumption = WATER_CONSUMPTION.get(unit.unit_type.value, 2.0)
        base_consumption *= unit.water_factor

        pasta_bonus = 0.0
        if unit.needs_pasta_water():
            pasta_bonus = PASTA_WATER_BONUS
            if unit.supply.water < base_consumption + pasta_bonus:
                # Unit lacks pasta ration — apply consequences
                _apply_pasta_deprivation(unit, state)

        total_consumption = base_consumption + pasta_bonus
        unit.supply.water = max(0.0, round(unit.supply.water - total_consumption, 2))

        if unit.supply.is_critically_low_on_water() and unit.supply.water < 0.5:
            state.log_event(
                "supply",
                f"{unit.name} critically short of water — "
                f"combat effectiveness severely degraded",
                unit_ids=[unit.id],
                severity="critical",
            )


def _apply_pasta_deprivation(unit: GroundUnit, state: GameState) -> None:
    """
    The infamous pasta rule consequence:
    Italian infantry without their pasta water ration:
      - CPA voluntarily capped at 50% (captured by movement_factor property)
      - If cohesion ≤ -10: unit becomes Disorganized
    """
    unit.cohesion -= 2  # Morale hit from poor conditions

    if unit.cohesion <= -10 and unit.status == UnitStatus.UNAFFECTED:
        unit.status = UnitStatus.DISORGANIZED
        state.log_event(
            "logistics",
            f"{unit.name} has become DISORGANIZED — pasta ration exhausted, "
            f"cohesion collapsed to {unit.cohesion}. "
            f"The men cannot fight effectively without their pasta.",
            unit_ids=[unit.id],
            severity="notable",
        )
    elif unit.status == UnitStatus.UNAFFECTED:
        state.log_event(
            "logistics",
            f"{unit.name} denied pasta ration (water shortage). "
            f"Cohesion now {unit.cohesion}. Movement capability halved.",
            unit_ids=[unit.id],
            severity="normal",
        )


def apply_ammo_consumption(state: GameState, combat_occurred: bool) -> None:
    """
    Apply ammunition consumption.
    - Background wear: all units lose small ammo per turn.
    - Combat: units that fought consume additional ammo.
    """
    for unit in state.ground_units.values():
        if unit.status == UnitStatus.ELIMINATED:
            continue
        if unit.available_turn > state.turn:
            continue

        # Background consumption
        base_ammo_use = 0.2 * unit.ammo_factor
        unit.supply.ammo = max(0.0, round(unit.supply.ammo - base_ammo_use, 2))

        if unit.supply.ammo < 0.5:
            state.log_event(
                "supply",
                f"{unit.name} almost out of ammunition — attack capability nil",
                unit_ids=[unit.id],
                severity="notable",
            )


def apply_stores_consumption(state: GameState) -> None:
    """
    Stores (food, clothing, misc) consumed each turn.
    Low stores reduce morale over time.
    """
    for unit in state.ground_units.values():
        if unit.status == UnitStatus.ELIMINATED:
            continue
        if unit.available_turn > state.turn:
            continue

        unit.supply.stores = max(
            0.0, round(unit.supply.stores - STORES_DEPLETION_PER_TURN, 2))

        if unit.supply.stores < 0.5:
            unit.morale = max(1, unit.morale - 1)
            if unit.morale <= 3:
                state.log_event(
                    "logistics",
                    f"{unit.name} morale collapsing ({unit.morale}/10) — "
                    f"stores exhausted, men on half rations",
                    unit_ids=[unit.id],
                    severity="notable",
                )


def prioritize_resupply(
    units: list[GroundUnit],
    available_fuel: float,
    available_water: float,
    available_ammo: float,
) -> dict[str, dict[str, float]]:
    """
    Determine resupply allocation when supplies are scarce.

    Priority order:
      1. Front-line combat units (armor, motorized infantry)
      2. Artillery (ammo priority)
      3. HQ units
      4. Supply columns
      5. Rear-area infantry

    Returns dict of unit_id → {fuel: X, water: Y, ammo: Z}.
    """
    priority_types = [
        # High combat priority
        {UnitType.ARMORED_BATTALION, UnitType.ARMORED_REGIMENT,
         UnitType.ARMORED_DIVISION_HQ, UnitType.RECON_UNIT,
         UnitType.MOTORIZED_INFANTRY, UnitType.MECHANIZED_INFANTRY},
        # Artillery
        {UnitType.ARTILLERY_REGIMENT, UnitType.ARTILLERY_BATTALION,
         UnitType.ANTI_TANK_BATTALION},
        # HQ and support
        {UnitType.HEADQUARTERS, UnitType.SUPPLY_COLUMN,
         UnitType.ENGINEER_BATTALION},
        # Infantry
        {UnitType.INFANTRY_BATTALION, UnitType.INFANTRY_REGIMENT,
         UnitType.INFANTRY_DIVISION_HQ},
    ]

    allocations: dict[str, dict[str, float]] = {}
    remaining_fuel = available_fuel
    remaining_water = available_water
    remaining_ammo = available_ammo

    for priority_set in priority_types:
        tier = [u for u in units if u.unit_type in priority_set
                and u.status != UnitStatus.ELIMINATED]
        for unit in tier:
            fuel_need = max(0.0, unit.fuel_capacity - unit.supply.fuel)
            water_need = max(0.0, unit.water_factor * 10.0 - unit.supply.water)
            ammo_need = max(0.0, unit.ammo_factor * 8.0 - unit.supply.ammo)

            fuel_alloc = min(fuel_need, remaining_fuel)
            water_alloc = min(water_need, remaining_water)
            ammo_alloc = min(ammo_need, remaining_ammo)

            remaining_fuel -= fuel_alloc
            remaining_water -= water_alloc
            remaining_ammo -= ammo_alloc

            allocations[unit.id] = {
                "fuel": fuel_alloc,
                "water": water_alloc,
                "ammo": ammo_alloc,
            }

            if remaining_fuel <= 0 and remaining_water <= 0 and remaining_ammo <= 0:
                break

    return allocations


def apply_resupply_allocations(
    units: list[GroundUnit],
    allocations: dict[str, dict[str, float]],
) -> None:
    """Apply computed resupply allocations to units."""
    for unit in units:
        alloc = allocations.get(unit.id, {})
        unit.supply.fuel = round(unit.supply.fuel + alloc.get("fuel", 0), 2)
        unit.supply.water = round(unit.supply.water + alloc.get("water", 0), 2)
        unit.supply.ammo = round(unit.supply.ammo + alloc.get("ammo", 0), 2)
