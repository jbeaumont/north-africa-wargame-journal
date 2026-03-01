"""
Hex model — a single map hex in CNA's pointy-top (sideways) hex grid.

Coordinate system
-----------------
Format: SCCRR  (5 characters)
  S  = map section letter A–E  (A=Tunisia/Tripolitania, B=Cyrenaica/Tobruk,
                                  C=Egypt border, D=Egypt, E=deep Egypt/Nile)
  CC = column 01–60  (increases west → east within a section)
  RR = row    01–33  (increases north → south)

Hex grid geometry (from buildFile.xml)
---------------------------------------
  sideways = True  (pointy-top hexes, columns run N–S)
  dx = 72.95 px/col,  dy = 85.25 px/row
  Even columns are offset +dy/2 southward relative to odd columns.

Neighbour offsets (col parity)
--------------------------------
  odd column  → N  (0,-1), NE (+1,-1), SE (+1, 0), S  (0,+1), SW (-1, 0), NW (-1,-1)
  even column → N  (0,-1), NE (+1, 0), SE (+1,+1), S  (0,+1), SW (-1,+1), NW (-1, 0)

These are the offsets used by neighbors() and by the engine's BFS supply tracer.

Terrain and hexside features
-----------------------------
Each hex has one primary Terrain type.  Hexside features (escarpment, wadi,
road, etc.) are stored per direction as HexsideFeature values.  See the
Terrain Effects Chart (rules_tables.json, rule 8.37) for movement costs.

Dynamic state (minefields, current fortification level) is stored in GameState,
not in Hex, so that it can change during the game without touching the base map.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Terrain types (primary hex terrain) ──────────────────────────────────────

class Terrain(str, Enum):
    CLEAR = "Clear"
    GRAVEL = "Gravel"
    SALT_MARSH = "Salt Marsh"
    HEAVY_VEGETATION = "Heavy Vegetation"
    ROUGH = "Rough"
    MOUNTAIN = "Mountain"
    DELTA = "Delta"
    DESERT = "Desert"
    MAJOR_CITY = "Major City"
    SWAMP = "Swamp"
    VILLAGE = "Village/Bir/Oasis"


# ── Hexside features ──────────────────────────────────────────────────────────

class HexsideFeature(str, Enum):
    NONE = "none"
    RIDGE = "ridge"
    SLOPE_UP = "slope_up"
    SLOPE_DOWN = "slope_down"
    ESCARPMENT_UP = "escarpment_up"       # vehicles may NEVER cross up (8.42)
    ESCARPMENT_DOWN = "escarpment_down"   # +6 CP non-mot, +3 CP mot (via track: +8 CP)
    WADI = "wadi"
    MAJOR_RIVER = "major_river"           # impassable except via road/railroad
    MINOR_RIVER = "minor_river"           # +2 CP; impassable in Rainstorm w/o road
    ROAD = "road"                         # reduces terrain cost by 1 (or -1 to hexside cost)
    RAILROAD = "railroad"                 # rail movement (8.7); +10 BD leaving rail
    TRACK = "track"                       # halves terrain costs; allows escarpment descent


DIRECTIONS = ("N", "NE", "SE", "S", "SW", "NW")

# Neighbour col/row deltas indexed by direction order above
_ODD_DELTAS = ((0, -1), (1, -1), (1, 0), (0, 1), (-1, 0), (-1, -1))
_EVEN_DELTAS = ((0, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0))

_SECTION_ORDER = "ABCDE"
_MAX_COL = 60
_MAX_ROW = 33


@dataclass
class Hex:
    # ── Identity ─────────────────────────────────────────────────────────────
    hex_id: str         # e.g. "C4807"
    section: str        # single letter A–E
    col: int            # 1–60
    row: int            # 1–33

    # ── Terrain ──────────────────────────────────────────────────────────────
    terrain: Terrain = Terrain.DESERT

    # Hexside features: maps direction string → HexsideFeature.
    # Directions not listed default to HexsideFeature.NONE.
    # e.g. {"N": HexsideFeature.ESCARPMENT_UP, "SE": HexsideFeature.WADI}
    hexsides: dict[str, HexsideFeature] = field(default_factory=dict)

    # ── Infrastructure ───────────────────────────────────────────────────────
    has_road: bool = False
    has_railroad: bool = False
    has_track: bool = False

    # ── Named locations ──────────────────────────────────────────────────────
    location_name: Optional[str] = None
    is_port: bool = False
    port_efficiency: int = 0    # supply tonnage per turn when port operational

    # ── Water ────────────────────────────────────────────────────────────────
    has_water_source: bool = False
    water_capacity: int = 0     # gallons available per OpStage

    # ── Fortification baseline ───────────────────────────────────────────────
    # This is the level at scenario start (from construction_state).
    # The *current* level during the game lives in GameState.fortifications.
    base_fortification_level: int = 0

    # ── Neighbour calculation ────────────────────────────────────────────────

    def hexside(self, direction: str) -> HexsideFeature:
        """Return the hexside feature for a given direction (default NONE)."""
        return self.hexsides.get(direction, HexsideFeature.NONE)

    def neighbors(self) -> list[str]:
        """
        Return the hex_ids of the up-to-6 neighbours of this hex.

        Neighbours that fall off the map edge (col < 1, col > 60, row out of
        range, or section out of A–E) are omitted.  Neighbours that cross
        section boundaries (col wraps from 1→60 or 60→1 in the adjacent
        section) are handled correctly.
        """
        deltas = _ODD_DELTAS if self.col % 2 == 1 else _EVEN_DELTAS
        sec_idx = _SECTION_ORDER.index(self.section)
        result: list[str] = []

        for dc, dr in deltas:
            new_col = self.col + dc
            new_row = self.row + dr
            new_sec_idx = sec_idx

            # Handle section boundary crossings
            if new_col < 1:
                new_sec_idx -= 1
                if new_sec_idx < 0:
                    continue            # off west edge of map
                new_col = _MAX_COL      # rightmost column of the western section
            elif new_col > _MAX_COL:
                new_sec_idx += 1
                if new_sec_idx >= len(_SECTION_ORDER):
                    continue            # off east edge of map
                new_col = 1             # leftmost column of the eastern section

            if new_row < 1 or new_row > _MAX_ROW:
                continue                # off north / south edge

            new_section = _SECTION_ORDER[new_sec_idx]
            result.append(f"{new_section}{new_col:02d}{new_row:02d}")

        return result

    def distance_to(self, other: Hex) -> int:
        """
        Hex distance using offset→cube coordinate conversion.
        Correct for the CNA sideways (pointy-top) grid.
        """
        def to_cube(h: Hex) -> tuple[int, int, int]:
            # Convert offset coords to cube coords for pointy-top grid
            # (col parity shifts the row offset)
            q = h.col
            r = h.row - (h.col - (h.col & 1)) // 2
            return q, r, -q - r

        q1, r1, s1 = to_cube(self)
        q2, r2, s2 = to_cube(other)
        return max(abs(q1 - q2), abs(r1 - r2), abs(s1 - s2))

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "hex_id": self.hex_id,
            "section": self.section,
            "col": self.col,
            "row": self.row,
            "terrain": self.terrain.value,
            "hexsides": {k: v.value for k, v in self.hexsides.items()},
            "has_road": self.has_road,
            "has_railroad": self.has_railroad,
            "has_track": self.has_track,
            "location_name": self.location_name,
            "is_port": self.is_port,
            "port_efficiency": self.port_efficiency,
            "has_water_source": self.has_water_source,
            "water_capacity": self.water_capacity,
            "base_fortification_level": self.base_fortification_level,
        }

    @classmethod
    def from_id(cls, hex_id: str) -> Hex:
        """Create a minimal desert hex from just a hex_id string."""
        section = hex_id[0]
        col = int(hex_id[1:3])
        row = int(hex_id[3:5])
        return cls(hex_id=hex_id, section=section, col=col, row=row)

    @classmethod
    def from_dict(cls, d: dict) -> Hex:
        return cls(
            hex_id=d["hex_id"],
            section=d["section"],
            col=d["col"],
            row=d["row"],
            terrain=Terrain(d.get("terrain", "Desert")),
            hexsides={
                k: HexsideFeature(v)
                for k, v in d.get("hexsides", {}).items()
            },
            has_road=d.get("has_road", False),
            has_railroad=d.get("has_railroad", False),
            has_track=d.get("has_track", False),
            location_name=d.get("location_name"),
            is_port=d.get("is_port", False),
            port_efficiency=d.get("port_efficiency", 0),
            has_water_source=d.get("has_water_source", False),
            water_capacity=d.get("water_capacity", 0),
            base_fortification_level=d.get("base_fortification_level", 0),
        )
