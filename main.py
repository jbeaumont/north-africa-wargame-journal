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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import anthropic

from src.agents.board_state import BoardStateAgent
from src.agents.rules_arbiter import validate_action, mechanical_precheck
from src.agents.player_allied import AlliedPlayerAgent
from src.agents.player_axis import AxisPlayerAgent
from src.agents.journal import JournalAgent
from src.agents._client import make_client
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

# Maximum concurrent arbiter API calls.  After mechanical_precheck eliminates
# ~90% of actions, very few reach the LLM arbiter; 4 workers avoids 429s while
# still parallelising the genuine edge-case checks.
_ARBITER_MAX_WORKERS = 4


def _build_verdicts(
    actions: List[Dict[str, Any]],
    board: BoardStateAgent,
    client: anthropic.Anthropic,
) -> List[Tuple[Dict[str, Any], Dict[str, Any], dict]]:
    """
    Phase 1 of action processing: validate all actions and return verdicts.

    For each action:
      1. Build engine context (deterministic, fast, sequential — board state
         must be consistent for each context snapshot).
      2. Run mechanical_precheck — reject obvious violations without an API call.
      3. Remaining actions are validated in parallel via ThreadPoolExecutor.

    Returns a list of (action, context, verdict) in original proposal order.
    """
    # Step 1 + 2: build contexts and run mechanical pre-checks sequentially.
    # Context building reads board state, so it must stay serial.
    entries: List[Tuple[Dict[str, Any], Dict[str, Any], Optional[dict]]] = []
    for action in actions:
        action_type = action.get("action", "")
        if action_type not in ("move", "combat"):
            # Engine-internal actions (supply, weather, etc.) skip validation.
            entries.append((action, {}, {"valid": True}))
            continue

        context = board.build_action_context(action)
        verdict = mechanical_precheck(action, context)  # None → needs arbiter
        entries.append((action, context, verdict))

    # Step 3: fire arbiter API calls in parallel for those that need it.
    needs_arbiter = [i for i, (_, _, v) in enumerate(entries) if v is None]
    n_precheck_approved = sum(1 for _, _, v in entries if v is not None and v.get("valid"))
    n_precheck_rejected = sum(1 for _, _, v in entries if v is not None and not v.get("valid"))
    log.info(
        "Precheck: %d approved, %d rejected, %d → LLM arbiter",
        n_precheck_approved, n_precheck_rejected, len(needs_arbiter),
    )

    if needs_arbiter:
        n_workers = min(_ARBITER_MAX_WORKERS, len(needs_arbiter))
        log.debug("Firing %d arbiter calls with %d workers", len(needs_arbiter), n_workers)

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            future_to_idx = {
                pool.submit(validate_action, entries[i][0], entries[i][1], client=client): i
                for i in needs_arbiter
            }
            for future in as_completed(future_to_idx):
                i = future_to_idx[future]
                action, context, _ = entries[i]
                entries[i] = (action, context, future.result())

    # All entries now have a verdict.
    return [(a, c, v) for a, c, v in entries]  # type: ignore[misc]


def _apply_validated(
    board: BoardStateAgent,
    player_agent,
    actions: List[Dict[str, Any]],
    client: anthropic.Anthropic,
    side_label: str,
) -> None:
    """
    Validate all proposed actions (in parallel where possible) then apply
    valid ones sequentially to the board.

    Two-phase approach:
      Phase 1 (_build_verdicts): all contexts built + arbiter called in parallel.
      Phase 2: valid actions applied to board state in original order.

    Rejected actions are logged and fed back to the player agent's
    rules-mastered memory so it learns over time.
    """
    # Phase 1: parallel validation (board state not mutated here)
    validated = _build_verdicts(actions, board, client)

    # Phase 2: sequential application (board state mutated here)
    for action, _context, verdict in validated:
        action_type = action.get("action", "unknown")
        unit_id = action.get("unit_id", "")

        if not verdict.get("valid", False):
            reason = verdict.get("reason", "(no reason)")
            rule_ref = verdict.get("rule_ref", "")
            log.warning(
                "[%s] Arbiter REJECTED %s for %s: %s (rule %s)",
                side_label, action_type, unit_id, reason, rule_ref,
            )
            if rule_ref:
                player_agent.append_rules_learned(
                    rule_ref,
                    f"Rejected {action_type} for {unit_id}: {reason}",
                    board.gs,
                )
            continue

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

    Supply checks run each OpStage (rule 32.16 — OOS determined per OpStage).
    Pasta rule fires per OpStage for each Italian infantry battalion.
    Prisoner stores cost is per OpStage (rule 28.15 explicit).

    Fuel evaporation is NOT here — it runs once per game-turn (rule 49.3).
    """
    # Supply checks — all active combat units
    result = board.apply_action({"action": "run_supply_checks"})
    if not result.success:
        log.warning("Bookkeeping action run_supply_checks failed: %s", result.reason)

    # Pasta rule — each Italian infantry battalion individually (rule 52.6)
    # Without a stores-distribution engine, treat all Italian battalions as
    # not receiving their Pasta Point (worst-case; TODO: wire to stores engine).
    from src.models.unit import UnitType, Nationality
    for unit in board.gs.units.values():
        if not unit.is_active():
            continue
        if not unit.pasta_rule:
            continue
        # Determine if this unit has access to water from the nearest dump.
        # Simplified heuristic: in_supply units receive their Pasta Point.
        received = (unit.supply_status.value == "in_supply")
        result = board.apply_action({
            "action": "apply_pasta_rule",
            "unit_id": unit.id,
            "received_pasta_point": received,
        })
        if not result.success:
            log.warning("Pasta rule failed for %s: %s", unit.id, result.reason)

    # Prisoner stores — pass current prisoner map (empty until combat engine
    # tracks prisoners; rule 28.15 has no effect until captures occur).
    result = board.apply_action({
        "action": "apply_prisoner_stores",
        "prisoner_points_by_hex": board.gs.__dict__.get("prisoner_points", {}),
    })
    if not result.success:
        log.warning("Prisoner stores failed: %s", result.reason)


def _run_end_of_turn_bookkeeping(board: BoardStateAgent) -> None:
    """
    Engine-internal actions that run once at the end of each game-turn
    (after all three OpStages complete).

    Fuel evaporation: rule 49.3 says "per game-turn" — not per OpStage.
    Running it three times per turn would drain dumps 3× too fast.
    """
    hot_weather = board.gs.weather == "hot"
    result = board.apply_action({
        "action": "apply_fuel_evaporation",
        "hot_weather": hot_weather,
    })
    if not result.success:
        log.warning("Fuel evaporation failed: %s", result.reason)


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

    # 6. End-of-turn bookkeeping: fuel evaporation (rule 49.3 — once per turn)
    _run_end_of_turn_bookkeeping(board)

    # 7. End-of-turn (writes turn_NNN_state.json + turn_NNN_events.json)
    result = board.apply_action({"action": "end_turn"})
    if result.success:
        log.info(
            "Turn %d complete — %d total events",
            turn,
            result.data.get("events_count", 0),
        )

    # 8. Journal
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

    client = make_client()

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
