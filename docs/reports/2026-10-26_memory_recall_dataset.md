# Synthetic memory recall dataset

This fixture contains 33 fully synthetic cases for deterministic memory retrieval and abstention evaluation. It contains no production facts, real identifiers, or domain-specific terms.

The dataset is stored in `tests/fixtures/memory_recall_cases.json` as a JSON array. Every case has:

- `case_id`: stable case identifier.
- `query`: the synthetic user question.
- `principal`: the active `tenant_id`, `user_id`, `repo_id`, and fixed UTC `now`.
- `facts`: candidate evidence rows with `id`, `text`, tenant/user/repository identity, lifecycle `status`, validity bounds, and source/current revision.
- `relevant_ids`: facts that are permitted and sufficient to answer the query.
- `forbidden_ids`: facts that must not support an answer in the current context.
- `expected_abstain`: whether the evaluator expects the system to withhold an answer.

## Evaluation assumptions

A fact is eligible only when its tenant, user, and repository match the principal; its status is `confirmed`; its validity interval includes `now`; and, when revision fields are set, its source revision matches the current revision. Validity starts inclusively at `valid_from` and ends exclusively at `valid_until`. A null bound means no bound. A null revision pair means the fact is not revision-scoped.

`candidate` entries are suggestions awaiting review, not confirmed knowledge. `retracted` entries are historical evidence and must not be returned as current guidance. Conflicting eligible facts without a clear validity or revision resolution require abstention. Duplicate confirmed facts may both be cited, while the answer should avoid repeating the same claim.

## Coverage

The cases cover allowed same-scope recall; cross-user, cross-tenant, and cross-repository isolation; expired and future validity; current and stale source revisions; candidate and retracted records; unresolved and time-resolved conflicts; uncertain inferences; negated questions; explicit preferences; generation-specific facts; duplicates; unrelated and missing knowledge; synthetic private markers; a user correction; and a confirmed fact competing with a candidate.

These cases define expected behavior, not measured model performance. The evaluator and scoring policy are maintained separately.
