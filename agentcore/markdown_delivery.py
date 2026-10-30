"""Markdown -> Feishu docx: one implementation behind the plugin's ``md_deliver`` tool.

A ``.md`` attachment opens as source in a Feishu chat (``#``, ``|`` rows and fence markers
show verbatim). Feishu's Drive import pipeline turns the same file into a native ``docx``
document whose headings, tables and lists render as document elements
(``drive-v1/import_task/import-user-guide``). Three platform steps:

1. upload the file as import *material* (``parent_type=ccm_import_open``) with an ``extra``
   JSON of ``{"obj_type": "docx", "file_extension": <real suffix>}``;
2. create the import task from the returned file token;
3. poll the ticket until ``job_status == 0`` and read the document token/URL.

Transport is injected (``Authorization`` header string + URL opener) so the sequencing is
unit-testable without network access, and credentials are never logged.
"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path
import time
from urllib.parse import urlencode
import urllib.error
import urllib.request
import uuid

API = "https://open.feishu.cn/open-apis"
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_MARKDOWN_BYTES = 20 * 1024 * 1024
#: Feishu distinguishes md / markdown / mark; the declared extension must match exactly (error 1069910).
MARKDOWN_SUFFIXES = {".md": "md", ".markdown": "markdown", ".mark": "mark"}
_POLLING_STATUSES = (1, 2)  # 1=initializing, 2=processing


class DeliveryError(RuntimeError):
    """The import has not finished yet. The ticket is the handle for a later resolve."""

    def __init__(self, message: str, ticket: str = ""):
        super().__init__(message)
        self.ticket = ticket


def _opener():
    # Staging is on the public internet: a host proxy that is down must not read as an API error.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _decode(response, action):
    try:
        payload = json.load(response)
    except ValueError:
        raise RuntimeError("feishu %s returned non-JSON" % action) from None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        code = payload.get("code") if isinstance(payload, dict) else None
        raise RuntimeError("feishu %s failed with code %s" % (action, code))
    return payload.get("data") or {}


def _call(method, path, *, authorization, body=None, params=None):
    url = API + path + ("?" + urlencode(params) if params else "")
    request = urllib.request.Request(
        url, method=method,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        headers={"Authorization": authorization, "Content-Type": "application/json; charset=utf-8"})
    try:
        with _opener().open(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            return _decode(response, path)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("feishu http %s on %s" % (exc.code, path)) from None
    except OSError as exc:
        raise RuntimeError("feishu transport failed on %s: %s" % (path, type(exc).__name__)) from None


def import_extension(source):
    """Return (suffix extension, ...) or raise when the file is not importable Markdown."""
    extension = MARKDOWN_SUFFIXES.get(Path(source).suffix.lower())
    if extension is None:
        raise ValueError("only Markdown files can be imported: " + Path(source).name)
    return extension


def check_source(source):
    source = Path(source)
    extension = import_extension(source)
    if not source.is_file():
        raise FileNotFoundError(str(source))
    size = source.stat().st_size
    if size > MAX_MARKDOWN_BYTES:
        raise ValueError("Markdown import limit is 20MB (%d bytes)" % size)
    return extension, source.read_bytes()


def _error_detail(exc):
    """Code + message from a platform error body, never the request (no credentials)."""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
    except (ValueError, OSError):
        return ""
    code, message = payload.get("code"), payload.get("msg") or payload.get("message") or ""
    return ("code=%s %s" % (code, str(message)[:160])).strip()


def upload_material(source, *, authorization):
    """Step 1: multipart material upload carrying the import declaration."""
    extension, content = check_source(source)
    source = Path(source)
    boundary = "----md" + uuid.uuid4().hex
    chunks = []
    for name, value in (("file_name", source.name), ("parent_type", "ccm_import_open"),
                        ("size", str(len(content))),
                        ("extra", json.dumps({"obj_type": "docx", "file_extension": extension}))):
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (boundary, name, value)).encode())
    chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                   "Content-Type: %s\r\n\r\n"
                   % (boundary, source.name, mimetypes.guess_type(source.name)[0] or "text/markdown")).encode())
    chunks.append(content)
    chunks.append(("\r\n--%s--\r\n" % boundary).encode())
    request = urllib.request.Request(
        API + "/drive/v1/medias/upload_all", data=b"".join(chunks), method="POST",
        headers={"Authorization": authorization,
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with _opener().open(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            data = _decode(response, "upload")
    except urllib.error.HTTPError as exc:
        # Keep the platform's own code/message: 99991672 (missing upload scope) and 1069910
        # (extension mismatch) are 400s that an operator must be able to tell apart.
        detail = _error_detail(exc)
        raise RuntimeError("feishu upload rejected: http %s %s" % (exc.code, detail)) from None
    except OSError as exc:
        raise RuntimeError("feishu transport failed on upload: %s" % type(exc).__name__) from None
    token = data.get("file_token")
    if not token:
        raise RuntimeError("feishu upload returned no file_token")
    return token, extension


def create_task(file_token, *, extension, title, parent_token, authorization):
    """Step 2: the import task. Returns its ticket."""
    body = {"file_extension": extension, "file_token": file_token, "type": "docx",
            "file_name": title, "point": {"mount_type": 1, "mount_key": parent_token or ""}}
    ticket = _call("POST", "/drive/v1/import_tasks", authorization=authorization, body=body).get("ticket")
    if not ticket:
        raise RuntimeError("feishu import returned no ticket")
    return ticket


def poll(ticket, *, authorization, wait_seconds=DEFAULT_POLL_SECONDS):
    """Step 3. Raises DeliveryError while the import is still running past the wait."""
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        result = (_call("GET", "/drive/v1/import_tasks/" + ticket, authorization=authorization)
                  .get("result") or {})
        status = result.get("job_status")
        if status == 0:
            token = result.get("token")
            if not token:
                raise RuntimeError("feishu import finished without a document token")
            return {"document_id": token,
                    "url": result.get("url") or ("https://feishu.cn/docx/" + token),
                    "warnings": result.get("extra") or []}
        if status not in _POLLING_STATUSES:
            raise RuntimeError("feishu import failed: job_status=%s %s"
                               % (status, result.get("job_error_msg") or ""))
        if time.monotonic() >= deadline:
            raise DeliveryError("import still running", ticket=ticket)
        time.sleep(min(2.0, max(0.2, deadline - time.monotonic())))


def share_with_org(document_id, *, authorization, allow_fallback: bool = False):
    """Make a bot-created document readable by colleagues in the same organisation.

    Documents created with tenant credentials belong to the app, so without this step even
    the person who asked cannot open the link. Best-effort: callers surface a failure instead
    of handing out a link that does not open. When allow_fallback is True, tenant policy
    rejections are returned as warning strings instead of raising RuntimeError.
    """
    body = {"external_access_entity": "closed", "security_entity": "anyone_can_view",
            "comment_entity": "anyone_can_view", "share_entity": "same_tenant",
            "manage_collaborator_entity": "collaborator_can_view",
            "link_share_entity": "tenant_readable", "copy_entity": "anyone_can_view"}
    data = _call("PATCH", "/drive/v2/permissions/" + document_id + "/public",
                 authorization=authorization, body=body, params={"type": "docx"})
    entity = ((data.get("permission_public") or {}).get("link_share_entity"))
    if entity != "tenant_readable":
        msg = "document share rejected (link_share_entity=%s)" % (entity,)
        if not allow_fallback:
            raise RuntimeError(msg)
        return msg
    return None


def deliver_markdown(source, *, authorization, title="", parent_token="",
                     wait_seconds=DEFAULT_POLL_SECONDS, allow_unshared_fallback: bool = False):
    """Import ``source``; returns ``{"state": "delivered", "url", "document_id", ...}``."""
    source = Path(source)
    file_token, extension = upload_material(source, authorization=authorization)
    ticket = create_task(file_token, extension=extension, title=title or source.stem,
                         parent_token=parent_token, authorization=authorization)
    outcome = poll(ticket, authorization=authorization, wait_seconds=wait_seconds)
    # A link nobody can open is worse than no link: share first, then report delivered.
    warning = share_with_org(outcome["document_id"], authorization=authorization, allow_fallback=allow_unshared_fallback)
    if warning:
        outcome.setdefault("warnings", []).append(warning)
    outcome.update(state="delivered", ticket=ticket, source=str(source))
    return outcome


def resolve_import(ticket, *, authorization, wait_seconds=DEFAULT_POLL_SECONDS, allow_unshared_fallback: bool = False):
    """Finish an import started earlier; the caller owns removing the source file."""
    outcome = poll(ticket, authorization=authorization, wait_seconds=wait_seconds)
    # A pending import resolved on the next turn must be shared exactly like a direct one:
    # the app owns the document, so without this the link is closed to the requester.
    warning = share_with_org(outcome["document_id"], authorization=authorization, allow_fallback=allow_unshared_fallback)
    if warning:
        outcome.setdefault("warnings", []).append(warning)
    outcome.update(state="delivered", ticket=ticket)
    return outcome
