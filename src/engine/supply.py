"""
Supply engine — BFS supply-line tracer and resource accounting.

Rules implemented
-----------------
  32.16  Supply line check: trace from unit to friendly Supply Unit within ½ CPA,
         using medium-truck movement costs (infantry movement for non-mot units);
         blocked by impassable terrain and enemy ZOC unoccupied by friendly units.

  32.16  Supply line range check only (see above).
         OOS effects live in 48.0+:
  51.21  1 Disorganization Point per game-turn without Stores.
  51.22  2% TOE Strength Point loss per 2 consecutive game-turns without Stores
         (infantry-type units only).
         NOTE: The 'Critical' supply status label and the two-consecutive-OpStages
         threshold are PLACEHOLDERS — the named 'Critical' status was NOT FOUND in
         the CNA PDF (rules audit 2026-03-05). TODO: replace SupplyStatus.CRITICAL
         with rule 51.21 DP accumulation and rule 51.22 step-loss logic once the
         combat engine can apply DPs and strength point loss.

  49.3   Fuel evaporation (full text confirmed):
           All players:          6% per game-turn, rounded down
           Commonwealth only,    9% per game-turn, Sept 1940 – last GT Aug 1941
             (poor British containers; replaced by copied German jerry can)
           Hot weather declared: +5% additional, taken immediately on declaration
           Note: rule also applies to "certain sources of water" (52.44) — deferred.

  52.6   Pasta rule (full text confirmed):
           Each Italian infantry battalion must receive +1 Water Point when
           Stores are distributed.
           Missing Pasta Point → may NOT voluntarily exceed CPA that Turn.
             (NOT "CPA halved" — that is a web-knowledge error; PDF is explicit.)
           Cohesion ≤ −10 AND no Pasta Point → immediately Disorganized
             as if cohesion reached −26.
           Recovery: as soon as Pasta Point received, regain original Cohesion
             Level (i.e. status restores; cohesion track was never changed).
         TODO: the "as if −26" treatment should also suppress ZOC (rule 10.14).
           hex_map.zoc_hexes() does not yet check pasta_restricted.  Add that
           check when board_state.py is built (step 7).

  28.15  Prisoners: 1 Store Point per 5 Prisoner Points per Operations Stage
         (NOT per game-turn).  Stores are subtracted from the nearest dump;
         "they need not be present" means the draw can go negative (deficit
         is flagged in the event log for the board-state agent to handle).

Usage
-----
    import json
    from src.engine.hex_map import HexMap
    from src.engine.supply import (
        run_supply_checks,
        apply_fuel_evaporation,
        apply_pasta_rule,
        apply_prisoner_stores_cost,
    )

    hmap = HexMap(game_state.hexes, rules["terrain_effects_chart"])

    # Supply-line checks for all combat units
    events = run_supply_checks(game_state, hmap)

    # Fuel evaporation (call once per game-turn during Stores Expenditure Stage)
    events += apply_fuel_evaporation(game_state)

    # Pasta rule (call per Italian battalion after stores are distributed)
    event = apply_pasta_rule(unit, received_pasta_point=True, game_state=game_state)

    # Prisoner stores cost (call per OpStage)
    events += apply_prisoner_stores_cost({"C2215": 12, "B4401": 5}, game_state)
"""

from __future__ import annotations

import heapq
from datetime import date
from typing import Dict, Iterable, List, Optional

from src.engine.hex_map import HexMap
from src.models.event import Event
from src.models.game_state import GameState
from src.models.hex import Hex
from src.models.supply import SupplyDump
from src.models.unit import (
    Nationality,
    Side,
    SupplyStatus,
    Unit,
    UnitSize,
    UnitStatus,
    UnitType,
)


# ── Rule 49.3: fuel evaporation constants ─────────────────────────────────────

# rule 49.3: standard rate — all players, every game-turn, rounded down
EVAP_RATE_STANDARD = 0.06

# rule 49.3: Commonwealth-only rate, Sept 1940 through last GT in August 1941
# "This is due to the poorly constructed containers used by the British; it
#  wasn't until the British copied that German 'jerry can' that their rate was
#  reduced."
EVAP_RATE_CW_EARLY = 0.09
_CW_EARLY_START = date(1940, 9, 1)
_CW_EARLY_END   = date(1941, 8, 31)  # inclusive: "last Game-Turn in August, 1941"

# rule 49.3: additional reduction when hot weather is declared; taken immediately
EVAP_RATE_HOT_WEATHER_BONUS = 0.05

# rule 28.15: 1 Store Point per every 5 Prisoner Points per OpStage
PRISONER_STORE_RATIO = 5


# ── BFS proxy units ────────────────────────────────────────────────────────────

def _truck_proxy(side: Side) -> Unit:
    """
    Minimal Unit proxy representing a medium truck for supply-line BFS.

    Rule 32.16: "The distance is calculated from the unit to the Supply Unit
    as though it is being traversed by a medium Truck unless the unit is a
    non-motorized infantry unit in which case it is traced as infantry movement."

    Nationality is set to a canonical value for the side — it does not affect
    movement costs in hex_map.entry_cost(); only `motorized` matters.
    """
    return Unit(
        id="_supply_bfs_truck",
        name="Medium Truck (BFS proxy)",
        nationality=Nationality.BRITISH if side == Side.COMMONWEALTH else Nationality.GERMAN,
        side=side,
        type=UnitType.TRUCK,
        size=UnitSize.BATTALION,
        motorized=True,
    )


def _infantry_proxy(side: Side) -> Unit:
    """
    Minimal Unit proxy representing foot infantry for supply-line BFS.

    Rule 32.16: used when tracing supply to/from non-motorized infantry units.
    """
    return Unit(
        id="_supply_bfs_inf",
        name="Infantry (BFS proxy)",
        nationality=Nationality.BRITISH if side == Side.COMMONWEALTH else Nationality.GERMAN,
        side=side,
        type=UnitType.INFANTRY,
        size=UnitSize.BATTALION,
        motorized=False,
    )


# ── Supply line check (rule 32.16) ────────────────────────────────────────────

def is_in_supply(
    unit: Unit,
    game_state: GameState,
    hex_map: HexMap,
) -> bool:
    """
    Return True if `unit` has a valid supply line to a friendly Supply Unit.

    Rule 32.16 (full text):
      "A Supply Unit may be drawn upon by any Friendly combat unit if it is
      within one-half (1/2) of that combat unit's CPA.  Thus, a non-motorized
      infantry unit would have to be within 5 CP's of a Supply Unit, while a
      heavy weapons unit being transported by Motorization Points would have to
      be within 10 CP's and a typical recce unit within 23 CP's.  The distance
      is calculated from the unit to the Supply Unit as though it is being
      traversed by a medium Truck unless the unit is a non-motorized infantry
      unit in which case it is traced as infantry movement.  The supply line
      may not be traced thru impassable terrain or enemy ZOC's unoccupied by
      Friendly units."

    Algorithm: Dijkstra BFS from unit's hex outward; stops expanding nodes
    once cost exceeds max_range.  Returns True as soon as a supply-dump hex
    is reached.
    """
    if not unit.hex_id or unit.is_eliminated():
        return False

    # Rule 32.16: supply range is based on the unit's natural (printed) CPA.
    # Rule 6.26: even a Disorganized unit (cohesion -26 / pasta-forced) "may
    # refuel and does consume Stores and Water" — so supply checks must still
    # run for immobile units.  Use formation_cpa_natural() which ignores status
    # penalties, unlike formation_cpa() which returns 0 for DISORGANIZED.
    cpa = game_state.formation_cpa_natural(unit)
    if cpa <= 0:
        # Unit has no CPA assigned; cannot determine supply range.
        return False

    max_range = cpa / 2.0  # rule 32.16: within ½ of the unit's own CPA

    # rule 32.16: medium truck for motorized; infantry movement for non-mot
    tracer = _truck_proxy(unit.side) if unit.motorized else _infantry_proxy(unit.side)
    all_units = list(game_state.units.values())

    # Precompute ZOC once — calling zoc_hexes() inside the BFS loop is O(N)
    # per expansion, making the overall BFS O(N * radius²).  Precomputing
    # reduces the inner loop to O(1) set membership tests.
    enemy_side = Side.AXIS if unit.side == Side.COMMONWEALTH else Side.COMMONWEALTH
    enemy_zoc: set = hex_map.zoc_hexes(enemy_side, all_units)
    # rule 32.16: a friendly unit in the ZOC hex cancels the block
    friendly_occupants: set = {
        u.hex_id for u in all_units
        if u.side == unit.side and u.hex_id and not u.is_eliminated()
    }

    # Dijkstra from unit's hex
    dist: Dict[str, float] = {unit.hex_id: 0.0}
    pq: list = [(0.0, unit.hex_id)]

    while pq:
        cost, hex_id = heapq.heappop(pq)

        if cost > max_range:
            break  # min-heap: all remaining nodes are farther

        if cost > dist.get(hex_id, float("inf")):
            continue  # stale heap entry

        # Check for a friendly (non-dummy) supply dump in this hex
        for dump in game_state.supply_dumps.values():
            if dump.side != unit.side.value:
                continue
            if dump.is_dummy:
                continue  # Axis decoy counters provide no supply
            if dump.hex_id == hex_id:
                return True

        # Rule 32.16: "Supply Unit" means the supply counter on the map, not
        # just static depots.  Supply Unit counters (UnitType.SUPPLY) placed
        # by the scenario or moved by the player also serve as endpoints.
        for su in game_state.units.values():
            if su.side != unit.side:
                continue
            if su.type != UnitType.SUPPLY:
                continue
            if su.is_eliminated():
                continue
            if su.hex_id == hex_id:
                return True

        # Expand to neighbours
        for nbr in hex_map.neighbors(hex_id):
            # rule 32.16: supply line blocked by enemy ZOC unless a friendly
            # unit occupies the ZOC hex
            if nbr in enemy_zoc and nbr not in friendly_occupants:
                continue

            # Impassable terrain blocks supply line
            move_cost = hex_map.entry_cost(tracer, hex_id, nbr)
            if move_cost == "P":
                continue

            new_cost = cost + float(move_cost)  # type: ignore[arg-type]
            if new_cost < dist.get(nbr, float("inf")):
                dist[nbr] = new_cost
                heapq.heappush(pq, (new_cost, nbr))

    return False


# ── OOS status update ─────────────────────────────────────────────────────────

def update_supply_status(unit: Unit, in_supply: bool) -> None:
    """
    Update unit.supply_status and unit.opstages_out_of_supply.

    Actual OOS effects per PDF (rules audit 2026-03-05):
      - Rule 51.21: 1 DP per game-turn without Stores.
      - Rule 51.22: 2% TOE SP loss per 2 consecutive game-turns (infantry only).
    The 'Critical' status label is a PLACEHOLDER — the concept was NOT FOUND in
    the CNA PDF.  The two-consecutive-OpStages → CRITICAL threshold is a
    placeholder until rule 51.21/51.22 effects are properly implemented in the
    combat engine.  TODO: replace with DP accumulation and strength loss.
    """
    if in_supply:
        unit.supply_status = SupplyStatus.IN_SUPPLY
        unit.opstages_out_of_supply = 0
    else:
        unit.opstages_out_of_supply += 1
        if unit.opstages_out_of_supply >= 2:
            unit.supply_status = SupplyStatus.CRITICAL
        else:
            unit.supply_status = SupplyStatus.OUT_OF_SUPPLY


# ── Fuel evaporation (rule 49.3) ──────────────────────────────────────────────

def apply_fuel_evaporation(
    game_state: GameState,
    hot_weather: bool = False,
) -> List[Event]:
    """
    Apply per-turn fuel evaporation to all supply dumps on the map.

    Rule 49.3 rates:
      EVAP_RATE_STANDARD      (0.06) — all players, every game-turn
      EVAP_RATE_CW_EARLY      (0.09) — Commonwealth only, Sept 1940 – Aug 1941
      EVAP_RATE_HOT_WEATHER_BONUS (0.05) — added when hot weather is declared

    Call once per game-turn during the Stores Expenditure Stage.
    Pass hot_weather=True when hot weather is declared for this OpStage; the
    +5% is applied as an additive bonus on top of the base rate in the same call
    (rule 49.3: "taken as soon as the hot weather is determined").

    Unlimited and dummy dumps are unaffected (SupplyDump.apply_fuel_evaporation
    already guards against this).

    Returns a list of fuel_evaporation Events, one per dump that lost fuel.
    """
    current_date = game_state.historical_date()
    events: List[Event] = []

    for dump in game_state.supply_dumps.values():
        if dump.fuel <= 0.0:
            continue

        # rule 49.3: Commonwealth early-war rate Sept 1940 – last GT Aug 1941
        if (
            dump.side == Side.COMMONWEALTH.value
            and _CW_EARLY_START <= current_date <= _CW_EARLY_END
        ):
            rate = EVAP_RATE_CW_EARLY   # 9% Commonwealth early-war
        else:
            rate = EVAP_RATE_STANDARD   # 6% all players

        # rule 49.3: +5% hot-weather bonus (additive, not compounded)
        if hot_weather:
            rate += EVAP_RATE_HOT_WEATHER_BONUS

        lost = dump.apply_fuel_evaporation(rate)
        if lost > 0.0:
            events.append(Event(
                turn=game_state.turn,
                opstage=game_state.opstage,
                type="fuel_evaporation",
                description=(
                    f"{dump.label or dump.id} lost {lost:.1f} fuel to evaporation "
                    f"(rate {rate * 100:.0f}%)"
                ),
                data={"dump_id": dump.id, "rate": rate, "lost": lost},
            ))

    return events


# ── Pasta rule (rule 52.6) ────────────────────────────────────────────────────

def apply_pasta_rule(
    unit: Unit,
    received_pasta_point: bool,
    game_state: GameState,
) -> Optional[Event]:
    """
    Enforce the Italian Pasta Rule for one unit.

    Rule 52.6 (full text confirmed):
      "Each Italian battalion, when it receives its Stores, must receive an
      additional 1 Point of Water when Stores are distributed.  Any
      battalion-sized unit that does not receive their Pasta Point (one Water
      point) may not voluntarily exceed their CPA that Turn.  Furthermore,
      Italian battalions not receiving their Pasta Point that have a Cohesion
      Level of -10 or worse immediately become Disorganized, as if they had
      reached -26.  As soon as such units get their Pasta Point, they regain
      the original Cohesion Level (i.e., the level they had before they
      disintegrated)."

    Notes:
      - "may not voluntarily exceed CPA" is NOT "CPA halved" (web-knowledge error).
      - "as if reached -26" imposes Disorganized status but does NOT change the
        cohesion track; the track retains its actual value.  Recovery simply
        restores ACTIVE status; cohesion is unchanged.
      - The "as if -26" clause also suppresses ZOC per rule 10.14, but
        hex_map.zoc_hexes() does not yet check pasta_restricted (see module TODO).
      - Only applies when unit.pasta_rule == True (Italian infantry battalions).

    received_pasta_point: True if this unit received its +1 Water Point this
      OpStage during stores distribution.

    Returns an Event if the unit's status changed, else None.
    """
    if not unit.pasta_rule or unit.is_eliminated():
        return None

    if received_pasta_point:
        if unit.pasta_restricted:
            unit.pasta_restricted = False
            # rule 52.6: "regain the original Cohesion Level" — pasta-forced
            # Disorganized lifts when Pasta Point is received.  Cohesion track
            # was never changed; restore ACTIVE if cohesion is above -26.
            if unit.status == UnitStatus.DISORGANIZED and unit.cohesion > -26:
                unit.status = UnitStatus.ACTIVE
                return Event(
                    turn=game_state.turn,
                    opstage=game_state.opstage,
                    type="recovery",
                    unit_id=unit.id,
                    description=(
                        f"{unit.name} received Pasta Point; "
                        f"pasta-forced disorganization lifted (cohesion {unit.cohesion})"
                    ),
                    data={"reason": "pasta_rule_restored", "cohesion": unit.cohesion},
                )
        return None

    # ── Missing pasta point ────────────────────────────────────────────────────

    unit.pasta_restricted = True  # movement engine enforces: no voluntary CPA excess

    # rule 52.6: cohesion ≤ -10 AND no Pasta Point → immediately Disorganized
    if unit.cohesion <= -10 and unit.status != UnitStatus.DISORGANIZED:
        unit.status = UnitStatus.DISORGANIZED
        return Event(
            turn=game_state.turn,
            opstage=game_state.opstage,
            type="pasta_rule",
            unit_id=unit.id,
            description=(
                f"{unit.name} missing Pasta Point; cohesion {unit.cohesion} ≤ −10 "
                f"→ immediately Disorganized as if cohesion = −26 (rule 52.6)"
            ),
            data={
                "water_available": 0.0,
                "water_required": 1.0,
                "cohesion": unit.cohesion,
                "forced_disorganized": True,
            },
        )

    # No disorganization trigger; CPA restriction applies only
    return Event(
        turn=game_state.turn,
        opstage=game_state.opstage,
        type="pasta_rule",
        unit_id=unit.id,
        description=(
            f"{unit.name} missing Pasta Point; "
            f"may not voluntarily exceed CPA this Turn (rule 52.6)"
        ),
        data={
            "water_available": 0.0,
            "water_required": 1.0,
            "cohesion": unit.cohesion,
            "forced_disorganized": False,
        },
    )


# ── Prisoner stores cost (rule 28.15) ─────────────────────────────────────────

def apply_prisoner_stores_cost(
    prisoner_points_by_hex: Dict[str, int],
    game_state: GameState,
) -> List[Event]:
    """
    Deduct Store Points for prisoners held on the map.

    Rule 28.15 (full text):
      "For every five Prisoner Points in a hex, the capturing Player must
      expend one Store Point per Operations Stage (not Game-Turn).  These
      Stores are expended before any other stores may be allocated.  They
      need not be present; they are subtracted from the nearest supply dump,
      etc."

    prisoner_points_by_hex: {hex_id → total Prisoner Points}.
    Stores are drawn from the nearest friendly dump (raw hex distance, terrain
    ignored).  If no dump has sufficient stores the deficit is logged in the
    event; the board-state agent is responsible for handling negative stocks.

    Call once per OpStage (NOT per game-turn — rule 28.15 is explicit).

    Returns a list of supply Events summarising stores consumed.
    """
    events: List[Event] = []

    for pris_hex, points in prisoner_points_by_hex.items():
        if points <= 0:
            continue

        stores_needed = points // PRISONER_STORE_RATIO  # rule 28.15: 1 per 5 pts
        if stores_needed <= 0:
            continue

        # Find nearest dump with stores (rule 28.15: "nearest supply dump")
        best_dump: Optional[SupplyDump] = None
        best_dist: float = float("inf")

        try:
            ph = Hex.from_id(pris_hex)
        except Exception:
            ph = None

        for dump in game_state.supply_dumps.values():
            if dump.is_dummy:
                continue
            # Unlimited dumps always have stores; finite dumps need > 0
            if not dump.is_unlimited and dump.stores <= 0.0:
                continue
            try:
                dh = Hex.from_id(dump.hex_id)
                d = float(ph.distance_to(dh)) if ph is not None else 999.0
            except Exception:
                d = 999.0
            if d < best_dist:
                best_dist = d
                best_dump = dump

        if best_dump is None:
            events.append(Event(
                turn=game_state.turn,
                opstage=game_state.opstage,
                type="supply",
                hex_from=pris_hex,
                description=(
                    f"{points} Prisoner Points at {pris_hex}: no dump with stores "
                    f"available; {stores_needed} Store Point(s) owed (rule 28.15)"
                ),
                data={
                    "prisoner_points": points,
                    "stores_owed": stores_needed,
                    "stores_drawn": 0.0,
                },
            ))
            continue

        drawn = best_dump.draw("stores", float(stores_needed))
        events.append(Event(
            turn=game_state.turn,
            opstage=game_state.opstage,
            type="supply",
            hex_from=pris_hex,
            description=(
                f"{points} Prisoner Points at {pris_hex}: drew {drawn:.0f} of "
                f"{stores_needed} Store Point(s) from {best_dump.label or best_dump.id} "
                f"(rule 28.15)"
            ),
            data={
                "prisoner_points": points,
                "stores_owed": stores_needed,
                "stores_drawn": drawn,
                "dump_id": best_dump.id,
            },
        ))

    return events


# ── Top-level supply-check pass ───────────────────────────────────────────────

def run_supply_checks(
    game_state: GameState,
    hex_map: HexMap,
) -> List[Event]:
    """
    Run supply-line checks for all active combat units this OpStage.

    For each active, non-logistics unit:
      1. Determine if it has a valid supply line (rule 32.16).
      2. Update supply_status and opstages_out_of_supply (rule 32.0 / 48.x+).
      3. Emit a 'supply' Event if the status changed.

    Does not handle fuel evaporation, pasta rule, or prisoner stores — call
    those separately at the appropriate stage in the turn sequence.

    Returns list of supply Events.
    """
    events: List[Event] = []

    for unit in game_state.units.values():
        if not unit.is_active():
            continue
        # Supply and truck counters are not combat units; skip
        if unit.type in (UnitType.SUPPLY, UnitType.TRUCK):
            continue

        prev_status = unit.supply_status
        in_supply = is_in_supply(unit, game_state, hex_map)
        update_supply_status(unit, in_supply)

        if unit.supply_status != prev_status:
            events.append(Event(
                turn=game_state.turn,
                opstage=game_state.opstage,
                type="supply",
                unit_id=unit.id,
                hex_from=unit.hex_id,
                description=(
                    f"{unit.name} supply: "
                    f"{prev_status.value} → {unit.supply_status.value}"
                ),
                data={
                    "new_status": unit.supply_status.value,
                    "prev_status": prev_status.value,
                    "opstages_oos": unit.opstages_out_of_supply,
                },
            ))

    return events
