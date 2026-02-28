"""
Unit counter models for The Campaign for North Africa simulation.

CNA uses ~1,600 cardboard counters covering ground units (battalion to division),
individual aircraft + pilots, and supply elements (fuel dumps, water trucks,
ammo depots). This module models all counter types as Python dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Nationality(str, Enum):
    ITALIAN = "IT"
    GERMAN = "GE"
    BRITISH = "GB"
    COMMONWEALTH = "CW"  # Australian, Indian, NZ, South African
    AMERICAN = "US"
    FREE_FRENCH = "FF"


class Side(str, Enum):
    AXIS = "axis"
    ALLIED = "allied"


class UnitType(str, Enum):
    # Ground combat units
    INFANTRY_DIVISION_HQ = "infantry_division_hq"
    INFANTRY_REGIMENT = "infantry_regiment"
    INFANTRY_BATTALION = "infantry_battalion"
    ARMORED_DIVISION_HQ = "armored_division_hq"
    ARMORED_REGIMENT = "armored_regiment"
    ARMORED_BATTALION = "armored_battalion"
    ARTILLERY_REGIMENT = "artillery_regiment"
    ARTILLERY_BATTALION = "artillery_battalion"
    ENGINEER_BATTALION = "engineer_battalion"
    RECON_UNIT = "recon_unit"
    MOTORIZED_INFANTRY = "motorized_infantry"
    MECHANIZED_INFANTRY = "mechanized_infantry"
    ANTI_TANK_BATTALION = "anti_tank_battalion"
    ANTI_AIRCRAFT_BATTALION = "anti_aircraft_battalion"
    # Support
    HEADQUARTERS = "headquarters"
    SUPPLY_COLUMN = "supply_column"
    # Supply counters
    FUEL_DUMP = "fuel_dump"
    WATER_TRUCK = "water_truck"
    AMMO_DEPOT = "ammo_depot"
    SUPPLY_DEPOT = "supply_depot"
    # Air
    FIGHTER_SQUADRON = "fighter_squadron"
    BOMBER_SQUADRON = "bomber_squadron"
    TRANSPORT_SQUADRON = "transport_squadron"
    RECON_SQUADRON = "recon_squadron"


class UnitStatus(str, Enum):
    """Current readiness status of a unit."""
    UNAFFECTED = "unaffected"   # Fully operational
    PINNED = "pinned"           # Cannot retreat before assault; reduced movement
    DISORGANIZED = "disorganized"  # Severely degraded; needs to rally
    ELIMINATED = "eliminated"   # Removed from play


@dataclass
class SupplyState:
    """
    Per-unit supply tracking. All quantities in 'supply points'.

    Fuel evaporation: 3%/turn for all non-British (jerry cans).
                      7%/turn for British (50-gallon drums, less efficient).
    Water consumed: varies by unit type; Italian infantry +1/OpStage for pasta.
    """
    fuel: float = 0.0
    water: float = 0.0
    ammo: float = 0.0
    stores: float = 0.0  # Food, uniforms, misc

    def apply_evaporation(self, nationality: Nationality) -> None:
        """Apply end-of-turn fuel evaporation."""
        rate = 0.07 if nationality == Nationality.BRITISH else 0.03
        self.fuel = round(self.fuel * (1.0 - rate), 2)

    def is_critically_low_on_fuel(self) -> bool:
        return self.fuel < 1.0

    def is_critically_low_on_water(self) -> bool:
        return self.water < 1.0

    def has_pasta_ration(self) -> bool:
        """Italian infantry needs water for pasta; this checks if supplied."""
        return self.water >= 2.0


@dataclass
class Counter:
    """Base class for all CNA counters."""
    id: str
    name: str
    nationality: Nationality
    side: Side
    unit_type: UnitType
    # Position on the hex map (hex id string, e.g. "0320")
    hex_id: Optional[str] = None
    # Turn on which this unit enters play (1-indexed; turn 1 = Sep 1940)
    available_turn: int = 1
    status: UnitStatus = UnitStatus.UNAFFECTED


@dataclass
class GroundUnit(Counter):
    """
    A ground combat or support unit.

    cpa:          Capability Point Allowance — basic movement budget per OpStage.
                  (Recce ~45, motorized ~25, infantry ~15, foot infantry ~10)
    toe_strength: Table of Organization strength (max steps × step value)
    morale:       0–10; affects combat resolution and rallying
    cohesion:     Running modifier; −10 or worse → Disorganized without supplies
    steps:        Current steps remaining (each step = 1 strength point lost when hit)
    max_steps:    Starting step count
    pasta_rule:   True for Italian infantry battalions — needs extra water ration
    """
    cpa: int = 15
    toe_strength: int = 6
    morale: int = 6
    cohesion: int = 0
    steps: int = 2
    max_steps: int = 2
    supply: SupplyState = field(default_factory=SupplyState)
    fuel_capacity: float = 3.0
    water_factor: float = 1.0
    ammo_factor: float = 1.0
    pasta_rule: bool = False
    # IDs of subordinate battalion/regiment counters
    subordinates: list[str] = field(default_factory=list)
    # ID of parent HQ
    parent_id: Optional[str] = None

    @property
    def is_infantry(self) -> bool:
        return self.unit_type in {
            UnitType.INFANTRY_BATTALION,
            UnitType.INFANTRY_REGIMENT,
            UnitType.INFANTRY_DIVISION_HQ,
            UnitType.MOTORIZED_INFANTRY,
            UnitType.MECHANIZED_INFANTRY,
        }

    @property
    def is_armor(self) -> bool:
        return self.unit_type in {
            UnitType.ARMORED_BATTALION,
            UnitType.ARMORED_REGIMENT,
            UnitType.ARMORED_DIVISION_HQ,
        }

    @property
    def movement_factor(self) -> float:
        """Effective movement; reduced when pinned or disorganized."""
        if self.status == UnitStatus.DISORGANIZED:
            return self.cpa * 0.5
        if self.status == UnitStatus.PINNED:
            return self.cpa * 0.75
        return float(self.cpa)

    def is_in_supply(self) -> bool:
        return not (self.supply.is_critically_low_on_fuel()
                    and self.unit_type not in {UnitType.INFANTRY_BATTALION,
                                               UnitType.INFANTRY_REGIMENT})

    def apply_step_loss(self, losses: int = 1) -> None:
        self.steps = max(0, self.steps - losses)
        if self.steps == 0:
            self.status = UnitStatus.ELIMINATED

    def needs_pasta_water(self) -> bool:
        """Return True if this unit requires a pasta water ration this OpStage."""
        return (self.pasta_rule
                and self.nationality == Nationality.ITALIAN
                and self.is_infantry)


@dataclass
class Pilot:
    """Individual pilot record — CNA tracks every pilot across the campaign."""
    id: str
    name: str
    nationality: Nationality
    experience: int = 0    # Missions flown; affects combat quality
    kills: int = 0
    wounded: bool = False
    killed_in_action: bool = False
    prisoner_of_war: bool = False


@dataclass
class AirUnit(Counter):
    """
    An individual aircraft counter.

    CNA tracks every aircraft AND its pilot separately. Aircraft wear out;
    pilots gain experience (or die). This is one of the game's most
    notorious record-keeping burdens.
    """
    aircraft_type: str = "unknown"
    # 0–100; below ~30 the aircraft is unserviceable
    airframe_condition: int = 100
    pilot: Optional[Pilot] = None
    # Missions: "interdiction", "ground_support", "air_superiority", "recon", "transport"
    current_mission: Optional[str] = None
    sortie_count: int = 0
    base_hex: Optional[str] = None  # Airfield hex

    @property
    def is_serviceable(self) -> bool:
        return (self.airframe_condition >= 30
                and self.status != UnitStatus.ELIMINATED)

    def fly_mission(self, mission_type: str) -> None:
        self.current_mission = mission_type
        self.sortie_count += 1
        if self.pilot:
            self.pilot.experience += 1


@dataclass
class SupplyCounter(Counter):
    """
    A supply element counter (fuel dump, water truck, ammo depot, etc.).

    These are separate physical counters on the map and must be physically
    moved by transport units — one of CNA's most demanding logistics challenges.
    """
    capacity: float = 100.0
    current_load: float = 100.0
    supply_type: str = "general"  # "fuel", "water", "ammo", "stores"
    transport_hex: Optional[str] = None  # Hex if being transported by truck

    @property
    def load_fraction(self) -> float:
        if self.capacity == 0:
            return 0.0
        return self.current_load / self.capacity

    def draw(self, amount: float) -> float:
        """Draw up to `amount` from this depot; return actual amount drawn."""
        actual = min(amount, self.current_load)
        self.current_load -= actual
        return actual

    def restock(self, amount: float) -> None:
        self.current_load = min(self.capacity, self.current_load + amount)
