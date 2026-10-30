"""Deliver hand-written Markdown as a readable online document, not a raw `.md` attachment.

A `.md` attachment opens as source in a chat client (`#`, `|` rows and fence markers show
verbatim), which is the poorest form of a finished report. The configured document
backend imports the same Markdown and produces a native document whose headings, tables
and lists render as document elements; the chat receives one readable link.

Two bounded admin tools:

* ``md_deliver``  -- import one Markdown file from the transfer outbox as a document.
* ``md_resolve``  -- poll an import that was still running last turn.

Delivery is all-or-nothing: the source ``.md`` leaves the outbox only when a document URL
was actually obtained. On failure the caller keeps its ``##FILE:`` marker, so the chat never
shows a dead link, and the next turn finishes with ``md_resolve`` instead of re-rendering.

The platform is Feishu. Its Drive import pipeline needs the app credentials from the
active profile's environment file (``FEISHU_APP_ID`` / ``FEISHU_APP_SECRET``); a token is
fetched once per process and never logged or returned.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
#: Only a request that asks for a document pays for the extra guidance line.
_DOCUMENT_REQUEST = re.compile(
    r"文档|报告|写成|整理成|汇总成|归档|纪要|复盘|docx|markdown|\.md", re.I)


def _engine(ctx):
    """Load the one canonical implementation from the administrator's checkout."""
    try:
        from agentcore.markdown_delivery import deliver_markdown, resolve_import
        return deliver_markdown, resolve_import
    except ModuleNotFoundError as error:
        if error.name not in ("agentcore", "agentcore.markdown_delivery"):
            raise
    # The installed runtime can be isolated from the source checkout: load the single
    # administrator-configured copy rather than vendoring a second one.
    root = ctx.get_config("engine_root", "")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError("engine_root must identify the administrator-installed source checkout")
    source = Path(root).resolve(strict=True) / "agentcore/markdown_delivery.py"
    spec = importlib.util.spec_from_file_location("markdown_delivery_engine", source)
    if spec is None or spec.loader is None:
        raise ImportError("markdown delivery engine unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.deliver_markdown, module.resolve_import


def _outbox(ctx):
    """No default path: the transfer outbox is deployment configuration."""
    root = ctx.get_config("outbox_root", "")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError("outbox_root must be the absolute path of the transfer outbox")
    return root


def _env_file(ctx):
    path = ctx.get_config("env_file", "")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("env_file must name the profile environment file holding the app credentials")
    return os.path.expanduser(path.strip())


def _inside(path, root):
    """Deliver only files under the outbox: no ``..``, no symlink escape, no prefix twin."""
    try:
        real_root = os.path.realpath(root)
        return os.path.commonpath([os.path.realpath(path), real_root]) == real_root
    except (OSError, ValueError):
        return False


def _env_value(path, name):
    """Read one key from the profile environment file without executing or interpolating it."""
    try:
        with open(path, encoding="utf-8") as stream:
            for raw in stream:
                line = raw.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, separator, value = line.partition("=")
                if not separator or key.strip() != name:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    return value[1:-1]
                return value.split("#", 1)[0].strip()
    except OSError:
        return None
    return None


class _TenantToken:
    """``tenant_access_token`` lives ~2h; one fetch per process covers a turn burst."""

    def __init__(self, ctx):
        self._ctx = ctx
        self._value = ""
        self._expires_at = 0.0

    def _credentials(self):
        app_id = (self._ctx.get_config("app_id", "") or "").strip()
        app_secret = (self._ctx.get_config("app_secret", "") or "").strip()
        env_file = _env_file(self._ctx)
        app_id = app_id or _env_value(env_file, "FEISHU_APP_ID") or ""
        app_secret = app_secret or _env_value(env_file, "FEISHU_APP_SECRET") or ""
        if not app_id or not app_secret:
            raise RuntimeError("app credentials are not configured for markdown delivery")
        return app_id, app_secret

    def authorization(self):
        if self._value and time.time() < self._expires_at - 60:
            return "Bearer " + self._value
        app_id, app_secret = self._credentials()
        request = urllib.request.Request(
            _TOKEN_URL, method="POST",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=20) as response:
                payload = json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError("token request failed: %s" % type(exc).__name__) from None
        if not isinstance(payload, dict) or payload.get("code") != 0 or not payload.get("tenant_access_token"):
            raise RuntimeError("token request rejected (code=%s)" % (
                payload.get("code") if isinstance(payload, dict) else None))
        self._value = payload["tenant_access_token"]
        self._expires_at = time.time() + float(payload.get("expire") or 7200)
        return "Bearer " + self._value


def _reply(ok, state, message, **extra):
    return json.dumps({"ok": ok, "format": "docx", "state": state, "message": message, **extra},
                      ensure_ascii=False)


def _handler(deliver, resolve, ctx, token):
    def call_deliver(args=None, **kwargs):
        # Hermes dispatches plugin tools as ``handler(args_dict, **kwargs)`` (tools/registry.py),
        # so the argument dict arrives POSITIONALLY; keyword calls must keep working too.
        kwargs = {**kwargs, **(args if isinstance(args, dict) else {})}
        path = kwargs.get("path")
        try:
            root = _outbox(ctx)
        except ValueError as exc:
            return _reply(False, "error", str(exc))
        if not isinstance(path, str) or not path.strip() or not _inside(path, root):
            return _reply(False, "error", "path must be a file inside " + root)
        source = Path(os.path.realpath(path))
        title = kwargs.get("title")
        try:
            authorization = token.authorization()
            outcome = deliver(source, authorization=authorization,
                              title=(title if isinstance(title, str) else "") or source.stem,
                              parent_token=ctx.get_config("folder_token", "") or "")
        except Exception as exc:  # never raise into the model loop
            ticket = getattr(exc, "ticket", "")
            return _reply(False, "pending" if ticket else "error",
                          "%s: %s" % (type(exc).__name__, exc), path=str(source), ticket=ticket)
        # The document exists now; the raw attachment would only duplicate it.
        source.unlink(missing_ok=True)
        return _reply(True, "delivered", outcome.get("url", ""),
                      document_id=outcome.get("document_id", ""),
                      warnings=outcome.get("warnings") or [])

    def call_resolve(args=None, **kwargs):
        kwargs = {**kwargs, **(args if isinstance(args, dict) else {})}
        ticket = kwargs.get("ticket")
        if not isinstance(ticket, str) or not ticket.strip():
            return _reply(False, "error", "ticket is required")
        source = kwargs.get("path")
        try:
            authorization = token.authorization()
            outcome = resolve(ticket.strip(), authorization=authorization)
        except Exception as exc:
            pending = bool(getattr(exc, "ticket", ""))
            return _reply(False, "pending" if pending else "error",
                          "%s: %s" % (type(exc).__name__, exc), ticket=ticket)
        try:
            root = _outbox(ctx)
        except ValueError:
            root = ""
        if root and isinstance(source, str) and source and _inside(source, root):
            Path(source).unlink(missing_ok=True)
        return _reply(True, "delivered", outcome.get("url", ""),
                      document_id=outcome.get("document_id", ""),
                      warnings=outcome.get("warnings") or [])

    return {"md_deliver": call_deliver, "md_resolve": call_resolve}


def _guide(*, user_message="", **kwargs):
    if not isinstance(user_message, str) or not _DOCUMENT_REQUEST.search(user_message):
        return None
    return {"context": (
        "Markdown delivery rule: a finished hand-written .md report must reach the user as an online "
        "document, not as a raw attachment. Write the .md into the return-file directory, then call "
        "md_deliver with that path and put its returned document link in the answer. Do not emit a "
        "##FILE: marker for Markdown and do not paste the whole report again: a document renders "
        "headings, tables and lists; a .md attachment shows source. If md_deliver reports state "
        "pending, say the document is still converting, keep the .md in place, give the ticket, and "
        "finish it next turn with md_resolve(path=..., ticket=...).")}


def register(ctx):
    if ctx.get_config("enabled", False) is not True:
        return
    deliver, resolve = _engine(ctx)
    handlers = _handler(deliver, resolve, ctx, _TenantToken(ctx))
    tools = (
        ("md_deliver",
         "Import a Markdown report from the outbox as an online document; returns its link. "
         "Use this instead of a ##FILE: marker for Markdown reports.",
         {"path": {"type": "string", "description": "Absolute path of a .md file inside the transfer outbox."},
          "title": {"type": "string", "description": "Document title; defaults to the file name."}},
         ["path"]),
        ("md_resolve",
         "Finish a pending Markdown import reported by md_deliver; returns the document link.",
         {"ticket": {"type": "string", "description": "Import ticket reported by md_deliver."},
          "path": {"type": "string", "description": "The .md file kept from the pending md_deliver."}},
         ["ticket"]),
    )
    for name, description, properties, required in tools:
        ctx.register_tool(name=name, toolset="markdown_delivery", description=description,
                          schema={"name": name, "description": description, "parameters": {
                              "type": "object", "additionalProperties": False,
                              "properties": properties, "required": required}},
                          handler=handlers[name])
    ctx.register_hook("pre_llm_call", _guide)
