# CNA Journal — Todo List

Last updated: 2026-03-01 (decisions logged)

---

## Done

- [x] Delete all old hand-crafted code, journal entries, and data
- [x] Write `ARCHITECTURE.md` — multi-agent design, block diagram, turn loop
- [x] Download real 129MB PDF rulebook (GitHub Release, now public)
- [x] Extract `data/rules/cna_rules.txt` — 1MB full OCR text, 192 pages
- [x] Extract `data/extracted/rules_tables.json` — TEC (23 terrain types, from scanned image), Anti-Armor CART (full d66 table, from scanned image), pasta rule (full text), supply/fuel/stores/water rules, ZOC, movement, sequence of play
- [x] Extract `data/extracted/counters.json` — all 2,468 Vassal counter definitions from `buildFile.xml`
- [x] Extract `data/extracted/unit_placements.json` — all 1,185 SetupStacks (all in OC chart — actual map positions are in encoded savedGame)
- [x] Extract `data/extracted/hex_grid.json` — dx=72.95, dy=85.25, sideways pointy-top hex grid, coordinate formulas, board list

---

## Blocked / Known Issues

- [ ] **Vassal savedGame decoding** — The Crusader scenario savedGame (`data/vassal/crusader/savedGame`) is encoded in Vassal's proprietary command stream format. We can read the header (`!VCS`) and XOR 0xC3 decoding gives partial output, but full decoding is not yet done. **Workaround: transcribe Crusader setup from PDF pages 80–85 manually.**

---

## Up Next (in order)

### 1. `data/extracted/scenarios/crusader.json`
Transcribe the Crusader scenario starting positions from PDF pages 80–85 (already rendered as images last session). Both sides' unit positions, supply dumps, air forces, initial fuel/ammo/stores levels. This is the **critical blocker** — nothing can run without it.

### 2. `src/models/` — Dataclasses
- `unit.py` — Unit dataclass (name, gpid, nationality, type, hex_id, steps, supply status, cohesion, fuel)
- `hex.py` — Hex dataclass (hex_id, terrain, features, units, supply_dumps)
- `game_state.py` — Master state (all units, all hexes, turn number, OpStage, supply levels, event log)

### 3. `src/engine/hex_map.py`
- Hex adjacency (6 neighbors, pointy-top sideways grid)
- Movement cost lookup (from TEC in `rules_tables.json`)
- Distance calculation
- ZOC projection

### 4. `src/engine/supply.py`
- BFS supply path: unit → friendly hexes → depot → port
- Fuel evaporation per OpStage (7% British drums, 3% jerry cans)
- Pasta rule enforcement (Italian battalions need +1 water)
- Out-of-supply status computation

### 5. `src/engine/combat.py`
- Anti-Armor Fire: d66 lookup in CART
- Close Assault: percentage-based loss calculation
- Barrage: column shift + 2d6
- Terrain modifiers from TEC

### 6. `src/engine/movement.py`
- Move execution with CP cost tracking
- Breakdown point accumulation
- Road/track cost reduction
- Escarpment rules (no vehicle up)

### 7. `src/agents/board_state.py`
- Deterministic Python (no LLM)
- Loads initial state from `scenarios/crusader.json`
- Applies validated actions from player agents
- Emits `turns/turn_NNN_state.json` + `turns/turn_NNN_events.json`

### 8. `src/agents/rules_arbiter.py`
- Stateless Claude API call
- Input: proposed action + board state snippet
- Context: `cna_rules.txt` + `rules_tables.json`
- Output: `{"valid": true}` or `{"valid": false, "reason": "...", "rule_ref": "8.37"}`

### 9. `src/agents/player_allied.py` + `player_axis.py`
- Claude API call with fog-of-war game state
- Persistent strategy memory (file-backed between turns)
- Persistent mastery log (rules learned / been burned by)
- Output: structured list of proposed actions

### 10. `src/agents/journal.py`
- Reads completed turn event logs
- Claude API call → first-person narrative markdown
- Output: `journal/turn_NNN_1941-NN-NN.md`

### 11. `main.py` + `requirements.txt`
- Wire up the full turn loop
- CLI: `python main.py --turns 1` to run one turn

---

## Decisions

- **Action granularity:** One Claude call per Operations Stage. The agent proposes all its moves for that stage at once, then the arbiter validates them. Reflects how a commander actually thinks; also much cheaper.
- **Scenario scope:** Land game only until it's solid. Air and naval/logistics modules are planned but deferred. Code should assume they exist (leave hooks) but not implement them yet.
- **Journal tone:** Factual, low-drama, low-hyperbole. Narrative focus is on *reasoning* — what the commander was trying to do, why, what happened. Not a thriller; more like a staff officer's after-action report with some inner voice.

## Open Questions

- **Fog of war implementation:** Does the Allied agent get told *that* enemy units exist in a hex (but not what type), or does it only learn from adjacency/combat contact? CNA has explicit reconnaissance rules (16.0). Decide when we build `player_allied.py`.
- **Agent personality:** Should each player agent have a distinct personality that affects both decisions and journal voice? Options: (a) historical commanders (Rommel vs. Cunningham/Ritchie, with Cunningham possibly replaced mid-game), (b) archetypes (e.g. "The Gambler" vs. "The Methodical"), (c) personality only affects journal narration, not move selection. Decide before building `player_allied.py` / `player_axis.py`.
