"""
Pre-campaign board setup validator for The Campaign for North Africa simulation.

GamesmasterAnthony walks around the table before Turn 1, checking that:
  1. The correct CNA map sheet is in use (key locations, terrain, hex count).
  2. Both sides are deployed per the September 1940 scenario setup.
  3. Supply counters are correctly positioned.

Returns a SetupReport; does NOT raise exceptions. Critical violations
halt the simulation before Turn 1 begins.

Scenario reference: "Operation E" — The Italian invasion of Egypt,
September 9, 1940 (CNA Turn 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.counter import Side
from ..models.game_state import GameState


# ── Expected map parameters ────────────────────────────────────────────────────

HEX_COUNT_RANGE = (115, 155)         # Valid total for the CNA North Africa map
EXPECTED_MIN_COL = 4                  # Leftmost column (Morocco area)
EXPECTED_MAX_COL = 22                 # Rightmost column (Suez/Cairo)

# Strategic hexes that MUST exist, with their expected terrain type and name.
# Derived directly from data/map/hexes.json.
REQUIRED_HEX_TERRAIN: dict[str, tuple[str, str]] = {
    "1001": ("port",                "Tripoli"),
    "1201": ("port",                "Benghazi"),
    "1701": ("port",                "Tobruk"),
    "1801": ("port",                "Mersa Matruh"),
    "1902": ("flat_desert_coastal", "El Alamein"),
    "2001": ("city",                "Alexandria"),
    "2201": ("city",                "Cairo"),
    "1910": ("salt_lake",           "Qattara Depression"),
}

# Hex 1910 (Qattara Depression) must be impassable — it anchors the southern flank.
IMPASSABLE_HEX = "1910"

# ── Expected scenario deployment (September 1940) ─────────────────────────────

# Axis turn-1 units must start at or west of the Libya/Egypt frontier.
# Tobruk (col 17) is the easternmost Axis-held port at game start.
AXIS_MAX_START_COL = 17

# Allied turn-1 units start deep in Egypt, well east of Tobruk.
# Mersa Matruh (col 18) is the westernmost Allied position.
ALLIED_MIN_START_COL = 18

# Specific unit IDs that must be present in the unit roster.
REQUIRED_UNIT_IDS: dict[str, str] = {
    "IT-10A-HQ":          "Italian 10th Army HQ",
    "IT-CIRENE-63INF":    "63rd Infantry Division 'Cirene'",
    "IT-MARMARICA-62INF": "62nd Infantry Division 'Marmarica'",
    "GB-WDF-HQ":          "Western Desert Force HQ",
    "GB-7ARM":            "7th Armoured Division HQ",
}

# Required supply depot placements: (depot_id, expected_hex)
REQUIRED_DEPOT_HEXES: list[tuple[str, str, str]] = [
    ("AX-TRIPOLI-BASE", "1001", "Tripoli Base Supply Depot"),
    ("AL-CAIRO-BASE",   "2201", "Cairo Base Supply Depot"),
]

# Minimum Axis-to-Allied active unit ratio at scenario start (historically ~2.5:1).
MIN_AXIS_ALLIED_RATIO = 2.0


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SetupViolation:
    severity: str        # "critical" | "warning" | "info"
    category: str        # "map" | "deployment" | "supply" | "scenario"
    rule_ref: str        # e.g. "§MAP-3" or "§SET-2"
    description: str
    unit_ids: list[str] = field(default_factory=list)
    hex_ids: list[str] = field(default_factory=list)


@dataclass
class SetupReport:
    violations: list[SetupViolation] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)

    @property
    def critical(self) -> list[SetupViolation]:
        return [v for v in self.violations if v.severity == "critical"]

    @property
    def warnings(self) -> list[SetupViolation]:
        return [v for v in self.violations if v.severity == "warning"]

    @property
    def infos(self) -> list[SetupViolation]:
        return [v for v in self.violations if v.severity == "info"]

    @property
    def passed(self) -> bool:
        return len(self.critical) == 0

    @property
    def all_clear(self) -> bool:
        return len(self.violations) == 0

    def summary_text(self) -> str:
        lines = [
            f"Setup Inspection — Scenario: Operation E (Turn 1, September 1940)",
            f"Checks run: {len(self.checks_run)}",
            f"Critical: {len(self.critical)}  "
            f"Warnings: {len(self.warnings)}  "
            f"Informational: {len(self.infos)}",
        ]
        for v in self.violations:
            tag = {"critical": "CRITICAL", "warning": "WARNING", "info": "INFO"}.get(
                v.severity, v.severity.upper()
            )
            lines.append(f"  [{tag}] {v.rule_ref}: {v.description}")
        return "\n".join(lines)


# ── Map identity checks ───────────────────────────────────────────────────────

def _check_hex_count(state: GameState, report: SetupReport) -> None:
    """§MAP-1 — Total hex count must be in the expected range for the CNA map."""
    report.checks_run.append("§MAP-1 Total hex count")
    n = len(list(state.map.all_hexes()))
    lo, hi = HEX_COUNT_RANGE
    if n < lo:
        report.violations.append(SetupViolation(
            severity="critical", category="map", rule_ref="§MAP-1",
            description=f"Map has only {n} hexes; expected {lo}–{hi} for the CNA theatre",
        ))
    elif n > hi:
        report.violations.append(SetupViolation(
            severity="warning", category="map", rule_ref="§MAP-1",
            description=f"Map has {n} hexes, which exceeds expected maximum of {hi}",
        ))
    else:
        report.violations.append(SetupViolation(
            severity="info", category="map", rule_ref="§MAP-1",
            description=f"Map hex count verified: {n} hexes (within expected range {lo}–{hi})",
        ))


def _check_geographic_span(state: GameState, report: SetupReport) -> None:
    """§MAP-2 — Map must span from Morocco (col 04) to the Nile Delta (col 22)."""
    report.checks_run.append("§MAP-2 Geographic span (col 04–22)")
    cols = set()
    for h in state.map.all_hexes():
        try:
            cols.add(int(h.hex_id[:2]))
        except (ValueError, IndexError):
            pass

    if not cols:
        report.violations.append(SetupViolation(
            severity="critical", category="map", rule_ref="§MAP-2",
            description="Cannot determine map column range — hex IDs appear malformed",
        ))
        return

    min_col, max_col = min(cols), max(cols)
    ok = True
    if min_col > EXPECTED_MIN_COL:
        report.violations.append(SetupViolation(
            severity="warning", category="map", rule_ref="§MAP-2",
            description=(
                f"Map does not extend west enough: leftmost column is {min_col:02d}, "
                f"expected {EXPECTED_MIN_COL:02d} (Morocco)"
            ),
        ))
        ok = False
    if max_col < EXPECTED_MAX_COL:
        report.violations.append(SetupViolation(
            severity="warning", category="map", rule_ref="§MAP-2",
            description=(
                f"Map does not extend east enough: rightmost column is {max_col:02d}, "
                f"expected {EXPECTED_MAX_COL:02d} (Cairo/Suez)"
            ),
        ))
        ok = False
    if ok:
        report.violations.append(SetupViolation(
            severity="info", category="map", rule_ref="§MAP-2",
            description=(
                f"Geographic span verified: columns {min_col:02d}–{max_col:02d} "
                f"(Morocco to Nile Delta)"
            ),
        ))


def _check_key_locations(state: GameState, report: SetupReport) -> None:
    """§MAP-3 — Strategic locations (Tripoli, Tobruk, El Alamein, Cairo…) must be on the map."""
    report.checks_run.append("§MAP-3 Key strategic location hexes present")
    missing = []
    wrong_terrain = []
    for hex_id, (expected_terrain, name) in REQUIRED_HEX_TERRAIN.items():
        h = state.map.get(hex_id)
        if not h:
            missing.append(f"{name} ({hex_id})")
        elif h.terrain.value != expected_terrain:
            wrong_terrain.append(
                f"{name} ({hex_id}): expected terrain '{expected_terrain}', "
                f"found '{h.terrain.value}'"
            )
    if missing:
        report.violations.append(SetupViolation(
            severity="critical", category="map", rule_ref="§MAP-3",
            description=f"Missing strategic hexes: {'; '.join(missing)}",
            hex_ids=[hid for hid, _ in REQUIRED_HEX_TERRAIN.items()
                     if not state.map.get(hid)],
        ))
    if wrong_terrain:
        report.violations.append(SetupViolation(
            severity="warning", category="map", rule_ref="§MAP-3",
            description=f"Terrain mismatch at strategic hexes: {'; '.join(wrong_terrain)}",
        ))
    if not missing and not wrong_terrain:
        names = [name for _, (_, name) in REQUIRED_HEX_TERRAIN.items()]
        report.violations.append(SetupViolation(
            severity="info", category="map", rule_ref="§MAP-3",
            description=(
                f"All {len(REQUIRED_HEX_TERRAIN)} key locations verified: "
                f"{', '.join(names)}"
            ),
        ))


def _check_qattara_impassable(state: GameState, report: SetupReport) -> None:
    """§MAP-4 — Qattara Depression (1910) must be impassable with no adjacencies."""
    report.checks_run.append("§MAP-4 Qattara Depression is impassable")
    h = state.map.get(IMPASSABLE_HEX)
    if not h:
        # Already caught by §MAP-3
        return
    if not h.is_impassable():
        report.violations.append(SetupViolation(
            severity="critical", category="map", rule_ref="§MAP-4",
            description=(
                f"Hex {IMPASSABLE_HEX} (Qattara Depression) is not flagged as impassable "
                f"— the southern flank anchor is missing, which will break the scenario"
            ),
            hex_ids=[IMPASSABLE_HEX],
        ))
    elif h.adjacent:
        report.violations.append(SetupViolation(
            severity="warning", category="map", rule_ref="§MAP-4",
            description=(
                f"Qattara Depression ({IMPASSABLE_HEX}) is impassable but has "
                f"{len(h.adjacent)} adjacency entries — units could theoretically be "
                f"ordered into it"
            ),
            hex_ids=[IMPASSABLE_HEX],
        ))
    else:
        report.violations.append(SetupViolation(
            severity="info", category="map", rule_ref="§MAP-4",
            description=(
                "Qattara Depression (1910) confirmed impassable with no adjacencies — "
                "southern flank anchor correctly placed"
            ),
            hex_ids=[IMPASSABLE_HEX],
        ))


# ── Scenario deployment checks ────────────────────────────────────────────────

def _check_required_units(state: GameState, report: SetupReport) -> None:
    """§SET-1 — Key named units must be present in the order of battle."""
    report.checks_run.append("§SET-1 Required units in order of battle")
    missing = []
    for uid, name in REQUIRED_UNIT_IDS.items():
        if uid not in state.ground_units:
            missing.append(f"{name} ({uid})")
    if missing:
        report.violations.append(SetupViolation(
            severity="critical", category="deployment", rule_ref="§SET-1",
            description=f"Missing required units: {'; '.join(missing)}",
            unit_ids=[uid for uid in REQUIRED_UNIT_IDS if uid not in state.ground_units],
        ))
    else:
        report.violations.append(SetupViolation(
            severity="info", category="deployment", rule_ref="§SET-1",
            description=(
                f"All {len(REQUIRED_UNIT_IDS)} required units confirmed in order of battle"
            ),
        ))


def _check_axis_deployment_zone(state: GameState, report: SetupReport) -> None:
    """§SET-2 — Axis turn-1 units must not start east of Tobruk (col 17)."""
    report.checks_run.append(
        f"§SET-2 Axis deployment west of column {AXIS_MAX_START_COL:02d}"
    )
    offenders = []
    for unit in state.ground_units.values():
        if unit.side != Side.AXIS:
            continue
        if unit.available_turn != 1 or not unit.hex_id:
            continue
        try:
            col = int(unit.hex_id[:2])
        except ValueError:
            continue
        if col > AXIS_MAX_START_COL:
            offenders.append(f"{unit.name} at {unit.hex_id} (col {col:02d})")
    if offenders:
        report.violations.append(SetupViolation(
            severity="warning", category="deployment", rule_ref="§SET-2",
            description=(
                f"Axis turn-1 unit(s) deployed east of Tobruk "
                f"(col {AXIS_MAX_START_COL:02d}): {'; '.join(offenders)}"
            ),
        ))
    else:
        report.violations.append(SetupViolation(
            severity="info", category="deployment", rule_ref="§SET-2",
            description=(
                f"Axis deployment verified: all turn-1 units in Libya "
                f"(column ≤ {AXIS_MAX_START_COL:02d})"
            ),
        ))


def _check_allied_deployment_zone(state: GameState, report: SetupReport) -> None:
    """§SET-3 — Allied turn-1 units must start in Egypt (col 18 or beyond)."""
    report.checks_run.append(
        f"§SET-3 Allied deployment east of column {ALLIED_MIN_START_COL:02d}"
    )
    offenders = []
    for unit in state.ground_units.values():
        if unit.side != Side.ALLIED:
            continue
        if unit.available_turn != 1 or not unit.hex_id:
            continue
        try:
            col = int(unit.hex_id[:2])
        except ValueError:
            continue
        if col < ALLIED_MIN_START_COL:
            offenders.append(f"{unit.name} at {unit.hex_id} (col {col:02d})")
    if offenders:
        report.violations.append(SetupViolation(
            severity="warning", category="deployment", rule_ref="§SET-3",
            description=(
                f"Allied turn-1 unit(s) deployed in Libyan territory "
                f"(col < {ALLIED_MIN_START_COL:02d}): {'; '.join(offenders)}"
            ),
        ))
    else:
        report.violations.append(SetupViolation(
            severity="info", category="deployment", rule_ref="§SET-3",
            description=(
                f"Allied deployment verified: all turn-1 units in Egypt "
                f"(column ≥ {ALLIED_MIN_START_COL:02d})"
            ),
        ))


def _check_unit_hex_controller(state: GameState, report: SetupReport) -> None:
    """§SET-4 — Turn-1 units must start in territory controlled by their own side."""
    report.checks_run.append("§SET-4 Units start in friendly-controlled territory")
    offenders = []
    side_ctrl = {Side.AXIS: "axis", Side.ALLIED: "allied"}
    for unit in state.ground_units.values():
        if unit.available_turn != 1 or not unit.hex_id:
            continue
        h = state.map.get(unit.hex_id)
        if not h or not h.initial_controller:
            continue  # Neutral or uncontrolled territory — skip
        expected_ctrl = side_ctrl.get(unit.side)
        if expected_ctrl and h.initial_controller != expected_ctrl:
            offenders.append(
                f"{unit.name} ({unit.side.value}) at {unit.hex_id} "
                f"(initial_controller: {h.initial_controller})"
            )
    if offenders:
        report.violations.append(SetupViolation(
            severity="warning", category="deployment", rule_ref="§SET-4",
            description=(
                f"Unit(s) start in enemy-controlled territory: {'; '.join(offenders[:3])}"
            ),
        ))
    else:
        report.violations.append(SetupViolation(
            severity="info", category="deployment", rule_ref="§SET-4",
            description=(
                "All turn-1 units confirmed in friendly-controlled starting territory"
            ),
        ))


def _check_force_ratio(state: GameState, report: SetupReport) -> None:
    """§SET-5 — Axis should have a historical numerical advantage at game start (~2.5:1)."""
    report.checks_run.append(f"§SET-5 Historical force ratio (Axis ≥ {MIN_AXIS_ALLIED_RATIO:.1f}:1)")
    axis_count = len([
        u for u in state.ground_units.values()
        if u.side == Side.AXIS and u.available_turn == 1
    ])
    allied_count = len([
        u for u in state.ground_units.values()
        if u.side == Side.ALLIED and u.available_turn == 1
    ])
    if allied_count == 0:
        report.violations.append(SetupViolation(
            severity="critical", category="scenario", rule_ref="§SET-5",
            description="No Allied turn-1 units found — deployment incomplete",
        ))
        return
    ratio = axis_count / allied_count
    if ratio < MIN_AXIS_ALLIED_RATIO:
        report.violations.append(SetupViolation(
            severity="warning", category="scenario", rule_ref="§SET-5",
            description=(
                f"Axis:Allied force ratio {ratio:.1f}:1 ({axis_count} vs {allied_count}) "
                f"is below historical minimum of {MIN_AXIS_ALLIED_RATIO:.1f}:1 "
                f"for the September 1940 scenario"
            ),
        ))
    else:
        report.violations.append(SetupViolation(
            severity="info", category="scenario", rule_ref="§SET-5",
            description=(
                f"Force ratio verified: {axis_count} Axis vs {allied_count} Allied "
                f"({ratio:.1f}:1 — historically plausible)"
            ),
        ))


# ── Supply placement checks ───────────────────────────────────────────────────

def _check_required_depots(state: GameState, report: SetupReport) -> None:
    """§SET-6 — Critical supply depots must be at their specified hexes."""
    report.checks_run.append("§SET-6 Critical supply depots correctly positioned")
    for depot_id, expected_hex, name in REQUIRED_DEPOT_HEXES:
        depot = state.supply_counters.get(depot_id)
        if not depot:
            report.violations.append(SetupViolation(
                severity="critical", category="supply", rule_ref="§SET-6",
                description=f"Required supply depot '{name}' ({depot_id}) not found",
            ))
        elif depot.hex_id != expected_hex:
            report.violations.append(SetupViolation(
                severity="critical", category="supply", rule_ref="§SET-6",
                description=(
                    f"'{name}' is at hex {depot.hex_id}, expected {expected_hex}"
                ),
                hex_ids=[depot.hex_id or "?", expected_hex],
            ))
        else:
            report.violations.append(SetupViolation(
                severity="info", category="supply", rule_ref="§SET-6",
                description=f"'{name}' correctly positioned at hex {expected_hex}",
                hex_ids=[expected_hex],
            ))


def _check_depots_on_passable_hexes(state: GameState, report: SetupReport) -> None:
    """§SET-7 — Supply counters must not be placed on impassable hexes."""
    report.checks_run.append("§SET-7 Supply counters on passable hexes")
    offenders = []
    for depot in state.supply_counters.values():
        if not depot.hex_id:
            continue
        h = state.map.get(depot.hex_id)
        if h and h.is_impassable():
            offenders.append(
                f"'{depot.name}' ({depot.id}) at {depot.hex_id} "
                f"({h.location_name or h.terrain.value})"
            )
    if offenders:
        report.violations.append(SetupViolation(
            severity="warning", category="supply", rule_ref="§SET-7",
            description=(
                f"Supply counter(s) placed on impassable hex — resources permanently "
                f"inaccessible: {'; '.join(offenders)}"
            ),
            hex_ids=[
                d.hex_id for d in state.supply_counters.values()
                if d.hex_id and state.map.get(d.hex_id)
                and state.map.get(d.hex_id).is_impassable()
            ],
        ))
    else:
        report.violations.append(SetupViolation(
            severity="info", category="supply", rule_ref="§SET-7",
            description="All supply counters verified on passable hexes",
        ))


def _check_each_side_has_fuel(state: GameState, report: SetupReport) -> None:
    """§SET-8 — Each side must have at least one accessible fuel source at turn 1."""
    report.checks_run.append("§SET-8 Each side has at least one fuel source")
    for side in (Side.AXIS, Side.ALLIED):
        depots = [
            d for d in state.supply_counters.values()
            if d.side == side
            and d.supply_type in ("fuel", "general")
            and d.current_load > 0
            and d.hex_id
            and (not state.map.get(d.hex_id) or not state.map.get(d.hex_id).is_impassable())
        ]
        if not depots:
            report.violations.append(SetupViolation(
                severity="critical", category="supply", rule_ref="§SET-8",
                description=f"{side.value.title()} side has no accessible fuel source at game start",
            ))
        else:
            report.violations.append(SetupViolation(
                severity="info", category="supply", rule_ref="§SET-8",
                description=(
                    f"{side.value.title()} fuel supply verified: "
                    f"{len(depots)} accessible source(s), "
                    f"total {sum(d.current_load for d in depots):.0f} points"
                ),
            ))


# ── Public API ────────────────────────────────────────────────────────────────

def validate_setup(state: GameState) -> SetupReport:
    """
    Run all pre-campaign setup checks against the initial game state.
    Must be called before Turn 1 is processed.
    Returns a SetupReport; does NOT raise exceptions.
    """
    report = SetupReport()

    # Map identity
    _check_hex_count(state, report)
    _check_geographic_span(state, report)
    _check_key_locations(state, report)
    _check_qattara_impassable(state, report)

    # Scenario deployment
    _check_required_units(state, report)
    _check_axis_deployment_zone(state, report)
    _check_allied_deployment_zone(state, report)
    _check_unit_hex_controller(state, report)
    _check_force_ratio(state, report)

    # Supply placement
    _check_required_depots(state, report)
    _check_depots_on_passable_hexes(state, report)
    _check_each_side_has_fuel(state, report)

    return report
