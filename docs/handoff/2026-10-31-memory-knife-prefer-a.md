# Handoff — Prefer A memory knife (progressive disclosure + needle monthly log)

**Date:** 2026-10-31 Asia/Shanghai  
**Branch:** `feat/memory-progressive-disclosure`  
**Floor tip / base:** `a1784a0` (re-confirmed `origin/main` before branch)  
**Prefer:** A thin — stack on `agentcore.memory`, do **not** replace store  
**Rails:** `/workspace/contracts/super-agent-memory-knife-prefer-a-rails.md`  
**Owners:** 焊枪 (this PR) · Gate 找茬喵 · Merge 扳手 · 手术刀 only if ABI gap

## Landed

### Progressive disclosure (claude-mem pattern)
- **NEW** `agentcore/memory/progressive.py`
  - **L1** `search(query, …)` → compact index hits: `fact_id · subject · kind/observation_class · created_at · cost_hint` — **no** full statement body; hard hit-count cap
  - **L2** `timeline(ids | query+window)` → chronological stubs (body-light)
  - **L3** `get_observations(ids)` → full statement + tags + details **only** for caller-selected ids (bounded; unknown ids honest)
- Stacks on `memory_store.recall_facts` / new thin `get_facts_by_ids` — **reuses FTS+vec ranking; no second ranker; no schema replace**

### Needle monthly log
- **NEW** `agentcore/memory/monthly_log.py`
- Needle `observation_class ∈ {decision, bugfix, security_alert, sensitive}`
- On admitted write (`write_fact` `written=True` **or** `memory_trigger` written path): append ONE line to `<base_dir>/memory/log/YYYY-MM.md` (Asia/Shanghai calendar month)
- Line: `- YYYY-MM-DD HH:MM · <class> · <subject> · <one-line ≤160> · id=<fact_id>`
- Idempotent by `fact_id`; non-needle skips
- **BAN honored:** no Feishu/IM · no gateway injection · no new model turn for log

### Taxonomy
- `memory_kind.py`: `NEEDLE_CLASSES`, `TAG_OBSERVATION_CLASS`, `classify_observation_class`, `is_needle`
- Existing `kind=technical/personal/context` **unchanged** (not collapsed into observation_class)
- `write_fact` / `memory_trigger.parse_facts` set `observation_class` tag when classifiable

### Prefetch / bridge
- `runtime_bridge.prefetch_context` default inject = **L1** via `progressive.search` + `format_l1_inject` (≪ naive recall dump)
- `plugins/memory_bridge`: optional `memory_search_l1` / `memory_timeline_l2` / `memory_get_observations_l3` tool ops when host `register_tool` exists
- `__init__.py`: minimal export of `progressive`, `monthly_log`, `get_facts_by_ids`

## PARK / BAN (untouched)
Bun / Chroma / CMEM / TeamBrain · OpenMausBot packs · heavy Codex/Temporal · cellular/KAD · store replace · second ranker · SessionStart full RAG dump · Feishu spam · Bot-box private clone

## Tests
- `tests/test_memory_progressive.py` — L1 body-absent+cap · L2 order · L3 full-only-for-ids · stacks on recall_facts
- `tests/test_memory_monthly_log.py` — needle append · idempotent · non-needle skip · trigger path
- Regression: existing `recall_facts` / `write_fact` / `memory_trigger` / `runtime_bridge` tests (prefetch asserts updated for L1 shape)

## Acceptance A0–A10
| ID | Status |
| :--- | :--- |
| A0 | PASS — branch off confirmed tip; WSL + ccp; no box clone |
| A1 | PASS — search body-absent + hard cap |
| A2 | PASS — timeline chronological stubs body-light |
| A3 | PASS — get_observations full-only-for-ids; unknown honest |
| A4 | PASS — prefetch default L1 |
| A5 | PASS — stacks on recall_facts; no second ranker / schema replace |
| A6 | PASS — needle → monthly line |
| A7 | PASS — idempotent by fact_id |
| A8 | PASS — non-needle skip |
| A9 | PASS — no Feishu / model turn in monthly_log |
| A10 | PASS — PARK vendors untouched |

## 手术刀
Not needed — store/recall ABI sufficient for thin facade + `get_facts_by_ids` helper.
