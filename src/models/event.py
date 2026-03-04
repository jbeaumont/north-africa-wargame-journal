"""
Event model — a discrete game event emitted by the engine each OpStage.

Events are collected during a turn and written to turns/turn_NNN_events.json.
The Journal Agent reads these events to generate its narrative.

Event types (event.type values)
--------------------------------
  movement        — unit moved from hex_from to hex_to
  combat          — combat resolved between units
  supply          — supply status change (in → OOS, or OOS → in)
  breakdown       — vehicle breakdown roll triggered (unit loses step or is halted)
  fuel_evaporation — periodic fuel loss applied to a dump
  pasta_rule      — Italian battalion missed water ration; may not voluntarily exceed CPA (rule 52.6)
  reinforcement   — new unit arrives on map
  elimination     — unit eliminated (steps reached 0)
  disorganization — unit became Disorganized (cohesion ≤ -10)
  recovery        — unit recovered from Disorganized
  supply_delivery — convoy delivers supply to a port / dump
  fortification   — fort constructed or upgraded
  weather         — weather changed for this OpStage
  initiative      — initiative determined for this OpStage
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Event:
    # ── When ─────────────────────────────────────────────────────────────────
    turn: int
    opstage: int        # 1, 2, or 3

    # ── What ─────────────────────────────────────────────────────────────────
    type: str           # see event type list above
    description: str    # human-readable summary for the Journal Agent

    # ── Who / Where ──────────────────────────────────────────────────────────
    unit_id: Optional[str] = None
    hex_from: Optional[str] = None
    hex_to: Optional[str] = None

    # ── Extra structured data ────────────────────────────────────────────────
    # Type-specific payload, e.g.:
    #   movement:       {"cp_cost": 4.5, "fuel_consumed": 12.0}
    #   combat:         {"attacker_ids": [...], "defender_ids": [...],
    #                    "combat_type": "close_assault",
    #                    "attacker_losses": 1, "defender_losses": 2}
    #   supply:         {"new_status": "out_of_supply", "dump_id": "ax-dump-3"}
    #   breakdown:      {"bd_points": 12, "roll": 3, "result": "breakdown"}
    #   fuel_evaporation: {"dump_id": "cw-dump-alex", "rate": 0.07, "lost": 120.5}
    #   pasta_rule:     {"water_available": 0.8, "water_required": 1.0}
    data: dict = field(default_factory=dict)

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "turn": self.turn,
            "opstage": self.opstage,
            "type": self.type,
            "description": self.description,
            "unit_id": self.unit_id,
            "hex_from": self.hex_from,
            "hex_to": self.hex_to,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Event:
        return cls(
            turn=d["turn"],
            opstage=d["opstage"],
            type=d["type"],
            description=d["description"],
            unit_id=d.get("unit_id"),
            hex_from=d.get("hex_from"),
            hex_to=d.get("hex_to"),
            data=d.get("data", {}),
        )
