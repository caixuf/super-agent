# Memory publication and recall quality gates

Curated documents are indexed verbatim from sectioned Markdown. Each revision has a durable source snapshot. New sections remain candidates until embeddings and, when available, vector rows are present. Publishing a revision and retiring its predecessor is one transaction. A source change during indexing refuses publication.

Named-topic lookup respects the same principal, bucket and scope filters as semantic recall. Indexed tags avoid corpus-wide scans. Whole ASCII identifiers do not match longer identifiers by substring. Ambiguous phrases fall through to semantic ranking.

Automatic facts use the verified user quotation as identity rather than a model-generated subject. Host cron turn IDs are excluded at capture, old-spool admission and queued-event processing; scheduled instructions are not human preferences.

Validation on the deployment host: 745 passed, 9 skipped. Optional native scheduler bindings account for the skips. Focused tests cover interrupted publication, changed sources, cross-user isolation, source-owned retirement, candidate vector repair, lookup scan budgets, repeated quotations and legacy cron records.

Deployment evidence exposed a compression boundary: an externally compressed document changed headings and lost previously successful queries. Reviewed aliases now retain old topic addresses, including CJK names inside questions. Alias metadata has its own immutable snapshot and publishes transactionally with the facts. A verified omitted quotation was indexed separately from the bounded prompt, with its original source hash checked. Changed contracts cannot reuse reviewed aliases.

The updated document passed the original 19 queries in both restored and production stores (12 positive hits, no false hits across 7 negatives, 94.4% returned-item precision). Three fresh native sessions proved actual fact-ID injection without tool calls. The completed-turn consumer is enabled again; all 20 admitted events were done, and no new automatic facts appeared from the three questions. Passing evidence for an older source revision cannot authorize a newer one. These checks do not prove complete information preservation for every compressed section; that broader audit remains open.
