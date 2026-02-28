"""
Strategic player agents for The Campaign for North Africa simulation.

Two player-side agents, one per side, each maintaining a living markdown
document that accumulates strategic notes across the campaign.

Each turn they receive:
  - Their side's full supply and unit situation
  - A summary of observable enemy activity
  - Their existing notes (current assessment + last 3 turn lessons)

They output:
  - An updated Current Assessment (what matters right now)
  - A new Turn Note entry (what was learned this turn)

The documents are stored as:
  journal/axis_commander.md
  journal/allied_commander.md

Characters
----------
Axis:  Phil — club member playing the Axis side.  Experienced CNA player.
       Analytical about supply math, treats Italian logistics as a puzzle
       to solve rather than a tragedy to suffer through.

Allied: Terry — club member playing the Allied side.  Patient and
        methodical.  Reads a lot of Western Desert history.  Knows the
        early game is quiet and waits for the right moment.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models.counter import Side
from ..models.game_state import GameState, turn_to_date_str

JOURNAL_DIR = Path("journal")

# ── Player character definitions ──────────────────────────────────────────────

_AXIS_SYSTEM = """\
You are Phil, playing the Axis side in a Campaign for North Africa (SPI, 1978) \
campaign at a wargames club. You've played CNA before and know what you're in for.

Key mechanics you're managing:
- Fuel evaporates each turn (3% Italian, lower for Germans when they arrive)
- Water is tracked per Operations Stage — units that run dry lose combat effectiveness
- Italian infantry need a pasta ration allocation or they take cohesion hits (§38.5)
- Supply lines must be within 5 hexes of a depot or units go out of supply
- The DAK arrives at Turn 14 — better logistics, no pasta dependency
- Tobruk port (hex 1701) is essential for pushing supply east
- The Axis has more units but Italian logistics are genuinely fragile

Write as a player thinking through the game. Talk about units by name, \
cite hex positions, reason through supply math. Reference historical context \
when it's interesting background, not as immersion. Voice: analytical, \
occasionally dry about the Italian supply situation, focused on solving the \
logistics puzzle. First person, game-club voice.\
"""

_ALLIED_SYSTEM = """\
You are Terry, playing the Allied side in a Campaign for North Africa (SPI, 1978) \
campaign at a wargames club. You've read a lot about the Western Desert campaign. \
You know the early game is quiet for your side.

Key mechanics you're managing:
- Allied supply evaporates at 7% per turn (worse than Axis — worth watching)
- Cairo (hex 2201) and Alexandria (hex 2001) are infinite supply bases — \
  protecting these is non-negotiable
- Italian 10th Army is larger but logistically fragile — pasta rule, fuel \
  evaporation, and OOS attrition all work against Phil
- DAK arrives at Turn 14 and changes the balance — that's the clock running
- Mersa Matruh (hex 1801) and El Alamein (hex 1902) are your natural defense lines
- Qattara Depression (hex 1910) locks your southern flank — free anchor
- 7th Armoured is your main mobile strike force — wait for the right moment

Write as a player thinking through the game. Talk about units by name, \
reason through positions and force ratios, note what you're watching. \
Reference historical parallels as interesting background context. Voice: patient, \
methodical, occasionally dry about watching Phil's supply situation deteriorate. \
First person, game-club voice.\
"""

_UPDATE_PROMPT = """\
It is now Turn {turn} — {date}.

=== MY FORCES ({side}) ===
Active units: {my_active}
Units out of supply: {my_oos}
{pasta_line}\
Notable force events this turn:
{my_events}

=== SUPPLY CHAIN ===
{supply_summary}

=== ENEMY SITUATION (intelligence) ===
Enemy active units: {enemy_active} (estimated)
Notable enemy events this turn:
{enemy_events}

=== MY EXISTING DOCTRINE ===
--- Current Assessment (previous turn) ---
{current_assessment}

--- Last {n_lessons} Lessons ---
{recent_lessons}

=== OUTPUT FORMAT — FOLLOW EXACTLY ===
Write your response as two sections separated by "---" on its own line. \
Do NOT use any markdown headers (no # symbols). Do NOT add classification \
labels or preamble. Start immediately with the section label on its own \
line, then a blank line, then the text.

CURRENT ASSESSMENT
[150-200 words: campaign situation, immediate priority, critical pressure points]

---

TURN {turn} LESSON — {date}
[120-150 words: what you learned or confirmed, citing supply figures, \
unit names, hex positions; root cause of any failures; \
how to repeat successes]\
"""


# ── Document assembly ─────────────────────────────────────────────────────────

_AXIS_DOC_PATH = JOURNAL_DIR / "axis_commander.md"
_ALLIED_DOC_PATH = JOURNAL_DIR / "allied_commander.md"

_DOC_TEMPLATE = """\
# {side_title} Commander's Strategic Journal

*{commander_name}*

---

## Current Strategic Assessment

*Updated Turn {turn} — {date}*

{assessment}

## Campaign Lessons

{lessons}
"""

_LESSON_TEMPLATE = "### Turn {turn} — {date}\n\n{text}\n"


def _doc_path(side: Side) -> Path:
    return _AXIS_DOC_PATH if side == Side.AXIS else _ALLIED_DOC_PATH


def _side_title(side: Side) -> str:
    return "Axis" if side == Side.AXIS else "Allied"


def _commander_name(side: Side) -> str:
    return (
        "Phil — playing Axis"
        if side == Side.AXIS
        else "Terry — playing Allied"
    )


def _parse_existing_doc(doc_text: str) -> tuple[str, list[tuple[int, str, str]]]:
    """
    Extract (current_assessment, [(turn, date, text), ...]) from an existing doc.
    Returns safe defaults if parsing fails.
    """
    assessment = "(No prior assessment — campaign just begun.)"
    lessons: list[tuple[int, str, str]] = []

    # Extract current assessment block
    m = re.search(
        r"## Current Strategic Assessment\n.*?\n\n(.*?)(?=\n---|\Z)",
        doc_text, re.DOTALL
    )
    if m:
        # Strip the italicised "Updated Turn N" line
        block = re.sub(r"\*Updated Turn \d+ — [^*]+\*\n\n", "", m.group(1))
        assessment = block.strip()

    # Extract lesson entries: ### Turn N — date\n\ntext
    for lesson_m in re.finditer(
        r"### Turn (\d+) — ([^\n]+)\n\n(.*?)(?=\n### Turn|\Z)",
        doc_text, re.DOTALL
    ):
        turn_n = int(lesson_m.group(1))
        date_s = lesson_m.group(2).strip()
        text_s = lesson_m.group(3).strip()
        lessons.append((turn_n, date_s, text_s))

    return assessment, lessons


def _assemble_doc(
    side: Side,
    turn: int,
    date: str,
    new_assessment: str,
    all_lessons: list[tuple[int, str, str]],
) -> str:
    lessons_md = "\n".join(
        _LESSON_TEMPLATE.format(turn=t, date=d, text=txt)
        for t, d, txt in reversed(all_lessons)   # Most recent first
    )
    return _DOC_TEMPLATE.format(
        side_title=_side_title(side),
        commander_name=_commander_name(side),
        turn=turn,
        date=date,
        assessment=new_assessment,
        lessons=lessons_md if lessons_md else "*(No lessons recorded yet.)*",
    )


# ── Context building ──────────────────────────────────────────────────────────

def _build_context(
    state: GameState,
    side: Side,
    current_assessment: str,
    recent_lessons: list[tuple[int, str, str]],
) -> str:
    date = turn_to_date_str(state.turn)
    enemy_side = Side.ALLIED if side == Side.AXIS else Side.AXIS

    my_units = state.active_units_for_side(side)
    enemy_units = state.active_units_for_side(enemy_side)

    # Out of supply
    my_oos = [
        u.name for u in my_units
        if u.id in state.supply_lines and not state.supply_lines[u.id].in_supply
    ]

    # Events split by side
    my_events_raw = [
        e.description for e in state.events
        if e.severity in ("notable", "critical")
        and any(
            u_id in [u.id for u in my_units]
            for u_id in e.unit_ids
        )
    ]
    enemy_events_raw = [
        e.description for e in state.events
        if e.severity in ("notable", "critical")
        and any(
            u_id in [u.id for u in enemy_units]
            for u_id in e.unit_ids
        )
    ]
    # Any notable event not captured above
    all_notable = [
        e.description for e in state.events if e.severity in ("notable", "critical")
    ]
    misc_events = [e for e in all_notable
                   if e not in my_events_raw and e not in enemy_events_raw]

    def _fmt_list(items: list[str], limit: int = 6) -> str:
        if not items:
            return "  (none reported)"
        return "\n".join(f"  • {x}" for x in items[:limit])

    # Supply summary
    supply_lines = []
    if state.supply_report:
        sr = state.supply_report
        supply_lines.append(f"  Fuel evaporated: {sr.fuel_evaporated:.1f} points")
        if sr.out_of_supply_units:
            supply_lines.append(
                f"  Out of supply: {', '.join(sr.out_of_supply_units[:4])}"
            )
        if sr.pasta_deprived_units and side == Side.AXIS:
            supply_lines.append(
                f"  Pasta-deprived (cohesion hit): "
                f"{', '.join(sr.pasta_deprived_units[:3])}"
            )
    my_depots = state.supply_depots_for_side(side)
    for d in my_depots[:4]:
        supply_lines.append(
            f"  {d.name}: {d.current_load:.0f}/{d.capacity:.0f} "
            f"({'at ' + d.hex_id if d.hex_id else 'no position'})"
        )

    pasta_line = ""
    if side == Side.AXIS and state.supply_report and state.supply_report.pasta_deprived_units:
        pasta_line = (
            f"Pasta-deprived units: "
            f"{', '.join(state.supply_report.pasta_deprived_units[:3])}\n"
        )

    lessons_text = "\n\n".join(
        f"Turn {t} — {d}:\n{txt}"
        for t, d, txt in recent_lessons[-3:]
    ) if recent_lessons else "(No prior lessons.)"

    return _UPDATE_PROMPT.format(
        turn=state.turn,
        date=date,
        side=_side_title(side).upper(),
        my_active=len(my_units),
        my_oos=", ".join(my_oos[:4]) if my_oos else "none",
        pasta_line=pasta_line,
        my_events=_fmt_list(my_events_raw + misc_events),
        supply_summary="\n".join(supply_lines) if supply_lines else "  (no data)",
        enemy_active=len(enemy_units),
        enemy_events=_fmt_list(enemy_events_raw),
        current_assessment=current_assessment,
        n_lessons=len(recent_lessons[-3:]),
        recent_lessons=lessons_text,
    )


# ── Parsing API response ──────────────────────────────────────────────────────

def _parse_response(text: str, turn: int, date: str) -> tuple[str, str]:
    """
    Extract (assessment, lesson) from the model's response.

    Handles: 'CURRENT ASSESSMENT', '## CURRENT ASSESSMENT', 'CURRENT ASSESSMENT:'
    and the same variants for 'TURN N LESSON'.  Falls back to a '---' split,
    then to a simple halving if all else fails.
    """
    # Strip any leading markdown headers / preamble the model adds
    cleaned = re.sub(r"^(#{1,3}[^\n]*\n+)+", "", text.strip())

    assessment = ""
    lesson = ""

    # Pattern: optional #s, optional spaces, section name, optional colon
    assessment_pat = re.compile(
        r"^(?:#{1,3}\s*)?CURRENT ASSESSMENT:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    lesson_pat = re.compile(
        r"^(?:#{1,3}\s*)?TURN \d+ LESSON[^:\n]*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    a_match = assessment_pat.search(cleaned)
    l_match = lesson_pat.search(cleaned)

    if a_match and l_match:
        a_start = a_match.end()
        l_start = l_match.end()
        if a_start < l_start:
            assessment = cleaned[a_start:l_match.start()].strip()
            lesson = cleaned[l_start:].strip()
        else:
            lesson = cleaned[l_start:a_match.start()].strip()
            assessment = cleaned[a_start:].strip()
    elif a_match:
        assessment = cleaned[a_match.end():].strip()
        lesson = assessment  # Duplicate if only one section found
    elif l_match:
        lesson = cleaned[l_match.end():].strip()
        assessment = lesson
    else:
        # Try splitting on the '---' separator
        parts = re.split(r"\n---+\n", cleaned, maxsplit=1)
        if len(parts) == 2:
            assessment = parts[0].strip()
            lesson = parts[1].strip()
            # Strip any residual lesson header from lesson part
            lesson = lesson_pat.sub("", lesson).strip()
        else:
            half = len(cleaned) // 2
            assessment = cleaned[:half].strip()
            lesson = cleaned[half:].strip()

    return assessment, lesson


# ── Public API ────────────────────────────────────────────────────────────────

_JOURNAL_CONTRIBUTION_PROMPT = """\
It is Turn {turn} — {date}.  Write your player notes for this turn's journal entry.

{side_label} side.  Active units: {my_active}.  OOS: {my_oos}.
{pasta_line}\
Supply snapshot: {supply_snapshot}
Notable events: {my_events}

Write 110–140 words in your own voice — what you observed this turn, what you're \
thinking about for next turn, what the mechanics gave or took from you. \
Be specific: name units, cite hex positions or supply figures. \
No dramatic language. No character immersion. Just a player's honest notes.

IMPORTANT: Do NOT start with a date header, bold text header, or any markdown \
heading. Start directly with your notes. End with a complete sentence.\
"""


def generate_journal_contribution(
    state: GameState,
    side: Side,
    client,
) -> str:
    """
    Generate a short (100-130 word) player-notes section for inclusion in
    the main turn journal.  Returns plain text (no markdown headers).
    """
    date = turn_to_date_str(state.turn)
    side_label = "Axis (Phil)" if side == Side.AXIS else "Allied (Terry)"
    system = _AXIS_SYSTEM if side == Side.AXIS else _ALLIED_SYSTEM

    my_units = state.active_units_for_side(side)
    my_oos = [
        u.name for u in my_units
        if u.id in state.supply_lines and not state.supply_lines[u.id].in_supply
    ]
    my_events_raw = [
        e.description for e in state.events
        if e.severity in ("notable", "critical")
        and any(u_id in [u.id for u in my_units] for u_id in e.unit_ids)
    ]

    pasta_line = ""
    if side == Side.AXIS and state.supply_report and state.supply_report.pasta_deprived_units:
        pasta_line = (
            f"Pasta-deprived: {', '.join(state.supply_report.pasta_deprived_units[:3])}\n"
        )

    supply_snapshot = "(no data)"
    if state.supply_report:
        sr = state.supply_report
        parts = [f"fuel evaporated {sr.fuel_evaporated:.1f} pts"]
        if sr.out_of_supply_units:
            parts.append(f"{len(sr.out_of_supply_units)} unit(s) OOS")
        supply_snapshot = ", ".join(parts)

    events_text = (
        "; ".join(my_events_raw[:4]) if my_events_raw else "(none notable)"
    )

    prompt = _JOURNAL_CONTRIBUTION_PROMPT.format(
        turn=state.turn,
        date=date,
        side_label=side_label,
        my_active=len(my_units),
        my_oos=", ".join(my_oos[:4]) if my_oos else "none",
        pasta_line=pasta_line,
        supply_snapshot=supply_snapshot,
        my_events=events_text,
    )

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=350,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def generate_dry_run_journal_contribution(state: GameState, side: Side) -> str:
    """Fallback for --dry-run mode."""
    date = turn_to_date_str(state.turn)
    label = "Phil (Axis)" if side == Side.AXIS else "Terry (Allied)"
    my_units = state.active_units_for_side(side)
    my_oos = [
        u.name for u in my_units
        if u.id in state.supply_lines and not state.supply_lines[u.id].in_supply
    ]
    return (
        f"[DRY RUN — {label}] Turn {state.turn} ({date}): "
        f"{len(my_units)} units active, "
        + (f"{len(my_oos)} OOS ({', '.join(my_oos[:2])}). " if my_oos else "all in supply. ")
        + "No API call made."
    )


def generate_commander_update(
    state: GameState,
    side: Side,
    client,
) -> str:
    """
    Generate a strategic update for one side's commander.
    Reads the existing doc, calls the Claude API, and returns the new
    full document text (caller is responsible for writing it to disk).
    """
    date = turn_to_date_str(state.turn)
    doc_path = _doc_path(side)

    # Load existing document
    existing_doc = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
    current_assessment, all_lessons = _parse_existing_doc(existing_doc)

    context = _build_context(state, side, current_assessment, all_lessons)
    system = _AXIS_SYSTEM if side == Side.AXIS else _ALLIED_SYSTEM

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=700,
        system=system,
        messages=[{"role": "user", "content": context}],
    )

    response_text = message.content[0].text.strip()
    new_assessment, lesson_text = _parse_response(response_text, state.turn, date)

    # Replace any existing entry for this turn (handles re-runs / regeneration)
    all_lessons = [(t, d, txt) for t, d, txt in all_lessons if t != state.turn]
    all_lessons.append((state.turn, date, lesson_text))

    return _assemble_doc(side, state.turn, date, new_assessment, all_lessons)


def generate_dry_run_commander_update(
    state: GameState,
    side: Side,
) -> str:
    """Fallback for --dry-run mode. Produces a simple placeholder update."""
    date = turn_to_date_str(state.turn)
    doc_path = _doc_path(side)
    existing_doc = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
    current_assessment, all_lessons = _parse_existing_doc(existing_doc)

    my_units = state.active_units_for_side(side)
    my_oos = [
        u for u in my_units
        if u.id in state.supply_lines and not state.supply_lines[u.id].in_supply
    ]
    sr = state.supply_report

    new_assessment = (
        f"Turn {state.turn} ({date}): {len(my_units)} active units. "
        f"{len(my_oos)} out of supply. "
        + (f"Fuel evaporated: {sr.fuel_evaporated:.1f} pts. " if sr else "")
        + "Assessment requires full API call."
    )
    lesson_text = (
        f"[DRY RUN — Turn {state.turn}] Supply situation: "
        + (f"{len(my_oos)} units OOS, {sr.fuel_evaporated:.1f} pts evaporated. "
           if sr else "")
        + "No API call made."
    )

    all_lessons = [(t, d, txt) for t, d, txt in all_lessons if t != state.turn]
    all_lessons.append((state.turn, date, lesson_text))
    return _assemble_doc(side, state.turn, date, new_assessment, all_lessons)


def write_commander_doc(side: Side, content: str) -> Path:
    """Write the commander document to disk. Returns the path."""
    path = _doc_path(side)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
