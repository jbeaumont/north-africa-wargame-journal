"""
Supply system model for The Campaign for North Africa simulation.

CNA's supply system is the heart of the game. It tracks four commodities:
  - Fuel:   Powers vehicle movement. Evaporates 3%/turn (7% for British).
  - Water:  Required for all movement in the desert. Italian infantry need extra
            for pasta (the famous 'pasta rule').
  - Ammo:   Consumed in combat. Low ammo degrades attack effectiveness.
  - Stores: Food, clothing, misc. Affects morale over time.

Supply depots must physically trace a supply line back to a port or base area.
All units must be within 5 hexes of a supply depot to be considered 'in supply'.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SupplyLine:
    """
    Represents a traced supply line from a unit back to its depot/base.

    CNA requires players to physically trace a path of friendly hexes
    from each unit to a supply source. If no valid path exists, the
    unit is 'out of supply' and suffers severe capability restrictions.
    """
    unit_id: str
    depot_id: Optional[str]           # ID of the supplying SupplyCounter
    path: list[str] = field(default_factory=list)  # Hex IDs from unit → depot
    is_valid: bool = False
    hex_distance: int = 9999

    @property
    def in_supply(self) -> bool:
        """Unit is in supply if it can trace ≤ 5 hexes to a friendly depot."""
        return self.is_valid and self.hex_distance <= 5


@dataclass
class ConvoyRecord:
    """
    Tracks a supply convoy moving along the North African coastal road or by sea.

    Axis convoys sail from Italian ports (Naples, Palermo) to Tripoli or Benghazi.
    Allied convoys sail from Alexandria or Gibraltar, or arrive overland from Cairo.
    Convoys are subject to interdiction by enemy air and naval forces.
    """
    convoy_id: str
    origin: str             # Port hex ID or "naples", "alexandria", etc.
    destination: str        # Port hex ID
    fuel_load: float = 0.0
    water_load: float = 0.0
    ammo_load: float = 0.0
    stores_load: float = 0.0
    # Turn on which convoy departs; arrives in N turns based on distance
    departure_turn: int = 0
    arrival_turn: int = 0
    # Interdiction damage (0.0 = intact, 1.0 = sunk)
    damage_fraction: float = 0.0
    arrived: bool = False

    @property
    def total_cargo(self) -> float:
        return self.fuel_load + self.water_load + self.ammo_load + self.stores_load

    def apply_interdiction(self, damage: float) -> None:
        """
        Apply aerial/naval interdiction. Damage reduces cargo proportionally.
        At 1.0 damage (sunk), all cargo is lost.
        """
        self.damage_fraction = min(1.0, self.damage_fraction + damage)
        loss_factor = 1.0 - self.damage_fraction
        self.fuel_load = round(self.fuel_load * loss_factor, 2)
        self.water_load = round(self.water_load * loss_factor, 2)
        self.ammo_load = round(self.ammo_load * loss_factor, 2)
        self.stores_load = round(self.stores_load * loss_factor, 2)


@dataclass
class SupplyReport:
    """
    End-of-turn supply status report for the journal generator.
    Captures the narrative-worthy supply events from each turn.
    """
    turn: int
    # Units that ran out of fuel this turn
    fuel_critical_units: list[str] = field(default_factory=list)
    # Units that ran out of water this turn
    water_critical_units: list[str] = field(default_factory=list)
    # Units that went out of supply (no depot within 5 hexes)
    out_of_supply_units: list[str] = field(default_factory=list)
    # Italian units that lacked pasta ration
    pasta_deprived_units: list[str] = field(default_factory=list)
    # Convoys that arrived this turn
    convoys_arrived: list[ConvoyRecord] = field(default_factory=list)
    # Convoys that were sunk / damaged
    convoys_damaged: list[ConvoyRecord] = field(default_factory=list)
    # Total fuel evaporated across all units
    fuel_evaporated: float = 0.0
    # Supply depots that ran critically low (< 20% capacity)
    low_depots: list[str] = field(default_factory=list)

    def has_crisis(self) -> bool:
        return bool(
            self.fuel_critical_units
            or self.water_critical_units
            or self.out_of_supply_units
            or self.pasta_deprived_units
            or self.convoys_damaged
        )

    def summary_text(self) -> str:
        lines = [f"Supply Report — Turn {self.turn}"]
        if self.fuel_critical_units:
            lines.append(f"  FUEL CRITICAL: {', '.join(self.fuel_critical_units)}")
        if self.water_critical_units:
            lines.append(f"  WATER CRITICAL: {', '.join(self.water_critical_units)}")
        if self.out_of_supply_units:
            lines.append(f"  OUT OF SUPPLY: {', '.join(self.out_of_supply_units)}")
        if self.pasta_deprived_units:
            lines.append(f"  PASTA DEPRIVED (Italian): {', '.join(self.pasta_deprived_units)}")
        if self.convoys_arrived:
            lines.append(f"  Convoys arrived: {len(self.convoys_arrived)}")
        if self.convoys_damaged:
            lines.append(f"  Convoys damaged/sunk: {len(self.convoys_damaged)}")
        lines.append(f"  Fuel evaporated this turn: {self.fuel_evaporated:.1f} points")
        if self.low_depots:
            lines.append(f"  Low depots: {', '.join(self.low_depots)}")
        return "\n".join(lines)
