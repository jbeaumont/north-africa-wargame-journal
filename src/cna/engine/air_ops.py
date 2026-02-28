"""
Air operations engine for The Campaign for North Africa simulation.

CNA tracks every individual aircraft AND pilot. Air missions include:
  - Air superiority (fighter sweeps, escort interception)
  - Ground support (close air support for ground attacks)
  - Interdiction (attacking supply lines, airfields, ports)
  - Reconnaissance (spotting, photo intelligence)
  - Naval strike (attacking convoys at sea)
  - Transport (supply drops, fuel delivery)

Aircraft wear out (airframe condition degrades with each sortie).
Pilots gain experience (increases effectiveness) but can be killed.
Squadrons below ~30% serviceable aircraft become combat-ineffective.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..models.counter import AirUnit, Pilot, Side
from ..models.game_state import GameState


@dataclass
class AirMissionOrder:
    squadron_id: str
    mission_type: str   # "air_superiority" | "ground_support" | "interdiction" | "recon"
    target_hex: str


@dataclass
class AirMissionResult:
    squadron_id: str
    mission_type: str
    target_hex: str
    success: bool
    aircraft_lost: int
    ground_damage: float   # Suppression effect on ground units (0.0–1.0)
    supply_disrupted: float  # Supply disruption from interdiction (0.0–1.0)
    description: str


def _pilot_effectiveness(pilot: Pilot | None) -> float:
    """Base effectiveness 1.0; better pilots more effective."""
    if not pilot:
        return 1.0
    exp_bonus = min(pilot.experience * 0.02, 0.5)  # Max +50% from XP
    wounded_penalty = -0.3 if pilot.wounded else 0.0
    return max(0.3, 1.0 + exp_bonus + wounded_penalty)


def execute_air_mission(
    squadron: AirUnit,
    order: AirMissionOrder,
    intercepting_squadron: AirUnit | None,
    state: GameState,
) -> AirMissionResult:
    """
    Execute a single air mission.

    If an intercepting squadron is provided, an air battle occurs first.
    Surviving aircraft then execute the mission (degraded if took losses).
    """
    if not squadron.is_serviceable:
        return AirMissionResult(
            squadron_id=squadron.id,
            mission_type=order.mission_type,
            target_hex=order.target_hex,
            success=False,
            aircraft_lost=0,
            ground_damage=0.0,
            supply_disrupted=0.0,
            description=f"{squadron.name} not serviceable — mission scrubbed",
        )

    aircraft_lost = 0
    mission_effectiveness = 1.0

    # Air-to-air combat if intercepted
    if intercepting_squadron and intercepting_squadron.is_serviceable:
        losses, intercept_effectiveness = _air_to_air_combat(
            attacker=squadron,
            defender=intercepting_squadron,
            state=state,
        )
        aircraft_lost += losses
        mission_effectiveness *= (1.0 - intercept_effectiveness * 0.5)

    # Weather and mechanical losses (1-in-12 chance per mission)
    if random.randint(1, 12) == 1:
        aircraft_lost += 1
        squadron.airframe_condition = max(0, squadron.airframe_condition - 5)

    # Pilot experience gain
    if squadron.pilot:
        squadron.pilot.experience += 1
        # Pilot death chance (very low baseline)
        if aircraft_lost > 0 and random.random() < 0.15:
            squadron.pilot.killed_in_action = True
            state.log_event(
                "air",
                f"Pilot of {squadron.name} killed in action on {order.mission_type} mission",
                unit_ids=[squadron.id],
                severity="notable",
            )
            squadron.pilot = None

    # Airframe wear from sortie
    squadron.airframe_condition = max(
        0, squadron.airframe_condition - random.randint(2, 6))
    squadron.sortie_count += 1

    # Mission outcome
    ground_damage = 0.0
    supply_disrupted = 0.0
    success = True

    if order.mission_type == "ground_support":
        ground_damage = mission_effectiveness * random.uniform(0.2, 0.6)
        _apply_ground_support(order.target_hex, ground_damage, state)

    elif order.mission_type == "interdiction":
        supply_disrupted = mission_effectiveness * random.uniform(0.1, 0.4)
        _apply_interdiction(order.target_hex, supply_disrupted, state)

    elif order.mission_type == "air_superiority":
        # Enemy air is suppressed
        pass

    elif order.mission_type == "recon":
        # Spotting info feeds into combat bonuses (simplified)
        pass

    desc = (
        f"{squadron.name} flew {order.mission_type} mission over {order.target_hex}. "
        f"Aircraft lost: {aircraft_lost}. "
        f"Effectiveness: {mission_effectiveness:.0%}. "
        f"Airframe condition: {squadron.airframe_condition}%."
    )

    if aircraft_lost > 0:
        state.log_event(
            "air",
            desc,
            unit_ids=[squadron.id],
            hex_ids=[order.target_hex],
            severity="notable",
        )

    return AirMissionResult(
        squadron_id=squadron.id,
        mission_type=order.mission_type,
        target_hex=order.target_hex,
        success=success and aircraft_lost < 3,
        aircraft_lost=aircraft_lost,
        ground_damage=ground_damage,
        supply_disrupted=supply_disrupted,
        description=desc,
    )


def _air_to_air_combat(
    attacker: AirUnit,
    defender: AirUnit,
    state: GameState,
) -> tuple[int, float]:
    """
    Simple air-to-air combat model.
    Returns (losses to attacker, intercept effectiveness 0-1).
    """
    att_quality = attacker.pilot.experience if attacker.pilot else 3
    def_quality = defender.pilot.experience if defender.pilot else 3

    att_roll = random.randint(1, 10) + att_quality // 5
    def_roll = random.randint(1, 10) + def_quality // 5

    losses = 0
    intercept_eff = 0.0

    if def_roll > att_roll + 2:
        losses = random.randint(1, 2)
        intercept_eff = 0.5
        state.log_event(
            "air",
            f"{defender.name} successfully intercepts {attacker.name} — "
            f"{losses} aircraft shot down",
            unit_ids=[attacker.id, defender.id],
            severity="notable",
        )
    elif def_roll > att_roll:
        losses = 1
        intercept_eff = 0.3
    else:
        # Attacker breaks through; defender takes losses
        if attacker.pilot:
            attacker.pilot.kills += 1
        intercept_eff = 0.1

    return losses, intercept_eff


def _apply_ground_support(hex_id: str, damage: float, state: GameState) -> None:
    """Apply ground support suppression to defending units in hex."""
    for unit in state.ground_units.values():
        if unit.hex_id == hex_id and unit.status != unit.status.ELIMINATED:
            # Suppression reduces combat effectiveness temporarily
            unit.cohesion -= int(damage * 5)
            if damage > 0.3:
                state.log_event(
                    "air",
                    f"Air support strike suppresses {unit.name} at {hex_id} "
                    f"(cohesion now {unit.cohesion})",
                    unit_ids=[unit.id],
                    hex_ids=[hex_id],
                    severity="normal",
                )


def _apply_interdiction(hex_id: str, disruption: float, state: GameState) -> None:
    """Apply interdiction — disrupts supply lines through this hex."""
    for depot in state.supply_counters.values():
        if depot.hex_id == hex_id and disruption > 0.2:
            lost = depot.current_load * disruption
            depot.current_load = max(0, round(depot.current_load - lost, 1))
            if lost > 5:
                state.log_event(
                    "air",
                    f"Interdiction raid hits {depot.name} at {hex_id} — "
                    f"{lost:.0f} supply points destroyed",
                    unit_ids=[depot.id],
                    hex_ids=[hex_id],
                    severity="notable",
                )


def generate_ai_air_orders(
    state: GameState, side_str: str
) -> list[AirMissionOrder]:
    """
    Generate air mission orders for the AI.
    Simple heuristic: prioritize interdiction of enemy supply depots,
    then ground support where combat is expected.
    """
    from ..models.counter import Side
    side = Side(side_str)
    enemy_side = Side.ALLIED if side == Side.AXIS else Side.AXIS
    orders: list[AirMissionOrder] = []

    serviceable = state.air_units_for_side(side)
    if not serviceable:
        return orders

    # Find enemy depot hexes for interdiction targets
    enemy_depots = [d for d in state.supply_counters.values()
                    if d.side == enemy_side and d.hex_id]

    for i, squadron in enumerate(serviceable[:4]):  # Max 4 missions per turn
        if i < len(enemy_depots) and enemy_depots[i].hex_id:
            orders.append(AirMissionOrder(
                squadron_id=squadron.id,
                mission_type="interdiction",
                target_hex=enemy_depots[i].hex_id,
            ))
        else:
            # Default to air superiority patrol
            orders.append(AirMissionOrder(
                squadron_id=squadron.id,
                mission_type="air_superiority",
                target_hex=squadron.base_hex or "1702",
            ))

    return orders
