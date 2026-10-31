# Handoff — Prefer B colleague packs (portable team/role pack discipline)

**Date:** 2026-10-31 Asia/Shanghai  
**Branch:** `feat/colleague-packs-prefer-b`  
**Floor tip / base:** `d6e04a7` (re-confirmed `origin/main` before branch)  
**Prefer:** B thin — OpenMausBot **pattern citation only**, map onto `colleague_catalog` / `colleague_dev`  
**Rails:** `/workspace/contracts/super-agent-colleague-packs-prefer-b-rails.md`  
**Owners:** 焊枪 (this PR) · Gate 找茬喵 · Merge 扳手 · 手术刀 only if catalog ABI gap

## Landed

### Portable pack shape
- **NEW** `agentcore/colleague_pack.py`
  - One pack = role roster (`role_id` + catalog `FIELDS`) + optional `skills[]` / `routines[]` references + optional playbook MD
  - Serialization: **JSON preferred** for machine import; YAML-frontmatter+MD optional (soft-dep PyYAML)
  - BAN in pack bytes: secrets · tokens · API keys · cookies · credentials · absolute host paths · Electron/Jev/harness blobs

### Import discipline (additive only)
- `parse_pack` · `redact_pack` · `validate_pack` · `import_additive` · `export_redacted` · `load_pack_file` (absolute path)
- Merge into administrator catalog **snapshot**; never wipe `DEV_ROLES` / `default_dev_catalog`
- Same `role_id` identical FIELDS → idempotent no-op
- Same `role_id` different FIELDS → honest `PackError` (no silent overwrite)
- New role_ids → append under catalog `MAX_ROLES` (ontology only — **not** auto-hire)
- skills → `enabled=false`; routines → `paused=true` metadata-only (`routines_seam=metadata_only`, no scheduler invented)

### Export discipline
- `export_redacted` strips banned keys / secret-shaped strings; skills/routines exported off/paused
- Preserves catalog `NOTICE`: pack text grants **no** dispatch permissions

### Optional plugin tools
- `plugins/colleague_catalog/pack_tools.py` + opt-in hook in `__init__.py`
- Requires `enabled: true` **and** `pack_import_export: true`
- Absolute host path only; model-writable import without host gate = BAN
- Import returns merged snapshot for the host to persist (does not mutate plugin settings from the model path)

## PARK / BAN (untouched)
Jev · Electron/Chief of Staff · Lightpanda · Bun/Chroma/CMEM · Temporal · cellular·KAD · Feishu card rework · memory Prefer A rework · auto-hire · silent overwrite · vendor OpenMausBot wholesale · skills default on · routines auto-schedule · Bot-box private clone

## Tests
- `tests/test_colleague_pack.py` + fixtures under `tests/fixtures/colleague_pack/`
- Covers B1–B7 + B10 notice + absolute path gate + catalog regression
- Catalog plugin existing tests remain green (pack tools default off)

## Acceptance B0–B10
| ID | Status |
| :--- | :--- |
| B0 | PASS — branch off confirmed tip `d6e04a7`; WSL + ccp; no box clone |
| B1 | PASS — parse/validate via catalog FIELDS / ROLE_PATTERN / size |
| B2 | PASS — secret keys/values / abs paths reject; redact strips |
| B3 | PASS — skills land enabled=false |
| B4 | PASS — routines paused metadata-only + deliver-or-reason (no scheduler seam) |
| B5 | PASS — additive under MAX_ROLES; existing preserved; DEV_ROLES untouched |
| B6 | PASS — conflict reject; identical idempotent |
| B7 | PASS — export redacted; skills off / routines paused |
| B8 | PASS — no OpenMausBot / Electron / Jev / Lightpanda / Bun vendor trees |
| B9 | PASS — Feishu card + memory Prefer A modules untouched (diff review) |
| B10 | PASS — NOTICE: text grants no dispatch permissions |

## 手术刀
Not needed — catalog ABI (`normalize_catalog` / `FIELDS` / `ROLE_PATTERN` / `MAX_ROLES` / `NOTICE`) sufficient for thin pack import. No registry/auto-hire change.
