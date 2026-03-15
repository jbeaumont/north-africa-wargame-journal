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
import time
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
'  return {"valid": true}.  Never emit {"valid": false} after concluding the move is valid.',
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


# ── Mechanical pre-check (no LLM needed) ──────────────────────────────────────

_MECHANICAL_VALID = {"valid": True}  # reusable approval sentinel


def mechanical_precheck(action: Dict[str, Any], context: Dict[str, Any]) -> Optional[dict]:
    """
    Deterministically reject OR approve actions without an LLM call.

    Returns:
      {"valid": False, ...}  — action is clearly illegal; skip arbiter.
      {"valid": True}        — action is clearly legal; skip arbiter.
      None                   — ambiguous; send to LLM arbiter.

    For moves, a definitive approval is issued when the unit starts outside
    any enemy ZOC (zoc_status == "none") and all deterministic checks pass.
    Units that start IN enemy ZOC (zoc_status "contact" or "engaged") still
    need the LLM to verify the ZOC exit cost and ZOC-to-ZOC rule (8.15).

    Rules checked (MOVE)
    --------------------
      (engine)   — cost 999 = non-adjacent or impassable; reject
      rule 6.13  — total_cp_cost must not exceed cp_remaining
      rule 8.13  — destination must not be enemy-occupied
      rule 8.14  — path may not continue past a ZOC hex
      rule 8.15  — ZOC-to-ZOC: unit in ZOC may not move directly to another ZOC hex
      rule 8.17  — non-motorized units (CPA ≤ 10) may not exceed 150% of CPA
      rule 9.4   — stacking: destination must have room for the moving unit

    Mechanical APPROVAL (MOVE): issued when zoc_status == "none" and all
    checks above pass.  At that point the LLM has nothing left to reason about.

    Rules checked (COMBAT)
    ----------------------
      rule 11.2  — attacker must be adjacent to defender
      rule 11.3  — attacker must have sufficient CP remaining

    Mechanical APPROVAL (COMBAT): issued when attacker is outside enemy ZOC
    (zoc_status == "none") and all CP/adjacency checks pass.  Units in enemy
    ZOC must still be checked for the "must attack ZOC-exerting unit" constraint
    (rule 10.31).
    """
    action_type = action.get("action", "")

    if action_type == "move":
        unit_ctx     = context.get("unit", {})
        path         = context.get("path", [])
        hex_costs    = context.get("path_hex_costs", {})
        total_cost   = context.get("total_cp_cost", 0.0)
        cp_remain    = float(unit_ctx.get("cp_remaining", 0))
        zoc_hexes    = set(context.get("zoc_hexes", []))
        enemy_occ    = set(context.get("enemy_occupied_hexes", []))
        zoc_status   = unit_ctx.get("zoc_status", "none")
        cpa          = float(unit_ctx.get("cpa", 0))
        is_motorized = bool(unit_ctx.get("is_motorized", True))
        stacking_in_dest = float(context.get("stacking_in_destination", 0))
        stacking_limit   = float(context.get("stacking_limit", 6))
        unit_sp          = float(context.get("unit_stacking_points", 1))

        # ── Rejection checks (always run) ─────────────────────────────────────

        # Any 999-cost hop is non-adjacent or impassable (engine guarantee)
        for hx, cost in hex_costs.items():
            if cost >= 999:
                return {
                    "valid": False,
                    "reason": f"Hex {hx} is non-adjacent or impassable (cost 999).",
                    "rule_ref": "8.13",
                }

        # Rule 6.13: total CP cost must not exceed remaining CP
        if total_cost > cp_remain:
            return {
                "valid": False,
                "reason": (
                    f"Total CP cost {total_cost} exceeds unit's remaining CP {cp_remain}."
                ),
                "rule_ref": "6.13",
            }

        # Rule 8.13: cannot enter an enemy-occupied hex
        dest = path[-1] if path else None
        if dest and dest in enemy_occ:
            return {
                "valid": False,
                "reason": f"Destination {dest} is occupied by an enemy unit (rule 8.13).",
                "rule_ref": "8.13",
            }

        # Rule 8.14: ZOC stop — path may not continue past the first ZOC hex
        hexes_entered = path[1:]  # exclude origin
        for hx in hexes_entered[:-1]:  # all but the last entered hex
            if hx in zoc_hexes:
                return {
                    "valid": False,
                    "reason": (
                        f"Path continues past ZOC hex {hx} — unit must stop immediately "
                        f"upon entering an enemy ZOC hex (rule 8.14)."
                    ),
                    "rule_ref": "8.14",
                }

        # Rule 8.15: ZOC-to-ZOC — unit starting in enemy ZOC may not move
        # directly to another enemy ZOC hex (rule 8.15 / 10.23)
        if zoc_status != "none" and dest and dest in zoc_hexes:
            return {
                "valid": False,
                "reason": (
                    f"Unit is in enemy ZOC and destination {dest} is also in enemy ZOC. "
                    f"A unit may not voluntarily move from one enemy ZOC hex into another "
                    f"(rule 8.15)."
                ),
                "rule_ref": "8.15",
            }

        # Rule 8.17: non-motorized units (CPA ≤ 10) may not voluntarily
        # exceed 150% of their base CPA
        if not is_motorized and cpa <= 10 and total_cost > 1.5 * cpa:
            return {
                "valid": False,
                "reason": (
                    f"Non-motorized unit (CPA {cpa}) may not exceed 150% of CPA "
                    f"({1.5 * cpa} CP max); proposed cost is {total_cost} CP (rule 8.17)."
                ),
                "rule_ref": "8.17",
            }

        # Rule 9.4: stacking — destination hex must have room for the moving unit
        if unit_sp > 0 and stacking_in_dest + unit_sp > stacking_limit:
            return {
                "valid": False,
                "reason": (
                    f"Destination {dest} already has {stacking_in_dest} SP occupied "
                    f"(limit {stacking_limit}); moving unit adds {unit_sp} SP (rule 9.4)."
                ),
                "rule_ref": "9.4",
            }

        # ── Mechanical approval ───────────────────────────────────────────────
        # If the unit starts OUTSIDE enemy ZOC all deterministic checks have
        # been satisfied.  The LLM has nothing left to reason about.
        if zoc_status == "none":
            return _MECHANICAL_VALID

        # Unit is in enemy ZOC: ZOC exit cost and ZOC-to-ZOC exception (10.24)
        # require LLM reasoning.  Fall through to the arbiter.
        return None

    if action_type == "combat":
        attacker_ctx   = context.get("attacker", {})
        adjacent       = context.get("adjacent", False)
        cp_cost        = float(context.get("attacker_cp_cost", 10))
        cp_remain      = float(context.get("attacker_cp_remaining", 0))
        zoc_status     = attacker_ctx.get("zoc_status", "none")

        if not adjacent:
            return {
                "valid": False,
                "reason": "Attacker is not adjacent to defender.",
                "rule_ref": "11.2",
            }

        if cp_remain < cp_cost:
            return {
                "valid": False,
                "reason": (
                    f"Attacker has only {cp_remain} CP remaining but combat costs {cp_cost} CP."
                ),
                "rule_ref": "11.3",
            }

        # Mechanical approval: if attacker is outside enemy ZOC, rule 10.31
        # (must attack ZOC-exerting unit) does not apply.
        if zoc_status == "none":
            return _MECHANICAL_VALID

        # Attacker is in enemy ZOC: need LLM to check rule 10.31
        return None

    return None  # unknown type: let arbiter handle


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

    # Retry up to 2 extra times on parse failure or transient API error.
    # Parse failures are rare but occur under parallel load (garbled JSON).
    # Exponential backoff: 1s, 2s.
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        if attempt:
            time.sleep(2 ** (attempt - 1))  # 1s, 2s
        try:
            # Use streaming with adaptive thinking; collect the final message.
            # Opus + adaptive thinking is used here because the remaining
            # actions that reach the arbiter (after mechanical_precheck) are
            # genuine edge cases requiring real rules reasoning.
            with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=1024,
                thinking={"type": "adaptive"},
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
                last_exc = ValueError("No text block in arbiter response")
                continue

            verdict = _parse_verdict(text_blocks[0].text)
            # _parse_verdict returns _FALLBACK_INVALID on parse failure;
            # check for that sentinel and retry rather than accepting a
            # spurious rejection.
            if verdict is _FALLBACK_INVALID:
                last_exc = ValueError("Arbiter response could not be parsed as JSON")
                continue

            return verdict

        except anthropic.APIError as exc:
            last_exc = exc
            continue

    if isinstance(last_exc, anthropic.APIError):
        return {**_FALLBACK_ERROR, "reason": f"Arbiter API error: {last_exc}"}
    return _FALLBACK_INVALID
