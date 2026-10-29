# Markdown delivery

Reports written as Markdown should reach the user as an **online document**, never as a
raw `.md` attachment: a chat client opens `.md` as source, so `#`, `|` rows and fence
markers show verbatim — the least readable form of a finished report. The plugin imports
the same file through the document platform's import pipeline and returns the document
link; headings, tables and lists render as document elements.

## Tools

| tool | effect |
| :--- | :--- |
| `md_deliver` | Uploads a `.md` from the transfer outbox as an import material, creates a document import task, polls it, and returns `{"ok": true, "state": "delivered", "message": "<document url>"}`. The source file is removed from the outbox **only** after the URL exists. |
| `md_resolve` | Polls a ticket that was still converting in the previous turn and returns the link; then removes the kept `.md`. |

Pending is a normal outcome: the import is asynchronous. `md_deliver` returns
`{"ok": false, "state": "pending", "ticket": "..."}` and **keeps** the file; the turn
reports the ticket and the next turn calls `md_resolve`. A hard failure returns
`state: "error"` with the platform reason and also keeps the file, so the chat never shows
a link to a document that does not exist. Nothing else about other tools changes; the
`pre_llm_call` rule appears only for requests that ask for a document.

## Deployment settings

```yaml
plugins:
  entries:
    markdown-delivery:
      settings:
        enabled: true
        engine_root: /absolute/path/to/source-checkout
        outbox_root: /absolute/path/to/relay/outbox
        env_file: /absolute/path/to/profile/.env
        folder_token: ""            # optional target folder; empty = personal space root
        # app_id / app_secret override the two credentials read from env_file
```

`engine_root` may be omitted when the engine package is importable from the runtime;
otherwise it points at the single installed source checkout (the same value used by
`illustrated-delivery`). `outbox_root` and `env_file` have no defaults — they are
deployment paths, not repo knowledge. Credentials are exchanged for a short-lived access
token inside the process and are never logged or returned.

## Platform requirements

* The document platform is Feishu. The app needs the import scope (`docs:document:import`,
  or `drive:drive`); without it the import task returns `1069902` and `md_deliver` reports
  `state: "error"`.
* Imported documents belong to the app, so a link is unreadable until it is shared. Both
  `md_deliver` and `md_resolve` PATCH `/drive/v2/permissions/<id>/public?type=docx` to
  `link_share_entity=tenant_readable` and verify the returned value before reporting
  `delivered`; a rejected share fails the delivery instead of handing out a closed link.
  `external_access_entity` stays `closed` (same-tenant readers only); opening it for
  cross-tenant readers is a separate, explicit decision.
* Markdown import limits: 20MB, 20,000 blocks, 2,000 cells and 100 columns per table.
  When the service truncates content it reports warning codes, forwarded verbatim in the
  result's `warnings` field so the answer can say so.
* The importer does **not** fetch images referenced by Markdown. Reports with diagrams
  belong in the illustrated-document renderer (HTML plus paginated previews); hand-written
  `.md` should stay text, tables and code.
