"""
Board State Agent — deterministic Python, no LLM calls.

This is the single source of truth for all game state.  It:
  1. Loads the initial state from a scenario JSON file.
  2. Applies validated actions proposed by the player agents.
  3. Runs end-of-OpStage housekeeping (supply checks, fuel evaporation, pasta rule).
  4. Writes structured output files at the end of each OpStage and game-turn.

No LLM calls are made here.  All decisions are deterministic: the engine either
can or cannot apply an action, and it records why.

Scenario loader
---------------
The scenario JSONs in data/extracted/scenarios/ describe unit placements in
groups (placement_type = "fixed" | "within_n_hexes") rather than as individual
Unit objects.  The loader creates one Unit per named unit in the scenario,
assigning it the center hex for "within_n_hexes" placements and the exact hex
for "fixed" placements.  Precise within-radius placement requires the human
operator (or a future setup assistant) to assign exact hexes; the loader uses
the center hex as a placeholder and sets org_flags to record the radius.

Turn output
-----------
After each OpStage:
  turns/turn_{NNN}_opstage_{M}_state.json    — full GameState snapshot
  turns/turn_{NNN}_opstage_{M}_events.json   — event log for this OpStage

After each game-turn (after OpStage 3):
  turns/turn_{NNN}_state.json                — end-of-turn state (alias)
  turns/turn_{NNN}_events.json               — merged event log (all 3 OpStages)

Actions
-------
Each action is a dict with an "action" key.  Supported actions:

  {"action": "move",       "unit_id": "...", "path": ["A0101", "A0102", ...],
                            "context": "voluntary",
                            "zoc_status": "none"|"contact"|"engaged",
                            "fuel_rate": 4.0}

  {"action": "run_supply_checks"}
    — runs BFS supply check for all active combat units

  {"action": "apply_fuel_evaporation", "hot_weather": false}
    — applies evaporation to all supply dumps (call once per game-turn)

  {"action": "apply_pasta_rule",
   "unit_id": "...", "received_pasta_point": true}

  {"action": "roll_weather"}
    — rolls 2d6 per rule 29.1, sets gs.weather for this OpStage

  {"action": "end_opstage"}
    — writes OpStage output files and resets per-OpStage tracking

  {"action": "end_turn"}
    — writes game-turn output files and advances turn counter

All other actions are rejected with a reason (they must first be validated by
the Rules Arbiter and re-submitted here).
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.engine.hex_map import HexMap
from src.engine.movement import ContactStatus, execute_move
from src.engine.supply import (
    apply_fuel_evaporation,
    apply_pasta_rule,
    apply_prisoner_stores_cost,
    run_supply_checks,
)
from src.models.event import Event
from src.models.game_state import Formation, GameState
from src.models.hex import Hex
from src.models.supply import SupplyDump
from src.models.unit import (
    Nationality,
    Side,
    SupplyStatus,
    Unit,
    UnitSize,
    UnitStatus,
    UnitType,
)


# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT  = Path(__file__).parent.parent.parent
_SCENARIOS  = _REPO_ROOT / "data" / "extracted" / "scenarios"
_TABLES     = _REPO_ROOT / "data" / "extracted" / "rules_tables.json"
_TURNS_DIR  = _REPO_ROOT / "turns"


# ── Nationality / type mapping ─────────────────────────────────────────────────

_NAT_MAP: Dict[str, Nationality] = {
    "british":      Nationality.BRITISH,
    "australian":   Nationality.AUSTRALIAN,
    "new_zealand":  Nationality.NEW_ZEALAND,
    "south_african": Nationality.SOUTH_AFRICAN,
    "indian":       Nationality.INDIAN,
    "polish":       Nationality.POLISH,
    "french":       Nationality.FRENCH,
    "greek":        Nationality.GREEK,
    "american":     Nationality.AMERICAN,
    "german":       Nationality.GERMAN,
    "italian":      Nationality.ITALIAN,
}

_TYPE_MAP: Dict[str, UnitType] = {
    "infantry":     UnitType.INFANTRY,
    "armor":        UnitType.ARMOR,
    "artillery":    UnitType.ARTILLERY,
    "anti_tank":    UnitType.ANTI_TANK,
    "anti_aircraft": UnitType.ANTI_AIRCRAFT,
    "reconnaissance": UnitType.RECONNAISSANCE,
    "recce":        UnitType.RECONNAISSANCE,
    "engineer":     UnitType.ENGINEER,
    "hq":           UnitType.HQ,
    "garrison":     UnitType.GARRISON,
    "supply":       UnitType.SUPPLY,
    "truck":        UnitType.TRUCK,
}

_SIZE_MAP: Dict[str, UnitSize] = {
    "company":   UnitSize.COMPANY,
    "battalion": UnitSize.BATTALION,
    "regiment":  UnitSize.REGIMENT,
    "brigade":   UnitSize.BRIGADE,
    "division":  UnitSize.DIVISION,
    "corps":     UnitSize.CORPS,
    "army":      UnitSize.ARMY,
}


# ── Default CPA by unit type (rule 6.15 approximations) ───────────────────────
#
# CPA values are printed on physical counters and vary per counter.  No machine-
# readable source has been extracted yet, so these defaults are used when the
# scenario JSON omits "cpa".  All values are confirmed in cna_rules.txt:
#
#   Infantry (non-motorized): CPA 10
#     rule 8.17: "Non-motorized units — those units with CPA's of ten or less"
#     rule 7329: "motorized infantry battalion (CPA of 10)" [i.e., walking = 10]
#
#   Infantry (motorized): CPA 20
#     rule 7342–7343: "CPA of 10 if the infantry are walking or a CPA of 20 or
#     25 if the infantry are motorized" — use 20 as the lower motorized bound.
#
#   Armor / Armoured: CPA 30
#     rule 3136: "tank battalion with a CPA of 25"; divisions and corps are
#     generally higher. 30 is a reasonable divisional default.
#
#   Artillery / Anti-Tank / Anti-Aircraft (guns, CPA 0+ class): CPA 10
#     rule 2415: "these units are considered to have a CPA of 10 for combat
#     purposes". rule 10808: "Guns with a CPA of 0+" — 10 used here.
#
#   Reconnaissance: CPA 35
#     rule 10866: "CPA of 35 or more" for motorized reconnaissance.
#
#   HQ / Support: CPA 25
#     Motorized headquarters. No explicit rule citation; approximation.
#     TODO: extract per-counter values from PDF counter sheets.
#
# These are approximations only.  Correct values require counter-level data
# extraction from the PDF.  Track as technical debt.

_DEFAULT_CPA_MOTORIZED: Dict[UnitType, int] = {
    UnitType.INFANTRY:        20,   # rule 7342–7343
    UnitType.ARMOR:           30,   # rule 3136 + divisional scale
    UnitType.ARTILLERY:       10,   # rule 2415
    UnitType.ANTI_TANK:       10,   # rule 2415 / 10808
    UnitType.ANTI_AIRCRAFT:   10,   # rule 2415 / 10808
    UnitType.RECONNAISSANCE:  35,   # rule 10866
    UnitType.ENGINEER:        20,   # motorized engineer; approximation
    UnitType.HQ:              25,   # approximation
    UnitType.GARRISON:        10,   # garrison = non-mobile; approximation
    UnitType.SUPPLY:          25,   # supply unit with trucks; approximation
    UnitType.TRUCK:           25,   # rule 16848: trucks use CPA 20 or 25
}

_DEFAULT_CPA_NON_MOTORIZED: Dict[UnitType, int] = {
    UnitType.INFANTRY:        10,   # rule 8.17, rule 7329
    UnitType.ARMOR:           10,   # immobile armor; rare but possible
    UnitType.ARTILLERY:       10,   # rule 2415
    UnitType.ANTI_TANK:       10,   # rule 2415 / 10808
    UnitType.ANTI_AIRCRAFT:   10,   # rule 2415 / 10808
    UnitType.RECONNAISSANCE:  10,   # non-motorized recce; approximation
    UnitType.ENGINEER:        10,   # walking engineers; approximation
    UnitType.HQ:              10,   # foot HQ; approximation
    UnitType.GARRISON:        10,   # rule 8.17
    UnitType.SUPPLY:          10,   # foot supply unit; approximation
    UnitType.TRUCK:           25,   # trucks are always motorized
}


def _default_cpa(unit_type: UnitType, motorized: bool) -> int:
    """
    Look up the default CPA for a unit type when no counter-specific value
    is available.  Returns a rule-backed approximation.
    See _DEFAULT_CPA_MOTORIZED / _DEFAULT_CPA_NON_MOTORIZED for citations.
    """
    table = _DEFAULT_CPA_MOTORIZED if motorized else _DEFAULT_CPA_NON_MOTORIZED
    return table.get(unit_type, 10)  # fallback: treat as non-motorized infantry


# ── Season helper (rule 29.1) ─────────────────────────────────────────────────

def _season_from_date(d: Optional[date]) -> str:
    """
    Return the current season name from a game date (rule 29.1).

    Rule 29.1 defines seasons by week:
      Spring   March III – June I
      Summer   June III  – September II
      Fall     September III – December II
      Winter   December III  – March II

    We approximate to month level (week-exact would need turn number).
    This is accurate for full months but may be off by ~1 week at season
    boundaries (e.g. early March = Winter, but this returns "spring").
    Approximation noted in rules_tables.json weather_system entry.
    """
    if d is None:
        return "fall"  # Crusader scenario default: November 1941 = Fall
    m = d.month
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    if m in (9, 10, 11):
        return "fall"
    return "winter"  # December, January, February


# ── Action result ──────────────────────────────────────────────────────────────

@dataclass
class ActionResult:
    """Outcome of applying one action to the board state."""
    action: str
    success: bool
    reason: Optional[str] = None
    events: List[Event] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)


# ── Scenario loader ────────────────────────────────────────────────────────────

def _unit_id(name: str, nationality: str, size: str, index: int) -> str:
    """Generate a stable unit ID from counter name and index."""
    slug = name.lower().replace(" ", "-").replace("/", "-").replace("'", "")
    return f"{nationality[:2].upper()}-{slug}-{index:03d}"


def _load_unit(raw: dict, side: Side, index: int) -> Unit:
    """Build a Unit dataclass from a scenario unit dict."""
    name = raw.get("name", f"Unknown-{index}")
    nat_str   = raw.get("nationality", "british" if side == Side.COMMONWEALTH else "german")
    nat       = _NAT_MAP.get(nat_str, Nationality.BRITISH)
    unit_type = _TYPE_MAP.get(raw.get("type", "infantry"), UnitType.INFANTRY)
    size      = _SIZE_MAP.get(raw.get("size", "battalion"), UnitSize.BATTALION)
    motorized = raw.get("motorized", unit_type in (UnitType.ARMOR, UnitType.RECONNAISSANCE))
    # Use counter-specific CPA if provided; otherwise apply type-based default.
    # The defaults are rule-backed approximations (see _DEFAULT_CPA_MOTORIZED).
    # TODO: replace with per-counter values extracted from PDF counter sheets.
    cpa       = raw.get("cpa") or _default_cpa(unit_type, motorized)
    steps     = raw.get("steps", 2)
    pasta     = raw.get("pasta_rule", False)
    org_flags = raw.get("org_flags", "")
    uid       = raw.get("id") or _unit_id(name, nat_str, size.value, index)

    return Unit(
        id=uid,
        name=name,
        nationality=nat,
        side=side,
        type=unit_type,
        size=size,
        motorized=motorized,
        cpa=cpa,
        cp_remaining=float(cpa),
        steps_current=steps,
        steps_max=steps,
        pasta_rule=pasta,
        org_flags=org_flags,
    )


def _load_supply_dump(raw: dict, side: Side, index: int) -> SupplyDump:
    """Build a SupplyDump from a scenario supply dump dict."""
    dump_id = raw.get("id") or f"{side.value}-dump-{index:03d}"
    unlimited = raw.get("ammo_unlimited") or raw.get("fuel_unlimited") or raw.get("stores_unlimited")
    return SupplyDump(
        id=dump_id,
        hex_id=raw.get("location", raw.get("hex", "")),
        side=side.value,
        label=raw.get("name"),
        ammo=float(raw.get("ammo") or 0.0),
        fuel=float(raw.get("fuel") or 0.0),
        stores=float(raw.get("stores") or 0.0),
        water=float(raw.get("water") or 0.0),
        is_unlimited=bool(unlimited),
    )


def load_scenario(scenario_name: str) -> GameState:
    """
    Build a GameState from a scenario JSON file.

    scenario_name: e.g. "crusader", "italian_campaign"
    """
    path = _SCENARIOS / f"{scenario_name}.json"
    with open(path) as f:
        raw = json.load(f)

    sc = raw.get("scenario", {})
    start = sc.get("start", {})
    turn    = start.get("game_turn", 1)
    opstage = start.get("opstage", 1)
    raw_date = start.get("historical_date")
    current_date = date.fromisoformat(raw_date) if raw_date else None

    gs = GameState(
        scenario=scenario_name,
        turn=turn,
        opstage=opstage,
        current_date=current_date,
        weather=raw.get("weather", "clear"),
        initiative=raw.get("initiative", "commonwealth"),
    )

    unit_index = 0

    for side_key, side in (("commonwealth", Side.COMMONWEALTH), ("axis", Side.AXIS)):
        side_data = raw.get(side_key, {})

        # ── Land units ────────────────────────────────────────────────────────
        for placement_group in side_data.get("land_units", []):
            placement_type = placement_group.get("placement_type", "fixed")
            if placement_type == "fixed":
                hex_id = placement_group.get("hex") or placement_group.get("placement_center")
            else:
                # within_n_hexes: use center as placeholder hex
                hex_id = placement_group.get("placement_center", "")

            radius = placement_group.get("placement_radius", 0)
            location_label = placement_group.get("location_label", "")

            for unit_raw in placement_group.get("units", []):
                unit = _load_unit(unit_raw, side, unit_index)
                unit.hex_id = hex_id
                if radius > 0:
                    # Annotate that exact hex is TBD within this radius
                    unit.org_flags = (
                        f"[SETUP: within {radius} hex(es) of {hex_id} — {location_label}] "
                        + unit.org_flags
                    )
                gs.units[unit.id] = unit
                unit_index += 1

        # ── Supply dumps ──────────────────────────────────────────────────────
        dumps_data = side_data.get("supply_dumps", {})
        dump_lists = (
            list(dumps_data.values()) if isinstance(dumps_data, dict) else [dumps_data]
        )
        dump_index = 0
        for dump_list in dump_lists:
            if not isinstance(dump_list, list):
                continue  # skip non-list values (notes, counts, nested dicts)
            for dump_raw in dump_list:
                if not isinstance(dump_raw, dict):
                    continue  # skip non-dict entries within a list
                dump = _load_supply_dump(dump_raw, side, dump_index)
                gs.supply_dumps[dump.id] = dump
                dump_index += 1

    # Recompute formation CPAs upward from children (rule 6.15).
    # Must run after all units are loaded.
    gs.recompute_formation_cpas()

    return gs


# ── Turn output ────────────────────────────────────────────────────────────────

def _ensure_turns_dir() -> None:
    _TURNS_DIR.mkdir(parents=True, exist_ok=True)


def write_opstage_output(
    gs: GameState,
    events: List[Event],
    opstage_override: Optional[int] = None,
    _turns_dir: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """
    Write OpStage output files.

    Returns (state_path, events_path).
    """
    turns_dir = _turns_dir if _turns_dir is not None else _TURNS_DIR
    turns_dir.mkdir(parents=True, exist_ok=True)
    t = gs.turn
    o = opstage_override if opstage_override is not None else gs.opstage
    state_path  = turns_dir / f"turn_{t:03d}_opstage_{o}_state.json"
    events_path = turns_dir / f"turn_{t:03d}_opstage_{o}_events.json"

    with open(state_path, "w") as f:
        json.dump(gs.to_dict(), f, indent=2)

    with open(events_path, "w") as f:
        json.dump([e.to_dict() for e in events], f, indent=2)

    return state_path, events_path


def write_turn_output(
    gs: GameState,
    all_events: List[Event],
    _turns_dir: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """
    Write end-of-turn output files (alias pointing to end-of-OpStage-3 state).

    Returns (state_path, events_path).
    """
    turns_dir = _turns_dir if _turns_dir is not None else _TURNS_DIR
    turns_dir.mkdir(parents=True, exist_ok=True)
    t = gs.turn
    state_path  = turns_dir / f"turn_{t:03d}_state.json"
    events_path = turns_dir / f"turn_{t:03d}_events.json"

    with open(state_path, "w") as f:
        json.dump(gs.to_dict(), f, indent=2)

    with open(events_path, "w") as f:
        json.dump([e.to_dict() for e in all_events], f, indent=2)

    return state_path, events_path


# ── Board State Agent ──────────────────────────────────────────────────────────

class BoardStateAgent:
    """
    Deterministic game-state manager.  No LLM calls.

    Usage
    -----
        agent = BoardStateAgent.from_scenario("crusader")
        result = agent.apply_action({"action": "run_supply_checks"})
        result = agent.apply_action({
            "action": "move",
            "unit_id": "BR-70th-inf-div-001",
            "path": ["C4807", "C4808"],
            "context": "voluntary",
        })
        state_path, events_path = agent.end_opstage()
    """

    def __init__(self, game_state: GameState) -> None:
        self.gs = game_state
        self._hex_map: Optional[HexMap] = None
        self._rules_tables: Optional[Dict[str, Any]] = None
        self._opstage_events: List[Event] = []
        self._turn_events: List[Event] = []
        # BD check tracking: {unit_id → had_check_this_opstage}
        self._bd_checked: Dict[str, bool] = {}

    @classmethod
    def from_scenario(cls, scenario_name: str) -> "BoardStateAgent":
        """Load initial state from a scenario JSON and return a new agent."""
        gs = load_scenario(scenario_name)
        return cls(gs)

    @classmethod
    def from_state_file(cls, state_path: str) -> "BoardStateAgent":
        """Resume from a saved state JSON (e.g. from a previous turn)."""
        with open(state_path) as f:
            d = json.load(f)
        gs = GameState.from_dict(d)
        return cls(gs)

    # ── Tables + HexMap ────────────────────────────────────────────────────────

    @property
    def tables(self) -> Dict[str, Any]:
        """Lazily load and cache rules_tables.json."""
        if self._rules_tables is None:
            with open(_TABLES) as f:
                self._rules_tables = json.load(f)
        return self._rules_tables

    @property
    def hex_map(self) -> HexMap:
        if self._hex_map is None:
            tec = self.tables["terrain_effects_chart"]["terrain_types"]
            self._hex_map = HexMap(self.gs.hexes, tec)
        return self._hex_map

    # ── Action dispatch ────────────────────────────────────────────────────────

    def apply_action(self, action: Dict[str, Any]) -> ActionResult:
        """
        Apply one validated action to the board state.

        The action must already be validated by the Rules Arbiter.
        This method applies the physical state change and records events.
        """
        action_type = action.get("action", "")

        if action_type == "move":
            return self._action_move(action)
        if action_type == "roll_weather":
            return self._action_roll_weather()
        if action_type == "run_supply_checks":
            return self._action_supply_checks()
        if action_type == "apply_fuel_evaporation":
            return self._action_fuel_evaporation(action)
        if action_type == "apply_pasta_rule":
            return self._action_pasta_rule(action)
        if action_type == "apply_prisoner_stores":
            return self._action_prisoner_stores(action)
        if action_type == "end_opstage":
            return self._action_end_opstage()
        if action_type == "end_turn":
            return self._action_end_turn()

        return ActionResult(
            action=action_type,
            success=False,
            reason=f"Unknown action '{action_type}'",
        )

    # ── Move action ────────────────────────────────────────────────────────────

    def _action_move(self, action: Dict[str, Any]) -> ActionResult:
        unit_id = action.get("unit_id", "")
        unit = self.gs.units.get(unit_id)
        if unit is None:
            return ActionResult("move", False, f"Unit '{unit_id}' not found")
        if not unit.is_active():
            return ActionResult("move", False, f"Unit '{unit_id}' is not active")

        path = action.get("path", [])
        if len(path) < 2:
            return ActionResult("move", False, "path must have at least 2 hexes")

        context = action.get("context", "voluntary")
        zoc_str = action.get("zoc_status", "none")
        zoc_status = {
            "none":    ContactStatus.NONE,
            "contact": ContactStatus.CONTACT,
            "engaged": ContactStatus.ENGAGED,
        }.get(zoc_str, ContactStatus.NONE)
        fuel_rate = float(action.get("fuel_rate", 0.0))
        had_bd    = self._bd_checked.get(unit_id, False)

        result = execute_move(
            unit, path, self.gs, self.hex_map,
            context=context,
            zoc_contact_status=zoc_status,
            had_previous_bd_check=had_bd,
            fuel_rate=fuel_rate,
        )

        # Update BD check tracker
        if result.breakdown_check_needed:
            self._bd_checked[unit_id] = True

        # Accumulate events
        self._opstage_events.extend(result.events)

        return ActionResult(
            action="move",
            success=result.stopped_reason is None or result.path_taken[-1] != path[0],
            reason=result.stopped_reason,
            events=result.events,
            data={
                "path_taken":     result.path_taken,
                "path_intended":  result.path_intended,
                "cp_spent":       result.cp_spent_total,
                "dp_earned":      result.dp_earned,
                "fuel_consumed":  result.fuel_consumed,
                "bd_after":       result.bd_after,
                "bd_check_needed": result.breakdown_check_needed,
            },
        )

    # ── Supply checks action ───────────────────────────────────────────────────

    def _action_supply_checks(self) -> ActionResult:
        events = run_supply_checks(self.gs, self.hex_map)
        self._opstage_events.extend(events)
        return ActionResult(
            action="run_supply_checks",
            success=True,
            events=events,
            data={"checks_run": len([u for u in self.gs.units.values() if u.is_active()])},
        )

    # ── Fuel evaporation action ────────────────────────────────────────────────

    def _action_fuel_evaporation(self, action: Dict[str, Any]) -> ActionResult:
        # rule 29.3: hot weather triggers the +5% fuel/water evaporation bonus (rule 49.3).
        # Use explicit flag if provided; otherwise derive from gs.weather set by roll_weather.
        hot_weather = bool(action.get("hot_weather", self.gs.weather == "hot"))
        events = apply_fuel_evaporation(self.gs, hot_weather=hot_weather)
        self._opstage_events.extend(events)
        return ActionResult(
            action="apply_fuel_evaporation",
            success=True,
            events=events,
            data={"hot_weather": hot_weather, "dumps_affected": len(events)},
        )

    # ── Pasta rule action ──────────────────────────────────────────────────────

    def _action_pasta_rule(self, action: Dict[str, Any]) -> ActionResult:
        unit_id = action.get("unit_id", "")
        unit = self.gs.units.get(unit_id)
        if unit is None:
            return ActionResult("apply_pasta_rule", False, f"Unit '{unit_id}' not found")

        received = bool(action.get("received_pasta_point", False))
        event = apply_pasta_rule(unit, received, self.gs)
        events = [event] if event else []
        self._opstage_events.extend(events)
        return ActionResult(
            action="apply_pasta_rule",
            success=True,
            events=events,
            data={"unit_id": unit_id, "received": received},
        )

    # ── Prisoner stores action ─────────────────────────────────────────────────

    def _action_prisoner_stores(self, action: Dict[str, Any]) -> ActionResult:
        prisoner_points = action.get("prisoner_points_by_hex", {})
        events = apply_prisoner_stores_cost(prisoner_points, self.gs)
        self._opstage_events.extend(events)
        return ActionResult(
            action="apply_prisoner_stores",
            success=True,
            events=events,
            data={"hexes": len(prisoner_points)},
        )

    # ── Weather roll ───────────────────────────────────────────────────────────

    def _action_roll_weather(self) -> ActionResult:
        """
        Roll for weather at the start of each OpStage (rule 29.1).

        Rolls 2d6 sequentially (d1*10 + d2), looks up outcome in the season
        row of the weather table (rules_tables.json weather_system.weather_table),
        and sets gs.weather to "normal" | "hot" | "sandstorm" | "rainstorm".

        NOTE: The actual Weather Table (rule 29.6) is not in the OCR text
        (the booklet says "see Charts and Tables"). The probabilities in
        rules_tables.json are approximations; see the _note field there.
        Only confirmed data point: rule 29.1 example "53 during summer = Hot Weather".
        """
        d1 = random.randint(1, 6)
        d2 = random.randint(1, 6)
        outcome = d1 * 10 + d2

        season = _season_from_date(self.gs.current_date)
        season_row = (
            self.tables
            .get("weather_system", {})
            .get("weather_table", {})
            .get(season, {})
        )

        weather = "normal"  # rule 29.2: normal weather is the default
        for weather_type in ("hot", "sandstorm", "rainstorm"):
            if outcome in season_row.get(weather_type, []):
                weather = weather_type
                break

        old_weather = self.gs.weather
        self.gs.weather = weather

        event = Event(
            type="weather_roll",
            turn=self.gs.turn,
            opstage=self.gs.opstage,
            description=f"Weather roll {d1}{d2} ({season}): {weather}",
            data={
                "d1": d1,
                "d2": d2,
                "outcome": outcome,
                "season": season,
                "weather": weather,
                "previous_weather": old_weather,
            },
        )
        self._opstage_events.append(event)

        return ActionResult(
            action="roll_weather",
            success=True,
            events=[event],
            data={"weather": weather, "d1": d1, "d2": d2, "season": season},
        )

    # ── Action context builder (for Rules Arbiter) ─────────────────────────────

    def build_action_context(
        self,
        action: Dict[str, Any],
        context_type: str = "voluntary",
    ) -> Dict[str, Any]:
        """
        Pre-compute the context dict the Rules Arbiter expects (ARCHITECTURE.md).

        The arbiter must never calculate context itself; all engine values
        (CP costs, ZOC status, stacking counts, supply status) are injected
        here.  Returns the context dict ready to pass to validate_action().

        Supported action types: "move", "combat".
        Returns {} for unknown action types.
        """
        action_type = action.get("action", "")
        if action_type == "move":
            return self._build_move_context(action, context_type)
        if action_type == "combat":
            return self._build_combat_context(action)
        return {}

    def _unit_snapshot(self, unit: Unit, zoc_set: Optional[set] = None) -> Dict[str, Any]:
        """
        Compact unit snapshot matching the Rules Arbiter context spec.
        zoc_set: set of hex_ids under enemy ZOC (to populate zoc_status field).
        """
        zoc_status = "none"
        if zoc_set and unit.hex_id and unit.hex_id in zoc_set:
            zoc_status = "contact"
        return {
            "id": unit.id,
            "name": unit.name,
            "cpa": unit.cpa,
            "cp_remaining": unit.cp_remaining,
            "side": unit.side.value,
            "hex_id": unit.hex_id,
            "supply_status": unit.supply_status.value,
            "breakdown_points": unit.breakdown_points,
            "is_motorized": unit.motorized,
            "zoc_status": zoc_status,
        }

    def _build_move_context(
        self, action: Dict[str, Any], context_type: str
    ) -> Dict[str, Any]:
        """
        Build the 'move' context dict for the Rules Arbiter.

        Computes: unit snapshot, path_hex_costs, total_cp_cost, zoc_hexes,
        enemy_occupied_hexes, stacking_in_destination, stacking_limit, weather.
        """
        unit_id = action.get("unit_id", "")
        unit = self.gs.units.get(unit_id)
        if unit is None:
            return {}
        path = action.get("path", [])
        if len(path) < 2:
            return {}

        enemy_side = Side.AXIS if unit.side == Side.COMMONWEALTH else Side.COMMONWEALTH
        enemy_units = [u for u in self.gs.units.values() if u.side == enemy_side and u.is_active()]

        # ZOC hexes under enemy control
        zoc_set = self.hex_map.zoc_hexes(enemy_side, enemy_units)

        # CP cost for each hex entered along the path (excluding origin)
        path_hex_costs: Dict[str, float] = {}
        total_cp_cost = 0.0
        for i in range(1, len(path)):
            cost = self.hex_map.entry_cost(unit, path[i - 1], path[i], self.gs.weather)
            numeric_cost = 999.0 if cost == "P" else float(cost)
            path_hex_costs[path[i]] = numeric_cost
            total_cp_cost += numeric_cost

        # ZOC exit cost: leaving a Contact hex costs 2 CP, Engaged costs 4 CP
        # (rule 8.15.2/8.15.3, from rules_tables.json zoc_rules.exit_cost).
        # We add it if the unit starts in enemy ZOC and action context reflects it.
        # The arbiter receives zoc_status from the action and checks independently.

        # Enemy occupied hexes
        enemy_occupied = sorted({u.hex_id for u in enemy_units if u.hex_id})

        # Stacking at destination (friendly units already there, excluding mover)
        dest_hex_id = path[-1]
        friendly_in_dest = [
            u for u in self.gs.units.values()
            if u.hex_id == dest_hex_id
            and u.is_active()
            and u.id != unit_id
            and u.side == unit.side
        ]
        stacking_sp = sum(self.hex_map.unit_stacking_points(u) for u in friendly_in_dest)

        # Stacking limit from TEC (rule 9.4: generally 6 SP; see rules_tables.json)
        dest_hex_obj = self.gs.hexes.get(dest_hex_id)
        terrain_key = dest_hex_obj.terrain.value if dest_hex_obj else "Clear"
        tec = self.tables.get("terrain_effects_chart", {}).get("terrain_types", {})
        stacking_limit = tec.get(terrain_key, {}).get("stack") or 6  # rule 9.4 default

        return {
            "unit": self._unit_snapshot(unit, zoc_set),
            "path": list(path),
            "path_hex_costs": path_hex_costs,
            "total_cp_cost": total_cp_cost,
            "zoc_hexes": sorted(zoc_set),
            "enemy_occupied_hexes": enemy_occupied,
            "stacking_in_destination": stacking_sp,
            "stacking_limit": stacking_limit,
            "weather": self.gs.weather,
            "context": context_type,
        }

    def _build_combat_context(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build the 'combat' context dict for the Rules Arbiter.

        Computes: attacker/defender snapshots, adjacency, terrain, supply status.
        """
        attacker_id = action.get("attacker_id", "")
        defender_id = action.get("defender_id", "")
        attacker = self.gs.units.get(attacker_id)
        defender = self.gs.units.get(defender_id)
        if attacker is None or defender is None:
            return {}

        combat_type = action.get("combat_type", "close_assault")
        attacker_cp_cost = float(action.get("attacker_cp_cost", 10))

        # Adjacency: attacker and defender must be in neighboring hexes
        adjacent = (
            self.hex_map.direction_to(attacker.hex_id or "", defender.hex_id or "") is not None
        )

        # Terrain at defender hex (lowercase for arbiter prompt readability)
        def_hex = self.gs.hexes.get(defender.hex_id or "")
        terrain = def_hex.terrain.value.lower() if def_hex else "clear"

        enemy_side = Side.AXIS if attacker.side == Side.COMMONWEALTH else Side.COMMONWEALTH
        enemy_units = [u for u in self.gs.units.values() if u.side == enemy_side and u.is_active()]
        zoc_set = self.hex_map.zoc_hexes(enemy_side, enemy_units)

        return {
            "attacker": self._unit_snapshot(attacker, zoc_set),
            "defender": {
                "id": defender.id,
                "hex_id": defender.hex_id,
                "supply_status": defender.supply_status.value,
            },
            "combat_type": combat_type,
            "attacker_cp_cost": attacker_cp_cost,
            "attacker_cp_remaining": attacker.cp_remaining,
            "adjacent": adjacent,
            "terrain": terrain,
            "defender_in_supply": defender.supply_status == SupplyStatus.IN_SUPPLY,
            "weather": self.gs.weather,
        }

    # ── End OpStage ────────────────────────────────────────────────────────────

    def _action_end_opstage(self) -> ActionResult:
        """Write OpStage output files, reset per-OpStage tracking."""
        state_path, events_path = write_opstage_output(
            self.gs, self._opstage_events,
        )
        self._turn_events.extend(self._opstage_events)

        summary = {
            "turn":         self.gs.turn,
            "opstage":      self.gs.opstage,
            "events_count": len(self._opstage_events),
            "state_file":   str(state_path),
            "events_file":  str(events_path),
        }

        # Reset per-OpStage tracking
        self._opstage_events = []
        self._bd_checked = {}

        # Reset unit CP remaining for next OpStage (rule 6.16)
        for unit in self.gs.units.values():
            cpa = self.gs.formation_cpa(unit)
            unit.cp_remaining = float(cpa) if cpa > 0 else 0.0
            unit.breakdown_points = 0.0  # BD resets each OpStage

        # Advance opstage
        if self.gs.opstage < 3:
            self.gs.opstage += 1
        # If this was opstage 3, end_turn should be called next

        return ActionResult(
            action="end_opstage",
            success=True,
            data=summary,
        )

    # ── End Turn ──────────────────────────────────────────────────────────────

    def _action_end_turn(self) -> ActionResult:
        """Write end-of-turn output files and advance the turn counter."""
        state_path, events_path = write_turn_output(self.gs, self._turn_events)

        summary = {
            "turn":         self.gs.turn,
            "events_count": len(self._turn_events),
            "state_file":   str(state_path),
            "events_file":  str(events_path),
        }

        self._turn_events = []

        # Advance turn and reset to OpStage 1
        self.gs.turn += 1
        self.gs.opstage = 1

        return ActionResult(
            action="end_turn",
            success=True,
            data=summary,
        )

    # ── Narrative helper ───────────────────────────────────────────────────────

    def narrative_summary(self) -> str:
        """Compact state summary for the Journal Agent's prompt."""
        return self.gs.narrative_summary()

    def fog_of_war(self, side: Side) -> dict:
        """Fog-of-war snapshot for a player agent."""
        return self.gs.fog_of_war(side)
