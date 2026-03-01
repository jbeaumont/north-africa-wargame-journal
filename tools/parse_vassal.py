#!/usr/bin/env python3
"""
parse_vassal.py
===============
Parses the Vassal module buildFile.xml to extract:
  1. All counter definitions (PieceSlot elements) with name, gpid, SVG image
  2. All SetupStack placements with board, location/x/y, and unit name
  3. Hex grid parameters from HexGrid elements

Outputs:
  data/extracted/counters.json      — all unit counter definitions
  data/extracted/unit_placements.json — all SetupStack positions

Usage:
    python tools/parse_vassal.py
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict

REPO_ROOT = Path(__file__).parent.parent
BUILD_FILE = REPO_ROOT / "data" / "vassal" / "buildFile.xml"
OUT_COUNTERS = REPO_ROOT / "data" / "extracted" / "counters.json"
OUT_PLACEMENTS = REPO_ROOT / "data" / "extracted" / "unit_placements.json"
OUT_HEX_GRID = REPO_ROOT / "data" / "extracted" / "hex_grid.json"


def tag_local(el):
    """Get local tag name (strip namespace if any)."""
    tag = el.tag
    if "}" in tag:
        return tag.split("}")[1]
    return tag


def parse_piece_text(text: str) -> dict:
    """
    Parse the Vassal piece definition text (tab-delimited trait stack).
    Returns a dict with extracted fields.
    """
    if not text:
        return {}

    # Split by tab to get trait blocks
    parts = text.split("\t")

    result = {}
    svg_images = []
    prototype_names = []

    for part in parts:
        part = part.strip()

        # 'piece' trait: piece;;;IMAGE.svg;ENTRY_NAME/LABEL
        if part.startswith("piece;"):
            fields = part.split(";")
            if len(fields) >= 4:
                img = fields[3]
                if img and img.endswith(".svg"):
                    svg_images.append(img)
                if len(fields) >= 5:
                    name_label = fields[4]
                    if "/" in name_label:
                        result["piece_name"] = name_label.split("/")[0]
                        result["piece_label"] = name_label.split("/")[1]
                    else:
                        result["piece_name"] = name_label

        # 'prototype' trait: prototype;PROTO_NAME\
        elif part.startswith("prototype;"):
            proto_name = part[len("prototype;"):].rstrip("\\").strip()
            if proto_name:
                prototype_names.append(proto_name)

        # emb2 trait (embellishment/image flip): contains SVG names
        elif part.startswith("+/null/emb2") or part.startswith("emb2"):
            # Extract all .svg filenames from this trait
            found_svgs = re.findall(r"[\w\-\.]+\.svg", part)
            svg_images.extend(found_svgs)

        # label trait: extract label text
        elif part.startswith("+/null/label") or part.startswith("label;"):
            # Look for pieceName usage
            pass

    if svg_images:
        result["svg_images"] = list(dict.fromkeys(svg_images))  # deduplicate
    if prototype_names:
        result["prototypes"] = list(dict.fromkeys(prototype_names))

    return result


def classify_unit(entry_name: str, owning_board: str) -> dict:
    """
    Classify a unit based on its name and owning board.
    Returns nationality, type, formation.
    """
    name = entry_name.lower()
    board = owning_board.lower()

    # Nationality
    nationality = "unknown"
    if board.startswith("br ") or board.startswith("br\t") or name.startswith("al ") or board.startswith("al "):
        nationality = "british"
    elif board.startswith("au "):
        nationality = "australian"
    elif board.startswith("in "):
        nationality = "indian"
    elif board.startswith("nz "):
        nationality = "new_zealand"
    elif board.startswith("sa "):
        nationality = "south_african"
    elif board.startswith("ge ") or board.startswith("ge\t"):
        nationality = "german"
    elif board.startswith("it ") or board.startswith("it\t"):
        nationality = "italian"
    elif board.startswith("al "):
        nationality = "allied_misc"

    # Unit type
    unit_type = "unknown"
    if any(k in name for k in ["inf", "infantry", "btn", "battalion", "bde"]):
        unit_type = "infantry"
    elif any(k in name for k in ["arm", "armor", "tank", "panzer", "pz"]):
        unit_type = "armor"
    elif any(k in name for k in ["art", "artillery", "how", "mortar", "gun"]):
        unit_type = "artillery"
    elif any(k in name for k in ["hq", "headquarters"]):
        unit_type = "hq"
    elif any(k in name for k in ["eng", "engineer", "sapper"]):
        unit_type = "engineer"
    elif any(k in name for k in ["recon", "recce", "cav", "cavalry", "armd car", "ac "]):
        unit_type = "recon"
    elif any(k in name for k in ["air", "fighter", "bomber", "plane", "squadron"]):
        unit_type = "air"
    elif any(k in name for k in ["supply", "depot", "dump", "sgsu", "fuel", "ammo"]):
        unit_type = "supply"
    elif any(k in name for k in ["anti-tank", "at ", "pak", "pounder", "6 pdr", "17 pdr"]):
        unit_type = "anti_tank"
    elif any(k in name for k in ["aa ", "anti-air", "ack ack", "flak", "bofors"]):
        unit_type = "anti_aircraft"
    elif name.startswith("m") and len(name) < 20:
        unit_type = "marker"

    return {"nationality": nationality, "unit_type": unit_type}


def extract_piece_slots(root) -> list[dict]:
    """Extract all PieceSlot elements (counter definitions)."""
    print("Extracting PieceSlot counter definitions...")
    slots = []

    for el in root.iter():
        if not tag_local(el).endswith("PieceSlot"):
            continue

        attrib = el.attrib
        entry_name = attrib.get("entryName", "")
        gpid = attrib.get("gpid", "")
        height = attrib.get("height", "")
        width = attrib.get("width", "")
        text = el.text or ""

        parsed = parse_piece_text(text)
        classification = classify_unit(entry_name, "")

        slot = {
            "gpid": gpid,
            "name": entry_name,
            "height": height,
            "width": width,
            **parsed,
            **classification,
        }
        slots.append(slot)

    print(f"  Found {len(slots)} PieceSlots")
    return slots


def extract_setup_stacks(root) -> list[dict]:
    """Extract all SetupStack elements (unit placements)."""
    print("Extracting SetupStack placements...")
    placements = []

    for el in root.iter():
        if not tag_local(el).endswith("SetupStack"):
            continue

        attrib = el.attrib
        name = attrib.get("name", "")
        owning_board = attrib.get("owningBoard", "")
        use_grid = attrib.get("useGridLocation", "false") == "true"
        location = attrib.get("location", "")
        x = attrib.get("x", "")
        y = attrib.get("y", "")

        # Get child PieceSlots
        children = []
        for child in el:
            child_name = child.attrib.get("entryName", "")
            child_gpid = child.attrib.get("gpid", "")
            child_text = child.text or ""
            child_parsed = parse_piece_text(child_text)
            children.append({
                "name": child_name,
                "gpid": child_gpid,
                **child_parsed,
            })

        classification = classify_unit(name, owning_board)

        placement = {
            "stack_name": name,
            "owning_board": owning_board,
            "formation": owning_board,
            "use_grid_location": use_grid,
            "location": location if use_grid else None,
            "x_pixel": int(x) if x and not use_grid else None,
            "y_pixel": int(y) if y and not use_grid else None,
            "units": children,
            **classification,
        }
        placements.append(placement)

    print(f"  Found {len(placements)} SetupStacks")
    return placements


def extract_hex_grid(root) -> dict:
    """Extract HexGrid parameters from the buildFile."""
    print("Extracting HexGrid parameters...")
    grids = []

    for el in root.iter():
        local = tag_local(el)
        if "HexGrid" in local or "SquareGrid" in local:
            attrib = el.attrib
            if attrib:
                grids.append({
                    "type": local,
                    **attrib,
                })

    # Also extract board dimensions
    boards = {}
    for el in root.iter():
        if tag_local(el).endswith("Board"):
            attrib = el.attrib
            board_name = attrib.get("name", "")
            if board_name:
                boards[board_name] = {
                    "image": attrib.get("image", ""),
                    "width": attrib.get("width", ""),
                    "height": attrib.get("height", ""),
                }

    # Find the main map hex grids (the ones with meaningful dx/dy)
    main_grids = [g for g in grids if float(g.get("dx", "0") or "0") > 10]
    print(f"  Found {len(grids)} grid definitions, {len(main_grids)} main hex grids")
    print(f"  Found {len(boards)} boards")

    # Sample the main grids
    for g in main_grids[:3]:
        print(f"    Grid: type={g['type']}, dx={g.get('dx')}, dy={g.get('dy')}, "
              f"x0={g.get('x0')}, y0={g.get('y0')}")

    return {
        "grids": grids,
        "boards": boards,
        "main_map_grids": main_grids[:10],
    }


def extract_zones(root) -> list[dict]:
    """Extract Zone definitions (named regions on the map)."""
    print("Extracting Zone definitions...")
    zones = []
    for el in root.iter():
        if "Zone" in tag_local(el):
            attrib = el.attrib
            name = attrib.get("name", "")
            if name and len(name) > 1:
                zones.append({
                    "name": name,
                    "type": tag_local(el),
                    **{k: v for k, v in attrib.items() if k != "name"},
                })
    print(f"  Found {len(zones)} zones")
    return zones[:200]  # First 200 for sanity


def summarize_by_formation(placements: list[dict]) -> dict:
    """Group placements by formation for summary."""
    by_formation = defaultdict(list)
    for p in placements:
        formation = p.get("owning_board") or "Unknown"
        by_formation[formation].append({
            "stack_name": p["stack_name"],
            "nationality": p["nationality"],
            "unit_type": p["unit_type"],
            "location": p.get("location"),
            "x": p.get("x_pixel"),
            "y": p.get("y_pixel"),
            "unit_count": len(p.get("units", [])),
        })
    return dict(sorted(by_formation.items()))


def main():
    print(f"Parsing {BUILD_FILE}...")
    tree = ET.parse(BUILD_FILE)
    root = tree.getroot()

    # Extract everything
    hex_grid_data = extract_hex_grid(root)
    slots = extract_piece_slots(root)
    placements = extract_setup_stacks(root)
    zones = extract_zones(root)

    # Build counters output
    counters = {
        "_source": str(BUILD_FILE.name),
        "_total": len(slots),
        "by_type": defaultdict(list),
        "by_nationality": defaultdict(list),
        "all": slots,
    }

    for s in slots:
        counters["by_type"][s.get("unit_type", "unknown")].append(s["name"])
        counters["by_nationality"][s.get("nationality", "unknown")].append(s["name"])

    counters["by_type"] = dict(
        (k, v) for k, v in sorted(counters["by_type"].items())
    )
    counters["by_nationality"] = dict(
        (k, v) for k, v in sorted(counters["by_nationality"].items())
    )

    with open(OUT_COUNTERS, "w") as f:
        json.dump(counters, f, indent=2)
    print(f"\nWritten: {OUT_COUNTERS} ({OUT_COUNTERS.stat().st_size//1024} KB)")

    # Print summary
    print("\nCounters by nationality:")
    for nat, names in counters["by_nationality"].items():
        print(f"  {nat:20} {len(names):4} counters")

    print("\nCounters by type:")
    for utype, names in counters["by_type"].items():
        print(f"  {utype:20} {len(names):4} counters")

    # Build placements output
    placements_data = {
        "_source": str(BUILD_FILE.name),
        "_total": len(placements),
        "by_formation": summarize_by_formation(placements),
        "all": placements,
    }

    with open(OUT_PLACEMENTS, "w") as f:
        json.dump(placements_data, f, indent=2)
    print(f"\nWritten: {OUT_PLACEMENTS} ({OUT_PLACEMENTS.stat().st_size//1024} KB)")

    # Build hex grid output
    hex_grid_out = {
        "_source": str(BUILD_FILE.name),
        "_note": "HexGrid parameters from Vassal module. dx/dy define hex spacing.",
        "grids": hex_grid_data["grids"],
        "boards": hex_grid_data["boards"],
        "main_map_grids": hex_grid_data["main_map_grids"],
        "zones_sample": zones,
    }

    with open(OUT_HEX_GRID, "w") as f:
        json.dump(hex_grid_out, f, indent=2)
    print(f"Written: {OUT_HEX_GRID} ({OUT_HEX_GRID.stat().st_size//1024} KB)")

    print("\nDone.")


if __name__ == "__main__":
    main()
