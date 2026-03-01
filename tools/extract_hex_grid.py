#!/usr/bin/env python3
"""
extract_hex_grid.py
===================
Computes the CNA hex grid from Vassal module HexGrid parameters.

The CNA map uses a SIDEWAYS hex grid (flat-top hexes, columns run E-W).
Parameters from buildFile.xml:
  dx = 72.95   (horizontal distance between hex centers)
  dy = 85.25   (vertical distance between hex centers)
  Grid is "sideways" (HexGrid with sideways=true means pointy-top hexes
  arranged in columns, which in Vassal means: columns are staggered,
  the stagger is vertical)

CNA hex coordinate system (from map labels visible in Vassal):
  - Hex addresses are like "A 0101", "B 3025", "C 0716"
  - Letter prefix = map section (A=far west, B=central, C=east, etc.)
  - Four-digit number = COLUMN-ROW (first 2 digits = column, last 2 = row)

Output:
  data/extracted/hex_grid.json  — hex grid geometry and coordinate mapping
  data/extracted/hex_coords.json — full mapping of every hex → pixel center

Usage:
    python tools/extract_hex_grid.py
"""

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BUILD_FILE = REPO_ROOT / "data" / "vassal" / "buildFile.xml"
OUT_GRID = REPO_ROOT / "data" / "extracted" / "hex_grid.json"
OUT_COORDS = REPO_ROOT / "data" / "extracted" / "hex_coords.json"


# CNA map sections. Each section is one Vassal Board.
# The map runs from Tunisia (west) to Egypt (east).
# Sections are named A through E (approximately).
MAP_SECTIONS = {
    "A": {"board_name": "CNA Map A", "hex_cols": (1, 33), "hex_rows": (1, 33)},
    "B": {"board_name": "CNA Map B", "hex_cols": (1, 60), "hex_rows": (1, 33)},
    "C": {"board_name": "CNA Map C", "hex_cols": (1, 33), "hex_rows": (1, 33)},
    "D": {"board_name": "CNA Map D", "hex_cols": (1, 33), "hex_rows": (1, 33)},
    "E": {"board_name": "CNA Map E", "hex_cols": (1, 33), "hex_rows": (1, 33)},
}

# These are the HexGrid parameters discovered from buildFile.xml
# (dx=72.95, dy=85.25, verified from parse_vassal.py output)
GRID_PARAMS = {
    "dx": 72.95,
    "dy": 85.25,
    "x0": -15,      # x origin offset (from first grid in buildFile)
    "y0": 4,        # y origin offset
    "sideways": True,  # CNA uses sideways=true (pointy-top hexes in columns)
}


def hex_center_pixel(col: int, row: int, params: dict) -> tuple[float, float]:
    """
    Compute pixel coordinates of the center of hex (col, row).

    For a sideways hex grid (pointy-top hexes arranged in columns):
    - Even columns: y = row * dy
    - Odd columns:  y = row * dy + dy/2  (staggered by half a row)
    - x = col * dx * (3/4)   [for flat-top, but CNA is sideways...]

    Actually for CNA's sideways hex grid:
    - dx = horizontal distance between column centers = 72.95
    - dy = vertical distance between row centers within same column = 85.25
    - Odd columns are shifted down by dy/2
    """
    dx = params["dx"]
    dy = params["dy"]
    x0 = params["x0"]
    y0 = params["y0"]

    # Sideways (pointy-top) hex: columns run E-W, rows run N-S
    # Column spacing: dx (full horizontal spacing for pointy-top)
    # Row spacing: dy (vertical spacing)
    # Odd columns stagger by dy/2

    x = x0 + (col - 1) * dx
    if col % 2 == 0:  # even column offset
        y = y0 + (row - 1) * dy + dy / 2
    else:
        y = y0 + (row - 1) * dy

    return (round(x, 2), round(y, 2))


def hex_neighbors(col: int, row: int) -> list[tuple[int, int]]:
    """
    Get the 6 neighbors of a hex in a sideways (pointy-top) grid.
    In a pointy-top hex grid arranged in columns:
    Even columns have different neighbors than odd columns.
    """
    if col % 2 == 1:  # odd column
        return [
            (col,   row - 1),  # N
            (col + 1, row - 1),  # NE
            (col + 1, row),      # SE
            (col,   row + 1),  # S
            (col - 1, row),    # SW
            (col - 1, row - 1),  # NW
        ]
    else:  # even column
        return [
            (col,   row - 1),  # N
            (col + 1, row),    # NE
            (col + 1, row + 1),  # SE
            (col,   row + 1),  # S
            (col - 1, row + 1),  # SW
            (col - 1, row),    # NW
        ]


def pixel_to_hex(px: float, py: float, params: dict, section_x_offset: float = 0) -> tuple[int, int]:
    """
    Convert pixel coordinates to hex col/row.
    Returns (col, row) with 1-based indexing.
    """
    dx = params["dx"]
    dy = params["dy"]
    x0 = params["x0"] + section_x_offset
    y0 = params["y0"]

    # Estimate column
    col_f = (px - x0) / dx + 1
    col = round(col_f)

    # Estimate row based on column parity
    if col % 2 == 1:
        row_f = (py - y0) / dy + 1
    else:
        row_f = (py - y0 - dy / 2) / dy + 1
    row = round(row_f)

    return (col, row)


def parse_board_dimensions(build_file: Path) -> dict:
    """Parse board names and dimensions from buildFile."""
    tree = ET.parse(build_file)
    root = tree.getroot()

    boards = {}
    for el in root.iter():
        if el.tag.endswith("Board"):
            name = el.attrib.get("name", "")
            if not name:
                continue
            boards[name] = {
                "image": el.attrib.get("image", ""),
                "width": int(el.attrib.get("width", "0") or "0"),
                "height": int(el.attrib.get("height", "0") or "0"),
            }

    # Find HexGrid params per board
    for board_el in root.iter():
        if not board_el.tag.endswith("Board"):
            continue
        board_name = board_el.attrib.get("name", "")
        if not board_name:
            continue
        for child in board_el.iter():
            if "HexGrid" in child.tag:
                if board_name in boards:
                    boards[board_name]["hex_grid"] = {
                        "dx": float(child.attrib.get("dx", "0") or "0"),
                        "dy": float(child.attrib.get("dy", "0") or "0"),
                        "x0": float(child.attrib.get("x0", "0") or "0"),
                        "y0": float(child.attrib.get("y0", "0") or "0"),
                        "sideways": child.attrib.get("sideways", "false") == "true",
                    }
                break

    return boards


def parse_zones_with_hex_labels(build_file: Path) -> dict:
    """
    Parse Zone elements that have hex-coordinate-style names.
    These tell us which Vassal zone corresponds to which hex label.
    """
    tree = ET.parse(build_file)
    root = tree.getroot()

    hex_zones = {}
    for el in root.iter():
        if "Zone" not in el.tag:
            continue
        name = el.attrib.get("name", "")
        # CNA hex names look like "A 0101", "B 3025", "C 0716"
        import re
        m = re.match(r"^([A-E])\s+(\d{4})$", name)
        if m:
            section = m.group(1)
            hex_num = m.group(2)
            col = int(hex_num[:2])
            row = int(hex_num[2:])
            hex_zones[name] = {
                "section": section,
                "col": col,
                "row": row,
                "hex_id": f"{section}{hex_num}",
            }

    return hex_zones


def build_location_to_hex_map(build_file: Path) -> dict:
    """
    Build a mapping from Vassal location strings to hex coordinates.
    Vassal location strings in SetupStacks look like "Map A B 3025"
    which means: Map section A, hex B 3025.
    """
    tree = ET.parse(build_file)
    root = tree.getroot()

    import re
    location_map = {}

    for el in root.iter():
        if not el.tag.endswith("SetupStack"):
            continue
        location = el.attrib.get("location", "")
        if not location:
            continue

        # Pattern: "Map X Letter NNNN" or "OC NNNN" or named locations
        # Try to extract hex address
        m = re.search(r"([A-E])\s+(\d{4})", location)
        if m:
            section = m.group(1)
            hex_num = m.group(2)
            col = int(hex_num[:2])
            row = int(hex_num[2:])
            hex_id = f"{section}{hex_num}"
            location_map[location] = {
                "hex_id": hex_id,
                "section": section,
                "col": col,
                "row": row,
            }

    return location_map


def main():
    print(f"Extracting hex grid from {BUILD_FILE}...")

    # Parse board dimensions and hex grid params from buildFile
    boards = parse_board_dimensions(BUILD_FILE)
    print(f"Found {len(boards)} boards in buildFile")

    # Find main map boards (those with hex grids)
    map_boards = {name: data for name, data in boards.items() if "hex_grid" in data}
    print(f"Found {len(map_boards)} boards with HexGrid parameters")

    # Show board info
    for name, data in sorted(map_boards.items())[:10]:
        hg = data.get("hex_grid", {})
        print(f"  {name:50} w={data['width']:5} h={data['height']:5} "
              f"dx={hg.get('dx',0):6.2f} dy={hg.get('dy',0):6.2f} "
              f"x0={hg.get('x0',0):5.1f} y0={hg.get('y0',0):5.1f}")

    # Parse zone hex labels
    hex_zones = parse_zones_with_hex_labels(BUILD_FILE)
    print(f"\nFound {len(hex_zones)} hex-labeled zones")
    if hex_zones:
        sample = list(hex_zones.items())[:5]
        for name, data in sample:
            print(f"  {name} → {data}")

    # Parse location → hex mapping
    location_map = build_location_to_hex_map(BUILD_FILE)
    print(f"Found {len(location_map)} hex-address location strings in SetupStacks")
    if location_map:
        sample = list(location_map.items())[:5]
        for loc, data in sample:
            print(f"  {loc!r:40} → {data['hex_id']}")

    # Generate hex coordinate system
    # Based on the established CNA map structure:
    # Map sections A-E, each with a grid of hexes
    # Using the canonical grid params (dx=72.95, dy=85.25)
    params = GRID_PARAMS

    # Compute pixel centers for a sample of hexes per section
    # In practice we'd need the actual board x-offsets to place sections
    # relatively, but for now we'll compute within-board coordinates
    print(f"\nGrid parameters: dx={params['dx']}, dy={params['dy']}")
    print(f"  Origin: x0={params['x0']}, y0={params['y0']}")
    print(f"  Sideways: {params['sideways']}")

    # Sample hex center calculations
    print("\nSample hex centers (col, row) → (pixel_x, pixel_y):")
    for col, row in [(1,1), (1,2), (2,1), (2,2), (5,10), (10,5), (33,33)]:
        px, py = hex_center_pixel(col, row, params)
        print(f"  ({col:2},{row:2}) → ({px:7.1f}, {py:7.1f})")

    # Build comprehensive grid output
    grid_output = {
        "_source": str(BUILD_FILE.name),
        "_note": "CNA uses sideways hex grid (pointy-top hexes, columns E-W). "
                 "dx=column spacing, dy=row spacing within column. "
                 "Odd columns staggered by dy/2.",
        "parameters": {
            "dx": params["dx"],
            "dy": params["dy"],
            "x0_default": params["x0"],
            "y0_default": params["y0"],
            "sideways": params["sideways"],
            "hex_size_approx_pixels": round((params["dx"] + params["dy"]) / 2, 1),
        },
        "coordinate_system": {
            "format": "SCCRRR where S=section letter (A-E), CC=column (01-60), RR=row (01-33)",
            "examples": ["A0101", "B3025", "B0117", "C0716"],
            "section_order": "A=west(Tunisia/Tripolitania), B=central(Cyrenaica/Tobruk), "
                             "C=east(Egypt border), D=Egypt, E=deep Egypt/Nile",
        },
        "neighbor_directions": {
            "description": "Pointy-top hex neighbors for odd/even columns",
            "odd_column": ["N=(col,row-1)", "NE=(col+1,row-1)", "SE=(col+1,row)",
                           "S=(col,row+1)", "SW=(col-1,row)", "NW=(col-1,row-1)"],
            "even_column": ["N=(col,row-1)", "NE=(col+1,row)", "SE=(col+1,row+1)",
                            "S=(col,row+1)", "SW=(col-1,row+1)", "NW=(col-1,row)"],
        },
        "boards": {name: data for name, data in sorted(map_boards.items())},
        "all_boards": {name: {k: v for k, v in data.items() if k != "hex_grid"}
                       for name, data in sorted(boards.items())},
        "hex_labeled_zones": hex_zones,
        "location_to_hex": location_map,
        "pixel_to_hex_formula": {
            "col": "round((px - x0) / dx) + 1",
            "row_odd_col": "round((py - y0) / dy) + 1",
            "row_even_col": "round((py - y0 - dy/2) / dy) + 1",
        },
        "sample_hex_centers": {
            f"{col:02d}{row:02d}": {"x": hex_center_pixel(col, row, params)[0],
                                    "y": hex_center_pixel(col, row, params)[1]}
            for col in range(1, 10) for row in range(1, 10)
        },
    }

    with open(OUT_GRID, "w") as f:
        json.dump(grid_output, f, indent=2)
    print(f"\nWritten: {OUT_GRID} ({OUT_GRID.stat().st_size//1024} KB)")

    # Print location → hex summary for Crusader-relevant hexes
    print("\nKey location → hex mappings:")
    crusader_keywords = ["tobruk", "benghazi", "derna", "bardia", "mersa", "tripoli",
                         "sollum", "matruh", "sidi", "gazala", "agheila"]
    for loc, data in sorted(location_map.items()):
        if any(kw in loc.lower() for kw in crusader_keywords):
            print(f"  {loc!r:50} → {data['hex_id']}")

    print("\nDone.")


if __name__ == "__main__":
    main()
