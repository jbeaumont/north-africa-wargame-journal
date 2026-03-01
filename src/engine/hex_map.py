"""
HexMap — movement cost and ZOC computation over the CNA hex grid.

Usage
-----
    import json
    from src.engine.hex_map import HexMap

    with open("data/extracted/rules_tables.json") as f:
        rules = json.load(f)
    tec = rules["terrain_effects_chart"]

    hmap = HexMap(game_state.hexes, tec)

    # Movement cost for a motorized unit entering C4808 from C4807
    cost = hmap.entry_cost(unit, "C4807", "C4808")   # float or "P"

    # Breakdown points from that move
    bd = hmap.entry_bd(unit, "C4807", "C4808")

    # All hexes under Axis ZOC
    axis_zoc = hmap.zoc_hexes(Side.AXIS, game_state.units.values())

Design notes
------------
Entry cost algorithm (rule 8.37):
  1. Base terrain CP cost for the hex being ENTERED.
  2. Add hexside feature CP modifier for the hexside crossed.
  3. ROAD modifier: base cost –1, hexside costs zeroed (note 6).
  4. TRACK modifier: CP unchanged; only BD is halved (note 7).
  5. Minimum final cost = 1.0 CP.

Return values from entry_cost():
  float         — CP cost (≥ 1.0)
  "P"           — prohibited for all units (or for this unit's motorization)

Minefields and fortification levels affect combat column shifts only, not
movement CP.  The combat engine reads fortification_level from GameState.

Up-escarpment (rule 8.42): no vehicle may ever cross upward.  Non-motorized
(foot) units may; we approximate at 8 CP (the value given in rule 8.42 note).

Track note 7: track halves BD but does NOT halve CP.  "except CP expended"
in the note means the CP itself is unchanged.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Union

from src.models.hex import Hex, HexsideFeature, Terrain
from src.models.unit import Unit, Side, UnitType


# ── Grid constants ────────────────────────────────────────────────────────────
# Duplicated from hex.py to avoid importing private names.

_DIRECTIONS = ("N", "NE", "SE", "S", "SW", "NW")

# (delta_col, delta_row) for each direction, by source-column parity
_ODD_COL_DELTAS  = ((0, -1), (+1, -1), (+1,  0), (0, +1), (-1,  0), (-1, -1))
_EVEN_COL_DELTAS = ((0, -1), (+1,  0), (+1, +1), (0, +1), (-1, +1), (-1,  0))

_SECTION_ORDER = "ABCDE"
_MAX_COL = 60
_MAX_ROW = 33

_OPPOSITE: Dict[str, str] = {
    "N": "S", "S": "N",
    "NE": "SW", "SW": "NE",
    "SE": "NW", "NW": "SE",
}


# ── ZOC ───────────────────────────────────────────────────────────────────────

# Unit types that project Zone of Control into adjacent hexes.
_ZOC_TYPES = frozenset({
    UnitType.INFANTRY,
    UnitType.ARMOR,
    UnitType.ARTILLERY,
    UnitType.ANTI_TANK,
    UnitType.ANTI_AIRCRAFT,
    UnitType.RECONNAISSANCE,
    UnitType.GARRISON,
})


# ── TEC key mapping ───────────────────────────────────────────────────────────

# Maps HexsideFeature → key in the TEC terrain_types dict.
# ROAD and TRACK are handled as modifiers, not as additive costs.
_HEXSIDE_TEC_KEY: Dict[HexsideFeature, Optional[str]] = {
    HexsideFeature.NONE:            None,
    HexsideFeature.RIDGE:           "Ridge_hexside",
    HexsideFeature.SLOPE_UP:        "Up_Slope_hexside",
    HexsideFeature.SLOPE_DOWN:      "Down_Slope_hexside",
    HexsideFeature.ESCARPMENT_UP:   "Up_Escarpment_hexside",
    HexsideFeature.ESCARPMENT_DOWN: "Down_Escarpment_hexside",
    HexsideFeature.WADI:            "Wadi_hexside",
    HexsideFeature.MAJOR_RIVER:     "Major_River_hexside",
    HexsideFeature.MINOR_RIVER:     "Minor_River_hexside",
    HexsideFeature.ROAD:            None,       # modifier, not additive cost
    HexsideFeature.TRACK:           None,       # modifier, not additive cost
    HexsideFeature.RAILROAD:        "Railroad", # same_as_terrain; no extra CP
}

# Default CP cost when TEC says "same_as_terrain" (underlying terrain unknown).
# Desert/Clear are the most common terrains in North Africa → 1 CP.
_SAME_AS_TERRAIN_DEFAULT = 1.0

# Non-motorized foot-unit cost to climb an escarpment (rule 8.42).
# The TEC gives "P_vehicles" in the non-mot column, meaning vehicles are
# prohibited; true foot infantry may climb.  The rules quote "+8 CP" in a
# note, which we use as the climb cost.
_ESCARPMENT_UP_FOOT_CP = 8.0


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_cp(raw) -> Union[float, str]:
    """
    Convert a raw TEC cp_mot / cp_non_mot value to float or a string sentinel.

    Returns:
      float        — CP cost or delta (may be negative for Road/Down_Slope)
      "P"          — prohibited for all units
      "P_vehicles" — prohibited for motorized/vehicle units
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    if raw == "P":
        return "P"
    if raw == "P_vehicles":
        return "P_vehicles"
    if raw == "same_as_terrain":
        return _SAME_AS_TERRAIN_DEFAULT
    if isinstance(raw, str):
        try:
            return float(raw)   # handles "+2", "-1", "+6", etc.
        except ValueError:
            return _SAME_AS_TERRAIN_DEFAULT
    return _SAME_AS_TERRAIN_DEFAULT


def _parse_bd(raw) -> float:
    """Convert a raw TEC bd value to float (strips trailing '*' markers)."""
    if isinstance(raw, (int, float)):
        return float(raw)
    if raw is None:
        return 0.0
    if isinstance(raw, str):
        try:
            return float(raw.rstrip("*"))
        except ValueError:
            return 0.0
    return 0.0


# ── HexMap ────────────────────────────────────────────────────────────────────

class HexMap:
    """
    Read-only movement-cost and ZOC interface over the CNA map.

    Construct once per OpStage (re-create if hexes change):

        hmap = HexMap(game_state.hexes, rules["terrain_effects_chart"])

    hexes : dict[hex_id → Hex]   — from GameState; sparse, unknown hexes
                                    auto-stub as Desert
    tec   : dict                 — full terrain_effects_chart dict, or just
                                    its terrain_types sub-dict
    """

    def __init__(self, hexes: Dict[str, Hex], tec: dict) -> None:
        self._hexes = hexes
        # Accept either the full TEC dict or just terrain_types
        self._tec: dict = tec.get("terrain_types", tec)

    # ── Hex access ────────────────────────────────────────────────────────────

    def get(self, hex_id: str) -> Hex:
        """Return Hex for hex_id; create a minimal Desert stub if not in map."""
        return self._hexes.get(hex_id) or Hex.from_id(hex_id)

    # ── Neighbour geometry ────────────────────────────────────────────────────

    def neighbors_by_direction(self, hex_id: str) -> Dict[str, Optional[str]]:
        """
        All 6 cardinal neighbours keyed by direction string.
        Off-map neighbours (edge of map or invalid section) map to None.
        """
        h = self.get(hex_id)
        deltas = _ODD_COL_DELTAS if h.col % 2 == 1 else _EVEN_COL_DELTAS
        sec_idx = _SECTION_ORDER.index(h.section)
        result: Dict[str, Optional[str]] = {}

        for i, (dc, dr) in enumerate(deltas):
            direction = _DIRECTIONS[i]
            nc, nr = h.col + dc, h.row + dr
            nsi = sec_idx

            # Handle section boundary crossings
            if nc < 1:
                nsi -= 1
                if nsi < 0:
                    result[direction] = None
                    continue
                nc = _MAX_COL
            elif nc > _MAX_COL:
                nsi += 1
                if nsi >= len(_SECTION_ORDER):
                    result[direction] = None
                    continue
                nc = 1

            if nr < 1 or nr > _MAX_ROW:
                result[direction] = None
                continue

            result[direction] = f"{_SECTION_ORDER[nsi]}{nc:02d}{nr:02d}"

        return result

    def neighbors(self, hex_id: str) -> List[str]:
        """Up to 6 on-map neighbour hex_ids."""
        return [h for h in self.neighbors_by_direction(hex_id).values()
                if h is not None]

    def direction_to(self, from_id: str, to_id: str) -> Optional[str]:
        """Direction string (N/NE/SE/S/SW/NW) from from_id to to_id, or None."""
        for d, nbr in self.neighbors_by_direction(from_id).items():
            if nbr == to_id:
                return d
        return None

    def are_adjacent(self, a: str, b: str) -> bool:
        return self.direction_to(a, b) is not None

    # ── Distance ──────────────────────────────────────────────────────────────

    def distance(self, from_id: str, to_id: str) -> int:
        """Shortest hex distance ignoring terrain and ZOC."""
        return self.get(from_id).distance_to(self.get(to_id))

    # ── Internal TEC helpers ──────────────────────────────────────────────────

    def _tec_entry(self, key: str) -> dict:
        return self._tec.get(key, {})

    def _crossing_feature(
        self, from_hex: Hex, to_hex: Hex, direction: str
    ) -> HexsideFeature:
        """
        Hexside feature for crossing from from_hex → to_hex.

        Checks from_hex.hexsides[direction] first; falls back to
        to_hex.hexsides[opposite] if the forward side is NONE.
        This handles the common case where hexside data is only stored
        on one side of the border hex.
        """
        fwd = from_hex.hexside(direction)
        bwd = to_hex.hexside(_OPPOSITE[direction])
        if fwd == HexsideFeature.NONE and bwd != HexsideFeature.NONE:
            return bwd
        return fwd

    def _base_terrain_cp(self, terrain: Terrain, motorized: bool) -> Union[float, str]:
        """Base CP cost to enter a hex with the given primary terrain."""
        entry = self._tec_entry(terrain.value)
        raw = entry.get("cp_mot" if motorized else "cp_non_mot", 1)
        return _parse_cp(raw)

    def _hexside_cp_delta(
        self, feature: HexsideFeature, motorized: bool
    ) -> Union[float, str]:
        """
        Additive CP modifier for crossing this hexside feature.

        ROAD and TRACK are handled as structural modifiers in entry_cost(),
        not as additive deltas, so they return 0.0 here.

        Returns float (may be negative) or "P" / "P_vehicles".
        """
        tec_key = _HEXSIDE_TEC_KEY.get(feature)
        if tec_key is None:
            return 0.0   # NONE, ROAD, TRACK — no additive delta

        entry = self._tec_entry(tec_key)
        raw = entry.get("cp_mot" if motorized else "cp_non_mot", 0)
        return _parse_cp(raw)

    def _hexside_bd(self, feature: HexsideFeature) -> float:
        """Breakdown points for crossing this hexside feature."""
        tec_key = _HEXSIDE_TEC_KEY.get(feature)
        if tec_key is None:
            return 0.0
        entry = self._tec_entry(tec_key)
        return _parse_bd(entry.get("bd", 0))

    # ── Movement cost (rule 8.37) ─────────────────────────────────────────────

    def entry_cost(
        self,
        unit: Unit,
        from_id: str,
        to_id: str,
        weather: str = "clear",
    ) -> Union[float, str]:
        """
        CP cost for unit to move from from_id into to_id.

        Returns:
          float   — CP to spend (always ≥ 1.0)
          "P"     — move is prohibited for this unit

        Algorithm (rule 8.37):
          1. Base terrain CP cost of the hex being entered.
          2. Add hexside feature CP modifier.
          3. ROAD on hexside: base cost − 1; hexside delta = 0 (note 6).
          4. TRACK on hexside: CP unchanged; only BD is halved (note 7).
          5. Total ≥ 1.0.

        Weather modifier (partial — rainstorm rules 4.0):
          Wadi/Minor River crossing is prohibited for motorized without
          road/railroad during rainstorm.  Pass weather="rainstorm" to enforce.
        """
        direction = self.direction_to(from_id, to_id)
        if direction is None:
            return "P"   # not adjacent

        from_hex = self.get(from_id)
        to_hex   = self.get(to_id)
        mot      = unit.motorized
        crossing = self._crossing_feature(from_hex, to_hex, direction)

        # ── Absolute prohibitions ─────────────────────────────────────────────

        # Swamp: impassable for everyone
        if to_hex.terrain == Terrain.SWAMP:
            return "P"

        # Salt Marsh: impassable for motorized (most vehicles); non-mot = 5 CP
        if to_hex.terrain == Terrain.SALT_MARSH and mot:
            return "P"   # note 2: most vehicles prohibited; track via note 2 = still P

        # Up Escarpment: no vehicle may ever cross upward (rule 8.42)
        if crossing == HexsideFeature.ESCARPMENT_UP:
            if mot:
                return "P"
            return _ESCARPMENT_UP_FOOT_CP   # foot infantry: ~8 CP

        # Major River: only crossable via road or railroad (note 11)
        if crossing == HexsideFeature.MAJOR_RIVER:
            has_bridge = (
                from_hex.has_road or from_hex.has_railroad
                or to_hex.has_road or to_hex.has_railroad
                or crossing in (HexsideFeature.ROAD, HexsideFeature.RAILROAD)
            )
            if not has_bridge:
                return "P"

        # Wadi/Minor River in rainstorm: motorized without road/railroad
        if weather == "rainstorm" and mot:
            if crossing in (HexsideFeature.WADI, HexsideFeature.MINOR_RIVER):
                has_road = (
                    crossing == HexsideFeature.ROAD
                    or from_hex.has_road or from_hex.has_railroad
                )
                if not has_road:
                    return "P"

        # ── Detect road / track on this hexside ──────────────────────────────

        road_on_hexside  = (crossing == HexsideFeature.ROAD)
        track_on_hexside = (crossing == HexsideFeature.TRACK)

        # ── Base terrain CP ───────────────────────────────────────────────────

        base_cp = self._base_terrain_cp(to_hex.terrain, mot)
        if base_cp == "P":
            return "P"
        base_cp = float(base_cp)

        # ── Hexside additive CP delta ─────────────────────────────────────────

        delta = self._hexside_cp_delta(crossing, mot)

        if delta == "P":
            return "P"

        if delta == "P_vehicles":
            # P_vehicles in non-mot column means vehicles prohibited; foot OK
            if mot:
                return "P"
            # For non-mot foot: the rule says prohibited for vehicles but
            # no explicit CP is given — we already handle ESCARPMENT_UP above.
            # Any other P_vehicles case: treat as impassable for safety.
            return "P"

        delta = float(delta)

        # ── Road modifier (note 6) ────────────────────────────────────────────
        # Road reduces base terrain by 1 and negates all hexside feature costs.

        if road_on_hexside:
            base_cp = max(0.0, base_cp - 1.0)
            delta   = 0.0

        # ── Track modifier (note 7) ───────────────────────────────────────────
        # Track: CP is unchanged ("same_as_terrain" for the Track hexside).
        # Only BD is halved — no CP change here.

        # (no action needed for CP)

        # ── Total, minimum 1 CP ───────────────────────────────────────────────

        return max(1.0, base_cp + delta)

    def entry_bd(
        self,
        unit: Unit,
        from_id: str,
        to_id: str,
    ) -> float:
        """
        Breakdown points accumulated when a motorized unit enters to_id.

        Non-motorized units never gain BD (they don't have vehicles to break
        down).  Returns 0.0 for non-motorized units.

        Track halves BD (rule 8.37 note 7), except escarpment-down (rule 8.42).
        Road negates hexside BD (note 6).
        """
        if not unit.motorized:
            return 0.0

        direction = self.direction_to(from_id, to_id)
        if direction is None:
            return 0.0

        from_hex = self.get(from_id)
        to_hex   = self.get(to_id)
        crossing = self._crossing_feature(from_hex, to_hex, direction)

        # Base terrain BD
        tec_entry  = self._tec_entry(to_hex.terrain.value)
        terrain_bd = _parse_bd(tec_entry.get("bd", 0))

        # Hexside BD
        hexside_bd = self._hexside_bd(crossing)

        # Track halves BD (except escarpment-down per rule 8.42)
        if crossing == HexsideFeature.TRACK:
            terrain_bd *= 0.5
            if crossing != HexsideFeature.ESCARPMENT_DOWN:
                hexside_bd *= 0.5

        # Road zeroes hexside BD (note 6)
        if crossing == HexsideFeature.ROAD:
            hexside_bd = 0.0

        return terrain_bd + hexside_bd

    # ── Combat terrain modifiers ──────────────────────────────────────────────

    def combat_column_shifts(
        self,
        defender_hex_id: str,
        combat_type: str,
        fort_level: int = 0,
    ) -> int:
        """
        Net column shift (negative = left = defender advantage) for a given
        combat type in a defender's hex.

        combat_type: "barrage" | "aa" (anti-armor) | "ca" (close assault)
        fort_level: current fortification level (0–3) from GameState.

        Returns integer shift to apply to the attacker's die roll column.
        Column shifts stack (terrain + fortification).
        """
        h = self.get(defender_hex_id)
        terrain_shift = self._combat_shift(h.terrain.value, combat_type)

        fort_shift = 0
        if fort_level > 0:
            fort_key = f"Fortification_Level_{fort_level}"
            fort_shift = self._combat_shift(fort_key, combat_type)

        return terrain_shift + fort_shift

    def _combat_shift(self, tec_key: str, combat_type: str) -> int:
        """
        Parse a column shift value from the TEC for the given terrain key
        and combat type.  L1 → -1, L2 → -2, 0 → 0.
        """
        entry = self._tec_entry(tec_key)
        raw = entry.get(combat_type, 0)   # combat_type is "barrage", "aa", or "ca"
        if raw == 0:
            return 0
        if isinstance(raw, str):
            raw = raw.rstrip("*")          # strip asterisk footnote markers
            if raw.startswith("L"):
                try:
                    return -int(raw[1:])   # "L1" → -1, "L2" → -2
                except ValueError:
                    return 0
            try:
                return int(raw)
            except ValueError:
                return 0
        return int(raw)

    # ── ZOC (Zone of Control) ─────────────────────────────────────────────────

    @staticmethod
    def projects_zoc(unit: Unit) -> bool:
        """
        True if this unit projects ZOC into its 6 adjacent hexes.

        ZOC-projecting types: infantry, armor, artillery, anti-tank,
        anti-aircraft, reconnaissance, garrison.  HQ, supply, and truck
        counters do NOT project ZOC.
        """
        return unit.type in _ZOC_TYPES and not unit.is_eliminated()

    def zoc_hexes(self, side: Side, units: Iterable[Unit]) -> set:
        """
        Set of hex_ids under ZOC of units belonging to side.

        A ZOC-projecting unit covers all 6 adjacent hexes (not its own hex).
        ZOC extends across all terrain and hexside features — terrain does not
        limit ZOC projection.
        """
        zoc: set = set()
        for u in units:
            if u.side == side and u.hex_id and self.projects_zoc(u):
                zoc.update(self.neighbors(u.hex_id))
        return zoc

    def in_enemy_zoc(
        self,
        hex_id: str,
        friendly_side: Side,
        units: Iterable[Unit],
    ) -> bool:
        """True if hex_id is under ZOC of any unit hostile to friendly_side."""
        enemy = (
            Side.AXIS if friendly_side == Side.COMMONWEALTH else Side.COMMONWEALTH
        )
        return hex_id in self.zoc_hexes(enemy, units)

    def zoc_cancelled(
        self,
        hex_id: str,
        friendly_side: Side,
        units: Iterable[Unit],
    ) -> bool:
        """
        True if hex_id is in enemy ZOC but a friendly unit occupies it —
        which allows a supply line to pass through (ZOC is 'contested').

        Callers can use this to decide whether to include hex_id in a supply
        BFS even though it's under enemy ZOC.
        """
        units_list = list(units)
        if not self.in_enemy_zoc(hex_id, friendly_side, units_list):
            return False
        return any(
            u.side == friendly_side
            and u.hex_id == hex_id
            and not u.is_eliminated()
            for u in units_list
        )
