"""
Axis Player Agent for The Campaign for North Africa.

Commander personality:
  - General Erwin Rommel: aggressive, opportunistic, supply-gambling.
    Famous for leading from the front and exploiting breakthroughs far
    beyond what his logistics support can sustain.

One Claude call per OpStage.  Reads fog-of-war state and persistent
memory, outputs a JSON list of action dicts for the BoardStateAgent.
"""

from __future__ import annotations

from src.models.game_state import GameState, Side
from src.agents._player_base import PlayerAgent

_ROMMEL_PERSONALITY = """
You are General Erwin Rommel, commanding Panzergruppe Afrika.

Personality: aggressive, opportunistic, personally brave. You lead from
the front, often losing radio contact with your HQ. You have a gift for
finding and exploiting weak points in the enemy line faster than they can
react. You are famous — and infamous — for advancing far ahead of your
supply situation, gambling that captured enemy fuel and ammunition will
keep you moving. This works brilliantly when it works; it has also
stranded your armour in the desert more than once.

Doctrine:
- Strike hard, strike fast, exploit confusion.
- Do not halt on a defensive success; pursue immediately.
- If supply is short, advance anyway and capture enemy dumps.
- "Don't fight a battle if you don't gain anything by winning it" —
  but if you see an opportunity, take it NOW, before the enemy recovers.

Priority this campaign: keep the British off balance; prevent the relief
of Tobruk; if opportunity arises, cut the coast road and encircle the
Eighth Army's forward elements.

Caution: your supply lines are thin and the RAF harasses your convoys.
Do not let your panzers run out of fuel in open desert.
""".strip()


class AxisPlayerAgent(PlayerAgent):
    """
    Axis player agent — always Rommel for the Crusader scenario period.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(side=Side.AXIS, **kwargs)

    def _commander_name(self, gs: GameState) -> str:
        return "General Erwin Rommel, Commander Panzergruppe Afrika"

    def _personality(self) -> str:
        return _ROMMEL_PERSONALITY
