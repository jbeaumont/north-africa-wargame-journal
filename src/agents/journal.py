"""
Journal Agent for The Campaign for North Africa.

Reads the turn output files written by BoardStateAgent.write_turn_output()
and generates a ~400-word first-person narrative markdown file for each turn.

Input files:
  turns/turn_{NNN}_state.json    — full GameState snapshot at turn end
  turns/turn_{NNN}_events.json   — merged event log for all 3 OpStages

Output:
  journal/turn_{NNN}_{YYYY-MM-DD}.md   — YAML front matter + narrative prose

Model: claude-opus-4-6, adaptive thinking, streaming.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic
from src.agents._client import make_client

# ── Paths ──────────────────────────────────────────────────────────────────────

_REPO_ROOT   = Path(__file__).parent.parent.parent
_TURNS_DIR   = _REPO_ROOT / "turns"
_JOURNAL_DIR = _REPO_ROOT / "journal"

# ── System prompt (prompt-cached; does not change turn-to-turn) ───────────────

_SYSTEM_PROMPT = """
You are the campaign historian for Operation Crusader, Libya, 1941–1943.

Your task: write a single journal entry covering one full game-turn (~8–9 days)
from the perspective of a senior staff officer present at the events.

STYLE GUIDE
-----------
- Factual, low-drama, low-hyperbole. This is a staff officer's after-action
  report with measured inner voice — not a thriller.
- Narrative focus: what the commanders were *trying* to do, why, and what
  actually happened. Cause-and-effect, not just a list of moves.
- First person plural ("We advanced on…", "The 7th Armoured pushed…",
  "Corps HQ received word that…").
- ~400 words. Don't pad; don't truncate significant events.
- If any orders were refused by higher authority (arbiter rejections), include
  them — they reveal what commanders wanted to do and add narrative tension.
- End with one sentence assessing the situation at turn's end.

OUTPUT
------
Return ONLY the journal body. No YAML front matter. No code fences. No preamble.
Plain markdown prose; paragraphs separated by blank lines.
""".strip()

# Event types that are always worth including in full
_PRIORITY_TYPES = frozenset({
    "combat",
    "elimination",
    "supply",
    "disorganization",
    "breakdown",
    "pasta_rule",
    "arbiter_rejection",
    "weather_roll",
})

# Cap on minor (non-priority) events shown per OpStage, to limit prompt size
_MINOR_EVENT_CAP = 8


# ── Journal Agent ──────────────────────────────────────────────────────────────

class JournalAgent:
    """
    Generates narrative turn journals from turn output files.

    One claude-opus-4-6 call per turn.  Adaptive thinking is enabled so
    Claude reasons about how to frame the events before writing prose.
    Streaming is used to avoid HTTP timeouts on longer narratives.
    """

    def __init__(
        self,
        journal_dir: Optional[Path] = None,
        turns_dir:   Optional[Path] = None,
    ) -> None:
        self.journal_dir = journal_dir or _JOURNAL_DIR
        self.turns_dir   = turns_dir   or _TURNS_DIR
        self._client     = make_client()

    # ── Public interface ───────────────────────────────────────────────────────

    def write_turn_journal(self, turn: int) -> Path:
        """
        Read turn files, call Claude, write the journal entry.

        Returns the path to the written markdown file.
        Raises FileNotFoundError if the turn state or events file is missing.
        """
        state, events = self._load_turn_files(turn)
        user_msg      = self._build_user_message(state, events)
        narrative     = self._call_claude(user_msg)
        return self._write_output(state, narrative)

    # ── File loading ───────────────────────────────────────────────────────────

    def _load_turn_files(self, turn: int):
        state_path  = self.turns_dir / f"turn_{turn:03d}_state.json"
        events_path = self.turns_dir / f"turn_{turn:03d}_events.json"

        if not state_path.exists():
            raise FileNotFoundError(f"State file not found: {state_path}")
        if not events_path.exists():
            raise FileNotFoundError(f"Events file not found: {events_path}")

        with open(state_path)  as f: state  = json.load(f)
        with open(events_path) as f: events = json.load(f)
        return state, events

    # ── Prompt construction ────────────────────────────────────────────────────

    def _build_user_message(self, state: dict, events: list) -> str:
        turn         = state.get("turn", 0)
        current_date = state.get("current_date", "unknown")
        weather      = state.get("weather", "normal")
        initiative   = state.get("initiative", "unknown")

        situation   = self._state_summary(state)
        events_text = self._format_events(events)

        rejections = [e for e in events if e.get("type") == "arbiter_rejection"]
        rejection_section = ""
        if rejections:
            items = "\n".join(
                f"- {e.get('description', '(no description)')}"
                for e in rejections
            )
            rejection_section = f"\n## Rejected Orders\n{items}\n"

        return (
            f"## Turn {turn} — {current_date}\n"
            f"Weather: {weather} | Initiative: {initiative}\n\n"
            f"## Situation at Turn End\n{situation}\n\n"
            f"## Events This Turn\n{events_text}"
            f"{rejection_section}\n"
            f"Write the journal entry for Turn {turn}."
        )

    def _state_summary(self, state: dict) -> str:
        """
        Compact situation summary from the end-of-turn state dict.

        Mirrors GameState.narrative_summary() but works from the raw JSON
        so the journal can run without a live GameState object.
        """
        units = state.get("units", {})

        def count(side: str, oos: bool) -> int:
            return sum(
                1 for u in units.values()
                if u.get("side") == side
                and u.get("status") != "eliminated"
                and bool(u.get("hex_id"))
                and (u.get("supply_status") != "in_supply") == oos
            )

        cw_active = count("commonwealth", oos=False) + count("commonwealth", oos=True)
        ax_active = count("axis", oos=False) + count("axis", oos=True)
        cw_oos    = count("commonwealth", oos=True)
        ax_oos    = count("axis",         oos=True)

        lines = [
            f"Commonwealth: {cw_active} active units"
            + (f", **{cw_oos} out-of-supply**" if cw_oos else ", all supplied"),
            f"Axis: {ax_active} active units"
            + (f", **{ax_oos} out-of-supply**" if ax_oos else ", all supplied"),
        ]

        # Surface notable OOS unit names (up to 5)
        def oos_names(side: str) -> List[str]:
            return [
                u.get("name", uid)
                for uid, u in units.items()
                if u.get("side") == side
                and u.get("supply_status") != "in_supply"
                and u.get("status") != "eliminated"
            ][:5]

        for side, label in (("commonwealth", "CW"), ("axis", "Axis")):
            names = oos_names(side)
            if names:
                lines.append(f"{label} OOS: {', '.join(names)}")

        return "\n".join(lines)

    def _format_events(self, events: List[dict]) -> str:
        """
        Format events grouped by OpStage.

        Priority event types are listed with bold type labels and shown in
        full.  Minor events are capped at _MINOR_EVENT_CAP per OpStage to
        keep prompt size manageable.
        """
        # Group by opstage
        by_opstage: Dict[int, List[dict]] = {}
        for e in events:
            by_opstage.setdefault(e.get("opstage", 0), []).append(e)

        if not by_opstage:
            return "(no events recorded)"

        sections: List[str] = []
        for opstage in sorted(by_opstage):
            stage_events = by_opstage[opstage]
            priority = [e for e in stage_events if e.get("type") in _PRIORITY_TYPES]
            minor    = [e for e in stage_events if e.get("type") not in _PRIORITY_TYPES]

            lines = [f"### OpStage {opstage}"]
            for e in priority:
                lines.append(
                    f"- **{e.get('type', '?')}**: {e.get('description', '')}"
                )
            for e in minor[:_MINOR_EVENT_CAP]:
                lines.append(f"- {e.get('description', '')}")
            if len(minor) > _MINOR_EVENT_CAP:
                lines.append(f"- *(+{len(minor) - _MINOR_EVENT_CAP} minor events)*")

            sections.append("\n".join(lines))

        return "\n\n".join(sections)

    # ── Claude call ────────────────────────────────────────────────────────────

    def _call_claude(self, user_message: str) -> str:
        """
        Stream a claude-opus-4-6 response with adaptive thinking.

        Returns the text content of the final message.
        """
        with self._client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=1024,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            msg = stream.get_final_message()

        return next(
            (b.text for b in msg.content if b.type == "text"),
            "(no narrative generated)",
        )

    # ── Output ────────────────────────────────────────────────────────────────

    def _write_output(self, state: dict, narrative: str) -> Path:
        """Write the journal file with YAML front matter."""
        self.journal_dir.mkdir(parents=True, exist_ok=True)

        turn         = state.get("turn", 0)
        current_date = state.get("current_date", "unknown")
        weather      = state.get("weather", "normal")
        initiative   = state.get("initiative", "unknown")

        front_matter = (
            f"---\n"
            f"turn: {turn}\n"
            f'date: "{current_date}"\n'
            f"weather: {weather}\n"
            f"initiative: {initiative}\n"
            f"---\n\n"
        )

        output_path = self.journal_dir / f"turn_{turn:03d}_{current_date}.md"
        output_path.write_text(front_matter + narrative + "\n")
        return output_path
