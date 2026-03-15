# Campaign for North Africa — AI Journal Architecture

## Overview

A multi-agent simulation of *The Campaign for North Africa* (SPI, 1979).
The goal is a journal — a first-person narrative written by Claude documenting
its experience managing two vast desert armies — not a playable game UI.

---

## Agent Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                       MULTI-AGENT FRAMEWORK                         │
│                                                                     │
│  ┌───────────────────────┐         ┌───────────────────────┐        │
│  │   ALLIED PLAYER AGENT │         │   AXIS PLAYER AGENT   │        │
│  │                       │         │                       │        │
│  │  Decision Engine      │         │  Decision Engine      │        │
│  │  Strategy Memory      │         │  Strategy Memory      │        │
│  │  Mastery Log          │         │  Mastery Log          │        │
│  │  (fog-of-war view)    │         │  (fog-of-war view)    │        │
│  └──────────┬────────────┘         └────────────┬──────────┘        │
│             │ proposed action                    │ proposed action   │
│             └─────────────────┬──────────────────┘                  │
│                               │                                     │
│                               ▼                                     │
│              ┌────────────────────────────────┐                     │
│              │      RULES ARBITER AGENT       │                     │
│              │                                │                     │
│              │  Loaded from rulebook data     │                     │
│              │  Validates: legal move?        │                     │
│              │  Checks: stacking, ZOC,        │                     │
│              │    supply, terrain, phase      │                     │
│              │  Returns: valid | invalid+why  │                     │
│              └────────────────┬───────────────┘                     │
│                               │                                     │
│              ┌────────────────▼───────────────┐                     │
│              │  BOARD STATE + ENGINE          │                     │
│              │  (deterministic Python, no LLM)│                     │
│              │                                │                     │
│              │  Single source of truth        │                     │
│              │  Sourced from REAL board data  │                     │
│              │  Applies validated actions     │                     │
│              │  Computes supply paths         │                     │
│              │  Resolves combat               │                     │
│              │  Emits structured event log    │                     │
│              └────────────────┬───────────────┘                     │
│                               │                                     │
└───────────────────────────────┼─────────────────────────────────────┘
                                │
                    ┌───────────▼────────────┐
                    │   OUTPUT LAYER         │
                    │  turn_NNN_events.json  │
                    │  turn_NNN_state.json   │
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │    JOURNAL AGENT       │
                    │  (Separate process)    │
                    │  Reads event logs      │
                    │  Generates narrative   │
                    └────────────────────────┘
```

---

## What Is (and Isn't) an Agent

Not everything labelled here is an LLM call. The distinction matters for cost,
reliability, and reproducibility:

| Component | LLM? | Reason |
|---|---|---|
| `player_allied.py` | Yes — Claude API | Strategy requires judgment |
| `player_axis.py` | Yes — Claude API | Strategy requires judgment |
| `rules_arbiter.py` | Yes — Claude API | Rule interpretation requires reasoning |
| `journal.py` | Yes — Claude API | Prose generation requires language |
| `board_state.py` | **No** — deterministic Python | Applying rules has one correct answer |
| `engine/hex_map.py` | **No** | Geometry and lookup tables |
| `engine/supply.py` | **No** | BFS graph traversal |
| `engine/movement.py` | **No** | Cost accounting |
| `engine/combat.py` | **No** | Dice + table lookup |

The engine and board state are the **ground truth** layer. LLM agents propose
and narrate; the engine enforces and records. An LLM call is never used where
a deterministic answer exists.

---

## Agent Descriptions

### Allied Player Agent (`src/agents/player_allied.py`)
- Claude API call with fog-of-war game state (only sees own units + observed enemy)
- Maintains persistent **Strategy Memory** — multi-turn plan, current objectives
- Maintains **Mastery Log** — what rules it has learned / been burned by
- Outputs a structured list of proposed actions for the turn (move, attack, supply, etc.)
- Does NOT know enemy positions unless revealed by reconnaissance or combat

### Axis Player Agent (`src/agents/player_axis.py`)
- Same structure as Allied, opposite side
- Separate strategy memory and mastery log
- German and Italian sub-commanders have distinct personalities and priorities

### Rules Arbiter Agent (`src/agents/rules_arbiter.py`)
- Claude API call with full rulebook context (`data/extracted/rules_tables.json` + `data/rules/cna_rules.txt`)
- Receives a proposed action + current board state snippet
- Returns: `{"valid": true}` or `{"valid": false, "reason": "...", "rule_ref": "6.53"}`
- Is the **only** agent that reads the rulebook — ground truth for legality
- Stateless: each call is independent

### Board State Agent (`src/agents/board_state.py`)
- **Deterministic Python** — no LLM calls
- Single source of truth for all game state
- Applies only validated actions (Rules Arbiter must approve first)
- Runs supply path calculation (BFS from unit → depot → port)
- Resolves combat (dice + modifiers from rules tables)
- Tracks fuel evaporation, water consumption, ammo expenditure, pasta rations
- Emits structured event log at end of each Operations Stage and Turn
- Reads initial state from `data/extracted/` (real Vassal data)

### Journal Agent (`src/agents/journal.py`)
- Separate process — runs after Board State Agent completes a turn
- Reads `turns/turn_NNN_events.json` and `turns/turn_NNN_state.json`
- Claude API call: writes first-person narrative from Claude's perspective
- No game logic — pure narrative generation
- Output: `journal/turn_NNN_YYYY-MM-DD.md`

---

## Data Sources (Ground Truth)

All game data must derive from primary sources. No hand-crafted stats.

| Source | Extracts | Output |
|--------|----------|--------|
| `data/vassal/buildFile.xml` | All counter definitions (unit type, steps, SVG) | `data/extracted/counters.json` |
| PDF rulebook (GitHub Release) | Rules text, CRT, TEC, supply tables, scenario setups | `data/extracted/rules_tables.json` |
| PDF rulebook | Sequence of play, stacking limits, ZOC rules | `data/rules/cna_rules.txt` (already extracted) |
| `data/vassal/CNA Map Vassal Mitch Guthrie 2021.png` | Visual reference | Used for rendering only |
| buildFile.xml HexGrid params | Hex grid geometry (dx=72.95, dy=85.25, sideways) | `data/extracted/hex_grid.json` |
| PDF Scenarios section | Crusader scenario starting positions | `data/extracted/scenarios/crusader.json` |

---

## Directory Structure

```
cna-journal/
├── ARCHITECTURE.md              # This file
├── data/
│   ├── assets.json              # External asset URLs (PDF, map)
│   ├── rules/
│   │   └── cna_rules.txt        # Full text extracted from PDF
│   ├── map/
│   │   ├── CNA Map Vassal Mitch Guthrie 2021.png
│   │   └── (hex coordinate mapping TBD)
│   ├── vassal/
│   │   ├── buildFile.xml        # Vassal module — counter library
│   │   └── crusader/            # Vassal crusader scenario save
│   └── extracted/               # Generated by tools/ — do not edit by hand
│       ├── counters.json        # All units from buildFile.xml
│       ├── hex_grid.json        # Hex grid geometry
│       ├── rules_tables.json    # CRT, TEC, supply tables from PDF
│       └── scenarios/
│           └── crusader.json    # Starting positions for Crusader
├── tools/                       # One-time extraction scripts
│   ├── parse_vassal.py          # buildFile.xml → counters.json
│   ├── extract_hex_grid.py      # buildFile.xml → hex_grid.json
│   └── extract_pdf_tables.py    # PDF → rules_tables.json
├── src/
│   ├── agents/
│   │   ├── board_state.py       # Deterministic; single source of truth
│   │   ├── rules_arbiter.py     # Claude API; validates against real rules
│   │   ├── player_allied.py     # Claude API; fog-of-war view
│   │   ├── player_axis.py       # Claude API; fog-of-war view
│   │   └── journal.py           # Claude API; narrative generation
│   ├── engine/
│   │   ├── hex_map.py           # Hex adjacency, movement cost
│   │   ├── supply.py            # BFS supply path calculation
│   │   ├── movement.py          # Move execution, fuel consumption
│   │   └── combat.py            # CRT lookup, loss application
│   └── models/
│       ├── game_state.py        # Master game state
│       ├── unit.py              # Unit dataclass
│       └── hex.py               # Hex dataclass
├── turns/                       # Structured output (one pair per turn)
│   ├── turn_001_state.json
│   ├── turn_001_events.json
│   └── ...
├── journal/                     # Narrative output (one per turn)
│   ├── turn_001_1941-11-18.md
│   └── ...
├── main.py                      # Entry point
└── requirements.txt
```

---

## Turn Loop

```
for each turn:
  1. Board State Agent emits fog-of-war snapshots for each side
  2. Allied Player Agent reads its snapshot → proposes actions
  3. For each proposed action:
       Rules Arbiter validates → if valid, Board State applies it
  4. Axis Player Agent reads its snapshot → proposes actions
  5. For each proposed action:
       Rules Arbiter validates → if valid, Board State applies it
  6. Board State resolves: supply, fuel evaporation, pasta, combat
  7. Board State writes: turn_NNN_state.json + turn_NNN_events.json
  8. [Separate] Journal Agent reads events → writes turn_NNN.md
```

---

## Scenario: Operation Crusader (November 1941)

The simulation begins at Operation Crusader — the British 8th Army offensive
that relieved Tobruk — because it is the best-documented scenario in the
Vassal module and represents the game at peak complexity (DAK fully deployed,
Tobruk garrison active, supply lines stretched on both sides).

Turn 1 = 18 November 1941 (the actual historical date of the Crusader offensive).

---

## Key Rules to Implement (in priority order)

1. **Supply** — trace path ≤N hexes through friendly hexes to depot to port
2. **Fuel evaporation** — 6% all players per turn; 9% Commonwealth only Sept 1940–Aug 1941; +5% hot weather (rule 49.3)
3. **Movement** — CPA per terrain type, road bonus, fuel consumption
4. **Stacking** — limits per hex per side
5. **ZOC** — zones of control, movement restrictions
6. **Combat** — CRT lookup, terrain modifiers, supply modifiers
7. **The pasta rule** — Italian infantry water + pasta ration per OpStage
8. **Air operations** — interdiction, ground support, air superiority

---

## Notes

- The Board State Agent is **deterministic** — no LLM. Reproducible results.
- The Rules Arbiter is the **only** agent that reads the rulebook.
- Player Agents see only **fog-of-war** — they cannot cheat.
- The Journal Agent is a **consumer** — it reads outputs, generates prose.
- All `data/extracted/` files are generated by `tools/` scripts and should be
  regenerated from source if the extraction logic changes.
