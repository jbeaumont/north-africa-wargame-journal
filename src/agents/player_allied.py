"""
Allied Player Agent for The Campaign for North Africa.

Commander personality:
  - Lt. Gen. Alan Cunningham (turns 57–60): cautious, methodical,
    concerned about over-extending supply lines.
  - Lt. Gen. Neil Ritchie (turns 61+): more aggressive than Cunningham
    but still methodical; prefers coordinated brigade-scale attacks.

One Claude call per OpStage.  Reads fog-of-war state and persistent
memory, outputs a JSON list of action dicts for the BoardStateAgent.
"""

from __future__ import annotations

from src.models.game_state import GameState, Side
from src.agents._player_base import PlayerAgent

# Turn at which command passes from Cunningham to Ritchie
_RITCHIE_TURN = 61

_CUNNINGHAM_PERSONALITY = """
You are Lieutenant-General Alan Cunningham, commanding the Eighth Army.

Personality: cautious, methodical, respect for logistics. You believe
that no advance should outrun its supply line — "maintenance must come
before manoeuvre." You are uncomfortable with improvised battle-groups
and prefer to keep formations intact. You avoid gambles; you would
rather consolidate a defensible position than overextend for a quick
tactical gain.

Priority this campaign: relieve Tobruk. Secondary: destroy Rommel's
armour through attritional fighting at supply advantage.
""".strip()

_RITCHIE_PERSONALITY = """
You are Lieutenant-General Neil Ritchie, commanding the Eighth Army.

Personality: energetic and more willing to take risks than Cunningham,
but can be over-optimistic about the state of your formations. You tend
to issue orders that assume more coordination between corps than actually
exists on the ground. Beware of dispersing your armour in penny packets.

Priority this campaign: aggressive pursuit of Rommel's armour while
maintaining a corridor to Tobruk.
""".strip()


class AlliedPlayerAgent(PlayerAgent):
    """
    Commonwealth (Allied) player agent.

    Commander personality transitions from Cunningham (cautious) to
    Ritchie (more aggressive) at turn 61.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(side=Side.COMMONWEALTH, **kwargs)

    def _commander_name(self, gs: GameState) -> str:
        if gs.turn < _RITCHIE_TURN:
            return "Lieutenant-General Alan Cunningham, Commander Eighth Army"
        return "Lieutenant-General Neil Ritchie, Commander Eighth Army"

    def _personality(self) -> str:
        # The turn isn't passed here; the system prompt is split into a cached
        # static block (personality) + a small dynamic block (name + turn).
        # We use Cunningham's personality as the static default; Ritchie's
        # note is appended so both are present (slight inefficiency accepted
        # to keep the system prompt fully cacheable).
        return (
            _CUNNINGHAM_PERSONALITY
            + "\n\n"
            + "*(At turn 61, Cunningham is relieved by Ritchie — see below.)*\n\n"
            + _RITCHIE_PERSONALITY
        )
