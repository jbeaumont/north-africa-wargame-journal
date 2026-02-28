"""
Hex map model for The Campaign for North Africa simulation.

The physical CNA map is 5 paper sheets, each 34" × 23", combined ~10 feet long.
Scale: 8 km per hex. Terrain types: 31 distinct classifications.
Key locations: Tripoli → El Agheila → Tobruk → Mersa Matruh → El Alamein → Cairo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Terrain(str, Enum):
    """
    31 terrain classifications from the CNA rules.
    Movement costs reflect actual CNA rule values (in Capability Points per hex).
    """
    FLAT_DESERT_COASTAL = "flat_desert_coastal"
    FLAT_DESERT_ROCKY = "flat_desert_rocky"
    FLAT_DESERT_SANDY = "flat_desert_sandy"
    FLAT_FARMLAND = "flat_farmland"
    FLAT_MARSH = "flat_marsh"
    FLAT_MUD = "flat_mud"
    FLAT_SWAMP = "flat_swamp"
    ROUGH_ESCARPMENT = "rough_escarpment"
    ROUGH_ROCKY = "rough_rocky"
    ROUGH_WADIS = "rough_wadis"
    ROUGH_BROKEN = "rough_broken"
    MOUNTAINS = "mountains"
    MOUNTAINS_WOODED = "mountains_wooded"
    HILLS = "hills"
    HILLS_WOODED = "hills_wooded"
    COASTAL = "coastal"
    SEA = "sea"
    SALT_LAKE = "salt_lake"
    CULTIVATED = "cultivated"
    OASIS = "oasis"
    SCRUB = "scrub"
    DUNES = "dunes"
    PLATEAU = "plateau"
    ESCARPMENT_EDGE = "escarpment_edge"
    WADI = "wadi"
    FORD = "ford"
    ROAD = "road"       # Used for road movement modifier
    TRACK = "track"     # Improved track
    PORT = "port"
    CITY = "city"
    AIRFIELD = "airfield"


# Movement cost in Capability Points per hex (for non-road movement)
TERRAIN_MOVEMENT_COST: dict[Terrain, float] = {
    Terrain.FLAT_DESERT_COASTAL: 2.0,
    Terrain.FLAT_DESERT_ROCKY: 2.0,
    Terrain.FLAT_DESERT_SANDY: 2.5,
    Terrain.FLAT_FARMLAND: 2.0,
    Terrain.FLAT_MARSH: 4.0,
    Terrain.FLAT_MUD: 3.0,
    Terrain.FLAT_SWAMP: 6.0,
    Terrain.ROUGH_ESCARPMENT: 4.0,
    Terrain.ROUGH_ROCKY: 3.0,
    Terrain.ROUGH_WADIS: 3.5,
    Terrain.ROUGH_BROKEN: 3.0,
    Terrain.MOUNTAINS: 6.0,
    Terrain.MOUNTAINS_WOODED: 6.0,
    Terrain.HILLS: 3.0,
    Terrain.HILLS_WOODED: 4.0,
    Terrain.COASTAL: 2.0,
    Terrain.SEA: 99.0,       # Impassable on land
    Terrain.SALT_LAKE: 99.0, # Impassable (Qattara Depression)
    Terrain.CULTIVATED: 2.0,
    Terrain.OASIS: 2.0,
    Terrain.SCRUB: 2.5,
    Terrain.DUNES: 3.0,
    Terrain.PLATEAU: 2.5,
    Terrain.ESCARPMENT_EDGE: 4.0,
    Terrain.WADI: 3.5,
    Terrain.FORD: 2.0,
    Terrain.ROAD: 2.0,       # Base; road movement modifier applied separately
    Terrain.TRACK: 2.0,
    Terrain.PORT: 2.0,
    Terrain.CITY: 2.0,
    Terrain.AIRFIELD: 2.0,
}

# Combat defense modifier by terrain (added to defender's roll)
TERRAIN_DEFENSE_MODIFIER: dict[Terrain, int] = {
    Terrain.FLAT_DESERT_COASTAL: 0,
    Terrain.FLAT_DESERT_ROCKY: 1,
    Terrain.FLAT_DESERT_SANDY: 0,
    Terrain.ROUGH_ESCARPMENT: 3,
    Terrain.ROUGH_ROCKY: 2,
    Terrain.ROUGH_WADIS: 2,
    Terrain.MOUNTAINS: 4,
    Terrain.HILLS: 2,
    Terrain.CITY: 3,
    Terrain.PORT: 2,
}


@dataclass
class Hex:
    """
    A single hex on the CNA map.

    hex_id: CNA-style four-digit string (column-row), e.g. "0320" = col 03, row 20.
    terrain: Primary terrain type for movement and combat.
    road_hexes: Adjacent hex IDs connected by paved road (0.5 CP movement cost).
    track_hexes: Adjacent hex IDs connected by improved track (1.0 CP).
    """
    hex_id: str
    terrain: Terrain
    # Named location (city, landmark) if any
    location_name: Optional[str] = None
    # Adjacent hex IDs (up to 6 for a hex grid)
    adjacent: list[str] = field(default_factory=list)
    # Subsets of adjacent hexes with road / track connections
    road_hexes: list[str] = field(default_factory=list)
    track_hexes: list[str] = field(default_factory=list)
    # Water availability
    has_water_source: bool = False
    water_capacity: int = 0  # Water points available per turn
    # Special features
    is_port: bool = False
    port_capacity: int = 0   # Supply points per turn deliverable by sea
    has_airfield: bool = False
    airfield_capacity: int = 0  # Max aircraft based here
    # Axis or Allied controlled at game start
    initial_controller: Optional[str] = None  # "axis" | "allied" | None

    def movement_cost(self, from_hex: "Hex") -> float:
        """
        CP cost to enter this hex from `from_hex`.
        Road movement (0.5 CP) applies if from_hex.hex_id is in self.road_hexes.
        Track movement (1.0 CP) applies similarly.
        """
        if from_hex.hex_id in self.road_hexes:
            return 0.5
        if from_hex.hex_id in self.track_hexes:
            return 1.0
        return TERRAIN_MOVEMENT_COST.get(self.terrain, 2.0)

    def defense_modifier(self) -> int:
        return TERRAIN_DEFENSE_MODIFIER.get(self.terrain, 0)

    def is_impassable(self) -> bool:
        return self.terrain in {Terrain.SEA, Terrain.SALT_LAKE}


class HexMap:
    """
    The full CNA map as an adjacency graph of Hex objects.

    Hex IDs use CNA's four-digit convention: CCRRR where CC = 2-digit column,
    RR = 2-digit row. The physical map covers approximately 500 hexes
    along the coast from Morocco to Egypt, with interior desert extending south.
    """

    def __init__(self) -> None:
        self._hexes: dict[str, Hex] = {}

    def add_hex(self, h: Hex) -> None:
        self._hexes[h.hex_id] = h

    def get(self, hex_id: str) -> Optional[Hex]:
        return self._hexes.get(hex_id)

    def __len__(self) -> int:
        return len(self._hexes)

    def adjacent_hexes(self, hex_id: str) -> list[Hex]:
        """Return all passable adjacent Hex objects."""
        h = self._hexes.get(hex_id)
        if not h:
            return []
        return [
            self._hexes[adj_id]
            for adj_id in h.adjacent
            if adj_id in self._hexes and not self._hexes[adj_id].is_impassable()
        ]

    def path_movement_cost(self, path: list[str]) -> float:
        """
        Total CP cost to traverse a sequence of hex IDs.
        path[0] is the starting hex (not counted); each subsequent hex costs
        the movement cost from its predecessor.
        """
        if len(path) < 2:
            return 0.0
        total = 0.0
        for i in range(1, len(path)):
            from_h = self._hexes.get(path[i - 1])
            to_h = self._hexes.get(path[i])
            if not from_h or not to_h:
                return float("inf")
            total += to_h.movement_cost(from_h)
        return total

    def bfs_range(self, start_hex_id: str, max_cp: float) -> set[str]:
        """
        Return the set of hex IDs reachable from start within max_cp cost.
        Uses BFS / Dijkstra-style exploration.
        """
        if start_hex_id not in self._hexes:
            return set()

        visited: dict[str, float] = {start_hex_id: 0.0}
        frontier: list[tuple[float, str]] = [(0.0, start_hex_id)]

        while frontier:
            cost_so_far, current = min(frontier, key=lambda x: x[0])
            frontier = [(c, h) for c, h in frontier if h != current]

            for neighbor in self.adjacent_hexes(current):
                current_hex = self._hexes[current]
                step_cost = neighbor.movement_cost(current_hex)
                new_cost = cost_so_far + step_cost
                if new_cost <= max_cp and (
                    neighbor.hex_id not in visited
                    or new_cost < visited[neighbor.hex_id]
                ):
                    visited[neighbor.hex_id] = new_cost
                    frontier.append((new_cost, neighbor.hex_id))

        return set(visited.keys())

    def hex_distance(self, hex_id_a: str, hex_id_b: str) -> int:
        """
        Straight-line hex distance (ignoring terrain) using BFS hop count.
        Useful for supply-line range checks (must be within 5 hexes of depot).
        """
        if hex_id_a == hex_id_b:
            return 0
        visited = {hex_id_a}
        frontier = [hex_id_a]
        distance = 0
        while frontier:
            distance += 1
            next_frontier = []
            for hid in frontier:
                h = self._hexes.get(hid)
                if not h:
                    continue
                for adj in h.adjacent:
                    if adj == hex_id_b:
                        return distance
                    if adj not in visited and adj in self._hexes:
                        visited.add(adj)
                        next_frontier.append(adj)
            frontier = next_frontier
        return 9999  # Disconnected / too far

    def find_named(self, name: str) -> Optional[Hex]:
        """Find a hex by its location name (case-insensitive)."""
        name_lower = name.lower()
        for h in self._hexes.values():
            if h.location_name and h.location_name.lower() == name_lower:
                return h
        return None

    def all_hexes(self) -> list[Hex]:
        return list(self._hexes.values())
