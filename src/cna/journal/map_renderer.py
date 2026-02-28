"""
ASCII hex-grid map renderer for CNA journal entries.

Renders the current game state as a fixed-width ASCII map suitable for
embedding in a markdown code block.  The hex grid uses a pointy-top
offset layout:

  Even-numbered columns sit at their natural row positions.
  Odd-numbered columns are shifted DOWN by half a hex (displayed between
  the even-column rows), which matches the adjacency data in hexes.json.

Each column occupies 3 display characters.  The result looks like:

    Col  09 10 11 12 13 14 15 16 17 18 19 20 21 22
    R01: P  P     P  ^  P  /  P  P  P  P  C     C
              P     P     P  P  P  P  P  P
    R02: .  .     .  .  .  /  .  .  .  .  .
           .     .     .  .  .  .  .  .

Symbol key
----------
A  Axis ground unit          L  Allied ground unit
D  Axis supply depot         d  Allied supply depot
P  Port (Axis ctrl)          p  Port (Allied ctrl)
C  City (Axis ctrl)          c  City (Allied ctrl)
O  Oasis
^  Hills / mountains
/  Escarpment
~  Impassable (sea/salt lake)
.  Axis-held open terrain
,  Allied-held open terrain
-  Neutral / uncontrolled
   (space) No hex here
"""

from __future__ import annotations

from ..models.game_state import GameState
from ..models.counter import Side, UnitStatus
from ..models.hex_map import Terrain


# ── column / row display range ────────────────────────────────────────────────
# We always render the full combat theater (Algeria → Cairo) regardless of
# where units happen to be this turn.
MIN_COL = 9
MAX_COL = 22
MIN_ROW = 0
MAX_ROW = 10

# Width of each column slot in the data portion of a display line.
# 3 chars: one for the symbol, two for spacing.
SLOT = 3

# ── terrain → single ASCII character ─────────────────────────────────────────
_TERRAIN_CHAR: dict[str, str] = {
    Terrain.PORT:               "P",
    Terrain.CITY:               "C",
    Terrain.OASIS:              "O",
    Terrain.HILLS:              "^",
    Terrain.HILLS_WOODED:       "^",
    Terrain.MOUNTAINS:          "^",
    Terrain.MOUNTAINS_WOODED:   "^",
    Terrain.ROUGH_ESCARPMENT:   "/",
    Terrain.ESCARPMENT_EDGE:    "/",
    Terrain.SALT_LAKE:          "~",
    Terrain.SEA:                "~",
    Terrain.FLAT_DESERT_ROCKY:  ":",
    Terrain.FLAT_DESERT_SANDY:  ",",
    Terrain.FLAT_FARMLAND:      "f",
    Terrain.CULTIVATED:         "f",
}
_DEFAULT_TERRAIN_CHAR = "."


def _terrain_char(terrain: Terrain, ctrl: str) -> str:
    """Return the base terrain symbol, adjusted for controller."""
    base = _TERRAIN_CHAR.get(terrain, _DEFAULT_TERRAIN_CHAR)
    # Ports and cities change case with controller.
    if terrain in (Terrain.PORT, Terrain.CITY):
        return base if ctrl == "axis" else base.lower()
    # Open terrain: '.' = axis, ',' = allied, '-' = neutral.
    if base == ".":
        if ctrl == "axis":
            return "."
        if ctrl == "allied":
            return ","
        return "-"
    return base


def _build_unit_index(state: GameState) -> dict[str, str]:
    """Map hex_id → display symbol for active ground units and supply depots."""
    index: dict[str, str] = {}

    # Ground units (combat units take priority over depots)
    for unit in state.ground_units.values():
        if unit.hex_id and unit.status != UnitStatus.ELIMINATED:
            sym = "A" if unit.side == Side.AXIS else "L"
            index[unit.hex_id] = sym

    # Supply depots (only where no combat unit already shown)
    for depot in state.supply_counters.values():
        if depot.hex_id and depot.hex_id not in index:
            sym = "D" if depot.side == Side.AXIS else "d"
            index[depot.hex_id] = sym

    return index


def _hex_symbol(hex_id: str, state: GameState, unit_index: dict[str, str]) -> str:
    """
    Return the single character that represents this hex.
    Priority: unit/depot > impassable terrain > named feature > terrain+control.
    Returns ' ' if the hex does not exist in the map.
    """
    h = state.map.get(hex_id)
    if h is None:
        return " "

    if h.is_impassable():
        return "~"

    if hex_id in unit_index:
        return unit_index[hex_id]

    ctrl = state.hex_control.get(hex_id, "")
    return _terrain_char(h.terrain, ctrl)


def _col_header_lines(min_col: int, max_col: int) -> tuple[str, str]:
    """
    Build two header lines showing column numbers: even cols on line A,
    odd cols on line B (shifted right one char).

    Returns (line_a, line_b).
    """
    n_cols = max_col - min_col + 1
    data_width = n_cols * SLOT

    # Row-label prefix placeholder (spaces matching "R00: ")
    prefix = "     "

    a_chars = [" "] * data_width
    b_chars = [" "] * (data_width + 1)   # +1 for the 1-char odd shift

    for col in range(min_col, max_col + 1):
        label = f"{col:02d}"
        base = (col - min_col) * SLOT
        if col % 2 == 0:                  # even col → A line
            if base + 1 < data_width:
                a_chars[base]     = label[0]
                a_chars[base + 1] = label[1]
        else:                             # odd col → B line (1-char right shift)
            pos = base + 1
            if pos + 1 < len(b_chars):
                b_chars[pos]     = label[0]
                b_chars[pos + 1] = label[1]

    return prefix + "".join(a_chars).rstrip(), prefix + " " + "".join(b_chars).rstrip()


def _data_line(row: int, odd_cols: bool, state: GameState,
               unit_index: dict[str, str],
               min_col: int, max_col: int) -> str:
    """
    Build the data portion of one display line for the given row.

    odd_cols=False → place even-column symbols at their natural positions.
    odd_cols=True  → place odd-column symbols shifted one char to the right.
    """
    n_cols = max_col - min_col + 1
    data_width = n_cols * SLOT + 1  # +1 to accommodate the 1-char odd shift
    chars = [" "] * data_width

    for col in range(min_col, max_col + 1):
        is_odd = (col % 2 == 1)
        if is_odd != odd_cols:
            continue

        hex_id = f"{col:02d}{row:02d}"
        sym = _hex_symbol(hex_id, state, unit_index)

        base = (col - min_col) * SLOT
        pos = base + 1 if odd_cols else base
        if 0 <= pos < data_width:
            chars[pos] = sym

    return "".join(chars).rstrip()


def render_ascii_map(state: GameState) -> str:
    """
    Render the entire current game state as an ASCII hex map.
    Returns a markdown fenced code block string.
    """
    unit_index = _build_unit_index(state)

    lines: list[str] = []

    # Column headers
    hdr_a, hdr_b = _col_header_lines(MIN_COL, MAX_COL)
    lines.append(hdr_a)
    lines.append(hdr_b)
    lines.append("")

    for row in range(MIN_ROW, MAX_ROW + 1):
        # Even-column line (row label "R00: ")
        even_data = _data_line(row, False, state, unit_index, MIN_COL, MAX_COL)
        odd_data  = _data_line(row, True,  state, unit_index, MIN_COL, MAX_COL)

        even_line = f"R{row:02d}: {even_data}"
        odd_line  = f"      {odd_data}"

        # Only emit lines that have content beyond the prefix.
        if even_data.strip():
            lines.append(even_line)
        if odd_data.strip():
            lines.append(odd_line)
        # If neither line has content, skip the row entirely (no hexes here).

    lines.append("")
    lines.append(
        "A=Axis  L=Allied  D=Axis depot  d=Allied depot  "
        "P/p=port  C/c=city  O=oasis"
    )
    lines.append(
        ".=Axis hex  ,=Allied hex  -=neutral  "
        "^=hills/mtn  /=escarp  ~=impass  :=rocky  f=farmland"
    )

    map_text = "\n".join(lines)
    return f"```\n{map_text}\n```"
