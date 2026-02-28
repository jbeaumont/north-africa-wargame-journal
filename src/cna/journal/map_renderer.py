"""
SVG hex-grid map renderer for CNA journal entries.

Generates a flat-top hex-grid map styled like a 1970s SPI wargame:
muted sandy palette, bold rule-box typography, unit counters as small
coloured rectangles with nationality abbreviations.

Hex geometry (flat-top orientation, confirmed by adjacency data):
  - Center-to-vertex radius R
  - Width  = 2R  (left vertex to right vertex)
  - Height = R√3 (flat top edge to flat bottom edge)
  - Column spacing = 1.5R  (horizontal, center to center)
  - Row spacing    = R√3   (vertical, within-column center to center)
  - Odd columns are offset downward by R√3/2 (half a row)

Vertices of a flat-top hex centered at (cx, cy):
  angle = 0°, 60°, 120°, 180°, 240°, 300° from positive-X axis
  (giving right, upper-right, upper-left, left, lower-left, lower-right)
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Optional

from ..models.game_state import GameState, turn_to_date_str
from ..models.counter import Side, UnitStatus, Nationality
from ..models.hex_map import Terrain


# ── Layout ────────────────────────────────────────────────────────────────────

R: float = 19.0           # hex radius (center to vertex), px

MIN_COL = 9
MAX_COL = 22
MIN_ROW = 0
MAX_ROW = 10

COL_SPACING = R * 1.5
ROW_SPACING = R * math.sqrt(3)
ODD_OFFSET  = ROW_SPACING / 2   # odd columns shift down this much

MARGIN_LEFT   = 38.0
MARGIN_TOP    = 64.0
MARGIN_BOTTOM = 100.0
MARGIN_RIGHT  = 24.0


# ── 1970s wargame colour palette ─────────────────────────────────────────────

TERRAIN_FILL: dict[str, str] = {
    # Blues
    "sea":                  "#7ab0cc",
    "salt_lake":            "#7ab0cc",
    # Sandy desert
    "flat_desert_coastal":  "#ddc98a",
    "flat_desert_rocky":    "#c4aa70",
    "flat_desert_sandy":    "#e8d8a0",
    "coastal":              "#d0c890",
    # Rough
    "rough_escarpment":     "#b88840",
    "escarpment_edge":      "#c89848",
    "rough_rocky":          "#c09a60",
    "rough_wadis":          "#b88858",
    "rough_broken":         "#c09060",
    "dunes":                "#e0cca0",
    # Elevated
    "hills":                "#a88858",
    "hills_wooded":         "#7a8448",
    "mountains":            "#907050",
    "mountains_wooded":     "#6a6040",
    "plateau":              "#b09868",
    "ridge":                "#c0a870",
    # Wet / agricultural
    "flat_farmland":        "#98b068",
    "cultivated":           "#a0b470",
    "flat_marsh":           "#78a060",
    "flat_mud":             "#907868",
    "flat_swamp":           "#607850",
    "scrub":                "#b0a868",
    "oasis":                "#70a060",
    "wadi":                 "#b09060",
    "ford":                 "#90b0a0",
    # Urban / infrastructure
    "port":                 "#c8d8b0",
    "city":                 "#c8c8b0",
    "airfield":             "#c0c0b0",
    # Roads (not used as hex terrain but included for safety)
    "road":                 "#ddc98a",
    "track":                "#ddc98a",
}
TERRAIN_FILL_DEFAULT = "#d4c090"

GRID_STROKE      = "#7a5c2c"
GRID_STROKE_W    = 0.7
AXIS_CTRL_STROKE = "#c04820"   # warm orange-red border on Axis hexes
ALLY_CTRL_STROKE = "#2848a0"   # blue border on Allied hexes
CTRL_STROKE_W    = 1.2

BG_COLOR    = "#c8b880"        # parchment
BORDER_COL  = "#4a3010"
TITLE_BG    = "#3a2808"
TITLE_FG    = "#f0e0b0"
TITLE_SUB   = "#c0a878"

# Nationality counter colours: (fill, text)
NAT_COLOR: dict[str, tuple[str, str]] = {
    "IT": ("#909080", "#ffffff"),   # Italian — slate gray
    "GE": ("#3c5030", "#ffffff"),   # German  — field green
    "GB": ("#8c2820", "#ffffff"),   # British — red
    "CW": ("#6c1c10", "#ffffff"),   # Commonwealth — dark red
    "US": ("#203860", "#ffffff"),   # American — navy
    "FF": ("#285878", "#ffffff"),   # Free French — blue
}
DEPOT_COLOR = ("#2c3c5c", "#ffffff")   # supply depot — dark blue


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _cx(col: int) -> float:
    return MARGIN_LEFT + (col - MIN_COL) * COL_SPACING


def _cy(row: int, col: int) -> float:
    offset = ODD_OFFSET if (col % 2 == 1) else 0.0
    return MARGIN_TOP + row * ROW_SPACING + offset


def _hex_points(cx: float, cy: float) -> str:
    """SVG points string for a flat-top regular hexagon."""
    pts = []
    for i in range(6):
        a = math.radians(60 * i)
        pts.append(f"{cx + R * math.cos(a):.2f},{cy + R * math.sin(a):.2f}")
    return " ".join(pts)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _unit_abbrev(unit) -> str:
    """Return a ≤4-char counter label for a ground unit."""
    name = unit.name
    m = re.search(r"\b(\d+)\b", name)
    num = m.group(1) if m else ""

    ut = unit.unit_type.value
    if "headquarters" in ut:
        t = "HQ"
    elif "recon" in ut:
        t = "Rec"
    elif "armor" in ut or "armour" in ut or "armored" in ut:
        t = "Arm"
    elif "artillery" in ut:
        t = "Art"
    elif "engineer" in ut:
        t = "Eng"
    elif "anti_tank" in ut:
        t = "AT"
    elif "anti_aircraft" in ut:
        t = "AA"
    elif "motorized" in ut or "mechanized" in ut:
        t = "Mot"
    elif "infantry" in ut:
        t = "Inf"
    else:
        t = "?"

    if num and t != "HQ":
        label = f"{num}{t}"
    elif num:
        label = f"{num}HQ"
    else:
        words = [w for w in name.split() if w[0].isupper()]
        label = "".join(w[0] for w in words[:3]) or "??"

    return label[:5]


# ── SVG builder ───────────────────────────────────────────────────────────────

def _svg(state: GameState, turn: int) -> str:
    # ── canvas size ───────────────────────────────────────────────────────────
    right_edge = _cx(MAX_COL) + R + MARGIN_RIGHT
    # Lowest possible hex centre: row MAX_ROW in an odd column
    bottom_edge = _cy(MAX_ROW, 1) + ODD_OFFSET + MARGIN_BOTTOM
    W = math.ceil(right_edge)
    H = math.ceil(bottom_edge)

    out: list[str] = []
    a = out.append   # shorthand

    a(f'<svg xmlns="http://www.w3.org/2000/svg" '
      f'width="{W}" height="{H}" '
      f'font-family="Arial Narrow,Arial,Helvetica,sans-serif">')

    # ── defs: clip path for map area ──────────────────────────────────────────
    mx = _cx(MIN_COL) - R - 1
    my = _cy(MIN_ROW, MIN_COL) - ROW_SPACING / 2 - 1
    mw = right_edge - mx + 2
    mh = _cy(MAX_ROW, 1) + ODD_OFFSET - my + 2
    a('<defs>')
    a(f'  <clipPath id="mc">'
      f'<rect x="{mx:.1f}" y="{my:.1f}" width="{mw:.1f}" height="{mh:.1f}"/>'
      f'</clipPath>')
    a('</defs>')

    # ── background ────────────────────────────────────────────────────────────
    a(f'<rect width="{W}" height="{H}" fill="{BG_COLOR}"/>')

    # ── title bar ─────────────────────────────────────────────────────────────
    a(f'<rect x="0" y="0" width="{W}" height="50" fill="{TITLE_BG}"/>')
    a(f'<text x="{W//2}" y="26" text-anchor="middle" '
      f'font-size="15" font-weight="bold" letter-spacing="4" fill="{TITLE_FG}">'
      f'CAMPAIGN FOR NORTH AFRICA</text>')
    date_line = f"Turn {turn} of 100  ·  Week of {turn_to_date_str(turn)}"
    a(f'<text x="{W//2}" y="43" text-anchor="middle" '
      f'font-size="10" fill="{TITLE_SUB}">{_esc(date_line)}</text>')

    # ── build index: hex_id → [unit, …] and → [depot, …] ────────────────────
    hex_units: dict[str, list] = {}
    hex_depots: dict[str, list] = {}

    for unit in state.ground_units.values():
        if (unit.hex_id
                and unit.status != UnitStatus.ELIMINATED
                and unit.available_turn <= state.turn):
            hex_units.setdefault(unit.hex_id, []).append(unit)

    for depot in state.supply_counters.values():
        if depot.hex_id:
            hex_depots.setdefault(depot.hex_id, []).append(depot)

    # ── hex tiles ─────────────────────────────────────────────────────────────
    a('<g id="tiles" clip-path="url(#mc)">')
    for col in range(MIN_COL, MAX_COL + 1):
        for row in range(MIN_ROW, MAX_ROW + 1):
            hid = f"{col:02d}{row:02d}"
            h = state.map.get(hid)
            if h is None:
                continue

            cx, cy = _cx(col), _cy(row, col)
            pts = _hex_points(cx, cy)

            fill = (TERRAIN_FILL.get("sea")
                    if h.is_impassable()
                    else TERRAIN_FILL.get(h.terrain.value, TERRAIN_FILL_DEFAULT))

            ctrl = state.hex_control.get(hid, "")
            if ctrl == "axis":
                stroke, sw = AXIS_CTRL_STROKE, CTRL_STROKE_W
            elif ctrl == "allied":
                stroke, sw = ALLY_CTRL_STROKE, CTRL_STROKE_W
            else:
                stroke, sw = GRID_STROKE, GRID_STROKE_W

            a(f'  <polygon points="{pts}" fill="{fill}" '
              f'stroke="{stroke}" stroke-width="{sw}"/>')

            # ── terrain feature icons ──────────────────────────────────────
            if not h.is_impassable() and hid not in hex_units:
                # Port pip
                if h.is_port:
                    a(f'  <circle cx="{cx:.1f}" cy="{cy - R*0.3:.1f}" r="3" '
                      f'fill="#b02010" stroke="#701008" stroke-width="0.5"/>')
                # Airfield cross (when no port)
                if h.has_airfield and not h.is_port:
                    aw = 4.0
                    a(f'  <line x1="{cx-aw:.1f}" y1="{cy:.1f}" '
                      f'x2="{cx+aw:.1f}" y2="{cy:.1f}" '
                      f'stroke="#404040" stroke-width="1.2"/>')
                    a(f'  <line x1="{cx:.1f}" y1="{cy-aw:.1f}" '
                      f'x2="{cx:.1f}" y2="{cy+aw:.1f}" '
                      f'stroke="#404040" stroke-width="1.2"/>')
                # Water well (oasis / water source, not a port)
                if h.has_water_source and not h.is_port:
                    a(f'  <circle cx="{cx:.1f}" cy="{cy:.1f}" r="2" '
                      f'fill="#4088b8" stroke="#205070" stroke-width="0.5"/>')

    a('</g>')

    # ── location names ────────────────────────────────────────────────────────
    a('<g id="names" clip-path="url(#mc)" '
      'font-size="5.5" fill="#2a200a" text-anchor="middle">')
    for col in range(MIN_COL, MAX_COL + 1):
        for row in range(MIN_ROW, MAX_ROW + 1):
            hid = f"{col:02d}{row:02d}"
            h = state.map.get(hid)
            if h and h.location_name:
                cx, cy = _cx(col), _cy(row, col)
                # Place name at bottom of hex so it doesn't clash with counters
                a(f'  <text x="{cx:.1f}" y="{cy + R*0.72:.1f}">'
                  f'{_esc(h.location_name)}</text>')
    a('</g>')

    # ── supply depots ─────────────────────────────────────────────────────────
    a('<g id="depots" clip-path="url(#mc)">')
    for hid, depots in hex_depots.items():
        if hid in hex_units:
            continue
        col, row = int(hid[:2]), int(hid[2:])
        if not (MIN_COL <= col <= MAX_COL and MIN_ROW <= row <= MAX_ROW):
            continue
        cx, cy = _cx(col), _cy(row, col)
        fill, txt = DEPOT_COLOR
        dw, dh = 16, 9
        a(f'  <rect x="{cx-dw/2:.1f}" y="{cy-dh/2:.1f}" '
          f'width="{dw}" height="{dh}" rx="1.5" '
          f'fill="{fill}" stroke="#1a1a3a" stroke-width="0.8"/>')
        a(f'  <text x="{cx:.1f}" y="{cy+3:.1f}" text-anchor="middle" '
          f'font-size="5.5" font-weight="bold" fill="{txt}">SUP</text>')
    a('</g>')

    # ── unit counters ─────────────────────────────────────────────────────────
    a('<g id="units" clip-path="url(#mc)">')
    cw, ch = 20, 13   # counter width × height

    for hid, units in hex_units.items():
        col, row = int(hid[:2]), int(hid[2:])
        if not (MIN_COL <= col <= MAX_COL and MIN_ROW <= row <= MAX_ROW):
            continue
        cx, cy = _cx(col), _cy(row, col)

        for i, unit in enumerate(units[:3]):
            nat = unit.nationality.value
            fill, txt = NAT_COLOR.get(nat, ("#707070", "#ffffff"))
            ux = cx - cw / 2 + i * 2.5
            uy = cy - ch / 2 - i * 2.5

            # Drop shadow
            a(f'  <rect x="{ux+1.5:.1f}" y="{uy+1.5:.1f}" '
              f'width="{cw}" height="{ch}" rx="1.5" fill="#00000050"/>')
            # Counter body
            a(f'  <rect x="{ux:.1f}" y="{uy:.1f}" '
              f'width="{cw}" height="{ch}" rx="1.5" '
              f'fill="{fill}" stroke="#1a1a1a" stroke-width="1"/>')
            # Inner highlight (classic wargame bevelled look)
            a(f'  <rect x="{ux+1.5:.1f}" y="{uy+1.5:.1f}" '
              f'width="{cw-3}" height="{ch-3}" rx="0.5" '
              f'fill="none" stroke="#ffffff50" stroke-width="0.6"/>')
            # Label
            label = _unit_abbrev(unit)
            a(f'  <text x="{ux+cw/2:.1f}" y="{uy+ch/2+2.5:.1f}" '
              f'text-anchor="middle" font-size="6" font-weight="bold" '
              f'fill="{txt}">{_esc(label)}</text>')

        if len(units) > 3:
            a(f'  <text x="{cx+cw/2+2:.1f}" y="{cy:.1f}" '
              f'font-size="8" fill="#303020">+{len(units)-3}</text>')
    a('</g>')

    # ── outer map frame ───────────────────────────────────────────────────────
    a(f'<rect x="{mx:.1f}" y="{my:.1f}" width="{mw:.1f}" height="{mh:.1f}" '
      f'fill="none" stroke="{BORDER_COL}" stroke-width="2"/>')

    # ── legend ────────────────────────────────────────────────────────────────
    ly = H - MARGIN_BOTTOM + 10
    lx = MARGIN_LEFT

    a(f'<text x="{lx}" y="{ly}" font-size="8" font-weight="bold" '
      f'letter-spacing="2" fill="{BORDER_COL}">LEGEND</text>')
    ly += 12

    # Unit nationality swatches
    nats = [("IT", "Italian"), ("GE", "German"), ("GB", "British"),
            ("CW", "Cmwlth"), ("US", "American")]
    nx = lx
    for nat, label in nats:
        fill, txt = NAT_COLOR[nat]
        a(f'<rect x="{nx}" y="{ly-8}" width="18" height="11" rx="1.5" '
          f'fill="{fill}" stroke="#1a1a1a" stroke-width="0.8"/>')
        a(f'<text x="{nx+21}" y="{ly}" font-size="8" fill="#302010">{label}</text>')
        nx += 70
    ly += 14

    # Terrain swatches
    terrain_leg = [
        ("flat_desert_coastal", "Desert"),
        ("hills",               "Hills"),
        ("rough_escarpment",    "Escarpment"),
        ("flat_farmland",       "Farmland"),
        ("sea",                 "Sea/Salt Lake"),
        ("oasis",               "Oasis"),
    ]
    tx = lx
    for key, label in terrain_leg:
        col_val = TERRAIN_FILL.get(key, TERRAIN_FILL_DEFAULT)
        a(f'<rect x="{tx}" y="{ly-8}" width="14" height="10" '
          f'fill="{col_val}" stroke="{GRID_STROKE}" stroke-width="0.6"/>')
        a(f'<text x="{tx+17}" y="{ly}" font-size="8" fill="#302010">{label}</text>')
        tx += 70
    ly += 13

    # Control note
    a(f'<rect x="{lx}" y="{ly-8}" width="14" height="10" '
      f'fill="{TERRAIN_FILL_DEFAULT}" '
      f'stroke="{AXIS_CTRL_STROKE}" stroke-width="{CTRL_STROKE_W}"/>')
    a(f'<text x="{lx+17}" y="{ly}" font-size="8" fill="#302010">Axis hex</text>')
    a(f'<rect x="{lx+70}" y="{ly-8}" width="14" height="10" '
      f'fill="{TERRAIN_FILL_DEFAULT}" '
      f'stroke="{ALLY_CTRL_STROKE}" stroke-width="{CTRL_STROKE_W}"/>')
    a(f'<text x="{lx+87}" y="{ly}" font-size="8" fill="#302010">Allied hex</text>')

    # Red pip = port, blue dot = water
    a(f'<circle cx="{lx+160}" cy="{ly-3}" r="3" fill="#b02010" stroke="#701008" stroke-width="0.5"/>')
    a(f'<text x="{lx+166}" y="{ly}" font-size="8" fill="#302010">Port</text>')
    a(f'<circle cx="{lx+210}" cy="{ly-3}" r="2" fill="#4088b8" stroke="#205070" stroke-width="0.5"/>')
    a(f'<text x="{lx+215}" y="{ly}" font-size="8" fill="#302010">Water</text>')

    a('</svg>')
    return "\n".join(out)


# ── public API ────────────────────────────────────────────────────────────────

def render_map(state: GameState, turn: int, journal_dir: Path) -> str:
    """
    Generate a 1970s-style SVG wargame map and save it to journal_dir.
    Returns a markdown image reference string for embedding in the journal entry.
    """
    svg_text = _svg(state, turn)
    filename = f"turn_{turn:03d}_map.svg"
    (journal_dir / filename).write_text(svg_text, encoding="utf-8")
    return f"![Turn {turn} map]({filename})"
