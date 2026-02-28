"""
Journal entry generator for The Campaign for North Africa simulation.

Uses the Anthropic Claude API to generate rich first-person narrative journal
entries based on the current game state. Each entry covers one week of the
campaign (one CNA turn).

The journal is written from the perspective of Claude — an AI encountering
this notoriously complex game and grappling with its demands: the obsessive
logistics, the constant supply crises, the Italian pasta problem, the
mechanical breakdowns in the desert heat, and the slowly turning tide of
a three-year campaign.
"""

from __future__ import annotations

import os
from typing import Optional

import anthropic

from ..models.game_state import GameState, turn_to_date_str
from ..models.counter import Side


JOURNAL_SYSTEM_PROMPT = """You are writing the session log for a Campaign for North Africa (SPI, 1978) campaign
run at a wargames club. CNA is the 1978 Richard Berg design — notoriously the most complex
wargame ever published. Three rulebook volumes, a ten-foot map, 1,600+ counters, 1,500 hours
to complete. The club runs it as a long campaign with two players (Phil on Axis, Terry on
Allied) and a referee (Anthony).

Write the session notes for this turn: what happened on the board, what the supply picture
looks like, which mechanics made themselves felt. Write in the voice of someone keeping an
accurate, engaged club record — interested in the game as a game, not theatrically involved
in it.

Voice: analytical, occasionally dry, treats the mechanics as the interesting puzzle they are.
References unit names and hex positions when relevant. Notes the historically interesting
situation when it arises, but as context — not as immersion. When Italian units are pasta-
deprived, note it as the rules curiosity it is, not as an existential tragedy.

Write in third person or neutral analytical voice. Do NOT write as "I". This is a campaign
record, not a diary. Do NOT use dramatic flourishes. Interesting ≠ overwhelming.
Do NOT invent a session date or list of attendees — just write the notes.
200-250 words."""


def generate_journal_entry(
    state: GameState,
    client: Optional[anthropic.Anthropic] = None,
    model: str = "claude-opus-4-6",
) -> str:
    """
    Generate a journal entry for the current turn.

    Parameters:
      state:  The current GameState (after all turn processing is complete).
      client: An anthropic.Anthropic client. If None, creates one from ANTHROPIC_API_KEY.
      model:  Claude model to use for generation.

    Returns the journal entry as a string.
    """
    if client is None:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    context = _build_context(state)

    response = client.messages.create(
        model=model,
        max_tokens=1200,
        system=JOURNAL_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": context}
        ],
    )

    return response.content[0].text


def _build_context(state: GameState) -> str:
    """Build the context message describing this turn's events for journal generation."""
    date_str = turn_to_date_str(state.turn)
    summary = state.narrative_summary()

    # Gather notable events by category
    notable_events = [e for e in state.events if e.severity in ("notable", "critical")]
    combat_events = [e for e in state.events if e.category == "combat"]
    supply_events = [e for e in state.events if e.category == "supply"]
    air_events = [e for e in state.events if e.category == "air"]
    reinforcement_events = [e for e in state.events if e.category == "reinforcement"]

    lines = [
        f"CAMPAIGN JOURNAL — Turn {state.turn} of 100",
        f"Week of: {date_str}",
        f"",
        f"=== GAME STATE ===",
        summary,
        f"",
        f"=== EVENTS THIS WEEK ===",
    ]

    if combat_events:
        lines.append("COMBAT:")
        for e in combat_events[:6]:
            lines.append(f"  • {e.description}")

    if supply_events:
        lines.append("SUPPLY SITUATION:")
        for e in supply_events[:6]:
            lines.append(f"  • {e.description}")

    if air_events:
        lines.append("AIR OPERATIONS:")
        for e in air_events[:4]:
            lines.append(f"  • {e.description}")

    if reinforcement_events:
        lines.append("REINFORCEMENTS:")
        for e in reinforcement_events[:3]:
            lines.append(f"  • {e.description}")

    if not notable_events:
        lines.append("  (A quiet week — no major engagements. Focus on logistics.)")

    lines += [
        f"",
        f"=== YOUR TASK ===",
        f"Write the session notes for this turn. Cover:",
        f"- The overall state of play on both sides",
        f"- The supply/fuel/water situation and what it means mechanically",
        f"- The notable events from this turn",
        f"- What the position looks like heading into next turn",
        f"(Note any pasta-ration failures as the interesting rule quirk they are.)",
    ]

    return "\n".join(lines)
