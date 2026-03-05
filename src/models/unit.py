"""
Unit model — represents a single CNA counter on the map.

Covers ground units only (infantry, armor, artillery, etc.).
Air units are a separate concern deferred to Phase 3.

Key CNA concepts modelled here:
  - Steps (strength levels; unit eliminated when steps_current reaches 0)
  - Cohesion (penalty track; affects combat morale via Section 17.0; at -26 the unit
    cannot move, attack, or defend, rule 6.26). The -10 threshold is ONLY for Italian
    battalions under the Pasta Rule (rule 52.6) — no general -10 status exists.
  - Capability Points (CPA): movement allowance per OpStage; 0 = use formation pool
  - Breakdown Points: accumulated wear on vehicles; triggers breakdown rolls
  - Supply tracking: in_supply / out_of_supply
  - Logistics loads: fuel, water, ammo, stores carried on the unit
  - Pasta rule flag: Italian infantry battalions need +1 water ration
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    AXIS = "axis"
    COMMONWEALTH = "commonwealth"


class Nationality(str, Enum):
    # Commonwealth
    BRITISH = "british"
    AUSTRALIAN = "australian"
    NEW_ZEALAND = "new_zealand"
    SOUTH_AFRICAN = "south_african"
    INDIAN = "indian"
    POLISH = "polish"
    FRENCH = "french"
    GREEK = "greek"
    AMERICAN = "american"
    # Axis
    GERMAN = "german"
    ITALIAN = "italian"


class UnitType(str, Enum):
    INFANTRY = "infantry"
    ARMOR = "armor"
    ARTILLERY = "artillery"
    ANTI_TANK = "anti_tank"
    ANTI_AIRCRAFT = "anti_aircraft"
    RECONNAISSANCE = "reconnaissance"
    ENGINEER = "engineer"
    HQ = "hq"
    GARRISON = "garrison"
    SUPPLY = "supply"           # supply depot counter
    TRUCK = "truck"             # truck column


class UnitSize(str, Enum):
    COMPANY = "company"
    BATTALION = "battalion"
    REGIMENT = "regiment"
    BRIGADE = "brigade"
    DIVISION = "division"
    CORPS = "corps"
    ARMY = "army"


class UnitStatus(str, Enum):
    ACTIVE = "active"
    BROKEN_DOWN = "broken_down"         # vehicle breakdown; can't move this OpStage
    DISORGANIZED = "disorganized"       # rule 6.26: at -26, cannot move/attack/defend.
                                        # Also used by pasta rule (52.6) to flag
                                        # Italian units treated "as if cohesion = -26".
    ELIMINATED = "eliminated"           # removed from play
    OFF_MAP = "off_map"                 # not yet on map (reinforcement pool)


class SupplyStatus(str, Enum):
    IN_SUPPLY = "in_supply"
    OUT_OF_SUPPLY = "out_of_supply"
    CRITICAL = "critical"               # PLACEHOLDER: 'Critical' as a named status is NOT
                                        # found in the CNA PDF. Actual OOS effects:
                                        # rule 51.21: +1 DP per game-turn without Stores;
                                        # rule 51.22: 2% TOE SP loss per 2 consecutive
                                        # game-turns (infantry only).
                                        # TODO: replace with rule 51.21/51.22 effects.


@dataclass
class Unit:
    # ── Identity ─────────────────────────────────────────────────────────────
    id: str                             # unique key, e.g. "BR-70-INF-DIV"
    name: str                           # display name
    nationality: Nationality
    side: Side
    type: UnitType
    size: UnitSize
    motorized: bool = True              # affects terrain CP costs & breakdown

    # ── Position & status ────────────────────────────────────────────────────
    hex_id: Optional[str] = None        # current hex; None = eliminated / off-map
    status: UnitStatus = UnitStatus.ACTIVE
    formation_id: Optional[str] = None  # parent formation for CP pooling
    org_flags: str = ""                 # raw Less/Assg/Att annotation from scenario

    # ── Strength ─────────────────────────────────────────────────────────────
    steps_current: int = 2
    steps_max: int = 2
    # Cohesion track: starts at 0; combat, OOS, and failed checks reduce it.
    # Negative cohesion affects combat morale continuously (Section 17.0).
    # At -26 or worse: cannot move, attack, or defend (rule 6.26).
    # The -10 threshold is ONLY used in the Italian Pasta Rule (rule 52.6).
    cohesion: int = 0

    # ── Capability Points (movement) ─────────────────────────────────────────
    cpa: int = 0                        # 0 = inherits from parent formation
    cp_remaining: float = 0.0           # leftover CP this OpStage
    breakdown_points: float = 0.0       # accumulated BD points this OpStage

    # ── Supply tracking ──────────────────────────────────────────────────────
    supply_status: SupplyStatus = SupplyStatus.IN_SUPPLY
    opstages_out_of_supply: int = 0     # consecutive OOS count

    # ── On-unit logistics loads (in game points) ─────────────────────────────
    fuel: float = 0.0
    fuel_capacity: float = 0.0          # max fuel the unit can carry
    water: float = 0.0
    ammo: float = 0.0
    stores: float = 0.0

    # ── Special rules ────────────────────────────────────────────────────────
    # True for Italian infantry battalions (rule 52.6).
    # Each battalion must receive 1 extra Water Point when Stores are distributed.
    # Missing Pasta Point → unit may NOT voluntarily exceed its CPA that Turn
    #   (it can still spend up to its full CPA; it just cannot go over it in
    #    special circumstances where exceeding CPA would normally be allowed).
    # If cohesion is already ≤ -10 AND no Pasta Point → immediately Disorganized
    #   as if cohesion reached -26.
    # Recovery: as soon as unit receives its Pasta Point, cohesion reverts to
    #   the level it had before disorganization.
    pasta_rule: bool = False
    # Set by the supply engine each OpStage when pasta_rule=True and the unit
    # did NOT receive its Pasta Point.  Cleared when the Pasta Point arrives.
    # The movement engine checks this flag to block voluntary CPA excess (52.6).
    pasta_restricted: bool = False

    # ── Derived helpers ──────────────────────────────────────────────────────

    def is_eliminated(self) -> bool:
        return self.status == UnitStatus.ELIMINATED or self.steps_current <= 0

    def is_active(self) -> bool:
        return self.hex_id is not None and not self.is_eliminated()

    def effective_cpa(self) -> int:
        """CPA for movement purposes.

        Rule 6.26: a unit at cohesion -26 (or pasta-rule forced 'as if -26')
        'may not move' — effective CPA is 0, not halved.
        Note: supply-range checks use the natural CPA (rule 32.16); see
        GameState.formation_cpa_natural().
        """
        if self.status == UnitStatus.DISORGANIZED:
            return 0  # rule 6.26: may not move, attack, or defend
        return self.cpa

    def stacking_points(self) -> int:
        """Stacking cost in Stacking Points for hex capacity checks."""
        # HQs and supply counters take no stacking space.
        if self.type in (UnitType.HQ, UnitType.SUPPLY):
            return 0
        return 1

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "nationality": self.nationality.value,
            "side": self.side.value,
            "type": self.type.value,
            "size": self.size.value,
            "motorized": self.motorized,
            "hex_id": self.hex_id,
            "status": self.status.value,
            "formation_id": self.formation_id,
            "org_flags": self.org_flags,
            "steps_current": self.steps_current,
            "steps_max": self.steps_max,
            "cohesion": self.cohesion,
            "cpa": self.cpa,
            "cp_remaining": self.cp_remaining,
            "breakdown_points": self.breakdown_points,
            "supply_status": self.supply_status.value,
            "opstages_out_of_supply": self.opstages_out_of_supply,
            "fuel": self.fuel,
            "fuel_capacity": self.fuel_capacity,
            "water": self.water,
            "ammo": self.ammo,
            "stores": self.stores,
            "pasta_rule": self.pasta_rule,
            "pasta_restricted": self.pasta_restricted,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Unit:
        return cls(
            id=d["id"],
            name=d["name"],
            nationality=Nationality(d["nationality"]),
            side=Side(d["side"]),
            type=UnitType(d["type"]),
            size=UnitSize(d["size"]),
            motorized=d.get("motorized", True),
            hex_id=d.get("hex_id"),
            status=UnitStatus(d.get("status", "active")),
            formation_id=d.get("formation_id"),
            org_flags=d.get("org_flags", ""),
            steps_current=d.get("steps_current", 2),
            steps_max=d.get("steps_max", 2),
            cohesion=d.get("cohesion", 0),
            cpa=d.get("cpa", 0),
            cp_remaining=d.get("cp_remaining", 0.0),
            breakdown_points=d.get("breakdown_points", 0.0),
            supply_status=SupplyStatus(d.get("supply_status", "in_supply")),
            opstages_out_of_supply=d.get("opstages_out_of_supply", 0),
            fuel=d.get("fuel", 0.0),
            fuel_capacity=d.get("fuel_capacity", 0.0),
            water=d.get("water", 0.0),
            ammo=d.get("ammo", 0.0),
            stores=d.get("stores", 0.0),
            pasta_rule=d.get("pasta_rule", False),
            pasta_restricted=d.get("pasta_restricted", False),
        )
