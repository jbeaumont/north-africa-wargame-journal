"""
Rules Arbiter Agent — validates proposed actions against the CNA rulebook.

Stateless Claude API call. No memory, no side effects.

The arbiter is the ONLY agent that reads the rulebook. It receives a
proposed action and pre-computed context from the Board State engine,
and returns a verdict:

    {"valid": True}
or
    {"valid": False, "reason": "...", "rule_ref": "8.14"}

The arbiter must NOT recalculate context. All values (CP costs, ZOC
status, stacking counts, supply status) are pre-computed by the engine
and injected via the `context` argument.

Action types supported: "move", "combat"

Context format
--------------
For "move":
    {
        "unit": {
            "id": "cw-7th-armoured-001",
            "name": "7th Armoured Div",
            "cpa": 30,
            "cp_remaining": 30,
            "side": "commonwealth",
            "hex_id": "A0101",
            "supply_status": "in_supply",
            "breakdown_points": 0,
            "is_motorized": True,
            "zoc_status": "none"     # "none" | "contact" | "engaged"
        },
        "path": ["A0101", "A0102", "A0103"],
        "path_hex_costs": {"A0102": 4, "A0103": 4},  # CP cost to enter each hex
        "total_cp_cost": 8,                           # sum including ZOC exit cost
        "zoc_hexes": ["A0102"],                       # hexes with active enemy ZOC
        "enemy_occupied_hexes": ["A0105"],
        "stacking_in_destination": 3,
        "stacking_limit": 20,
        "weather": "normal",
        "context": "voluntary"   # "voluntary" | "reaction" | "retreat"
    }

For "combat":
    {
        "attacker": { <same fields as unit above> },
        "defender": { "id": ..., "hex_id": ..., "supply_status": ... },
        "combat_type": "close_assault" | "barrage" | "anti_armor",
        "attacker_cp_cost": 10,
        "attacker_cp_remaining": 20,
        "adjacent": True,
        "terrain": "open",
        "defender_in_supply": True,
        "weather": "normal"
    }

Model
-----
claude-opus-4-6 with adaptive thinking. Streaming for reliability.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import anthropic
from src.agents._client import make_client

# ── Paths ─────────────────────────────────────────────────────────────────────

_RULES_TEXT   = Path(__file__).parent.parent.parent / "data" / "rules" / "cna_rules.txt"
_RULES_TABLES = Path(__file__).parent.parent.parent / "data" / "extracted" / "rules_tables.json"

# ── Rules loading ──────────────────────────────────────────────────────────────

def _load_rules_tables() -> dict:
    with open(_RULES_TABLES) as f:
        return json.load(f)


def _load_rules_text() -> str:
    with open(_RULES_TEXT) as f:
        return f.read()


# Cache at module level (loaded once per process)
_RULES_TABLES_CACHE: Optional[dict] = None
_RULES_TEXT_CACHE: Optional[str] = None


def _rules_tables() -> dict:
    global _RULES_TABLES_CACHE
    if _RULES_TABLES_CACHE is None:
        _RULES_TABLES_CACHE = _load_rules_tables()
    return _RULES_TABLES_CACHE


def _rules_text() -> str:
    global _RULES_TEXT_CACHE
    if _RULES_TEXT_CACHE is None:
        _RULES_TEXT_CACHE = _load_rules_text()
    return _RULES_TEXT_CACHE


# ── Rule text extraction ───────────────────────────────────────────────────────

def extract_rule_text(rule_num: str, chars: int = 600) -> str:
    """
    Extract the raw text for a specific rule number from cna_rules.txt.

    The OCR'd PDF uses bracket-style markers: [8.14], {10.21}, (6.13), etc.
    Returns up to `chars` characters starting from the rule marker, or an
    empty string if the rule is not found.
    """
    text = _rules_text()
    pattern = re.compile(
        rf'[\[\{{\(]{re.escape(rule_num)}[\]\}}\)]',
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if not m:
        return ""
    start = m.start()
    snippet = text[start : start + chars]
    # Collapse excessive whitespace from OCR line breaks
    snippet = re.sub(r'[ \t]*\n[ \t]*', ' ', snippet)
    snippet = re.sub(r'  +', ' ', snippet)
    return snippet.strip()


def extract_rules_for_action(action_type: str) -> str:
    """
    Return a compact block of raw rule text relevant to the action type.
    """
    if action_type == "move":
        rule_nums = [
            "6.13", "6.16", "6.21",          # CPA and DP rules
            "8.11", "8.12", "8.13", "8.14",  # movement basics + ZOC stop
            "8.15", "8.16", "8.17",          # ZOC exit, overrun CPA, non-motorized cap
            "8.42",                           # escarpment/track downward only
            "10.1", "10.21", "10.22",         # ZOC exertion and blocking
            "10.23", "10.25", "10.26",        # ZOC movement rules
        ]
    elif action_type == "combat":
        rule_nums = [
            "11.2", "11.3",                  # CP cost for combat
            "12.1", "12.3",                  # barrage
            "13.0",                          # retreat before assault
            "15.0", "15.1",                  # close assault
            "10.31", "10.32",               # ZOC combat requirements (holding off)
        ]
    else:
        rule_nums = []

    parts = []
    for rn in rule_nums:
        text = extract_rule_text(rn, chars=500)
        if text:
            parts.append(text)

    return "\n\n".join(parts)


# ── System prompt ──────────────────────────────────────────────────────────────

def _build_system_prompt(action_type: str) -> str:
    tables = _rules_tables()

    # Select only the relevant sub-sections to keep the prompt focused
    if action_type == "move":
        relevant_keys = [
            "capability_point_system",
            "movement_rules",
            "zoc_rules",
            "stacking_rules",
            "terrain_effects_chart",
        ]
    elif action_type == "combat":
        relevant_keys = [
            "capability_point_system",
            "combat_system",
            "zoc_rules",
            "terrain_effects_chart",
        ]
    else:
        relevant_keys = list(tables.keys())

    selected_tables = {k: tables[k] for k in relevant_keys if k in tables}
    raw_rule_excerpts = extract_rules_for_action(action_type)

    return f"""You are the Rules Arbiter for The Campaign for North Africa (SPI, 1979).

Your sole job is to decide whether a proposed {action_type} action is LEGAL under
the rules. You receive:
  1. The proposed action.
  2. Pre-computed context from the deterministic game engine. You must trust
     these values and must NOT recalculate them yourself.

OUTPUT FORMAT (return ONLY valid JSON, nothing else):
  If legal:   {{"valid": true}}
  If illegal: {{"valid": false, "reason": "<concise explanation>", "rule_ref": "<rule number>"}}

The "rule_ref" field must cite the specific rule number from the rulebook that
makes the action illegal (e.g. "8.14", "10.23").

---

STRUCTURED RULES (extracted from official rulebook):

{json.dumps(selected_tables, indent=2)}

---

RAW RULE TEXT EXCERPTS (from OCR'd PDF — may have OCR artifacts):

{raw_rule_excerpts}

---

VALIDATION GUIDELINES for {action_type}:

{"MOVE:" + chr(10) + chr(10).join([
"- A unit may never enter a hex containing an enemy unit (rule 8.13).",
"- ZOC STOP (rule 8.14): if a hex in the path is in `zoc_hexes`, the unit stops",
"  IMMEDIATELY upon entering it.  A path that ENDS at a ZOC hex is LEGAL — the",
"  unit simply halts there.  Only reject if there are additional hexes in the path",
"  AFTER the ZOC hex (i.e. the unit would continue past where it must stop).",
"- To exit an enemy ZOC: 2 CP if in Contact, 4 CP if Engaged (rule 8.15).",
"- A unit may NOT voluntarily exit a ZOC directly into another enemy ZOC unless rule 10.24 applies.",
"- Total CP cost (including ZOC exit cost) must not push breakdown points beyond the unit's breakdown rating before checking.",
"- Non-motorized units (CPA ≤ 10) may not voluntarily exceed 150% of base CPA (rule 8.17).",
"- No vehicle may move UP an escarpment (rule 8.42).",
"- The engine has pre-computed total_cp_cost AND path_hex_costs. Trust them.",
"  A hex cost of 999 means non-adjacent or impassable.  If ANY hex cost is 999, reject.",
"  Otherwise, trust the engine values — do NOT re-derive costs from terrain names.",
"- IMPORTANT: if your step-by-step analysis concludes that every rule is satisfied,",
"  return {\"valid\": true}.  Never emit {\"valid\": false} after concluding the move is valid.",
]) if action_type == "move" else ""}

{"COMBAT:" + chr(10) + chr(10).join([
"- Attacker must be adjacent to defender (engine sets adjacent=True/False).",
"- Barrage range: artillery must be within range (engine pre-computes).",
"- CP cost for combat is deducted from attacker's remaining CPs.",
"- If attacker has insufficient CPs and is non-motorized, the attack may be invalid.",
"- Anti-armor fire: only units assigned to anti-armor role can perform it.",
"- ZOC combat requirements: units in enemy ZOC must attack that unit (rule 10.31).",
]) if action_type == "combat" else ""}

Do not add commentary outside the JSON. Think carefully using the rules above, then
output only the JSON verdict."""


# ── Validation ─────────────────────────────────────────────────────────────────

_FALLBACK_INVALID = {"valid": False, "reason": "Arbiter response could not be parsed as JSON.", "rule_ref": ""}
_FALLBACK_ERROR   = {"valid": False, "reason": "Arbiter API call failed.", "rule_ref": ""}


def _parse_verdict(text: str) -> dict:
    """
    Extract the JSON verdict from the model's response text.

    Handles cases where the model wraps the JSON in a code block.
    """
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from a markdown code block
    code_block = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if code_block:
        try:
            return json.loads(code_block.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding a bare JSON object anywhere in the text
    obj = re.search(r'\{[^{}]*"valid"[^{}]*\}', text, re.DOTALL)
    if obj:
        try:
            return json.loads(obj.group(0))
        except json.JSONDecodeError:
            pass

    return _FALLBACK_INVALID


def validate_action(
    action: Dict[str, Any],
    context: Dict[str, Any],
    *,
    client: Optional[anthropic.Anthropic] = None,
) -> dict:
    """
    Validate a proposed action against the CNA rulebook.

    Parameters
    ----------
    action  : The action dict from the player agent
              (must have an "action" key: "move" or "combat").
    context : Pre-computed engine context (CP costs, ZOC status, etc.).
              The arbiter trusts these values and does not recalculate.
    client  : Optional anthropic.Anthropic client. Created from
              ANTHROPIC_API_KEY env var if not provided.

    Returns
    -------
    {"valid": True}
    or
    {"valid": False, "reason": "...", "rule_ref": "8.14"}
    """
    if client is None:
        client = make_client()

    action_type = action.get("action", "")
    if action_type not in ("move", "combat"):
        return {
            "valid": False,
            "reason": f"Unknown action type '{action_type}'. Arbiter only validates 'move' and 'combat'.",
            "rule_ref": "",
        }

    system_prompt = _build_system_prompt(action_type)
    user_message = (
        f"Validate this proposed {action_type} action:\n\n"
        f"ACTION:\n{json.dumps(action, indent=2)}\n\n"
        f"PRE-COMPUTED CONTEXT (engine-computed — trust these values):\n"
        f"{json.dumps(context, indent=2)}\n\n"
        "Respond with ONLY JSON. No text outside the JSON object."
    )

    try:
        # Use streaming with adaptive thinking; collect the final message
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    # Cache the large static system prompt across calls
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            response = stream.get_final_message()

        # Extract the text content block (thinking blocks are separate)
        text_blocks = [b for b in response.content if b.type == "text"]
        if not text_blocks:
            return _FALLBACK_INVALID

        return _parse_verdict(text_blocks[0].text)

    except anthropic.APIError as exc:
        return {**_FALLBACK_ERROR, "reason": f"Arbiter API error: {exc}"}
