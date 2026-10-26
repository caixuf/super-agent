# T4 integrated delivery report

The user asked for readable, useful documents and appropriate automatic use,
including architecture introductions, explanations and plans. This change
updates the existing rendering skill and engine, adds a temporary per-turn
advice hook, and reviews actual outputs rather than only checking file creation.

## Implemented

- Meaning-based advice supports responsibilities, relationships, stages,
  dependencies, decisions and reusable explanation. User format preferences and
  local follow-ups take precedence. Unknown meanings are left to model judgment.
- The skill requires plain language: lead with the answer, explain roles and
  actions, introduce terms, keep implementation references in the evidence area,
  and distinguish verified facts from plans and uncertainty.
- The renderer waits for diagrams, fonts and layout, checks real SVG and leftover
  code elements, measures full height and captures contiguous readable pages.
  It rejects bad diagrams, stale-output success and horizontal overflow.
- Reusable scripts locate the canonical engine and verify the same browser
  metrics. Four real fixtures include two required failures; no fake SVG stub is
  counted as successful rendering.
- Optional rendering dependencies are declared as the documents extra.

## Evidence

- Real configured-model delivery-choice probe: 14/14 curated scenarios matched
  the expected mode. This checks common and paraphrased requests, not arbitrary
  semantic coverage. Inputs and decisions are retained in the companion JSON.
- Three factual host examples were generated: architecture, phased plan and
  recovery explanation. Each has complete HTML and two PNG pages; each diagram
  rendered with no error or small-label warning. All six PNG pages were visually
  inspected for readable text, meaningful diagrams, continuity and plain writing.
- Installed-skill wrapper was exercised from outside the engine checkout with
  its explicit engine path. Real-browser verification returned complete=true,
  one rendered diagram, no residual code, no overflow and no warnings.
- The live gateway acknowledged pre_llm_call registration. Process ID stayed
  3381565, so no service restart occurred. An audit of configuration preserved
  all unrelated settings; the public enable API reordered the plugin allow-list,
  whose membership was compared independently of order.
- The previous skill and raw configuration were backed up privately in the host
  profile. No credentials or raw configuration are included in this report.

## Limits and remaining acceptance

The hook advises; it cannot force the model to call the renderer or ensure good
writing for every future request. Actual generated artifacts still require
render checks and visual review. Normal user conversations should be observed
to calibrate missed or excessive document creation. User acceptance is pending.

The complete one-click robot package and Docker verification remain the final
project phase, as requested. They were not run during T4.


## Final repository gate

With the real optional FlowCoro binding on PYTHONPATH and temporary databases on
local tmpfs, `bash verify.sh` exited 0: 267 tests passed in 17.64s, no skips,
9 shell syntax checks passed, and installer dry-run exited 0. The count is the
complete current worktree, including other concurrent uncommitted tests. This
commit excludes those unrelated edits. Docker was intentionally deferred.
