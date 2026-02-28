"""
Master game state for The Campaign for North Africa simulation.

GameState is the single source of truth for an entire game snapshot:
  - All counters (ground units, aircraft, supply elements)
  - The hex map
  - Current turn / operations stage
  - Supply lines
  - Convoy records
  - Event log (for journal generation)

A new GameState is snapshotted at the start of each turn for the journal.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional

from .counter import GroundUnit, AirUnit, SupplyCounter, Side
from .hex_map import HexMap
from .supply import SupplyLine, ConvoyRecord, SupplyReport


# The campaign runs from September 1940 to May 1943 (100 turns, 1 turn = 1 week).
CAMPAIGN_START_DATE = datetime.date(1940, 9, 9)  # Turn 1


def turn_to_date(turn: int) -> datetime.date:
    """Convert a 1-indexed turn number to the corresponding historical week."""
    return CAMPAIGN_START_DATE + datetime.timedelta(weeks=turn - 1)


def turn_to_date_str(turn: int) -> str:
    return turn_to_date(turn).strftime("%d %B %Y")


@dataclass
class Event:
    """
    A discrete game event recorded during turn processing.
    Events are fed to the journal generator to produce narrative entries.
    """
    category: str    # "movement", "combat", "supply", "air", "command", "reinforcement"
    description: str
    unit_ids: list[str] = field(default_factory=list)
    hex_ids: list[str] = field(default_factory=list)
    severity: str = "normal"  # "normal" | "notable" | "critical"


@dataclass
class GameState:
    """
    Complete snapshot of the game at a given point in time.

    The simulation creates one of these per turn and passes it to the
    journal generator, which uses it to produce a narrative entry.
    """
    turn: int
    opstage: int = 0   # 0 = turn start; 1–3 = operations stages within the turn

    # Hex map
    map: HexMap = field(default_factory=HexMap)

    # All unit counters, keyed by unit ID
    ground_units: dict[str, GroundUnit] = field(default_factory=dict)
    air_units: dict[str, AirUnit] = field(default_factory=dict)
    supply_counters: dict[str, SupplyCounter] = field(default_factory=dict)

    # Supply lines calculated this turn
    supply_lines: dict[str, SupplyLine] = field(default_factory=dict)

    # Active convoy records
    convoys: list[ConvoyRecord] = field(default_factory=list)

    # Event log for this turn
    events: list[Event] = field(default_factory=list)

    # Turn supply report (compiled at end of turn)
    supply_report: Optional[SupplyReport] = None

    # Hex control: hex_id → "axis" | "allied"
    hex_control: dict[str, str] = field(default_factory=dict)

    def date_str(self) -> str:
        return turn_to_date_str(self.turn)

    def log_event(self, category: str, description: str,
                  unit_ids: list[str] | None = None,
                  hex_ids: list[str] | None = None,
                  severity: str = "normal") -> None:
        self.events.append(Event(
            category=category,
            description=description,
            unit_ids=unit_ids or [],
            hex_ids=hex_ids or [],
            severity=severity,
        ))

    def units_for_side(self, side: Side) -> list[GroundUnit]:
        return [u for u in self.ground_units.values() if u.side == side]

    def active_units_for_side(self, side: Side) -> list[GroundUnit]:
        from .counter import UnitStatus
        return [
            u for u in self.units_for_side(side)
            if u.status != UnitStatus.ELIMINATED
            and u.available_turn <= self.turn
        ]

    def air_units_for_side(self, side: Side) -> list[AirUnit]:
        return [u for u in self.air_units.values()
                if u.side == side and u.is_serviceable]

    def supply_depots_for_side(self, side: Side) -> list[SupplyCounter]:
        return [s for s in self.supply_counters.values()
                if s.side == side and s.hex_id is not None]

    def out_of_supply_units(self, side: Side) -> list[GroundUnit]:
        return [
            u for u in self.active_units_for_side(side)
            if u.id in self.supply_lines
            and not self.supply_lines[u.id].in_supply
        ]

    def narrative_summary(self) -> str:
        """
        Compact text summary of game state for inclusion in journal prompts.
        """
        from .counter import UnitStatus, Nationality

        axis_total = len(self.active_units_for_side(Side.AXIS))
        allied_total = len(self.active_units_for_side(Side.ALLIED))
        axis_oos = len(self.out_of_supply_units(Side.AXIS))
        allied_oos = len(self.out_of_supply_units(Side.ALLIED))

        # Find frontline positions (hexes occupied by both sides nearby)
        axis_hexes = {u.hex_id for u in self.active_units_for_side(Side.AXIS)
                      if u.hex_id}
        allied_hexes = {u.hex_id for u in self.active_units_for_side(Side.ALLIED)
                        if u.hex_id}

        lines = [
            f"Campaign Turn {self.turn} — {self.date_str()}",
            f"",
            f"AXIS FORCES:  {axis_total} active units | {axis_oos} out of supply",
            f"ALLIED FORCES: {allied_total} active units | {allied_oos} out of supply",
        ]

        # Notable units summary
        notable_axis = [u for u in self.active_units_for_side(Side.AXIS)
                        if u.steps < u.max_steps and u.steps > 0]
        if notable_axis:
            lines.append(f"AXIS DAMAGED: "
                         + ", ".join(f"{u.name} ({u.steps}/{u.max_steps} steps)"
                                     for u in notable_axis[:5]))

        notable_allied = [u for u in self.active_units_for_side(Side.ALLIED)
                          if u.steps < u.max_steps and u.steps > 0]
        if notable_allied:
            lines.append(f"ALLIED DAMAGED: "
                         + ", ".join(f"{u.name} ({u.steps}/{u.max_steps} steps)"
                                     for u in notable_allied[:5]))

        if self.supply_report:
            lines.append("")
            lines.append(self.supply_report.summary_text())

        if self.events:
            lines.append("")
            lines.append("KEY EVENTS THIS TURN:")
            notable = [e for e in self.events if e.severity in ("notable", "critical")]
            for e in notable[:8]:
                lines.append(f"  [{e.category.upper()}] {e.description}")

        return "\n".join(lines)
