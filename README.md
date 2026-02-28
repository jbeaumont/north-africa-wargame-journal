# The Campaign for North Africa — AI Journal

An AI simulation and journal of Claude playing *The Campaign for North Africa* (CNA),
the 1978 SPI wargame by Richard Berg. Widely regarded as the most complex board game
ever created — 1,500+ hours to complete, three rulebook volumes, a ten-foot map,
and over 1,600 counters.

**The goal is not a playable game.** It is a journal: a first-person narrative
written by Claude documenting its experience managing two vast desert armies across
a three-year campaign, with obsessive fidelity to the game's logistics systems.

---

## The Game

The Campaign for North Africa covers the entire North African theater of WWII
from Italy's invasion of Egypt in September 1940 to the Axis surrender in Tunisia
in May 1943. Ten players are recommended (five per side), each managing a
specialized role: Commander-in-Chief, Logistics Commander, Rear Area Commander,
Air Commander, Front-line Commander.

The game's core mechanic is **logistics**, not combat. Every liter of fuel must
be tracked. Water consumption is calculated per unit per Operations Stage. Fuel
evaporates — 3% per turn for most units, 7% for British forces (they used
50-gallon drums instead of the superior German jerry cans). Supply depots must
physically trace a supply line back to a port.

And then there is the pasta rule.

### The Pasta Rule

Italian infantry battalions require an additional water ration per Operations Stage
to prepare pasta. Units denied their pasta ration have their movement capability
halved. Units whose morale collapses from pasta deprivation become Disorganized.
Designer Richard Berg has admitted this was intentional satire of his own game's
complexity. It is in the simulation anyway.

---

## Project Structure

```
cna-journal/
├── run_simulation.py          # Entry point
├── requirements.txt
├── src/cna/
│   ├── models/
│   │   ├── counter.py         # All unit types (GroundUnit, AirUnit, SupplyCounter)
│   │   ├── hex_map.py         # Hex grid, 31 terrain types, movement costs
│   │   ├── supply.py          # Supply lines, convoy records, supply reports
│   │   └── game_state.py      # Master game state per turn
│   ├── engine/
│   │   ├── movement.py        # CPA movement, fuel consumption, breakdowns
│   │   ├── supply_chain.py    # BFS supply line calc, fuel evaporation
│   │   ├── logistics.py       # Water/ammo/stores, pasta rule, resupply
│   │   ├── combat.py          # 2d6 resolution, terrain modifiers, losses
│   │   ├── air_ops.py         # Air missions, pilot tracking, interdiction
│   │   └── turn.py            # Turn/OpStage orchestrator
│   ├── data/
│   │   └── loader.py          # JSON -> dataclass loaders
│   └── journal/
│       ├── generator.py       # Claude API narrative generator
│       └── formatter.py       # Markdown output writer
├── data/
│   ├── map/
│   │   ├── hexes.json         # 133 hexes, Tripoli to Cairo + Tunisia
│   │   └── locations.json     # Named locations with historical notes
│   └── units/
│       ├── axis_ground.json   # Italian 10th Army + German DAK OOB (47 units)
│       ├── allied_ground.json # British WDF, 8th Army, US II Corps OOB (35 units)
│       ├── axis_air.json      # Regia Aeronautica + Luftwaffe squadrons
│       ├── allied_air.json    # RAF Desert Air Force + USAAF squadrons
│       └── supply.json        # Fuel dumps, water trucks, ammo depots
├── journal/                   # Generated journal entries (one per turn)
└── tests/                     # 32 tests covering all major systems
```

---

## Running the Simulation

### Prerequisites

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here
```

### Run all 100 turns (generates journal via Claude API)

```bash
python run_simulation.py
```

### Dry run (no API calls, placeholder text)

```bash
python run_simulation.py --dry-run
```

### Run a subset of turns

```bash
python run_simulation.py --start 14 --turns 5  # Turns 14-18: DAK arrives
python run_simulation.py --start 61 --turns 3  # Turns 61-63: Operation Torch
```

### Run tests

```bash
python -m pytest tests/ -v
```

---

## Key Simulation Systems

### Supply Chain
Units must trace a path of 5 hexes or fewer through friendly-controlled hexes to a
supply depot. The BFS algorithm respects terrain and enemy hex control.
Out-of-supply units cannot attack and move at reduced capability.

### Fuel Evaporation
Every turn, fuel evaporates from all unit stocks:
- British forces: 7% (50-gallon drums, highly inefficient)
- All others: 3% (German jerry cans, far superior)

This is a historically documented factor in Allied logistics difficulties.

### Movement (Capability Points)
Each unit has a CPA (Capability Point Allowance) per Operations Stage:
- Road: 0.5 CP/hex
- Flat desert: 2.0 CP/hex
- Escarpment/rough: 3.5-4.0 CP/hex
- Mountains: 6.0 CP/hex

Motorized units consume fuel proportional to distance moved. Vehicles can
break down (d6 roll per OpStage; 1 = breakdown, lose 1 step).

### The Pasta Rule
At each Operations Stage start, each Italian infantry battalion needs
water ration + 1 pasta bonus. Without it:
- CPA halved (captured in movement_factor property)
- Cohesion penalty (-2 per deprived OpStage)
- If cohesion <= -10: unit becomes Disorganized

### Combat
Impulse-based; alternating activation. Resolution:
2d6 + terrain_mod + supply_mod + morale_mod + strength_ratio_mod

Attacker loses 2-3 ammo points per combat. Low ammo = -3 attack modifier.

### Air Operations
Individual squadron tracking with airframe condition (0-100%).
Missions: air superiority, ground support, interdiction, reconnaissance.
Pilots gain experience with each sortie. Aircraft wear out; pilots die.

---

## Historical Accuracy

The simulation is seeded with historical OOB data:
- **Turn 1** (Sep 1940): Italian 10th Army invades Egypt
- **Turn 14** (Dec 1940): DAK begins arriving (5th Light Division)
- **Turn 22** (Feb 1941): 15th Panzer Division arrives
- **Turn 35** (May 1941): 90th Light Africa Division arrives
- **Turn 61** (Nov 1942): Operation Torch -- US II Corps lands in Algeria
- **Turn 100** (May 1943): Campaign ends; Axis surrender in Tunisia

Key events (Operation Compass, Rommel's first offensive, Tobruk siege,
Gazala, El Alamein, Torch, and Tunisia) will emerge from simulation dynamics.

---

## The Journal

Journal entries are generated by Claude's claude-opus-4-6 model using
the full game state at the end of each turn as context. The prompt instructs
Claude to write as itself -- an AI encountering this legendary game's complexity
and finding genuine narrative in the logistics.

See `journal/README.md` for the complete index of entries.
