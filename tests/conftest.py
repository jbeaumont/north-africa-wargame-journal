"""
pytest configuration and fixture-loading helpers.

Fixtures live in tests/fixtures/*.json.  Each file contains a top-level
"cases" list.  Test modules load them with load_fixture() and iterate.

Fixture file schema (all files)
--------------------------------
{
  "rule":        "49.3",            -- rule number (string)
  "description": "...",            -- human-readable summary
  "cases": [
    {
      "id":          "snake_case",  -- unique within file
      "description": "...",        -- what this case tests
      "rule_clause": "...",        -- exact PDF text being exercised
      "inputs":  { ... },          -- engine inputs (schema varies by rule)
      "expected": { ... }          -- expected engine outputs
    }
  ]
}

The test runner imports load_fixture(name) to get the cases list, then
parametrises pytest with pytest.mark.parametrize("case", cases).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> list[dict[str, Any]]:
    """
    Load all cases from tests/fixtures/<name>.json.

    name: filename without the .json suffix, e.g. "fuel_evaporation_49_3"
    """
    path = FIXTURE_DIR / f"{name}.json"
    with path.open() as f:
        data = json.load(f)
    return data["cases"]


def case_id(case: dict) -> str:
    """pytest parametrize id: prefer case["id"], fall back to case["description"]."""
    return case.get("id") or case.get("description", "unknown")
