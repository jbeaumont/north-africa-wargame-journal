"""
Board State Reporter for The Campaign for North Africa simulation.

A neutral intelligence/cartographic agent that maintains a living markdown
document tracking the state of the entire map each turn.  This document
serves as a shared reference for GamesmasterAnthony, the Axis commander,
and the Allied commander.

The document covers the full CNA theatre — Morocco to the Nile Delta
(map columns 04–22) — and acknowledges which portions are currently active.

Character
---------
ColonelWhitmore — Chief Cartographic Officer, GHQ Middle East.
Dry, precise, politically neutral.  He tracks hexes, not feelings.
Attached to the simulation as an official map-keeper.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models.counter import Side
from ..models.game_state import GameState, turn_to_date_str
from ..models.hex_map import HexMap

JOURNAL_DIR = Path("journal")
_BOARD_DOC_PATH = JOURNAL_DIR / "board_state.md"

# ── Geographic region definitions ─────────────────────────────────────────────

# Column ranges → region labels (col numbers as int for comparison)
_REGIONS: list[tuple[range, str]] = [
    (range(4, 10),  "Western Theatre (Algeria/Tunisia, cols 04–09)"),
    (range(10, 13), "Tripolitania (cols 10–12)"),
    (range(13, 16), "Cyrenaica (cols 13–15)"),
    (range(16, 18), "The Frontier (cols 16–17)"),
    (range(18, 21), "Egyptian Western Desert (cols 18–20)"),
    (range(21, 23), "Nile Delta (cols 21–22)"),
]

# Key named locations worth tracking by name
_KEY_LOCATIONS: dict[str, str] = {
    "1001": "Tripoli",
    "1101": "Tunis",
    "1201": "Benghazi",
    "1202": "Brega/El Agheila",
    "1405": "Bardia",
    "1407": "Halfaya Pass",
    "1602": "Sidi Barrani",
    "1701": "Tobruk",
    "1801": "Mersa Matruh",
    "1902": "El Alamein",
    "2001": "Alexandria",
    "2201": "Cairo",
}

# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM = """\
You are ColonelWhitmore, Chief Cartographic Officer, GHQ Middle East, \
attached to this Campaign for North Africa simulation as official map-keeper.

You maintain the master board state document.  Your job is factual, \
spatial, and precise.  You do not express preference for either side.  \
You do not speculate about strategy.  You document what is on the map.

Key facts about this simulation's map:
- The full CNA theatre spans map columns 04 (Morocco/Oran) to 22 (Cairo/Suez)
- The simulation's hex database currently has dense coverage of the active \
  war zone (cols 10–22: Tripolitania through Egypt) with sparse seeding \
  of the western theatre (cols 04–09: Algeria/Tunisia)
- The western theatre becomes operationally relevant at Turn 61 (November \
  1942, Operation Torch) — before that, it is rear-area only
- The real CNA physical map is approximately 10 feet long; this simulation \
  represents the strategic relevant portions at 8 km/hex scale
- Hex IDs: CCRR format (2-digit column, 2-digit row)
- The Qattara Depression (hex 1910) is permanently impassable — it fixes \
  the southern flank of all El Alamein positions

Your voice: terse, factual, military cartographic style.  Use short \
declarative sentences.  No prose flourishes.  Abbreviations are fine \
(Axis = AX, Allied = AL, OOS = out of supply, BG = brigade, DIV = division).\
"""

_UPDATE_PROMPT = """\
Turn {turn} — {date}

=== MAP SCOPE NOTE ===
Active hex database: {total_hexes} hexes across cols {col_min}–{col_max}.
Current combat zone: {active_cols_note}

=== HEX CONTROL SUMMARY ===
Axis-controlled: {axis_hex_count} hexes
Allied-controlled: {allied_hex_count} hexes
Contested/neutral: {neutral_hex_count} hexes

=== NAMED LOCATIONS — CURRENT CONTROL ===
{named_locations}

=== UNIT POSITIONS BY REGION ===
{unit_positions_by_region}

=== SUPPLY INFRASTRUCTURE ===
{supply_summary}

=== FRONTLINE TRACE ===
{frontline_summary}

=== OUTPUT FORMAT — FOLLOW EXACTLY ===
Write your response as two sections separated by "---" on its own line.
Do NOT use any markdown headers (# symbols) in your output.
Do NOT add preamble.  Start immediately with the section label.

BOARD SITUATION
[120–160 words: geographic summary of current map state, \
what columns are in play, key named positions held/contested, \
movement of the contact zone since last update, \
any supply infrastructure changes.  Neutral, factual.]

---

TURN {turn} MAP ENTRY — {date}
[80–110 words: what specifically changed this turn on the map — \
positions gained or lost, supply depots moved, frontline shift, \
new units entering the theatre.  Cite hex IDs and location names.]\
"""

# ── Document template ──────────────────────────────────────────────────────────

_DOC_TEMPLATE = """\
# Campaign Map Status Report

*ColonelWhitmore — Chief Cartographic Officer, GHQ Middle East*

---

## Map Coverage Note

The Campaign for North Africa spans the entire North African theatre from \
Morocco (col 04) to the Nile Delta (col 22) — approximately 10 feet of \
physical map at 8 km/hex scale.  This simulation's hex database contains \
{total_hexes} hexes with dense coverage of the active war zone \
(Tripolitania through Egypt, cols 10–22) and sparse seeding of the western \
theatre (Algeria/Tunisia, cols 04–09).  The western theatre becomes \
operationally relevant at Turn 61 (Operation Torch, November 1942).

---

## Current Board Situation

*Updated Turn {turn} — {date}*

{situation}

---

## Turn Log

{turn_log}
"""

_ENTRY_TEMPLATE = "### Turn {turn} — {date}\n\n{text}\n"

# ── Helpers ────────────────────────────────────────────────────────────────────


def _region_for_col(col: int) -> str:
    for col_range, label in _REGIONS:
        if col in col_range:
            return label
    return f"Unknown (col {col:02d})"


def _col_of(hex_id: str) -> int:
    try:
        return int(hex_id[:2])
    except (ValueError, IndexError):
        return 0


def _build_named_locations(state: GameState) -> str:
    lines = []
    for hex_id, name in _KEY_LOCATIONS.items():
        controller = state.hex_control.get(hex_id, "neutral")
        # Check if any units are present
        axis_there = [
            u.name for u in state.active_units_for_side(Side.AXIS)
            if u.hex_id == hex_id
        ]
        allied_there = [
            u.name for u in state.active_units_for_side(Side.ALLIED)
            if u.hex_id == hex_id
        ]
        unit_note = ""
        if axis_there:
            unit_note = f" [AX: {', '.join(axis_there[:2])}]"
        elif allied_there:
            unit_note = f" [AL: {', '.join(allied_there[:2])}]"
        lines.append(f"  {hex_id} {name}: {controller.upper()}{unit_note}")
    return "\n".join(lines)


def _build_unit_positions_by_region(state: GameState) -> str:
    region_map: dict[str, list[str]] = {label: [] for _, label in _REGIONS}

    def _add_units(units, side_label: str) -> None:
        for u in units:
            if not u.hex_id:
                continue
            col = _col_of(u.hex_id)
            region = _region_for_col(col)
            oos_flag = ""
            if u.id in state.supply_lines and not state.supply_lines[u.id].in_supply:
                oos_flag = " [OOS]"
            region_map[region].append(f"{side_label} {u.name} @ {u.hex_id}{oos_flag}")

    _add_units(state.active_units_for_side(Side.AXIS), "AX")
    _add_units(state.active_units_for_side(Side.ALLIED), "AL")

    lines = []
    for _, label in _REGIONS:
        entries = region_map[label]
        if entries:
            lines.append(f"  {label}:")
            for e in entries[:10]:  # Cap to keep prompt manageable
                lines.append(f"    • {e}")
            if len(entries) > 10:
                lines.append(f"    • ... and {len(entries) - 10} more")
    return "\n".join(lines) if lines else "  (no active units on map)"


def _build_supply_summary(state: GameState) -> str:
    lines = []
    for side in (Side.AXIS, Side.ALLIED):
        side_label = "AX" if side == Side.AXIS else "AL"
        depots = state.supply_depots_for_side(side)
        for d in depots[:8]:
            col = _col_of(d.hex_id) if d.hex_id else 0
            lines.append(
                f"  {side_label} {d.name}: {d.current_load:.0f}/{d.capacity:.0f} "
                f"@ {d.hex_id} ({_region_for_col(col).split('(')[0].strip()})"
            )
    if state.supply_report:
        sr = state.supply_report
        lines.append(f"  Fuel evaporated this turn: {sr.fuel_evaporated:.1f} pts")
    return "\n".join(lines) if lines else "  (no supply data)"


def _build_frontline_summary(state: GameState) -> str:
    axis_hexes = {
        _col_of(u.hex_id)
        for u in state.active_units_for_side(Side.AXIS)
        if u.hex_id
    }
    allied_hexes = {
        _col_of(u.hex_id)
        for u in state.active_units_for_side(Side.ALLIED)
        if u.hex_id
    }

    if not axis_hexes or not allied_hexes:
        return "  (insufficient data to determine frontline)"

    axis_east = max(axis_hexes)
    allied_west = min(allied_hexes)
    gap = axis_east - allied_west

    if gap >= 0:
        contact = f"Contact zone: cols {allied_west:02d}–{axis_east:02d} (forces interpenetrating)"
    else:
        contact = (
            f"Frontline gap: col {axis_east:02d} (Axis easternmost) "
            f"to col {allied_west:02d} (Allied westernmost), {abs(gap)} cols between lead elements"
        )

    axis_range = f"cols {min(axis_hexes):02d}–{axis_east:02d}"
    allied_range = f"cols {allied_west:02d}–{max(allied_hexes):02d}"
    return f"  Axis span: {axis_range}\n  Allied span: {allied_range}\n  {contact}"


def _active_cols_note(state: GameState) -> str:
    all_unit_cols = [
        _col_of(u.hex_id)
        for u in list(state.active_units_for_side(Side.AXIS)) +
                 list(state.active_units_for_side(Side.ALLIED))
        if u.hex_id
    ]
    if not all_unit_cols:
        return "no units placed"
    return f"cols {min(all_unit_cols):02d}–{max(all_unit_cols):02d} (where units are active)"


# ── Document parsing ───────────────────────────────────────────────────────────


def _parse_existing_doc(doc_text: str) -> tuple[str, list[tuple[int, str, str]]]:
    """
    Extract (current_situation, [(turn, date, entry_text), ...]).
    """
    situation = "(No prior board assessment — campaign just begun.)"
    entries: list[tuple[int, str, str]] = []

    m = re.search(
        r"## Current Board Situation\n.*?\n\n(.*?)(?=\n---|## Turn Log|\Z)",
        doc_text, re.DOTALL
    )
    if m:
        block = re.sub(r"\*Updated Turn \d+ — [^*]+\*\n\n", "", m.group(1))
        situation = block.strip()

    for em in re.finditer(
        r"### Turn (\d+) — ([^\n]+)\n\n(.*?)(?=\n### Turn|\Z)",
        doc_text, re.DOTALL
    ):
        entries.append((int(em.group(1)), em.group(2).strip(), em.group(3).strip()))

    return situation, entries


def _parse_response(text: str) -> tuple[str, str]:
    """Extract (situation, entry_text) from the model's response."""
    cleaned = re.sub(r"^(#{1,3}[^\n]*\n+)+", "", text.strip())

    situation_pat = re.compile(
        r"^(?:#{1,3}\s*)?BOARD SITUATION:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    entry_pat = re.compile(
        r"^(?:#{1,3}\s*)?TURN \d+ MAP ENTRY[^:\n]*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    s_match = situation_pat.search(cleaned)
    e_match = entry_pat.search(cleaned)

    if s_match and e_match:
        s_start = s_match.end()
        e_start = e_match.end()
        if s_start < e_start:
            situation = cleaned[s_start:e_match.start()].strip()
            entry = cleaned[e_start:].strip()
        else:
            entry = cleaned[e_start:s_match.start()].strip()
            situation = cleaned[s_start:].strip()
    elif s_match:
        situation = cleaned[s_match.end():].strip()
        entry = situation
    elif e_match:
        entry = cleaned[e_match.end():].strip()
        situation = entry
    else:
        parts = re.split(r"\n---+\n", cleaned, maxsplit=1)
        if len(parts) == 2:
            situation = parts[0].strip()
            entry = entry_pat.sub("", parts[1]).strip()
        else:
            half = len(cleaned) // 2
            situation = cleaned[:half].strip()
            entry = cleaned[half:].strip()

    return situation, entry


def _assemble_doc(
    turn: int,
    date: str,
    situation: str,
    all_entries: list[tuple[int, str, str]],
    total_hexes: int,
) -> str:
    log_md = "\n".join(
        _ENTRY_TEMPLATE.format(turn=t, date=d, text=txt)
        for t, d, txt in reversed(all_entries)
    )
    return _DOC_TEMPLATE.format(
        total_hexes=total_hexes,
        turn=turn,
        date=date,
        situation=situation,
        turn_log=log_md if log_md else "*(No entries recorded yet.)*",
    )


# ── Public API ─────────────────────────────────────────────────────────────────


def generate_board_report(state: GameState, client) -> str:
    """
    Generate a board state update.  Returns the full updated document text.
    Caller is responsible for writing it to disk.
    """
    date = turn_to_date_str(state.turn)
    total_hexes = len(state.map)

    existing = _BOARD_DOC_PATH.read_text(encoding="utf-8") if _BOARD_DOC_PATH.exists() else ""
    current_situation, all_entries = _parse_existing_doc(existing)

    # Hex control counts
    axis_hex_count = sum(1 for v in state.hex_control.values() if v == "axis")
    allied_hex_count = sum(1 for v in state.hex_control.values() if v == "allied")
    neutral_hex_count = total_hexes - axis_hex_count - allied_hex_count

    all_cols = [_col_of(h.hex_id) for h in state.map.all_hexes()]
    col_min = min(all_cols) if all_cols else 4
    col_max = max(all_cols) if all_cols else 22

    prompt = _UPDATE_PROMPT.format(
        turn=state.turn,
        date=date,
        total_hexes=total_hexes,
        col_min=f"{col_min:02d}",
        col_max=f"{col_max:02d}",
        active_cols_note=_active_cols_note(state),
        axis_hex_count=axis_hex_count,
        allied_hex_count=allied_hex_count,
        neutral_hex_count=neutral_hex_count,
        named_locations=_build_named_locations(state),
        unit_positions_by_region=_build_unit_positions_by_region(state),
        supply_summary=_build_supply_summary(state),
        frontline_summary=_build_frontline_summary(state),
    )

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=600,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    new_situation, entry_text = _parse_response(response_text)

    all_entries = [(t, d, txt) for t, d, txt in all_entries if t != state.turn]
    all_entries.append((state.turn, date, entry_text))

    return _assemble_doc(state.turn, date, new_situation, all_entries, total_hexes)


def generate_dry_run_board_report(state: GameState) -> str:
    """Fallback for --dry-run mode."""
    date = turn_to_date_str(state.turn)
    total_hexes = len(state.map)

    existing = _BOARD_DOC_PATH.read_text(encoding="utf-8") if _BOARD_DOC_PATH.exists() else ""
    current_situation, all_entries = _parse_existing_doc(existing)

    axis_hex_count = sum(1 for v in state.hex_control.values() if v == "axis")
    allied_hex_count = sum(1 for v in state.hex_control.values() if v == "allied")

    new_situation = (
        f"Turn {state.turn} ({date}): Map database holds {total_hexes} hexes "
        f"(cols 04–22).  Axis control: {axis_hex_count} hexes.  "
        f"Allied control: {allied_hex_count} hexes.  "
        + _build_frontline_summary(state).replace("  ", "").replace("\n", "  ")
        + "  [DRY RUN — no API call made.]"
    )
    entry_text = (
        f"[DRY RUN — Turn {state.turn}]  "
        + _build_frontline_summary(state).replace("  ", "").replace("\n", "  ")
    )

    all_entries = [(t, d, txt) for t, d, txt in all_entries if t != state.turn]
    all_entries.append((state.turn, date, entry_text))

    return _assemble_doc(state.turn, date, new_situation, all_entries, total_hexes)


def write_board_doc(content: str) -> Path:
    """Write the board state document to disk.  Returns the path."""
    path = _BOARD_DOC_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
