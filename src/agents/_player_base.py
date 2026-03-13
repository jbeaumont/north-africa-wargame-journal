"""
Shared base class for CNA Player Agents (Allied and Axis).

Each agent:
  - Makes one Claude API call per OpStage per side.
  - Receives fog-of-war state + narrative summary as context.
  - Reads/writes persistent memory files (strategy + rules mastered).
  - Returns a list of action dicts for the BoardStateAgent dispatcher.

Model: claude-opus-4-6 with adaptive thinking, streaming.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic
from src.agents._client import make_client

from src.models.game_state import GameState, Side

# ── Paths ──────────────────────────────────────────────────────────────────────

_MEMORY_DIR = Path(__file__).parent.parent.parent / "memory"

# ── Action schema shown to the agent ──────────────────────────────────────────

_ACTION_SCHEMA = """
## Action Schema

Each element of the `actions` array must be one of:

### Move
```json
{
  "action": "move",
  "unit_id": "<unit id string>",
  "path": ["<from_hex>", "<hex2>", ..., "<dest_hex>"]
}
```
Rules:
- path must start at the unit's current hex.
- Total CP cost of all hexes entered must not exceed the unit's CPA.
- Path must not pass through enemy-occupied hexes (unless attacking).
- Motorized units cost ½ CP per hex on road hexsides.

### Combat (Close Assault)
```json
{
  "action": "combat",
  "attacker_id": "<unit id>",
  "defender_id": "<unit id>",
  "combat_type": "close_assault",
  "attacker_cp_cost": <integer>
}
```
Rules:
- Attacker must be adjacent to defender.
- Attacker must have sufficient CP remaining.
- Use combat_type "barrage" for artillery bombardment, "anti_armor" for AT fire.

### No action
If you have nothing useful to do this OpStage, return an empty actions list.
""".strip()


# ── Base class ─────────────────────────────────────────────────────────────────

class PlayerAgent:
    """
    Base class for CNA player agents.

    Subclasses must implement:
      _commander_name(gs)  — return the historical commander's name
      _personality()       — return personality paragraph for system prompt
    """

    def __init__(self, side: Side, memory_dir: Optional[Path] = None) -> None:
        self.side = side
        self.memory_dir = memory_dir or _MEMORY_DIR
        self._client = make_client()

    # ── Public interface ───────────────────────────────────────────────────────

    def propose_actions(self, gs: GameState) -> List[Dict[str, Any]]:
        """
        Make a single Claude call and return a list of action dicts.

        Each action dict matches the BoardStateAgent dispatcher schema.
        Actions will be validated by the Rules Arbiter before execution.
        """
        system = self._system_prompt(gs)
        user   = self._user_message(gs)

        with self._client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = stream.get_final_message()

        result = self._parse_response(msg)
        strategy_note = result.get("strategy_note", "")
        if strategy_note:
            self._append_strategy(strategy_note, gs)

        return result.get("actions", [])

    # ── Subclass hooks ─────────────────────────────────────────────────────────

    def _commander_name(self, gs: GameState) -> str:  # override in subclass
        return self.side.value.title() + " Commander"

    def _personality(self) -> str:  # override in subclass
        return ""

    # ── Prompt construction ────────────────────────────────────────────────────

    def _system_prompt(self, gs: GameState) -> list:
        """
        Static system prompt (cacheable via prompt-caching).

        Split into two blocks so the large static block is cached:
          Block 1 (cached): personality, action schema, output format.
          Block 2 (not cached): commander name with turn context (changes
            when Cunningham hands off to Ritchie at turn 61).

        Kept as a list of TextBlockParam dicts for the messages API.
        """
        static_body = f"""
{self._personality()}

You are commanding forces in the Western Desert, North Africa (1941-1943).
This is a simulation of *The Campaign for North Africa* (SPI, 1979).

## Your Objective
Propose tactical actions for your forces this Operations Stage.
Think carefully about supply lines, terrain, and enemy contact before
committing to moves. Your actions will be validated by a Rules Arbiter —
rejected actions waste your commander's time.

{_ACTION_SCHEMA}

## Output Format
Respond with ONLY a JSON code block containing:
```json
{{
  "actions": [ ...action dicts... ],
  "strategy_note": "1–2 sentence note on your plan for the record"
}}
```
No text outside the JSON block. If you have nothing to do, use `"actions": []`.
""".strip()

        return [
            {
                "type": "text",
                "text": static_body,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"You are {self._commander_name(gs)}, "
                        f"Turn {gs.turn} / OpStage {gs.opstage}.",
            },
        ]

    def _user_message(self, gs: GameState) -> str:
        fow    = gs.fog_of_war(self.side)
        units  = fow.get("units", {})
        contacts = fow.get("contact_hexes", [])

        # ── Own units table ────────────────────────────────────────────────────
        rows = []
        for uid, u in sorted(units.items(), key=lambda x: x[1].get("name", "")):
            if u.get("side") != self.side.value:
                continue
            rows.append(
                f"| {uid:<30} | {u.get('name',''):<28} | "
                f"{u.get('hex_id','?'):<6} | "
                f"{u.get('cpa',0):>3}/{u.get('cp_remaining',0):<3} | "
                f"{u.get('supply_status','?'):<12} | "
                f"{'mot' if u.get('motorized') else 'foot'}"
            )
        unit_table = (
            "| Unit ID                         | Name                         | "
            "Hex    | CPA/CP | Supply       | Type\n"
            "|---------------------------------|------------------------------|"
            "--------|--------|--------------|-----\n"
            + "\n".join(rows) if rows else "(no active units)"
        )

        # ── Memory files ───────────────────────────────────────────────────────
        strategy_path = self.memory_dir / f"{self.side.value}_strategy.md"
        rules_path    = self.memory_dir / f"{self.side.value}_rules_mastered.md"
        strategy_text = strategy_path.read_text() if strategy_path.exists() else "(none yet)"
        rules_text    = rules_path.read_text()    if rules_path.exists()     else "(none yet)"

        # ── Narrative summary ─────────────────────────────────────────────────
        narrative = gs.narrative_summary()

        contact_str = ", ".join(contacts) if contacts else "(none)"

        return f"""## Situation Report — {gs.historical_date_str()}
Turn {gs.turn} / OpStage {gs.opstage} / Weather: {gs.weather}

{narrative}

## Your Units
{unit_table}

## Enemy Contact
Enemy presence confirmed at hexes: {contact_str}
(Type and strength unknown — rule 16 fog of war)

## Strategic Memory
### Previous Strategy Notes
{strategy_text}

### Rules Mastered (from Arbiter rejections)
{rules_text}

---
Propose your actions for this OpStage. Remember: conserve supply, respect ZOC,
and do not enter enemy-occupied hexes without attacking.
"""

    # ── Response parsing ───────────────────────────────────────────────────────

    def _parse_response(self, msg: anthropic.types.Message) -> Dict[str, Any]:
        """
        Extract the JSON object from Claude's response text.

        Tries to find a ```json ... ``` block first, then falls back to the
        outermost {...} in the response. Returns empty dict on failure.
        """
        text = next((b.text for b in msg.content if b.type == "text"), "")

        # Try fenced code block
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # Fall back to first outermost brace match
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass

        return {"actions": [], "strategy_note": ""}

    # ── Memory helpers ─────────────────────────────────────────────────────────

    def _append_strategy(self, note: str, gs: GameState) -> None:
        """Append a strategy note to this side's strategy memory file."""
        self.memory_dir.mkdir(exist_ok=True)
        path = self.memory_dir / f"{self.side.value}_strategy.md"
        with open(path, "a") as f:
            f.write(
                f"\n### Turn {gs.turn} / OpStage {gs.opstage}"
                f" — {gs.historical_date_str()}\n"
                f"{note}\n"
            )

    def append_rules_learned(self, rule_ref: str, note: str, gs: GameState) -> None:
        """
        Called by the engine when the Rules Arbiter rejects an action.
        Appends the corrected rule understanding to the rules_mastered file.
        """
        self.memory_dir.mkdir(exist_ok=True)
        path = self.memory_dir / f"{self.side.value}_rules_mastered.md"
        with open(path, "a") as f:
            f.write(
                f"\n### Rule {rule_ref} — Turn {gs.turn} / OpStage {gs.opstage}\n"
                f"{note}\n"
            )
