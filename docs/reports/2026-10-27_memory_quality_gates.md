# Memory publication and recall quality gates

Curated documents are indexed verbatim from sectioned Markdown. Each revision has a durable source snapshot. New sections remain candidates until embeddings and, when available, vector rows are present. Publishing a revision and retiring its predecessor is one transaction. A source change during indexing refuses publication.

Named-topic lookup respects the same principal, bucket and scope filters as semantic recall. Indexed tags avoid corpus-wide scans. Whole ASCII identifiers do not match longer identifiers by substring. Ambiguous phrases fall through to semantic ranking.

Automatic facts use the verified user quotation as identity rather than a model-generated subject. Host cron turn IDs are excluded at capture, old-spool admission and queued-event processing; scheduled instructions are not human preferences.

Validation on the deployment host: 745 passed, 9 skipped. Optional native scheduler bindings account for the skips. Focused tests cover interrupted publication, changed sources, cross-user isolation, source-owned retirement, candidate vector repair, lookup scan budgets, repeated quotations and legacy cron records.

Deployment evidence also exposed an important unresolved acceptance boundary: an externally compressed document changed headings and lost previously successful queries. Its new revision failed evaluation, so the automatic consumer remains disabled. Passing evidence for an older source revision cannot authorize a newer one. Compression must preserve queryable facts as well as reduce prompt size.
