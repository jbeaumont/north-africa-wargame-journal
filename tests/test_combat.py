"""Tests for the combat resolution engine."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from cna.models.counter import GroundUnit, Nationality, Side, UnitType, SupplyState
from cna.models.hex_map import HexMap, Hex, Terrain
from cna.models.game_state import GameState
from cna.engine.combat import CombatDeclaration, resolve_combat


def _make_map_with_hex(hex_id: str, terrain: Terrain = Terrain.FLAT_DESERT_ROCKY) -> HexMap:
    m = HexMap()
    m.add_hex(Hex(hex_id=hex_id, terrain=terrain, adjacent=[]))
    return m


def _attacker(steps=3, morale=7, ammo=5.0, fuel=5.0) -> GroundUnit:
    u = GroundUnit(
        id="ATT-1",
        name="Attacker Unit",
        nationality=Nationality.GERMAN,
        side=Side.AXIS,
        unit_type=UnitType.ARMORED_REGIMENT,
        cpa=40,
        toe_strength=10,
        morale=morale,
        steps=steps,
        max_steps=steps,
    )
    u.supply.fuel = fuel
    u.supply.ammo = ammo
    u.hex_id = "0101"
    u.available_turn = 1
    return u


def _defender(steps=3, morale=7) -> GroundUnit:
    u = GroundUnit(
        id="DEF-1",
        name="Defender Unit",
        nationality=Nationality.BRITISH,
        side=Side.ALLIED,
        unit_type=UnitType.INFANTRY_REGIMENT,
        cpa=15,
        toe_strength=8,
        morale=morale,
        steps=steps,
        max_steps=steps,
    )
    u.supply.water = 5.0
    u.supply.ammo = 5.0
    u.hex_id = "0102"
    u.available_turn = 1
    return u


class TestCombatResolution:
    def test_combat_produces_result(self):
        m = _make_map_with_hex("0102")
        state = GameState(turn=1, map=m)
        att = _attacker()
        dfn = _defender()
        state.ground_units["ATT-1"] = att
        state.ground_units["DEF-1"] = dfn
        decl = CombatDeclaration("ATT-1", "DEF-1", "0102")
        result = resolve_combat(att, dfn, decl, m, state)
        assert result.attacker_id == "ATT-1"
        assert result.defender_id == "DEF-1"
        assert result.description != ""

    def test_eliminated_attacker_cancels_combat(self):
        from cna.models.counter import UnitStatus
        m = _make_map_with_hex("0102")
        state = GameState(turn=1, map=m)
        att = _attacker()
        att.status = UnitStatus.ELIMINATED
        dfn = _defender()
        decl = CombatDeclaration("ATT-1", "DEF-1", "0102")
        result = resolve_combat(att, dfn, decl, m, state)
        assert "cancelled" in result.description.lower()

    def test_ammo_consumed_in_attack(self):
        m = _make_map_with_hex("0102")
        state = GameState(turn=1, map=m)
        att = _attacker(ammo=10.0)
        dfn = _defender()
        initial_ammo = att.supply.ammo
        decl = CombatDeclaration("ATT-1", "DEF-1", "0102")
        resolve_combat(att, dfn, decl, m, state)
        assert att.supply.ammo < initial_ammo

    def test_out_of_ammo_attacker_penalized(self):
        """Unit with no ammo should have negative attack modifier."""
        # We can verify this indirectly: many combats should average poorly
        import random
        random.seed(42)
        m = _make_map_with_hex("0102")

        wins_with_ammo = 0
        wins_without = 0
        trials = 50

        for _ in range(trials):
            state = GameState(turn=1, map=m)
            att_ammo = _attacker(ammo=10.0)
            att_no_ammo = _attacker(ammo=0.0)
            dfn1 = _defender()
            dfn2 = _defender()
            decl = CombatDeclaration("ATT-1", "DEF-1", "0102")
            r1 = resolve_combat(att_ammo, dfn1, decl, m, state)
            r2 = resolve_combat(att_no_ammo, dfn2, decl, m, state)
            if r1.defender_losses > r1.attacker_losses:
                wins_with_ammo += 1
            if r2.defender_losses > r2.attacker_losses:
                wins_without += 1

        # Attacker with ammo should win more often than without
        assert wins_with_ammo >= wins_without, (
            f"Expected ammo to help: {wins_with_ammo} vs {wins_without}")

    def test_terrain_defense_modifier_applied(self):
        """Escarpment terrain should help the defender."""
        import random
        random.seed(99)

        escarp_map = _make_map_with_hex("0102", Terrain.ROUGH_ESCARPMENT)
        flat_map = _make_map_with_hex("0102", Terrain.FLAT_DESERT_ROCKY)

        escarp_def_wins = 0
        flat_def_wins = 0
        trials = 50

        for _ in range(trials):
            state_e = GameState(turn=1, map=escarp_map)
            state_f = GameState(turn=1, map=flat_map)
            att_e = _attacker()
            att_f = _attacker()
            dfn_e = _defender()
            dfn_f = _defender()
            decl = CombatDeclaration("ATT-1", "DEF-1", "0102")
            r_e = resolve_combat(att_e, dfn_e, decl, escarp_map, state_e)
            r_f = resolve_combat(att_f, dfn_f, decl, flat_map, state_f)
            if r_e.attacker_retreated or r_e.attacker_losses > r_e.defender_losses:
                escarp_def_wins += 1
            if r_f.attacker_retreated or r_f.attacker_losses > r_f.defender_losses:
                flat_def_wins += 1

        # Escarpment should favor defender more often
        assert escarp_def_wins >= flat_def_wins, (
            f"Escarpment should help defender: {escarp_def_wins} vs {flat_def_wins}")

    def test_step_losses_applied_to_units(self):
        """Units that lose combat should have reduced steps."""
        import random
        random.seed(0)

        m = _make_map_with_hex("0102")
        total_loss = 0
        for _ in range(30):
            state = GameState(turn=1, map=m)
            att = _attacker(steps=4)
            dfn = _defender(steps=4)
            decl = CombatDeclaration("ATT-1", "DEF-1", "0102")
            r = resolve_combat(att, dfn, decl, m, state)
            total_loss += r.attacker_losses + r.defender_losses

        assert total_loss > 0  # Some combats should produce losses
