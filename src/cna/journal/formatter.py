"""
Markdown formatter for CNA journal entries.

Writes journal entries to the journal/ directory as individual .md files,
one per turn. Also maintains a master index (journal/README.md) listing
all entries with dates and key events.
"""

from __future__ import annotations

from pathlib import Path

from ..models.game_state import GameState, turn_to_date_str
from ..models.counter import Side, UnitStatus
from .map_renderer import render_ascii_map


JOURNAL_DIR = Path(__file__).parent.parent.parent.parent / "journal"


def write_journal_entry(
    turn: int,
    entry_text: str,
    state: GameState,
    journal_dir: Path = JOURNAL_DIR,
) -> Path:
    """
    Write a journal entry to journal/turn_NNN_YYYY-MM-DD.md.
    Returns the path of the written file.
    """
    journal_dir.mkdir(parents=True, exist_ok=True)

    from ..models.game_state import turn_to_date
    date = turn_to_date(turn)
    filename = f"turn_{turn:03d}_{date.strftime('%Y-%m-%d')}.md"
    filepath = journal_dir / filename

    frontmatter = _build_frontmatter(turn, state)
    map_block = render_ascii_map(state)
    sidebar = _build_sidebar(state)
    content = (
        frontmatter
        + "\n\n"
        + map_block
        + "\n\n"
        + entry_text
        + "\n\n---\n\n"
        + sidebar
    )

    filepath.write_text(content, encoding="utf-8")
    return filepath


def _build_frontmatter(turn: int, state: GameState) -> str:
    """Build the markdown header for a journal entry."""
    date_str = turn_to_date_str(turn)
    return f"""# Campaign Journal — Turn {turn}
## Week of {date_str}

*The Campaign for North Africa — AI Journal*
*Turn {turn} of 100 | Operations Stage complete*

---"""


def _build_sidebar(state: GameState) -> str:
    """Build a statistical sidebar for the journal entry."""
    axis_units = state.active_units_for_side(Side.AXIS)
    allied_units = state.active_units_for_side(Side.ALLIED)

    axis_steps = sum(u.steps for u in axis_units)
    allied_steps = sum(u.steps for u in allied_units)

    axis_oos = len(state.out_of_supply_units(Side.AXIS))
    allied_oos = len(state.out_of_supply_units(Side.ALLIED))

    axis_elim = [u for u in state.ground_units.values()
                 if u.side == Side.AXIS and u.status == UnitStatus.ELIMINATED]
    allied_elim = [u for u in state.ground_units.values()
                   if u.side == Side.ALLIED and u.status == UnitStatus.ELIMINATED]

    lines = [
        "## Situation Report",
        "",
        "| Metric | Axis | Allied |",
        "|--------|------|--------|",
        f"| Active units | {len(axis_units)} | {len(allied_units)} |",
        f"| Total steps | {axis_steps} | {allied_steps} |",
        f"| Out of supply | {axis_oos} | {allied_oos} |",
        f"| Eliminated | {len(axis_elim)} | {len(allied_elim)} |",
    ]

    if state.supply_report:
        sr = state.supply_report
        lines += [
            "",
            "### Supply Situation",
            "",
        ]
        if sr.fuel_critical_units:
            lines.append(f"**Fuel critical:** {', '.join(sr.fuel_critical_units[:3])}")
        if sr.water_critical_units:
            lines.append(f"**Water critical:** {', '.join(sr.water_critical_units[:3])}")
        if sr.out_of_supply_units:
            lines.append(f"**Out of supply:** {', '.join(sr.out_of_supply_units[:3])}")
        if sr.pasta_deprived_units:
            lines.append(
                f"**Pasta-deprived (Italian):** {', '.join(sr.pasta_deprived_units[:3])}")
        if sr.fuel_evaporated > 0:
            lines.append(f"**Fuel evaporated:** {sr.fuel_evaporated:.1f} points")

    # Notable events digest
    notable = [e for e in state.events if e.severity == "critical"]
    if notable:
        lines += ["", "### Critical Events"]
        for e in notable[:5]:
            lines.append(f"- {e.description}")

    return "\n".join(lines)


def write_master_index(
    completed_turns: list[tuple[int, str, GameState]],
    journal_dir: Path = JOURNAL_DIR,
) -> Path:
    """
    Write/update journal/README.md with links to all completed turns.

    completed_turns: list of (turn_number, first_line_of_entry, state) tuples.
    """
    journal_dir.mkdir(parents=True, exist_ok=True)
    index_path = journal_dir / "README.md"

    lines = [
        "# The Campaign for North Africa — AI Journal",
        "",
        "An AI journal documenting Claude's experience playing *The Campaign for North Africa*,",
        "the 1978 SPI wargame widely regarded as the most complex board game ever created.",
        "",
        "**Campaign:** 9 September 1940 – 13 May 1943  ",
        "**Turns:** 100 total (1 turn = 1 week)  ",
        "**Sides:** Axis (Italy + Germany) vs. Allied (Britain, Commonwealth, USA)  ",
        "",
        "---",
        "",
        "## Journal Entries",
        "",
        "| Turn | Date | Preview |",
        "|------|------|---------|",
    ]

    for turn_num, first_line, st in completed_turns:
        date_str = turn_to_date_str(turn_num)
        from ..models.game_state import turn_to_date
        date = turn_to_date(turn_num)
        filename = f"turn_{turn_num:03d}_{date.strftime('%Y-%m-%d')}.md"
        preview = first_line[:80].replace("|", "\\|") if first_line else ""
        lines.append(f"| [{turn_num}]({filename}) | {date_str} | {preview}... |")

    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path
