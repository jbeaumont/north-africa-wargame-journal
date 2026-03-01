"""
Unit model — represents a single CNA counter on the map.

Covers ground units only (infantry, armor, artillery, etc.).
Air units are a separate concern deferred to Phase 3.

Key CNA concepts modelled here:
  - Steps (strength levels; unit eliminated when steps_current reaches 0)
  - Cohesion (penalty track; -10 or worse triggers Disorganized status)
  - Capability Points (CPA): movement allowance per OpStage; 0 = use formation pool
  - Breakdown Points: accumulated wear on vehicles; triggers breakdown rolls
  - Supply tracking: in_supply / out_of_supply / critical
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
    DISORGANIZED = "disorganized"       # cohesion ≤ -10; halved CPA, combat penalty
    ELIMINATED = "eliminated"           # removed from play
    OFF_MAP = "off_map"                 # not yet on map (reinforcement pool)


class SupplyStatus(str, Enum):
    IN_SUPPLY = "in_supply"
    OUT_OF_SUPPLY = "out_of_supply"
    CRITICAL = "critical"               # 2+ consecutive OpStages out of supply


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
    # At -10 or below the unit is Disorganized (rule 19.0 / 20.0).
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

    # ── Derived helpers ──────────────────────────────────────────────────────

    def is_eliminated(self) -> bool:
        return self.status == UnitStatus.ELIMINATED or self.steps_current <= 0

    def is_active(self) -> bool:
        return self.hex_id is not None and not self.is_eliminated()

    def effective_cpa(self) -> int:
        """CPA after status penalties (disorganized = halved)."""
        base = self.cpa
        if self.status == UnitStatus.DISORGANIZED:
            base = base // 2
        return base

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
        )
