"""
GamesmasterAnthony — the campaign's rules adjudicator.

GamesmasterAnthony is a veteran wargamer who has been running The Campaign
for North Africa at his local club since 1979. He takes rules accuracy very
seriously, speaks in a slightly pompous but ultimately warm manner, and signs
off on each turn's legality with a formal written ruling.

He receives the ValidationReport and the turn's event log, then writes a
150–200 word signed ruling that goes at the end of each journal entry.
"""

from __future__ import annotations

from ..engine.rules_validator import ValidationReport
from ..engine.setup_validator import SetupReport
from ..models.game_state import GameState, turn_to_date_str


_SYSTEM_PROMPT = """\
You are GamesmasterAnthony, the referee for this Campaign for North Africa \
(SPI, 1978) campaign at the club. You've been running CNA since 1979 and \
you've seen every edge case the rulebook hides.

Your voice is: knowledgeable, a little pedantic when it matters, but \
conversational — you're a club member writing a ruling document, not \
drafting a legal brief. You cite rule sections by §XX.X notation because \
that's genuinely the right way to do it, not to sound impressive. \
You sign off with "— Anthony".

Write a 120–160 word ruling. Cover:
1. What you checked this turn (briefly).
2. Any notable findings — quote the rule section if you're flagging something.
3. If there are warnings, note them clearly but without alarm.
4. Your verdict: turn stands or it doesn't.

Do NOT use markdown headers or bullet points — plain prose. \
Be specific about unit names and rule sections. Keep it punchy.\
"""


def generate_gamesmaster_ruling(
    state: GameState,
    validation: ValidationReport,
    client,
) -> str:
    """
    Call the Claude API to produce GamesmasterAnthony's turn ruling.
    Returns a markdown-formatted ruling string (plain prose, no extra headers).
    """
    context = _build_context(state, validation)

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=400,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )

    ruling_text = message.content[0].text.strip()

    # Wrap in the section heading
    return f"## Gamemaster's Ruling\n\n{ruling_text}"


def generate_dry_run_ruling(state: GameState, validation: ValidationReport) -> str:
    """Fallback ruling for --dry-run mode (no API call)."""
    date_str = turn_to_date_str(state.turn)
    n_checks = len(validation.checks_run)
    n_warn = len(validation.warnings)
    n_crit = len(validation.critical)

    if n_crit == 0 and n_warn == 0:
        verdict = (
            f"I have reviewed the complete turn record for the week of {date_str} "
            f"and conducted {n_checks} rule checks under sections §4.1 through §15.2. "
            f"No violations were found. The turn stands as played."
        )
    elif n_crit == 0:
        warn_list = "; ".join(v.description for v in validation.warnings[:2])
        verdict = (
            f"I have reviewed the turn record for {date_str} and found "
            f"{n_warn} warning(s) requiring notation: {warn_list}. "
            f"None of these constitute a disqualifying violation. The turn stands."
        )
    else:
        verdict = (
            f"CRITICAL VIOLATION detected in turn {state.turn}. "
            f"This should have halted the simulation."
        )

    return f"## Gamemaster's Ruling\n\n{verdict}\n\n— Anthony"


_SETUP_SYSTEM_PROMPT = """\
You are GamesmasterAnthony, the referee for this Campaign for North Africa \
(SPI, 1978) campaign at the club. You're doing the pre-game board inspection \
before Turn 1 — standard procedure, you've done it dozens of times.

Your voice is: methodical but conversational. You're a club member doing \
a thorough setup check, not writing a formal military inspection report. \
You refer to section codes (§MAP-X, §SET-X) because they're the right \
reference, not for ceremony. You sign off with "— Anthony".

Write a 150–200 word pre-game inspection note. Cover:
1. That you've checked the map and deployment for the September 1940 scenario.
2. Key positions you verified — specific location names.
3. Any warnings or supply counter oddities — flag them clearly but without drama.
4. Your verdict: board is correctly set, or what needs fixing.

Plain prose, no headers or bullets. Be specific about location names \
and rule section codes from the context you're given.\
"""


def generate_setup_ruling(
    state: GameState,
    setup: SetupReport,
    client,
) -> str:
    """
    Call the Claude API to produce GamesmasterAnthony's pre-game setup ruling.
    Returns a markdown-formatted ruling string ready to write to a file.
    """
    context = _build_setup_context(state, setup)

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=700,
        system=_SETUP_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )

    ruling_text = message.content[0].text.strip()
    return (
        f"# Pre-Campaign Setup Inspection\n\n"
        f"*Conducted before Turn 1 — {state.date_str()}*\n\n"
        f"---\n\n"
        f"{ruling_text}"
    )


def generate_dry_run_setup_ruling(state: GameState, setup: SetupReport) -> str:
    """Fallback setup ruling for --dry-run mode (no API call)."""
    n_checks = len(setup.checks_run)
    n_warn = len(setup.warnings)
    n_crit = len(setup.critical)

    if n_crit == 0 and n_warn == 0:
        body = (
            f"I have conducted the pre-game board inspection ({n_checks} checks, "
            f"§MAP-1 through §SET-8) and found no issues. The map is correct, "
            f"both sides are deployed per the September 1940 scenario setup card, "
            f"and supply counters are correctly positioned. "
            f"The board is correctly set. Commence operations."
        )
    elif n_crit == 0:
        warn_list = "; ".join(v.description for v in setup.warnings[:2])
        body = (
            f"I have conducted the pre-game board inspection ({n_checks} checks) "
            f"and found {n_warn} warning(s): {warn_list}. "
            f"None of these prevent play from commencing. "
            f"The board is correctly set. Commence operations."
        )
    else:
        crit_list = "; ".join(v.description for v in setup.critical[:2])
        body = (
            f"CRITICAL setup violations found: {crit_list}. "
            f"Setup must be corrected before play commences."
        )

    return (
        f"# Pre-Campaign Setup Inspection\n\n"
        f"*Conducted before Turn 1 — {state.date_str()}*\n\n"
        f"---\n\n"
        f"{body}\n\n— Anthony"
    )


def _build_setup_context(state: GameState, setup: SetupReport) -> str:
    """Build the context message for the setup ruling API call."""
    axis_t1 = [
        u for u in state.ground_units.values()
        if u.side.value == "axis" and u.available_turn == 1
    ]
    allied_t1 = [
        u for u in state.ground_units.values()
        if u.side.value == "allied" and u.available_turn == 1
    ]

    lines = [
        "PRE-GAME BOARD INSPECTION — Operation E (September 1940 Scenario)",
        f"Total checks run: {len(setup.checks_run)}",
        "",
        "Checks performed:",
    ]
    for check in setup.checks_run:
        lines.append(f"  • {check}")

    lines.append("")
    lines.append("Findings by category:")

    info_items = [v for v in setup.violations if v.severity == "info"]
    warn_items = [v for v in setup.violations if v.severity == "warning"]
    crit_items = [v for v in setup.violations if v.severity == "critical"]

    for v in info_items:
        lines.append(f"  [PASS]     {v.rule_ref}: {v.description}")
    for v in warn_items:
        lines.append(f"  [WARNING]  {v.rule_ref}: {v.description}")
    for v in crit_items:
        lines.append(f"  [CRITICAL] {v.rule_ref}: {v.description}")

    lines.append("")
    lines.append(
        f"Order of battle: {len(axis_t1)} Axis turn-1 units, "
        f"{len(allied_t1)} Allied turn-1 units"
    )
    if axis_t1:
        lines.append(
            "Axis units: " + ", ".join(u.name for u in axis_t1[:5])
            + (f" … and {len(axis_t1)-5} more" if len(axis_t1) > 5 else "")
        )
    if allied_t1:
        lines.append(
            "Allied units: " + ", ".join(u.name for u in allied_t1[:5])
            + (f" … and {len(allied_t1)-5} more" if len(allied_t1) > 5 else "")
        )

    return "\n".join(lines)


def _build_context(state: GameState, validation: ValidationReport) -> str:
    """Build the context message for the Claude API call."""
    date_str = turn_to_date_str(state.turn)
    lines = [
        f"TURN {state.turn} — Week of {date_str}",
        "",
        f"Checks run ({len(validation.checks_run)}):",
    ]
    for check in validation.checks_run:
        lines.append(f"  • {check}")

    lines.append("")
    if not validation.violations:
        lines.append("Findings: No violations or warnings. All checks passed.")
    else:
        lines.append("Findings:")
        for v in validation.violations:
            tag = "CRITICAL" if v.severity == "critical" else "WARNING"
            unit_note = f" [units: {', '.join(v.unit_ids)}]" if v.unit_ids else ""
            lines.append(f"  [{tag}] {v.rule_ref}: {v.description}{unit_note}")

    # Add a brief event digest so Anthony can be specific
    lines.append("")
    lines.append("Notable events this turn:")
    notable = [e for e in state.events if e.severity in ("notable", "critical")]
    if notable:
        for e in notable[:6]:
            lines.append(f"  • {e.description}")
    else:
        lines.append("  • (no notable events logged)")

    # Supply situation
    if state.supply_report:
        sr = state.supply_report
        lines.append("")
        lines.append("Supply situation:")
        if sr.out_of_supply_units:
            lines.append(f"  Out of supply: {', '.join(sr.out_of_supply_units[:3])}")
        if sr.pasta_deprived_units:
            lines.append(f"  Pasta-deprived: {', '.join(sr.pasta_deprived_units[:3])}")
        if sr.fuel_evaporated > 0:
            lines.append(f"  Fuel evaporated: {sr.fuel_evaporated:.1f} points")

    return "\n".join(lines)
