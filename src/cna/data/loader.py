"""
Data loader — reads JSON files from data/ and returns typed Python objects.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models.counter import (
    GroundUnit, AirUnit, SupplyCounter,
    Nationality, Side, UnitType, SupplyState,
)
from ..models.hex_map import HexMap, Hex, Terrain
from ..models.supply import SupplyReport

DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"


def _nat(v: str) -> Nationality:
    return Nationality(v)


def _side(v: str) -> Side:
    return Side(v)


def _unit_type(v: str) -> UnitType:
    return UnitType(v)


def load_ground_units(path: Path) -> dict[str, GroundUnit]:
    with open(path) as f:
        data = json.load(f)
    units: dict[str, GroundUnit] = {}
    for raw in data["units"]:
        unit = GroundUnit(
            id=raw["id"],
            name=raw["name"],
            nationality=_nat(raw["nationality"]),
            side=_side(raw["side"]),
            unit_type=_unit_type(raw["type"]),
            cpa=raw.get("cpa", 15),
            toe_strength=raw.get("toe_strength", 6),
            morale=raw.get("morale", 6),
            cohesion=raw.get("cohesion", 0),
            steps=raw.get("steps", 2),
            max_steps=raw.get("max_steps", 2),
            supply=SupplyState(
                fuel=raw.get("fuel_capacity", 0.0) * 0.8,  # Start at 80% fuel
                water=raw.get("water_factor", 1.0) * 8.0,
                ammo=raw.get("ammo_factor", 1.0) * 6.0,
                stores=4.0,
            ),
            fuel_capacity=raw.get("fuel_capacity", 3.0),
            water_factor=raw.get("water_factor", 1.0),
            ammo_factor=raw.get("ammo_factor", 1.0),
            pasta_rule=raw.get("pasta_rule", False),
            hex_id=raw.get("initial_hex"),
            available_turn=raw.get("available_turn", 1),
            subordinates=raw.get("subordinates", []),
            parent_id=None,
        )
        units[unit.id] = unit
    return units


def load_supply_counters(path: Path) -> dict[str, SupplyCounter]:
    with open(path) as f:
        data = json.load(f)
    counters: dict[str, SupplyCounter] = {}
    for raw in data["supply_counters"]:
        counter = SupplyCounter(
            id=raw["id"],
            name=raw["name"],
            nationality=_nat(raw["nationality"]),
            side=_side(raw["side"]),
            unit_type=UnitType(raw["type"]),
            hex_id=raw.get("hex_id"),
            available_turn=raw.get("available_turn", 1),
            capacity=raw.get("capacity", 100.0),
            current_load=raw.get("current_load", 100.0),
            supply_type=raw.get("supply_type", "general"),
        )
        counters[counter.id] = counter
    return counters


def load_hex_map() -> HexMap:
    path = DATA_DIR / "map" / "hexes.json"
    with open(path) as f:
        data = json.load(f)

    hex_map = HexMap()
    for raw in data["hexes"]:
        terrain_str = raw.get("terrain", "flat_desert_rocky")
        try:
            terrain = Terrain(terrain_str)
        except ValueError:
            terrain = Terrain.FLAT_DESERT_ROCKY

        h = Hex(
            hex_id=raw["hex_id"],
            terrain=terrain,
            location_name=raw.get("location_name"),
            adjacent=raw.get("adjacent", []),
            road_hexes=raw.get("road_hexes", []),
            track_hexes=raw.get("track_hexes", []),
            has_water_source=raw.get("has_water_source", False),
            water_capacity=raw.get("water_capacity", 0),
            is_port=raw.get("is_port", False),
            port_capacity=raw.get("port_capacity", 0),
            has_airfield=raw.get("has_airfield", False),
            airfield_capacity=raw.get("airfield_capacity", 0),
            initial_controller=raw.get("initial_controller"),
        )
        hex_map.add_hex(h)

    # Enforce symmetric adjacency: if A lists B, ensure B lists A.
    for h in hex_map.all_hexes():
        for adj_id in list(h.adjacent):
            adj_hex = hex_map.get(adj_id)
            if adj_hex and h.hex_id not in adj_hex.adjacent:
                adj_hex.adjacent.append(h.hex_id)

    return hex_map


def load_all_ground_units() -> dict[str, GroundUnit]:
    units: dict[str, GroundUnit] = {}
    for filename in ("axis_ground.json", "allied_ground.json"):
        units.update(load_ground_units(DATA_DIR / "units" / filename))
    return units


def load_all_supply_counters() -> dict[str, SupplyCounter]:
    return load_supply_counters(DATA_DIR / "units" / "supply.json")
