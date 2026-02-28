"""
Post-turn rules validator for The Campaign for North Africa simulation.

Runs a battery of deterministic consistency checks after each turn completes.
Returns a ValidationReport that distinguishes critical violations (which halt
the simulation) from warnings (which are passed to GamesmasterAnthony for
commentary).

Rule references use the CNA rulebook section format: §XX.X
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.counter import UnitStatus, Side
from ..models.game_state import GameState
from ..models.hex_map import Terrain


@dataclass
class Violation:
    severity: str          # "critical" | "warning"
    rule_ref: str          # e.g. "§14.3"
    description: str
    unit_ids: list[str] = field(default_factory=list)
    hex_ids: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    turn: int
    violations: list[Violation] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)

    @property
    def critical(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "critical"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]

    @property
    def passed(self) -> bool:
        return len(self.critical) == 0

    def summary_text(self) -> str:
        lines = [f"Validation — Turn {self.turn}",
                 f"Checks run: {len(self.checks_run)}",
                 f"Critical violations: {len(self.critical)}",
                 f"Warnings: {len(self.warnings)}"]
        for v in self.violations:
            tag = "CRITICAL" if v.severity == "critical" else "WARNING"
            lines.append(f"  [{tag}] {v.rule_ref}: {v.description}")
        return "\n".join(lines)


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_impassable_hexes(state: GameState, report: ValidationReport) -> None:
    """§6.2 — No land unit may end its movement in an impassable hex."""
    report.checks_run.append("§6.2 Impassable hex occupation")
    for unit in state.ground_units.values():
        if unit.status == UnitStatus.ELIMINATED or not unit.hex_id:
            continue
        if unit.available_turn > state.turn:
            continue
        h = state.map.get(unit.hex_id)
        if h and h.is_impassable():
            report.violations.append(Violation(
                severity="critical",
                rule_ref="§6.2",
                description=(
                    f"{unit.name} is in impassable hex {unit.hex_id} "
                    f"(terrain: {h.terrain.value})"
                ),
                unit_ids=[unit.id],
                hex_ids=[unit.hex_id],
            ))


def _check_step_bounds(state: GameState, report: ValidationReport) -> None:
    """§8.1 — Unit steps must be in range [0, max_steps]."""
    report.checks_run.append("§8.1 Step count bounds")
    for unit in state.ground_units.values():
        if unit.steps < 0:
            report.violations.append(Violation(
                severity="critical",
                rule_ref="§8.1",
                description=f"{unit.name} has negative steps ({unit.steps})",
                unit_ids=[unit.id],
            ))
        elif unit.steps > unit.max_steps:
            report.violations.append(Violation(
                severity="critical",
                rule_ref="§8.1",
                description=(
                    f"{unit.name} has {unit.steps} steps but max is {unit.max_steps}"
                ),
                unit_ids=[unit.id],
            ))


def _check_fuel_bounds(state: GameState, report: ValidationReport) -> None:
    """§13.1 — Fuel may not fall below 0 or exceed fuel_capacity."""
    report.checks_run.append("§13.1 Fuel level bounds")
    for unit in state.ground_units.values():
        if unit.status == UnitStatus.ELIMINATED:
            continue
        if unit.supply.fuel < -0.1:
            report.violations.append(Violation(
                severity="critical",
                rule_ref="§13.1",
                description=(
                    f"{unit.name} has negative fuel ({unit.supply.fuel:.2f})"
                ),
                unit_ids=[unit.id],
            ))
        elif unit.supply.fuel > unit.fuel_capacity + 0.1:
            report.violations.append(Violation(
                severity="warning",
                rule_ref="§13.1",
                description=(
                    f"{unit.name} fuel {unit.supply.fuel:.1f} exceeds "
                    f"capacity {unit.fuel_capacity:.1f}"
                ),
                unit_ids=[unit.id],
            ))


def _check_water_bounds(state: GameState, report: ValidationReport) -> None:
    """§13.2 — Water supply may not fall below 0."""
    report.checks_run.append("§13.2 Water level bounds")
    for unit in state.ground_units.values():
        if unit.status == UnitStatus.ELIMINATED:
            continue
        if unit.supply.water < -0.1:
            report.violations.append(Violation(
                severity="critical",
                rule_ref="§13.2",
                description=(
                    f"{unit.name} has negative water ({unit.supply.water:.2f})"
                ),
                unit_ids=[unit.id],
            ))


def _check_active_units_placed(state: GameState, report: ValidationReport) -> None:
    """§4.2 — Every active unit must occupy a hex on the map."""
    report.checks_run.append("§4.2 Active units have a map position")
    for side in (Side.AXIS, Side.ALLIED):
        for unit in state.active_units_for_side(side):
            if not unit.hex_id:
                report.violations.append(Violation(
                    severity="critical",
                    rule_ref="§4.2",
                    description=(
                        f"{unit.name} is active (turn {state.turn} ≥ "
                        f"available turn {unit.available_turn}) but has no hex position"
                    ),
                    unit_ids=[unit.id],
                ))
            elif not state.map.get(unit.hex_id):
                report.violations.append(Violation(
                    severity="critical",
                    rule_ref="§4.2",
                    description=(
                        f"{unit.name} is at hex {unit.hex_id} which does not "
                        f"exist on the map"
                    ),
                    unit_ids=[unit.id],
                    hex_ids=[unit.hex_id],
                ))


def _check_zero_step_units_eliminated(state: GameState, report: ValidationReport) -> None:
    """§8.4 — A unit reduced to 0 steps must be marked Eliminated."""
    report.checks_run.append("§8.4 Zero-step units marked eliminated")
    for unit in state.ground_units.values():
        if unit.steps == 0 and unit.status != UnitStatus.ELIMINATED:
            report.violations.append(Violation(
                severity="critical",
                rule_ref="§8.4",
                description=(
                    f"{unit.name} has 0 steps but status is {unit.status.value}"
                ),
                unit_ids=[unit.id],
            ))


def _check_disorganized_cohesion(state: GameState, report: ValidationReport) -> None:
    """§15.2 — A Disorganized unit must have cohesion ≤ -10."""
    report.checks_run.append("§15.2 Disorganized unit cohesion threshold")
    for unit in state.ground_units.values():
        if unit.status != UnitStatus.DISORGANIZED:
            continue
        if unit.cohesion > -8:    # Allow 2pt tolerance for rally/event ordering
            report.violations.append(Violation(
                severity="warning",
                rule_ref="§15.2",
                description=(
                    f"{unit.name} is Disorganized but cohesion is {unit.cohesion} "
                    f"(expected ≤ -10)"
                ),
                unit_ids=[unit.id],
            ))


def _check_hex_control_consistency(state: GameState, report: ValidationReport) -> None:
    """§9.1 — A hex cannot be claimed by a side with no units present if the
    opposing side has units there."""
    report.checks_run.append("§9.1 Hex control vs unit presence")
    axis_hexes: set[str] = set()
    allied_hexes: set[str] = set()

    for unit in state.active_units_for_side(Side.AXIS):
        if unit.hex_id:
            axis_hexes.add(unit.hex_id)
    for unit in state.active_units_for_side(Side.ALLIED):
        if unit.hex_id:
            allied_hexes.add(unit.hex_id)

    for hex_id, ctrl in state.hex_control.items():
        if ctrl == "axis" and hex_id in allied_hexes and hex_id not in axis_hexes:
            report.violations.append(Violation(
                severity="warning",
                rule_ref="§9.1",
                description=(
                    f"Hex {hex_id} marked Axis but only Allied units present"
                ),
                hex_ids=[hex_id],
            ))
        elif ctrl == "allied" and hex_id in axis_hexes and hex_id not in allied_hexes:
            report.violations.append(Violation(
                severity="warning",
                rule_ref="§9.1",
                description=(
                    f"Hex {hex_id} marked Allied but only Axis units present"
                ),
                hex_ids=[hex_id],
            ))


def _check_morale_bounds(state: GameState, report: ValidationReport) -> None:
    """§15.1 — Unit morale must stay in range [0, 10]."""
    report.checks_run.append("§15.1 Morale bounds")
    for unit in state.ground_units.values():
        if unit.status == UnitStatus.ELIMINATED:
            continue
        if not (0 <= unit.morale <= 10):
            report.violations.append(Violation(
                severity="warning",
                rule_ref="§15.1",
                description=f"{unit.name} morale out of range: {unit.morale}",
                unit_ids=[unit.id],
            ))


def _check_depot_load_bounds(state: GameState, report: ValidationReport) -> None:
    """§12.4 — Supply depot load may not exceed capacity."""
    report.checks_run.append("§12.4 Depot load within capacity")
    for depot in state.supply_counters.values():
        if depot.current_load > depot.capacity + 0.1:
            report.violations.append(Violation(
                severity="warning",
                rule_ref="§12.4",
                description=(
                    f"Depot {depot.name} load {depot.current_load:.1f} "
                    f"exceeds capacity {depot.capacity:.1f}"
                ),
            ))
        elif depot.current_load < -0.1:
            report.violations.append(Violation(
                severity="critical",
                rule_ref="§12.4",
                description=f"Depot {depot.name} has negative load ({depot.current_load:.2f})",
            ))


def _check_premature_unit_activity(state: GameState, report: ValidationReport) -> None:
    """§4.1 — Future-entry units must not appear in this turn's event log."""
    report.checks_run.append("§4.1 Reinforcement schedule (event log)")
    future_ids = {
        u.id for u in state.ground_units.values()
        if u.available_turn > state.turn and u.status != UnitStatus.ELIMINATED
    }
    if not future_ids:
        return
    for event in state.events:
        for uid in event.unit_ids:
            if uid in future_ids:
                unit = state.ground_units[uid]
                report.violations.append(Violation(
                    severity="warning",
                    rule_ref="§4.1",
                    description=(
                        f"{unit.name} (enters turn {unit.available_turn}) "
                        f"appears in a '{event.category}' event this turn: "
                        f"{event.description[:80]}"
                    ),
                    unit_ids=[uid],
                ))


# ── Public API ────────────────────────────────────────────────────────────────

def validate_turn(state: GameState) -> ValidationReport:
    """
    Run all post-turn consistency checks against the current game state.
    Returns a ValidationReport; does NOT raise exceptions.
    """
    report = ValidationReport(turn=state.turn)

    _check_impassable_hexes(state, report)
    _check_step_bounds(state, report)
    _check_fuel_bounds(state, report)
    _check_water_bounds(state, report)
    _check_active_units_placed(state, report)
    _check_zero_step_units_eliminated(state, report)
    _check_disorganized_cohesion(state, report)
    _check_hex_control_consistency(state, report)
    _check_morale_bounds(state, report)
    _check_depot_load_bounds(state, report)
    _check_premature_unit_activity(state, report)

    return report
