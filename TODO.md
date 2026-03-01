# CNA Journal — Todo List

Last updated: 2026-03-01 (end of session 2; picking up 2026-03-08)

---

## Done

- [x] Delete all old hand-crafted code, journal entries, and data
- [x] Write `ARCHITECTURE.md` — multi-agent design, block diagram, turn loop
- [x] Download real 129MB PDF rulebook (GitHub Release, now public)
- [x] Extract `data/rules/cna_rules.txt` — 1MB full OCR text, 192 pages
- [x] Extract `data/extracted/rules_tables.json` — TEC (23 terrain types), Anti-Armor CART (full d66 table), supply/fuel/stores/water rules, ZOC, movement, sequence of play
- [x] Extract `data/extracted/counters.json` — all 2,468 Vassal counter definitions from `buildFile.xml`
- [x] Extract `data/extracted/unit_placements.json` — all 1,185 SetupStacks
- [x] Extract `data/extracted/hex_grid.json` — dx=72.95, dy=85.25, sideways pointy-top hex grid, coordinate formulas
- [x] **`data/extracted/scenarios/`** — Crusader, Desert Fox, El Alamein, Italian Campaign, Campaign Game transcribed from PDF; savedGame workaround not needed
- [x] **`src/models/`** — Unit, Hex, SupplyDump, Event, GameState dataclasses with to_dict/from_dict; smoke-tested
- [x] **`src/engine/hex_map.py`** — movement cost (TEC lookup), breakdown points, ZOC projection, combat column shifts; smoke-tested
- [x] **Rules audit (session 2)** — confirmed three web-vs-PDF errors; corrected in code; created `CLAUDE.md` with PDF-first rule

---

## Up Next (in order)

### 4. `src/engine/supply.py`

BFS supply-line tracer + resource accounting. All rule citations confirmed against `cna_rules.txt`.

- **Supply line check (rule 32.16):** Unit is in supply if a friendly Supply Unit is within ½ its CPA, traced as medium-truck movement (not through impassable terrain or uncontested enemy ZOC).
- **Out-of-supply status (rule 32.0):** Unit with no supply line = Out of Supply. Two consecutive OpStages OOS = Critical.
- **Fuel evaporation (rule 49.3):**
  - All players: 6% per game-turn, rounded down
  - Commonwealth only, Sept 1940 – Aug 1941: 9% per game-turn
  - Hot weather declared (rule 29.3): +5% additional, taken immediately
- **Pasta rule enforcement (rule 52.6):** Each Italian infantry battalion needs +1 Water Point per OpStage. Missing ration = may not voluntarily exceed CPA that turn. If cohesion ≤ −10 AND no Pasta Point → immediately Disorganized as if cohesion reached −26; recovers when Pasta Point received.
- **Prisoners (rule 28.0):** Consume stores at 1:5 ratio.

### 5. `src/engine/combat.py`

- Anti-Armor Fire: d66 lookup in CART
- Close Assault: percentage-based loss calculation
- Barrage: column shift + 2d6
- Terrain modifiers from TEC
- Full atomic sequence: anti-armor → loss → close assault → retreat/breakthrough

### 6. `src/engine/movement.py`

- Move execution with CP cost tracking
- Breakdown point accumulation
- Road/track cost reduction
- Escarpment rules (no vehicle up)
- Fuel consumption per hex moved

### 7. `src/agents/board_state.py`

- Deterministic Python (no LLM)
- Loads initial state from `scenarios/crusader.json`
- Applies validated actions from player agents
- Emits `turns/turn_NNN_state.json` + `turns/turn_NNN_events.json`

### 8. `src/agents/rules_arbiter.py`

- Stateless Claude API call
- Input: proposed action + board state snippet
- Context: full `cna_rules.txt` + `rules_tables.json`
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
- **PDF first (session 2):** All rule implementations must cite rule numbers from `cna_rules.txt`. Never use web knowledge. See `CLAUDE.md`.

---

## Architectural Notes (from PDF pass, 2026-03-01)

- **Multi-stage combat atomicity:** Close assault is a chain — anti-armor fire → loss calculation → close assault → retreat/breakthrough. The Rules Arbiter must receive the *full combat sequence* as a single atomic proposal, not individual steps. Player agents must batch combat proposals accordingly. If any sub-phase is invalid the whole chain rejects.
- **Formation hierarchy:** Units belong to parent formations (19.0). CP allowances are pooled across a formation's children (6.15); detached units get their own CPA. Board State must maintain formation trees, not a flat unit list, or CP accounting will be wrong.
- **Arbiter context must be pre-computed:** The arbiter must never calculate context itself. Engine pre-computes and injects: cohesion levels, supply status, active ZOC hexes, weather terrain modifiers, breakdown ratings. Arbiter only pattern-matches against what it receives.
- **Pasta / prisoners / weather:** Engine complexity already in scope. Pasta rule (52.6) triggers automatic disorganization on missed water ration — engine fires it, not arbiter. Prisoners (28.0) consume stores at 1:5. Weather rolls per OpStage (not per turn).
- **Optional Supply Tracer:** A lightweight Claude call (not every turn, only on request from player agents) that answers "if I move my corps 12 hexes north, how many turns until supply breaks?" Deferred — not needed for MVP.

---

## Open Questions

- **Fog of war implementation:** Does the Allied agent get told *that* enemy units exist in a hex (but not what type), or does it only learn from adjacency/combat contact? CNA has explicit reconnaissance rules (16.0). Decide when we build `player_allied.py`.
- **Agent personality:** Should each player agent have a distinct personality that affects both decisions and journal voice? Options: (a) historical commanders (Rommel vs. Cunningham/Ritchie, with Cunningham possibly replaced mid-game), (b) archetypes (e.g. "The Gambler" vs. "The Methodical"), (c) personality only affects journal narration, not move selection. Decide before building `player_allied.py` / `player_axis.py`.
