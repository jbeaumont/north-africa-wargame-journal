"""
SupplyDump model — a supply depot on the map.

CNA logistics tracks four resources separately:
  Fuel    (gallons; rule 49.3 evaporation applies each game-turn)
  Ammo    (ammunition points)
  Stores  (general supplies; also consumed by prisoners at 1:5 ratio)
  Water   (water points; rule 52.6 pasta rule adds +1 demand per Italian battalion)

Fuel evaporation (rule 49.3) — applied during Stores Expenditure Stage each turn:
  All players:           6% per game-turn, rounded down
  Commonwealth only      9% per game-turn, Sept 1940 through end of Aug 1941
    (poor British fuel containers; reduced after they adopted the German jerry can)
  Hot weather (29.3):  +5% additional, taken immediately when declared

There is no "drums vs jerry cans" split in the rule.  The 9% Commonwealth rate
covers the entire early-war period regardless of container type.

Dumps can be:
  - Fixed (placed at setup on a specific hex)
  - Unlimited (Alexandria, Cairo, Tripoli — never depleted)
  - Dummy (Axis player can bluff Commonwealth with empty decoys)
  - Port (can receive convoy resupply; limited by port_efficiency per turn)

The SupplyDump is the engine's unit of account.  Supply availability is checked
by rule 32.16: a unit is in supply if a friendly Supply Unit is within ½ of
its CPA, traced as medium-truck movement (or infantry movement for non-mot units),
not passing through impassable terrain or uncontested enemy ZOC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


_RESOURCES = ("fuel", "ammo", "stores", "water")


@dataclass
class SupplyDump:
    # ── Identity ─────────────────────────────────────────────────────────────
    id: str             # unique key, e.g. "cw-dump-tobruk" or "ax-dump-3"
    hex_id: str         # where the dump is on the map
    side: str           # "axis" | "commonwealth"

    # ── Flags ────────────────────────────────────────────────────────────────
    is_unlimited: bool = False  # Alexandria, Cairo, Tripoli — draw freely
    is_dummy: bool = False      # Axis decoy — all loads = 0 and don't reveal
    is_port: bool = False       # can accept convoy deliveries
    port_efficiency: int = 0    # max supply points receivable per turn

    # ── Contents ─────────────────────────────────────────────────────────────
    fuel: float = 0.0
    ammo: float = 0.0
    stores: float = 0.0
    water: float = 0.0

    # ── Label ────────────────────────────────────────────────────────────────
    label: str = ""     # human-readable name ("Tobruk", "Dump 1", etc.)

    # ── Operations ───────────────────────────────────────────────────────────

    def draw(self, resource: str, amount: float) -> float:
        """
        Draw up to `amount` of a resource from this dump.
        Returns actual amount drawn (may be less than requested if depleted).
        Unlimited dumps always return the full amount requested.
        """
        if resource not in _RESOURCES:
            raise ValueError(f"Unknown resource: {resource!r}")
        if self.is_unlimited:
            return amount
        current: float = getattr(self, resource)
        drawn = min(current, amount)
        setattr(self, resource, current - drawn)
        return drawn

    def deposit(self, resource: str, amount: float) -> None:
        """Add supply to this dump (convoy delivery, resupply phase)."""
        if resource not in _RESOURCES:
            raise ValueError(f"Unknown resource: {resource!r}")
        if not self.is_unlimited:
            setattr(self, resource, getattr(self, resource) + amount)

    def apply_fuel_evaporation(self, rate: float) -> float:
        """
        Apply per-turn fuel evaporation (rule 49.3).

        rate values:
          0.06  — standard rate for all players
          0.09  — Commonwealth rate, Sept 1940 through end Aug 1941
          0.05  — additional hot-weather reduction (add to base rate before calling)

        Returns gallons lost (for event log).
        """
        if self.is_unlimited or self.is_dummy:
            return 0.0
        lost = math.floor(self.fuel * rate)  # rule 49.3: "rounded down"
        self.fuel -= lost
        return lost

    def total_contents(self) -> dict[str, float]:
        return {r: getattr(self, r) for r in _RESOURCES}

    def is_empty(self) -> bool:
        return all(getattr(self, r) == 0.0 for r in _RESOURCES)

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "hex_id": self.hex_id,
            "side": self.side,
            "is_unlimited": self.is_unlimited,
            "is_dummy": self.is_dummy,
            "is_port": self.is_port,
            "port_efficiency": self.port_efficiency,
            "fuel": self.fuel,
            "ammo": self.ammo,
            "stores": self.stores,
            "water": self.water,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SupplyDump:
        return cls(
            id=d["id"],
            hex_id=d["hex_id"],
            side=d["side"],
            is_unlimited=d.get("is_unlimited", False),
            is_dummy=d.get("is_dummy", False),
            is_port=d.get("is_port", False),
            port_efficiency=d.get("port_efficiency", 0),
            fuel=d.get("fuel", 0.0),
            ammo=d.get("ammo", 0.0),
            stores=d.get("stores", 0.0),
            water=d.get("water", 0.0),
            label=d.get("label", ""),
        )
