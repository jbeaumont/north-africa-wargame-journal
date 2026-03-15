#!/usr/bin/env python3
"""
extract_pdf_tables.py
=====================
Downloads the CNA rulebook PDF from GitHub Releases and extracts:
  - Full page-by-page text → data/rules/cna_rules.txt  (overwrites old OCR version)
  - Structured rules tables → data/extracted/rules_tables.json
    · Terrain Effects Chart (TEC)
    · Combat Results Table (CRT)
    · Supply cost tables
    · Sequence of play
    · Key rule sections (supply, movement, stacking, ZOC, combat, air)

Usage:
    python tools/extract_pdf_tables.py
    python tools/extract_pdf_tables.py --skip-download   # if PDF already in /tmp/cna.pdf
"""

import json
import re
import sys
import os
from pathlib import Path

PDF_URL = "https://github.com/jbeaumont/jason-s-playground/releases/download/assets-v1/The.Campaign.for.North.Africa.pdf"
# Use the text-layer PDF already in the repo; fall back to /tmp download if absent
_LOCAL_PDF = Path(__file__).parent.parent / "data" / "rules" / "The Campaign for North Africa_text.pdf"
PDF_PATH = _LOCAL_PDF if _LOCAL_PDF.exists() else Path("/tmp/cna_rulebook.pdf")
REPO_ROOT = Path(__file__).parent.parent
RULES_TXT = REPO_ROOT / "data" / "rules" / "cna_rules.txt"
EXTRACTED_DIR = REPO_ROOT / "data" / "extracted"
OUTPUT_JSON = EXTRACTED_DIR / "rules_tables.json"


def download_pdf():
    import urllib.request
    print(f"Downloading PDF from GitHub Releases...")
    print(f"  URL: {PDF_URL}")
    print(f"  Destination: {PDF_PATH}")

    def progress(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            pct = min(100, downloaded * 100 // total_size)
            mb = downloaded / 1_000_000
            total_mb = total_size / 1_000_000
            print(f"\r  {mb:.1f} MB / {total_mb:.1f} MB ({pct}%)", end="", flush=True)

    urllib.request.urlretrieve(PDF_URL, PDF_PATH, reporthook=progress)
    print(f"\n  Downloaded: {PDF_PATH.stat().st_size / 1_000_000:.1f} MB")


def extract_text_with_fitz(pdf_path: Path) -> list[dict]:
    """Extract all pages as text using PyMuPDF."""
    import fitz
    print(f"\nExtracting text with PyMuPDF...")
    doc = fitz.open(str(pdf_path))
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text("text")
        pages.append({
            "page": i + 1,
            "text": text,
        })
        if (i + 1) % 10 == 0:
            print(f"  Page {i+1}/{len(doc)}", end="\r", flush=True)
    print(f"  Extracted {len(pages)} pages total")
    doc.close()
    return pages


def save_full_text(pages: list[dict]):
    """Write page-by-page text to cna_rules.txt."""
    print(f"\nWriting full text to {RULES_TXT}...")
    RULES_TXT.parent.mkdir(parents=True, exist_ok=True)
    with open(RULES_TXT, "w", encoding="utf-8") as f:
        for p in pages:
            f.write(f"\n=== PAGE {p['page']} ===\n")
            f.write(p["text"])
    size_kb = RULES_TXT.stat().st_size // 1024
    print(f"  Written: {size_kb} KB")


def find_section(pages: list[dict], patterns: list[str], window: int = 5) -> list[dict]:
    """Return pages that match any of the given regex patterns."""
    results = []
    for p in pages:
        for pat in patterns:
            if re.search(pat, p["text"], re.IGNORECASE):
                results.append(p)
                break
    return results


def extract_terrain_effects(pages: list[dict]) -> dict:
    """Find and parse the Terrain Effects Chart."""
    print("  Extracting Terrain Effects Chart...")
    tec_pages = find_section(pages, [
        r"terrain effects chart",
        r"TEC",
        r"movement cost.*terrain",
        r"terrain.*movement cost",
    ])

    raw_text = "\n".join(p["text"] for p in tec_pages[:6])

    terrain_types = {}
    # CNA terrain types from the rules
    known_terrain = [
        "Road", "Trail", "Flat Desert", "Rough Desert", "Rocky Desert",
        "Escarpment", "Wadi", "Marsh", "Salt Marsh", "Hills", "Mountains",
        "Coastal", "Port", "Town", "Village", "Airfield", "Sand Sea",
        "Soft Sand", "Hard Sand",
    ]

    for terrain in known_terrain:
        pattern = rf"{re.escape(terrain)}.*?(\d+[\.,]?\d*)"
        match = re.search(pattern, raw_text, re.IGNORECASE)
        if match:
            terrain_types[terrain] = {
                "movement_cost": match.group(1).replace(",", "."),
                "raw": match.group(0)[:80],
            }

    return {
        "source_pages": [p["page"] for p in tec_pages[:6]],
        "terrain_types": terrain_types,
        "raw_text": raw_text[:3000],
    }


def extract_combat_results_table(pages: list[dict]) -> dict:
    """Find and parse the Combat Results Table."""
    print("  Extracting Combat Results Table...")
    crt_pages = find_section(pages, [
        r"combat results table",
        r"\bCRT\b",
        r"attacker.*defender.*odds",
        r"DR.*EX.*DE",
    ])

    raw_text = "\n".join(p["text"] for p in crt_pages[:4])

    # CNA uses 2d6, odds ratios, and results like AE, DE, AR, DR, EX
    crt_data = {
        "dice": "2d6",
        "result_codes": {
            "AE": "Attacker Eliminated",
            "DE": "Defender Eliminated",
            "AR": "Attacker Retreat",
            "DR": "Defender Retreat",
            "EX": "Exchange",
            "NE": "No Effect",
        },
        "source_pages": [p["page"] for p in crt_pages[:4]],
        "raw_text": raw_text[:3000],
    }

    # Try to extract actual table rows
    rows = []
    for line in raw_text.split("\n"):
        line = line.strip()
        if re.match(r"^\d+\s+[\w\s]+", line):
            rows.append(line)
    crt_data["raw_rows"] = rows[:40]

    return crt_data


def extract_sequence_of_play(pages: list[dict]) -> dict:
    """Extract the sequence of play."""
    print("  Extracting Sequence of Play...")
    sop_pages = find_section(pages, [
        r"sequence of play",
        r"5\.0.*sequence",
        r"operations stage",
    ])

    raw_text = "\n".join(p["text"] for p in sop_pages[:6])

    phases = []
    phase_pattern = re.compile(
        r"(\d+\.\d+[\d\.]*)\s+([A-Z][A-Z\s,\-\(\)]+?)(?=\n\d+\.\d+|\Z)",
        re.MULTILINE
    )
    for m in phase_pattern.finditer(raw_text):
        phases.append({
            "rule": m.group(1).strip(),
            "name": m.group(2).strip()[:100],
        })

    return {
        "source_pages": [p["page"] for p in sop_pages[:6]],
        "phases": phases[:30],
        "raw_text": raw_text[:4000],
    }


def extract_supply_rules(pages: list[dict]) -> dict:
    """Extract supply rules section."""
    print("  Extracting Supply Rules...")
    supply_pages = find_section(pages, [
        r"supply.*rules",
        r"9\.0.*supply",
        r"10\.0.*supply",
        r"fuel.*evapor",
        r"supply line",
        r"out of supply",
    ])

    raw_text = "\n".join(p["text"] for p in supply_pages[:10])

    # Look for fuel evaporation rates
    evap = {}
    british_match = re.search(r"british.*?(\d+)\s*%", raw_text, re.IGNORECASE)
    german_match = re.search(r"german.*?(\d+)\s*%", raw_text, re.IGNORECASE)
    general_match = re.search(r"(\d+)\s*%.*evapor", raw_text, re.IGNORECASE)
    if british_match:
        evap["british_pct"] = int(british_match.group(1))
    if german_match:
        evap["german_pct"] = int(german_match.group(1))
    if general_match and "general_pct" not in evap:
        evap["general_pct"] = int(general_match.group(1))

    # Look for supply path distance
    dist_match = re.search(r"(\d+)\s*hex(?:es)?\s*(?:or fewer|maximum|of|from)", raw_text, re.IGNORECASE)
    supply_path_hexes = int(dist_match.group(1)) if dist_match else None

    return {
        "source_pages": [p["page"] for p in supply_pages[:10]],
        "fuel_evaporation": evap,
        "supply_path_max_hexes": supply_path_hexes,
        "raw_text": raw_text[:5000],
    }


def extract_stacking_limits(pages: list[dict]) -> dict:
    """Extract stacking rules."""
    print("  Extracting Stacking Limits...")
    stack_pages = find_section(pages, [
        r"stacking",
        r"6\.0.*stack",
        r"stacking limit",
    ])
    raw_text = "\n".join(p["text"] for p in stack_pages[:4])
    return {
        "source_pages": [p["page"] for p in stack_pages[:4]],
        "raw_text": raw_text[:3000],
    }


def extract_zoc_rules(pages: list[dict]) -> dict:
    """Extract Zone of Control rules."""
    print("  Extracting ZOC Rules...")
    zoc_pages = find_section(pages, [
        r"zone of control",
        r"\bZOC\b",
        r"7\.0.*zone",
    ])
    raw_text = "\n".join(p["text"] for p in zoc_pages[:4])
    return {
        "source_pages": [p["page"] for p in zoc_pages[:4]],
        "raw_text": raw_text[:3000],
    }


def extract_pasta_rule(pages: list[dict]) -> dict:
    """Extract the pasta rule."""
    print("  Extracting Pasta Rule...")
    pasta_pages = find_section(pages, [
        r"pasta",
        r"italian.*water",
        r"water.*pasta",
    ])
    raw_text = "\n".join(p["text"] for p in pasta_pages[:3])
    return {
        "source_pages": [p["page"] for p in pasta_pages[:3]],
        "raw_text": raw_text[:2000],
    }


def extract_movement_rules(pages: list[dict]) -> dict:
    """Extract movement rules."""
    print("  Extracting Movement Rules...")
    move_pages = find_section(pages, [
        r"movement.*rules",
        r"8\.0.*movement",
        r"capability point",
        r"\bCPA\b",
        r"movement allowance",
    ])
    raw_text = "\n".join(p["text"] for p in move_pages[:8])

    # Look for road movement bonus
    road_match = re.search(r"road.*?(\d+[\.,]\d*)\s*(?:cp|movement|per hex)", raw_text, re.IGNORECASE)

    return {
        "source_pages": [p["page"] for p in move_pages[:8]],
        "road_movement_cost": road_match.group(1) if road_match else None,
        "raw_text": raw_text[:5000],
    }


def extract_scenario_crusader(pages: list[dict]) -> dict:
    """Extract Crusader scenario setup."""
    print("  Extracting Crusader Scenario...")
    scenario_pages = find_section(pages, [
        r"crusader",
        r"operation crusader",
        r"november 1941",
        r"18 november",
    ])
    raw_text = "\n".join(p["text"] for p in scenario_pages[:10])
    return {
        "source_pages": [p["page"] for p in scenario_pages[:10]],
        "raw_text": raw_text[:8000],
    }


def build_section_index(pages: list[dict]) -> list[dict]:
    """Build an index of all rule sections with page numbers."""
    print("  Building section index...")
    index = []
    section_pat = re.compile(r"^(\d+\.\d+[\d\.]*)\s+([A-Z][^\n]{5,80})", re.MULTILINE)
    for p in pages:
        for m in section_pat.finditer(p["text"]):
            rule_num = m.group(1)
            title = m.group(2).strip()
            # Filter out obvious false positives (coordinates, dates, etc.)
            if len(title) > 5 and not re.match(r"^\d", title):
                index.append({
                    "rule": rule_num,
                    "title": title[:80],
                    "page": p["page"],
                })
    # Deduplicate by rule number, keep first occurrence
    seen = {}
    deduped = []
    for item in index:
        if item["rule"] not in seen:
            seen[item["rule"]] = True
            deduped.append(item)
    return deduped


def main():
    skip_download = "--skip-download" in sys.argv

    EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)

    # Download only if we don't already have the PDF locally
    if PDF_PATH.exists():
        print(f"Using PDF at {PDF_PATH} ({PDF_PATH.stat().st_size / 1_000_000:.1f} MB)")
    elif skip_download:
        print(f"ERROR: --skip-download specified but no PDF found at {PDF_PATH}")
        sys.exit(1)
    else:
        download_pdf()

    # Extract text
    pages = extract_text_with_fitz(PDF_PATH)
    save_full_text(pages)

    # Extract structured data
    print("\nExtracting structured rules data...")
    tables = {
        "_source": str(PDF_URL),
        "_extraction_note": "Extracted by tools/extract_pdf_tables.py using PyMuPDF. Raw text preserved for manual review.",
        "section_index": build_section_index(pages),
        "terrain_effects_chart": extract_terrain_effects(pages),
        "combat_results_table": extract_combat_results_table(pages),
        "sequence_of_play": extract_sequence_of_play(pages),
        "supply_rules": extract_supply_rules(pages),
        "stacking_rules": extract_stacking_limits(pages),
        "zoc_rules": extract_zoc_rules(pages),
        "pasta_rule": extract_pasta_rule(pages),
        "movement_rules": extract_movement_rules(pages),
        "scenario_crusader": extract_scenario_crusader(pages),
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(tables, f, indent=2, ensure_ascii=False)

    size_kb = OUTPUT_JSON.stat().st_size // 1024
    print(f"\nWritten: {OUTPUT_JSON} ({size_kb} KB)")
    print(f"Section index: {len(tables['section_index'])} entries")
    print(f"Full text: {RULES_TXT} ({RULES_TXT.stat().st_size // 1024} KB)")
    print("\nDone. Review data/extracted/rules_tables.json for quality.")


if __name__ == "__main__":
    main()
