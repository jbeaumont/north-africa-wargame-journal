#!/usr/bin/env python3
"""
The Campaign for North Africa — AI Journal Simulation
======================================================

Entry point. Runs the full 100-turn campaign simulation and generates
a journal entry (via the Claude API) for each turn.

Usage:
    python run_simulation.py                    # Run all 100 turns
    python run_simulation.py --turns 5          # Run first 5 turns only
    python run_simulation.py --start 10 --turns 3  # Run turns 10-12
    python run_simulation.py --dry-run          # Simulate without API calls

Environment:
    ANTHROPIC_API_KEY  Required for journal generation (unless --dry-run)

Output:
    journal/turn_NNN_YYYY-MM-DD.md  — one file per turn
    journal/README.md               — master index
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Load .env if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from cna.data.loader import load_all_ground_units, load_all_supply_counters, load_hex_map
from cna.models.game_state import GameState, turn_to_date_str
from cna.models.counter import Side
from cna.engine.turn import process_turn
from cna.engine.rules_validator import validate_turn
from cna.engine.setup_validator import validate_setup
from cna.journal.generator import generate_journal_entry
from cna.journal.gamesmaster import (
    generate_gamesmaster_ruling, generate_dry_run_ruling,
    generate_setup_ruling, generate_dry_run_setup_ruling,
)
from cna.journal.commanders import (
    generate_commander_update, generate_dry_run_commander_update,
    generate_journal_contribution, generate_dry_run_journal_contribution,
    write_commander_doc,
)
from cna.journal.board_reporter import (
    generate_board_report, generate_dry_run_board_report, write_board_doc,
)
from cna.journal.formatter import write_journal_entry, write_master_index, JOURNAL_DIR


def build_initial_state() -> GameState:
    """Load all data and build the Turn 1 game state."""
    print("Loading hex map...")
    hex_map = load_hex_map()
    print(f"  {len(hex_map)} hexes loaded")

    print("Loading ground units...")
    ground_units = load_all_ground_units()
    print(f"  {len(ground_units)} ground units loaded")

    print("Loading supply counters...")
    supply_counters = load_all_supply_counters()
    print(f"  {len(supply_counters)} supply counters loaded")

    state = GameState(
        turn=1,
        map=hex_map,
        ground_units=ground_units,
        supply_counters=supply_counters,
    )

    # Initialize hex control from initial_controller data
    for h in hex_map.all_hexes():
        if h.initial_controller:
            state.hex_control[h.hex_id] = h.initial_controller

    print(f"Initial hex control: {len(state.hex_control)} hexes")
    return state


def run_simulation(
    state: GameState,
    start_turn: int,
    end_turn: int,
    dry_run: bool = False,
    verbose: bool = True,
) -> None:
    """
    Run the simulation from start_turn to end_turn (inclusive).
    Generates and writes a journal entry for each turn.
    """
    import anthropic as anthropic_module

    client = None
    if not dry_run:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not set. Use --dry-run to skip journal generation.")
            sys.exit(1)
        client = anthropic_module.Anthropic(api_key=api_key)

    completed: list[tuple[int, str, GameState]] = []

    for turn in range(start_turn, end_turn + 1):
        state.turn = turn
        state.events.clear()  # Clear previous turn's events

        date_str = turn_to_date_str(turn)
        if verbose:
            print(f"\n{'='*60}")
            print(f"Turn {turn:3d} / 100 — {date_str}")
            print(f"{'='*60}")

        # === RUN THE TURN ===
        supply_report = process_turn(state)

        # === VALIDATE THE TURN ===
        validation = validate_turn(state)
        if not validation.passed:
            print(f"\n{'!'*60}")
            print(f"  RULES VIOLATION — Turn {turn} halted")
            print(f"{'!'*60}")
            for v in validation.critical:
                print(f"  [CRITICAL] {v.rule_ref}: {v.description}")
            print("\nFix the engine bug above and re-run from this turn.")
            sys.exit(1)
        if verbose and validation.warnings:
            print(f"  Rules: {len(validation.warnings)} warning(s) — "
                  f"passed to GamesmasterAnthony")

        if verbose:
            axis_active = len(state.active_units_for_side(Side.AXIS))
            allied_active = len(state.active_units_for_side(Side.ALLIED))
            print(f"  Axis:   {axis_active} active units")
            print(f"  Allied: {allied_active} active units")
            if supply_report.out_of_supply_units:
                print(f"  OOS:    {', '.join(supply_report.out_of_supply_units[:3])}")
            if supply_report.pasta_deprived_units:
                print(f"  PASTA:  {', '.join(supply_report.pasta_deprived_units[:2])} "
                      f"denied their pasta ration")
            print(f"  Events: {len(state.events)} logged")

        # === GENERATE JOURNAL ENTRY ===
        if dry_run:
            entry_text = _generate_dry_run_entry(turn, state, supply_report)
            ruling = generate_dry_run_ruling(state, validation)
        else:
            if verbose:
                print(f"  Generating journal entry via Claude API...")
            try:
                entry_text = generate_journal_entry(state, client=client)
            except Exception as e:
                print(f"  WARNING: Journal API call failed ({e}), using dry-run fallback")
                entry_text = _generate_dry_run_entry(turn, state, supply_report)
            if verbose:
                print(f"  Generating Gamemaster's Ruling...")
            try:
                ruling = generate_gamesmaster_ruling(state, validation, client=client)
            except Exception as e:
                print(f"  WARNING: Ruling API call failed ({e}), using dry-run fallback")
                ruling = generate_dry_run_ruling(state, validation)

        # === PLAYER JOURNAL CONTRIBUTIONS ===
        axis_notes = ""
        allied_notes = ""
        for side, attr in ((Side.AXIS, "axis_notes"), (Side.ALLIED, "allied_notes")):
            side_label = side.value.title()
            if dry_run:
                notes = generate_dry_run_journal_contribution(state, side)
            else:
                if verbose:
                    print(f"  Generating {side_label} player notes...")
                try:
                    notes = generate_journal_contribution(state, side, client=client)
                except Exception as e:
                    print(f"  WARNING: {side_label} player notes API call failed ({e}), "
                          f"using fallback")
                    notes = generate_dry_run_journal_contribution(state, side)
            if side == Side.AXIS:
                axis_notes = notes
            else:
                allied_notes = notes

        # === WRITE JOURNAL TO FILE ===
        filepath = write_journal_entry(
            turn, entry_text, state, ruling=ruling,
            axis_notes=axis_notes, allied_notes=allied_notes,
        )
        if verbose:
            print(f"  Written: {filepath.name}")

        # === COMMANDER STRATEGIC UPDATES ===
        for side in (Side.AXIS, Side.ALLIED):
            side_label = side.value.title()
            if dry_run:
                cmd_doc = generate_dry_run_commander_update(state, side)
            else:
                if verbose:
                    print(f"  Generating {side_label} commander update...")
                try:
                    cmd_doc = generate_commander_update(state, side, client=client)
                except Exception as e:
                    print(f"  WARNING: {side_label} commander API call failed ({e}), "
                          f"using fallback")
                    cmd_doc = generate_dry_run_commander_update(state, side)
            cmd_path = write_commander_doc(side, cmd_doc)
            if verbose:
                print(f"  Updated: {cmd_path.name}")

        # === BOARD STATE REPORT ===
        if dry_run:
            board_doc = generate_dry_run_board_report(state)
        else:
            if verbose:
                print(f"  Generating board state report...")
            try:
                board_doc = generate_board_report(state, client=client)
            except Exception as e:
                print(f"  WARNING: Board report API call failed ({e}), using fallback")
                board_doc = generate_dry_run_board_report(state)
        board_path = write_board_doc(board_doc)
        if verbose:
            print(f"  Updated: {board_path.name}")

        first_line = entry_text.split("\n")[0][:100]
        completed.append((turn, first_line, state))

        # Rate limiting — be polite to the API
        if not dry_run and turn < end_turn:
            time.sleep(1)

    # === UPDATE MASTER INDEX ===
    index_path = write_master_index(completed)
    if verbose:
        print(f"\nMaster index updated: {index_path}")
        print(f"\nSimulation complete. {len(completed)} turns processed.")


def _run_setup_inspection(
    state: GameState,
    dry_run: bool,
    verbose: bool,
) -> None:
    """
    Run the pre-campaign setup inspection and write the ruling to
    journal/setup_ruling.md.  Halts on critical violations.
    """
    import anthropic as anthropic_module

    if verbose:
        print(f"\n{'='*60}")
        print(f"  PRE-CAMPAIGN SETUP INSPECTION")
        print(f"  GamesmasterAnthony checking the board...")
        print(f"{'='*60}")

    setup = validate_setup(state)

    if verbose:
        print(f"  Checks run: {len(setup.checks_run)}")
        if setup.warnings:
            for w in setup.warnings:
                print(f"  [WARNING] {w.rule_ref}: {w.description}")
        if setup.infos:
            print(f"  {len(setup.infos)} checks passed cleanly")

    if not setup.passed:
        print(f"\n{'!'*60}")
        print(f"  SETUP VIOLATION — cannot start simulation")
        print(f"{'!'*60}")
        for v in setup.critical:
            print(f"  [CRITICAL] {v.rule_ref}: {v.description}")
        print("\nCorrect the setup errors above and re-run.")
        sys.exit(1)

    # Generate the ruling
    if dry_run:
        ruling_text = generate_dry_run_setup_ruling(state, setup)
    else:
        if verbose:
            print(f"  Generating setup ruling via Claude API...")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            ruling_text = generate_dry_run_setup_ruling(state, setup)
        else:
            try:
                client = anthropic_module.Anthropic(api_key=api_key)
                ruling_text = generate_setup_ruling(state, setup, client)
            except Exception as e:
                print(f"  WARNING: Setup ruling API call failed ({e}), using fallback")
                ruling_text = generate_dry_run_setup_ruling(state, setup)

    # Write to journal/setup_ruling.md
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    setup_path = JOURNAL_DIR / "setup_ruling.md"
    setup_path.write_text(ruling_text, encoding="utf-8")

    if verbose:
        warn_note = (
            f" ({len(setup.warnings)} warning(s) noted)"
            if setup.warnings else " (all clear)"
        )
        print(f"  Setup inspection complete{warn_note}")
        print(f"  Written: {setup_path.name}")
    print()


def _generate_dry_run_entry(turn: int, state: GameState, supply_report) -> str:
    """Generate a placeholder journal entry for --dry-run mode."""
    date_str = turn_to_date_str(turn)
    axis_count = len(state.active_units_for_side(Side.AXIS))
    allied_count = len(state.active_units_for_side(Side.ALLIED))

    events_summary = ""
    if state.events:
        notable = [e.description for e in state.events if e.severity in ("notable", "critical")]
        if notable:
            events_summary = "\n\nNotable events:\n" + "\n".join(f"- {d}" for d in notable[:4])

    pasta_note = ""
    if supply_report.pasta_deprived_units:
        pasta_note = (
            f"\n\nThe Italian pasta situation has deteriorated. "
            f"{', '.join(supply_report.pasta_deprived_units[:2])} have been denied their "
            f"water ration for pasta preparation. Cohesion is suffering."
        )

    return f"""Week of {date_str}: the campaign continues its grinding pace.

[DRY RUN — no API call made. This is a placeholder entry.]

Turn {turn} sees {axis_count} Axis units and {allied_count} Allied units active across the front.
The logistics situation {'is under pressure' if supply_report.has_crisis() else 'is manageable'}.
Fuel evaporation this week: {supply_report.fuel_evaporated:.1f} supply points lost to the desert heat.
{events_summary}{pasta_note}

The game's complexity continues to astonish. Every movement consumes fuel.
Every week, the evaporation nibbles away at the carefully hoarded reserves.
The desert does not care about operational plans."""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Campaign for North Africa AI Journal simulation"
    )
    parser.add_argument(
        "--turns", type=int, default=100,
        help="Number of turns to run (default: 100)"
    )
    parser.add_argument(
        "--start", type=int, default=1,
        help="Starting turn number (default: 1)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run simulation without calling Claude API (uses placeholder text)"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress turn-by-turn output"
    )
    args = parser.parse_args()

    end_turn = min(args.start + args.turns - 1, 100)
    print(f"\nCampaign for North Africa — AI Journal")
    print(f"Running turns {args.start} to {end_turn}")
    if args.dry_run:
        print("(DRY RUN — Claude API not called)")
    print()

    state = build_initial_state()

    # === SETUP INSPECTION (Turn 1 only) ===
    if args.start == 1:
        _run_setup_inspection(state, args.dry_run, not args.quiet)

    # Fast-forward state to start_turn (skip processing; just advance turn counter)
    if args.start > 1:
        state.turn = args.start
        print(f"Starting from turn {args.start} (initial state, no pre-processing)")

    run_simulation(
        state=state,
        start_turn=args.start,
        end_turn=end_turn,
        dry_run=args.dry_run,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    main()
