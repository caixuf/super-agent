# Durable memory runtime and real business analysis

Date: 2026-10-27. Implementation and host evidence; full workstation acceptance remains open.

The analysis runner now accepts a complete JSON fence while still verifying every citation. Provider truncation is a distinct failure, not malformed JSON guessed into a success. The durable pool preserves bounded public runner error codes even on nonzero exit, without exposing arbitrary stdout or stderr. A real read-only source review completed through the resident worker after the host set an explicit low reasoning budget; origin/session routing was persisted. Completion backfill is still unverified.

The new memory-bridge plugin separates text capture from the host terminal verdict. post_llm_call does not certify completion: it creates an awaiting_verdict capture. Only on_session_end with completed=True and neither failed nor interrupted promotes it. A supervised runtime_service admits completed captures to the existing event queue and uses its leased worker. It checks the configured principal on every event, records the event key as fact provenance, and joins background index work before done. Missing verdicts are not guessed after restart.

Automatic extraction requires a verbatim quote from the user's actual message. The stored statement is that quote, not the model's paraphrase. Invalid/truncated extraction and partial writes remain failures for retry; valid empty facts are distinct. An administrator-supplied input resolver can recover the original user input from the transport's trusted metadata. It must reject unknown machine envelopes. The gateway does not import the memory engine or run an extractor inline.

The existing localvec provider owns recall, limited by actual injected characters and fact count. No second recall hook is introduced. Formatting includes fact IDs, deduplicates and truncates to the block budget; logs contain counts and error types instead of conversation/provider error text.

Verification: verify.sh passed with 728 tests and 9 skips before the final diagnostic test; the final focused contracts passed 48 tests. Eight isolated real-model extraction cases passed (four positive decisions/preferences and four abstentions), followed by a new-process recall and user-isolation probe. Isolated embedding was deterministic and does not establish production semantic recall quality.

Production probing caught transport-envelope contamination and unsupported paraphrasing. The two probe facts were deprecated for audit, the trusted original-input resolver was installed, and a later capture stored the exact real user goal. Production capture/admission/extraction succeeded. However, a fresh bare native gateway session still lacked the expected fact ID in its actual API content. A relay-side anchor could mask this failure, so answer similarity is explicitly not acceptance. Standalone recall and an independent MemoryManager succeed; native gateway recall remains FAIL pending observation.

patches/patch_memory_prefetch_observation.py adds fail-open exception-type logging to the host's existing prefetch seam. It is idempotent and refuses anchor drift. Its host source was updated but its live module awaits an idle reload window; busy checks prevented restarting a user's active turn. No Docker/final release was performed.

Final rollout state: capture remains active; the extraction service has been stopped and disabled until native recall and the existing production-quality baseline pass. Queued captures are retained for resumption. This does not pause the overall workstation goal. The existing baseline includes empty recall and an acronym false match; isolated positives do not supersede it.
