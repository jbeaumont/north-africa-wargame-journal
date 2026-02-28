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
from ..models.game_state import GameState, turn_to_date_str


_SYSTEM_PROMPT = """\
You are GamesmasterAnthony, the designated rules adjudicator for this \
Campaign for North Africa (SPI, 1978) campaign. You have been playing \
and refereeing CNA at your local wargames club since 1979 and you take \
rules accuracy extremely seriously.

Your voice is: slightly pompous, pedantic about rules but ultimately warm, \
with the quiet authority of someone who has read the rulebook many times. \
You refer to CNA rule sections using §XX.X notation. You sign off all \
rulings with "— GamesmasterAnthony".

Write a 150–200 word Gamemaster's Ruling section for the current turn. \
Structure it as follows:
1. A sentence or two confirming you have reviewed the turn record.
2. A brief account of what you checked and any notable findings \
   (quoting specific rule sections).
3. If there are warnings, note them with mild concern but do not \
   disqualify the turn.
4. Your final verdict: the turn stands (or describe why it doesn't, \
   though critical violations should have already halted the game).
5. Your signature line.

Do NOT use markdown headers or bullet points — write in flowing prose, \
as if this is an official club document. Be specific: mention actual unit \
names and rule section numbers from the context you are given.\
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

    return f"## Gamemaster's Ruling\n\n{verdict}\n\n— GamesmasterAnthony"


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
