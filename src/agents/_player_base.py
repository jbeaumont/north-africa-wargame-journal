"""
Shared base class for CNA Player Agents (Allied and Axis).

Each agent:
  - Makes one Claude API call per OpStage per side.
  - Receives fog-of-war state + narrative summary as context.
  - Reads/writes persistent memory files (strategy + rules mastered).
  - Returns a list of action dicts for the BoardStateAgent dispatcher.

Model: claude-sonnet-4-6, streaming.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic
from src.agents._client import make_client

from src.engine.hex_map import HexMap
from src.models.game_state import GameState, Side
from src.models.hex import (
    Terrain, HexsideFeature, DIRECTIONS,
    _ODD_DELTAS, _EVEN_DELTAS, _SECTION_ORDER, _MAX_COL, _MAX_ROW,
)

log = logging.getLogger("cna")


def _hex_neighbor(hex_id: str, direction: str) -> Optional[str]:
    """Return the hex_id of the neighbour in *direction* from *hex_id*, or None if off-map."""
    sec = hex_id[0]
    col = int(hex_id[1:3])
    row = int(hex_id[3:5])
    idx = DIRECTIONS.index(direction)
    deltas = _ODD_DELTAS if col % 2 == 1 else _EVEN_DELTAS
    dc, dr = deltas[idx]
    new_col = col + dc
    new_row = row + dr
    sec_idx = _SECTION_ORDER.index(sec)
    if new_col < 1:
        sec_idx -= 1
        if sec_idx < 0:
            return None
        new_col = _MAX_COL
    elif new_col > _MAX_COL:
        sec_idx += 1
        if sec_idx >= len(_SECTION_ORDER):
            return None
        new_col = 1
    if new_row < 1 or new_row > _MAX_ROW:
        return None
    return f"{_SECTION_ORDER[sec_idx]}{new_col:02d}{new_row:02d}"

# ── Paths ──────────────────────────────────────────────────────────────────────

_MEMORY_DIR  = Path(__file__).parent.parent.parent / "memory"
_TABLES_PATH = Path(__file__).parent.parent.parent / "data" / "extracted" / "rules_tables.json"

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
- Every consecutive pair must be ADJACENT (appear as neighbors in the grid above).
  A two-row jump like C3424→C3426 is NOT adjacent and costs 999 CP.
  Going from C3424 south two hexes requires two steps: C3424→C3425→C3426.
- Total CP cost of all hexes entered must not exceed the unit's CPA.
- Path must not pass through enemy-occupied hexes (unless attacking).
- Motorized units cost ½ CP per hex on road hexsides.
- ZOC STOP (rule 8.14): if any hex in the path is in enemy ZOC, the unit
  STOPS IMMEDIATELY there. Do NOT include further hexes in the path beyond
  the first ZOC hex — those steps will be rejected. Check "Enemy ZOC Hexes"
  below before writing any path.

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
        self._tec: Optional[dict] = None  # lazy-loaded TEC for HexMap

    def _hex_map(self, gs: GameState) -> HexMap:
        """Build (or rebuild) a HexMap for the given GameState's hexes."""
        if self._tec is None:
            with open(_TABLES_PATH) as f:
                tables = json.load(f)
            self._tec = tables["terrain_effects_chart"]["terrain_types"]
        return HexMap(gs.hexes, self._tec)

    # ── Public interface ───────────────────────────────────────────────────────

    def propose_actions(self, gs: GameState) -> List[Dict[str, Any]]:
        """
        Make a single Claude call and return a list of action dicts.

        Each action dict matches the BoardStateAgent dispatcher schema.
        Actions will be validated by the Rules Arbiter before execution.
        Retries up to 3 times on transient network errors.
        """
        import time
        system = self._system_prompt(gs)
        user   = self._user_message(gs)

        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(3):
            if attempt:
                time.sleep(2 ** attempt)
            try:
                with self._client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=8192,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                ) as stream:
                    msg = stream.get_final_message()
                break  # success
            except Exception as exc:
                last_exc = exc
                continue
        else:
            raise last_exc

        result = self._parse_response(msg)
        strategy_note = result.get("strategy_note", "")
        if strategy_note:
            self._append_strategy(strategy_note, gs)

        actions = result.get("actions", [])
        if not actions:
            # Log the raw text so we can tell whether the model deliberately
            # returned [] or whether JSON parsing silently failed.
            raw_text = next(
                (b.text for b in msg.content if b.type == "text"), ""
            )
            log.warning(
                "[%s] 0 actions — raw response (first 600 chars):\n%s",
                self.side.value,
                raw_text[:600],
            )
        return actions

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
        dumps  = fow.get("supply_dumps", {})

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

        # ── Supply dumps table ────────────────────────────────────────────────
        dump_rows = []
        for d in sorted(dumps.values(), key=lambda x: x.get("label", "")):
            if d.get("is_dummy"):
                continue
            fuel  = "unlimited" if d.get("is_unlimited") else f"{d.get('fuel',0):.0f}"
            dump_rows.append(
                f"  {d.get('label') or d.get('id','?'):<22} hex {d.get('hex_id','?'):<6}  fuel:{fuel}"
            )
        dump_str = "\n".join(dump_rows) if dump_rows else "  (none)"

        # ── Memory files ───────────────────────────────────────────────────────
        strategy_path = self.memory_dir / f"{self.side.value}_strategy.md"
        rules_path    = self.memory_dir / f"{self.side.value}_rules_mastered.md"
        strategy_text = strategy_path.read_text() if strategy_path.exists() else "(none yet)"
        rules_text    = rules_path.read_text()    if rules_path.exists()     else "(none yet)"

        # ── Narrative summary ─────────────────────────────────────────────────
        narrative = gs.narrative_summary()

        contact_str = ", ".join(contacts) if contacts else "(none)"

        # ── Impassable hex list ───────────────────────────────────────────────
        # Derive from gs.hexes: Swamp is impassable for everyone; Salt Marsh is
        # impassable for motorized units. List them so agents don't waste actions
        # trying to route through them.
        impassable_all = sorted(
            hid for hid, h in gs.hexes.items()
            if h.terrain == Terrain.SWAMP
        )
        impassable_mot = sorted(
            hid for hid, h in gs.hexes.items()
            if h.terrain == Terrain.SALT_MARSH
        )
        # Escarpment-UP hexsides: impassable for motorized units (rule 8.42).
        # Format: "FROM → TO" so agents can avoid the specific crossing.
        escarpment_crossings: list[str] = []
        for hid, h in gs.hexes.items():
            for direction, feature in h.hexsides.items():
                if feature == HexsideFeature.ESCARPMENT_UP:
                    neighbor = _hex_neighbor(hid, direction)
                    if neighbor:
                        escarpment_crossings.append(f"{hid}→{neighbor}")
        escarpment_crossings.sort()

        impassable_parts = []
        if impassable_all:
            impassable_parts.append(
                f"Impassable for ALL units (Swamp): {', '.join(impassable_all)}"
            )
        if impassable_mot:
            impassable_parts.append(
                f"Impassable for MOTORIZED units (Salt Marsh): {', '.join(impassable_mot)}"
            )
        if escarpment_crossings:
            impassable_parts.append(
                "Impassable for MOTORIZED units (escarpment UP — rule 8.42):\n  "
                + ", ".join(escarpment_crossings)
            )
        impassable_str = (
            "\n".join(impassable_parts)
            if impassable_parts
            else "  (none known in loaded map area)"
        )

        # ── Enemy ZOC hexes ───────────────────────────────────────────────────
        # Rule 8.14: a unit entering an enemy ZOC hex must STOP IMMEDIATELY.
        # Pre-compute the ZOC set so agents can see exactly which hexes are
        # "stop hexes" before writing any path.
        enemy_side = Side.AXIS if self.side == Side.COMMONWEALTH else Side.COMMONWEALTH
        hm = self._hex_map(gs)
        enemy_units_list = [
            u for u in gs.units.values()
            if u.side == enemy_side and u.is_active()
        ]
        zoc_set = hm.zoc_hexes(enemy_side, enemy_units_list)
        if zoc_set:
            zoc_str = ", ".join(sorted(zoc_set))
        else:
            zoc_str = "(none)"

        # ── Per-unit legal next-hex list ──────────────────────────────────────
        # Rather than exposing the raw offset algebra (confusing), pre-compute
        # the 6 actual neighbors for every active friendly unit's current hex.
        # Agents simply pick the best neighbor; they do NOT need to derive
        # coordinates themselves.
        own_side_units_adj: list[str] = []
        for u in sorted(gs.units.values(), key=lambda x: x.id):
            if u.side != self.side or not u.is_active() or not u.hex_id:
                continue
            nbrs = sorted(
                v for v in hm.neighbors_by_direction(u.hex_id).values()
                if v is not None
            )
            own_side_units_adj.append(
                f"  {u.id} (at {u.hex_id}): neighbors = {', '.join(nbrs)}"
            )
        adjacency_str = "\n".join(own_side_units_adj) if own_side_units_adj else "  (none)"

        # ── OOS guidance ──────────────────────────────────────────────────────
        own_active = [u for u in units.values() if u.get("side") == self.side.value]
        n_oos = sum(1 for u in own_active if u.get("supply_status") != "in_supply")
        oos_note = ""
        if n_oos > 0:
            oos_note = f"""
## IMPORTANT: Out-of-Supply Guidance ({n_oos}/{len(own_active)} units OOS)
Out-of-supply units are NOT immobile — you MUST still propose actions:
- Move OOS units TOWARD your nearest supply dump (listed above) to restore supply.
- OOS units may still ATTACK enemy units, especially other OOS enemies (equal footing).
- Staying stationary accomplishes nothing. Every OpStage you fail to move is wasted.
- Do NOT return an empty actions list just because units are out of supply.
Supply dumps you can move toward are listed in "Your Supply Dumps" above.
"""

        return f"""## Situation Report — {gs.historical_date_str()}
Turn {gs.turn} / OpStage {gs.opstage} / Weather: {gs.weather}

{narrative}
{oos_note}
## Your Units
{unit_table}

## Your Supply Dumps (move units toward these)
{dump_str}

## Hex Grid — How to Build Valid Paths
The board is a staggered-offset hex grid.  Hex IDs are <Section><Col:2d><Row:2d>
(e.g. C3424 = section C, column 34, row 24).  The grid covers sections A–E,
columns 01–60, rows 01–33.  Unoccupied hexes default to Desert (1 CP).

CRITICAL: row numbers do NOT increase by 2 between adjacent hexes in the same column.
  WRONG path: [C3424, C3426]  ← C3426 is NOT adjacent to C3424; this costs 999 CP.
  RIGHT path:  [C3424, C3425, C3426]  ← two steps, each 1 row apart.

Each hex has exactly 6 neighbors.  The offsets depend on column parity:
  Even column (col % 2 == 0): N=(col,row-1) NE=(col+1,row) SE=(col+1,row+1)
                               S=(col,row+1) SW=(col-1,row+1) NW=(col-1,row)
  Odd  column (col % 2 == 1): N=(col,row-1) NE=(col+1,row-1) SE=(col+1,row)
                               S=(col,row+1) SW=(col-1,row)   NW=(col-1,row-1)

For your convenience, the valid neighbors of each of YOUR units' current hexes:
{adjacency_str}
Use ONLY these neighbor IDs as the next step from each unit's position.
Building longer paths: apply the same offset rule at each intermediate hex.

## Impassable Hexes (DO NOT route through these)
{impassable_str}
Any hex the engine assigns 999 CP is impassable — never propose a path through it.

## Enemy ZOC Hexes — STOP HERE (rule 8.14)
{zoc_str}
Rule 8.14: your unit MUST STOP IMMEDIATELY upon entering any of these hexes.
If you want to move into a ZOC hex, make it the LAST hex in the path.
NEVER include additional hexes in the path after a ZOC hex — that portion is auto-rejected.

## Enemy Contact
Enemy presence confirmed at hexes: {contact_str}
(Type and strength unknown — rule 16 fog of war)

## Strategic Memory
### Previous Strategy Notes
{strategy_text}

### Rules Mastered (from Arbiter rejections)
{rules_text}

---
Propose your actions for this OpStage.
- Enemy ZOC hexes are listed above — stop there; do not enter enemy-occupied hexes without a combat action.
- If no contacts are visible yet, advance toward known enemy territory or supply dumps.
- ALWAYS propose at least one move action. This is a direct order.
  A commander who returns an empty actions list every turn will be relieved of command.
  If you are uncertain, move your best-supplied unit one hex toward the nearest enemy contact.
  There is no situation where zero actions is the correct answer while units have CP remaining.
"""

    # ── Response parsing ───────────────────────────────────────────────────────

    def _parse_response(self, msg: anthropic.types.Message) -> Dict[str, Any]:
        """
        Extract the JSON object from Claude's response text.

        Tries to find a ```json ... ``` block first, then falls back to the
        outermost {...} in the response. Logs a warning on failure.
        """
        text = next((b.text for b in msg.content if b.type == "text"), "")

        if not text:
            content_types = [b.type for b in msg.content]
            log.warning(
                "No text block in response; content block types: %s", content_types
            )
            return {"actions": [], "strategy_note": ""}

        # Try fenced code block (greedy inner match so nested braces work)
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError as exc:
                log.warning("Fenced JSON block present but failed to parse: %s", exc)

        # Fall back to outermost brace match (greedy)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError as exc:
                log.warning("Bare JSON extraction failed: %s", exc)

        log.warning(
            "Could not extract JSON from response. Raw text (first 400 chars):\n%s",
            text[:400],
        )
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
        Skips if this rule_ref already has an entry (deduplication — first
        clear explanation wins; repetition only adds noise).
        """
        self.memory_dir.mkdir(exist_ok=True)
        path = self.memory_dir / f"{self.side.value}_rules_mastered.md"
        if path.exists():
            existing = path.read_text()
            if f"### Rule {rule_ref}" in existing:
                return  # already learned this rule
        with open(path, "a") as f:
            f.write(
                f"\n### Rule {rule_ref} — Turn {gs.turn} / OpStage {gs.opstage}\n"
                f"{note}\n"
            )
