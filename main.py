"""
main.py — Campaign for North Africa simulation entry point.

Usage:
    python main.py --scenario crusader --turns 1
    python main.py --scenario crusader --turns 3 --no-journal
    python main.py --resume turns/turn_057_state.json --turns 1

Turn loop (per the ARCHITECTURE.md spec):
    For each turn:
        For each OpStage 1–3:
            1. Roll weather
            2. Allied player proposes actions → arbiter validates → board applies
            3. Axis player proposes actions → arbiter validates → board applies
            4. End-of-OpStage bookkeeping (supply checks, fuel evap, pasta)
        End-of-turn bookkeeping
        Journal agent writes narrative markdown

On unhandled exception: saves turns/CRASH_turn_NNN.json and re-raises.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Dict, Any, List

import anthropic

from src.agents.board_state import BoardStateAgent
from src.agents.rules_arbiter import validate_action
from src.agents.player_allied import AlliedPlayerAgent
from src.agents.player_axis import AxisPlayerAgent
from src.agents.journal import JournalAgent
from src.models.unit import Side

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cna")

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent
_TURNS_DIR = _REPO_ROOT / "turns"
_MEMORY_DIR = _REPO_ROOT / "memory"


# ── Action application helpers ────────────────────────────────────────────────

def _apply_validated(
    board: BoardStateAgent,
    player_agent,
    actions: List[Dict[str, Any]],
    client: anthropic.Anthropic,
    side_label: str,
) -> None:
    """
    Validate each proposed action through the Rules Arbiter and apply it
    to the board.  Rejected actions are logged and fed back to the player
    agent's rules-mastered memory so it learns over time.
    """
    for action in actions:
        action_type = action.get("action", "unknown")
        unit_id = action.get("unit_id", "")

        # Only move and combat go through the arbiter; other action types
        # (supply, weather, end_opstage) are engine-internal and always valid.
        if action_type in ("move", "combat"):
            context = board.build_action_context(action)
            verdict = validate_action(action, context, client=client)

            if not verdict.get("valid", False):
                reason = verdict.get("reason", "(no reason)")
                rule_ref = verdict.get("rule_ref", "")
                log.warning(
                    "[%s] Arbiter REJECTED %s for %s: %s (rule %s)",
                    side_label, action_type, unit_id, reason, rule_ref,
                )
                # Teach the agent what it got wrong
                if rule_ref:
                    player_agent.append_rules_learned(
                        rule_ref,
                        f"Rejected {action_type} for {unit_id}: {reason}",
                        board.gs,
                    )
                continue  # skip the rejected action

        result = board.apply_action(action)
        if result.success:
            log.info(
                "[%s] Applied %s for %s",
                side_label, action_type, unit_id or "(engine)",
            )
        else:
            log.warning(
                "[%s] Engine REFUSED %s for %s: %s",
                side_label, action_type, unit_id, result.reason,
            )


# ── End-of-OpStage engine actions ─────────────────────────────────────────────

def _run_opstage_bookkeeping(board: BoardStateAgent) -> None:
    """
    Engine-internal actions that run at the end of every OpStage.
    These are not player proposals — the engine always runs them.
    """
    for action_type in (
        "run_supply_checks",
        "apply_fuel_evaporation",
        "apply_pasta_rule",
        "apply_prisoner_stores",
    ):
        result = board.apply_action({"action": action_type})
        if not result.success:
            log.warning("Bookkeeping action %s failed: %s", action_type, result.reason)


# ── Crash save ────────────────────────────────────────────────────────────────

def _save_crash_state(board: BoardStateAgent) -> None:
    """Write a crash snapshot to turns/CRASH_turn_NNN.json."""
    try:
        _TURNS_DIR.mkdir(parents=True, exist_ok=True)
        crash_path = _TURNS_DIR / f"CRASH_turn_{board.gs.turn:03d}.json"
        crash_path.write_text(json.dumps(board.gs.to_dict(), indent=2))
        log.error("Crash state saved to %s", crash_path)
    except Exception as save_err:
        log.error("Could not save crash state: %s", save_err)


# ── Single turn ───────────────────────────────────────────────────────────────

def run_turn(
    board: BoardStateAgent,
    allied: AlliedPlayerAgent,
    axis: AxisPlayerAgent,
    journal: JournalAgent,
    client: anthropic.Anthropic,
    write_journal: bool,
) -> None:
    """Execute one full game turn (3 OpStages + end-of-turn + optional journal)."""
    turn = board.gs.turn
    log.info("═══ Turn %d (%s) ═══", turn, board.gs.current_date or "unknown date")

    for opstage in range(1, 4):
        board.gs.opstage = opstage
        log.info("--- OpStage %d ---", opstage)

        # 1. Weather roll (engine-internal; no arbiter needed)
        result = board.apply_action({"action": "roll_weather"})
        log.info("Weather: %s", board.gs.weather)

        # 2. Allied player
        log.info("[Allied] Proposing actions…")
        allied_actions = allied.propose_actions(board.gs)
        log.info("[Allied] %d actions proposed", len(allied_actions))
        _apply_validated(board, allied, allied_actions, client, "Allied")

        # 3. Axis player
        log.info("[Axis] Proposing actions…")
        axis_actions = axis.propose_actions(board.gs)
        log.info("[Axis] %d actions proposed", len(axis_actions))
        _apply_validated(board, axis, axis_actions, client, "Axis")

        # 4. End-of-OpStage bookkeeping (supply, fuel, pasta, prisoners)
        _run_opstage_bookkeeping(board)

        # 5. Close the OpStage (writes opstage output files, resets CP)
        result = board.apply_action({"action": "end_opstage"})
        if result.success:
            log.info(
                "OpStage %d closed (%d events)",
                opstage,
                result.data.get("events_count", 0),
            )

    # 6. End-of-turn (writes turn_NNN_state.json + turn_NNN_events.json)
    result = board.apply_action({"action": "end_turn"})
    if result.success:
        log.info(
            "Turn %d complete — %d total events",
            turn,
            result.data.get("events_count", 0),
        )

    # 7. Journal
    if write_journal:
        log.info("[Journal] Writing narrative for turn %d…", turn)
        journal_path = journal.write_turn_journal(turn)
        log.info("[Journal] Written: %s", journal_path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Campaign for North Africa — AI wargame journal simulator",
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--scenario",
        metavar="NAME",
        help="Scenario to load (e.g. crusader, desert_fox, el_alamein)",
    )
    source.add_argument(
        "--resume",
        metavar="STATE_JSON",
        help="Resume from a saved turn state file (e.g. turns/turn_057_state.json)",
    )

    parser.add_argument(
        "--turns",
        type=int,
        default=1,
        metavar="N",
        help="Number of turns to simulate (default: 1)",
    )
    parser.add_argument(
        "--no-journal",
        action="store_true",
        help="Skip journal generation (faster; no Claude API call for narrative)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Initialise ────────────────────────────────────────────────────────────

    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    _TURNS_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Initialising board state…")
    if args.resume:
        board = BoardStateAgent.from_state_file(args.resume)
        log.info("Resumed from %s (turn %d)", args.resume, board.gs.turn)
    else:
        board = BoardStateAgent.from_scenario(args.scenario)
        log.info("Loaded scenario '%s' (turn %d)", args.scenario, board.gs.turn)

    client = anthropic.Anthropic()

    allied = AlliedPlayerAgent()
    axis = AxisPlayerAgent()
    journal = JournalAgent()

    write_journal = not args.no_journal

    # ── Turn loop ─────────────────────────────────────────────────────────────

    for _ in range(args.turns):
        try:
            run_turn(board, allied, axis, journal, client, write_journal)
        except KeyboardInterrupt:
            log.info("Interrupted by user.")
            _save_crash_state(board)
            sys.exit(0)
        except Exception:
            log.error("Unhandled exception during turn %d:", board.gs.turn)
            log.error(traceback.format_exc())
            _save_crash_state(board)
            raise

    log.info("Simulation complete.")


if __name__ == "__main__":
    main()
