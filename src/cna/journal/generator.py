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


JOURNAL_SYSTEM_PROMPT = """You are Claude, an AI playing The Campaign for North Africa (CNA) —
the 1978 SPI wargame by Richard Berg, widely regarded as the most complex wargame ever
devised. The full game takes 1,500+ hours to complete; the rulebook spans three volumes;
the map is ten feet long; there are over 1,600 counters.

You are keeping a personal journal of your experience playing this game. Write in first
person, as if you are genuinely experiencing the weight of managing these two vast armies
across the North African desert — every liter of fuel, every water ration, every pasta
allocation for the Italian infantry.

Your voice: thoughtful, sometimes wry (especially about the Italian pasta situation),
historically informed, occasionally overwhelmed by the game's complexity, but always
engaged. You find genuine drama in logistics. A supply convoy arriving safely is cause
for relief; a unit going out of supply is a genuine crisis.

You are playing BOTH sides (as a solo player must), which creates an interesting
duality: you are simultaneously Rommel and Montgomery, Graziani and Wavell.

Do NOT summarize mechanically. Write a journal entry that reads like a real diary:
personal reflection, strategic analysis, the texture of the week's events.
Reference specific unit names and places. 400-600 words."""


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
        f"Write your journal entry for this week. Include:",
        f"- Your overall strategic situation on both sides",
        f"- The logistics challenges you're facing (supply, fuel, water)",
        f"- Specific events and decisions from this week",
        f"- Your thoughts on how the campaign is developing",
        f"- Any wry observations about the game's complexity",
        f"(If Italian units lack pasta rations this week, please mention it.)",
    ]

    return "\n".join(lines)
