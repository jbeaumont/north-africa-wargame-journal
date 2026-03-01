# Claude Code Instructions — Campaign for North Africa

## The Prime Directive: PDF First, Always

This project simulates *The Campaign for North Africa* (SPI, 1979).
The rulebook is notorious for having rules that contradict popular summaries
on BoardGameGeek, Wikipedia, and general wargame resources.
**Never rely on web knowledge or general familiarity with CNA rules.**

### Rule for every implementation decision:

Before writing or modifying any engine rule, mechanic, or constant:

1. **Search `data/rules/cna_rules.txt`** — the full OCR'd PDF text (45,496 lines).
   Use `grep -n "rule_number\|keyword" data/rules/cna_rules.txt` to find the passage.
2. **Cite the rule number** in code comments — e.g. `# rule 49.3`.
3. **Quote the relevant clause** in the comment if it's non-obvious.
4. If the OCR text is ambiguous, note that explicitly in the comment.

The three bugs found in the first code review — all came from web knowledge, not the PDF:

| What the web says | What the PDF (rule) actually says |
|---|---|
| Pasta rule halves CPA | Missing ration = may not *voluntarily exceed* CPA (52.6) |
| 7% drums / 3% jerry cans evaporation | 6% all / 9% CW only Sept'40–Aug'41 / +5% hot weather (49.3) |
| ZOC is type-based (infantry, armor…) | ZOC is size/SP-based; blocked by river/escarpment hexsides (10.11, 10.21) |

These were caught in a rules audit. Others will exist. Assume any rule you
"know" from general wargame knowledge is wrong until confirmed in the PDF.

---

## Where to Find Things

| What you need | Where it lives |
|---|---|
| Full rules text (searchable) | `data/rules/cna_rules.txt` |
| TEC, CRT, supply tables (structured JSON) | `data/extracted/rules_tables.json` |
| All unit counter definitions | `data/extracted/counters.json` |
| Hex grid geometry | `data/extracted/hex_grid.json` |
| Crusader scenario setup | `data/extracted/scenarios/crusader.json` |
| Architecture and agent design | `ARCHITECTURE.md` |
| Task tracking | `TODO.md` |

---

## Code Standards

- **Engine code is deterministic** — no LLM calls in `src/engine/` or `src/models/`.
- **All rule constants must have a rule number comment.**
  Bad:  `EVAP_RATE_CW = 0.09`
  Good: `EVAP_RATE_CW_EARLY = 0.09  # rule 49.3: Commonwealth rate Sept 1940–Aug 1941`
- **Test edge cases against the PDF**, not against intuition.
  When a test passes for a common case, also check the exception clauses in the rule.
- **Stacking Points, not unit types**, drive ZOC — rule 10.11. Do not regress this.
- Before marking any engine module complete, grep `cna_rules.txt` for the rule section
  and confirm all sub-clauses are handled or explicitly deferred with a TODO comment.

---

## Development Branch

All work goes on: `claude/north-africa-wargame-journal-jInb7`
Push with: `git push -u origin claude/north-africa-wargame-journal-jInb7`
