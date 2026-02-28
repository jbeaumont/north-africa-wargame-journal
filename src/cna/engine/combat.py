"""
Combat resolution engine for The Campaign for North Africa simulation.

CNA combat rules:
  - Impulse-based: phasing player moves, then non-phasing player reacts.
  - Combat is declared at the end of the movement phase.
  - Resolution: 2d6 + terrain modifier + leader modifier.
  - Results: step losses, retreat, pin, no effect.
  - Attacking costs ammunition.
  - Low ammo drastically reduces attack effectiveness.
  - Units with 0 steps are eliminated.

The game deliberately de-emphasizes combat relative to logistics —
you can "win" the logistics battle and watch the enemy crumble.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..models.counter import GroundUnit, UnitStatus
from ..models.hex_map import HexMap
from ..models.game_state import GameState


@dataclass
class CombatDeclaration:
    attacker_id: str
    defender_id: str
    combat_hex: str


@dataclass
class CombatResult:
    attacker_id: str
    defender_id: str
    attacker_roll: int
    defender_roll: int
    final_attacker: int  # After modifiers
    final_defender: int
    attacker_losses: int   # Step losses
    defender_losses: int
    defender_retreated: bool
    attacker_retreated: bool
    description: str


# Modifier to attacker's roll based on supply status
def _supply_attack_modifier(unit: GroundUnit) -> int:
    mod = 0
    if unit.supply.is_critically_low_on_fuel():
        mod -= 2
    if unit.supply.ammo < 1.0:
        mod -= 3
    if unit.supply.is_critically_low_on_water():
        mod -= 1
    return mod


def _supply_defense_modifier(unit: GroundUnit) -> int:
    mod = 0
    if unit.supply.is_critically_low_on_water():
        mod -= 1
    return mod


def _morale_modifier(unit: GroundUnit) -> int:
    """High morale gives +1; low morale gives -1."""
    if unit.morale >= 9:
        return 1
    if unit.morale <= 4:
        return -1
    return 0


def resolve_combat(
    attacker: GroundUnit,
    defender: GroundUnit,
    declaration: CombatDeclaration,
    hex_map: HexMap,
    state: GameState,
) -> CombatResult:
    """
    Resolve a single combat engagement.

    Attacker roll = 2d6 + supply_mod + morale_mod + strength_ratio_mod
    Defender roll = 2d6 + terrain_mod + supply_mod + morale_mod

    Result table (net = attacker_final - defender_final):
      ≥ +4:  Defender loses 2 steps, must retreat 2 hexes
      +2,+3: Defender loses 1 step, retreat 1 hex
      0,+1:  Defender pinned; attacker loses 1 step
      -1,-2: No effect; both sides disrupted
      ≤ -3:  Attacker retreats 2 hexes, loses 1 step
    """
    if (attacker.status == UnitStatus.ELIMINATED
            or defender.status == UnitStatus.ELIMINATED):
        return CombatResult(
            attacker_id=attacker.id, defender_id=defender.id,
            attacker_roll=0, defender_roll=0,
            final_attacker=0, final_defender=0,
            attacker_losses=0, defender_losses=0,
            defender_retreated=False, attacker_retreated=False,
            description="Combat cancelled — unit eliminated",
        )

    # Dice rolls
    att_roll = random.randint(1, 6) + random.randint(1, 6)
    def_roll = random.randint(1, 6) + random.randint(1, 6)

    # Modifiers
    terrain_mod = 0
    combat_hex = hex_map.get(declaration.combat_hex)
    if combat_hex:
        terrain_mod = combat_hex.defense_modifier()

    att_supply_mod = _supply_attack_modifier(attacker)
    def_supply_mod = _supply_defense_modifier(defender)
    att_morale_mod = _morale_modifier(attacker)
    def_morale_mod = _morale_modifier(defender)

    # Strength ratio modifier (rough force ratio)
    strength_mod = 0
    if attacker.steps > 0 and defender.steps > 0:
        ratio = attacker.toe_strength / max(defender.toe_strength, 1)
        if ratio >= 3:
            strength_mod = 2
        elif ratio >= 2:
            strength_mod = 1
        elif ratio <= 0.5:
            strength_mod = -2

    final_att = att_roll + att_supply_mod + att_morale_mod + strength_mod
    final_def = def_roll + terrain_mod + def_supply_mod + def_morale_mod

    net = final_att - final_def

    attacker_losses = 0
    defender_losses = 0
    defender_retreated = False
    attacker_retreated = False

    if net >= 4:
        defender_losses = 2
        defender_retreated = True
        result_text = "DECISIVE attacker victory"
    elif net in (2, 3):
        defender_losses = 1
        defender_retreated = True
        result_text = "Attacker victory"
    elif net in (0, 1):
        attacker_losses = 1
        defender.status = UnitStatus.PINNED
        result_text = "Defender holds; attacker takes casualties"
    elif net in (-1, -2):
        result_text = "Inconclusive engagement"
    else:  # net <= -3
        attacker_losses = 1
        attacker_retreated = True
        result_text = "Attacker repulsed"

    # Apply losses
    if attacker_losses > 0:
        attacker.apply_step_loss(attacker_losses)
    if defender_losses > 0:
        defender.apply_step_loss(defender_losses)

    # Ammo consumed in attack
    ammo_used = 1.5 * attacker.ammo_factor
    attacker.supply.ammo = max(0.0, round(attacker.supply.ammo - ammo_used, 2))

    # Handle retreats (simplified: move to adjacent hex away from attacker)
    if defender_retreated and defender.hex_id and attacker.hex_id:
        _retreat_unit(defender, attacker.hex_id, hex_map, state)
    if attacker_retreated and attacker.hex_id and defender.hex_id:
        _retreat_unit(attacker, defender.hex_id, hex_map, state)

    description = (
        f"{attacker.name} vs {defender.name} at {declaration.combat_hex}: "
        f"{result_text} (att {final_att} vs def {final_def})"
        f" — Losses: att -{attacker_losses} steps, def -{defender_losses} steps"
    )

    state.log_event(
        "combat",
        description,
        unit_ids=[attacker.id, defender.id],
        hex_ids=[declaration.combat_hex],
        severity="notable" if (attacker_losses + defender_losses > 0) else "normal",
    )

    return CombatResult(
        attacker_id=attacker.id,
        defender_id=defender.id,
        attacker_roll=att_roll,
        defender_roll=def_roll,
        final_attacker=final_att,
        final_defender=final_def,
        attacker_losses=attacker_losses,
        defender_losses=defender_losses,
        defender_retreated=defender_retreated,
        attacker_retreated=attacker_retreated,
        description=description,
    )


def _retreat_unit(unit: GroundUnit, toward_hex: str, hex_map: HexMap,
                  state: GameState) -> None:
    """Move a unit away from toward_hex by one or two hexes."""
    if not unit.hex_id:
        return
    h = hex_map.get(unit.hex_id)
    if not h:
        return

    # Pick the adjacent hex that maximizes distance from toward_hex
    candidates = [
        adj for adj in h.adjacent
        if adj in [x.hex_id for x in hex_map.adjacent_hexes(unit.hex_id)]
        and adj != toward_hex
    ]
    if candidates:
        # Prefer hex away from the enemy
        retreat_hex = candidates[0]  # Simplified
        old_hex = unit.hex_id
        unit.hex_id = retreat_hex
        state.hex_control[retreat_hex] = "axis" if unit.side.value == "axis" else "allied"
        state.log_event(
            "combat",
            f"{unit.name} retreats from {old_hex} to {retreat_hex}",
            unit_ids=[unit.id],
            hex_ids=[old_hex, retreat_hex],
        )


def generate_ai_combat_declarations(
    state: GameState, phasing_side: str
) -> list[CombatDeclaration]:
    """
    Determine which combats to declare for the phasing side.
    Declares combat when a unit is adjacent to an enemy unit.
    """
    from ..models.counter import Side
    side = Side(phasing_side)
    enemy_side = Side.ALLIED if side == Side.AXIS else Side.AXIS
    declarations = []

    # Build enemy unit hex lookup
    enemy_hexes: dict[str, GroundUnit] = {}
    for unit in state.active_units_for_side(enemy_side):
        if unit.hex_id:
            enemy_hexes[unit.hex_id] = unit

    for unit in state.active_units_for_side(side):
        if not unit.hex_id or unit.status == UnitStatus.ELIMINATED:
            continue
        if unit.supply.ammo < 0.5:
            continue  # No ammo — cannot attack

        h = state.map.get(unit.hex_id)
        if not h:
            continue

        for adj_hex_id in h.adjacent:
            if adj_hex_id in enemy_hexes:
                enemy_unit = enemy_hexes[adj_hex_id]
                declarations.append(CombatDeclaration(
                    attacker_id=unit.id,
                    defender_id=enemy_unit.id,
                    combat_hex=adj_hex_id,
                ))
                break  # Each unit only attacks once per impulse

    return declarations
