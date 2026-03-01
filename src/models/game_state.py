"""
GameState — the master state of a CNA game at a point in time.

This is the single source of truth for the Board State Agent.  All other
agents (player agents, Rules Arbiter, Journal) receive either a full snapshot
or a fog-of-war view derived from this object.

Key structures
--------------
  units         dict[unit_id → Unit]
  hexes         dict[hex_id  → Hex]       (sparse; only populated hexes needed)
  supply_dumps  dict[dump_id → SupplyDump]
  formations    dict[formation_id → Formation]  (for CP pooling)
  minefields    dict[hex_id  → Minefield]  (dynamic; changes during game)
  fortifications dict[hex_id → int]        (current level; starts from scenario setup)

Turn structure
--------------
  The full CNA campaign is 111 turns × 3 OpStages each.
  Turn 1 OpStage 1 = 9 September 1940 (Italian Offensive).
  Crusader scenario starts at Turn 57 / OpStage 3 = 18 November 1941.

Formation trees (rule 19.0)
----------------------------
  Units belong to parent formations.  CP allowance is pooled across a
  formation's children (rule 6.15).  Detached units get their own CPA.
  The engine uses Formation.cp_pool when a unit's own cpa == 0.

Fog of war
----------
  fog_of_war(side) returns a dict omitting enemy units that are not adjacent
  to any friendly unit.  Player agents receive this view, not the full state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from src.models.unit import Unit, Side, SupplyStatus
from src.models.hex import Hex
from src.models.supply import SupplyDump
from src.models.event import Event


# ── Calendar ────────────────────────────────────────────────────────────────

# CNA campaign anchor dates (from scenario setup sheets).
# The game covers Sep 1940 – May 1943 in 111 turns, but the turns are NOT
# exactly 7 days each (~8.8 days/turn on average).  No simple linear formula
# works across all scenarios.  GameState therefore stores the current date
# explicitly; the scenario loader sets it, and each OpStage advances it.
#
# Known anchors:
#   GT1  / OS1 = 1940-09-09  (Italian Offensive)
#   GT26 / OS3 = 1941-03-24  (Desert Fox / Rommel arrives)
#   GT57 / OS3 = 1941-11-18  (Operation Crusader)
#   GT111/ OS3 = 1943-05-13  (Campaign end)
CAMPAIGN_START_DATE = date(1940, 9, 9)


# ── Formation ────────────────────────────────────────────────────────────────

@dataclass
class Formation:
    """
    A command node in the formation hierarchy.

    child_ids may be unit_ids (leaf units) or other formation_ids (sub-formations).
    The engine resolves the tree to compute CP pooling for the OpStage.
    """
    id: str
    name: str
    side: Side
    hq_unit_id: Optional[str]           # the HQ counter that leads this formation
    child_ids: list[str] = field(default_factory=list)
    cpa: int = 0                        # formation CPA; inherited by children with cpa==0
    cp_pool: float = 0.0                # shared CP pool refreshed each OpStage

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "side": self.side.value,
            "hq_unit_id": self.hq_unit_id,
            "child_ids": self.child_ids,
            "cpa": self.cpa,
            "cp_pool": self.cp_pool,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Formation:
        return cls(
            id=d["id"],
            name=d["name"],
            side=Side(d["side"]),
            hq_unit_id=d.get("hq_unit_id"),
            child_ids=d.get("child_ids", []),
            cpa=d.get("cpa", 0),
            cp_pool=d.get("cp_pool", 0.0),
        )


# ── Minefield ────────────────────────────────────────────────────────────────

@dataclass
class Minefield:
    """
    A minefield overlaying a hex.

    side = who laid it (determines "friendly" vs "enemy" from each player's
    perspective — same object, different meaning by viewer).
    is_dummy = Axis bluff counter (no actual effect on movement/combat).
    revealed = enemy knows this minefield exists (e.g. after a unit trips it).
    """
    hex_id: str
    side: str           # "axis" | "commonwealth"
    is_dummy: bool = False
    revealed: bool = False

    def to_dict(self) -> dict:
        return {
            "hex_id": self.hex_id,
            "side": self.side,
            "is_dummy": self.is_dummy,
            "revealed": self.revealed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Minefield:
        return cls(
            hex_id=d["hex_id"],
            side=d["side"],
            is_dummy=d.get("is_dummy", False),
            revealed=d.get("revealed", False),
        )


# ── GameState ────────────────────────────────────────────────────────────────

@dataclass
class GameState:
    # ── Scenario & turn ──────────────────────────────────────────────────────
    scenario: str                           # e.g. "crusader", "italian_campaign"
    turn: int                               # absolute campaign turn (1–111)
    opstage: int                            # 1, 2, or 3

    # ── Core game objects ────────────────────────────────────────────────────
    units: dict[str, Unit] = field(default_factory=dict)
    hexes: dict[str, Hex] = field(default_factory=dict)
    supply_dumps: dict[str, SupplyDump] = field(default_factory=dict)
    formations: dict[str, Formation] = field(default_factory=dict)
    minefields: dict[str, Minefield] = field(default_factory=dict)
    # Current fortification level per hex (0 = none; overrides hex.base_fortification_level
    # once the game starts modifying it).
    fortifications: dict[str, int] = field(default_factory=dict)

    # ── Turn metadata ────────────────────────────────────────────────────────
    # current_date: set by the scenario loader to the correct historical date.
    # Falls back to CAMPAIGN_START_DATE if not explicitly set.
    current_date: Optional[date] = None

    weather: str = "clear"              # "clear" | "rainstorm" | "khamsin"
    initiative: str = "commonwealth"    # who has initiative this OpStage

    # ── Turn event log ───────────────────────────────────────────────────────
    # Reset at the start of each OpStage; written to disk at OpStage end.
    events: list[Event] = field(default_factory=list)

    # ── Calendar helpers ─────────────────────────────────────────────────────

    def historical_date(self) -> date:
        """
        Return the current historical date.

        Use current_date if set by the scenario loader (preferred — correct).
        Fall back to CAMPAIGN_START_DATE for states that haven't been
        initialised with a real date yet.
        """
        return self.current_date if self.current_date is not None else CAMPAIGN_START_DATE

    def historical_date_str(self) -> str:
        return self.historical_date().strftime("%-d %B %Y")

    def advance_date(self, days: int) -> None:
        """Advance current_date by `days` days (called by the engine each OpStage)."""
        self.current_date = self.historical_date() + timedelta(days=days)

    # ── Hex helpers ──────────────────────────────────────────────────────────

    def get_hex(self, hex_id: str) -> Hex:
        """Return the Hex for hex_id, creating a minimal desert stub if unknown."""
        if hex_id not in self.hexes:
            self.hexes[hex_id] = Hex.from_id(hex_id)
        return self.hexes[hex_id]

    def fortification_level(self, hex_id: str) -> int:
        """Current fortification level, checking dynamic overrides first."""
        if hex_id in self.fortifications:
            return self.fortifications[hex_id]
        if hex_id in self.hexes:
            return self.hexes[hex_id].base_fortification_level
        return 0

    # ── Unit helpers ─────────────────────────────────────────────────────────

    def units_in_hex(self, hex_id: str, side: Optional[Side] = None) -> list[Unit]:
        result = [
            u for u in self.units.values()
            if u.hex_id == hex_id and not u.is_eliminated()
        ]
        if side is not None:
            result = [u for u in result if u.side == side]
        return result

    def active_units(self, side: Optional[Side] = None) -> list[Unit]:
        result = [u for u in self.units.values() if u.is_active()]
        if side is not None:
            result = [u for u in result if u.side == side]
        return result

    # ── Supply dump helpers ──────────────────────────────────────────────────

    def dumps_in_hex(self, hex_id: str, side: Optional[str] = None) -> list[SupplyDump]:
        result = [d for d in self.supply_dumps.values() if d.hex_id == hex_id]
        if side:
            result = [d for d in result if d.side == side]
        return result

    # ── Formation helpers ─────────────────────────────────────────────────────

    def formation_cpa(self, unit: Unit) -> int:
        """
        Resolve effective CPA for a unit.
        If the unit has its own cpa > 0, return that.
        Otherwise walk up formation tree until a cpa is found.
        """
        if unit.cpa > 0:
            return unit.effective_cpa()
        if unit.formation_id and unit.formation_id in self.formations:
            formation = self.formations[unit.formation_id]
            if formation.cpa > 0:
                cpa = formation.cpa
                if unit.status.value == "disorganized":
                    cpa = cpa // 2
                return cpa
        return 0  # detached with no CPA assigned — engine will flag this

    # ── Event logging ────────────────────────────────────────────────────────

    def log(self, event: Event) -> None:
        self.events.append(event)

    # ── Narrative helpers ────────────────────────────────────────────────────

    def narrative_summary(self) -> str:
        """
        Compact state summary fed into the Journal Agent's prompt.
        Highlights supply crises and notable positions.
        """
        cw = self.active_units(Side.COMMONWEALTH)
        ax = self.active_units(Side.AXIS)
        cw_oos = [u for u in cw if u.supply_status != SupplyStatus.IN_SUPPLY]
        ax_oos = [u for u in ax if u.supply_status != SupplyStatus.IN_SUPPLY]

        lines = [
            f"Turn {self.turn} / OpStage {self.opstage} — {self.historical_date_str()}",
            f"Weather: {self.weather}   Initiative: {self.initiative}",
            f"Commonwealth: {len(cw)} active units  ({len(cw_oos)} out of supply)",
            f"Axis:         {len(ax)} active units  ({len(ax_oos)} out of supply)",
        ]

        # Highlight critically low fuel dumps
        for dump in self.supply_dumps.values():
            if not dump.is_unlimited and not dump.is_dummy and dump.fuel < 300:
                lines.append(
                    f"  CRITICAL FUEL: {dump.label or dump.id} "
                    f"({dump.hex_id}) — {dump.fuel:.0f} gal remaining"
                )

        # Pasta rule violations this OpStage
        pasta_events = [
            e for e in self.events if e.type == "pasta_rule"
        ]
        if pasta_events:
            lines.append(
                f"  PASTA RULE: {len(pasta_events)} Italian battalion(s) "
                f"short on water this OpStage"
            )

        return "\n".join(lines)

    # ── Fog of war ───────────────────────────────────────────────────────────

    def fog_of_war(self, viewing_side: Side) -> dict:
        """
        Return a state snapshot with enemy units hidden unless adjacent to a
        friendly unit.  Used as the Player Agent's input.
        """
        # Build set of friendly hex_ids for adjacency check
        friendly_hexes: set[str] = {
            u.hex_id for u in self.units.values()
            if u.side == viewing_side and u.hex_id
        }

        # Build set of "visible" enemy hexes (adjacent to any friendly hex)
        visible_enemy_hexes: set[str] = set()
        for h_id in friendly_hexes:
            h = self.get_hex(h_id)
            visible_enemy_hexes.update(h.neighbors())

        visible_units: dict[str, dict] = {}
        for uid, unit in self.units.items():
            if unit.is_eliminated():
                continue
            if unit.side == viewing_side:
                visible_units[uid] = unit.to_dict()
            elif unit.hex_id in visible_enemy_hexes:
                # Enemy unit in contact range — visible but flag it
                visible_units[uid] = {**unit.to_dict(), "_observed": True}
            # Otherwise: enemy unit unknown to this side — omitted

        own_dumps = {
            k: v.to_dict()
            for k, v in self.supply_dumps.items()
            if v.side == viewing_side.value
        }

        return {
            "scenario": self.scenario,
            "turn": self.turn,
            "opstage": self.opstage,
            "current_date": self.historical_date().isoformat(),
            "weather": self.weather,
            "initiative": self.initiative,
            "viewing_side": viewing_side.value,
            "units": visible_units,
            "supply_dumps": own_dumps,
            "events": [e.to_dict() for e in self.events],
        }

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "turn": self.turn,
            "opstage": self.opstage,
            "current_date": self.historical_date().isoformat(),
            "weather": self.weather,
            "initiative": self.initiative,
            "units": {k: v.to_dict() for k, v in self.units.items()},
            "hexes": {k: v.to_dict() for k, v in self.hexes.items()},
            "supply_dumps": {k: v.to_dict() for k, v in self.supply_dumps.items()},
            "formations": {k: v.to_dict() for k, v in self.formations.items()},
            "minefields": {k: v.to_dict() for k, v in self.minefields.items()},
            "fortifications": self.fortifications,
            "events": [e.to_dict() for e in self.events],
        }

    @classmethod
    def from_dict(cls, d: dict) -> GameState:
        raw_date = d.get("date") or d.get("current_date")
        current_date = date.fromisoformat(raw_date) if raw_date else None
        gs = cls(
            scenario=d["scenario"],
            turn=d["turn"],
            opstage=d["opstage"],
            current_date=current_date,
            weather=d.get("weather", "clear"),
            initiative=d.get("initiative", "commonwealth"),
        )
        gs.units = {k: Unit.from_dict(v) for k, v in d.get("units", {}).items()}
        gs.hexes = {k: Hex.from_dict(v) for k, v in d.get("hexes", {}).items()}
        gs.supply_dumps = {
            k: SupplyDump.from_dict(v) for k, v in d.get("supply_dumps", {}).items()
        }
        gs.formations = {
            k: Formation.from_dict(v) for k, v in d.get("formations", {}).items()
        }
        gs.minefields = {
            k: Minefield.from_dict(v) for k, v in d.get("minefields", {}).items()
        }
        gs.fortifications = d.get("fortifications", {})
        gs.events = [Event.from_dict(e) for e in d.get("events", [])]
        return gs
