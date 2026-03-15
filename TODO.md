# CNA Journal — Todo List

Last updated: 2026-03-08 (session 6: rules_arbiter.py done; plan revised)

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

## Done (continued)

- [x] **`src/engine/supply.py`** — BFS supply-line tracer (rule 32.16), OOS status (32.0/48.x+),
  fuel evaporation (49.3), pasta rule (52.6), prisoner stores (28.15). All rule citations confirmed
  against `cna_rules.txt` before writing. Smoke-tested.

---

## Up Next (in order)

- [x] **Testing infrastructure** — `tests/conftest.py`, `tests/fixtures/`, oracle-generated fixtures, `test_supply.py`, `test_hex_map.py`, `test_movement.py`, `test_combat.py`, `test_board_state.py`

- [x] **`src/engine/combat.py`** — Anti-Armor Fire (d66 CART), Close Assault, Barrage, terrain modifiers, atomic sequence
- [x] **`src/engine/movement.py`** — CP cost tracking, breakdown points, road/track reduction, escarpment, fuel consumption
- [x] **`src/agents/board_state.py`** — Deterministic dispatcher; loads scenarios; applies actions; emits turn JSON files
- [x] **`src/agents/rules_arbiter.py`** — Stateless Claude API call; validates move/combat against rules_tables.json + cna_rules.txt excerpts; prompt-cached system prompt

---

## Up Next (in order)

### 9a. Fix Formation CPA (rule 6.15) — prerequisite for correct simulation

**Problem**: All scenario units have `cpa=0` because no source data contains counter CPAs.
`formation_cpa()` falls back to the parent formation's CPA (wrong direction — rule 6.15
says the formation CPA = lowest of its children, flowing upward).

**Fix**: Add a `_DEFAULT_CPA` table in `board_state.py` keyed by unit type, backed by
rule citations from `cna_rules.txt`. Apply defaults during scenario loading. Fix
`formation_cpa()` to compute `min(child.cpa)` across formation children (correct direction).
Mark as approximations; proper values require counter-level data extraction from PDF.

Rule citations:
- Non-motorized infantry: CPA 10 (rule 8.17: "units with CPA's of ten or less"; rule 7329)
- Motorized infantry: CPA 20 (rule 7342–7343)
- Armor/armored: CPA 30 (rule 3136: "tank battalion with a CPA of 25"; divisions higher)
- Artillery (guns): CPA 10 for combat (rule 2415: "considered to have a CPA of 10 for combat")
- Reconnaissance: CPA 35 (rule 10866: "CPA of 35 or more")
- HQ/support: CPA 25 (motorized; approximation)

### 9b. Add `build_action_context()` to `board_state.py`

Pre-computes the `context` dict that `validate_action()` expects. Bridges the gap
between the game engine and the Rules Arbiter. Without this, nothing can call the
arbiter with real data.

Fields to compute: unit snapshot, total_cp_cost, zoc_hexes, enemy_occupied_hexes,
stacking_in_destination, stacking_limit, weather, path_hex_costs.

### 9c. Weather roll in `board_state.py`

One-line fix: roll for weather at the start of each OpStage. Sets `gs.weather`.
Uses the weather table from `rules_tables.json`. Currently `hot_weather` flag is
never set, so fuel evaporation is always calculated at the wrong rate.

### 10. `src/agents/player_allied.py` + `player_axis.py`

**Decisions (resolved)**:
- **Fog of war**: Agent sees own units + hex-level enemy presence for adjacent hexes
  ("enemy contact reported at B0407") but not type/strength. Matches rule 16 intent.
  `fog_of_war()` in board_state already hides non-adjacent enemy; add contact-presence
  field for adjacent hexes.
- **Personality**: Historical commanders. Rommel for Axis (aggressive, supply-gambling).
  Cunningham for Allied turns 57–60, Ritchie from turn 61 onward (more methodical).
  Encoded in system prompt; affects both move selection tone and journal voice.

Implementation:
- `claude-opus-4-6`, adaptive thinking, one call per OpStage per side
- Persistent `memory/allied_strategy.md` + `memory/axis_strategy.md` (append-only)
- Persistent `memory/allied_rules_mastered.md` + `memory/axis_rules_mastered.md`
- Output: JSON list of action dicts matching board_state's action schema

### 11. `src/agents/journal.py`

- Reads `turns/turn_{NNN}_state.json` + `turns/turn_{NNN}_events.json`
- `claude-opus-4-6` call → first-person narrative markdown
- YAML front matter: turn, opstage, date, side
- ~400 words; include rejected actions (arbiter refusals) for narrative tension
- Output: `journal/turn_{NNN}_{date}.md`

### 12. `requirements.txt`

Separate task — don't bury in main.py. At minimum: `anthropic`.

### 13. `main.py`

- CLI: `python main.py --scenario crusader --turns 1`
- `--resume turns/turn_057_state.json` to continue from saved state
- Turn loop: for each OpStage → roll weather → Allied proposes → validate+apply →
  Axis proposes → validate+apply → end_opstage
- After OpStage 3: end_turn → journal
- Create `memory/` dir if missing
- On unhandled exception: save `turns/CRASH_turn_NNN.json` + re-raise

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

- ~~Fog of war~~ — Resolved: hex-level presence for adjacent hexes, not type/strength.
- ~~Agent personality~~ — Resolved: historical commanders (Rommel; Cunningham → Ritchie).
